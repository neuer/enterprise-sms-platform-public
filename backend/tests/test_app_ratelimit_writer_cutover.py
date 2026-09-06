"""#676 方案 A：真实 Lua + 切换状态机 + 新旧 writer 驱动。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.services.app_ratelimit import (
    COST_BUCKET_TTL_SECONDS,
    COST_WINDOW_SECONDS,
    WEIGHTED_WINDOW_LUA,
    WRITER_CUTOVER_MARKER_KEY,
    ApplicationRateLimiter,
    ApplicationRateLimitExceeded,
    ControlPlaneUnavailable,
)
from app.services.app_ratelimit_cutover import (
    COST_WINDOW_SECONDS as CUTOVER_WINDOW,
)
from app.services.app_ratelimit_cutover import (
    CUTOVER_ADMISSION_REASON,
    CUTOVER_SAFETY_MARGIN_SECONDS,
    WRITER_PROTOCOL_VERSION,
    AdmissionView,
    CutoverError,
    ProbeResult,
    check_supported_launch,
    parse_cutover_marker,
    read_cutover_marker,
    run_writer_cutover,
    trusted_writer_version,
)
from tests.support.lua_redis import AsyncLuaRedis, LuaRedis

# 8354c31 旧 writer：只写 v1 :buckets，不认识 marker / v2。
V1_BUCKETS_LUA = """
local rec_key = KEYS[1]
local seg_key = KEYS[2]
local now_sec = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local rec_limit = tonumber(ARGV[3])
local rec_weight = tonumber(ARGV[4])
local seg_limit = tonumber(ARGV[5])
local seg_weight = tonumber(ARGV[6])
local ttl = tonumber(ARGV[7])
local function window_total(key)
  local total = 0
  for offset = 0, window - 1 do
    local epoch = now_sec - offset
    local weight = tonumber(redis.call('HGET', key, tostring(epoch))) or 0
    total = total + weight
  end
  redis.call('HDEL', key, tostring(now_sec - window))
  return total
end
local recipients = window_total(rec_key)
local segments = window_total(seg_key)
if recipients + rec_weight > rec_limit then return 0 end
if segments + seg_weight > seg_limit then return 0 end
redis.call('HINCRBY', rec_key, tostring(now_sec), rec_weight)
redis.call('HINCRBY', seg_key, tostring(now_sec), seg_weight)
redis.call('EXPIRE', rec_key, ttl)
redis.call('EXPIRE', seg_key, ttl)
return 1
"""

# #658 新 writer：max(v1,v2) 且首个业务请求会把空 marker 写成 active。
LEGACY_MAX_MERGE_LUA = """
local rec_key = KEYS[1]
local seg_key = KEYS[2]
local v1_rec_key = KEYS[3]
local v1_seg_key = KEYS[4]
local marker_key = KEYS[5]
local rec_limit = tonumber(ARGV[1])
local rec_weight = tonumber(ARGV[2])
local seg_limit = tonumber(ARGV[3])
local seg_weight = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])
local marker_ttl = tonumber(ARGV[6])
local t = redis.call('TIME')
local now_sec = tonumber(t[1])
local last = tonumber(redis.call('HGET', rec_key, 'last_epoch'))
if last ~= nil and now_sec < last then
  now_sec = last
end
local function redis_type(key)
  local typ = redis.call('TYPE', key)
  if type(typ) == 'table' and typ.ok ~= nil then
    return typ.ok
  end
  return typ
end
local function ring_total(key)
  local total = 0
  local window_start = now_sec - 59
  for slot = 0, 59 do
    local epoch = tonumber(redis.call('HGET', key, 'e'..slot))
    local weight = tonumber(redis.call('HGET', key, 'w'..slot)) or 0
    if epoch ~= nil and epoch >= window_start and epoch <= now_sec then
      total = total + weight
    end
  end
  return total
