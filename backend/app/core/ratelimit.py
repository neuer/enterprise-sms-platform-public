"""厂商全局双 lane 令牌桶的唯一 Redis Lua 实现。"""

from __future__ import annotations

from typing import Any, Literal, Protocol

TOKEN_BUCKET_LUA = """
local lane = ARGV[1]
local capacity = tonumber(ARGV[2])
local reserved = tonumber(ARGV[3])
local clock = redis.call('TIME')
local now_ms = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local kind = redis.call('TYPE', KEYS[1]).ok
if kind ~= 'none' and kind ~= 'hash' then return -2 end
local tokens = tonumber(redis.call('HGET', KEYS[1], 'tokens'))
local last_ms = tonumber(redis.call('HGET', KEYS[1], 'last_ms'))
if kind == 'none' then
  tokens = capacity
  last_ms = now_ms
elseif tokens == nil or last_ms == nil or tokens < 0 or last_ms < 0
    or tokens ~= math.floor(tokens) or last_ms ~= math.floor(last_ms)
    or tokens > 9007199254740991 or last_ms > 9007199254740000 then
  return -2
elseif last_ms > now_ms then
  -- 时钟回退只冻结预算；保留高水位，恢复后不得再补同一周期。
  tokens = 0
else
  local elapsed_seconds = math.floor((now_ms - last_ms) / 1000)
  if elapsed_seconds > 0 then
    tokens = math.min(capacity, tokens + elapsed_seconds * capacity)
    last_ms = last_ms + elapsed_seconds * 1000
  end
end
tokens = math.min(capacity, tokens)
local allowed = 0
if lane == 'realtime' and tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
elseif lane == 'bulk' and tokens > reserved then
  tokens = tokens - 1
  allowed = 1
end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'last_ms', last_ms)
redis.call('PEXPIRE', KEYS[1], math.max(60000, last_ms - now_ms + 1000))
if allowed == 1 then
  return last_ms
end
return -1
"""

TOKEN_REFUND_LUA = """
local capacity = tonumber(ARGV[1])
local lease_epoch = tonumber(ARGV[2])
local clock = redis.call('TIME')
local now_ms = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local tokens = tonumber(redis.call('HGET', KEYS[1], 'tokens'))
local last_ms = tonumber(redis.call('HGET', KEYS[1], 'last_ms'))
if tokens == nil or last_ms == nil or last_ms ~= lease_epoch then
  return 0
end
if now_ms < last_ms or now_ms >= last_ms + 1000
    or tokens < 0 or tokens ~= math.floor(tokens) then return 0 end
tokens = math.min(capacity, tokens + 1)
redis.call('HSET', KEYS[1], 'tokens', tokens, 'last_ms', last_ms)
redis.call('PEXPIRE', KEYS[1], 60000)
return 1
"""


class RedisEval(Protocol):
    async def eval(self, *args: Any) -> Any: ...


class TokenBucket:
    """容量 vendor_qps，每整秒补满；bulk 尊重 realtime 预留。

    补桶使用 control Redis 的 TIME，时间高水位不回退；成功时返回 last_ms 作为 lease。
    """

    def __init__(self, redis: RedisEval, *, key: str = "ratelimit:vendor") -> None:
        self.redis = redis
        self.key = key

    def _bucket_key(self, vendor_id: str | None) -> str:
        if vendor_id:
            return f"{self.key}:{vendor_id}"
        return self.key

    async def acquire(
        self,
        *,
        lane: Literal["realtime", "bulk"] | str,
        vendor_qps: int,
        reserved_realtime_qps: int,
        vendor_id: str | None = None,
    ) -> int | None:
        if lane not in {"realtime", "bulk"}:
            raise ValueError("lane must be realtime or bulk")
        if vendor_qps < 1 or not 0 <= reserved_realtime_qps < vendor_qps:
            raise ValueError("invalid vendor QPS reservation")
        result = await self.redis.eval(
            TOKEN_BUCKET_LUA,
            1,
            self._bucket_key(vendor_id),
            lane,
            str(vendor_qps),
            str(reserved_realtime_qps),
        )
        lease_epoch = int(result)
        if lease_epoch < -1:
            raise RuntimeError("vendor token bucket state unavailable")
        return None if lease_epoch < 0 else lease_epoch

    async def refund(
        self,
        *,
        vendor_qps: int,
        lease_epoch: int,
        vendor_id: str | None = None,
    ) -> None:
        """原子返还一个未使用令牌，且永不超过桶容量。"""

        if vendor_qps < 1:
            raise ValueError("vendor_qps must be positive")
        await self.redis.eval(
            TOKEN_REFUND_LUA,
            1,
            self._bucket_key(vendor_id),
            str(vendor_qps),
            str(lease_epoch),
        )
