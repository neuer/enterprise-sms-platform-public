from __future__ import annotations

import json
from typing import Any

import pytest

from app.core.auth.backends import SessionStateUnavailable
from app.core.auth.security_events import (
    AuthSecurityTransition,
    SqlAuthSecurityEventRepository,
)

TRANSITION_ID = "8a5a77a4-286f-4d81-9a64-5379e30df986"


class FakeConnection:
    def __init__(self, *, failure: BaseException | None = None) -> None:
        self.failure = failure
        self.calls: list[tuple[str, Any]] = []

    async def execute(self, statement: object, params: Any = None) -> None:
        self.calls.append((str(statement), params))
        if self.failure is not None:
            raise self.failure

    def begin_nested(self) -> FakeContext:
        return FakeContext(self)


class FakeContext:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, *_args: object) -> None:
        return None


class FakeEngine:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def begin(self) -> FakeContext:
        return FakeContext(self.connection)

    async def dispose(self) -> None:
        return None


def transition() -> AuthSecurityTransition:
    return AuthSecurityTransition(
        action="auth_account_locked",
        transition_id=TRANSITION_ID,
        provider_code="local",
        result_code="ACCOUNT_LOCKED",
        count=5,
        remaining_ttl_seconds=900,
        ip="10.0.0.8",
    )


@pytest.mark.asyncio
async def test_security_transition_is_idempotent_and_contains_no_login_or_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.core.auth.security_events as security_events

    bound: list[tuple[str, str]] = []

    async def bind(_connection: object, *, actor_name: str, action: str) -> None:
        bound.append((actor_name, action))

    monkeypatch.setattr(security_events, "bind_connection_system_audit", bind)
    connection = FakeConnection()
    repository = SqlAuthSecurityEventRepository()
    repository._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]

    await repository.ensure_transition(transition())

    assert bound == [("auth-system", "auth_account_locked")]
    sql, params = connection.calls[0]
    assert "INSERT INTO audit_log" in sql
    assert "ON CONFLICT" not in sql
    assert params["transition_id"] == TRANSITION_ID
    assert params["ip"] == "10.0.0.8"
    assert json.loads(params["after"]) == {
        "count": 5,
        "provider_code": "local",
        "remaining_ttl_seconds": 900,
        "result_code": "ACCOUNT_LOCKED",
        "transition_id": TRANSITION_ID,
    }
    assert "password" not in repr(connection.calls).casefold()
    assert "token" not in repr(connection.calls).casefold()


@pytest.mark.asyncio
async def test_security_transition_only_swallows_postgres_unique_violation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.core.auth.security_events as security_events

    class UniqueViolation(Exception):
        sqlstate = "23505"

    async def bind(_connection: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(security_events, "bind_connection_system_audit", bind)
    repository = SqlAuthSecurityEventRepository()
    repository._engine = lambda: FakeEngine(  # type: ignore[method-assign]
        FakeConnection(failure=security_events.IntegrityError("sql", {}, UniqueViolation()))
    )

    await repository.ensure_transition(transition())


@pytest.mark.asyncio
async def test_security_transition_database_failure_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.core.auth.security_events as security_events

    async def bind(_connection: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(security_events, "bind_connection_system_audit", bind)
    repository = SqlAuthSecurityEventRepository()
    repository._engine = lambda: FakeEngine(  # type: ignore[method-assign]
        FakeConnection(failure=OSError("database unavailable"))
    )

    with pytest.raises(SessionStateUnavailable):
        await repository.ensure_transition(transition())


def test_security_transition_rejects_non_uuid_object_id() -> None:
    with pytest.raises(ValueError, match="转换无效"):
        AuthSecurityTransition(
            action="auth_ip_banned",
            transition_id="not-a-transition",
            provider_code="ad",
            result_code="RATE_LIMITED",
            count=20,
            remaining_ttl_seconds=900,
            ip="10.0.0.8",
        )
