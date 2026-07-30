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
from app.vendor.zhihui import RawPulledPayload

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
    async def get_report_raw(self) -> RawPulledPayload: ...


class ReportRepository(Protocol):
    async def persist_raw(self, **values: Any) -> int: ...

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
    ) -> None:
        self.gateway = gateway
        self.repository = repository
        self.crypto = crypto
        self.alerts = alerts

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
        vendor_task_id = str(value["taskId"]).strip()
        custom_id = str(value["customId"]).strip()
        return ProtectedReport(
            event_key=_report_event_key(
                vendor_task_id=vendor_task_id,
                custom_id=custom_id,
                canonical_phone_hmac=hmac_candidates[min(hmac_candidates)],
                report_status=status,
                report_desc=report_desc,
                report_time=report_time,
            ),
            vendor_task_id=vendor_task_id,
            custom_id=custom_id,
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

    async def poll_once(self) -> int:
        if self.gateway is None:
            raise RuntimeError("report gateway is not configured")
        pulled = await self.gateway.get_report_raw()
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
        records = pulled.data if isinstance(pulled.data, list) else []
        custom_ids = sorted(
            {
                str(normalized["customId"]).strip()
                for item in records
                if isinstance(item, dict)
                and (normalized := _normalized(item)).get("customId")
                and isinstance(normalized["customId"], str)
            }
        )
        raw_id = await self.repository.persist_raw(
            payload_enc=encrypted.payload,
            payload_sha256=payload_sha256,
            key_version=encrypted.key_version,
            custom_ids=custom_ids,
            item_count=len(records),
        )
        return await self.process_existing(raw_id, pulled.data)

    async def process_existing(self, raw_id: int, data: object) -> int:
        """解析已独立提交的 raw；供轮询与受控重放共用。"""

        try:
            if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
                raise ValueError("GetReport.data must be an object array")
            for item in data:
                report = self._parse(item)
                applied = await self.repository.apply_report(raw_id, report)
                if applied is None:
                    await self.repository.persist_unmatched(raw_id, report)
                elif applied.changed:
                    await self._alert_failure_rate(applied.batch_id)
        except Exception as error:
            await self.repository.mark_error(raw_id, f"{type(error).__name__}: {error}"[:256])
            raise
        await self.repository.mark_processed(raw_id)
        return len(data)
