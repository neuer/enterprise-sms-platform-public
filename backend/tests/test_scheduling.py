from __future__ import annotations

from datetime import UTC, datetime

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
