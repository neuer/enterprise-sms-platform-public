from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from typing_extensions import TypedDict

from app.services.freq import (
    MARKET_INCREMENT_LUA,
    VERIFY_INCREMENT_LUA,
    FrequencyFenceLost,
    FrequencyLimiter,
    FrequencyLimits,
)


class FakeRedis:
    def __init__(self, values: list[Any]) -> None:
        self.values = iter(values)
        self.calls: list[tuple[Any, ...]] = []

    async def eval(self, *args: Any) -> Any:
        self.calls.append(args)
        return next(self.values)


class WindowRedis:
    """按目标 Lua 契约模拟窗口计数与幂等组件缓存。"""

    def __init__(self) -> None:
        self.claims: dict[str, str] = {}
        self.counters: dict[str, int] = {}
        self.results: dict[str, dict[str, int]] = {}
        self.result_ttls: dict[str, int] = {}
        self.increments: dict[tuple[str, str], int] = {}

    def _component(self, counter_key: str, result_key: str, field: str, ttl: int) -> int:
        cached = self.results.get(result_key, {}).get(field)
        if cached is not None:
            return cached
        window_counter = (counter_key, result_key.rsplit(":", maxsplit=1)[-1])
        self.counters[counter_key] = self.increments.get(window_counter, 0) + 1
        self.increments[window_counter] = self.counters[counter_key]
        created = result_key not in self.results
        self.results.setdefault(result_key, {})[field] = self.counters[counter_key]
        if created:
            self.result_ttls[result_key] = ttl
        return self.counters[counter_key]

    async def eval(self, script: str, numkeys: int, *args: Any) -> list[int]:
        if script == VERIFY_INCREMENT_LUA:
            assert numkeys == 5
            minute_key, day_key, claim_key, result_min, result_day = args[:5]
            minute_ttl, day_ttl, fenced, token, field = args[5:]
            assert minute_ttl and day_ttl and fenced == "1"
            if self.claims.get(claim_key) != token:
                return [-1, 0, 0]
            minute_count = self._component(minute_key, result_min, field, int(minute_ttl))
            day_count = self._component(day_key, result_day, field, int(day_ttl))
            return [1, minute_count, day_count]
        if script == MARKET_INCREMENT_LUA:
            assert numkeys == 3
            count_key, claim_key, result_day = args[:3]
            day_ttl, fenced, token, field = args[3:]
            assert day_ttl and fenced == "1"
            if self.claims.get(claim_key) != token:
                return [-1, 0]
            count = self._component(count_key, result_day, field, int(day_ttl))
            return [1, count]
        raise AssertionError("unexpected script")


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **values: int) -> None:
        self.now += timedelta(**values)


class FencedAllowArgs(TypedDict):
    app_id: int
    phone_hmac: str
    limits: FrequencyLimits
    claim_key: str
    claim_token: str
    result_key: str


class AllowArgs(TypedDict):
    app_id: int
    phone_hmac: str
    limits: FrequencyLimits


class PartialFencedAllowArgs(TypedDict):
    app_id: int
    limits: FrequencyLimits
    claim_key: str
    result_key: str


@pytest.mark.asyncio
async def test_verify_uses_global_hmac_minute_and_day_keys_without_phone() -> None:
    redis = FakeRedis([[1, 2, 1]])
    limiter = FrequencyLimiter(redis, clock=lambda: datetime(2026, 7, 11, 0, 0, 30, tzinfo=UTC))

    allowed = await limiter.allow(
        "verify",
        app_id=9,
        phone_hmac="a" * 64,
        limits=FrequencyLimits(),
    )

    assert allowed is False
    assert len(redis.calls) == 1
    assert redis.calls[0][1:4] == (
        2,
        f"freq:v:{'a' * 64}:m",
        f"freq:v:{'a' * 64}:d",
    )
    assert "13800138000" not in str(redis.calls)


@pytest.mark.asyncio
async def test_market_uses_app_scoped_day_key_and_override() -> None:
    redis = FakeRedis([[1, 2]])
    limiter = FrequencyLimiter(redis, clock=lambda: datetime(2026, 7, 11, 8, 0, tzinfo=UTC))
    limits = FrequencyLimits.from_config(
        verify_per_minute=1,
        verify_per_day=10,
        market_per_day=1,
        override={"market_per_day": 2},
    )

    assert await limiter.allow("market", app_id=7, phone_hmac="b" * 64, limits=limits)
    assert redis.calls[0][2] == f"freq:m:7:{'b' * 64}:d"


