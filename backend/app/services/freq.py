"""号码级频控的唯一 Redis Lua 实现。"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

HMAC_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SHANGHAI = ZoneInfo("Asia/Shanghai")
VERIFY_INCREMENT_LUA = """
if ARGV[3] == '1' and redis.call('GET', KEYS[3]) ~= ARGV[4] then
  return {-1, 0, 0}
end
local minute_count = false
local day_count = false
if ARGV[3] == '1' then
  minute_count = redis.call('HGET', KEYS[4], ARGV[5])
  day_count = redis.call('HGET', KEYS[5], ARGV[5])
end
if not minute_count then
  minute_count = redis.call('INCR', KEYS[1])
if minute_count == 1 then redis.call('PEXPIREAT', KEYS[1], ARGV[1]) end
  if ARGV[3] == '1' then
    local result_created = redis.call('EXISTS', KEYS[4]) == 0
    redis.call('HSET', KEYS[4], ARGV[5], minute_count)
    if result_created then redis.call('PEXPIREAT', KEYS[4], ARGV[1]) end
  end
end
if not day_count then
  day_count = redis.call('INCR', KEYS[2])
  if day_count == 1 then redis.call('PEXPIREAT', KEYS[2], ARGV[2]) end
  if ARGV[3] == '1' then
    local result_created = redis.call('EXISTS', KEYS[5]) == 0
    redis.call('HSET', KEYS[5], ARGV[5], day_count)
    if result_created then redis.call('PEXPIREAT', KEYS[5], ARGV[2]) end
  end
end
return {1, tonumber(minute_count), tonumber(day_count)}
"""

MARKET_INCREMENT_LUA = """
if ARGV[2] == '1' and redis.call('GET', KEYS[2]) ~= ARGV[3] then
  return {-1, 0}
end
local current = false
if ARGV[2] == '1' then
  current = redis.call('HGET', KEYS[3], ARGV[4])
end
if not current then
  current = redis.call('INCR', KEYS[1])
  if current == 1 then redis.call('PEXPIREAT', KEYS[1], ARGV[1]) end
  if ARGV[2] == '1' then
    local result_created = redis.call('EXISTS', KEYS[3]) == 0
    redis.call('HSET', KEYS[3], ARGV[4], current)
    if result_created then redis.call('PEXPIREAT', KEYS[3], ARGV[1]) end
  end
end
return {1, tonumber(current)}
"""


class RedisEval(Protocol):
    async def eval(self, *args: Any) -> Any: ...


class FrequencyFenceLost(RuntimeError):
    """幂等 claim 已失租，禁止继续写入号码频控。"""


@dataclass(frozen=True, slots=True)
class FrequencyLimits:
    verify_per_minute: int = 1
    verify_per_day: int = 10
    market_per_day: int = 1

    @classmethod
    def from_config(
        cls,
        *,
        verify_per_minute: int,
        verify_per_day: int,
        market_per_day: int,
        override: Mapping[str, int] | None,
    ) -> FrequencyLimits:
        values = {
            "verify_per_minute": verify_per_minute,
            "verify_per_day": verify_per_day,
            "market_per_day": market_per_day,
        }
        if override:
            values.update({key: value for key, value in override.items() if key in values})
        invalid = any(
            not isinstance(value, int) or isinstance(value, bool) or value < 1
            for value in values.values()
        )
        if invalid:
            raise ValueError("frequency limits must be positive integers")
        return cls(**values)


def utc_now() -> datetime:
    return datetime.now(UTC)


class FrequencyLimiter:
    """只用 phone_hmac 构造频控键，原子 INCR 并对齐时间边界。"""

    def __init__(
        self,
        redis: RedisEval,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.redis = redis
        self.clock = clock

    def _windows(self) -> tuple[int, int, str, str]:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("frequency clock must be timezone-aware")
        next_minute = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
        local = now.astimezone(SHANGHAI)
        next_day = (local + timedelta(days=1)).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        minute_expire_at_ms = int(next_minute.timestamp() * 1000)
        day_expire_at_ms = int(next_day.timestamp() * 1000)
        minute_window = str(int(now.timestamp() // 60))
        day_window = local.strftime("%Y%m%d")
        return minute_expire_at_ms, day_expire_at_ms, minute_window, day_window

    async def allow(
        self,
        category: str,
        *,
        app_id: int,
        phone_hmac: str,
        limits: FrequencyLimits,
        claim_key: str | None = None,
        claim_token: str | None = None,
        result_key: str | None = None,
    ) -> bool:
        if HMAC_PATTERN.fullmatch(phone_hmac) is None:
            raise ValueError("phone_hmac must be 64 lowercase hex characters")
        if (claim_key is None) != (claim_token is None):
            raise ValueError("claim_key and claim_token must be provided together")
        if (claim_key is None) != (result_key is None):
            raise ValueError("fenced frequency requires a result_key")
        if category == "notice":
            return True
        minute_ttl, day_ttl, minute_window, day_window = self._windows()
        fenced = claim_key is not None
        if category == "verify":
            keys = [f"freq:v:{phone_hmac}:m", f"freq:v:{phone_hmac}:d"]
            if claim_key is not None:
                keys.extend(
                    (
                        claim_key,
                        f"{result_key}:verify:m:{minute_window}",
                        f"{result_key}:verify:d:{day_window}",
                    )
                )
            raw = await self.redis.eval(
                VERIFY_INCREMENT_LUA,
                len(keys),
                *keys,
                str(minute_ttl),
                str(day_ttl),
                "1" if fenced else "0",
                claim_token or "",
                phone_hmac if fenced else "",
            )
            status, minute_count, day_count = (int(item) for item in raw)
            if status == -1:
                raise FrequencyFenceLost("idempotency claim lost before frequency write")
            return (
                minute_count <= limits.verify_per_minute
                and day_count <= limits.verify_per_day
            )
        if category == "market":
            keys = [f"freq:m:{app_id}:{phone_hmac}:d"]
            if claim_key is not None:
                keys.extend((claim_key, f"{result_key}:market:d:{day_window}"))
            raw = await self.redis.eval(
                MARKET_INCREMENT_LUA,
                len(keys),
                *keys,
                str(day_ttl),
                "1" if fenced else "0",
                claim_token or "",
                phone_hmac if fenced else "",
            )
            status, count = (int(item) for item in raw)
            if status == -1:
                raise FrequencyFenceLost("idempotency claim lost before frequency write")
            return count <= limits.market_per_day
        raise ValueError("unsupported category")
