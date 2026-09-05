"""真实 Redis 7：v1 buckets 活动窗口必须被 v2 环形槽继承，不得重置额度。"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from app.services.app_ratelimit import (
    ApplicationRateLimiter,
    ApplicationRateLimitExceeded,
    ControlPlaneUnavailable,
)

pytestmark = pytest.mark.skipif(
    "AUTH_GUARD_REDIS_URL" not in os.environ,
    reason="requires isolated Redis 7",
)


async def _client() -> Redis:
    redis = Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"], decode_responses=True)
    await redis.ping()
    return redis


def _app_id() -> int:
    return 8_000_000 + int.from_bytes(uuid4().bytes[:3], "big")


@pytest.mark.asyncio
async def test_real_redis_exhausted_v1_does_not_reset_on_v2() -> None:
    redis = await _client()
    app_id = _app_id()
    rec_v1 = f"ratelimit:app:{app_id}:recipients:buckets"
    seg_v1 = f"ratelimit:app:{app_id}:segments:buckets"
    rec_v2 = f"ratelimit:app:{app_id}:recipients:v2"
    mig = f"ratelimit:app:{app_id}:cost:mig"
    try:
        now_sec = int((await redis.time())[0])
        await redis.hset(rec_v1, mapping={str(now_sec): "10000"})
        await redis.hset(seg_v1, mapping={str(now_sec): "10000"})
        await redis.expire(rec_v1, 70)
        await redis.expire(seg_v1, 70)
        limiter = ApplicationRateLimiter(redis)
        with pytest.raises(ApplicationRateLimitExceeded):
            await limiter.consume_send_cost(
                app_id=app_id,
                recipient_count=1,
                segment_count=1,
                recipient_limit=10_000,
                segment_limit=10_000,
            )
        assert await redis.exists(rec_v2) == 0
        assert await redis.hget(rec_v1, str(now_sec)) == "10000"
        assert await redis.exists(mig) == 0
    finally:
        await redis.delete(rec_v1, seg_v1, rec_v2, f"ratelimit:app:{app_id}:segments:v2", mig)
        await redis.aclose()


@pytest.mark.asyncio
async def test_real_redis_partial_v1_is_inherited_by_two_writers() -> None:
    redis = await _client()
    second = Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"], decode_responses=True)
    app_id = _app_id()
    rec_v1 = f"ratelimit:app:{app_id}:recipients:buckets"
    seg_v1 = f"ratelimit:app:{app_id}:segments:buckets"
    rec_v2 = f"ratelimit:app:{app_id}:recipients:v2"
    seg_v2 = f"ratelimit:app:{app_id}:segments:v2"
    mig = f"ratelimit:app:{app_id}:cost:mig"
    try:
        now_sec = int((await redis.time())[0])
        await redis.hset(rec_v1, mapping={str(now_sec): "8000"})
        await redis.hset(seg_v1, mapping={str(now_sec): "8000"})
        first = ApplicationRateLimiter(redis)
        other = ApplicationRateLimiter(second)
        await first.consume_send_cost(
            app_id=app_id,
            recipient_count=1_000,
            segment_count=1_000,
            recipient_limit=10_000,
            segment_limit=10_000,
        )
        await other.consume_send_cost(
            app_id=app_id,
            recipient_count=1_000,
            segment_count=1_000,
            recipient_limit=10_000,
            segment_limit=10_000,
        )
        with pytest.raises(ApplicationRateLimitExceeded):
            await first.consume_send_cost(
                app_id=app_id,
                recipient_count=1,
                segment_count=1,
                recipient_limit=10_000,
                segment_limit=10_000,
            )
        assert await redis.hget(rec_v1, str(now_sec)) == "8000"
        assert await redis.hget(mig, "state") == "active"
        assert await redis.hget(mig, "schema_version") == "2"
        # 首次写入把 v1 差额折进 v2，两笔 1000 后环形槽总量为 10000，而不是 2000。
        copied = 0
        for slot in range(60):
            copied += int(await redis.hget(rec_v2, f"w{slot}") or 0)
        assert copied == 10_000
    finally:
        await redis.delete(rec_v1, seg_v1, rec_v2, seg_v2, mig)
        await redis.aclose()
        await second.aclose()


@pytest.mark.asyncio
async def test_real_redis_malformed_v1_fails_closed() -> None:
    redis = await _client()
    app_id = _app_id()
    rec_v1 = f"ratelimit:app:{app_id}:recipients:buckets"
    seg_v1 = f"ratelimit:app:{app_id}:segments:buckets"
    try:
        now_sec = int((await redis.time())[0])
        await redis.hset(rec_v1, mapping={str(now_sec): "abc"})
        await redis.hset(seg_v1, mapping={str(now_sec): "1"})
        with pytest.raises(ControlPlaneUnavailable):
            await ApplicationRateLimiter(redis).consume_send_cost(
                app_id=app_id,
                recipient_count=1,
                segment_count=1,
                recipient_limit=10_000,
                segment_limit=10_000,
            )
    finally:
        await redis.delete(rec_v1, seg_v1)
        await redis.aclose()