@pytest.mark.asyncio
async def test_verify_fence_is_checked_in_same_lua_as_both_increments() -> None:
    redis = FakeRedis([[1, 1, 1]])
    limiter = FrequencyLimiter(redis, clock=lambda: datetime(2026, 7, 11, 0, 0, 30, tzinfo=UTC))

    assert await limiter.allow(
        "verify",
        app_id=7,
        phone_hmac="d" * 64,
        limits=FrequencyLimits(),
        claim_key="idem:claim:7:biz-1",
        claim_token="owner-token",
        result_key="idem:freq:7:biz-1",
    )
    call = redis.calls[0]
    assert call[1] == 5
    assert call[4] == "idem:claim:7:biz-1"
    assert call[5].startswith("idem:freq:7:biz-1:verify:m:")
    assert call[6].startswith("idem:freq:7:biz-1:verify:d:")
    assert "owner-token" in call


@pytest.mark.asyncio
async def test_frequency_fence_loss_is_explicit_and_writes_are_rejected() -> None:
    redis = FakeRedis([[-1, 0]])
    limiter = FrequencyLimiter(
        redis, clock=lambda: datetime(2026, 7, 11, 0, 0, tzinfo=UTC)
    )

    with pytest.raises(FrequencyFenceLost):
        await limiter.allow(
            "market",
            app_id=7,
            phone_hmac="e" * 64,
            limits=FrequencyLimits(),
            claim_key="idem:claim:7:biz-2",
            claim_token="stale-owner",
            result_key="idem:freq:7:biz-2",
        )


@pytest.mark.asyncio
async def test_owner_handoff_reuses_cached_frequency_result_without_increment() -> None:
    redis = WindowRedis()
    claim_key = "idem:claim:7:handoff"
    result_key = "idem:freq:7:handoff"
    redis.claims[claim_key] = "old-owner"
    limiter = FrequencyLimiter(
        redis, clock=lambda: datetime(2026, 7, 11, 0, 0, tzinfo=UTC)
    )
    phone1 = "1" * 64
    phone2 = "2" * 64
    kwargs: PartialFencedAllowArgs = {
        "app_id": 7,
        "limits": FrequencyLimits(),
        "claim_key": claim_key,
        "result_key": result_key,
    }

    assert await limiter.allow("verify", phone_hmac=phone1, claim_token="old-owner", **kwargs)
    redis.claims[claim_key] = "new-owner"
    with pytest.raises(FrequencyFenceLost):
        await limiter.allow("verify", phone_hmac=phone2, claim_token="old-owner", **kwargs)
    assert await limiter.allow("verify", phone_hmac=phone1, claim_token="new-owner", **kwargs)
    assert await limiter.allow("verify", phone_hmac=phone2, claim_token="new-owner", **kwargs)

    assert redis.counters[f"freq:v:{phone1}:m"] == 1
    assert redis.counters[f"freq:v:{phone2}:m"] == 1
    result_keys = sorted(redis.results)
    assert len(result_keys) == 2
    assert all(set(redis.results[key]) == {phone1, phone2} for key in result_keys)
    assert sorted(redis.result_ttls.values()) == sorted(
        [
            int(datetime(2026, 7, 11, 0, 1, tzinfo=UTC).timestamp() * 1000),
            int(datetime(2026, 7, 11, 16, 0, tzinfo=UTC).timestamp() * 1000),
        ]
    )
    assert "13800138000" not in str(redis.results)


@pytest.mark.asyncio
async def test_cross_minute_recounts_minute_but_reuses_same_day_count() -> None:
    redis = WindowRedis()
    clock = MutableClock(datetime(2026, 7, 11, 0, 0, 30, tzinfo=UTC))
    claim_key = "idem:claim:7:window"
    redis.claims[claim_key] = "owner"
    phone_hmac = "3" * 64
    limiter = FrequencyLimiter(redis, clock=clock)
    kwargs: FencedAllowArgs = {
        "app_id": 7,
        "phone_hmac": phone_hmac,
        "limits": FrequencyLimits(),
        "claim_key": claim_key,
        "claim_token": "owner",
        "result_key": "idem:freq:7:window",
    }

    assert await limiter.allow("verify", **kwargs)
    clock.advance(minutes=1)
    assert await limiter.allow("verify", **kwargs)
    minute_increments = [key for key in redis.increments if key[0].endswith(":m")]
    day_increments = [key for key in redis.increments if key[0].endswith(":d")]
    assert len(minute_increments) == 2
    assert len(day_increments) == 1


