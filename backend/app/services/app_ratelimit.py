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


class RateLimitRedis(Protocol):
    async def eval(self, *args: Any) -> Any: ...


class ApplicationRateLimitExceeded(RuntimeError):
    """应用每分钟受理次数已耗尽。"""


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
        if limit_per_minute < 1:
            raise ValueError("application rate limit must be positive")
        now_ms = int(self.clock().timestamp() * 1000)
        try:
            allowed = await self.redis.eval(
                SLIDING_WINDOW_LUA,
                1,
                f"ratelimit:app:{app_id}",
                str(now_ms),
                str(now_ms - 60_000),
                str(limit_per_minute),
                self.nonce(),
            )
        except Exception as exc:
            raise ControlPlaneUnavailable("应用限流控制面不可用") from exc
        if not int(allowed):
            raise ApplicationRateLimitExceeded("应用请求频率超限")
