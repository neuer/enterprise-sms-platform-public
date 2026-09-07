from __future__ import annotations

import asyncio
from uuid import UUID

import pytest

from app.services.outbox import OutboxDispatcher, OutboxLease, OutboxLeaseLost
from tests.test_outbox import FakeRepository, lease


@pytest.mark.asyncio
async def test_slow_broker_claims_only_available_slots_with_fresh_tail_leases() -> None:
    clock = 0
    leased_at: dict[UUID, int] = {}
    started: asyncio.Queue[UUID] = asyncio.Queue()
    release = asyncio.Semaphore(0)
    active = 0
    peak_active = 0

    class Repository(FakeRepository):
        async def lease_due(self, *, limit: int, lease_seconds: int) -> list[OutboxLease]:
            assert limit == 1
            result = await super().lease_due(limit=limit, lease_seconds=lease_seconds)
            for event in result:
                leased_at[event.event_id] = clock
            return result

        async def mark_published(self, event_id: UUID, lease_id: UUID) -> None:
            assert clock - leased_at[event_id] < 60
            await super().mark_published(event_id, lease_id)

    class Publisher:
        async def publish(self, event: OutboxLease) -> None:
            nonlocal active, peak_active
            active += 1
            peak_active = max(peak_active, active)
            started.put_nowait(event.event_id)
            try:
                await release.acquire()
            finally:
                active -= 1

    repository = Repository([lease() for _ in range(100)])
    dispatch = asyncio.create_task(OutboxDispatcher(repository, Publisher()).dispatch_once())
    try:
        for offset in range(0, 100, 4):
            for _ in range(4):
                await asyncio.wait_for(started.get(), 1)
            # 尾部仍为 pending，尚未取得租约；每组故意耗用 55/60 秒租期。
            assert len(leased_at) == offset + 4
            clock += 55
            for _ in range(4):
                release.release()
        assert await dispatch == 100
    finally:
        dispatch.cancel()
        await asyncio.gather(dispatch, return_exceptions=True)
    assert peak_active == 4
    assert active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_cancel_or_publish_writeback_failure_drains_workers_without_late_marks(
    cancel: bool,
) -> None:
    started: asyncio.Queue[UUID] = asyncio.Queue()
    release = asyncio.Event()
    stopped: set[UUID] = set()
    rows = [lease() for _ in range(100)]

    class Repository(FakeRepository):
        async def mark_published(self, event_id: UUID, lease_id: UUID) -> None:
            assert event_id == rows[0].event_id
            raise OutboxLeaseLost("synthetic writeback lost")

    class Publisher:
        async def publish(self, event: OutboxLease) -> None:
            started.put_nowait(event.event_id)
            try:
                if event.event_id == rows[0].event_id:
                    await release.wait()
                else:
                    await asyncio.Event().wait()
            finally:
                stopped.add(event.event_id)

    repository = Repository(rows.copy())
    dispatch = asyncio.create_task(OutboxDispatcher(repository, Publisher()).dispatch_once())
    for _ in range(4):
        await asyncio.wait_for(started.get(), 1)
    if cancel:
        dispatch.cancel()
        with pytest.raises(asyncio.CancelledError):
            await dispatch
    else:
        release.set()
        with pytest.raises(OutboxLeaseLost):
            await dispatch
    assert stopped == {row.event_id for row in rows[:4]}
    assert len(repository.leases) == 96
    assert not any(event[0] in {"published", "publish_failed"} for event in repository.events)