@pytest.mark.asyncio
async def test_microsecond_boundary_uses_same_counter_with_exact_window_expiry() -> None:
    clock = MutableClock(datetime(2026, 7, 11, 0, 0, 59, 500000, tzinfo=UTC))
    redis = FakeRedis([[1, 1, 1], [1, 1, 2]])
    limiter = FrequencyLimiter(redis, clock=clock)
    values: FencedAllowArgs = {
        "app_id": 7,
        "phone_hmac": "9" * 64,
        "limits": FrequencyLimits(),
        "claim_key": "idem:claim:7:edge",
        "claim_token": "owner",
        "result_key": "idem:freq:7:edge",
    }
    await limiter.allow("verify", **values)
    clock.advance(seconds=1)
    await limiter.allow("verify", **values)

    first, second = redis.calls
    assert first[2:4] == second[2:4]
    assert first[7] == str(
        int(datetime(2026, 7, 11, 0, 1, tzinfo=UTC).timestamp() * 1000)
    )
    assert second[7] == str(
        int(datetime(2026, 7, 11, 0, 2, tzinfo=UTC).timestamp() * 1000)
    )
    assert first[5] != second[5]


@pytest.mark.asyncio
async def test_cross_shanghai_day_recounts_both_components() -> None:
    redis = WindowRedis()
    clock = MutableClock(datetime(2026, 7, 11, 15, 59, 30, tzinfo=UTC))
    claim_key = "idem:claim:7:day"
    redis.claims[claim_key] = "owner"
    limiter = FrequencyLimiter(redis, clock=clock)
    kwargs: FencedAllowArgs = {
        "app_id": 7,
        "phone_hmac": "4" * 64,
        "limits": FrequencyLimits(),
        "claim_key": claim_key,
        "claim_token": "owner",
        "result_key": "idem:freq:7:day",
    }

    assert await limiter.allow("verify", **kwargs)
    clock.advance(minutes=1)
    assert await limiter.allow("verify", **kwargs)
    assert len(redis.increments) == 4


@pytest.mark.asyncio
async def test_cached_over_limit_count_denies_in_window_then_new_window_rechecks() -> None:
    redis = WindowRedis()
    clock = MutableClock(datetime(2026, 7, 11, 0, 0, 30, tzinfo=UTC))
    phone_hmac = "5" * 64
    limiter = FrequencyLimiter(redis, clock=clock)
    common: AllowArgs = {
        "app_id": 7,
        "phone_hmac": phone_hmac,
        "limits": FrequencyLimits(),
    }
    redis.claims["idem:claim:7:first"] = "first"
    assert await limiter.allow(
        "verify",
        **common,
        claim_key="idem:claim:7:first",
        claim_token="first",
        result_key="idem:freq:7:first",
    )
    redis.claims["idem:claim:7:deny"] = "old"
    deny: FencedAllowArgs = {
        "app_id": 7,
        "phone_hmac": phone_hmac,
        "limits": FrequencyLimits(),
        "claim_key": "idem:claim:7:deny",
        "claim_token": "old",
        "result_key": "idem:freq:7:deny",
    }
    assert not await limiter.allow("verify", **deny)
    redis.claims["idem:claim:7:deny"] = "new"
    deny["claim_token"] = "new"
    assert not await limiter.allow("verify", **deny)
    clock.advance(minutes=1)
    assert await limiter.allow("verify", **deny)


@pytest.mark.asyncio
async def test_same_biz_verify_and_market_do_not_share_day_component() -> None:
    redis = WindowRedis()
    clock = MutableClock(datetime(2026, 7, 11, 0, 0, 30, tzinfo=UTC))
    claim_key = "idem:claim:7:category"
    redis.claims[claim_key] = "owner"
    limiter = FrequencyLimiter(redis, clock=clock)
    common: FencedAllowArgs = {
        "app_id": 7,
        "phone_hmac": "6" * 64,
        "limits": FrequencyLimits(),
        "claim_key": claim_key,
        "claim_token": "owner",
        "result_key": "idem:freq:7:category",
    }
    assert await limiter.allow("verify", **common)
    assert await limiter.allow("market", **common)
    result_keys = set(redis.results)
    assert any(":verify:d:" in key for key in result_keys)
    assert any(":market:d:" in key for key in result_keys)
    assert redis.counters[f"freq:m:7:{'6' * 64}:d"] == 1


def test_frequency_lua_orders_fence_cache_and_increment() -> None:
    for script in (VERIFY_INCREMENT_LUA, MARKET_INCREMENT_LUA):
        assert script.index("redis.call('GET'") < script.index("redis.call('HGET'")
        assert script.index("redis.call('HGET'") < script.index("redis.call('INCR'")
        assert "PEXPIREAT" in script
        assert "redis.call('EXPIRE'" not in script


@pytest.mark.asyncio
async def test_notice_has_no_number_frequency_limit() -> None:
    redis = FakeRedis([])
    limiter = FrequencyLimiter(redis)
    assert await limiter.allow(
        "notice",
        app_id=1,
        phone_hmac="c" * 64,
        limits=FrequencyLimits(),
    )
    assert redis.calls == []
