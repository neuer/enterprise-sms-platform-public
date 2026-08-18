"""GetReply 完整 raw 密文先落库，再解析为无明文手机号的回复对象。"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from app.services.crypto import CryptoService, EncryptionContext
from app.services.masking import mask_phone_text
from app.services.raw_spill import RawSpillStore
from app.vendor.identifiers import (
    protect_vendor_custom_id,
    protect_vendor_task_id,
    validate_vendor_custom_id,
    validate_vendor_ext_code,
)
from app.vendor.zhihui import (
    RawPulledPayload,
    VendorResponseTooLarge,
    decode_pulled_payload,
)

LOGGER = logging.getLogger(__name__)
VENDOR_LOCAL_TIME = re.compile(
    r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?"
)
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True, slots=True)
class ProtectedReply:
    vendor_task_id: str
    custom_id: str | None
    match_custom_id: str | None
    phone_enc: bytes
    phone_hmac: str
    phone_mask: str
    key_version: int
    phone_hmacs: tuple[str, ...]
    ext_code: str
    content_enc: bytes
    reply_time: datetime
    dedup_hash: str
    dedup_key_version: int


class ReplyGateway(Protocol):
    async def get_reply_raw(self) -> RawPulledPayload: ...


class ReplyRepository(Protocol):
    async def persist_raw(self, **values: Any) -> int: ...

    async def update_metadata(
        self,
        raw_id: int,
        *,
        custom_ids: list[str],
        item_count: int,
    ) -> None: ...

    async def filter_known_custom_ids(self, custom_ids: list[str]) -> list[str]: ...

    async def store_reply(self, raw_id: int, reply: ProtectedReply) -> None: ...

    async def mark_processed(self, raw_id: int) -> None: ...

    async def mark_error(self, raw_id: int, error: str) -> None: ...


def _normalized(item: dict[str, Any]) -> dict[str, Any]:
    return {str(key).strip(): value for key, value in item.items()}


def _collect_vendor_custom_ids(data: list[dict[str, Any]]) -> list[str]:
    """逐条校验 customId；非法值跳过，避免一条坏数据打断整批索引。"""

    collected: set[str] = set()
    for item in data:
        raw = _normalized(item).get("customId")
        if not isinstance(raw, str):
            continue
        try:
            collected.add(validate_vendor_custom_id(raw))
        except ValueError:
            continue
    return sorted(collected)


class ReplyIngestService:
    """严格保持 persist_raw 提交先于任何业务结构解析。"""

    def __init__(
        self,
        gateway: ReplyGateway | None,
        repository: ReplyRepository,
        crypto: CryptoService,
        *,
        alerts: Any | None = None,
        spill: RawSpillStore | None = None,
    ) -> None:
        self.gateway = gateway
        self.repository = repository
        self.crypto = crypto
        self.alerts = alerts
        self.spill = spill

    def _parse(self, item: dict[str, Any]) -> ProtectedReply:
        value = _normalized(item)
        required = {"taskId", "phone", "contents", "replyTime"}
        if not required.issubset(value):
            raise ValueError("reply fields are incomplete")
        raw_task_id, task_id = protect_vendor_task_id(self.crypto, value["taskId"])
        custom_value = value.get("customId")
        if custom_value is not None and not isinstance(custom_value, str):
            raise ValueError("customId must be a string or null")
        raw_custom_id: str | None = None
        custom_id: str | None = None
        if custom_value is not None:
            raw_custom_id, custom_id = protect_vendor_custom_id(self.crypto, custom_value)
            if not raw_custom_id:
                raw_custom_id = None
                custom_id = None
        content = value["contents"]
        if not isinstance(content, str) or not 1 <= len(content) <= 500:
            raise ValueError("contents length must be between 1 and 500")
        # 平台发送链路从不设置 extCode，且业务投影不消费该字段。仅校验厂商
        # 合同后丢弃，避免把 4–6 位 OTP 伪装成扩展号写入明文元数据列。
        validate_vendor_ext_code(value.get("extCode", ""))
        ext_value = ""
        raw_reply_time = str(value["replyTime"])
        try:
            reply_time = datetime.fromisoformat(raw_reply_time.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("replyTime format is invalid") from None
        if reply_time.tzinfo is None:
            if VENDOR_LOCAL_TIME.fullmatch(raw_reply_time) is None:
                raise ValueError("replyTime must include timezone")
            reply_time = reply_time.replace(tzinfo=SHANGHAI_TIMEZONE)
        if reply_time.utcoffset() is None:
            raise ValueError("replyTime timezone must define an offset")
        phone = str(value["phone"])
        protected = self.crypto.protect_phone(phone, table="reply_event")
        hmac_candidates = self.crypto.hmac_candidates(phone)
        masked_content = mask_phone_text(content)
        dedup_source = "\x1f".join(
            (
                raw_task_id,
                raw_custom_id or "",
                hmac_candidates[min(hmac_candidates)],
                content,
                reply_time.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            )
        )
        legacy_digest = hashlib.sha256(dedup_source.encode("utf-8")).digest()
        dedup_key_version, dedup_hash = self.crypto.stable_hmac_fingerprint(
            legacy_digest,
            domain="reply-event",
        )
        content_enc = self.crypto.encrypt_bound_packed_text(
            masked_content,
            EncryptionContext(
                domain="reply-content",
                table="reply_event",
                column="content_enc",
                object_id=dedup_hash,
            ),
        )
        return ProtectedReply(
            vendor_task_id=task_id,
            custom_id=custom_id,
            match_custom_id=raw_custom_id,
            phone_enc=protected.phone_enc,
            phone_hmac=protected.phone_hmac,
            phone_mask=protected.phone_mask,
            key_version=protected.key_version,
            phone_hmacs=tuple(hmac_candidates.values()),
            ext_code=ext_value,
            content_enc=content_enc,
            reply_time=reply_time,
            dedup_hash=dedup_hash,
            dedup_key_version=dedup_key_version,
        )

    async def _alert_consume_gap(self, error_type: str) -> None:
        if self.alerts is None:
            return
        try:
            await self.alerts.emit(
                alert_type="vendor_raw_persist_failed",
                level="crit",
                title="厂商拉走即消费响应未能落库，需人工介入",
                detail={"source": "reply", "error_type": error_type},
                dedup_key="vendor_raw_persist_failed:reply",
            )
        except Exception as exc:
            LOGGER.error(
                "vendor raw persist alert unavailable",
                extra={"source": "reply", "error_type": type(exc).__name__},
            )

    async def recover_spills(self) -> int:
        """把落库前崩溃留下的加密 spill 恢复进 raw_vendor_log。"""

        if self.spill is None:
            return 0
        recovered = 0
        for record in self.spill.list_pending():
            if record.source != "reply":
                continue
            try:
                await self.repository.persist_raw(
                    payload_enc=record.payload_enc,
                    payload_sha256=record.payload_sha256,
                    key_version=record.key_version,
                    http_status=record.http_status,
                    content_encoding=record.content_encoding,
                    custom_ids=[],
                    item_count=0,
                )
            except Exception as error:
                await self._alert_consume_gap(type(error).__name__)
                continue
            self.spill.remove(record.source, record.payload_sha256)
            recovered += 1
        return recovered

    async def poll_once(self) -> int:
        if self.gateway is None:
            raise RuntimeError("reply gateway is not configured")
        await self.recover_spills()
        try:
            pulled = await self.gateway.get_reply_raw()
        except VendorResponseTooLarge as error:
            await self._alert_consume_gap(type(error).__name__)
            raise
        except Exception as error:
            await self._alert_consume_gap(type(error).__name__)
            raise
        payload_sha256 = hashlib.sha256(pulled.raw_payload).hexdigest()
        encrypted = self.crypto.encrypt_bound_bytes(
            pulled.raw_payload,
            EncryptionContext(
                domain="vendor-raw",
                table="raw_vendor_log",
                column="payload_enc",
                object_id=f"reply:{payload_sha256}",
            ),
        )
        if self.spill is not None:
            self.spill.write(
                source="reply",
                payload_sha256=payload_sha256,
                key_version=encrypted.key_version,
                http_status=pulled.status_code,
                content_encoding=pulled.content_encoding,
                payload_enc=encrypted.payload,
            )
        try:
            raw_id = await self.repository.persist_raw(
                payload_enc=encrypted.payload,
                payload_sha256=payload_sha256,
                key_version=encrypted.key_version,
                http_status=pulled.status_code,
                content_encoding=pulled.content_encoding,
                custom_ids=[],
                item_count=0,
            )
        except Exception as error:
            await self._alert_consume_gap(type(error).__name__)
            raise
        if self.spill is not None:
            self.spill.remove("reply", payload_sha256)
        try:
            data = decode_pulled_payload(pulled, "GetReply")
            if isinstance(data, list) and all(isinstance(item, dict) for item in data):
                custom_ids = _collect_vendor_custom_ids(data)
                custom_ids = await self.repository.filter_known_custom_ids(custom_ids)
                await self.repository.update_metadata(
                    raw_id,
                    custom_ids=custom_ids,
                    item_count=len(data),
                )
        except Exception as error:
            await self.repository.mark_error(
                raw_id,
                f"{type(error).__name__}: vendor response parsing failed",
            )
            raise
        return await self.process_existing(raw_id, data)

    async def process_existing(self, raw_id: int, data: object) -> int:
        """解析已独立提交的 reply raw；轮询与受控重放共用。"""

        try:
            if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
                raise ValueError("GetReply.data must be an object array")
            skipped = 0
            for index, item in enumerate(data):
                try:
                    reply = self._parse(item)
                except (ValueError, KeyError, TypeError) as error:
                    skipped += 1
                    LOGGER.warning(
                        "skipping invalid reply item",
                        extra={
                            "raw_id": raw_id,
                            "item_index": index,
                            "error_type": type(error).__name__,
                        },
                    )
                    continue
                await self.repository.store_reply(raw_id, reply)
        except Exception as error:
            await self.repository.mark_error(
                raw_id,
                f"{type(error).__name__}: reply parsing failed",
            )
            raise
        if skipped:
            await self.repository.mark_error(
                raw_id,
                f"skipped {skipped} invalid reply items",
            )
            if self.alerts is not None:
                try:
                    await self.alerts.emit(
                        alert_type="raw_item_skipped",
                        level="crit",
                        title="厂商上行回复存在无法解析的条目，raw 保持可重放",
                        detail={
                            "raw_id": raw_id,
                            "skipped_count": skipped,
                            "source": "reply",
                        },
                        dedup_key=f"raw_item_skipped:reply:{raw_id}",
                    )
                except Exception as exc:
                    LOGGER.error(
                        "raw skip alert unavailable",
                        extra={"raw_id": raw_id, "error_type": type(exc).__name__},
                    )
            return len(data)
        await self.repository.mark_processed(raw_id)
        return len(data)
