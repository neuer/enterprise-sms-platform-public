from __future__ import annotations

from typing import Any

import pytest

from app.core.ratelimit import TOKEN_BUCKET_LUA, TOKEN_REFUND_LUA, TokenBucket


class FakeRedis:
    def __init__(self, result: int) -> None:
        self.result = result
        self.calls: list[tuple[Any, ...]] = []

    async def eval(self, *args: Any) -> Any:
        self.calls.append(args)
        return self.result


@pytest.mark.asyncio
async def test_token_bucket_passes_lane_capacity_and_reservation_to_one_lua_call() -> None:
    redis = FakeRedis(12000)
    bucket = TokenBucket(redis, key="vendor:tokens")
    assert await bucket.acquire(
        lane="bulk",
        vendor_qps=5,
        reserved_realtime_qps=2,
        now_ms=12345,
    ) == 12000
    call = redis.calls[0]
    assert call[1:3] == (1, "vendor:tokens")
    assert call[3:] == ("bulk", "5", "2", "12345")


@pytest.mark.asyncio
async def test_token_bucket_rejects_invalid_lane_and_reservation() -> None:
    bucket = TokenBucket(FakeRedis(-1))
    with pytest.raises(ValueError):
        await bucket.acquire(
            lane="other",
            vendor_qps=5,
            reserved_realtime_qps=2,
        )
    with pytest.raises(ValueError):
        await bucket.acquire(
            lane="bulk",
            vendor_qps=2,
            reserved_realtime_qps=2,
        )


@pytest.mark.asyncio
async def test_token_refund_is_atomic_and_capped_at_vendor_qps() -> None:
    redis = FakeRedis(1)
    bucket = TokenBucket(redis, key="vendor:tokens")

    await bucket.refund(vendor_qps=5, lease_epoch=12000)

    call = redis.calls[0]
    assert call[0] == TOKEN_REFUND_LUA
    assert call[1:] == (1, "vendor:tokens", "5", "12000")
    assert "math.min(capacity, tokens + 1)" in TOKEN_REFUND_LUA
    assert "last_ms ~= lease_epoch" not in TOKEN_REFUND_LUA
    assert "tokens == nil or last_ms == nil" in TOKEN_REFUND_LUA
    assert "HINCRBY" not in TOKEN_BUCKET_LUA
    assert "HINCRBY" not in TOKEN_REFUND_LUA
    assert "return last_ms" in TOKEN_BUCKET_LUA


@pytest.mark.asyncio
async def test_rejected_acquire_returns_no_lease_for_refund() -> None:
    bucket = TokenBucket(FakeRedis(-1))
    assert (
        await bucket.acquire(
            lane="realtime",
            vendor_qps=5,
            reserved_realtime_qps=2,
            now_ms=13000,
        )
        is None
    )
