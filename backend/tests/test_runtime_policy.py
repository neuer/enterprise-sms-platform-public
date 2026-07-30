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
    RuntimePolicy,
    SqlRuntimePolicyLoader,
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
        "anomaly_scan_minutes",
        "usage_projection_reconcile_seconds",
    } == BEAT_STARTUP_ONLY_KEYS
    assert "callback_retry_schedule" not in BEAT_STARTUP_ONLY_KEYS


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
        ("import_max_mb", "11", "import_max_mb"),
        ("import_max_rows", "50001", "import_max_rows"),
        ("callback_timeout_seconds", "11", "callback_timeout_seconds"),
        ("login_lock_minutes", "1441", "login_lock_minutes"),
        ("login_ip_ban_minutes", "1441", "login_ip_ban_minutes"),
        ("test_send_max", "6", "test_send_max"),
    ),
)
def test_runtime_policy_rejects_resource_exhaustion_bounds(
    key: str,
    value: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        RuntimePolicy.from_mapping({key: value})


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


@pytest.mark.asyncio
async def test_task_safe_loader_reuses_process_shared_engine(
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
            self.disposed = False

        def connect(self) -> Context:
            return Context()

        async def dispose(self) -> None:
            self.disposed = True

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
        task_safe=True,
    )

    await loader.load()
    await loader.load()

    assert engine.disposed is True
