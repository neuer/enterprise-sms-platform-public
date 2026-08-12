from __future__ import annotations

from collections.abc import Iterator
from typing import cast

import pytest

from app.services.security_daily_repository import SqlSecurityDailyRepository
from app.settings import Settings


class FakeResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def mappings(self) -> FakeResult:
        return self

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self.rows)


class FakeConnection:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.calls: list[str] = []

    async def execute(self, statement: object) -> FakeResult:
        self.calls.append(str(statement))
        return FakeResult(self.rows)


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
        self.disposed = False

    def connect(self) -> FakeContext:
        return FakeContext(self.connection)

    async def dispose(self) -> None:
        self.disposed = True


@pytest.mark.asyncio
async def test_auto_delivery_configuration_uses_only_nonsecret_projection() -> None:
    connection = FakeConnection(
        [
            {"key": "security_daily_enabled", "value": "true"},
            {"key": "security_daily_resend_configured", "value": "true"},
            {"key": "security_daily_recipient_count", "value": "1"},
        ]
    )
    engine = FakeEngine(connection)
    repository = SqlSecurityDailyRepository(settings=cast(Settings, object()))
    repository._engine = lambda: engine  # type: ignore[method-assign]

    configuration = await repository.auto_delivery_configuration()

    assert configuration.enabled
    assert configuration.resend_configured
    assert configuration.recipient_count == 1
    assert len(connection.calls) == 1
    assert "security_daily_resend_configured" in connection.calls[0]
    assert "security_daily_recipient_count" in connection.calls[0]
    assert "security_daily_resend_api_key" not in connection.calls[0]
    assert engine.disposed