end
local function v1_active(key)
  local typ = redis_type(key)
  if typ == 'none' then
    return 0
  end
  if typ ~= 'hash' then
    return nil
  end
  local total = 0
  local window_start = now_sec - 59
  for epoch = window_start, now_sec do
    local raw = redis.call('HGET', key, tostring(epoch))
    if raw ~= false and raw ~= nil then
      local weight = tonumber(raw)
      if weight == nil or weight < 0 then
        return nil
      end
      total = total + weight
    end
  end
  return total
end
local v2_rec = ring_total(rec_key)
local v2_seg = ring_total(seg_key)
local v1_rec = v1_active(v1_rec_key)
local v1_seg = v1_active(v1_seg_key)
if v1_rec == nil or v1_seg == nil then
  return -2
end
local recipients = v2_rec
if v1_rec > recipients then
  recipients = v1_rec
end
local segments = v2_seg
if v1_seg > segments then
  segments = v1_seg
end
if recipients + rec_weight > rec_limit then return 0 end
if segments + seg_weight > seg_limit then return 0 end
local rec_add = rec_weight
if v1_rec > v2_rec then
  rec_add = rec_add + (v1_rec - v2_rec)
end
local seg_add = seg_weight
if v1_seg > v2_seg then
  seg_add = seg_add + (v1_seg - v2_seg)
end
local slot = now_sec % 60
local function ring_add(key, weight)
  local epoch_field = 'e'..slot
  local weight_field = 'w'..slot
  local owned = tonumber(redis.call('HGET', key, epoch_field))
  if owned ~= now_sec then
    redis.call('HSET', key, epoch_field, now_sec)
    redis.call('HSET', key, weight_field, weight)
  else
    redis.call('HINCRBY', key, weight_field, weight)
  end
  redis.call('HSET', key, 'last_epoch', now_sec)
  redis.call('EXPIRE', key, ttl)
end
ring_add(rec_key, rec_add)
ring_add(seg_key, seg_add)
if redis.call('HGET', marker_key, 'generation') == false then
  redis.call('HSET', marker_key, 'schema_version', '2')
  redis.call('HSET', marker_key, 'cutover_epoch', tostring(now_sec))
  redis.call('HSET', marker_key, 'generation', '1')
  redis.call('HSET', marker_key, 'state', 'active')
  redis.call('EXPIRE', marker_key, marker_ttl)
end
return 1
"""


def _keys(app_id: int) -> tuple[str, str, str, str]:
    return (
        f"ratelimit:app:{app_id}:recipients:v2",
        f"ratelimit:app:{app_id}:segments:v2",
        f"ratelimit:app:{app_id}:recipients:buckets",
        f"ratelimit:app:{app_id}:segments:buckets",
    )


class V1BucketWriter:
    """实际旧二进制驱动：只递增 v1 epoch Hash。"""

    def consume(
        self,
        redis: LuaRedis,
        *,
        app_id: int,
        weight: int,
        limit: int,
    ) -> int:
        rec_v2, seg_v2, rec_v1, seg_v1 = _keys(app_id)
        _ = rec_v2, seg_v2
        return int(
            redis.eval(
                V1_BUCKETS_LUA,
                2,
                rec_v1,
                seg_v1,
                str(redis.now_sec),
                "60",
                str(limit),
                str(weight),
                str(limit),
                str(weight),
                str(COST_BUCKET_TTL_SECONDS),
            )
        )


class LegacyMaxMergeWriter:
    """#658 新 writer：max 合并且会把空 marker 写成 active。"""

    def consume(
        self,
        redis: LuaRedis,
        *,
        app_id: int,
        weight: int,
        limit: int,
    ) -> int:
        rec_v2, seg_v2, rec_v1, seg_v1 = _keys(app_id)
        return int(
            redis.eval(
                LEGACY_MAX_MERGE_LUA,
                5,
                rec_v2,
                seg_v2,
                rec_v1,
                seg_v1,
                f"ratelimit:app:{app_id}:cost:mig",
                str(limit),
                str(weight),
                str(limit),
                str(weight),
                str(COST_BUCKET_TTL_SECONDS),
                "604800",
            )
        )


