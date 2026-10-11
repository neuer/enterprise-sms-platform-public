from __future__ import annotations

import asyncio
from typing import Any

import pytest
from redis.exceptions import LockNotOwnedError

from app.core.redis_lock import HeartbeatLock, LeaseUncertain


class FakeLock:
    def __init__(self, *, acquired: bool = True, release_error: Exception | None = None) -> None:
        self.acquired = acquired
        self.release_error = release_error
        self.extend_calls: list[tuple[int, dict[str, Any]]] = []
        self.released = 0

    async def acquire(self, *, blocking: bool = False) -> bool:
        assert blocking is False
        return self.acquired

    async def extend(self, ttl: int, **kwargs: Any) -> None:
        self.extend_calls.append((ttl, kwargs))

    async def release(self) -> None:
        self.released += 1
        if self.release_error is not None:
            raise self.release_error


@pytest.mark.asyncio
async def test_heartbeat_extends_with_absolute_ttl_until_release() -> None:
    lock = FakeLock()
    heartbeat = HeartbeatLock(lock, ttl_s=90, beat_s=1)

    assert await heartbeat.acquire()
    await asyncio.sleep(1.2)
    assert await heartbeat.release()

    assert lock.extend_calls
    assert lock.extend_calls[0] == (90, {"replace_ttl": True})
    assert lock.released == 1
    extended = len(lock.extend_calls)
    await asyncio.sleep(1.2)
    assert len(lock.extend_calls) == extended


@pytest.mark.asyncio
async def test_busy_lock_is_not_acquired_and_never_extended() -> None:
    lock = FakeLock(acquired=False)
    heartbeat = HeartbeatLock(lock, ttl_s=90, beat_s=30)

    assert not await heartbeat.acquire()
    assert lock.extend_calls == []


@pytest.mark.asyncio
async def test_lost_lease_release_is_tolerated_and_reported() -> None:
    lock = FakeLock(release_error=LockNotOwnedError("expired"))
    heartbeat = HeartbeatLock(lock, ttl_s=90, beat_s=30)

    assert await heartbeat.acquire()
    assert not await heartbeat.release()
    assert lock.released == 1


def test_heartbeat_must_beat_faster_than_lease() -> None:
    with pytest.raises(ValueError):
        HeartbeatLock(FakeLock(), ttl_s=90, beat_s=90)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [False, ConnectionError("sensitive-token"), LockNotOwnedError("sensitive-key")]
)
async def test_renewal_failure_signals_once_and_reacquire_resets(
    failure: Any, caplog: pytest.LogCaptureFixture
) -> None:
    class FaultLock(FakeLock):
        async def extend(self, ttl: int, **kwargs: Any) -> Any:
            if isinstance(failure, Exception):
                raise failure
            return failure

    lock = FaultLock()
    heartbeat = HeartbeatLock(lock, ttl_s=2, beat_s=1)
    assert await heartbeat.acquire()
    assert not await heartbeat.acquire()
    task = heartbeat._task
    await asyncio.wait_for(heartbeat.wait_uncertain(), 2)
    assert heartbeat.lease_uncertain
    with pytest.raises(LeaseUncertain):
        heartbeat.ensure_lease()
    assert not await heartbeat.release()
    assert task is not None and task.done()
    assert not await heartbeat.release()
    assert lock.released == 1
    assert len([r for r in caplog.records if r.message == "redis lock renewal uncertain"]) == 1
    assert "sensitive" not in caplog.text
    assert await heartbeat.acquire()
    assert not heartbeat.lease_uncertain
    heartbeat.ensure_lease()
    assert await heartbeat.release()


@pytest.mark.asyncio
async def test_deadline_detected_at_boundary_even_before_heartbeat_runs(monkeypatch) -> None:
    now = [100.0]
    monkeypatch.setattr("app.core.redis_lock.monotonic", lambda: now[0])
    lock = HeartbeatLock(FakeLock(), ttl_s=3, beat_s=1)
    assert await lock.acquire()
    now[0] += 3
    with pytest.raises(LeaseUncertain):
        lock.ensure_lease()
    assert lock.uncertain_reason == "deadline_elapsed"
    assert not await lock.release()
    assert await lock.acquire()
    assert lock.uncertain_reason is None
    assert await lock.release()


@pytest.mark.asyncio
async def test_hung_renewal_is_cancelled_at_original_deadline() -> None:
    cancelled = asyncio.Event()

    class HungLock(FakeLock):
        async def extend(self, ttl: int, **kwargs: Any) -> bool:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            return True

    lock = HeartbeatLock(HungLock(), ttl_s=2, beat_s=1)
    assert await lock.acquire()
    await asyncio.wait_for(lock.wait_uncertain(), 3)
    assert cancelled.is_set()
    assert lock.uncertain_reason == "deadline_elapsed"
    assert not await lock.release()


@pytest.mark.asyncio
async def test_late_success_cannot_restore_an_expired_lease(monkeypatch) -> None:
    now = [100.0]
    monkeypatch.setattr("app.core.redis_lock.monotonic", lambda: now[0])

    class LateLock(FakeLock):
        async def extend(self, ttl: int, **kwargs: Any) -> bool:
            now[0] += 4
            return True

    lock = HeartbeatLock(LateLock(), ttl_s=3, beat_s=1)
    assert await lock.acquire()
    await asyncio.wait_for(lock.wait_uncertain(), 2)
    assert lock.uncertain_reason == "deadline_elapsed"
    assert not await lock.release()


@pytest.mark.asyncio
async def test_lease_watcher_counts_once_and_alerts_even_if_counter_fails() -> None:
    from app.tasks.poll_report import _finish_lease, _watch_lease

    class Events:
        def __init__(self):
            self.calls = []

        async def record(self, source, reason):
            self.calls.append((source, reason))
            raise ConnectionError("synthetic")

    class Alerts:
        def __init__(self):
            self.calls = []

        async def emit(self, **kwargs):
            self.calls.append(kwargs)

    lock = HeartbeatLock(FakeLock(), ttl_s=3, beat_s=1)
    assert await lock.acquire()
    events, alerts = Events(), Alerts()
    watcher = asyncio.create_task(_watch_lease(lock, alerts, "report", events))
    lock._mark_uncertain("owner_lost")
    await _finish_lease(lock, watcher, alerts, "report")
    assert events.calls == [("report", "owner_lost")]
    assert len(alerts.calls) == 1
    assert watcher.done()
