"""状态报告原始密文落地、解析回写与 unmatched 保留。"""

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
from app.services.raw_spill import (
    CAPTURE_COMPLETE,
    CAPTURE_COMPLETE_TOO_LARGE,
    CAPTURE_TRUNCATED,
    RawSpillStore,
    SpillQuotaExceeded,
)
from app.vendor.identifiers import (
    degrade_vendor_identifier,
    protect_vendor_custom_id,
    protect_vendor_task_id,
    validate_vendor_custom_id,
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
class ProtectedReport:
    event_key: str
    vendor_task_id: str
    custom_id: str
    match_custom_id: str
    phone_enc: bytes
    phone_hmac: str
    phone_mask: str
    key_version: int
    report_status: int
    message_status: str
    report_desc: str
    report_time: datetime
    phone_hmacs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FailureRateAlert:
    batch_id: int
    batch_no: str
    delivered: int
    failed: int
    threshold: int


@dataclass(frozen=True, slots=True)
class ReportApplyResult:
    """报告已匹配的投影结果；仅 changed=true 允许派生统计与回调。"""

    batch_id: int
    changed: bool


class ReportGateway(Protocol):
    async def get_report_raw(self, body_sink: Any | None = None) -> RawPulledPayload: ...


class ReportRepository(Protocol):
    async def persist_raw(self, **values: Any) -> int: ...

    async def update_metadata(
        self,
        raw_id: int,
        *,
        custom_ids: list[str],
        item_count: int,
    ) -> None: ...

    async def filter_known_custom_ids(self, custom_ids: list[str]) -> list[str]: ...

    async def apply_report(
        self,
        raw_id: int,
        report: ProtectedReport,
    ) -> ReportApplyResult | None: ...

    async def failure_rate_candidate(self, batch_id: int) -> FailureRateAlert | None: ...

    async def persist_unmatched(self, raw_id: int, report: ProtectedReport) -> None: ...

    async def mark_processed(self, raw_id: int) -> None: ...

    async def mark_error(self, raw_id: int, error: str) -> None: ...


class AlertEmitter(Protocol):
    async def emit(
        self,
        *,
        alert_type: str,
        level: str,
        title: str,
        detail: dict[str, Any],
        dedup_key: str,
    ) -> None: ...


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


def _message_status(report_status: int) -> str:
    if report_status == 1:
        return "delivered"
    if report_status in {2, 99}:
        return "failed"
    if report_status == 0:
        return "unknown"
    return "other"


def _report_event_key(
    *,
    vendor_task_id: str,
    custom_id: str,
    canonical_phone_hmac: str,
    report_status: int,
    report_desc: str,
    report_time: datetime,
) -> str:
    """生成不含明文 PII、跨活动密钥切换稳定的报告事实键。"""

    timestamp = report_time.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    values = (
        vendor_task_id,
        custom_id,
        canonical_phone_hmac,
        str(report_status),
        report_desc,
        timestamp,
    )
    canonical = "".join(f"{len(value.encode('utf-8'))}:{value}" for value in values)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ReportIngestService:
    """先提交 raw 密文，再解析；任何解析错误保留 processed=false。"""

    def __init__(
        self,
        gateway: ReportGateway | None,
        repository: ReportRepository,
        crypto: CryptoService,
        *,
        alerts: AlertEmitter | None = None,
        spill: RawSpillStore | None = None,
    ) -> None:
        self.gateway = gateway
        self.repository = repository
        self.crypto = crypto
        self.alerts = alerts
        self.spill = spill

    async def _alert_failure_rate(self, batch_id: int) -> None:
        if self.alerts is None:
            return
        try:
            candidate = await self.repository.failure_rate_candidate(batch_id)
            if candidate is None:
                return
            denominator = candidate.delivered + candidate.failed
            await self.alerts.emit(
                alert_type="failure_rate",
                level="warn",
                title="批次失败率超过阈值",
                detail={
                    "batch_no": candidate.batch_no,
                    "delivered": candidate.delivered,
                    "failed": candidate.failed,
                    "failure_rate_percent": round(candidate.failed * 100 / denominator, 2),
                    "threshold_percent": candidate.threshold,
                },
                dedup_key=f"failure_rate:{candidate.batch_no}",
            )
        except Exception as exc:
            LOGGER.error(
                "failure rate alert unavailable",
                extra={"batch_id": batch_id, "error_type": type(exc).__name__},
            )

    async def _persist_lost_payload(
        self,
        source: str,
        raw_payload: bytes,
        *,
        status_code: int,
        complete: bool,
    ) -> None:
        # raw_vendor_log.http_status 约束为 100..599；异常路径缺失或越界时
        # 记 200（响应体已被读到才可能超限），否则兜底落库必然违约失败。
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
                object_id=f"{source}:{payload_sha256}",
            ),
        )
        await self._spill_write(
            source=source,
            payload_sha256=payload_sha256,
            key_version=encrypted.key_version,
            http_status=status_code,
            content_encoding="identity",
            payload_enc=encrypted.payload,
            capture_state=capture_state,
        )
        raw_id = await self.repository.persist_raw(
            payload_enc=encrypted.payload,
            payload_sha256=payload_sha256,
            key_version=encrypted.key_version,
            http_status=status_code,
            content_encoding="identity",
            custom_ids=[],
            item_count=0,
            capture_state=capture_state,
        )
        self._spill_remove(source, payload_sha256)
        if complete:
            await self.repository.mark_error(
                raw_id, f"{source} oversized payload persisted after consume gap"
            )
            await self._alert_oversized_complete(source)
        else:
            await self.repository.mark_error(
                raw_id, f"{source} truncated vendor response beyond recovery limit"
            )
            await self._alert_truncated(source)

    async def _alert_consume_gap(self, source: str, error_type: str) -> None:
        if self.alerts is None:
            return
        try:
            await self.alerts.emit(
                alert_type="vendor_raw_persist_failed",
                level="crit",
                title="厂商拉走即消费响应未能落库，需人工介入",
                detail={"source": source, "error_type": error_type},
                dedup_key=f"vendor_raw_persist_failed:{source}",
            )
        except Exception as exc:
            LOGGER.error(
                "vendor raw persist alert unavailable",
                extra={"source": source, "error_type": type(exc).__name__},
            )

    async def _alert_truncated(self, source: str) -> None:
        await self._emit_capture_alert(
            source,
            alert_type="vendor_raw_truncated",
            title="厂商拉走即消费响应被截断，不得当作正常可重放 raw",
        )

    async def _alert_oversized_complete(self, source: str) -> None:
        await self._emit_capture_alert(
            source,
            alert_type="vendor_raw_oversized_complete",
            title="厂商拉走即消费响应完整但超过自动解析上限",
        )

    async def _alert_spill_quota(self, source: str) -> None:
        await self._emit_capture_alert(
            source,
            alert_type="vendor_raw_spill_quota_exceeded",
            title="raw spill 配额已满，已停止继续拉取以防消费缺口",
        )

    async def _emit_capture_alert(self, source: str, *, alert_type: str, title: str) -> None:
        if self.alerts is None:
            return
        try:
            await self.alerts.emit(
                alert_type=alert_type,
                level="crit",
                title=title,
                detail={"source": source},
                dedup_key=f"{alert_type}:{source}",
            )
        except Exception as exc:
            LOGGER.error(
                "vendor raw capture alert unavailable",
                extra={
                    "source": source,
                    "alert_type": alert_type,
                    "error_type": type(exc).__name__,
                },
            )

    async def _spill_write(
        self,
        *,
        source: str,
        payload_sha256: str,
        key_version: int,
        http_status: int,
        content_encoding: str,
        payload_enc: bytes,
        capture_state: str = CAPTURE_COMPLETE,
    ) -> None:
        """spill 是崩溃兜底而非硬依赖：写盘失败告警后继续 DB 落库。"""

        if self.spill is None:
            return
        try:
            self.spill.write(
                source=source,
                payload_sha256=payload_sha256,
                key_version=key_version,
                http_status=http_status,
                content_encoding=content_encoding,
                payload_enc=payload_enc,
                capture_state=capture_state,
            )
        except SpillQuotaExceeded:
            await self._alert_spill_quota(source)
        except Exception as exc:
            LOGGER.error(
                "raw spill write failed",
                extra={"source": source, "error_type": type(exc).__name__},
            )
            if self.alerts is None:
                return
            try:
                await self.alerts.emit(
                    alert_type="vendor_raw_spill_failed",
                    level="crit",
                    title="raw spill 写盘失败，崩溃兜底暂不可用",
                    detail={"source": source, "error_type": type(exc).__name__},
                    dedup_key=f"vendor_raw_spill_failed:{source}",
                )
            except Exception as alert_exc:
                LOGGER.error(
                    "raw spill alert unavailable",
                    extra={"source": source, "error_type": type(alert_exc).__name__},
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

    async def _alert_skipped(self, raw_id: int, skipped: int, *, source: str) -> None:
        if self.alerts is None:
            return
        try:
            await self.alerts.emit(
                alert_type="raw_item_skipped",
                level="crit",
                title="厂商回执存在无法解析的条目，raw 保持可重放",
                detail={"raw_id": raw_id, "skipped_count": skipped, "source": source},
                dedup_key=f"raw_item_skipped:{source}:{raw_id}",
            )
        except Exception as exc:
            LOGGER.error(
                "raw skip alert unavailable",
                extra={"raw_id": raw_id, "error_type": type(exc).__name__},
            )

    def _parse(self, item: dict[str, Any]) -> ProtectedReport:
        value = _normalized(item)
        required = {
            "taskId",
            "customId",
            "phone",
            "reportStatus",
            "reportDescription",
            "reportTime",
        }
        if not required.issubset(value):
            raise ValueError("report fields are incomplete")
        status = value["reportStatus"]
        if not isinstance(status, int) or isinstance(status, bool):
            raise ValueError("reportStatus must be an integer")
        raw_report_time = value["reportTime"]
        if not isinstance(raw_report_time, str):
            raise ValueError("reportTime must be a string")
        try:
            report_time = datetime.fromisoformat(raw_report_time.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("reportTime format is invalid") from None
        if report_time.tzinfo is None:
            if VENDOR_LOCAL_TIME.fullmatch(raw_report_time) is None:
                raise ValueError("reportTime must include timezone")
            report_time = report_time.replace(tzinfo=SHANGHAI_TIMEZONE)
        if report_time.utcoffset() is None:
            raise ValueError("reportTime timezone must define an offset")
        phone = str(value["phone"])
        # 报告携带的号码密文只在 unmatched_report 的授权导出边界解密；
        # 匹配报告使用 sms_message 原始四元组，不解密该副本。
        protected = self.crypto.protect_phone(phone, table="unmatched_report")
        hmac_candidates = self.crypto.hmac_candidates(phone)
        phone_hmacs = tuple(hmac_candidates.values())
        report_desc = mask_phone_text(str(value["reportDescription"]))[:128]
        raw_task_value = value["taskId"]
        if not isinstance(raw_task_value, str):
            raise ValueError("taskId must be a string")
        normalized_task = raw_task_value.strip()
        try:
            raw_task_id, vendor_task_id = protect_vendor_task_id(
                self.crypto,
                normalized_task,
            )
        except ValueError:
            # 空/非法 taskId 不再整条丢弃：匹配不依赖 taskId，原值只进入
            # 事件摘要与 HMAC 伪标识，报告事实照常保留。
            raw_task_id = normalized_task
            vendor_task_id = degrade_vendor_identifier(
                self.crypto,
                normalized_task,
                domain="vendor-task-id",
            )
        raw_custom_value = value["customId"]
        if not isinstance(raw_custom_value, str):
            raise ValueError("customId must be a string")
        normalized_custom = raw_custom_value.strip()
        match_custom_id = ""
        try:
            if not normalized_custom:
                raise ValueError("customId is empty")
            match_custom_id, custom_id = protect_vendor_custom_id(
                self.crypto,
                normalized_custom,
            )
        except ValueError:
            # 空/非法 customId 必非本平台在途消息：跳过批次匹配、直接落
            # unmatched_report 保留事实，不再把整个 raw 钉死在重放循环。
            custom_id = degrade_vendor_identifier(
                self.crypto,
                normalized_custom,
                domain="vendor-custom-id",
            )
        return ProtectedReport(
            event_key=_report_event_key(
                vendor_task_id=raw_task_id,
                custom_id=normalized_custom,
                canonical_phone_hmac=hmac_candidates[min(hmac_candidates)],
                report_status=status,
                report_desc=report_desc,
                report_time=report_time,
            ),
            vendor_task_id=vendor_task_id,
            custom_id=custom_id,
            match_custom_id=match_custom_id,
            phone_enc=protected.phone_enc,
            phone_hmac=protected.phone_hmac,
            phone_mask=protected.phone_mask,
            key_version=protected.key_version,
            report_status=status,
            message_status=_message_status(status),
            report_desc=report_desc,
            report_time=report_time,
            phone_hmacs=phone_hmacs,
        )

    async def recover_spills(self) -> int:
        """把落库前崩溃留下的加密 spill 恢复进 raw_vendor_log。"""

        if self.spill is None:
            return 0
        recovered = 0
        pending = [
            *self.spill.list_pending(),
            *self.spill.list_pending_streams(self.crypto),
        ]
        for record in pending:
            if record.source != "report":
                continue
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
                await self._alert_consume_gap("report", type(error).__name__)
                continue
            if record.capture_state == CAPTURE_TRUNCATED:
                await self.repository.mark_error(
                    raw_id, "report truncated vendor response beyond recovery limit"
                )
                await self._alert_truncated("report")
            elif record.capture_state == CAPTURE_COMPLETE_TOO_LARGE:
                await self.repository.mark_error(
                    raw_id, "report oversized payload persisted after consume gap"
                )
                await self._alert_oversized_complete("report")
            record.path.unlink(missing_ok=True)
            self._spill_remove(record.source, record.payload_sha256)
            recovered += 1
        return recovered

    async def poll_once(self) -> int:
        if self.gateway is None:
            raise RuntimeError("report gateway is not configured")
        await self.recover_spills()
        if self.spill is not None and not self.spill.can_accept():
            await self._alert_spill_quota("report")
            return 0
        stream = None
        if self.spill is not None:
            try:
                stream = self.spill.open_stream("report", self.crypto)
            except SpillQuotaExceeded:
                await self._alert_spill_quota("report")
                return 0
        try:
            pulled = await self.gateway.get_report_raw(body_sink=stream)
        except VendorResponseTooLarge as error:
            if error.raw_body:
                await self._persist_lost_payload(
                    "report",
                    error.raw_body,
                    status_code=error.status_code if error.status_code is not None else 0,
                    complete=error.complete,
                )
                if stream is not None:
                    stream.discard()
            else:
                await self._alert_consume_gap("report", type(error).__name__)
            raise
        except Exception as error:
            await self._alert_consume_gap("report", type(error).__name__)
            raise
        payload_sha256 = hashlib.sha256(pulled.raw_payload).hexdigest()
        encrypted = self.crypto.encrypt_bound_bytes(
            pulled.raw_payload,
            EncryptionContext(
                domain="vendor-raw",
                table="raw_vendor_log",
                column="payload_enc",
                object_id=f"report:{payload_sha256}",
            ),
        )
        await self._spill_write(
            source="report",
            payload_sha256=payload_sha256,
            key_version=encrypted.key_version,
            http_status=pulled.status_code,
            content_encoding=pulled.content_encoding,
            payload_enc=encrypted.payload,
        )
        if stream is not None:
            stream.discard()
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
            await self._alert_consume_gap("report", type(error).__name__)
            raise
        self._spill_remove("report", payload_sha256)
        try:
            data = decode_pulled_payload(pulled, "GetReport")
        except Exception as error:
            await self.repository.mark_error(
                raw_id,
                f"{type(error).__name__}: vendor response parsing failed",
            )
            raise
        return await self.process_existing(raw_id, data)

    async def process_existing(self, raw_id: int, data: object) -> int:
        """解析已独立提交的 raw；供轮询、spill 恢复与受控重放共用。"""

        try:
            if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
                raise ValueError("GetReport.data must be an object array")
            # custom_ids 索引元数据在共享路径重建：spill 恢复与人工/自动重放
            # 落库的 raw 同样要对 uncertain 定位可见（规则 4 数据源）。
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
                    report = self._parse(item)
                except (ValueError, KeyError, TypeError) as error:
                    skipped += 1
                    LOGGER.warning(
                        "skipping invalid report item",
                        extra={
                            "raw_id": raw_id,
                            "item_index": index,
                            "error_type": type(error).__name__,
                        },
                    )
                    continue
                if report.match_custom_id:
                    applied = await self.repository.apply_report(raw_id, report)
                else:
                    # 空/非法 customId 无可匹配的平台批次；空串匹配可能
                    # 误连旧发送的空 custom_id 分片，必须直接落 unmatched。
                    applied = None
                if applied is None:
                    await self.repository.persist_unmatched(raw_id, report)
                elif applied.changed:
                    await self._alert_failure_rate(applied.batch_id)
        except Exception as error:
            await self.repository.mark_error(raw_id, f"{type(error).__name__}: {error}"[:256])
            raise
        if skipped:
            await self.repository.mark_error(
                raw_id,
                f"skipped {skipped} invalid report items",
            )
            await self._alert_skipped(raw_id, skipped, source="report")
            return len(data)
        await self.repository.mark_processed(raw_id)
        return len(data)
