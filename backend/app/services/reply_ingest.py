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
    degrade_vendor_identifier,
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
# 退订语判定与前端 ReplyView 的 OPT_OUT_RE 同规则；在打码后内容上判定，
# 打码只替换数字，不影响 TD/T/退订 识别。
OPT_OUT_CONTENT = re.compile(r"(?:TD|T|退订)", re.IGNORECASE)
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
    is_optout: bool
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
    """逐条校验 customId；非法或空值跳过，避免一条坏数据打断整批索引。"""

    collected: set[str] = set()
    for item in data:
        raw = _normalized(item).get("customId")
        if not isinstance(raw, str):
            continue
        try:
            normalized = validate_vendor_custom_id(raw)
        except ValueError:
            continue
        if normalized:
            collected.add(normalized)
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
        raw_task_value = value["taskId"]
        if not isinstance(raw_task_value, str):
            raise ValueError("taskId must be a string")
        normalized_task = raw_task_value.strip()
        try:
            raw_task_id, task_id = protect_vendor_task_id(self.crypto, normalized_task)
        except ValueError:
            # 空/非法 taskId 不再整条丢弃：回复以号码为主体，taskId 仅为
            # 关联线索，原值只进入去重摘要与 HMAC 伪标识。
            raw_task_id = normalized_task
            task_id = degrade_vendor_identifier(
                self.crypto,
                normalized_task,
                domain="vendor-task-id",
            )
        custom_value = value.get("customId")
        if custom_value is not None and not isinstance(custom_value, str):
            raise ValueError("customId must be a string or null")
        raw_custom_id: str | None = None
        custom_id: str | None = None
        if custom_value is not None and custom_value.strip():
            # 先判空再指纹化：空串通过合同校验但无法指纹化，历史实现会在
            # 此抛错导致整条回复被跳过、raw 钉死在重放循环。
            try:
                raw_custom_id, custom_id = protect_vendor_custom_id(
                    self.crypto,
                    custom_value.strip(),
                )
            except ValueError:
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
        is_optout = OPT_OUT_CONTENT.fullmatch(masked_content.strip()) is not None
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
            is_optout=is_optout,
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

    async def _spill_write(
        self,
        *,
        payload_sha256: str,
        key_version: int,
        http_status: int,
        content_encoding: str,
        payload_enc: bytes,
    ) -> None:
        """spill 是崩溃兜底而非硬依赖：写盘失败告警后继续 DB 落库。"""

        if self.spill is None:
            return
        try:
            self.spill.write(
                source="reply",
                payload_sha256=payload_sha256,
                key_version=key_version,
                http_status=http_status,
                content_encoding=content_encoding,
                payload_enc=payload_enc,
            )
        except Exception as exc:
            LOGGER.error(
                "raw spill write failed",
                extra={"source": "reply", "error_type": type(exc).__name__},
            )
            if self.alerts is None:
                return
            try:
                await self.alerts.emit(
                    alert_type="vendor_raw_spill_failed",
                    level="crit",
                    title="raw spill 写盘失败，崩溃兜底暂不可用",
                    detail={"source": "reply", "error_type": type(exc).__name__},
                    dedup_key="vendor_raw_spill_failed:reply",
                )
            except Exception as alert_exc:
                LOGGER.error(
                    "raw spill alert unavailable",
                    extra={"source": "reply", "error_type": type(alert_exc).__name__},
                )

    def _spill_remove(self, source: str, payload_sha256: str) -> None:
        """DB 已落库后清理 spill；清理失败无害，只记日志。"""

        if self.spill is None:
            return
        try:
            self.spill.remove(source, payload_sha256)
        except Exception as exc:
            LOGGER.warning(
                "raw spill cleanup failed",
                extra={"source": source, "error_type": type(exc).__name__},
            )

    async def _persist_lost_payload(
        self,
        raw_payload: bytes,
        *,
        status_code: int,
    ) -> None:
        """超限响应的已读部分仍按拉走即消费落密文，保持可人工重放。"""

        # raw_vendor_log.http_status 约束为 100..599；异常路径缺失或越界时
        # 记 200（响应体已被读到才可能超限）。
        if not 100 <= status_code <= 599:
            status_code = 200
        payload_sha256 = hashlib.sha256(raw_payload).hexdigest()
        encrypted = self.crypto.encrypt_bound_bytes(
            raw_payload,
            EncryptionContext(
                domain="vendor-raw",
                table="raw_vendor_log",
                column="payload_enc",
                object_id=f"reply:{payload_sha256}",
            ),
        )
        await self._spill_write(
            payload_sha256=payload_sha256,
            key_version=encrypted.key_version,
            http_status=status_code,
            content_encoding="identity",
            payload_enc=encrypted.payload,
        )
        raw_id = await self.repository.persist_raw(
            payload_enc=encrypted.payload,
            payload_sha256=payload_sha256,
            key_version=encrypted.key_version,
            http_status=status_code,
            content_encoding="identity",
            custom_ids=[],
            item_count=0,
        )
        self._spill_remove("reply", payload_sha256)
        await self.repository.mark_error(raw_id, "reply payload persisted after consume gap")

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
            self._spill_remove(record.source, record.payload_sha256)
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
            if error.raw_body:
                await self._persist_lost_payload(
                    error.raw_body,
                    status_code=error.status_code if error.status_code is not None else 0,
                )
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
        await self._spill_write(
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
        self._spill_remove("reply", payload_sha256)
        try:
            data = decode_pulled_payload(pulled, "GetReply")
        except Exception as error:
            await self.repository.mark_error(
                raw_id,
                f"{type(error).__name__}: vendor response parsing failed",
            )
            raise
        return await self.process_existing(raw_id, data)

    async def process_existing(self, raw_id: int, data: object) -> int:
        """解析已独立提交的 reply raw；轮询、spill 恢复与受控重放共用。"""

        try:
            if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
                raise ValueError("GetReply.data must be an object array")
            # custom_ids 索引元数据在共享路径重建，恢复/重放的 raw 同样可见。
            custom_ids = await self.repository.filter_known_custom_ids(
                _collect_vendor_custom_ids(data)
            )
            await self.repository.update_metadata(
                raw_id,
                custom_ids=custom_ids,
                item_count=len(data),
            )
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
