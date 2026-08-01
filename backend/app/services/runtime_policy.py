"""sys_config 的类型化运行策略与单点解析。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import IPv4Network, IPv6Network, ip_network
from typing import Any

from sqlalchemy import text

from app.core.runtime_resources import database_engine
from app.settings import Settings, get_settings

DEFAULTS: dict[str, str] = {
    "approval_threshold": "100",
    "market_approval_threshold": "50",
    "approval_expire_hours": "24",
    "market_send_window": "08:00-21:00",
    "vendor_batch_size": "500",
    "vendor_qps": "5",
    "reserved_realtime_qps": "2",
    "report_poll_seconds": "60",
    "reply_poll_seconds": "300",
    "balance_poll_seconds": "600",
    "balance_alert_threshold": "10000",
    "fail_rate_threshold": "20",
    "fail_rate_min_total": "50",
    "report_timeout_hours": "48",
    "uncertain_alert_hours": "24",
    "reconcile_interval_min": "5",
    "msg_retention_months": "12",
    "audit_retention_months": "36",
    "raw_log_retention_days": "90",
    "import_expire_hours": "24",
    "export_retention_days": "7",
    "sensitive_hit_action": "block",
    "key_grace_hours": "72",
    "login_fail_limit": "5",
    "login_lock_minutes": "15",
    "login_ip_fail_limit": "20",
    "login_ip_ban_minutes": "15",
    "callback_timeout_seconds": "5",
    "callback_retry_schedule": "60,300,900,3600,3600",
    "callback_allow_cidrs": "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16",
    "alert_wecom_webhook": "",
    "alert_mail_to": "",
    "alert_smtp_host": "smtp",
    "alert_smtp_port": "25",
    "alert_mail_from": "sms-platform@localhost",
    "unsubscribe_suffix": "回T退订",
    "unsubscribe_auto_append": "true",
    "verify_freq_per_minute": "1",
    "verify_freq_per_day": "10",
    "market_freq_per_day": "1",
    "import_max_mb": "10",
    "import_max_rows": "50000",
    "test_send_max": "5",
    "verify_otp_mask": "true",
    "unmatched_retention_days": "90",
    "anomaly_enabled": "true",
    "anomaly_multiplier": "3",
    "anomaly_min_total": "500",
    "anomaly_scan_minutes": "60",
    "job_history_days": "30",
    "usage_projection_reconcile_seconds": "300",
    "usage_ledger_retention_days": "90",
    "security_daily_enabled": "false",
    "security_daily_recipient_count": "0",
    "security_daily_resend_configured": "false",
}

BEAT_STARTUP_ONLY_KEYS = frozenset(
    {
        "report_poll_seconds",
        "reply_poll_seconds",
        "reconcile_interval_min",
        "balance_poll_seconds",
        "anomaly_scan_minutes",
        "usage_projection_reconcile_seconds",
    }
)

INT_CONFIG_KEYS = frozenset(
    {
        "approval_threshold",
        "market_approval_threshold",
        "approval_expire_hours",
        "vendor_batch_size",
        "vendor_qps",
        "reserved_realtime_qps",
        "report_poll_seconds",
        "reply_poll_seconds",
        "balance_poll_seconds",
        "balance_alert_threshold",
        "fail_rate_threshold",
        "fail_rate_min_total",
        "report_timeout_hours",
        "uncertain_alert_hours",
        "reconcile_interval_min",
        "msg_retention_months",
        "audit_retention_months",
        "raw_log_retention_days",
        "import_expire_hours",
        "export_retention_days",
        "key_grace_hours",
        "login_fail_limit",
        "login_lock_minutes",
        "login_ip_fail_limit",
        "login_ip_ban_minutes",
        "callback_timeout_seconds",
        "alert_smtp_port",
        "verify_freq_per_minute",
        "verify_freq_per_day",
        "market_freq_per_day",
        "import_max_mb",
        "import_max_rows",
        "test_send_max",
        "unmatched_retention_days",
        "anomaly_multiplier",
        "anomaly_min_total",
        "anomaly_scan_minutes",
        "job_history_days",
        "usage_projection_reconcile_seconds",
        "usage_ledger_retention_days",
        "security_daily_recipient_count",
    }
)
BOOL_CONFIG_KEYS = frozenset(
    {
        "unsubscribe_auto_append",
        "verify_otp_mask",
        "anomaly_enabled",
        "security_daily_enabled",
        "security_daily_resend_configured",
    }
)
APPROVED_IPV4_CALLBACK_NETWORKS = (
    IPv4Network("10.0.0.0/8"),
    IPv4Network("172.16.0.0/12"),
    IPv4Network("192.168.0.0/16"),
)
APPROVED_IPV6_CALLBACK_NETWORKS = (IPv6Network("fc00::/7"),)


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


def _bounded(
    values: Mapping[str, str],
    key: str,
    *,
    maximum: int,
    allow_zero: bool = False,
) -> int:
    value = _positive(values, key, allow_zero=allow_zero)
    if value > maximum:
        raise InvalidRuntimePolicy(f"{key} 不得大于 {maximum}")
    return value


def _boolean(values: Mapping[str, str], key: str) -> bool:
    raw = values[key].strip().casefold()
    if raw not in {"true", "false"}:
        raise InvalidRuntimePolicy(f"{key} 必须为布尔值 true/false")
    return raw == "true"


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
        for key in INT_CONFIG_KEYS:
            _positive(
                values,
                key,
                allow_zero=key in {"reserved_realtime_qps", "security_daily_recipient_count"},
            )
        for key in BOOL_CONFIG_KEYS:
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

        smtp_port = _positive(values, "alert_smtp_port")
        if smtp_port > 65535:
            raise InvalidRuntimePolicy("alert_smtp_port 必须在 1-65535 范围")
        fail_rate = _positive(values, "fail_rate_threshold")
        if fail_rate > 100:
            raise InvalidRuntimePolicy("fail_rate_threshold 必须在 1-100 范围")
        _bounded(
            values,
            "security_daily_recipient_count",
            maximum=3,
            allow_zero=True,
        )
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

        vendor_qps = _bounded(values, "vendor_qps", maximum=1_000)
        reserved_qps = _bounded(
            values,
            "reserved_realtime_qps",
            maximum=999,
            allow_zero=True,
        )
        if reserved_qps >= vendor_qps:
            raise InvalidRuntimePolicy("reserved_realtime_qps 必须小于 vendor_qps")

        return cls(
            login_fail_limit=_bounded(values, "login_fail_limit", maximum=20),
            login_lock_minutes=_bounded(values, "login_lock_minutes", maximum=1_440),
            login_ip_fail_limit=_bounded(values, "login_ip_fail_limit", maximum=1_000),
            login_ip_ban_minutes=_bounded(
                values,
                "login_ip_ban_minutes",
                maximum=1_440,
            ),
            callback_timeout_seconds=_bounded(
                values,
                "callback_timeout_seconds",
                maximum=10,
            ),
            callback_retry_schedule=retries,
            callback_allow_cidrs=values["callback_allow_cidrs"],
            import_max_mb=_bounded(values, "import_max_mb", maximum=10),
            import_max_rows=_bounded(values, "import_max_rows", maximum=50_000),
            import_expire_hours=_bounded(
                values,
                "import_expire_hours",
                maximum=168,
            ),
            test_send_max=_bounded(values, "test_send_max", maximum=5),
            uncertain_alert_hours=_bounded(
                values,
                "uncertain_alert_hours",
                maximum=720,
            ),
            vendor_qps=vendor_qps,
            reserved_realtime_qps=reserved_qps,
            balance_alert_threshold=_positive(values, "balance_alert_threshold"),
            unsubscribe_suffix=values["unsubscribe_suffix"],
            unsubscribe_auto_append=_boolean(values, "unsubscribe_auto_append"),
            verify_otp_mask=_boolean(values, "verify_otp_mask"),
            verify_freq_per_minute=_bounded(
                values,
                "verify_freq_per_minute",
                maximum=100,
            ),
            verify_freq_per_day=_bounded(
                values,
                "verify_freq_per_day",
                maximum=10_000,
            ),
            market_freq_per_day=_bounded(
                values,
                "market_freq_per_day",
                maximum=1_000,
            ),
            market_send_window=window,
            sensitive_hit_action=values["sensitive_hit_action"],
            approval_threshold=_bounded(
                values,
                "approval_threshold",
                maximum=1_000_000,
            ),
            market_approval_threshold=_bounded(
                values,
                "market_approval_threshold",
                maximum=1_000_000,
            ),
            approval_expire_hours=_bounded(
                values,
                "approval_expire_hours",
                maximum=168,
            ),
        )


class SqlRuntimePolicyLoader:
    """每次调用读取一份 sys_config 快照；调用方决定请求或任务边界。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        task_safe: bool = False,
    ) -> None:
        self.settings = settings or get_settings()
        self.task_safe = task_safe

    async def load(self) -> RuntimePolicy:
        engine = database_engine(self.settings.database_url)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(text("SELECT key,value FROM sys_config"))
                values = {str(row["key"]): str(row["value"]) for row in result.mappings()}
        finally:
            await engine.dispose()
        return RuntimePolicy.from_mapping(values)
