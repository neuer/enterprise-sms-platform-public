"""实际供应商 Lua：宿主时钟不能补桶，服务端回退不能重复发放额度。"""

from __future__ import annotations

from typing import Any

import pytest

import app.core.ratelimit as ratelimit
from app.core.ratelimit import TokenBucket
from tests.support.lua_redis import LuaRedis, lua_binary

pytestmark = pytest.mark.skipif(
    lua_binary() is None, reason="requires Lua; real Redis also covered by integration"
)


class ClockRedis:
    def __init__(self) -> None:
        self.server = LuaRedis(1000)

    async def eval(self, *args: Any) -> Any:
        return self.server.eval(*args)


async def take(
    bucket: TokenBucket, *, vendor: str = "primary", lane: str = "realtime"
) -> int | None:
    return await bucket.acquire(lane=lane, vendor_qps=5, reserved_realtime_qps=2, vendor_id=vendor)


@pytest.mark.asyncio
@pytest.mark.parametrize("skew", [1, 120])
async def test_worker_clock_interleaving_never_refills(
    skew: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    redis = ClockRedis()
    bucket = TokenBucket(redis)
    granted = []
    for index in range(20):
        host_time = 1000 + (skew if index % 2 == 0 else -skew)
        monkeypatch.setattr(ratelimit, "time", lambda value=host_time: value, raising=False)
        granted.append(await take(bucket))
    assert sum(value is not None for value in granted) == 5
    assert redis.server.hgetall("ratelimit:vendor:primary") == {"tokens": "0", "last_ms": "1000000"}


@pytest.mark.asyncio
async def test_server_rollback_recovery_and_jump_never_duplicate_epoch() -> None:
    redis = ClockRedis()
    bucket = TokenBucket(redis)
    assert await take(bucket) == 1000000
    redis.server.now_sec = 880
    assert await take(bucket) is None
    assert redis.server.hgetall("ratelimit:vendor:primary")["last_ms"] == "1000000"
    assert redis.server.expires["ratelimit:vendor:primary"] >= 121000
    redis.server.now_sec = 1000
    assert await take(bucket) is None
    redis.server.now_sec = 1001
    assert [await take(bucket) for _ in range(6)] == [1001000] * 5 + [None]
    redis.server.now_sec = 9000
    assert [await take(bucket) for _ in range(6)] == [9000000] * 5 + [None]


@pytest.mark.asyncio
async def test_lanes_vendor_and_refund_share_only_the_right_epoch() -> None:
    redis = ClockRedis()
    bucket = TokenBucket(redis)
    assert [await take(bucket, lane="bulk") for _ in range(4)] == [1000000] * 3 + [None]
    assert [await take(bucket) for _ in range(3)] == [1000000] * 2 + [None]
    assert await take(bucket, vendor="backup") == 1000000
    await bucket.refund(vendor_qps=5, lease_epoch=1000000, vendor_id="backup")
    assert await take(bucket) is None
    await bucket.refund(vendor_qps=5, lease_epoch=1000000, vendor_id="primary")
    assert await take(bucket) == 1000000
    redis.server.now_sec += 1
    assert await take(bucket) == 1001000
    await bucket.refund(vendor_qps=5, lease_epoch=1000000, vendor_id="primary")
    assert redis.server.hgetall("ratelimit:vendor:primary")["tokens"] == "4"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields",
    [
        {"tokens": "3"},
        {"last_ms": "1000000"},
        {"tokens": "bad", "last_ms": "1000000"},
        {"tokens": "-1", "last_ms": "1000000"},
        {"tokens": "1.5", "last_ms": "1000000"},
    ],
)
async def test_incomplete_or_invalid_existing_bucket_fails_closed(fields: dict[str, str]) -> None:
    redis = ClockRedis()
    redis.server.seed_hash("ratelimit:vendor:primary", fields)
    with pytest.raises(RuntimeError, match="state unavailable"):
        await take(TokenBucket(redis))
    assert redis.server.hgetall("ratelimit:vendor:primary") == fields


@pytest.mark.asyncio
async def test_wrong_type_and_redis_failure_do_not_grant() -> None:
    redis = ClockRedis()
    redis.server.seed_string("ratelimit:vendor:primary", "invalid")
    with pytest.raises(RuntimeError, match="state unavailable"):
        await take(TokenBucket(redis))

    class Unavailable:
        async def eval(self, *_args: Any) -> None:
            raise ConnectionError("TIME or EVAL denied")

    with pytest.raises(ConnectionError):
        await take(TokenBucket(Unavailable()))


@pytest.mark.asyncio
async def test_refund_during_clock_rollback_does_not_reopen_or_expire_future_budget() -> None:
    redis = ClockRedis()
    bucket = TokenBucket(redis)
    assert await take(bucket) == 1000000
    redis.server.now_sec = 880
    assert await take(bucket) is None
    ttl = redis.server.expires["ratelimit:vendor:primary"]
    await bucket.refund(vendor_qps=5, lease_epoch=1000000, vendor_id="primary")
    assert redis.server.hgetall("ratelimit:vendor:primary")["tokens"] == "0"
    # Lua shim 仅返回本次 TTL 写入；拒绝退款不得缩短高水位保护期。
    assert redis.server.expires.get("ratelimit:vendor:primary", ttl) >= ttl
