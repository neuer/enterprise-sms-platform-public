"""GetReply 完整 raw 密文先落库，再解析为无明文手机号的回复对象。"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from app.services.crypto import CryptoService, EncryptionContext
from app.services.masking import mask_phone_text
from app.services.raw_spill import (
    CAPTURE_COMPLETE,
    CAPTURE_COMPLETE_TOO_LARGE,
    CAPTURE_PROTOCOL_INVALID,
    CAPTURE_TRUNCATED,
    RawSpillStore,
    RecoverRoundBudget,
    SpillQuotaExceeded,
    discard_header_only_stream,
    is_non_replayable_capture,
    iter_records_for_recover,
    manage_raw_spill_stream,
)
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
VENDOR_LOCAL_TIME = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?")


def _discard_unused_stream(stream: Any) -> None:
    """仅释放 announce 前的 header-only 租约；已 announce 的捕获留给恢复。"""

    discard_header_only_stream(stream)


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
    async def get_reply_raw(self, body_sink: Any | None = None) -> RawPulledPayload: ...


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

    async def _alert_truncated(self) -> None:
        await self._emit_capture_alert(
            alert_type="vendor_raw_truncated",
            title="厂商拉走即消费响应被截断，不得当作正常可重放 raw",
        )

    async def _alert_protocol_invalid(self) -> None:
        await self._emit_capture_alert(
            alert_type="vendor_raw_protocol_invalid",
            title="厂商拉走即消费响应协议异常，不得当作正常可重放 raw",
        )

    async def _alert_quarantine(self, stream_id: str) -> None:
        """terminal 失败的 quarantine 告警不可被恢复路径静默吞掉。"""

        if self.alerts is None:
            LOGGER.error(
                "vendor raw stream quarantined without alert sink",
                extra={"source": "reply"},
            )
            return
        try:
            await self.alerts.emit(
                alert_type="vendor_raw_stream_quarantined",
                level="crit",
                title="raw spill terminal 不完整，已认证 chunk 已隔离为截断事实",
                detail={"source": "reply", "stream_id": stream_id},
                dedup_key=f"vendor_raw_stream_quarantined:reply:{stream_id}",
            )
        except Exception as exc:
            LOGGER.error(
                "vendor raw quarantine alert unavailable",
                extra={"source": "reply", "error_type": type(exc).__name__},
            )

    async def _alert_oversized_complete(self) -> None:
        await self._emit_capture_alert(
            alert_type="vendor_raw_oversized_complete",
            title="厂商拉走即消费响应完整但超过自动解析上限",
        )

    async def _alert_spill_quota(self) -> None:
        await self._emit_capture_alert(
            alert_type="vendor_raw_spill_quota_exceeded",
            title="raw spill 配额已满，已停止继续拉取以防消费缺口",
        )

    async def _alert_artifact_quarantine(self, result: Any) -> None:
        """不可认证/损坏/孤儿对象离开活动配额必须可观测；载荷不含 PII 或路径。"""

        isolated = int(getattr(result, "isolated", 0) or 0)
        temps = int(getattr(result, "temps_reclaimed", 0) or 0)
        expired = int(getattr(result, "quarantine_expired", 0) or 0)
        dropped = int(getattr(result, "quarantine_capacity_dropped", 0) or 0)
        if isolated < 1 and temps < 1 and expired < 1 and dropped < 1:
            return
        if self.alerts is None:
            LOGGER.error(
                "vendor raw nonactive quarantine without alert sink",
                extra={"source": "reply"},
            )
            return
        try:
            await self.alerts.emit(
                alert_type="vendor_raw_nonactive_quarantine",
                level="crit",
                title="raw spill 不可恢复对象已隔离到非活动配额，活动拉取容量已释放",
                detail={
                    "source": "reply",
                    "isolated": isolated,
                    "temps_reclaimed": temps,
                    "unauthenticated_partial": int(
                        getattr(result, "unauthenticated_partial", 0) or 0
                    ),
                    "orphans": int(getattr(result, "orphans", 0) or 0),
                    "quarantine_expired": expired,
                    "quarantine_capacity_dropped": dropped,
                },
                dedup_key="vendor_raw_nonactive_quarantine:reply",
            )
        except Exception as exc:
            LOGGER.error(
                "vendor raw nonactive quarantine alert unavailable",
                extra={"source": "reply", "error_type": type(exc).__name__},
            )
        if dropped < 1:
            return
        try:
            await self.alerts.emit(
                alert_type="vendor_raw_quarantine_capacity",
                level="crit",
                title="raw spill 非活动隔离已达容量，已丢弃最旧无 PII 证据",
                detail={"source": "reply", "dropped": dropped},
                dedup_key="vendor_raw_quarantine_capacity:reply",
            )
        except Exception as exc:
            LOGGER.error(
                "vendor raw quarantine capacity alert unavailable",
                extra={"source": "reply", "error_type": type(exc).__name__},
            )

    async def _alert_header_only(self, result: Any) -> None:
        """超龄 header-only 回收必须可观测，避免配额耗尽后静默停轮询。"""

        cleaned = int(getattr(result, "header_cleaned", getattr(result, "total", result)) or 0)
        if cleaned < 1 or self.alerts is None:
            return
        try:
            await self.alerts.emit(
                alert_type="vendor_raw_header_only",
                level="crit",
                title="raw spill 存在超龄 header-only 残留，已回收以免耗尽拉取配额",
                detail={
                    "source": "reply",
                    "cleaned": cleaned,
                    "header_only": int(getattr(result, "header_only", 0) or 0),
                    "partial_header": int(getattr(result, "partial_header", 0) or 0),
                    "corrupt_header": int(getattr(result, "corrupt_header", 0) or 0),
                    "incomplete_frames": int(getattr(result, "incomplete_frames", 0) or 0),
                },
                dedup_key="vendor_raw_header_only:reply",
            )
        except Exception as exc:
            LOGGER.error(
                "vendor raw header-only alert unavailable",
                extra={"source": "reply", "error_type": type(exc).__name__},
            )

    async def _emit_capture_alert(self, *, alert_type: str, title: str) -> None:
        if self.alerts is None:
            return
        try:
            await self.alerts.emit(
                alert_type=alert_type,
                level="crit",
                title=title,
                detail={"source": "reply"},
                dedup_key=f"{alert_type}:reply",
            )
        except Exception as exc:
            LOGGER.error(
                "vendor raw capture alert unavailable",
                extra={
                    "source": "reply",
                    "alert_type": alert_type,
                    "error_type": type(exc).__name__,
                },
            )

    async def _spill_write(
        self,
        *,
        payload_sha256: str,
        key_version: int,
        http_status: int,
        content_encoding: str,
        payload_enc: bytes,
        capture_state: str = CAPTURE_COMPLETE,
    ) -> bool:
        """secondary spill 必须回报成败；失败只告警，不得当作已形成持久副本。"""

        if self.spill is None:
            return False
        try:
            self.spill.write(
                source="reply",
                payload_sha256=payload_sha256,
                key_version=key_version,
                http_status=http_status,
                content_encoding=content_encoding,
                payload_enc=payload_enc,
                capture_state=capture_state,
            )
            return True
        except SpillQuotaExceeded:
            await self._alert_spill_quota()
            return False
        except Exception as exc:
            LOGGER.error(
                "raw spill write failed",
                extra={"source": "reply", "error_type": type(exc).__name__},
            )
            if self.alerts is not None:
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
            return False

    async def _commit_raw(
        self,
        *,
        payload_sha256: str,
        key_version: int,
        http_status: int,
        content_encoding: str,
        payload_enc: bytes,
        capture_state: str,
        stream: Any | None,
    ) -> int:
        """PostgreSQL 提交成功前不得删除 stream，除非 secondary spill 已 fsync。"""

        spill_ok = await self._spill_write(
            payload_sha256=payload_sha256,
            key_version=key_version,
            http_status=http_status,
            content_encoding=content_encoding,
            payload_enc=payload_enc,
            capture_state=capture_state,
        )
        if spill_ok and stream is not None:
            stream.discard()
        try:
            raw_id = await self.repository.persist_raw(
                payload_enc=payload_enc,
                payload_sha256=payload_sha256,
                key_version=key_version,
                http_status=http_status,
                content_encoding=content_encoding,
                custom_ids=[],
                item_count=0,
                capture_state=capture_state,
            )
        except Exception as error:
            await self._alert_consume_gap(type(error).__name__)
            raise
        if stream is not None:
            stream.discard()
        self._spill_remove("reply", payload_sha256)
        return raw_id

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
        complete: bool,
        stream: Any | None = None,
    ) -> None:
        """超限响应的已读部分仍按拉走即消费落密文；截断不得当作正常可重放。"""

        # raw_vendor_log.http_status 约束为 100..599；异常路径缺失或越界时
        # 记 200（响应体已被读到才可能超限）。
        if not 100 <= status_code <= 599:
            status_code = 200
        capture_state = CAPTURE_COMPLETE_TOO_LARGE if complete else CAPTURE_TRUNCATED
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
        raw_id = await self._commit_raw(
            payload_sha256=payload_sha256,
            key_version=encrypted.key_version,
            http_status=status_code,
            content_encoding="identity",
            payload_enc=encrypted.payload,
            capture_state=capture_state,
            stream=stream,
        )
        if complete:
            await self.repository.mark_error(
                raw_id, "reply oversized payload persisted after consume gap"
            )
            await self._alert_oversized_complete()
        else:
            await self.repository.mark_error(
                raw_id, "reply truncated vendor response beyond recovery limit"
            )
            await self._alert_truncated()

    async def recover_spills(self) -> int:
        """把落库前崩溃留下的加密 spill 恢复进 raw_vendor_log。"""

        if self.spill is None:
            return 0
        reclaim = getattr(self.spill, "reclaim_idle", None)
        if callable(reclaim):
            result = reclaim("reply", self.crypto)
            if result:
                await self._alert_header_only(result)
            await self._alert_artifact_quarantine(result)
        recovered = 0
        attempted = 0
        used_bytes = 0
        started_at = time.monotonic()
        budget = getattr(self.spill, "recover_budget", None) or RecoverRoundBudget()
        for record in iter_records_for_recover(self.spill, self.crypto, "reply"):
            if budget.exhausted(
                recovered=attempted, used_bytes=used_bytes, started_at=started_at
            ):
                break
            size = int(getattr(record, "recover_weight_bytes", 0) or len(record.payload_enc))
            try:
                raw_id = await self.repository.persist_raw(
                    payload_enc=record.payload_enc,
                    payload_sha256=record.payload_sha256,
                    key_version=record.key_version,
                    http_status=record.http_status,
                    content_encoding=record.content_encoding,
                    custom_ids=[],
                    item_count=0,
                    capture_state=record.capture_state,
                )
            except Exception as error:
                await self._alert_consume_gap(type(error).__name__)
                attempted += 1
                used_bytes += size
                continue
            if record.capture_state == CAPTURE_TRUNCATED:
                await self.repository.mark_error(
                    raw_id, "reply truncated vendor response beyond recovery limit"
                )
                await self._alert_truncated()
            elif record.capture_state == CAPTURE_PROTOCOL_INVALID:
                await self.repository.mark_error(raw_id, "reply protocol-invalid vendor response")
                await self._alert_protocol_invalid()
            elif record.capture_state == CAPTURE_COMPLETE_TOO_LARGE:
                await self.repository.mark_error(
                    raw_id, "reply oversized payload persisted after consume gap"
                )
                await self._alert_oversized_complete()
            if record.quarantined:
                await self._alert_quarantine(record.stream_id)
            record.path.unlink(missing_ok=True)
            self._spill_remove(record.source, record.payload_sha256)
            if record.stream_id:
                self.spill.remove_stream(record.source, record.stream_id)
            attempted += 1
            used_bytes += size
            recovered += 1
        return recovered

    async def poll_once(self) -> int:
        if self.gateway is None:
            raise RuntimeError("reply gateway is not configured")
        await self.recover_spills()
        stream = None
        if self.spill is not None:
            try:
                stream = self.spill.open_stream("reply", self.crypto)
            except SpillQuotaExceeded:
                await self._alert_spill_quota()
                return 0
        try:
            with manage_raw_spill_stream(stream):
                pulled = await self.gateway.get_reply_raw(body_sink=stream)
        except VendorResponseTooLarge as error:
            if error.raw_body:
                await self._persist_lost_payload(
                    error.raw_body,
                    status_code=error.status_code if error.status_code is not None else 0,
                    complete=error.complete,
                    stream=stream,
                )
            else:
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
        capture_state = CAPTURE_PROTOCOL_INVALID if pulled.protocol_invalid else CAPTURE_COMPLETE
        raw_id = await self._commit_raw(
            payload_sha256=payload_sha256,
            key_version=encrypted.key_version,
            http_status=pulled.status_code,
            content_encoding=pulled.content_encoding,
            payload_enc=encrypted.payload,
            capture_state=capture_state,
            stream=stream,
        )
        if is_non_replayable_capture(capture_state):
            await self.repository.mark_error(raw_id, "reply protocol-invalid vendor response")
            await self._alert_protocol_invalid()
            return 0
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
