from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import app.services.runtime_policy as runtime_policy_module
from app.services.runtime_policy import (
    BEAT_STARTUP_ONLY_KEYS,
    BOOL_CONFIG_KEYS,
    DEFAULTS,
    INT_CONFIG_KEYS,
    InvalidRuntimePolicy,
    RuntimePolicy,
    SqlRuntimePolicyLoader,
    ensure_callback_cidrs_within_deployment,
)


def test_runtime_policy_inventory_covers_every_schema_config_key() -> None:
    schema = (Path(__file__).parents[2] / "schema.sql").read_text(encoding="utf-8")
    block = schema.split("INSERT INTO sys_config", 1)[1].split(";", 1)[0]
    schema_keys = set(re.findall(r"\('([^']+)'\s*,", block))

    assert set(DEFAULTS) == schema_keys
    assert not (INT_CONFIG_KEYS & BOOL_CONFIG_KEYS)
    assert schema_keys >= INT_CONFIG_KEYS | BOOL_CONFIG_KEYS


def test_only_beat_schedule_keys_are_startup_only() -> None:
    assert {
        "report_poll_seconds",
        "reply_poll_seconds",
        "reconcile_interval_min",
        "balance_poll_seconds",
        "approval_scan_seconds",
        "scheduled_scan_seconds",
        "anomaly_scan_minutes",
        "usage_projection_reconcile_seconds",
    } == BEAT_STARTUP_ONLY_KEYS
    assert "callback_retry_schedule" not in BEAT_STARTUP_ONLY_KEYS


def test_config_specs_groups_and_types_are_consistent() -> None:
    from app.services.runtime_policy import CONFIG_GROUP_ORDER, CONFIG_SPECS

    assert set(CONFIG_SPECS) == set(DEFAULTS)
    for key, spec in CONFIG_SPECS.items():
        assert spec.group in CONFIG_GROUP_ORDER, key
        assert spec.value_type in {"str", "int", "bool"}, key
        if spec.value_type != "int":
            assert spec.minimum is None and spec.maximum is None, key
        if spec.minimum is not None and spec.maximum is not None:
            assert spec.minimum <= spec.maximum, key
        # 注册默认值必须自身可过校验，避免升级后 fail closed
    assert RuntimePolicy.from_mapping({})


def test_runtime_policy_returns_typed_snapshot() -> None:
    policy = RuntimePolicy.from_mapping(
        {
            "callback_timeout_seconds": "9",
            "test_send_max": "3",
            "balance_alert_threshold": "8800",
            "unsubscribe_auto_append": "false",
        }
    )

    assert policy.callback_timeout_seconds == 9
    assert policy.test_send_max == 3
    assert policy.balance_alert_threshold == 8800
    assert policy.unsubscribe_auto_append is False


