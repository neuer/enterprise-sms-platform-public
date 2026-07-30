"""已落地 raw 厂商报文的完整性校验与受控重放。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

from app.services.crypto import EncryptionContext


class RawReplayNotFound(LookupError):
    pass


class RawReplayConflict(RuntimeError):
    pass


class RawIntegrityConflict(RawReplayConflict):
    pass


@dataclass(frozen=True, slots=True)
class RawReplayRecord:
    id: int
    source: str
    payload_enc: bytes
    payload_sha256: str
    key_version: int
    processed: bool


@dataclass(frozen=True, slots=True)
class RawReplayClaim:
    record: RawReplayRecord
    claimed: bool


class RawReplayRepository(Protocol):
    async def claim_raw_for_replay(self, raw_id: int) -> RawReplayClaim | None: ...

    async def mark_replay_error(self, raw_id: int, error: str) -> None: ...

    async def audit_raw_replay(
        self,
        raw_id: int,
        *,
        source: str,
        items: int,
        actor: str,
        ip: str,
    ) -> None: ...


class RawCrypto(Protocol):
    def decrypt_bound_bytes(
        self,
        payload: bytes,
        key_version: int,
        context: EncryptionContext,
        *,
        allow_legacy: bool = True,
    ) -> bytes: ...


class ExistingRawProcessor(Protocol):
    async def process_existing(self, raw_id: int, data: object) -> int: ...


class RawReplayService:
    """重放只在内存解密，响应和审计仅返回无 PII 元数据。"""

    def __init__(
        self,
        repository: RawReplayRepository,
        crypto: RawCrypto,
        reports: ExistingRawProcessor,
        replies: ExistingRawProcessor,
    ) -> None:
        self.repository = repository
        self.crypto = crypto
        self.reports = reports
        self.replies = replies

    async def _integrity_error(self, raw_id: int, message: str) -> None:
        await self.repository.mark_replay_error(raw_id, message)

    async def replay(self, raw_id: int, *, actor: str, ip: str) -> int:
        claim = await self.repository.claim_raw_for_replay(raw_id)
        if claim is None:
            raise RawReplayNotFound(raw_id)
        if not claim.claimed:
            if claim.record.processed:
                raise RawReplayConflict("仅未处理 raw 可重放")
            raise RawReplayConflict("raw 正在处理中，请稍后重试")
        record = claim.record
        if record.processed:
            raise RawReplayConflict("仅未处理 raw 可重放")
        raw = self.crypto.decrypt_bound_bytes(
            record.payload_enc,
            record.key_version,
            EncryptionContext(
                domain="vendor-raw",
                table="raw_vendor_log",
                column="payload_enc",
                object_id=f"{record.source}:{record.payload_sha256}",
            ),
        )
        if hashlib.sha256(raw).hexdigest() != record.payload_sha256:
            await self._integrity_error(raw_id, "raw payload integrity mismatch")
            raise RawIntegrityConflict("raw payload integrity mismatch")
        try:
            document = json.loads(raw)
        except (ValueError, UnicodeError):
            await self._integrity_error(raw_id, "raw payload is not valid JSON")
            raise RawIntegrityConflict("raw payload is not valid JSON") from None
        if not isinstance(document, dict) or document.get("code") != 0 or "data" not in document:
            await self._integrity_error(raw_id, "raw vendor envelope is invalid")
            raise RawIntegrityConflict("raw vendor envelope is invalid")
        if record.source == "report":
            count = await self.reports.process_existing(raw_id, document["data"])
        elif record.source == "reply":
            count = await self.replies.process_existing(raw_id, document["data"])
        else:
            await self._integrity_error(raw_id, "raw source is invalid")
            raise RawIntegrityConflict("raw source is invalid")
        await self.repository.audit_raw_replay(
            raw_id,
            source=record.source,
            items=count,
            actor=actor,
            ip=ip,
        )
        return count
