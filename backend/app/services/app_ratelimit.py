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

WEIGHTED_WINDOW_LUA = """
local function weighted_total(key)
  local members = redis.call('ZRANGE', key, 0, -1)
  local total = 0
  for i = 1, #members do
    local prefix = string.match(members[i], '^(%d+):')
    total = total + tonumber(prefix or '1')
  end
  return total
end
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[2])
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', ARGV[2])
local recipients = weighted_total(KEYS[1])
local segments = weighted_total(KEYS[2])
if recipients + tonumber(ARGV[4]) > tonumber(ARGV[3]) then return 0 end
if segments + tonumber(ARGV[6]) > tonumber(ARGV[5]) then return 0 end
redis.call('ZADD', KEYS[1], ARGV[1], ARGV[4] .. ':' .. ARGV[7])
redis.call('ZADD', KEYS[2], ARGV[1], ARGV[6] .. ':' .. ARGV[7])
redis.call('EXPIRE', KEYS[1], 60)
redis.call('EXPIRE', KEYS[2], 60)
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
        now_ms = int(self.clock().timestamp() * 1000)
        try:
            allowed = await self.redis.eval(
                WEIGHTED_WINDOW_LUA,
                2,
                f"ratelimit:app:{app_id}:recipients",
                f"ratelimit:app:{app_id}:segments",
                str(now_ms),
                str(now_ms - 60_000),
                str(recipient_limit),
                str(recipient_count),
                str(segment_limit),
                str(segment_count),
                self.nonce(),
            )
        except Exception as exc:
            raise ControlPlaneUnavailable("应用限流控制面不可用") from exc
        if not int(allowed):
            raise ApplicationRateLimitExceeded("应用请求频率超限")

    async def _consume_requests(self, *, key: str, limit_per_minute: int) -> None:
        if limit_per_minute < 1:
            raise ValueError("application rate limit must be positive")
        now_ms = int(self.clock().timestamp() * 1000)
        try:
            allowed = await self.redis.eval(
                SLIDING_WINDOW_LUA,
                1,
                key,
                str(now_ms),
                str(now_ms - 60_000),
                str(limit_per_minute),
                self.nonce(),
            )
        except Exception as exc:
            raise ControlPlaneUnavailable("应用限流控制面不可用") from exc
        if not int(allowed):
            raise ApplicationRateLimitExceeded("应用请求频率超限")