@pytest.mark.parametrize(
    ("key", "value", "message"),
    (
        ("vendor_qps", "1001", "vendor_qps"),
        ("vendor_batch_size", "1001", "vendor_batch_size"),
        ("import_max_mb", "11", "import_max_mb"),
        ("import_max_rows", "50001", "import_max_rows"),
        ("callback_timeout_seconds", "11", "callback_timeout_seconds"),
        ("login_lock_minutes", "1441", "login_lock_minutes"),
        ("login_ip_ban_minutes", "1441", "login_ip_ban_minutes"),
        ("test_send_max", "6", "test_send_max"),
        ("report_poll_seconds", "9", "report_poll_seconds"),
        ("report_poll_seconds", "3601", "report_poll_seconds"),
        ("reply_poll_seconds", "29", "reply_poll_seconds"),
        ("balance_poll_seconds", "59", "balance_poll_seconds"),
        ("reconcile_interval_min", "61", "reconcile_interval_min"),
        ("approval_scan_seconds", "29", "approval_scan_seconds"),
        ("approval_scan_seconds", "3601", "approval_scan_seconds"),
        ("scheduled_scan_seconds", "9", "scheduled_scan_seconds"),
        ("scheduled_scan_seconds", "3601", "scheduled_scan_seconds"),
        ("anomaly_scan_minutes", "4", "anomaly_scan_minutes"),
        ("anomaly_scan_minutes", "1441", "anomaly_scan_minutes"),
        ("anomaly_multiplier", "101", "anomaly_multiplier"),
        ("anomaly_min_total", "1000001", "anomaly_min_total"),
        ("usage_projection_reconcile_seconds", "59", "usage_projection_reconcile_seconds"),
        ("balance_alert_threshold", "100000001", "balance_alert_threshold"),
        ("fail_rate_min_total", "1000001", "fail_rate_min_total"),
        ("report_timeout_hours", "721", "report_timeout_hours"),
        ("key_grace_hours", "721", "key_grace_hours"),
        ("msg_retention_months", "121", "msg_retention_months"),
        ("audit_retention_months", "121", "audit_retention_months"),
        ("raw_log_retention_days", "3651", "raw_log_retention_days"),
        ("unmatched_retention_days", "3651", "unmatched_retention_days"),
        ("usage_ledger_retention_days", "3651", "usage_ledger_retention_days"),
        ("export_retention_days", "91", "export_retention_days"),
        ("job_history_days", "366", "job_history_days"),
    ),
)
def test_runtime_policy_rejects_resource_exhaustion_bounds(
    key: str,
    value: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        RuntimePolicy.from_mapping({key: value})


def test_beat_scan_intervals_are_registered_config_keys() -> None:
    from app.services.runtime_policy import CONFIG_SPECS

    for key in ("approval_scan_seconds", "scheduled_scan_seconds"):
        spec = CONFIG_SPECS[key]
        assert spec.value_type == "int" and spec.beat_restart is True
        assert spec.group == "运行调度"
    assert {"approval_scan_seconds", "scheduled_scan_seconds"} <= INT_CONFIG_KEYS


@pytest.mark.parametrize(
    ("key", "value"),
    (
        ("alert_mail_to", "not-an-email"),
        ("alert_mail_to", "ops@example.com, bad-address"),
        ("alert_mail_to", "ops@example.com,"),
        ("alert_mail_from", ""),
        ("alert_mail_from", "no-at-sign"),
    ),
)
def test_alert_mail_config_requires_valid_mailboxes(key: str, value: str) -> None:
    with pytest.raises(ValueError, match="邮箱"):
        RuntimePolicy.from_mapping({key: value})


@pytest.mark.parametrize(
    ("key", "value"),
    (
        ("alert_mail_to", ""),
        ("alert_mail_to", "ops@example.com"),
        ("alert_mail_to", "ops@example.com, sec@example.internal"),
        ("alert_mail_from", "sms-platform@localhost"),
    ),
)
def test_alert_mail_config_accepts_valid_mailboxes(key: str, value: str) -> None:
    assert RuntimePolicy.from_mapping({key: value})


@pytest.mark.parametrize(
    "schedule",
    (
        "60,30,900,3600,3600",
        "60,300,900,3600,86401",
    ),
)
def test_callback_retry_schedule_is_bounded_and_monotonic(schedule: str) -> None:
    with pytest.raises(ValueError, match="递增"):
        RuntimePolicy.from_mapping({"callback_retry_schedule": schedule})


@pytest.mark.parametrize(
    "cidrs",
    (
        "0.0.0.0/0",
        "::/0",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "224.0." "0.0/4",
        "::1/128",
        "fe80::/10",
    ),
)
def test_callback_allowlist_rejects_non_approved_private_cidrs(cidrs: str) -> None:
    with pytest.raises(ValueError, match="私网"):
        RuntimePolicy.from_mapping({"callback_allow_cidrs": cidrs})


@pytest.mark.parametrize(
    "cidrs",
    (
        "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16",
        "10.23.0.0/16,192.168.8.0/24",
        "fd12:3456::/48",
    ),
)
def test_callback_allowlist_accepts_approved_private_subnets(cidrs: str) -> None:
    assert RuntimePolicy.from_mapping({"callback_allow_cidrs": cidrs}).callback_allow_cidrs


def test_empty_callback_deployment_boundary_disables_all_egress() -> None:
    assert (
        ensure_callback_cidrs_within_deployment(
            "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16",
            (),
        )
        == ()
    )


def test_empty_callback_deployment_boundary_still_rejects_unsafe_cidrs() -> None:
    with pytest.raises(InvalidRuntimePolicy, match="私网"):
        ensure_callback_cidrs_within_deployment("0.0.0.0/0", ())


@pytest.mark.asyncio
async def test_loader_disposes_engine_handle_after_each_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Result:
        def mappings(self) -> list[dict[str, object]]:
            return []

    class Connection:
        async def execute(self, statement: object) -> Result:
            return Result()

    class Context:
        async def __aenter__(self) -> Connection:
            return Connection()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Engine:
        def __init__(self) -> None:
            self.dispose_calls = 0

        def connect(self) -> Context:
            return Context()

        async def dispose(self) -> None:
            self.dispose_calls += 1

    engine = Engine()
    monkeypatch.setattr(
        runtime_policy_module,
        "database_engine",
        lambda _database_url: engine,
    )
    loader = SqlRuntimePolicyLoader(
        cast(
            Any,
            SimpleNamespace(
                database_url="postgresql+asyncpg://unused",
                database_url_for=lambda _role: "postgresql+asyncpg://unused",
            ),
        ),
    )

    await loader.load()
    await loader.load()

    # dispose 在进程共享引擎上是 no-op，但每次 load 都必须归还句柄
    assert engine.dispose_calls == 2
