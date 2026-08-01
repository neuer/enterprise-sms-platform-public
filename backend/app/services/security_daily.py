"""安全日报的脱敏契约、受控投递请求和预览服务。"""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.core.auth.accounts import SecurityPrincipal

SecurityStatus = Literal["normal", "attention", "high"]
GenerationStatus = Literal["pending", "ready", "failed", "unavailable"]
DeliveryStatus = Literal["not_sent", "pending", "sending", "sent", "failed"]
ConfigurationState = Literal["disabled", "dispatcher_missing", "recipients_empty", "ready"]
DeliveryAction = Literal["send", "retry"]
DeliveryRequestState = Literal["pending", "sent", "failed"]

SHANGHAI_OFFSET = timedelta(hours=8)
SHANGHAI_TZ = timezone(SHANGHAI_OFFSET, name="Asia/Shanghai")
PHONE_IN_TEXT = re.compile(r"(?<!\d)1\d{10}(?!\d)")
BEARER_IN_TEXT = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]+", re.IGNORECASE)
FORBIDDEN_KEYS = frozenset(
    {
        "credential",
        "body",
        "ciphertext",
        "content",
        "encrypted",
        "log",
        "logs",
        "mobile",
        "mobiles",
        "password",
        "phone",
        "phones",
        "raw",
        "raw_log",
        "raw_logs",
        "request",
        "request_body",
        "secret",
        "sms_body",
        "token",
    }
)


class SecurityDailyValidationError(ValueError):
    """日报结构、时间或隐私约束不成立。"""


class SecurityDailyNotFound(LookupError):
    """指定报告不存在。"""


class SecurityDailyUnavailable(RuntimeError):
    """报告数据或独立投递控制面暂不可用。"""


class SecurityDailyConfigurationError(SecurityDailyUnavailable):
    """安全日报运行配置损坏或无法安全解释。"""


class SecurityDailyStateConflict(RuntimeError):
    """报告当前状态不允许执行指定动作。"""


class SecurityDailyControlError(RuntimeError):
    """独立安全日报 mailer 控制面不可用。"""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SecurityMetric(_StrictModel):
    label: str = Field(min_length=1, max_length=32)
    value: str = Field(min_length=1, max_length=32)
    tone: Literal["neutral", "good", "warn", "danger"]
    note: str = Field(default="", max_length=80)


class SecurityDetailRow(_StrictModel):
    label: str = Field(min_length=1, max_length=40)
    value: str = Field(min_length=1, max_length=160)
    assessment: str = Field(min_length=1, max_length=80)
    tone: Literal["neutral", "good", "warn", "danger"]


class SecurityAuditRow(_StrictModel):
    time: str = Field(min_length=1, max_length=32)
    actor: str = Field(min_length=1, max_length=64)
    source_ip: str = Field(min_length=1, max_length=64)
    action: str = Field(min_length=1, max_length=80)
    assessment: str = Field(min_length=1, max_length=80)
    tone: Literal["neutral", "good", "warn", "danger"]


class SecurityActionItem(_StrictModel):
    priority: Literal["high", "medium", "low"]
    title: str = Field(min_length=1, max_length=80)
    detail: str = Field(min_length=1, max_length=300)


class SecurityCoverageItem(_StrictModel):
    source: str = Field(min_length=1, max_length=60)
    window: str = Field(min_length=1, max_length=100)
    status: str = Field(min_length=1, max_length=40)
    note: str = Field(min_length=1, max_length=240)
    tone: Literal["neutral", "good", "warn", "danger"]


class SecurityDailyPayload(_StrictModel):
    """API 与 mailer 共同使用的已脱敏结构化报告契约。"""

    schema_version: Literal[1]
    report_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    period_start: str = Field(min_length=20, max_length=35)
    period_end: str = Field(min_length=20, max_length=35)
    generated_at: str = Field(min_length=20, max_length=35)
    status: SecurityStatus
    summary: str = Field(min_length=1, max_length=500)
    pending_confirmation: str = Field(default="", max_length=500)
    metrics: list[SecurityMetric] = Field(min_length=5, max_length=5)
    ssh: list[SecurityDetailRow] = Field(max_length=10)
    web: list[SecurityDetailRow] = Field(max_length=10)
    audit: list[SecurityAuditRow] = Field(max_length=10)
    runtime: list[SecurityDetailRow] = Field(max_length=10)
    actions: list[SecurityActionItem] = Field(max_length=10)
    coverage: list[SecurityCoverageItem] = Field(max_length=10)

    @model_validator(mode="after")
    def validate_period(self) -> SecurityDailyPayload:
        try:
            report_day = date.fromisoformat(self.report_date)
            start = _parse_shanghai_timestamp(self.period_start, "period_start")
            end = _parse_shanghai_timestamp(self.period_end, "period_end")
            generated = _parse_shanghai_timestamp(self.generated_at, "generated_at")
        except ValueError as error:
            raise SecurityDailyValidationError(str(error)) from error
        if start >= end or start.date() != report_day or end.date() != report_day:
            raise SecurityDailyValidationError("日报统计窗口与报告日期不一致")
        if generated <= end:
            raise SecurityDailyValidationError("日报生成时间必须晚于统计窗口")
        return self


