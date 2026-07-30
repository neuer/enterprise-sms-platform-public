from __future__ import annotations

import json
from typing import Any

import pytest

from app.core.auth.accounts import SecurityPrincipal

CORRELATION_ID = "c0a80101-0000-4000-8000-000000000131"
SENTINEL = "formal-key-private-13800138000"
ADMIN = SecurityPrincipal(1, 10, "admin", "平台部", "admin")


class FakeConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def execute(self, statement: object, params: Any = None) -> None:
        self.calls.append((str(statement), params))


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

    def begin(self) -> FakeContext:
        return FakeContext(self.connection)

    async def dispose(self) -> None:
        return None


def repository():
    from app.services.vendor_test_security_audit import (
        SqlVendorTestSecurityAuditRepository,
    )

    connection = FakeConnection()
    repo = SqlVendorTestSecurityAuditRepository()
    repo._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]
    return repo, connection


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "outcome", "safe_code"),
    (
        ("vendor_test_step_up", "succeeded", None),
        ("vendor_test_seal_session", "failed", "CONTROL_AGENT_UNAVAILABLE"),
    ),
)
async def test_security_event_inserts_real_safe_audit_row(
    action: str,
    outcome: str,
    safe_code: str | None,
) -> None:
    repo, connection = repository()

    await repo.record(
        correlation_id=CORRELATION_ID,
        principal=ADMIN,
        action=action,
        outcome=outcome,
        safe_code=safe_code,
    )

    assert len(connection.calls) == 1
    sql, params = connection.calls[0]
    assert "INSERT INTO audit_log" in sql
    assert params["actor"] == "admin"
    assert params["role"] == "admin"
    assert params["action"] == action
    assert params["object_id"] == CORRELATION_ID
    payload = json.loads(params["after"])
    assert payload == {
        "correlation_id": CORRELATION_ID,
        "count": 1,
        "outcome": outcome,
        **({"safe_code": safe_code} if safe_code is not None else {}),
    }
    assert SENTINEL not in repr(connection.calls)
    assert "password" not in repr(connection.calls).casefold()
    assert "token" not in repr(connection.calls).casefold()


@pytest.mark.asyncio
async def test_security_event_rejects_unapproved_metadata_before_database_access() -> None:
    repo, connection = repository()

    with pytest.raises(ValueError, match="审计事件无效"):
        await repo.record(
            correlation_id=CORRELATION_ID,
            principal=ADMIN,
            action="arbitrary_action",
            outcome="failed",
            safe_code="unsafe detail",
        )

    assert connection.calls == []
