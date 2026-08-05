"""sys_config 的类型化运行策略与单点解析。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import IPv4Network, IPv6Network, ip_network
from typing import Any

from sqlalchemy import text

from app.core.runtime_resources import database_engine
from app.settings import Settings, get_settings

GROUP_SCHEDULING = "运行调度"
GROUP_SENDING = "发送策略"
GROUP_SECURITY = "安全控制"
GROUP_ALERTING = "告警通知"
GROUP_LIFECYCLE = "生命周期"
CONFIG_GROUP_ORDER: tuple[str, ...] = (
    GROUP_SCHEDULING,
    GROUP_SENDING,
    GROUP_SECURITY,
    GROUP_ALERTING,
    GROUP_LIFECYCLE,
)


@dataclass(frozen=True, slots=True)
class ConfigSpec:
    """单个系统参数的注册元数据；minimum/maximum 仅对 int 参数生效。"""

    default: str
    value_type: str
    group: str
    minimum: int | None = None
    maximum: int | None = None
    allow_zero: bool = False
    beat_restart: bool = False


# 系统参数唯一注册表：新增参数只需在此登记并同步 schema.sql seed。
CONFIG_SPECS: dict[str, ConfigSpec] = {
    "approval_threshold": ConfigSpec("100", "int", GROUP_SENDING, maximum=1_000_000),
    "market_approval_threshold": ConfigSpec(
        "50", "int", GROUP_SENDING, maximum=1_000_000
    ),
    "approval_expire_hours": ConfigSpec("24", "int", GROUP_SENDING, maximum=168),
    "market_send_window": ConfigSpec("08:00-21:00", "str", GROUP_SENDING),
    "vendor_batch_size": ConfigSpec("500", "int", GROUP_SCHEDULING, maximum=1_000),
    "vendor_qps": ConfigSpec("5", "int", GROUP_SCHEDULING, maximum=1_000),
    "reserved_realtime_qps": ConfigSpec(
        "2", "int", GROUP_SCHEDULING, maximum=999, allow_zero=True
    ),
    "report_poll_seconds": ConfigSpec(
        "60", "int", GROUP_SCHEDULING, minimum=10, maximum=3_600, beat_restart=True
    ),
    "reply_poll_seconds": ConfigSpec(
        "300", "int", GROUP_SCHEDULING, minimum=30, maximum=86_400, beat_restart=True
    ),
    "balance_poll_seconds": ConfigSpec(
        "600", "int", GROUP_SCHEDULING, minimum=60, maximum=86_400, beat_restart=True
    ),
    "approval_scan_seconds": ConfigSpec(
        "300", "int", GROUP_SCHEDULING, minimum=30, maximum=3_600, beat_restart=True
    ),
    "scheduled_scan_seconds": ConfigSpec(
        "60", "int", GROUP_SCHEDULING, minimum=10, maximum=3_600, beat_restart=True
    ),
    "balance_alert_threshold": ConfigSpec(
        "10000", "int", GROUP_ALERTING, maximum=100_000_000
    ),
    "fail_rate_threshold": ConfigSpec("20", "int", GROUP_ALERTING, maximum=100),
    "fail_rate_min_total": ConfigSpec("50", "int", GROUP_ALERTING, maximum=1_000_000),
    "report_timeout_hours": ConfigSpec("48", "int", GROUP_SCHEDULING, maximum=720),
    "uncertain_alert_hours": ConfigSpec("24", "int", GROUP_ALERTING, maximum=720),
    "reconcile_interval_min": ConfigSpec(
        "5", "int", GROUP_SCHEDULING, maximum=60, beat_restart=True
    ),
    "msg_retention_months": ConfigSpec("12", "int", GROUP_LIFECYCLE, maximum=120),
    "audit_retention_months": ConfigSpec("36", "int", GROUP_LIFECYCLE, maximum=120),
    "raw_log_retention_days": ConfigSpec("90", "int", GROUP_LIFECYCLE, maximum=3_650),
    "import_expire_hours": ConfigSpec("24", "int", GROUP_LIFECYCLE, maximum=168),
    "export_retention_days": ConfigSpec("7", "int", GROUP_LIFECYCLE, maximum=90),
    "sensitive_hit_action": ConfigSpec("block", "str", GROUP_SENDING),
    "key_grace_hours": ConfigSpec("72", "int", GROUP_SECURITY, maximum=720),
    "login_fail_limit": ConfigSpec("5", "int", GROUP_SECURITY, maximum=20),
    "login_lock_minutes": ConfigSpec("15", "int", GROUP_SECURITY, maximum=1_440),
    "login_ip_fail_limit": ConfigSpec("20", "int", GROUP_SECURITY, maximum=1_000),
    "login_ip_ban_minutes": ConfigSpec("15", "int", GROUP_SECURITY, maximum=1_440),
    "callback_timeout_seconds": ConfigSpec("5", "int", GROUP_SECURITY, maximum=10),
    "callback_retry_schedule": ConfigSpec(
        "60,300,900,3600,3600", "str", GROUP_SECURITY
    ),
    "callback_allow_cidrs": ConfigSpec(
        "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16", "str", GROUP_SECURITY
    ),
    "alert_wecom_webhook": ConfigSpec("", "str", GROUP_ALERTING),
    "alert_mail_to": ConfigSpec("", "str", GROUP_ALERTING),
    "alert_smtp_host": ConfigSpec("smtp", "str", GROUP_ALERTING),
    "alert_smtp_port": ConfigSpec("25", "int", GROUP_ALERTING, maximum=65_535),
    "alert_mail_from": ConfigSpec("sms-platform@localhost", "str", GROUP_ALERTING),
    "unsubscribe_suffix": ConfigSpec("回T退订", "str", GROUP_SENDING),
    "unsubscribe_auto_append": ConfigSpec("true", "bool", GROUP_SENDING),
    "verify_freq_per_minute": ConfigSpec("1", "int", GROUP_SENDING, maximum=100),
    "verify_freq_per_day": ConfigSpec("10", "int", GROUP_SENDING, maximum=10_000),
    "market_freq_per_day": ConfigSpec("1", "int", GROUP_SENDING, maximum=1_000),
    "import_max_mb": ConfigSpec("10", "int", GROUP_SENDING, maximum=10),
    "import_max_rows": ConfigSpec("50000", "int", GROUP_SENDING, maximum=50_000),
    "test_send_max": ConfigSpec("5", "int", GROUP_SENDING, maximum=5),
    "verify_otp_mask": ConfigSpec("true", "bool", GROUP_SENDING),
    "unmatched_retention_days": ConfigSpec(
        "90", "int", GROUP_LIFECYCLE, maximum=3_650
    ),
    "anomaly_enabled": ConfigSpec("true", "bool", GROUP_ALERTING),
    "anomaly_multiplier": ConfigSpec("3", "int", GROUP_ALERTING, maximum=100),
    "anomaly_min_total": ConfigSpec("500", "int", GROUP_ALERTING, maximum=1_000_000),
    "anomaly_scan_minutes": ConfigSpec(
        "60", "int", GROUP_SCHEDULING, minimum=5, maximum=1_440, beat_restart=True
    ),
    "job_history_days": ConfigSpec("30", "int", GROUP_LIFECYCLE, maximum=365),
    "usage_projection_reconcile_seconds": ConfigSpec(
        "300", "int", GROUP_SCHEDULING, minimum=60, maximum=86_400, beat_restart=True
    ),
    "usage_ledger_retention_days": ConfigSpec(
        "90", "int", GROUP_LIFECYCLE, maximum=3_650
    ),
    "security_daily_enabled": ConfigSpec("false", "bool", GROUP_SECURITY),
    "security_daily_recipient_count": ConfigSpec(
        "0", "int", GROUP_SECURITY, maximum=3, allow_zero=True
    ),
    "security_daily_resend_api_key": ConfigSpec("", "str", GROUP_SECURITY),
    "security_daily_resend_configured": ConfigSpec("false", "bool", GROUP_SECURITY),
}

DEFAULTS: dict[str, str] = {key: spec.default for key, spec in CONFIG_SPECS.items()}

BEAT_STARTUP_ONLY_KEYS = frozenset(
    key for key, spec in CONFIG_SPECS.items() if spec.beat_restart
)
INT_CONFIG_KEYS = frozenset(
    key for key, spec in CONFIG_SPECS.items() if spec.value_type == "int"
)
BOOL_CONFIG_KEYS = frozenset(
    key for key, spec in CONFIG_SPECS.items() if spec.value_type == "bool"
)

APPROVED_IPV4_CALLBACK_NETWORKS = (
    IPv4Network("10.0.0.0/8"),
    IPv4Network("172.16.0.0/12"),
    IPv4Network("192.168.0.0/16"),
)
APPROVED_IPV6_CALLBACK_NETWORKS = (IPv6Network("fc00::/7"),)

_MAILBOX_RE = re.compile(r"^[^\s,@]+@[^\s,@]+$")


class InvalidRuntimePolicy(ValueError):
    """运行策略值不满足格式或跨字段约束。"""


def parse_private_callback_cidrs(raw: str) -> tuple[IPv4Network | IPv6Network, ...]:
    """只接受 RFC1918 IPv4 或 ULA IPv6 的相同/更细子网。"""

    items = raw.split(",")
    try:
        if not items or any(not item.strip() for item in items):
            raise ValueError
        networks = tuple(ip_network(item.strip(), strict=False) for item in items)
    except ValueError as error:
        raise InvalidRuntimePolicy("callback_allow_cidrs 包含无效 CIDR") from error
    for network in networks:
        approved = (
            any(network.subnet_of(root) for root in APPROVED_IPV4_CALLBACK_NETWORKS)
            if isinstance(network, IPv4Network)
            else any(network.subnet_of(root) for root in APPROVED_IPV6_CALLBACK_NETWORKS)
        )
        if not approved:
            raise InvalidRuntimePolicy("callback_allow_cidrs 只允许批准私网的相同或更细子网")
    return networks


def _positive(values: Mapping[str, str], key: str, *, allow_zero: bool = False) -> int:
    raw = values[key].strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise InvalidRuntimePolicy(f"{key} 必须为整数") from error
    if str(value) != raw or value < (0 if allow_zero else 1):
        raise InvalidRuntimePolicy(f"{key} 必须为正整数")
    return value


def _boolean(values: Mapping[str, str], key: str) -> bool:
    raw = values[key].strip().casefold()
    if raw not in {"true", "false"}:
        raise InvalidRuntimePolicy(f"{key} 必须为布尔值 true/false")
    return raw == "true"


def _validate_mail_list(key: str, raw: str, *, allow_empty: bool) -> None:
    """轻量邮箱格式守门：告警通道只防手误，完整可达性由出站校验负责。"""

    text_value = raw.strip()
    if not text_value:
        if allow_empty:
            return
        raise InvalidRuntimePolicy(f"{key} 必须为合法邮箱地址")
    for address in text_value.split(","):
        if not _MAILBOX_RE.fullmatch(address.strip()):
            raise InvalidRuntimePolicy(f"{key} 必须为逗号分隔的合法邮箱地址")


@dataclass(frozen=True, slots=True)
class RuntimePolicy:
    """在一个请求或任务边界读取的一致配置快照。"""

    login_fail_limit: int
    login_lock_minutes: int
    login_ip_fail_limit: int
    login_ip_ban_minutes: int
    callback_timeout_seconds: int
    callback_retry_schedule: tuple[int, ...]
    callback_allow_cidrs: str
    import_max_mb: int
    import_max_rows: int
    import_expire_hours: int
    test_send_max: int
    uncertain_alert_hours: int
    vendor_qps: int
    reserved_realtime_qps: int
    balance_alert_threshold: int
    unsubscribe_suffix: str
    unsubscribe_auto_append: bool
    verify_otp_mask: bool
    verify_freq_per_minute: int
    verify_freq_per_day: int
    market_freq_per_day: int
    market_send_window: str
    sensitive_hit_action: str
    approval_threshold: int
    market_approval_threshold: int
    approval_expire_hours: int

    @classmethod
    def from_mapping(cls, supplied: Mapping[str, Any]) -> RuntimePolicy:
        values = DEFAULTS | {key: str(value) for key, value in supplied.items()}
        parsed: dict[str, int] = {}
        for key, spec in CONFIG_SPECS.items():
            if spec.value_type == "int":
                value = _positive(values, key, allow_zero=spec.allow_zero)
                if spec.minimum is not None and value < spec.minimum:
                    raise InvalidRuntimePolicy(f"{key} 不得小于 {spec.minimum}")
                if spec.maximum is not None and value > spec.maximum:
                    raise InvalidRuntimePolicy(f"{key} 不得大于 {spec.maximum}")
                parsed[key] = value
            elif spec.value_type == "bool":
                _boolean(values, key)
        window = values["market_send_window"].strip()
        try:
            start, finish = window.split("-", 1)
            start_minutes = int(start[:2]) * 60 + int(start[3:])
            finish_minutes = int(finish[:2]) * 60 + int(finish[3:])
            valid_clock = (
                len(start) == 5
                and len(finish) == 5
                and start[2] == finish[2] == ":"
                and 0 <= int(start[:2]) <= 23
                and 0 <= int(finish[:2]) <= 23
                and 0 <= int(start[3:]) <= 59
                and 0 <= int(finish[3:]) <= 59
                and start_minutes < finish_minutes
            )
        except (ValueError, IndexError):
            valid_clock = False
        if not valid_clock:
            raise InvalidRuntimePolicy("market_send_window 必须为递增的 HH:MM-HH:MM")

        parse_private_callback_cidrs(values["callback_allow_cidrs"])

        if values["sensitive_hit_action"] not in {"block", "audit"}:
            raise InvalidRuntimePolicy("sensitive_hit_action 只允许 block/audit")
        try:
            retries = tuple(
                int(item.strip()) for item in values["callback_retry_schedule"].split(",")
            )
        except ValueError as error:
            raise InvalidRuntimePolicy("callback_retry_schedule 必须为五个正整数") from error
        if len(retries) != 5 or any(value < 1 for value in retries):
            raise InvalidRuntimePolicy("callback_retry_schedule 必须为五个正整数")
        if any(value > 86_400 for value in retries) or tuple(sorted(retries)) != retries:
            raise InvalidRuntimePolicy(
                "callback_retry_schedule 必须递增且单次等待不超过 86400 秒"
            )

        vendor_qps = parsed["vendor_qps"]
        reserved_qps = parsed["reserved_realtime_qps"]
        if reserved_qps >= vendor_qps:
            raise InvalidRuntimePolicy("reserved_realtime_qps 必须小于 vendor_qps")

        _validate_mail_list("alert_mail_to", values["alert_mail_to"], allow_empty=True)
        _validate_mail_list(
            "alert_mail_from", values["alert_mail_from"], allow_empty=False
        )

        return cls(
            login_fail_limit=parsed["login_fail_limit"],
            login_lock_minutes=parsed["login_lock_minutes"],
            login_ip_fail_limit=parsed["login_ip_fail_limit"],
            login_ip_ban_minutes=parsed["login_ip_ban_minutes"],
            callback_timeout_seconds=parsed["callback_timeout_seconds"],
            callback_retry_schedule=retries,
            callback_allow_cidrs=values["callback_allow_cidrs"],
            import_max_mb=parsed["import_max_mb"],
            import_max_rows=parsed["import_max_rows"],
            import_expire_hours=parsed["import_expire_hours"],
            test_send_max=parsed["test_send_max"],
            uncertain_alert_hours=parsed["uncertain_alert_hours"],
            vendor_qps=vendor_qps,
            reserved_realtime_qps=reserved_qps,
            balance_alert_threshold=parsed["balance_alert_threshold"],
            unsubscribe_suffix=values["unsubscribe_suffix"],
            unsubscribe_auto_append=_boolean(values, "unsubscribe_auto_append"),
            verify_otp_mask=_boolean(values, "verify_otp_mask"),
            verify_freq_per_minute=parsed["verify_freq_per_minute"],
            verify_freq_per_day=parsed["verify_freq_per_day"],
            market_freq_per_day=parsed["market_freq_per_day"],
            market_send_window=window,
            sensitive_hit_action=values["sensitive_hit_action"],
            approval_threshold=parsed["approval_threshold"],
            market_approval_threshold=parsed["market_approval_threshold"],
            approval_expire_hours=parsed["approval_expire_hours"],
        )


class SqlRuntimePolicyLoader:
    """每次调用读取一份 sys_config 快照；调用方决定请求或任务边界。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def load(self) -> RuntimePolicy:
        engine = database_engine(self.settings.database_url)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(text("SELECT key,value FROM sys_config"))
                values = {str(row["key"]): str(row["value"]) for row in result.mappings()}
        finally:
            await engine.dispose()
        return RuntimePolicy.from_mapping(values)
