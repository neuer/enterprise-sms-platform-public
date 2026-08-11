"""uncertain 分片基于 raw 报文证据的受控修复。"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from app.services.crypto import CryptoService, EncryptionContext
from app.services.runtime_policy import RuntimePolicy
from app.vendor.identifiers import protect_vendor_task_id


@dataclass(frozen=True, slots=True)
class UncertainChunk:
    chunk_id: int
    custom_id: str
    uncertain_since: datetime


@dataclass(frozen=True, slots=True)
class RawCandidate:
    raw_id: int
    source: str
    payload_enc: bytes
    payload_sha256: str
    key_version: int


class UncertainRepository(Protocol):
    async def list_uncertain(self) -> list[UncertainChunk]: ...

    async def raw_candidates(self, custom_id: str) -> list[RawCandidate]: ...

    async def resolve_submitted(self, chunk_id: int, task_id: str) -> None: ...

    async def alert_overdue(self, chunk: UncertainChunk) -> None: ...


def utc_now() -> datetime:
    return datetime.now(UTC)


class UncertainReconciler:
    """没有解密后匹配的 customId 证据就绝不迁移或重发。"""

    def __init__(
        self,
        repository: UncertainRepository,
        crypto: CryptoService,
        *,
        clock: Callable[[], datetime] = utc_now,
        alert_after: timedelta = timedelta(hours=24),
    ) -> None:
        self.repository = repository
        self.crypto = crypto
        self.clock = clock
        self.alert_after = alert_after

    @classmethod
    def from_policy(
        cls,
        repository: UncertainRepository,
        crypto: CryptoService,
        policy: RuntimePolicy,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> UncertainReconciler:
        return cls(
            repository,
            crypto,
            clock=clock,
            alert_after=timedelta(hours=policy.uncertain_alert_hours),
        )

    def _task_id(self, candidate: RawCandidate, custom_id: str) -> str | None:
        try:
            payload = self.crypto.decrypt_bound_bytes(
                candidate.payload_enc,
                candidate.key_version,
                EncryptionContext(
                    domain="vendor-raw",
                    table="raw_vendor_log",
                    column="payload_enc",
                    object_id=f"{candidate.source}:{candidate.payload_sha256}",
                ),
            )
            document = json.loads(payload)
        except (ValueError, UnicodeError):
            return None
        if not isinstance(document, dict) or not isinstance(document.get("data"), list):
            return None
        for item in document["data"]:
            if (
                isinstance(item, dict)
                and item.get("customId") == custom_id
                and isinstance(item.get("taskId"), str)
                and item["taskId"]
            ):
                return str(item["taskId"])
        return None

    async def run_once(self) -> int:
        resolved = 0
        now = self.clock()
        for chunk in await self.repository.list_uncertain():
            task_id: str | None = None
            for candidate in await self.repository.raw_candidates(chunk.custom_id):
                task_id = self._task_id(candidate, chunk.custom_id)
                if task_id is not None:
                    break
            if task_id is not None:
                _raw_task_id, task_pseudonym = protect_vendor_task_id(
                    self.crypto,
                    task_id,
                )
                await self.repository.resolve_submitted(
                    chunk.chunk_id,
                    task_pseudonym,
                )
                resolved += 1
            elif now - chunk.uncertain_since >= self.alert_after:
                await self.repository.alert_overdue(chunk)
        return resolved
