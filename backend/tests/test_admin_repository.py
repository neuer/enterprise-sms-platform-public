from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from app.core.auth.accounts import SecurityPrincipal
from app.services.admin import AuditQuery, ConfigUpdate, InvalidAdminQuery
from app.services.admin_repository import SqlAdminRepository

NOW = datetime(2026, 7, 12, 8, tzinfo=UTC)
CORRELATION_ID = UUID("30000000-0000-4000-8000-000000000009")
ADMIN = SecurityPrincipal(1, 10, "admin01", "平台部", "admin")


class FakeResult:
    def __init__(self, rows: list[dict[str, object]] | None = None, scalar: object = None) -> None:
        self.rows = rows or []
        self.scalar = scalar

    def mappings(self) -> FakeResult:
        return self

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self.rows)

    def scalar_one(self) -> object:
        return self.scalar


class FakeConnection:
    def __init__(self, results: list[FakeResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, Any]] = []

    async def execute(self, statement: object, params: Any = None) -> FakeResult:
        self.calls.append((str(statement), params))
        return self.results.pop(0)


class FakeContext:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, *_: object) -> None:
        return None


class FakeEngine:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def connect(self) -> FakeContext:
        return FakeContext(self.connection)

    def begin(self) -> FakeContext:
        return FakeContext(self.connection)

    async def dispose(self) -> None:
        return None


def repository(results: list[FakeResult]) -> tuple[SqlAdminRepository, FakeConnection]:
    connection = FakeConnection(results)
    repo = SqlAdminRepository()
    repo._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]
    return repo, connection


def config_row(key: str, value: str, value_type: str = "str") -> dict[str, object]:
    return {
        "key": key,
        "value": value,
        "value_type": value_type,
        "description": "说明",
        "updated_by": None,
        "updated_at": NOW,
    }


@pytest.mark.asyncio
async def test_audit_list_uses_parameterized_filters_and_stable_pagination() -> None:
    repo, connection = repository(
        [
            FakeResult(scalar=1),
            FakeResult(
                [
                    {
                        "id": 8,
                        "correlation_id": CORRELATION_ID,
                        "actor": "admin01",
                        "actor_subject_kind": "human",
                        "actor_account_id": 1,
                        "actor_identity_id": 10,
                        "actor_app_id": None,
                        "role": "admin",
                        "ip": "10.0.0.8",
                        "action": "config_update",
                        "object_type": "sys_config",
                        "object_id": "vendor_qps",
                        "before_val": {"value": "5"},
                        "after_val": {"value": "8"},
                        "created_at": NOW,
                    }
                ]
            ),
        ]
    )
    query = AuditQuery("admin01", "config_update", "sys_config", NOW, NOW, 2, 20)

    items, total = await repo.list_audits(query)

    assert total == 1 and items[0].object_id == "vendor_qps"
    assert items[0].correlation_id == CORRELATION_ID
    sql, params = connection.calls[1]
    assert "ORDER BY created_at DESC,id DESC" in sql
    assert "before_val" in sql and "after_val" in sql
    assert params["offset"] == 20 and params["actor"] == "admin01"


@pytest.mark.asyncio
async def test_config_update_locks_rows_and_audits_safe_before_after() -> None:
    before = [
        config_row("vendor_qps", "5", "int"),
        config_row("reserved_realtime_qps", "2", "int"),
        config_row(
            "alert_wecom_webhook",
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=old",
        ),
    ]
    after = [
        config_row("vendor_qps", "8", "int"),
        config_row(
            "alert_wecom_webhook",
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=new",
        ),
    ]
    repo, connection = repository(
        [
            FakeResult(),
            FakeResult(before),
            FakeResult(),
            FakeResult(),
            FakeResult(),
            FakeResult(after),
        ]
    )

    result = await repo.update_configs(
        (
            ConfigUpdate("vendor_qps", "8"),
            ConfigUpdate(
                "alert_wecom_webhook",
                "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=new",
            ),
        ),
        principal=ADMIN,
        ip="10.0.0.8",
    )

    assert result[0].value == "8"
    assert "pg_advisory_xact_lock" in connection.calls[0][0]
    assert "FROM sys_config" in connection.calls[1][0]
    assert "WHERE key" not in connection.calls[1][0]
    assert "FOR UPDATE" in connection.calls[1][0]
    audit_params = [params for sql, params in connection.calls if "INSERT INTO audit_log" in sql]
    assert audit_params[0]["before"] == '{"value": "5"}'
    assert audit_params[0]["after"] == '{"value": "8"}'
    assert audit_params[1]["before"] == '{"configured": true}'
    assert audit_params[1]["after"] == '{"configured": true}'
    assert "old" not in str(audit_params) and "new" not in str(audit_params)


@pytest.mark.asyncio
async def test_config_update_rejects_internal_revision_keys() -> None:
    repo, connection = repository([])

    with pytest.raises(InvalidAdminQuery, match="不存在"):
        await repo.update_configs(
            (ConfigUpdate("__sensitive_word_revision", "0"),),
            principal=ADMIN,
            ip="10.0.0.8",
        )

    assert connection.calls == []


@pytest.mark.asyncio
async def test_config_listing_hides_only_literal_internal_prefix() -> None:
    repo, connection = repository([FakeResult()])

    await repo.list_configs()

    assert "LEFT(key,2) <> '__'" in connection.calls[0][0]
    assert "NOT LIKE '__%'" not in connection.calls[0][0]


@pytest.mark.asyncio
async def test_transaction_final_gate_rejects_public_callback_allowlist() -> None:
    before = [
        config_row("vendor_qps", "5", "int"),
        config_row("reserved_realtime_qps", "2", "int"),
        config_row("callback_allow_cidrs", "10.0.0.0/8"),
    ]
    repo, connection = repository([FakeResult(), FakeResult(before)])

    with pytest.raises(ValueError, match="私网"):
        await repo.update_configs(
            (ConfigUpdate("callback_allow_cidrs", "0.0.0.0/0"),),
            principal=ADMIN,
            ip="10.0.0.8",
        )

    assert len(connection.calls) == 2
