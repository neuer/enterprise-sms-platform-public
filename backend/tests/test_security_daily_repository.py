from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from typing import Any, cast
from uuid import uuid4

import pytest

from app.services.security_daily import (
    DeliveryStatus,
    SecurityDailyControlResult,
    SecurityDailyReportRecord,
)
from app.services.security_daily_repository import SqlSecurityDailyRepository
from app.settings import Settings

SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")


class FakeMappings:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self.rows)

    def one_or_none(self) -> dict[str, object] | None:
        assert len(self.rows) <= 1
        return self.rows[0] if self.rows else None


class FakeResult:
    def __init__(
        self,
        rows: list[dict[str, object]] | None = None,
        *,
        scalar: object | None = None,
    ) -> None:
        self.rows = rows or []
        self.scalar = scalar

    def mappings(self) -> FakeMappings:
        return FakeMappings(self.rows)

    def scalar_one_or_none(self) -> object | None:
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
        self.disposed = False

    def connect(self) -> FakeContext:
        return FakeContext(self.connection)

    def begin(self) -> FakeContext:
        return FakeContext(self.connection)

    async def dispose(self) -> None:
        self.disposed = True


def report(
    *, delivery_status: DeliveryStatus = "pending", retry_count: int = 1
) -> SecurityDailyReportRecord:
    now = datetime(2026, 8, 13, 8, tzinfo=SHANGHAI)
    return SecurityDailyReportRecord(
        id=10086,
        report_date=date(2026, 8, 12),
        period_start=now - timedelta(days=1),
        period_end=now,
        status="normal",
        generation_source="auto",
        generation_status="ready",
        delivery_status=delivery_status,
        generated_at=now,
        delivered_at=None,
        recipient_count=1,
        retry_count=retry_count,
        last_error=None,
        last_error_at=None,
        updated_at=now,
        payload={"redacted": True},
    )


def repository(
    results: list[FakeResult],
) -> tuple[SqlSecurityDailyRepository, FakeConnection]:
    connection = FakeConnection(results)
    repo = SqlSecurityDailyRepository(settings=cast(Settings, object()))
    repo._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]
    return repo, connection


@pytest.mark.asyncio
async def test_auto_delivery_configuration_uses_only_nonsecret_projection() -> None:
    connection = FakeConnection(
        [
            FakeResult(
                [
                    {"key": "security_daily_enabled", "value": "true"},
                    {"key": "security_daily_resend_configured", "value": "true"},
                    {"key": "security_daily_recipient_count", "value": "1"},
                ]
            )
        ]
    )
    engine = FakeEngine(connection)
    repo = SqlSecurityDailyRepository(settings=cast(Settings, object()))
    repo._engine = lambda: engine  # type: ignore[method-assign]

    configuration = await repo.auto_delivery_configuration()

    assert configuration.enabled
    assert configuration.resend_configured
    assert configuration.recipient_count == 1
    assert len(connection.calls) == 1
    sql = connection.calls[0][0]
    assert "security_daily_resend_configured" in sql
    assert "security_daily_recipient_count" in sql
    assert "security_daily_resend_api_key" not in sql
    assert engine.disposed


@pytest.mark.asyncio
async def test_request_delivery_supersedes_pending_request_from_old_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_request_id = uuid4()
    requested_at = datetime(2026, 8, 13, 8, tzinfo=SHANGHAI)
    repo, connection = repository(
        [
            FakeResult(),
            FakeResult(scalar="3"),
            FakeResult([{"delivery_status": "pending", "retry_count": 1}]),
            FakeResult(
                [
                    {
                        "request_id": old_request_id,
                        "requested_at": requested_at,
                        "state": "pending",
                        "config_version": 2,
                    }
                ]
            ),
            FakeResult(),
            FakeResult(),
            FakeResult(),
            FakeResult(),
        ]
    )

    async def no_audit_bind(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(
        "app.services.security_daily_repository.bind_connection_system_audit",
        no_audit_bind,
    )

    request = await repo.request_delivery(report(), "send", system=True)

    assert request.request_id != old_request_id
    assert request.config_version == 3
    assert request.idempotent is False
    supersede_sql, supersede_params = connection.calls[4]
    assert "SET state='failed'" in supersede_sql
    assert supersede_params["request_id"] == old_request_id
    assert "配置已更新" in supersede_params["error"]
    report_update = next(
        params
        for sql, params in connection.calls
        if "SET delivery_status='pending',retry_count" in sql
    )
    assert report_update["retry_count"] == 2


@pytest.mark.asyncio
async def test_request_delivery_reuses_same_version_pending_request() -> None:
    request_id = uuid4()
    requested_at = datetime(2026, 8, 13, 8, tzinfo=SHANGHAI)
    repo, connection = repository(
        [
            FakeResult(),
            FakeResult(scalar="3"),
            FakeResult([{"delivery_status": "pending", "retry_count": 1}]),
            FakeResult(
                [
                    {
                        "request_id": request_id,
                        "requested_at": requested_at,
                        "state": "pending",
                        "config_version": 3,
                    }
                ]
            ),
            FakeResult(),
        ]
    )

    request = await repo.request_delivery(report(), "send", system=True)

    assert request.request_id == request_id
    assert request.config_version == 3
    assert request.idempotent is True
    assert any("SET updated_at=now()" in sql for sql, _ in connection.calls)
    assert all("SET state='failed'" not in sql for sql, _ in connection.calls)


@pytest.mark.asyncio
async def test_superseded_request_result_cannot_overwrite_current_report() -> None:
    request_id = uuid4()
    repo, connection = repository(
        [
            FakeResult(),
            FakeResult(
                [
                    {
                        "report_id": 10086,
                        "report_date": date(2026, 8, 12),
                        "state": "failed",
                    }
                ]
            ),
        ]
    )

    await repo.apply_control_result(
        SecurityDailyControlResult(
            request_id=request_id,
            report_date=date(2026, 8, 12),
            state="sent",
            completed_at=datetime(2026, 8, 13, 8, 10, tzinfo=SHANGHAI),
        )
    )

    assert len(connection.calls) == 2
    assert "pg_advisory_xact_lock" in connection.calls[0][0]
    assert all("UPDATE security_daily_report" not in sql for sql, _ in connection.calls)
