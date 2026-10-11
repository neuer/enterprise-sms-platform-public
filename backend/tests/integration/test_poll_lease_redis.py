"""真实 Redis 租期到期与接管；旧持有者不能释放新 token。"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from app.core.redis_lock import HeartbeatLock, LeaseUncertain

pytestmark = pytest.mark.skipif("AUTH_GUARD_REDIS_URL" not in os.environ, reason="isolated Redis 7")


@pytest.mark.asyncio
async def test_expired_owner_observes_takeover_and_cannot_release_new_lock() -> None:
    redis = Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"])
    key = f"test:poll-lease:{uuid4().hex}"
    old = HeartbeatLock(redis.lock(key, timeout=3, thread_local=False), ttl_s=3, beat_s=1)
    new = redis.lock(key, timeout=10, thread_local=False)
    try:
        assert await old.acquire()
        await redis.pexpire(key, 1)
        for _ in range(100):
            if await new.acquire(blocking=False):
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("isolated lock did not expire")
        await asyncio.wait_for(old.wait_uncertain(), 2)
        with pytest.raises(LeaseUncertain):
            old.ensure_lease()
        assert not await old.release()
        assert await new.owned()
        await new.release()
        assert await old.acquire()
        old.ensure_lease()
        assert await old.release()
    finally:
        await old.release()
        if await new.owned():
            await new.release()
        await redis.aclose()


@pytest.mark.asyncio
async def test_redis_connection_failure_signals_without_cancelling_work() -> None:
    redis = Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"])
    # 仅替换续租连接为本机不可连接端口，实际 owner 保留在隔离 Redis。
    unavailable = Redis(host="127.0.0.1", port=0, socket_connect_timeout=0.1)
    key = f"test:poll-lease:{uuid4().hex}"
    underlying = redis.lock(key, timeout=5, thread_local=False)
    heartbeat = HeartbeatLock(underlying, ttl_s=5, beat_s=1)
    try:
        assert await heartbeat.acquire()
        underlying.redis = unavailable
        await asyncio.wait_for(heartbeat.wait_uncertain(), 3)
        assert heartbeat.lease_uncertain
        underlying.redis = redis
        assert await underlying.owned()
        assert not await heartbeat.release()
        assert not await redis.exists(key)
    finally:
        underlying.redis = redis
        await heartbeat.release()
        await unavailable.aclose()
        await redis.aclose()
