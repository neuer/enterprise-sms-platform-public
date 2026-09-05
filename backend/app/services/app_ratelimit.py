"""应用维度每分钟滑动窗口受理限流。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

SLIDING_WINDOW_LUA = """
local t = redis.call('TIME')
local now_ms = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now_ms - 60000)
local count = redis.call('ZCARD', KEYS[1])
if count >= tonumber(ARGV[1]) then return 0 end
redis.call('ZADD', KEYS[1], now_ms, ARGV[2])
redis.call('EXPIRE', KEYS[1], 60)
return 1
"""

COST_WINDOW_SECONDS = 60
COST_BUCKET_TTL_SECONDS = 70
COST_MIGRATION_TTL_SECONDS = 604800
WEIGHTED_WINDOW_LUA = """
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
  for epoch = now_sec + 1, now_sec + 59 do
    local raw = redis.call('HGET', key, tostring(epoch))
    if raw ~= false and raw ~= nil then
      return nil
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


class RateLimitRedis(Protocol):
    async def eval(self, *args: Any) -> Any: ...


class ApplicationRateLimitExceeded(RuntimeError):
    """应用每分钟受理次数或成本额度已耗尽。"""


class ControlPlaneUnavailable(RuntimeError):
    """control Redis 不可用，受理必须失败关闭。"""


def utc_now() -> datetime:
    return datetime.now(UTC)


class ApplicationRateLimiter:
    def __init__(
        self,
        redis: RateLimitRedis,
        *,
        clock: Callable[[], datetime] = utc_now,
        nonce: Callable[[], str] = lambda: uuid4().hex,
    ) -> None:
        self.redis = redis
        self.clock = clock
        self.nonce = nonce

    async def check(self, *, app_id: int, limit_per_minute: int) -> None:
        await self._consume_requests(
            key=f"ratelimit:app:{app_id}",
            limit_per_minute=limit_per_minute,
        )

    async def check_replay(self, *, app_id: int, limit_per_minute: int) -> None:
        """幂等重放走独立 read bucket，不消耗新发送额度。"""

        await self._consume_requests(
            key=f"ratelimit:app:{app_id}:replay",
            limit_per_minute=limit_per_minute,
        )

    async def consume_send_cost(
        self,
        *,
        app_id: int,
        recipient_count: int,
        segment_count: int,
        recipient_limit: int,
        segment_limit: int,
    ) -> None:
        """按收件人与预计分段计入分钟额度；1×10000 与 100×100 等价。

        迁移窗口内同时读 v1 `:buckets` 与 v2 环形槽，按 max 合并后只写 v2，
        避免空 v2 被当成零而重置活动窗口额度。
        """

        if (
            recipient_count < 1
            or segment_count < 1
            or recipient_limit < 1
            or segment_limit < 1
        ):
            raise ValueError("application cost limits must be positive")
        try:
            allowed = await self.redis.eval(
                WEIGHTED_WINDOW_LUA,
                5,
                f"ratelimit:app:{app_id}:recipients:v2",
                f"ratelimit:app:{app_id}:segments:v2",
                f"ratelimit:app:{app_id}:recipients:buckets",
                f"ratelimit:app:{app_id}:segments:buckets",
                f"ratelimit:app:{app_id}:cost:mig",
                str(recipient_limit),
                str(recipient_count),
                str(segment_limit),
                str(segment_count),
                str(COST_BUCKET_TTL_SECONDS),
                str(COST_MIGRATION_TTL_SECONDS),
            )
        except Exception as exc:
            raise ControlPlaneUnavailable("应用限流控制面不可用") from exc
        try:
            outcome = int(allowed)
        except (TypeError, ValueError) as exc:
            raise ControlPlaneUnavailable("应用限流控制面不可用") from exc
        if outcome < 0:
            raise ControlPlaneUnavailable("应用限流控制面不可用")
        if outcome == 0:
            raise ApplicationRateLimitExceeded("应用请求频率超限")

    async def _consume_requests(self, *, key: str, limit_per_minute: int) -> None:
        if limit_per_minute < 1:
            raise ValueError("application rate limit must be positive")
        member = self.nonce()
        try:
            allowed = await self.redis.eval(
                SLIDING_WINDOW_LUA,
                1,
                key,
                str(limit_per_minute),
                member,
            )
        except Exception as exc:
            raise ControlPlaneUnavailable("应用限流控制面不可用") from exc
        if not int(allowed):
            raise ApplicationRateLimitExceeded("应用请求频率超限")
