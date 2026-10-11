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
