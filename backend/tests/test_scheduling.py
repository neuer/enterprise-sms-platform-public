from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.services import scheduling_repository as scheduling_repository_module
from app.services.batch_query import BatchAccessScope
from app.services.scheduling import ScheduledBatch, SchedulingService
from app.services.scheduling_repository import SqlSchedulingRepository


def test_scheduling_repository_always_uses_process_shared_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared_engine = object()
    settings = type("SettingsStub", (), {"database_url": "postgresql+asyncpg://test"})()
    monkeypatch.setattr(
        scheduling_repository_module,
        "database_engine",
        lambda _database_url: shared_engine,
    )

    assert SqlSchedulingRepository(settings)._engine() is shared_engine
    assert SqlSchedulingRepository(settings, pooled=True)._engine() is shared_engine


class FakeRepository:
    def __init__(self) -> None:
        self.due = [ScheduledBatch("market-1", "market")]
        self.cancelled = ScheduledBatch(
            "market-1", "market", app_id=7, dept="平台部", quota_date="20260711", quota_cost=9
        )
        self.rescheduled: tuple[str, datetime] | None = None
        self.approval_expire_hours = 0

    async def claim_due(self) -> list[ScheduledBatch]:
        return self.due

    async def cancel(self, batch_no: str, scope: BatchAccessScope) -> ScheduledBatch | None:
        return self.cancelled

    async def reschedule(
        self,
        batch_no: str,
        scope: BatchAccessScope,
        scheduled_at: datetime,
        *,
        approval_expire_hours: int,
        max_schedule_ahead_days: int = 90,
    ) -> bool:
        self.rescheduled = (batch_no, scheduled_at)
        self.approval_expire_hours = approval_expire_hours
        return True


class FakePublisher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def enqueue(self, batch_no: str, queue: str) -> None:
        self.calls.append((batch_no, queue))


class FakeQuota:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def refund_once(self, **values: object) -> object:
        self.calls.append(values)
        return object()


@pytest.mark.asyncio
async def test_due_batches_are_claimed_once_and_enqueued_by_category() -> None:
    publisher = FakePublisher()
    assert await SchedulingService(FakeRepository(), FakeQuota(), publisher).dispatch_due() == 1
    assert publisher.calls == [("market-1", "bulk")]


@pytest.mark.asyncio
async def test_cancel_refunds_once_and_reschedule_requires_future_time() -> None:
    repository = FakeRepository()
    quota = FakeQuota()
    service = SchedulingService(
        repository,
        quota,
        FakePublisher(),
        clock=lambda: datetime(2026, 7, 11, 8, 0, tzinfo=UTC),
        approval_expire_hours=7,
    )
    scope = BatchAccessScope(app_id=7)
    await service.cancel("market-1", scope)
    assert quota.calls[0]["event_id"] == "batch:market-1:cancelled"
    assert quota.calls[0]["category"] == "market"
    with pytest.raises(ValueError):
        await service.reschedule("market-1", scope, datetime(2026, 7, 11, 7, 0, tzinfo=UTC))
    target = datetime(2026, 7, 12, 8, 0, tzinfo=UTC)
    await service.reschedule("market-1", scope, target)
    assert repository.rescheduled == ("market-1", target)
    assert repository.approval_expire_hours == 7


@pytest.mark.asyncio
async def test_reschedule_rejects_beyond_max_schedule_window() -> None:
    repository = FakeRepository()
    service = SchedulingService(
        repository,
        FakeQuota(),
        FakePublisher(),
        clock=lambda: datetime(2026, 7, 11, 8, 0, tzinfo=UTC),
        max_schedule_ahead_days=90,
    )

    with pytest.raises(ValueError, match="不能超过"):
        await service.reschedule(
            "market-1",
            BatchAccessScope(app_id=7),
            datetime(2026, 10, 20, 8, 0, tzinfo=UTC),
        )
    assert repository.rescheduled is None


@pytest.mark.asyncio
async def test_outbox_cancel_does_not_apply_a_second_direct_refund() -> None:
    repository = FakeRepository()
    repository.cancelled = ScheduledBatch(
        "market-1",
        "market",
        app_id=7,
        dept="平台部",
        quota_date="20260711",
        quota_cost=9,
        outbox_persisted=True,
    )
    quota = FakeQuota()

    await SchedulingService(repository, quota, FakePublisher()).cancel(
        "market-1",
        BatchAccessScope(app_id=7),
    )

    assert quota.calls == []


class _Rows:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def one_or_none(self) -> dict[str, object] | None:
        return self.rows[0] if self.rows else None