def _seed_v1(redis: LuaRedis, app_id: int, used: int) -> None:
    now = redis.now_sec
    _, _, rec_v1, seg_v1 = _keys(app_id)
    redis.seed_hash(rec_v1, {str(now): str(used)})
    redis.seed_hash(seg_v1, {str(now): str(used)})


def _seed_v2(redis: LuaRedis, app_id: int, used: int) -> None:
    now = redis.now_sec
    rec_v2, seg_v2, _, _ = _keys(app_id)
    slot = now % 60
    redis.seed_hash(
        rec_v2,
        {f"e{slot}": str(now), f"w{slot}": str(used), "last_epoch": str(now)},
    )
    redis.seed_hash(
        seg_v2,
        {f"e{slot}": str(now), f"w{slot}": str(used), "last_epoch": str(now)},
    )


def _active_marker_fields(*, generation: int = 1, now: int = 1_778_100_000) -> dict[str, str]:
    return {
        "schema_version": "1",
        "generation": str(generation),
        "target_writer_version": "2",
        "minimum_writer_version": "2",
        "fence_time": str(now - 80),
        "not_before": str(now - 15),
        "state": "active_v2",
        "release_binding": "rel-676",
        "admission_reason": CUTOVER_ADMISSION_REASON,
        "window_seconds": str(COST_WINDOW_SECONDS),
        "safety_margin_seconds": str(CUTOVER_SAFETY_MARGIN_SECONDS),
    }


def _seed_active_marker(redis: LuaRedis, *, generation: int = 1) -> None:
    redis.seed_hash(
        WRITER_CUTOVER_MARKER_KEY,
        _active_marker_fields(generation=generation, now=redis.now_sec),
    )


def _v2_total(redis: LuaRedis, app_id: int) -> int:
    rec_v2, _, _, _ = _keys(app_id)
    total = 0
    for slot in range(60):
        total += int(redis.hashes.get(rec_v2, {}).get(f"w{slot}", "0"))
    return total


def _v1_total(redis: LuaRedis, app_id: int) -> int:
    _, _, rec_v1, _ = _keys(app_id)
    now = redis.now_sec
    return int(redis.hashes.get(rec_v1, {}).get(str(now), "0") or 0)


@dataclass
class RecordingExecutor:
    old_writer_present: bool = False
    inflight_present: bool = False
    probe_status: str = "absent"
    redis_error: bool = False
    admission_state: str = "open"
    admission_reason: str = "ok"
    preexisting_closed: str | None = None
    root_version: int = WRITER_PROTOCOL_VERSION
    events: list[str] = field(default_factory=list)

    def close_admission(self, *, reason: str, generation: int) -> AdmissionView:
        self.events.append(f"close:{reason}:{generation}")
        if self.preexisting_closed is not None:
            self.admission_state = "closed"
            self.admission_reason = self.preexisting_closed
            return self.query_admission()
        self.admission_state = "closed"
        self.admission_reason = reason
        return self.query_admission()

    def query_admission(self) -> AdmissionView:
        if self.redis_error:
            raise CutoverError("admission facts unavailable")
        owned = (
            self.admission_state == "closed"
            and self.admission_reason == CUTOVER_ADMISSION_REASON
            and self.preexisting_closed is None
        )
        return AdmissionView(
            state=self.admission_state,
            reason=self.admission_reason,
            owned=owned,
        )

    def open_admission_if_owned(self, *, reason: str) -> AdmissionView:
        self.events.append(f"open:{reason}")
        current = self.query_admission()
        if not current.owned or current.reason != reason:
            return current
        self.admission_state = "open"
        self.admission_reason = "ok"
        return self.query_admission()

    def isolate_writers(self) -> ProbeResult:
        self.events.append("isolate")
        if self.probe_status in {"timeout", "error"}:
            return ProbeResult(self.probe_status, self.probe_status)  # type: ignore[arg-type]
        if self.old_writer_present:
            return ProbeResult("present", "old writer")
        return ProbeResult("absent")

    def probe_writers(self) -> ProbeResult:
        self.events.append("probe_writers")
        if self.probe_status in {"timeout", "error"}:
            return ProbeResult(self.probe_status, self.probe_status)  # type: ignore[arg-type]
        if self.old_writer_present:
            return ProbeResult("present", "old writer")
        return ProbeResult("absent")

    def probe_in_flight_accepts(self) -> ProbeResult:
        self.events.append("probe_inflight")
        if self.inflight_present:
            return ProbeResult("present", "in-flight")
        return ProbeResult("absent")

    def trusted_writer_version(self, root: Path) -> int:
        _ = root
        return self.root_version