def _parse_shanghai_timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field} 必须是 ISO8601 时间") from error
    if parsed.tzinfo is None or parsed.utcoffset() != SHANGHAI_OFFSET:
        raise ValueError(f"{field} 必须使用 +08:00")
    return parsed


def _assert_safe_payload(value: object, path: str = "report") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise SecurityDailyValidationError(f"{path} 包含无效字段")
            if key.casefold() in FORBIDDEN_KEYS:
                raise SecurityDailyValidationError(f"{path}.{key} 不允许出现敏感字段")
            _assert_safe_payload(nested, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, nested in enumerate(value):
            _assert_safe_payload(nested, f"{path}[{index}]")
        return
    if isinstance(value, str) and (
        PHONE_IN_TEXT.search(value)
        or BEARER_IN_TEXT.search(value)
        or "BEGIN PRIVATE KEY" in value.upper()
    ):
        raise SecurityDailyValidationError(f"{path} 包含手机号或凭据文本")


def validate_security_daily_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    """严格校验外部摘要，并返回可安全持久化的 JSON 对象。"""

    try:
        model = SecurityDailyPayload.model_validate(value)
    except (ValidationError, SecurityDailyValidationError) as error:
        raise SecurityDailyValidationError("安全日报结构校验失败") from error
    payload = model.model_dump(mode="json")
    _assert_safe_payload(payload)
    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > 256 * 1024:
        raise SecurityDailyValidationError("安全日报超过安全大小上限")
    return payload


@dataclass(frozen=True, slots=True)
class SecurityDailyReportRecord:
    id: int
    report_date: date
    period_start: datetime
    period_end: datetime
    status: SecurityStatus
    generation_status: GenerationStatus
    delivery_status: DeliveryStatus
    generated_at: datetime | None
    delivered_at: datetime | None
    recipient_count: int
    retry_count: int
    last_error: str | None
    last_error_at: datetime | None
    updated_at: datetime
    payload: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class SecurityDailyOverview:
    enabled: bool
    configuration_state: ConfigurationState
    schedule_time: str
    timezone: str
    period_description: str
    last_generated_at: datetime | None
    last_delivered_at: datetime | None
    next_scheduled_at: datetime | None
    latest_failure: str | None
    delivery_status: DeliveryStatus | None
    recipient_count: int
    resend_configured: bool
    sender_domain: str
    sender_address: str
    beat_restart_required: bool


@dataclass(frozen=True, slots=True)
class SecurityDailyQuery:
    report_date_from: date | None
    report_date_to: date | None
    status: SecurityStatus | None
    generation_status: GenerationStatus | None
    delivery_status: DeliveryStatus | None
    page: int
    page_size: int


@dataclass(frozen=True, slots=True)
class SecurityDailyPage:
    items: tuple[SecurityDailyReportRecord, ...]
    total: int
    page: int
    page_size: int


@dataclass(frozen=True, slots=True)
class SecurityDailyDeliveryRequest:
    request_id: UUID
    report_date: date
    action: DeliveryAction
    state: DeliveryRequestState
    requested_at: datetime
    idempotent: bool


@dataclass(frozen=True, slots=True)
class SecurityDailyControlResult:
    request_id: UUID
    report_date: date
    state: Literal["sent", "failed"]
    completed_at: datetime
    error: str | None = None


@dataclass(frozen=True, slots=True)
class SecurityDailyPreview:
    report_date: date
    status: SecurityStatus
    html: str
    text: str
    payload: dict[str, Any]


class SecurityDailyRepository(Protocol):
    async def overview(self, *, now: datetime) -> SecurityDailyOverview: ...

    async def list_reports(self, query: SecurityDailyQuery) -> SecurityDailyPage: ...

    async def get_report(self, report_date: date) -> SecurityDailyReportRecord | None: ...

    async def request_delivery(
        self,
        report: SecurityDailyReportRecord,
        action: DeliveryAction,
        *,
        principal: SecurityPrincipal,
        ip: str,
    ) -> SecurityDailyDeliveryRequest: ...

    async def pending_delivery_requests(self) -> tuple[tuple[UUID, date], ...]: ...

    async def apply_control_result(self, result: SecurityDailyControlResult) -> None: ...

    async def mark_request_failed(self, request_id: UUID, message: str) -> None: ...


class SecurityDailyControl(Protocol):
    async def submit(
        self,
        request: SecurityDailyDeliveryRequest,
        payload: dict[str, Any],
    ) -> None: ...

    async def result(self, request_id: UUID) -> SecurityDailyControlResult | None: ...


class FileSecurityDailyControl:
    """通过共享只读脱敏文件和独立 mailer 交换投递请求，不接触 Resend Key。"""

    def __init__(self, control_dir: Path) -> None:
        self.control_dir = control_dir
        self.request_dir = control_dir / "requests"
        self.result_dir = control_dir / "results"

    async def submit(
        self,
        request: SecurityDailyDeliveryRequest,
        payload: dict[str, Any],
    ) -> None:
        await asyncio.to_thread(self._write_request, request, payload)

    def _write_request(
        self,
        request: SecurityDailyDeliveryRequest,
        payload: dict[str, Any],
    ) -> None:
        if not self.control_dir.is_dir():
            raise SecurityDailyControlError("安全日报独立投递器未连接")
        self.request_dir.mkdir(mode=0o700, exist_ok=True)
        self.result_dir.mkdir(mode=0o700, exist_ok=True)
        path = self.request_dir / f"{request.request_id}.json"
        temp = self.request_dir / f".{request.request_id}.tmp"
        body = {
            "request_id": str(request.request_id),
            "report_date": request.report_date.isoformat(),
            "action": request.action,
            "payload": validate_security_daily_payload(payload),
        }
        try:
            temp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
            os.chmod(temp, 0o600)
            os.replace(temp, path)
        except OSError as error:
            with suppress(OSError):
                temp.unlink(missing_ok=True)
            raise SecurityDailyControlError("安全日报投递请求写入失败") from error

    async def result(self, request_id: UUID) -> SecurityDailyControlResult | None:
        return await asyncio.to_thread(self._read_result, request_id)

    def _read_result(self, request_id: UUID) -> SecurityDailyControlResult | None:
        path = self.result_dir / f"{request_id}.json"
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError
            result = SecurityDailyControlResult(
                request_id=UUID(str(value["request_id"])),
                report_date=date.fromisoformat(str(value["report_date"])),
                state=cast(Literal["sent", "failed"], str(value["state"])),
                completed_at=_parse_shanghai_timestamp(str(value["completed_at"]), "completed_at"),
                error=str(value["error"])[:256] if value.get("error") else None,
            )
        except (KeyError, TypeError, ValueError, OSError) as error:
            raise SecurityDailyControlError("安全日报投递结果无效") from error
        if result.request_id != request_id or result.state not in {"sent", "failed"}:
            raise SecurityDailyControlError("安全日报投递结果不匹配")
        if result.error and (
            PHONE_IN_TEXT.search(result.error) or BEARER_IN_TEXT.search(result.error)
        ):
            raise SecurityDailyControlError("安全日报投递结果包含敏感信息")
        return result


def _next_schedule(now: datetime) -> datetime:
    local = now.astimezone(SHANGHAI_TZ)
    target = local.replace(hour=8, minute=0, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return target


def resolve_configuration_state(
    *, enabled: bool, resend_configured: bool, recipient_count: int
) -> ConfigurationState:
    """把当前安全日报配置归一为可解释的状态，不用默认值伪造可用性。"""

    if not enabled:
        return "disabled"
    if not resend_configured:
        return "dispatcher_missing"
    if recipient_count == 0:
        return "recipients_empty"
    return "ready"


def _timeline(record: SecurityDailyReportRecord) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if record.generated_at is not None:
        events.append({"type": "generated", "at": record.generated_at, "label": "报告生成"})
    if record.last_error_at is not None:
        events.append(
            {
                "type": "failed",
                "at": record.last_error_at,
                "label": "投递失败",
                "detail": record.last_error,
            }
        )
    if record.delivered_at is not None:
        events.append({"type": "sent", "at": record.delivered_at, "label": "已投递"})
    return events


def _render_preview(payload: dict[str, Any]) -> tuple[str, str]:
    """基于同一脱敏对象生成安全的 HTML/纯文本预览。"""

    validated = validate_security_daily_payload(payload)
    report_date = str(validated["report_date"])
    status = {"normal": "正常", "attention": "关注", "high": "高风险"}[validated["status"]]
    sections = [
        ("管理摘要", validated["summary"]),
        ("待确认", validated["pending_confirmation"] or "无"),
    ]
    for key, label in (
        ("metrics", "核心指标"),
        ("ssh", "SSH 与主机安全"),
        ("web", "Web/API"),
        ("audit", "管理审计"),
        ("runtime", "运行状态"),
        ("actions", "建议处置"),
        ("coverage", "证据范围"),
    ):
        sections.append((label, json.dumps(validated[key], ensure_ascii=False, indent=2)))
    text_lines = [f"服务器安全日报 [{status}]", f"日期：{report_date}", ""]
    text_lines.extend(f"{label}\n{value}\n" for label, value in sections)
    text_preview = "\n".join(text_lines)
    html_sections = "".join(
        f"<section><h2>{html.escape(label)}</h2><pre>{html.escape(str(value))}</pre></section>"
        for label, value in sections
    )
    html_preview = (
        "<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
        f"<title>服务器安全日报</title><main><h1>服务器安全日报 [{html.escape(status)}]</h1>"
        f"<p>日期：{html.escape(report_date)}</p>{html_sections}</main></html>"
    )
    return html_preview, text_preview


class SecurityDailyService:
    """编排日报查询、脱敏预览和不持有邮件凭据的投递请求。"""

    def __init__(
        self,
        repository: SecurityDailyRepository,
        control: SecurityDailyControl,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(SHANGHAI_TZ),
    ) -> None:
        self.repository = repository
        self.control = control
        self.clock = clock

    async def overview(self) -> SecurityDailyOverview:
        await self._synchronize_control_results()
        now = self.clock()
        return await self.repository.overview(now=now)

    async def list_reports(self, query: SecurityDailyQuery) -> SecurityDailyPage:
        await self._synchronize_control_results()
        return await self.repository.list_reports(query)

    async def get_report(self, report_date: date) -> SecurityDailyReportRecord:
        await self._synchronize_control_results()
        record = await self.repository.get_report(report_date)
        if record is None:
            raise SecurityDailyNotFound(report_date.isoformat())
        return record

    async def preview(self, report_date: date) -> SecurityDailyPreview:
        record = await self.get_report(report_date)
        if record.payload is None or record.generation_status != "ready":
            raise SecurityDailyUnavailable("日报数据不可用，不能生成预览")
        html_preview, text_preview = _render_preview(record.payload)
        return SecurityDailyPreview(
            report_date=record.report_date,
            status=record.status,
            html=html_preview,
            text=text_preview,
            payload=record.payload,
        )

    async def request_delivery(
        self,
        report_date: date,
        action: DeliveryAction,
        *,
        principal: SecurityPrincipal,
        ip: str,
    ) -> SecurityDailyDeliveryRequest:
        overview = await self.overview()
        if not overview.enabled:
            raise SecurityDailyUnavailable("安全日报尚未启用")
        record = await self.get_report(report_date)
        if record.payload is None or record.generation_status != "ready":
            raise SecurityDailyUnavailable("日报数据不可用，不能投递")
        if action == "retry" and record.delivery_status != "failed":
            raise SecurityDailyStateConflict("只有投递失败的日报允许重试")
        request = await self.repository.request_delivery(
            record,
            action,
            principal=principal,
            ip=ip,
        )
        if request.idempotent:
            return request
        try:
            await self.control.submit(request, record.payload)
        except SecurityDailyControlError:
            await self.repository.mark_request_failed(request.request_id, "独立投递器不可用")
            raise
        return request

    @staticmethod
    def timeline(record: SecurityDailyReportRecord) -> list[dict[str, Any]]:
        return _timeline(record)

    @staticmethod
    def next_schedule(now: datetime) -> datetime:
        return _next_schedule(now)

    async def _synchronize_control_results(self) -> None:
        """把 mailer 的已完成结果回写事实表；未完成或暂不可读时保持 pending。"""

        for request_id, report_date in await self.repository.pending_delivery_requests():
            result = await self.control.result(request_id)
            if result is None:
                continue
            if result.report_date != report_date:
                raise SecurityDailyControlError("安全日报投递结果日期不匹配")
            await self.repository.apply_control_result(result)