class _Result:
    def __init__(self, rows: list[dict[str, object]] | None = None) -> None:
        self.rows = rows or []

    def mappings(self) -> _Rows:
        return _Rows(self.rows)

    def scalar(self) -> str:
        return "08:00-21:00"

    def scalar_one(self) -> int:
        return 2


class _Connection:
    def __init__(self, row: dict[str, object] | None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.row = row

    async def execute(self, statement: object, params: Any = None) -> _Result:
        sql = str(statement)
        self.calls.append((sql, params))
        if "batch_reschedule" in sql:
            raise RuntimeError("synthetic audit failure")
        if "SELECT" in sql and self.row is not None:
            return _Result([self.row])
        return _Result()


class _Context:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _Connection:
        return self.connection

    async def __aexit__(self, *_: object) -> None:
        return None


class _Engine:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def begin(self) -> _Context:
        return _Context(self.connection)

    async def dispose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_reschedule_audit_uses_shanghai_text_and_keeps_aware_column() -> None:
    scheduled_at = datetime(2026, 7, 12, 0, 30, tzinfo=UTC)
    connection = _Connection(
        {"id": 9, "channel": "web", "category": "notice", "had_approval": True},
    )
    repository = SqlSchedulingRepository()
    repository._engine = lambda: _Engine(connection)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="synthetic audit failure"):
        await repository.reschedule(
            "a" * 32,
            BatchAccessScope(all_departments=True),
            scheduled_at,
            approval_expire_hours=6,
        )

    update = next(params for sql, params in connection.calls if "UPDATE sms_batch" in sql)
    assert update["scheduled_at"] == scheduled_at
    assert update["status"] == "pending_approval"
    idem = next(
        params for sql, params in connection.calls if "idempotency_record" in sql
    )
    assert idem["scheduled_at"] == scheduled_at
    assert "CAST(:scheduled_at AS timestamptz)" in connection.calls[2][0]
    approval = next(sql for sql, _params in connection.calls if "UPDATE approval" in sql)
    assert "CAST(:expire_hours AS integer)" in approval
    assert "approver_account_id=NULL" in approval
    assert "approver_identity_id=NULL" in approval
    assert "generation=generation+1" in approval
    audit_sql = connection.calls[-1][0]
    assert "CAST(:scheduled_at AS text)" in audit_sql
    assert connection.calls[-1][1]["scheduled_at"] == scheduled_at.astimezone(
        ZoneInfo("Asia/Shanghai")
    ).isoformat()
    assert connection.calls[-1][1]["scheduled_at"].endswith("+08:00")
    assert connection.calls[-1][1]["generation"] == 2
    assert isinstance(update["scheduled_at"], datetime)
    assert update["deferred_reason"] is None


@pytest.mark.asyncio
async def test_reschedule_without_reapproval_still_types_the_audit_clock() -> None:
    scheduled_at = datetime(2026, 10, 23, 16, 0, tzinfo=UTC) + timedelta(0)
    connection = _Connection(
        {"id": 3, "channel": "api", "category": "notice", "had_approval": False}
    )
    repository = SqlSchedulingRepository()
    repository._engine = lambda: _Engine(connection)  # type: ignore[method-assign]
    raised = False
    try:
        await repository.reschedule(
            "b" * 32,
            BatchAccessScope(app_id=4),
            scheduled_at,
            approval_expire_hours=24,
        )
    except RuntimeError:
        raised = True
    assert raised
    assert all("UPDATE approval" not in sql for sql, _params in connection.calls)
    assert connection.calls[-1][1]["scheduled_at"] == "2026-10-24T00:00:00+08:00"


@pytest.mark.asyncio
async def test_market_reschedule_uses_the_next_window_immediately() -> None:
    requested = datetime(2026, 10, 26, 21, 24, 37, tzinfo=ZoneInfo("Asia/Shanghai"))
    connection = _Connection(
        {"id": 11, "channel": "web", "category": "market", "had_approval": False}
    )
    repository = SqlSchedulingRepository()
    repository._engine = lambda: _Engine(connection)  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="synthetic audit failure"):
        await repository.reschedule(
            "c" * 32,
            BatchAccessScope(all_departments=True),
            requested,
            approval_expire_hours=24,
        )
    update = next(params for sql, params in connection.calls if "UPDATE sms_batch" in sql)
    expected = datetime(2026, 10, 27, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert update["scheduled_at"] == expected
    assert update["deferred_reason"] == "market_window"
    assert connection.calls[-1][1]["scheduled_at"] == "2026-10-27T08:00:00+08:00"
