"""应用维度每分钟滑动窗口受理限流。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

SLIDING_WINDOW_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[2])
local count = redis.call('ZCARD', KEYS[1])
if count >= tonumber(ARGV[3]) then return 0 end
redis.call('ZADD', KEYS[1], ARGV[1], ARGV[4])
redis.call('EXPIRE', KEYS[1], 60)
return 1
"""

COST_WINDOW_SECONDS = 60
COST_BUCKET_TTL_SECONDS = 70
WEIGHTED_WINDOW_LUA = """
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
        """按收件人与预计分段计入分钟额度；1×10000 与 100×100 等价。"""

        if (
            recipient_count < 1
            or segment_count < 1
            or recipient_limit < 1
            or segment_limit < 1
        ):
            raise ValueError("application cost limits must be positive")
        now_sec = int(self.clock().timestamp())
        try:
            allowed = await self.redis.eval(
                WEIGHTED_WINDOW_LUA,
                2,
                f"ratelimit:app:{app_id}:recipients:buckets",
                f"ratelimit:app:{app_id}:segments:buckets",
                str(now_sec),
                str(COST_WINDOW_SECONDS),
                str(recipient_limit),
                str(recipient_count),
                str(segment_limit),
                str(segment_count),
                str(COST_BUCKET_TTL_SECONDS),
            )
        except Exception as exc:
            raise ControlPlaneUnavailable("应用限流控制面不可用") from exc
        if not int(allowed):
            raise ApplicationRateLimitExceeded("应用请求频率超限")

    async def _consume_requests(self, *, key: str, limit_per_minute: int) -> None:
        if limit_per_minute < 1:
            raise ValueError("application rate limit must be positive")
        now_ms = int(self.clock().timestamp() * 1000)
        member = self.nonce()
        try:
            allowed = await self.redis.eval(
                SLIDING_WINDOW_LUA,
                1,
                key,
                str(now_ms),
                str(now_ms - 60_000),
                str(limit_per_minute),
                member,
            )
        except Exception as exc:
            raise ControlPlaneUnavailable("应用限流控制面不可用") from exc
        if not int(allowed):
            raise ApplicationRateLimitExceeded("应用请求频率超限")