def _advance(
    redis: LuaRedis,
    executor: RecordingExecutor,
    *,
    binding: str = "rel-676",
    rollback: bool = False,
) -> object:
    return run_writer_cutover(
        redis=redis,
        executor=executor,
        release_binding=binding,
        root=Path("."),
        rollback=rollback,
    )


def test_mixed_old_and_new_writers_max_misses_independent_increments() -> None:
    redis = LuaRedis(1_778_200_000)
    _seed_v1(redis, 7, 100)
    _seed_v2(redis, 7, 100)
    assert LegacyMaxMergeWriter().consume(redis, app_id=7, weight=10, limit=200) == 1
    assert V1BucketWriter().consume(redis, app_id=7, weight=20, limit=200) == 1
    assert _v2_total(redis, 7) == 110
    assert _v1_total(redis, 7) == 120
    assert max(_v1_total(redis, 7), _v2_total(redis, 7)) == 120
    assert 100 + 10 + 20 == 130


def test_quota_100_accepts_110_when_old_and_new_writers_run_together() -> None:
    redis = LuaRedis(1_778_200_100)
    _seed_v1(redis, 7, 60)
    assert LegacyMaxMergeWriter().consume(redis, app_id=7, weight=20, limit=100) == 1
    assert V1BucketWriter().consume(redis, app_id=7, weight=30, limit=100) == 1
    assert _v2_total(redis, 7) == 80
    assert _v1_total(redis, 7) == 90
    assert 60 + 20 + 30 == 110


@pytest.mark.asyncio
async def test_cutover_cannot_activate_while_old_writer_is_present() -> None:
    redis = LuaRedis(1_778_200_200)
    executor = RecordingExecutor(old_writer_present=True)
    result = _advance(redis, executor)
    assert result.ok is False
    assert result.state == "preparing"
    assert "old writer" in result.error
    assert result.admission_state == "closed"
    assert result.opened is False
    marker = read_cutover_marker(redis)
    assert marker is not None and marker.state == "preparing"


@pytest.mark.asyncio
async def test_cutover_wait_starts_after_last_old_write_is_fenced() -> None:
    redis = LuaRedis(1_778_200_300)
    executor = RecordingExecutor(old_writer_present=True)
    first = _advance(redis, executor)
    assert first.ok is False
    assert read_cutover_marker(redis).fence_time is None  # type: ignore[union-attr]
    executor.old_writer_present = False
    redis.now_sec = 1_778_200_400
    second = _advance(redis, executor)
    marker = read_cutover_marker(redis)
    assert marker is not None
    assert marker.fence_time == 1_778_200_400
    assert marker.not_before == (
        1_778_200_400 + COST_WINDOW_SECONDS + CUTOVER_SAFETY_MARGIN_SECONDS
    )
    assert marker.state == "waiting_window"
    assert second.error == "waiting for not_before"


@pytest.mark.asyncio
async def test_cutover_uses_redis_time_and_full_window() -> None:
    redis = LuaRedis(5_000)
    executor = RecordingExecutor()
    waiting = _advance(redis, executor)
    marker = read_cutover_marker(redis)
    assert marker is not None
    assert waiting.error == "waiting for not_before"
    assert marker.not_before == 5_000 + CUTOVER_WINDOW + CUTOVER_SAFETY_MARGIN_SECONDS
    redis.now_sec = marker.not_before - 1
    still = _advance(redis, executor)
    assert read_cutover_marker(redis).state == "waiting_window"  # type: ignore[union-attr]
    assert still.state == "waiting_window"
    redis.now_sec = marker.not_before
    done = _advance(redis, executor)
    assert done.ok is True
    assert done.state == "active_v2"
    assert done.redis_time == marker.not_before


