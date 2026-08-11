from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.core.auth.accounts import ApplicationPrincipal, SecurityPrincipal
from app.core.auth.principal_context import audit_principal_scope
from app.core.correlation import correlation_scope
from app.services import sensitive_read_audit as module
from app.services.sensitive_read_audit import SensitiveReadAuditor


class FakeBegin:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_: object) -> None:
        return None


class FakeEngine:
    def __init__(self) -> None:
        self.disposed = False

    def begin(self) -> FakeBegin:
        return FakeBegin()

    async def dispose(self) -> None:
        self.disposed = True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "principal,expected_role",
    (
        (SecurityPrincipal(11, 101, "operator01", "平台部", "operator"), "operator"),
        (ApplicationPrincipal(7, "app-iam", "平台部"), None),
    ),
)
async def test_sensitive_read_audit_uses_stable_principal_and_value_free_payload(
    monkeypatch: pytest.MonkeyPatch,
    principal: SecurityPrincipal | ApplicationPrincipal,
    expected_role: str | None,
) -> None:
    engine = FakeEngine()
    events: list[Any] = []

    async def capture(_connection: object, event: object) -> None:
        events.append(event)

    monkeypatch.setattr(module, "database_engine", lambda _url: engine)
    monkeypatch.setattr(module, "insert_audit", capture)
    auditor = SensitiveReadAuditor(SimpleNamespace(database_url="postgresql://test"))  # type: ignore[arg-type]
    with audit_principal_scope(principal), correlation_scope():
        await auditor.record(
            action="message_content_read",
            object_type="message_page",
            object_id="search",
            ip="192.0.2.10",
            count=2,
        )

    event = events[0]
    assert event.principal == principal
    assert event.role == expected_role
    assert event.after == {"result_count": 2}
    assert "content" not in event.after
    assert event.ip == "192.0.2.10"
    assert engine.disposed is True


@pytest.mark.asyncio
async def test_sensitive_read_audit_fails_closed_without_authenticated_principal() -> None:
    auditor = SensitiveReadAuditor(SimpleNamespace(database_url="postgresql://test"))  # type: ignore[arg-type]
    with audit_principal_scope(), pytest.raises(RuntimeError, match="principal unavailable"):
        await auditor.record(
            action="reply_content_read",
            object_type="reply_page",
            object_id="list",
            ip="192.0.2.10",
            count=1,
        )
