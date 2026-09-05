from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.app_ratelimit import (
    COST_MIGRATION_TTL_SECONDS,
    WEIGHTED_WINDOW_LUA,
    ApplicationRateLimiter,
    ApplicationRateLimitExceeded,
    ControlPlaneUnavailable,
)


def test_weighted_cost_lua_uses_fixed_time_buckets() -> None:
    assert "ZRANGE" not in WEIGHTED_WINDOW_LUA
    assert "ZADD" not in WEIGHTED_WINDOW_LUA
    assert "HGETALL" not in WEIGHTED_WINDOW_LUA
    assert "HINCRBY" in WEIGHTED_WINDOW_LUA
    assert "redis.call('TIME')" in WEIGHTED_WINDOW_LUA
    assert "for slot = 0, 59" in WEIGHTED_WINDOW_LUA
    assert "last_epoch" in WEIGHTED_WINDOW_LUA
    assert "KEYS[3]" in WEIGHTED_WINDOW_LUA
    assert "KEYS[5]" in WEIGHTED_WINDOW_LUA
    assert "redis.call('TYPE'" in WEIGHTED_WINDOW_LUA
    assert "if v1_rec > recipients then" in WEIGHTED_WINDOW_LUA
    assert "rec_add = rec_add + (v1_rec - v2_rec)" in WEIGHTED_WINDOW_LUA
    assert "redis.call('DEL'" not in WEIGHTED_WINDOW_LUA
    assert "redis.call('KEYS'" not in WEIGHTED_WINDOW_LUA
    assert "redis.call('SCAN'" not in WEIGHTED_WINDOW_LUA


@pytest.mark.asyncio
async def test_weighted_cost_keys_stay_under_control_acl_prefix() -> None:
    redis = FakeRedis(1)
    limiter = ApplicationRateLimiter(
        redis,
        clock=lambda: datetime(2026, 7, 11, 8, 0, 12, tzinfo=UTC),
    )
    await limiter.consume_send_cost(
        app_id=7,
        recipient_count=3,
        segment_count=3,
        recipient_limit=100,
        segment_limit=100,
    )
    keys = redis.calls[0][2:7]
    assert keys == (
        "ratelimit:app:7:recipients:v2",
        "ratelimit:app:7:segments:v2",
        "ratelimit:app:7:recipients:buckets",
        "ratelimit:app:7:segments:buckets",
        "ratelimit:app:7:cost:mig",
    )
    assert redis.calls[0][1] == 5
    assert str(COST_MIGRATION_TTL_SECONDS) in redis.calls[0]


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
    assert "redis.call('TIME')" in call[0]


@pytest.mark.asyncio
async def test_application_sliding_window_rejects_at_limit() -> None:
    with pytest.raises(ApplicationRateLimitExceeded):
        await ApplicationRateLimiter(FakeRedis(0)).check(app_id=7, limit_per_minute=1)


class WeightedRedis:
    def __init__(self) -> None:
        self.totals: dict[str, int] = {}

    async def eval(self, script: object, numkeys: object, *args: object) -> int:
        if int(numkeys) < 2:
            return 1
        recipient_key = str(args[0])
        segment_key = str(args[1])
        argv_at = int(numkeys)
        recipient_limit = int(args[argv_at])
        recipient_weight = int(args[argv_at + 1])
        segment_limit = int(args[argv_at + 2])
        segment_weight = int(args[argv_at + 3])
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


