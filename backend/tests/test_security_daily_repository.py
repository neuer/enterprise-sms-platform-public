from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from typing import Any, cast
from uuid import uuid4

import pytest

from app.core.auth.accounts import SecurityPrincipal
from app.services.security_daily import (
    DeliveryStatus,
    SecurityDailyConfigurationUpdate,
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

    def scalar_one(self) -> object:
        assert self.scalar is not None
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
async def test_audit_evidence_uses_full_day_and_consistent_self_event_filter() -> None:
    final_event = datetime(2026, 8, 1, 23, 59, 59, 900_000, tzinfo=SHANGHAI)
    repo, connection = repository(
        [
            FakeResult(
                [
                    {
                        "created_at": final_event,
                        "actor": "admin",
                        "source_ip": "198.51.100.7",
                        "action": "config_update",
                    }
                ]
            ),
            FakeResult(scalar=1),
            FakeResult([{"action": "config_update", "n": 1}]),
        ]
    )

    evidence = await repo.audit_evidence(
        datetime(2026, 8, 1, 0, tzinfo=SHANGHAI),
        datetime(2026, 8, 1, 23, 59, 59, tzinfo=SHANGHAI),
    )

    assert evidence is not None
    assert evidence.total == 1
    assert evidence.category_counts == (("系统配置", 1),)
    assert evidence.events[0].time == "2026-08-01 23:59:59"
    expected_end = datetime(2026, 8, 2, 0, tzinfo=SHANGHAI)
    assert all(params["end"] == expected_end for _, params in connection.calls)
    assert all(
        "action NOT LIKE 'security_daily_%'" in sql for sql, _ in connection.calls
    )


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
            FakeResult(
                [
                    {
                        "delivery_status": "pending",
                        "retry_count": 1,
                        "delivery_generation": 1,
                    }
                ]
            ),
            FakeResult(
                [
                    {
                        "request_id": old_request_id,
                        "requested_at": requested_at,
                        "state": "pending",
                        "config_version": 2,
                        "delivery_generation": 1,
                        "recipient_set_digest": "",
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
            FakeResult(
                [
                    {
                        "delivery_status": "pending",
                        "retry_count": 1,
                        "delivery_generation": 1,
                    }
                ]
            ),
            FakeResult(
                [
                    {
                        "request_id": request_id,
                        "requested_at": requested_at,
                        "state": "pending",
                        "config_version": 3,
                        "delivery_generation": 1,
                        "recipient_set_digest": "",
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
async def test_superseded_request_result_cannot_overwrite_current_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
                        "error": "安全日报邮件配置已更新，旧投递请求已失效",
                        "config_version": 2,
                        "delivery_generation": 1,
                    }
                ]
            ),
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

    await repo.apply_control_result(
        SecurityDailyControlResult(
            request_id=request_id,
            report_date=date(2026, 8, 12),
            state="sent",
            completed_at=datetime(2026, 8, 13, 8, 10, tzinfo=SHANGHAI),
        )
    )

    request_update = next(
        params
        for sql, params in connection.calls
        if "UPDATE security_daily_delivery_request" in sql
    )
    assert request_update["state"] == "sent"
    report_update = next(
        (sql, params)
        for sql, params in connection.calls
        if "UPDATE security_daily_report" in sql
    )
    assert "delivery_generation" in report_update[0]
    assert report_update[1]["delivery_generation"] == 1
    audit = next(
        json.loads(params["after"])
        for sql, params in connection.calls
        if "INSERT INTO audit_log" in sql
    )
    assert audit["state"] == "sent"
    assert audit["delivery_generation"] == 1
    assert "re_" not in json.dumps(audit)
    assert "@" not in json.dumps(audit)


@pytest.mark.asyncio
async def test_writer_race_failed_request_still_accepts_mailer_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
                        "error": "独立投递器不可用",
                        "config_version": 3,
                        "delivery_generation": 1,
                    }
                ]
            ),
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

    await repo.apply_control_result(
        SecurityDailyControlResult(
            request_id=request_id,
            report_date=date(2026, 8, 12),
            state="sent",
            completed_at=datetime(2026, 8, 13, 8, 10, tzinfo=SHANGHAI),
        )
    )

    assert any("UPDATE security_daily_report" in sql for sql, _ in connection.calls)
    report_update = next(
        params
        for sql, params in connection.calls
        if "UPDATE security_daily_report" in sql
    )
    assert report_update["delivery_status"] == "sent"


@pytest.mark.asyncio
async def test_update_configuration_audits_publish_phases_without_secrets() -> None:
    repo, connection = repository([FakeResult() for _ in range(20)])

    configuration = await repo.update_configuration(
        SecurityDailyConfigurationUpdate(
            enabled=True,
            recipients=("security-owner@example.com",),
            api_key="re_secret_key",
        ),
        principal=SecurityPrincipal(1, 2, "admin", "security", "admin"),
        ip="127.0.0.1",
    )

    assert configuration.config_version == 1
    audits = [
        json.loads(params["after"])
        for sql, params in connection.calls
        if "INSERT INTO audit_log" in sql
    ]
    assert [item["publish_state"] for item in audits] == ["db_committed", "file_pending"]
    encoded = json.dumps(audits)
    assert "re_secret_key" not in encoded
    assert "security-owner@example.com" not in encoded
    assert all(item["operation_id"] for item in audits)
    assert all("recipient_count" in item for item in audits)


@pytest.mark.asyncio
async def test_mark_file_committed_audit_excludes_key_and_recipients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, connection = repository([FakeResult() for _ in range(8)])

    async def no_audit_bind(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(
        "app.services.security_daily_repository.bind_connection_system_audit",
        no_audit_bind,
    )

    await repo.mark_configuration_publish_state(
        config_version=3,
        publish_state="file_committed",
        operation_id="op-1",
    )

    audits = [
        json.loads(params["after"])
        for sql, params in connection.calls
        if "INSERT INTO audit_log" in sql
    ]
    assert audits == [
        {
            "config_version": 3,
            "publish_state": "file_committed",
            "operation_id": "op-1",
        }
    ]
    encoded = json.dumps(connection.calls)
    assert "re_" not in encoded
    assert "@" not in encoded


@pytest.mark.asyncio
async def test_pending_delivery_requests_include_unknown_and_writer_race() -> None:
    repo, connection = repository([FakeResult([])])

    await repo.pending_delivery_requests()

    sql = connection.calls[0][0]
    assert "state IN ('pending','unknown')" in sql
    assert "独立投递器不可用" in sql
    assert "ORDER BY report_date DESC, requested_at DESC" in sql


@pytest.mark.asyncio
async def test_request_delivery_does_not_supersede_claimed_request() -> None:
    request_id = uuid4()
    requested_at = datetime(2026, 8, 13, 8, tzinfo=SHANGHAI)
    repo, connection = repository(
        [
            FakeResult(),
            FakeResult(scalar="3"),
            FakeResult(
                [
                    {
                        "delivery_status": "pending",
                        "retry_count": 1,
                        "delivery_generation": 1,
                    }
                ]
            ),
            FakeResult(
                [
                    {
                        "request_id": request_id,
                        "requested_at": requested_at,
                        "state": "pending",
                        "config_version": 2,
                        "delivery_generation": 1,
                        "recipient_set_digest": "a" * 64,
                    }
                ]
            ),
        ]
    )

    request = await repo.request_delivery(
        report(), "send", system=True, control_evidence="claimed"
    )

    assert request.request_id == request_id
    assert request.config_version == 2
    assert request.delivery_generation == 1
    assert all("SET state='failed'" not in sql for sql, _ in connection.calls)
    assert all(
        "INSERT INTO security_daily_delivery_request" not in sql
        for sql, _ in connection.calls
    )


@pytest.mark.asyncio
async def test_request_delivery_opens_new_generation_after_sent_config_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_request_id = uuid4()
    requested_at = datetime(2026, 8, 13, 8, tzinfo=SHANGHAI)
    repo, connection = repository(
        [
            FakeResult(),
            FakeResult(scalar="3"),
            FakeResult(
                [
                    {
                        "delivery_status": "sent",
                        "retry_count": 0,
                        "delivery_generation": 1,
                    }
                ]
            ),
            FakeResult(
                [
                    {
                        "request_id": old_request_id,
                        "requested_at": requested_at,
                        "state": "sent",
                        "config_version": 2,
                        "delivery_generation": 1,
                        "recipient_set_digest": "b" * 64,
                    }
                ]
            ),
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

    request = await repo.request_delivery(report(delivery_status="sent"), "send", system=True)

    assert request.request_id != old_request_id
    assert request.delivery_generation == 2
    assert request.config_version == 3
    assert request.idempotent is False
    insert = next(
        params
        for sql, params in connection.calls
        if "INSERT INTO security_daily_delivery_request" in sql
    )
    assert insert["delivery_generation"] == 2
    report_update = next(
        params
        for sql, params in connection.calls
        if "SET delivery_status='pending',retry_count" in sql
    )
    assert report_update["delivery_generation"] == 2