@pytest.mark.asyncio
async def test_unconfirmed_probe_or_redis_error_keeps_admission_closed() -> None:
    redis = LuaRedis(1_778_200_500)
    timeout = RecordingExecutor(probe_status="timeout")
    timed = _advance(redis, timeout)
    assert timed.ok is False
    assert timed.admission_state == "closed"
    assert timed.opened is False
    errored_exec = RecordingExecutor(probe_status="error")
    errored = _advance(redis, errored_exec)
    assert errored.ok is False
    assert errored.admission_state == "closed"
    broken = RecordingExecutor(redis_error=True)
    broken_result = _advance(redis, broken)
    assert broken_result.ok is False
    assert broken_result.opened is False


@pytest.mark.asyncio
async def test_duplicate_cutover_resumes_same_generation_without_reset() -> None:
    redis = LuaRedis(1_778_200_600)
    _seed_v2(redis, 7, 40)
    executor = RecordingExecutor()
    _advance(redis, executor)
    generation = read_cutover_marker(redis).generation  # type: ignore[union-attr]
    redis.now_sec = read_cutover_marker(redis).not_before  # type: ignore[union-attr]
    activated = _advance(redis, executor)
    assert activated.state == "active_v2"
    assert activated.generation == generation
    again = _advance(redis, executor)
    assert again.generation == generation
    assert again.state == "active_v2"
    assert _v2_total(redis, 7) == 40


@pytest.mark.asyncio
async def test_missing_or_conflicting_marker_does_not_auto_activate() -> None:
    redis = LuaRedis(1_778_200_700)
    limiter = ApplicationRateLimiter(AsyncLuaRedis(redis), nonce=lambda: "n")
    with pytest.raises(ControlPlaneUnavailable):
        await limiter.consume_send_cost(
            app_id=7,
            recipient_count=1,
            segment_count=1,
            recipient_limit=100,
            segment_limit=100,
        )
    assert WRITER_CUTOVER_MARKER_KEY not in redis.hashes
    redis.seed_hash(
        WRITER_CUTOVER_MARKER_KEY,
        {"state": "active", "generation": "1"},
    )
    with pytest.raises(ControlPlaneUnavailable):
        await limiter.consume_send_cost(
            app_id=7,
            recipient_count=1,
            segment_count=1,
            recipient_limit=100,
            segment_limit=100,
        )
    assert redis.hashes[WRITER_CUTOVER_MARKER_KEY]["state"] == "active"


def test_unsupported_old_binary_is_blocked_by_supported_launcher(
    tmp_path: Path,
) -> None:
    old_root = tmp_path / "old"
    old_root.mkdir()
    marker = parse_cutover_marker(_active_marker_fields())
    with pytest.raises(CutoverError, match="unsupported old writer"):
        check_supported_launch(root=old_root, marker=marker, environment="production")
    new_root = Path(__file__).resolve().parents[2]
    assert trusted_writer_version(new_root) == WRITER_PROTOCOL_VERSION
    check_supported_launch(root=new_root, marker=marker, environment="production")
    with pytest.raises(CutoverError, match="unsupported old writer"):
        check_supported_launch(root=old_root, marker=None, environment="production")
    check_supported_launch(root=new_root, marker=None, environment="production")