class CostWindowRedis:
    """按 Lua 合同解释 v1 Hash + v2 环形槽，供迁移矩阵使用真实命令语义。"""

    def __init__(self, now_sec: int) -> None:
        self.now_sec = now_sec
        self.hashes: dict[str, dict[str, str]] = {}
        self.kinds: dict[str, str] = {}
        self.expires: dict[str, int] = {}
        self.deleted: list[str] = []

    def seed_hash(self, key: str, fields: dict[str, str]) -> None:
        self.kinds[key] = "hash"
        self.hashes[key] = dict(fields)

    def seed_string(self, key: str, value: str) -> None:
        self.kinds[key] = "string"
        self.hashes[key] = {"_string": value}

    def _type(self, key: str) -> str:
        return self.kinds.get(key, "none")

    def _hget(self, key: str, field: str) -> str | None:
        if self._type(key) == "none":
            return None
        if self._type(key) != "hash":
            raise RuntimeError("WRONGTYPE")
        return self.hashes.get(key, {}).get(field)

    def _hset(self, key: str, field: str, value: str) -> None:
        self.kinds[key] = "hash"
        self.hashes.setdefault(key, {})[field] = value

    def _hincrby(self, key: str, field: str, amount: int) -> None:
        current = int(self._hget(key, field) or "0")
        self._hset(key, field, str(current + amount))

    def _ring_total(self, key: str, now_sec: int) -> int:
        total = 0
        window_start = now_sec - 59
        for slot in range(60):
            epoch_raw = self._hget(key, f"e{slot}")
            epoch = int(epoch_raw) if epoch_raw is not None else None
            weight = int(self._hget(key, f"w{slot}") or "0")
            if epoch is not None and window_start <= epoch <= now_sec:
                total += weight
        return total

    def _v1_active(self, key: str, now_sec: int) -> int | None:
        kind = self._type(key)
        if kind == "none":
            return 0
        if kind != "hash":
            return None
        total = 0
        for epoch in range(now_sec - 59, now_sec + 1):
            raw = self._hget(key, str(epoch))
            if raw is None:
                continue
            try:
                weight = int(raw)
            except ValueError:
                return None
            if weight < 0:
                return None
            total += weight
        for epoch in range(now_sec + 1, now_sec + 60):
            if self._hget(key, str(epoch)) is not None:
                return None
        return total

    async def eval(self, script: object, numkeys: object, *args: object) -> int:
        assert "if v1_rec > recipients then" in str(script)
        assert int(numkeys) == 5
        rec_key, seg_key, v1_rec_key, v1_seg_key, marker_key = map(str, args[:5])
        rec_limit = int(args[5])
        rec_weight = int(args[6])
        seg_limit = int(args[7])
        seg_weight = int(args[8])
        ttl = int(args[9])
        marker_ttl = int(args[10])
        now_sec = self.now_sec
        last_raw = self._hget(rec_key, "last_epoch")
        if last_raw is not None and now_sec < int(last_raw):
            now_sec = int(last_raw)
        try:
            v1_rec = self._v1_active(v1_rec_key, now_sec)
            v1_seg = self._v1_active(v1_seg_key, now_sec)
        except RuntimeError:
            return -2
        if v1_rec is None or v1_seg is None:
            return -2
        recipients = max(self._ring_total(rec_key, now_sec), v1_rec)
        segments = max(self._ring_total(seg_key, now_sec), v1_seg)
        if recipients + rec_weight > rec_limit or segments + seg_weight > seg_limit:
            return 0
        rec_add = rec_weight + max(0, v1_rec - self._ring_total(rec_key, now_sec))
        seg_add = seg_weight + max(0, v1_seg - self._ring_total(seg_key, now_sec))
        slot = now_sec % 60
        for key, weight in ((rec_key, rec_add), (seg_key, seg_add)):
            owned = self._hget(key, f"e{slot}")
            if owned != str(now_sec):
                self._hset(key, f"e{slot}", str(now_sec))
                self._hset(key, f"w{slot}", str(weight))
            else:
                self._hincrby(key, f"w{slot}", weight)
            self._hset(key, "last_epoch", str(now_sec))
            self.expires[key] = ttl
        if self._hget(marker_key, "generation") is None:
            self._hset(marker_key, "schema_version", "2")
            self._hset(marker_key, "cutover_epoch", str(now_sec))
            self._hset(marker_key, "generation", "1")
            self._hset(marker_key, "state", "active")
            self.expires[marker_key] = marker_ttl
        return 1


def _limiter(redis: CostWindowRedis) -> ApplicationRateLimiter:
    return ApplicationRateLimiter(redis, nonce=lambda: "n")


