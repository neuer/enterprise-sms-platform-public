"""厂商全局双 lane 令牌桶的唯一 Redis Lua 实现。"""

from __future__ import annotations

from time import time
from typing import Any, Literal, Protocol

TOKEN_BUCKET_LUA = """
local lane = ARGV[1]
local capacity = tonumber(ARGV[2])
local reserved = tonumber(ARGV[3])
local now_ms = tonumber(ARGV[4])
local tokens = tonumber(redis.call('HGET', KEYS[1], 'tokens'))
local last_ms = tonumber(redis.call('HGET', KEYS[1], 'last_ms'))
if tokens == nil or last_ms == nil then
  tokens = capacity
  last_ms = now_ms
else
  local elapsed_seconds = math.floor((now_ms - last_ms) / 1000)
  if elapsed_seconds > 0 then
    tokens = math.min(capacity, tokens + elapsed_seconds * capacity)
    last_ms = last_ms + elapsed_seconds * 1000
  end
end
local allowed = 0
if lane == 'realtime' and tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
elseif lane == 'bulk' and tokens > reserved then
  tokens = tokens - 1
  allowed = 1
end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'last_ms', last_ms)
redis.call('PEXPIRE', KEYS[1], 60000)
if allowed == 1 then
  return last_ms
end
return -1
"""

TOKEN_REFUND_LUA = """
local capacity = tonumber(ARGV[1])
local lease_epoch = tonumber(ARGV[2])
local tokens = tonumber(redis.call('HGET', KEYS[1], 'tokens'))
local last_ms = tonumber(redis.call('HGET', KEYS[1], 'last_ms'))
if tokens == nil or last_ms == nil or last_ms ~= lease_epoch then
  return 0
end
tokens = math.min(capacity, tokens + 1)
redis.call('HSET', KEYS[1], 'tokens', tokens, 'last_ms', last_ms)
redis.call('PEXPIRE', KEYS[1], 60000)
return 1
"""


class RedisEval(Protocol):
    async def eval(self, *args: Any) -> Any: ...


class TokenBucket:
    """容量 vendor_qps，每整秒补满；bulk 尊重 realtime 预留。

    取令牌只使用 control Redis ACL 已放行的 HGET/HSET；成功时返回 last_ms 作为 lease。
    """

    def __init__(self, redis: RedisEval, *, key: str = "ratelimit:vendor") -> None:
        self.redis = redis
        self.key = key

    async def acquire(
        self,
        *,
        lane: Literal["realtime", "bulk"] | str,
        vendor_qps: int,
        reserved_realtime_qps: int,
        now_ms: int | None = None,
    ) -> int | None:
        if lane not in {"realtime", "bulk"}:
            raise ValueError("lane must be realtime or bulk")
        if vendor_qps < 1 or not 0 <= reserved_realtime_qps < vendor_qps:
            raise ValueError("invalid vendor QPS reservation")
        timestamp = int(time() * 1000) if now_ms is None else now_ms
        result = await self.redis.eval(
            TOKEN_BUCKET_LUA,
            1,
            self.key,
            lane,
            str(vendor_qps),
            str(reserved_realtime_qps),
            str(timestamp),
        )
        lease_epoch = int(result)
        return None if lease_epoch < 0 else lease_epoch

    async def refund(self, *, vendor_qps: int, lease_epoch: int) -> None:
        """原子返还一个未使用令牌，且永不超过桶容量。"""

        if vendor_qps < 1:
            raise ValueError("vendor_qps must be positive")
        await self.redis.eval(
            TOKEN_REFUND_LUA,
            1,
            self.key,
            str(vendor_qps),
            str(lease_epoch),
        )
