from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.app_ratelimit import (
    WEIGHTED_WINDOW_LUA,
    ApplicationRateLimiter,
    ApplicationRateLimitExceeded,
    ControlPlaneUnavailable,
)


def test_weighted_cost_lua_uses_zrange_for_member_weights() -> None:
    assert "ZRANGE" in WEIGHTED_WINDOW_LUA
    assert "ZREMRANGEBYSCORE" in WEIGHTED_WINDOW_LUA


class FakeRedis:
    def __init__(self, allowed: int) -> None:
        self.allowed = allowed
        self.calls: list[tuple[object, ...]] = []

    async def eval(self, *args: object) -> int:
        self.calls.append(args)
        return self.allowed


@pytest.mark.asyncio
async def test_application_sliding_window_uses_app_key_and_atomic_lua() -> None:
    redis = FakeRedis(1)
    limiter = ApplicationRateLimiter(
        redis,
        clock=lambda: datetime(2026, 7, 11, 8, 0, tzinfo=UTC),
        nonce=lambda: "request-1",
    )
    await limiter.check(app_id=7, limit_per_minute=60)
    call = redis.calls[0]
    assert call[1:3] == (1, "ratelimit:app:7")
    assert call[-2:] == ("60", "request-1")


@pytest.mark.asyncio
async def test_application_sliding_window_rejects_at_limit() -> None:
    with pytest.raises(ApplicationRateLimitExceeded):
        await ApplicationRateLimiter(FakeRedis(0)).check(app_id=7, limit_per_minute=1)


class WeightedRedis:
    def __init__(self) -> None:
        self.totals: dict[str, int] = {}

    async def eval(self, script: object, numkeys: object, *args: object) -> int:
        if int(numkeys) != 2:
            return 1
        recipient_key = str(args[0])
        segment_key = str(args[1])
        recipient_limit = int(args[4])
        recipient_weight = int(args[5])
        segment_limit = int(args[6])
        segment_weight = int(args[7])
        next_recipients = self.totals.get(recipient_key, 0) + recipient_weight
        next_segments = self.totals.get(segment_key, 0) + segment_weight
        if next_recipients > recipient_limit or next_segments > segment_limit:
            return 0
        self.totals[recipient_key] = next_recipients
        self.totals[segment_key] = next_segments
        return 1


@pytest.mark.asyncio
async def test_one_large_request_equals_many_small_recipient_cost() -> None:
    limiter = ApplicationRateLimiter(WeightedRedis(), nonce=lambda: "n")
    await limiter.consume_send_cost(
        app_id=7,
        recipient_count=10_000,
        segment_count=10_000,
        recipient_limit=10_000,
        segment_limit=10_000,
    )
    with pytest.raises(ApplicationRateLimitExceeded):
        await limiter.consume_send_cost(
            app_id=7,
            recipient_count=100,
            segment_count=100,
            recipient_limit=10_000,
            segment_limit=10_000,
        )


@pytest.mark.asyncio
async def test_one_hundred_small_requests_fill_same_recipient_budget() -> None:
    limiter = ApplicationRateLimiter(WeightedRedis(), nonce=lambda: "n")
    for _ in range(100):
        await limiter.consume_send_cost(
            app_id=7,
            recipient_count=100,
            segment_count=100,
            recipient_limit=10_000,
            segment_limit=10_000,
        )
    with pytest.raises(ApplicationRateLimitExceeded):
        await limiter.consume_send_cost(
            app_id=7,
            recipient_count=1,
            segment_count=1,
            recipient_limit=10_000,
            segment_limit=10_000,
        )


@pytest.mark.asyncio
async def test_long_sms_counts_estimated_segments() -> None:
    from app.services.billing import calculate_quota_cost

    long_content = "测" * 140
    cost = calculate_quota_cost(long_content, recipient_count=10)
    assert cost == 30
    redis = WeightedRedis()
    limiter = ApplicationRateLimiter(redis, nonce=lambda: "n")
    await limiter.consume_send_cost(
        app_id=7,
        recipient_count=10,
        segment_count=cost,
        recipient_limit=10_000,
        segment_limit=30,
    )
    with pytest.raises(ApplicationRateLimitExceeded):
        await limiter.consume_send_cost(
            app_id=7,
            recipient_count=10,
            segment_count=cost,
            recipient_limit=10_000,
            segment_limit=30,
        )


@pytest.mark.asyncio
async def test_send_cost_redis_errors_fail_closed() -> None:
    class BoomRedis:
        async def eval(self, *args: object) -> int:
            raise RuntimeError("redis down")

    with pytest.raises(ControlPlaneUnavailable):
        await ApplicationRateLimiter(BoomRedis()).consume_send_cost(
            app_id=7,
            recipient_count=1,
            segment_count=1,
            recipient_limit=10_000,
            segment_limit=10_000,
        )


@pytest.mark.asyncio
async def test_replay_bucket_uses_independent_key() -> None:
    redis = FakeRedis(1)
    limiter = ApplicationRateLimiter(redis, nonce=lambda: "replay-1")
    await limiter.check_replay(app_id=7, limit_per_minute=60)
    assert redis.calls[0][1:3] == (1, "ratelimit:app:7:replay")
