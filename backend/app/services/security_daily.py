"""安全日报的脱敏契约、受控投递请求和预览服务。"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.core.auth.accounts import SecurityPrincipal

SecurityStatus = Literal["normal", "attention", "high"]
GenerationSource = Literal["auto", "manual"]
GenerationStatus = Literal["pending", "ready", "failed", "unavailable"]
DeliveryStatus = Literal["not_sent", "pending", "sending", "sent", "failed"]
ConfigurationState = Literal["disabled", "dispatcher_missing", "recipients_empty", "ready"]
DeliveryAction = Literal["send", "retry"]
DeliveryRequestState = Literal["pending", "sent", "failed"]

SHANGHAI_OFFSET = timedelta(hours=8)
SHANGHAI_TZ = timezone(SHANGHAI_OFFSET, name="Asia/Shanghai")
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
SSH_FAILED_CONFIRM_THRESHOLD = 20
COUNT_VALUE_PREFIX = re.compile(r"^\s*(?P<count>\d+)(?=$|[\s次条个（])")
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

    def __post_init__(self) -> None:
        if (
            not isinstance(self.config_version, int)
            or isinstance(self.config_version, bool)
            or self.config_version < 1
        ):
            raise SecurityDailyConfigurationError("安全日报配置版本无效")


@dataclass(frozen=True, slots=True)
class SecurityDailyControlResult:
    request_id: UUID
    report_date: date
    state: Literal["sent", "failed"]
    completed_at: datetime
    error: str | None = None


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


class SecurityDailyRepository(Protocol):
    async def configuration(self) -> SecurityDailyConfiguration: ...

    async def auto_delivery_configuration(
        self,
    ) -> SecurityDailyAutoDeliveryConfiguration: ...

    async def audit_evidence(
        self, period_start: datetime, period_end: datetime
    ) -> SecurityDailyAuditEvidence | None: ...

    async def ingest_payload(
        self,
        payload: dict[str, Any],
        *,
        recipient_count: int,
        force: bool = False,
        generation_source: GenerationSource = "auto",
    ) -> bool: ...

    async def mark_unavailable(
        self,
        report_date: date,
        *,
        period_start: datetime,
        period_end: datetime,
        reason: str,
        generation_source: GenerationSource = "auto",
    ) -> bool: ...

    async def update_configuration(
        self,
        update: SecurityDailyConfigurationUpdate,
        *,
        principal: SecurityPrincipal,
        ip: str,
    ) -> SecurityDailyConfiguration: ...

    async def overview(self, *, now: datetime) -> SecurityDailyOverview: ...

    async def list_reports(self, query: SecurityDailyQuery) -> SecurityDailyPage: ...

    async def get_report(self, report_id: int) -> SecurityDailyReportRecord | None: ...

    async def get_latest_report(
        self,
        report_date: date,
        *,
        generation_source: GenerationSource | None = None,
    ) -> SecurityDailyReportRecord | None: ...

    async def exists_sent_delivery(self, report_date: date) -> bool: ...

    async def request_delivery(
        self,
        report: SecurityDailyReportRecord,
        action: DeliveryAction,
        *,
        principal: SecurityPrincipal | None = None,
        ip: str | None = None,
        system: bool = False,
    ) -> SecurityDailyDeliveryRequest: ...

    async def pending_delivery_requests(self) -> tuple[tuple[UUID, date], ...]: ...

    async def apply_control_result(self, result: SecurityDailyControlResult) -> None: ...

    async def mark_request_failed(self, request_id: UUID, message: str) -> None: ...

    async def mark_delivery_failed(self, report_id: int, message: str) -> bool: ...


async def generate_security_daily_for_date(
    repository: SecurityDailyRepository,
    control_dir: Path,
    *,
    report_date: date,
    recipient_count: int,
    force: bool = False,
    generation_source: GenerationSource = "auto",
    generated_at: datetime | None = None,
) -> bool:
    """读取指定上海自然日的脱敏快照并写入日报事实表。

    定时任务和管理员手动生成共用此入口，确保两条路径使用完全相同的
    文件大小、JSON 结构和 unavailable 语义；该函数只处理已脱敏结构化证据，
    不会触发邮件投递。记录与 payload 的 generated_at 使用平台生成时刻，
    而不是采集器写快照的时刻，保证每次生成都有独立可区分的生成时间。
    """

    period_start = datetime.combine(report_date, datetime.min.time(), tzinfo=SHANGHAI_TZ)
    period_end = datetime.combine(
        report_date,
        datetime.max.time().replace(microsecond=0),
        tzinfo=SHANGHAI_TZ,
    )
    generation_time = generated_at or datetime.now(SHANGHAI_TZ)
    source = control_dir / "incoming" / f"{report_date.isoformat()}.json"
    try:
        if not source.is_file() or source.stat().st_size > MAX_SECURITY_DAILY_INPUT_BYTES:
            return await repository.mark_unavailable(
                report_date,
                period_start=period_start,
                period_end=period_end,
                reason="安全日报证据源不可用",
                generation_source=generation_source,
            )
        raw = await asyncio.to_thread(source.read_text, encoding="utf-8")
        value: Any = json.loads(raw)
        if not isinstance(value, dict):
            raise SecurityDailyValidationError("security report input must be an object")
        # 平台侧派生计算只消费已通过结构、时间和脱敏校验的快照；缺列或
        # 不完整输入必须显式记为 unavailable，不能在 enrich 阶段异常退出。
        value = validate_security_daily_payload(value)
        evidence = await repository.audit_evidence(period_start, period_end)
        if evidence is not None:
            value = _enrich_audit_evidence(value, evidence)
        value = _finalize_security_daily_payload(value)
        previous = await repository.get_latest_report(report_date - timedelta(days=1))
        if (
            previous is not None
            and previous.generation_status == "ready"
            and previous.payload is not None
        ):
            value = _enrich_day_over_day(value, previous.payload)
        value["generated_at"] = generation_time.isoformat()
        return await repository.ingest_payload(
            value,
            recipient_count=recipient_count,
            force=force,
            generation_source=generation_source,
        )
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
        SecurityDailyValidationError,
    ):
        return await repository.mark_unavailable(
            report_date,
            period_start=period_start,
            period_end=period_end,
            reason="安全日报证据源校验失败",
            generation_source=generation_source,
        )


AUDIT_ASSESSMENT_BY_ACTION: dict[str, str] = {
    "login": "认证事件",
    "session_refresh": "认证事件",
    "logout": "认证事件",
    "local_password_change": "密码安全操作",
    "local_password_reset": "密码安全操作",
    "role_override": "账号与角色管理",
    "account_status_change": "账号与角色管理",
    "force_logout": "账号与角色管理",
    "config_update": "系统配置变更",
    "security_daily_config_update": "系统配置变更",
    "auth_provider_save_draft": "认证源配置变更",
    "auth_provider_test": "认证源配置变更",
    "auth_provider_activate": "认证源配置变更",
    "auth_provider_disable": "认证源配置变更",
    "auth_provider_role_mappings_replace": "认证源配置变更",
    "app_create": "应用密钥管理",
    "app_update": "应用密钥管理",
    "app_disable": "应用密钥管理",
    "app_rotate_key": "应用密钥管理",
    "app_revoke_old_key": "应用密钥管理",
    "app_rotate_callback_secret": "应用密钥管理",
    "approval_decision": "审批操作",
    "message_send": "发送操作",
    "batch_cancel": "发送操作",
    "batch_reschedule": "发送操作",
    "batch_resend_failed": "发送操作",
    "blacklist_add": "管控操作",
    "blacklist_delete": "管控操作",
    "sensitive_word_add": "管控操作",
    "sensitive_word_delete": "管控操作",
    "reply_optout": "退订操作",
}

AUDIT_CATEGORY_BY_ACTION: dict[str, str] = {
    "login": "登录认证",
    "session_refresh": "登录认证",
    "logout": "登录认证",
    "local_password_change": "密码安全",
    "local_password_reset": "密码安全",
    "role_override": "账号与角色",
    "account_status_change": "账号与角色",
    "force_logout": "账号与角色",
    "config_update": "系统配置",
    "security_daily_config_update": "系统配置",
    "auth_provider_save_draft": "认证源配置",
    "auth_provider_test": "认证源配置",
    "auth_provider_activate": "认证源配置",
    "auth_provider_disable": "认证源配置",
    "auth_provider_role_mappings_replace": "认证源配置",
    "app_create": "应用密钥",
    "app_update": "应用密钥",
    "app_disable": "应用密钥",
    "app_rotate_key": "应用密钥",
    "app_revoke_old_key": "应用密钥",
    "app_rotate_callback_secret": "应用密钥",
    "approval_decision": "审批操作",
    "message_send": "发送操作",
    "batch_cancel": "发送操作",
    "batch_reschedule": "发送操作",
    "batch_resend_failed": "发送操作",
    "blacklist_add": "管控操作",
    "blacklist_delete": "管控操作",
    "sensitive_word_add": "管控操作",
    "sensitive_word_delete": "管控操作",
    "reply_optout": "退订操作",
    "export_create": "数据导出",
    "export_download": "数据导出",
    "import_create": "号码导入",
    "import_resolve": "号码导入",
}


def _audit_category(action: str) -> str:
    """把审计 action 归为邮件可读的类别；未知动作归入其他管理操作。"""

    return AUDIT_CATEGORY_BY_ACTION.get(action, "其他管理操作")


AUDIT_TONE_BY_ACTION: dict[str, str] = {
    "login": "good",
    "session_refresh": "good",
    "logout": "good",
    "local_password_change": "warn",
    "local_password_reset": "warn",
    "role_override": "warn",
    "account_status_change": "warn",
    "force_logout": "warn",
    "config_update": "warn",
    "security_daily_config_update": "warn",
    "auth_provider_save_draft": "warn",
    "auth_provider_activate": "warn",
    "auth_provider_disable": "warn",
    "auth_provider_role_mappings_replace": "warn",
    "app_rotate_key": "warn",
    "app_revoke_old_key": "warn",
    "app_rotate_callback_secret": "warn",
}


def _audit_source_display(value: str) -> str:
    """最小化日报中的审计来源地址；审计事实记录仍保留原始值。"""

    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return value
    if isinstance(address, ipaddress.IPv4Address):
        return str(address)
    if address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    if not address.is_global:
        return f"{address.compressed}（IPv6）"
    network = ipaddress.ip_network(f"{address.compressed}/64", strict=False)
    return f"{network.network_address.compressed}/64（IPv6，脱敏）"


def _enrich_audit_evidence(
    payload: dict[str, Any], evidence: SecurityDailyAuditEvidence
) -> dict[str, Any]:
    """把平台审计摘要注入日报，并翻转管理审计覆盖状态。"""

    payload["audit"] = [
        {
            "time": event.time,
            "actor": event.actor,
            "source_ip": _audit_source_display(event.source_ip),
            "action": event.action,
            "assessment": AUDIT_ASSESSMENT_BY_ACTION.get(event.action, "管理操作"),
            "tone": AUDIT_TONE_BY_ACTION.get(event.action, "neutral"),
        }
        for event in evidence.events[:10]
    ]
    for item in payload["coverage"]:
        if item["source"] == "管理审计":
            item["status"] = "完整"
            item["note"] = "按平台审计事实表聚合，不读取载荷明细"
            item["tone"] = "good"
    if evidence.total > 0 and evidence.category_counts:
        category_text = "、".join(
            f"{label} {count}" for label, count in evidence.category_counts[:3]
        )
        payload["summary"] = (
            f"{payload['summary']} 管理审计共 {evidence.total} 条：{category_text}。"
        )
    return payload


def _count_from_value(value: str) -> int | None:
    """解析指标前缀计数；不可解析时返回 None，禁止静默伪造为 0。"""

    match = COUNT_VALUE_PREFIX.match(str(value))
    return int(match.group("count")) if match is not None else None


def _count_delta_suffix(current: str, previous: str) -> str | None:
    """生成“（昨日 N，↑x%）”环比后缀；任一值不可解析时返回 None。"""

    current_count = _count_from_value(current)
    previous_count = _count_from_value(previous)
    if current_count is None or previous_count is None:
        return None
    if current_count == previous_count:
        return "（与昨日持平）"
    if previous_count == 0:
        return f"（昨日 0，新增 {current_count}）"
    delta = current_count - previous_count
    numerator = abs(delta) * 100
    # 常规四舍五入，避免 bankers rounding 把 12.5% 显示为 12%。
    percent = (2 * numerator + previous_count) // (2 * previous_count)
    arrow = "↑" if delta > 0 else "↓"
    return f"（昨日 {previous_count}，{arrow}{percent}%）"


def _labeled_row(
    rows: Sequence[dict[str, Any]], *labels: str
) -> dict[str, Any] | None:
    """按语义标签定位行，避免展示项重排后把不同指标互相比较。"""

    expected = frozenset(labels)
    return next((item for item in rows if str(item.get("label")) in expected), None)


def _enrich_day_over_day(payload: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    """在平台侧追加昨日环比：SSH 失败、Web 5xx、敏感路径三个关键面。"""

    comparisons: list[str] = []
    specifications = (
        ("metrics", ("SSH 认证失败", "攻击尝试"), "SSH 失败认证"),
        ("metrics", ("Web/API 5xx",), "Web 5xx"),
        ("web", ("敏感路径",), "敏感路径"),
    )
    for section, labels, label in specifications:
        current = _labeled_row(payload[section], *labels)
        old = _labeled_row(previous.get(section, []), *labels)
        if current is None or old is None:
            continue
        suffix = _count_delta_suffix(current["value"], old["value"])
        if suffix is None:
            continue
        current["value"] = f"{current['value']}{suffix}"
        comparisons.append(f"{label}{suffix}")
    if comparisons:
        payload["summary"] = f"{payload['summary']} 较昨日：{'、'.join(comparisons)}。"
    return payload


def _finalize_security_daily_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """在平台侧重算状态/待确认/建议，覆盖采集快照与审计注入后的口径漂移。"""

    gaps = [str(item["source"]) for item in payload["coverage"] if item.get("status") != "完整"]
    ssh_metric = _labeled_row(payload["metrics"], "SSH 认证失败", "攻击尝试")
    web_5xx_metric = _labeled_row(payload["metrics"], "Web/API 5xx")
    gap_metric = _labeled_row(payload["metrics"], "证据覆盖缺口")
    sensitive_row = _labeled_row(payload["web"], "敏感路径")
    runtime_unhealthy_row = _labeled_row(payload["runtime"], "异常容器")

    ssh_failed = (
        _count_from_value(str(ssh_metric["value"])) if ssh_metric is not None else None
    )
    web_5xx = (
        _count_from_value(str(web_5xx_metric["value"]))
        if web_5xx_metric is not None
        else None
    )
    web_sensitive = (
        _count_from_value(str(sensitive_row["value"]))
        if sensitive_row is not None
        else None
    )
    runtime_unhealthy = (
        _count_from_value(str(runtime_unhealthy_row["value"]))
        if runtime_unhealthy_row is not None
        else None
    )
    ssh_failed_count = ssh_failed or 0
    web_5xx_count = web_5xx or 0
    web_sensitive_count = web_sensitive or 0
    runtime_unhealthy_count = runtime_unhealthy or 0
    ssh_needs_confirm = (
        ssh_failed is not None and ssh_failed >= SSH_FAILED_CONFIRM_THRESHOLD
    )
    status = (
        "high"
        if web_sensitive_count
        else (
            "attention"
            if gaps
            or ssh_needs_confirm
            or web_5xx_count
            or runtime_unhealthy_count
            else "normal"
        )
    )
    pending_items: list[str] = []
    if web_sensitive_count:
        pending_items.append(f"敏感路径命中 {web_sensitive_count} 次，需要核查访问来源")
    if gaps:
        pending_items.append(f"待接入证据源：{'、'.join(gaps)}")
    if ssh_needs_confirm:
        pending_items.append("存在 SSH 失败认证，需要确认是否属于授权运维")
    if web_5xx_count:
        pending_items.append(f"存在 {web_5xx_count} 次 Web/API 5xx，需要核查服务异常")
    if runtime_unhealthy_count:
        pending_items.append(f"存在 {runtime_unhealthy_count} 个异常容器，需要核查运行态")
    pending = "；".join(pending_items) + "。" if pending_items else "无待确认事项。"
    actions: list[dict[str, str]] = []
    if web_sensitive_count:
        actions.append(
            {
                "priority": "high",
                "title": "核查敏感路径命中",
                "detail": "访问日志发现敏感路径命中；日报不保留原始路径，请到受控日志平台核对。",
            }
        )
    if gaps:
        actions.append(
            {
                "priority": "medium",
                "title": "补齐日报证据源",
                "detail": f"接入{'、'.join(gaps)}后，下一日报才可覆盖完整安全面。",
            }
        )
    if ssh_needs_confirm:
        actions.append(
            {
                "priority": "medium",
                "title": "核查 SSH 失败认证",
                "detail": (
                    f"当日发现 {ssh_failed_count} 次 SSH 失败认证；日报不保留来源明细，"
                    "请结合主机安全日志核对。"
                ),
            }
        )
    if web_5xx_count:
        actions.append(
            {
                "priority": "medium",
                "title": "核查 Web/API 5xx",
                "detail": (
                    f"当日发现 {web_5xx_count} 次服务端错误，请结合受控访问日志核对。"
                ),
            }
        )
    if runtime_unhealthy_count:
        actions.append(
            {
                "priority": "medium",
                "title": "核查平台异常容器",
                "detail": (
                    f"采集时发现 {runtime_unhealthy_count} 个容器未运行、暂停、重启或"
                    "健康检查失败，请核对当前运行态。"
                ),
            }
        )
    payload["status"] = status
    payload["pending_confirmation"] = pending
    payload["actions"] = actions
    if gap_metric is not None:
        gap_metric["value"] = str(len(gaps))
        gap_metric["tone"] = "warn" if gaps else "good"
        gap_metric["note"] = "缺口数量"
    if ssh_failed_count and not ssh_needs_confirm and ssh_metric is not None:
        ssh_metric["tone"] = "neutral"
        ssh_metric["note"] = "低于关注阈值，无需人工确认"
    return payload


def _problem_payload(
    report_date: date,
    *,
    period_start: datetime,
    period_end: datetime,
    generated_at: datetime,
    reason: str,
) -> dict[str, Any]:
    """构建“证据不可用”问题通报 payload；指标一律显式不可用，不虚构数值。"""

    window = f"{report_date.isoformat()} 00:00 — 23:59（UTC+8）"
    unavailable = {"value": "不可用", "tone": "warn", "note": "证据缺失"}
    detail = {"value": "不可用", "assessment": "证据缺失", "tone": "warn"}
    payload = {
        "schema_version": 1,
        "report_date": report_date.isoformat(),
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "generated_at": generated_at.isoformat(),
        "status": "attention",
        "summary": f"安全日报证据不可用：{reason}。未生成任何脱敏指标，请检查采集器与日志源。",
        "pending_confirmation": "需要人工核查证据源状态。",
        "metrics": [
            {"label": "SSH 认证失败", **unavailable},
            {"label": "SSH 成功认证", **unavailable},
            {"label": "Fail2ban 封禁", **unavailable},
            {"label": "Web/API 5xx", **unavailable},
            {"label": "证据覆盖缺口", "value": "全部", "tone": "danger", "note": "证据源不可用"},
        ],
        "ssh": [
            {"label": "失败认证", **detail},
            {"label": "成功认证", **detail},
            {"label": "Fail2ban", **detail},
        ],
        "web": [
            {"label": "访问请求", **detail},
            {"label": "拒绝请求", **detail},
            {"label": "服务端错误", **detail},
            {"label": "敏感路径", **detail},
        ],
        "audit": [],
        "runtime": [
            {
                "label": "证据采集器",
                "value": "未产出快照",
                "assessment": "需要核查",
                "tone": "warn",
            }
        ],
        "actions": [
            {
                "priority": "high",
                "title": "核查安全日报证据源",
                "detail": f"{reason}；请检查主机采集器、日志文件与权限后重新生成。",
            }
        ],
        "coverage": [
            {
                "source": name,
                "window": window,
                "status": "缺失",
                "note": "证据源不可用",
                "tone": "warn",
            }
            for name in (
                "SSH journal",
                "Fail2ban",
                "Web/API access log",
                "管理审计",
                "运行态探针",
            )
        ],
    }
    return validate_security_daily_payload(payload)


class SecurityDailyControl(Protocol):
    async def sync_configuration(self, configuration: SecurityDailyConfiguration) -> None: ...

    async def submit(
        self,
        request: SecurityDailyDeliveryRequest,
        payload: dict[str, Any],
    ) -> None: ...

    async def result(self, request_id: UUID) -> SecurityDailyControlResult | None: ...


class FileSecurityDailyControl:
    """通过请求目录和独立配置目录与 mailer 交换日报配置和投递请求。"""

    def __init__(self, control_dir: Path, config_dir: Path) -> None:
        self.control_dir = control_dir
        self.config_dir = config_dir
        self.request_dir = control_dir / "requests"
        self.result_dir = control_dir / "results"
        self.config_path = config_dir / "resend.json"

    async def sync_configuration(self, configuration: SecurityDailyConfiguration) -> None:
        await asyncio.to_thread(self._write_configuration, configuration)

    def _write_configuration(self, configuration: SecurityDailyConfiguration) -> None:
        """原子同步 UI 配置；文件仅挂载给独立 mailer 读取。"""

        temporary = self.config_dir / ".resend.json.tmp"
        try:
            self.config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(
                    {
                        "api_key": validate_resend_api_key(configuration.api_key),
                        "recipients": list(
                            validate_resend_recipients(configuration.recipients)
                        ),
                        "config_version": configuration.config_version,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.config_path)
        except (OSError, SecurityDailyConfigurationError) as error:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
            raise SecurityDailyControlError("安全日报配置同步失败") from error

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
            "config_version": request.config_version,
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
        if record.generation_status == "unavailable":
            event_type = "evidence_unavailable"
            label = "证据不可用"
        elif record.generation_status == "failed":
            event_type = "generation_failed"
            label = "生成失败"
        else:
            event_type = "delivery_failed"
            label = "投递失败"
        events.append(
            {
                "type": event_type,
                "at": record.last_error_at,
                "label": label,
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
        (
            "今日需要你处理",
            "\n".join(
                f"[{item['priority']}] {item['title']}: {item['detail']}"
                for item in validated["actions"]
            )
            or "今日无需处理",
        ),
    ]
    for key, label in (
        ("metrics", "核心指标"),
        ("ssh", "SSH 与主机安全"),
        ("web", "Web/API"),
        ("audit", "管理审计"),
        ("runtime", "运行状态"),
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
    """编排日报查询、UI 配置和独立 mailer 投递请求。"""

    _PENDING_DELIVERY_RECOVERY_DELAY = timedelta(minutes=5)

    def __init__(
        self,
        repository: SecurityDailyRepository,
        control: SecurityDailyControl,
        *,
        control_dir: Path | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(SHANGHAI_TZ),
    ) -> None:
        self.repository = repository
        self.control = control
        self.control_dir = control_dir
        self.clock = clock

    async def configuration(self) -> SecurityDailyConfiguration:
        return await self.repository.configuration()

    async def configure(
        self,
        update: SecurityDailyConfigurationUpdate,
        *,
        principal: SecurityPrincipal,
        ip: str,
    ) -> SecurityDailyConfiguration:
        """保存管理员配置并同步独立 mailer；Key 不进入日报投递请求。"""

        configuration = await self.repository.update_configuration(
            update,
            principal=principal,
            ip=ip,
        )
        await self.control.sync_configuration(configuration)
        return configuration

    async def overview(self) -> SecurityDailyOverview:
        await self._synchronize_control_results()
        now = self.clock()
        return await self.repository.overview(now=now)

    async def list_reports(self, query: SecurityDailyQuery) -> SecurityDailyPage:
        await self._synchronize_control_results()
        return await self.repository.list_reports(query)

    async def get_report(self, report_id: int) -> SecurityDailyReportRecord:
        await self._synchronize_control_results()
        record = await self.repository.get_report(report_id)
        if record is None:
            raise SecurityDailyNotFound(str(report_id))
        return record

    async def generate_latest(
        self,
        *,
        principal: SecurityPrincipal,
        ip: str,
    ) -> SecurityDailyReportRecord:
        """手动无条件重生成上一上海自然日并立即提交投递。

        每次生成都新增一条记录：证据可用时发送脱敏报告；证据不可用时发送
        问题通报并保留 unavailable 记录。投递请求处理中拒绝重生成。
        """

        configuration = await self.repository.configuration()
        if not configuration.enabled:
            raise SecurityDailyUnavailable("安全日报尚未启用")
        if not configuration.api_key or not configuration.recipients:
            raise SecurityDailyUnavailable("安全日报发信配置不完整，无法生成并发送")
        if self.control_dir is None:
            raise SecurityDailyUnavailable("安全日报生成控制目录不可用")

        report_date = self.clock().astimezone(SHANGHAI_TZ).date() - timedelta(days=1)
        existing = await self.repository.get_latest_report(report_date)
        if existing is not None and existing.delivery_status in {"pending", "sending"}:
            raise SecurityDailyStateConflict("该日日报存在处理中的投递请求，请稍后再生成")
        changed = await generate_security_daily_for_date(
            self.repository,
            self.control_dir,
            report_date=report_date,
            recipient_count=len(configuration.recipients),
            force=True,
            generation_source="manual",
            generated_at=self.clock(),
        )
        if not changed:
            raise SecurityDailyUnavailable("证据源不可用，未生成新日报，未发送邮件")
        record = await self.repository.get_latest_report(report_date)
        if record is None:
            raise SecurityDailyUnavailable("安全日报生成未产出记录")
        payload_override: dict[str, Any] | None = None
        if record.generation_status != "ready" or record.payload is None:
            payload_override = _problem_payload(
                record.report_date,
                period_start=record.period_start,
                period_end=record.period_end,
                generated_at=self.clock(),
                reason=record.last_error or "证据源不可用",
            )
        await self.request_delivery(
            record.id,
            "send",
            principal=principal,
            ip=ip,
            payload_override=payload_override,
        )
        refreshed = await self.repository.get_report(record.id)
        if refreshed is None:
            raise SecurityDailyUnavailable("安全日报生成未产出记录")
        return refreshed

    async def submit_auto_delivery(self, report_date: date) -> SecurityDailyDeliveryRequest | None:
        """自动路径：无论正常报告还是证据不可用，都向收件人提交一次通知。

        每天只提交一次；当天已有任意记录投递成功（含手动）则跳过；发信配置
        不完整时显式记录失败原因，恢复配置后自动补发。
        """

        configuration = await self.repository.auto_delivery_configuration()
        if not configuration.enabled:
            return None
        if await self.repository.exists_sent_delivery(report_date):
            return None
        record = await self.repository.get_latest_report(
            report_date, generation_source="auto"
        )
        if record is None:
            return None
        if record.delivery_status == "sent":
            return None
        if record.delivery_status in {"pending", "sending"}:
            pending_age = self.clock() - record.updated_at
            if pending_age < self._PENDING_DELIVERY_RECOVERY_DELAY:
                return None
        if record.delivery_status == "failed" and not (record.last_error or "").startswith(
            "安全日报发信配置不完整"
        ):
            return None
        if not configuration.resend_configured or configuration.recipient_count == 0:
            await self.repository.mark_delivery_failed(
                record.id,
                "安全日报发信配置不完整（缺少 Resend Key 或收件人）",
            )
            return None
        if (
            record.generation_status == "ready"
            and record.payload is not None
            and record.generated_at is not None
            and self.control_dir is not None
        ):
            snapshot = self.control_dir / "incoming" / f"{report_date.isoformat()}.json"
            try:
                snapshot_mtime = snapshot.stat().st_mtime
            except OSError:
                snapshot_mtime = None
            if snapshot_mtime is not None and snapshot_mtime > record.generated_at.timestamp():
                # 快照比已入库 payload 更新：直接发送会外发过期数据，
                # 跳过自动投递，等待管理员手动“立即生成”后发送。
                return None
        payload_override: dict[str, Any] | None = None
        if record.generation_status != "ready" or record.payload is None:
            payload_override = _problem_payload(
                record.report_date,
                period_start=record.period_start,
                period_end=record.period_end,
                generated_at=self.clock(),
                reason=record.last_error or "证据源不可用",
            )
        return await self.request_delivery(
            record.id,
            "send",
            system=True,
            payload_override=payload_override,
        )

    async def preview(self, report_id: int) -> SecurityDailyPreview:
        record = await self.get_report(report_id)
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
        report_id: int,
        action: DeliveryAction,
        *,
        principal: SecurityPrincipal | None = None,
        ip: str | None = None,
        system: bool = False,
        payload_override: dict[str, Any] | None = None,
    ) -> SecurityDailyDeliveryRequest:
        overview = await self.overview()
        if not overview.enabled:
            raise SecurityDailyUnavailable("安全日报尚未启用")
        record = await self.get_report(report_id)
        submit_payload = payload_override
        if submit_payload is None and (
            record.payload is None or record.generation_status != "ready"
        ):
            raise SecurityDailyUnavailable("日报数据不可用，不能投递")
        if submit_payload is None:
            submit_payload = record.payload
        assert submit_payload is not None
        if action == "retry" and record.delivery_status != "failed":
            raise SecurityDailyStateConflict("只有投递失败的日报允许重试")
        request = await self.repository.request_delivery(
            record,
            action,
            principal=principal,
            ip=ip,
            system=system,
        )
        resubmit_pending = (
            request.idempotent and request.state == "pending" and action == "send"
        )
        # 控制文件丢失时复用同一 request_id 重建请求，保留 Resend 幂等边界；
        # 已完成请求仍只返回事实，不再次接触投递器。
        if request.idempotent and not resubmit_pending:
            return request
        try:
            if not system:
                await self.control.sync_configuration(await self.repository.configuration())
            await self.control.submit(request, submit_payload)
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