@pytest.mark.asyncio
@pytest.mark.parametrize("used", [0, 100, 5_000, 9_900, 10_000])
async def test_v1_recipient_usage_is_inherited_by_v2(used: int) -> None:
    now = 1_778_000_000
    redis = CostWindowRedis(now)
    if used:
        redis.seed_hash("ratelimit:app:7:recipients:buckets", {str(now): str(used)})
        redis.seed_hash("ratelimit:app:7:segments:buckets", {str(now): str(used)})
    limiter = _limiter(redis)
    remaining = 10_000 - used
    if remaining:
        await limiter.consume_send_cost(
            app_id=7,
            recipient_count=remaining,
            segment_count=remaining,
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
    assert redis.hashes.get("ratelimit:app:7:recipients:buckets", {}) == (
        {str(now): str(used)} if used else {}
    )
    assert redis.deleted == []


@pytest.mark.asyncio
async def test_v1_full_recipients_with_partial_segments_rejects_without_v2_write() -> None:
    now = 1_778_000_100
    redis = CostWindowRedis(now)
    redis.seed_hash("ratelimit:app:7:recipients:buckets", {str(now): "10000"})
    redis.seed_hash("ratelimit:app:7:segments:buckets", {str(now): "100"})
    with pytest.raises(ApplicationRateLimitExceeded):
        await _limiter(redis).consume_send_cost(
            app_id=7,
            recipient_count=1,
            segment_count=1,
            recipient_limit=10_000,
            segment_limit=10_000,
        )
    assert "ratelimit:app:7:recipients:v2" not in redis.hashes


@pytest.mark.asyncio
async def test_v1_full_segments_with_partial_recipients_rejects() -> None:
    now = 1_778_000_200
    redis = CostWindowRedis(now)
    redis.seed_hash("ratelimit:app:7:recipients:buckets", {str(now): "100"})
    redis.seed_hash("ratelimit:app:7:segments:buckets", {str(now): "10000"})
    with pytest.raises(ApplicationRateLimitExceeded):
        await _limiter(redis).consume_send_cost(
            app_id=7,
            recipient_count=1,
            segment_count=1,
            recipient_limit=10_000,
            segment_limit=10_000,
        )


@pytest.mark.asyncio
async def test_v1_and_v2_use_max_not_sum() -> None:
    now = 1_778_000_300
    redis = CostWindowRedis(now)
    redis.seed_hash("ratelimit:app:7:recipients:buckets", {str(now): "4000"})
    redis.seed_hash("ratelimit:app:7:segments:buckets", {str(now): "4000"})
    slot = now % 60
    redis.seed_hash(
        "ratelimit:app:7:recipients:v2",
        {f"e{slot}": str(now), f"w{slot}": "4000", "last_epoch": str(now)},
    )
    redis.seed_hash(
        "ratelimit:app:7:segments:v2",
        {f"e{slot}": str(now), f"w{slot}": "4000", "last_epoch": str(now)},
    )
    await _limiter(redis).consume_send_cost(
        app_id=7,
        recipient_count=6_000,
        segment_count=6_000,
        recipient_limit=10_000,
        segment_limit=10_000,
    )
    assert redis.hashes["ratelimit:app:7:recipients:v2"][f"w{slot}"] == "10000"


@pytest.mark.asyncio
async def test_malformed_or_future_v1_fails_closed() -> None:
    now = 1_778_000_400
    broken = CostWindowRedis(now)
    broken.seed_hash("ratelimit:app:7:recipients:buckets", {str(now): "abc"})
    broken.seed_hash("ratelimit:app:7:segments:buckets", {str(now): "1"})
    with pytest.raises(ControlPlaneUnavailable):
        await _limiter(broken).consume_send_cost(
            app_id=7,
            recipient_count=1,
            segment_count=1,
            recipient_limit=10_000,
            segment_limit=10_000,
        )

    future = CostWindowRedis(now)
    future.seed_hash("ratelimit:app:7:recipients:buckets", {str(now + 5): "1"})
    future.seed_hash("ratelimit:app:7:segments:buckets", {})
    with pytest.raises(ControlPlaneUnavailable):
        await _limiter(future).consume_send_cost(
            app_id=7,
            recipient_count=1,
            segment_count=1,
            recipient_limit=10_000,
            segment_limit=10_000,
        )

    wrong_type = CostWindowRedis(now)
    wrong_type.seed_string("ratelimit:app:7:recipients:buckets", "nope")
    with pytest.raises(ControlPlaneUnavailable):
        await _limiter(wrong_type).consume_send_cost(
            app_id=7,
            recipient_count=1,
            segment_count=1,
            recipient_limit=10_000,
            segment_limit=10_000,
        )


@pytest.mark.asyncio
async def test_migration_marker_is_written_once() -> None:
    now = 1_778_000_500
    redis = CostWindowRedis(now)
    limiter = _limiter(redis)
    await limiter.consume_send_cost(
        app_id=7,
        recipient_count=1,
        segment_count=1,
        recipient_limit=10_000,
        segment_limit=10_000,
    )
    redis.hashes["ratelimit:app:7:cost:mig"]["generation"] = "1"
    first = dict(redis.hashes["ratelimit:app:7:cost:mig"])
    await limiter.consume_send_cost(
        app_id=7,
        recipient_count=1,
        segment_count=1,
        recipient_limit=10_000,
        segment_limit=10_000,
    )
    assert redis.hashes["ratelimit:app:7:cost:mig"] == first
    assert first["state"] == "active"
    assert first["schema_version"] == "2"
    assert redis.expires["ratelimit:app:7:cost:mig"] == COST_MIGRATION_TTL_SECONDS


@pytest.mark.asyncio
async def test_two_new_writers_share_inherited_v1_budget() -> None:
    now = 1_778_000_600
    redis = CostWindowRedis(now)
    redis.seed_hash("ratelimit:app:7:recipients:buckets", {str(now): "8000"})
    redis.seed_hash("ratelimit:app:7:segments:buckets", {str(now): "8000"})
    first = _limiter(redis)
    second = _limiter(redis)
    await first.consume_send_cost(
        app_id=7,
        recipient_count=1_000,
        segment_count=1_000,
        recipient_limit=10_000,
        segment_limit=10_000,
    )
    await second.consume_send_cost(
        app_id=7,
        recipient_count=1_000,
        segment_count=1_000,
        recipient_limit=10_000,
        segment_limit=10_000,
    )
    with pytest.raises(ApplicationRateLimitExceeded):
        await first.consume_send_cost(
            app_id=7,
            recipient_count=1,
            segment_count=1,
            recipient_limit=10_000,
            segment_limit=10_000,
        )