@pytest.mark.asyncio
async def test_rollback_after_v2_spend_requires_same_freeze_and_drain() -> None:
    redis = LuaRedis(1_778_200_800)
    executor = RecordingExecutor()
    _advance(redis, executor)
    redis.now_sec = read_cutover_marker(redis).not_before  # type: ignore[union-attr]
    assert _advance(redis, executor).state == "active_v2"
    _seed_v2(redis, 7, 70)
    blocked = _advance(redis, RecordingExecutor(old_writer_present=True), rollback=True)
    assert blocked.ok is False
    assert blocked.opened is False
    assert read_cutover_marker(redis).generation >= 1  # type: ignore[union-attr]
    redis.now_sec += 1
    waiting = _advance(redis, RecordingExecutor(), rollback=True)
    assert waiting.error == "waiting for not_before"
    redis.now_sec = read_cutover_marker(redis).not_before  # type: ignore[union-attr]
    finished = _advance(redis, RecordingExecutor(), rollback=True)
    marker = read_cutover_marker(redis)
    assert finished.ok is True
    assert marker is not None
    assert marker.state == "preparing"
    assert marker.minimum_writer_version == 1
    assert _v2_total(redis, 7) == 70


@pytest.mark.asyncio
async def test_cutover_does_not_clear_unrelated_closed_reason() -> None:
    redis = LuaRedis(1_778_200_900)
    executor = RecordingExecutor(preexisting_closed="outbox_backlog")
    _advance(redis, executor)
    redis.now_sec = read_cutover_marker(redis).not_before  # type: ignore[union-attr]
    done = _advance(redis, executor)
    assert done.ok is True
    assert done.opened is False
    assert done.admission_reason == "outbox_backlog"
    assert "open:writer_cutover" not in executor.events


@pytest.mark.asyncio
async def test_expired_v1_window_then_v2_has_only_one_budget() -> None:
    now = 1_778_201_000
    redis = LuaRedis(now)
    _seed_active_marker(redis)
    _, _, rec_v1, seg_v1 = _keys(7)
    redis.seed_hash(rec_v1, {str(now - 120): "90"})
    redis.seed_hash(seg_v1, {str(now - 120): "90"})
    limiter = ApplicationRateLimiter(AsyncLuaRedis(redis), nonce=lambda: "n")
    await limiter.consume_send_cost(
        app_id=7,
        recipient_count=100,
        segment_count=100,
        recipient_limit=100,
        segment_limit=100,
    )
    with pytest.raises(ApplicationRateLimitExceeded):
        await limiter.consume_send_cost(
            app_id=7,
            recipient_count=1,
            segment_count=1,
            recipient_limit=100,
            segment_limit=100,
        )
    assert _v2_total(redis, 7) == 100


def test_scheme_a_does_not_claim_max_merges_independent_increments() -> None:
    assert "max 不是混跑" in WEIGHTED_WINDOW_LUA
    assert "HSET', marker_key, 'state'" not in WEIGHTED_WINDOW_LUA


def test_compose_executor_probe_does_not_trust_tree_metadata() -> None:
    import sys

    root = Path(__file__).resolve().parents[2]
    scripts = str(root / "deploy" / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from writer_cutover import ComposeWriterExecutor

    class _Runner:
        def run(self, argv: list[str], *, timeout_s: int = 30) -> str:
            _ = timeout_s
            if "stop" in argv:
                return ""
            if "ps" in argv:
                return "api\n"
            raise AssertionError(argv)

    executor = ComposeWriterExecutor(
        runner=_Runner(),
        compose=("docker", "compose"),
        root=root,
    )
    assert executor.isolate_writers().status == "present"
    assert executor.probe_writers().status == "present"


def test_compose_executor_probe_timeout_is_not_absent() -> None:
    import sys

    root = Path(__file__).resolve().parents[2]
    scripts = str(root / "deploy" / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from writer_cutover import CommandError, ComposeWriterExecutor

    class _Runner:
        def run(self, argv: list[str], *, timeout_s: int = 30) -> str:
            _ = argv, timeout_s
            raise CommandError("timeout", "TimeoutExpired")

    executor = ComposeWriterExecutor(
        runner=_Runner(),
        compose=("docker", "compose"),
        root=root,
    )
    assert executor.isolate_writers().status == "timeout"
    assert executor.probe_writers().status == "timeout"
