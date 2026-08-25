"""安全日报的脱敏契约、事实记录类型与配置校验。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

SecurityStatus = Literal["normal", "attention", "high"]
GenerationSource = Literal["auto", "manual"]
GenerationStatus = Literal["pending", "ready", "failed", "unavailable"]
DeliveryStatus = Literal["not_sent", "pending", "sending", "sent", "failed", "unknown"]
ConfigurationState = Literal["disabled", "dispatcher_missing", "recipients_empty", "ready"]
DeliveryAction = Literal["send", "retry"]
DeliveryRequestState = Literal["pending", "sent", "failed", "unknown"]

SHANGHAI_OFFSET = timedelta(hours=8)
SHANGHAI_TZ = timezone(SHANGHAI_OFFSET, name="Asia/Shanghai")
SENDER_DOMAIN = "reports.neuer.cn"
SENDER_ADDRESS = "security-daily@reports.neuer.cn"
PHONE_IN_TEXT = re.compile(r"(?<!\d)1\d{10}(?!\d)")
BEARER_IN_TEXT = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]+", re.IGNORECASE)
RESEND_EMAIL_PATTERN = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63}"
)
MAX_RESEND_API_KEY_LENGTH = 512
MAX_RESEND_RECIPIENTS = 3
MAX_SECURITY_DAILY_INPUT_BYTES = 384 * 1024
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


def validate_resend_api_key(value: str, *, allow_empty: bool = True) -> str:
    """校验管理员 UI 提交的 Resend Key；空值用于停用或清空配置。"""

    normalized = value.strip()
    if not normalized and allow_empty:
        return normalized
    if not normalized or len(normalized) > MAX_RESEND_API_KEY_LENGTH:
        raise SecurityDailyConfigurationError("Resend Key 不能为空且长度不能超过 512")
    if any(character.isspace() for character in normalized):
        raise SecurityDailyConfigurationError("Resend Key 不能包含空白字符")
    return normalized


def validate_resend_recipients(values: Sequence[str]) -> tuple[str, ...]:
    """校验安全日报收件人；允许暂时为空以便先保存未完成配置。"""

    normalized = tuple(value.strip() for value in values)
    if len(normalized) > MAX_RESEND_RECIPIENTS:
        raise SecurityDailyConfigurationError("安全日报收件人最多 3 个")
    if any(
        len(value) > 254
        or RESEND_EMAIL_PATTERN.fullmatch(value) is None
        or len(value.partition("@")[0]) > 64
        for value in normalized
    ):
        raise SecurityDailyConfigurationError("安全日报收件人地址无效")
    if len({value.casefold() for value in normalized}) != len(normalized):
        raise SecurityDailyConfigurationError("安全日报收件人不能重复")
    return normalized


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
    ssh: list[SecurityDetailRow] = Field(min_length=3, max_length=10)
    web: list[SecurityDetailRow] = Field(min_length=4, max_length=10)
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
    generation_source: GenerationSource
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
class SecurityDailyConfiguration:
    """安全日报 UI 配置；api_key 只在后端与独立 mailer 文件边界流转。"""

    enabled: bool
    api_key: str
    recipients: tuple[str, ...]
    config_version: int = 1

    def __post_init__(self) -> None:
        if (
            not isinstance(self.config_version, int)
            or isinstance(self.config_version, bool)
            or self.config_version < 1
        ):
            raise SecurityDailyConfigurationError("安全日报配置版本无效")


@dataclass(frozen=True, slots=True)
class SecurityDailyAutoDeliveryConfiguration:
    """自动投递只需非敏感配置投影，不承载 Resend Key 或收件地址。"""

    enabled: bool
    resend_configured: bool
    recipient_count: int

    def __post_init__(self) -> None:
        if not 0 <= self.recipient_count <= MAX_RESEND_RECIPIENTS:
            raise SecurityDailyConfigurationError("安全日报收件人数量超出范围")


@dataclass(frozen=True, slots=True)
class SecurityDailyConfigurationUpdate:
    """管理员配置变更；api_key=None 表示保留当前 Key。"""

    enabled: bool
    recipients: tuple[str, ...]
    api_key: str | None = None


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
    config_version: int = 1
    delivery_id: str = ""

    def __post_init__(self) -> None:
        if (
            not isinstance(self.config_version, int)
            or isinstance(self.config_version, bool)
            or self.config_version < 1
        ):
            raise SecurityDailyConfigurationError("安全日报配置版本无效")
        if self.delivery_id and (
            len(self.delivery_id) > 128
            or any(character.isspace() for character in self.delivery_id)
        ):
            raise SecurityDailyConfigurationError("安全日报投递身份无效")


@dataclass(frozen=True, slots=True)
class SecurityDailyAuditEvent:
    """日报展示的审计事件快照；只含非载荷列，不含审计 JSON 明细。"""

    time: str
    actor: str
    source_ip: str
    action: str


@dataclass(frozen=True, slots=True)
class SecurityDailyAuditEvidence:
    """管理审计证据汇总；events 按时间倒序取最近事件。"""

    total: int
    events: tuple[SecurityDailyAuditEvent, ...]
    category_counts: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True, slots=True)
class SecurityDailyPreview:
    report_date: date
    status: SecurityStatus
    html: str
    text: str
    payload: dict[str, Any]
