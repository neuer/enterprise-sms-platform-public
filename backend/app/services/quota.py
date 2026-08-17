"""应用与部门配额 Redis 投影辅助；事实账本见 usage_ledger。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol

RESERVE_LUA = """
if ARGV[5] == '1' and redis.call('GET', KEYS[4]) ~= ARGV[6] then
  return {-1, 0, 0}
end
local app_used = tonumber(redis.call('GET', KEYS[1]) or '0')
local dept_used = tonumber(redis.call('GET', KEYS[2]) or '0')
local cost = tonumber(ARGV[1])
if ARGV[5] == '1' then
  local reserved_fingerprint = redis.call('GET', KEYS[5])
  if reserved_fingerprint then
    if reserved_fingerprint == ARGV[7] then return {2, app_used, dept_used} end
    return {-2, app_used, dept_used}
  end
end
local app_limit = tonumber(ARGV[2])
local dept_limit = tonumber(ARGV[3])
if (app_limit > 0 and app_used + cost > app_limit) or
   (dept_limit > 0 and dept_used + cost > dept_limit) then
  return {0, app_used, dept_used}
end
app_used = redis.call('INCRBY', KEYS[1], cost)
dept_used = redis.call('INCRBY', KEYS[2], cost)
local volume_used = redis.call('INCRBY', KEYS[3], cost)
if app_used == cost then redis.call('EXPIRE', KEYS[1], ARGV[4]) end
if dept_used == cost then redis.call('EXPIRE', KEYS[2], ARGV[4]) end
if volume_used == cost then redis.call('EXPIRE', KEYS[3], ARGV[4]) end
if ARGV[5] == '1' then redis.call('SET', KEYS[5], ARGV[7], 'EX', ARGV[4]) end
return {1, app_used, dept_used}
"""

REFUND_RESERVATION_LUA = """
local cost = tonumber(ARGV[1])
local reserved_fingerprint = redis.call('GET', KEYS[4])
local app_used = tonumber(redis.call('GET', KEYS[1]) or '0')
local dept_used = tonumber(redis.call('GET', KEYS[2]) or '0')
if not reserved_fingerprint then return {0, app_used, dept_used} end
if reserved_fingerprint ~= ARGV[2] then return {-1, app_used, dept_used} end
if redis.call('EXISTS', KEYS[1]) == 1 then
  app_used = math.max(0, app_used - cost)
  redis.call('SET', KEYS[1], app_used, 'KEEPTTL')
end
if redis.call('EXISTS', KEYS[2]) == 1 then
  dept_used = math.max(0, dept_used - cost)
  redis.call('SET', KEYS[2], dept_used, 'KEEPTTL')
end
if redis.call('EXISTS', KEYS[3]) == 1 then
  local volume_used = math.max(0, tonumber(redis.call('GET', KEYS[3])) - cost)
  redis.call('SET', KEYS[3], volume_used, 'KEEPTTL')
end
redis.call('DEL', KEYS[4])
return {1, app_used, dept_used}
"""

REFUND_LUA = """
local cost = tonumber(ARGV[1])
local app_used = 0
local dept_used = 0
if redis.call('EXISTS', KEYS[1]) == 1 then
  app_used = math.max(0, tonumber(redis.call('GET', KEYS[1])) - cost)
  redis.call('SET', KEYS[1], app_used, 'KEEPTTL')
end
if redis.call('EXISTS', KEYS[2]) == 1 then
  dept_used = math.max(0, tonumber(redis.call('GET', KEYS[2])) - cost)
  redis.call('SET', KEYS[2], dept_used, 'KEEPTTL')
end
if redis.call('EXISTS', KEYS[3]) == 1 then
  local volume_used = math.max(0, tonumber(redis.call('GET', KEYS[3])) - cost)
  redis.call('SET', KEYS[3], volume_used, 'KEEPTTL')
end
return {app_used, dept_used}
"""

REFUND_ONCE_LUA = """
if not redis.call('SET', KEYS[4], '1', 'NX', 'EX', ARGV[2]) then
  return {0, tonumber(redis.call('GET', KEYS[1]) or '0'),
             tonumber(redis.call('GET', KEYS[2]) or '0')}
end
local cost = tonumber(ARGV[1])
local app_used = 0
local dept_used = 0
if redis.call('EXISTS', KEYS[1]) == 1 then
  app_used = math.max(0, tonumber(redis.call('GET', KEYS[1])) - cost)
  redis.call('SET', KEYS[1], app_used, 'KEEPTTL')
end
if redis.call('EXISTS', KEYS[2]) == 1 then
  dept_used = math.max(0, tonumber(redis.call('GET', KEYS[2])) - cost)
  redis.call('SET', KEYS[2], dept_used, 'KEEPTTL')
end
if redis.call('EXISTS', KEYS[3]) == 1 then
  local volume_used = math.max(0, tonumber(redis.call('GET', KEYS[3])) - cost)
  redis.call('SET', KEYS[3], volume_used, 'KEEPTTL')
end
return {1, app_used, dept_used}
"""


class QuotaRedis(Protocol):
    async def eval(self, *args: Any) -> Any: ...


class QuotaExceeded(RuntimeError):
    """应用或部门日配额不足，对应 QUOTA_EXCEEDED/429。"""


class QuotaFenceLost(RuntimeError):
    """幂等 claim 已失租，禁止继续预扣配额。"""


class QuotaReservationConflict(RuntimeError):
    """同一幂等请求重复计算出不同配额成本，必须 fail-closed。"""


@dataclass(frozen=True, slots=True)
class QuotaUsage:
    app_used: int
    dept_used: int
    reused: bool = False


@dataclass(frozen=True, slots=True)
class QuotaRelease:
    released: bool
    usage: QuotaUsage


class QuotaService:
    """单个 Lua 同时操作应用与部门键，禁止部分预扣。"""

    def __init__(self, redis: QuotaRedis) -> None:
        self.redis = redis

    @staticmethod
    def _reservation_fingerprint(dept: str, category: str, cost: int) -> str:
        canonical = json.dumps(
            [dept, category, cost], ensure_ascii=False, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _keys(
        app_id: int,
        dept: str,
        category: str,
        date_key: str,
    ) -> tuple[str, str, str]:
        if category not in {"verify", "notice", "market"}:
            raise ValueError("invalid quota category")
        return (
            f"quota:app:{app_id}:{date_key}",
            f"quota:dept:{dept}:{date_key}",
            f"quota:volume:app:{app_id}:{category}:{date_key}",
        )

    async def reserve(
        self,
        *,
        app_id: int,
        dept: str,
        category: str,
        date_key: str,
        cost: int,
        app_limit: int,
        dept_limit: int,
        ttl_s: int,
        claim_key: str | None = None,
        claim_token: str | None = None,
        reservation_key: str | None = None,
    ) -> QuotaUsage:
        if cost < 0 or app_limit < 0 or dept_limit < 0 or ttl_s < 1:
            raise ValueError("invalid quota reservation")
        if (claim_key is None) != (claim_token is None):
            raise ValueError("claim_key and claim_token must be provided together")
        if (claim_key is None) != (reservation_key is None):
            raise ValueError("fenced quota requires a reservation_key")
        keys = self._keys(app_id, dept, category, date_key)
        fingerprint = self._reservation_fingerprint(dept, category, cost)
        eval_keys = (
            (*keys, claim_key, reservation_key) if claim_key is not None else keys
        )
        raw = await self.redis.eval(
            RESERVE_LUA,
            len(eval_keys),
            *eval_keys,
            str(cost),
            str(app_limit),
            str(dept_limit),
            str(ttl_s),
            "1" if claim_key is not None else "0",
            claim_token or "",
            fingerprint if claim_key is not None else "",
        )
        allowed, app_used, dept_used = (int(item) for item in raw)
        if allowed == -1:
            raise QuotaFenceLost("idempotency claim lost before quota write")
        if allowed == -2:
            raise QuotaReservationConflict("idempotent quota cost changed")
        if not allowed:
            raise QuotaExceeded("日配额不足")
        return QuotaUsage(app_used, dept_used, reused=allowed == 2)

    async def refund_reservation(
        self,
        *,
        app_id: int,
        dept: str,
        category: str,
        date_key: str,
        cost: int,
        reservation_key: str,
    ) -> QuotaRelease:
        if cost < 0 or not reservation_key:
            raise ValueError("invalid quota reservation refund")
        keys = self._keys(app_id, dept, category, date_key)
        fingerprint = self._reservation_fingerprint(dept, category, cost)
        raw = await self.redis.eval(
            REFUND_RESERVATION_LUA,
            4,
            *keys,
            reservation_key,
            str(cost),
            fingerprint,
        )
        released, app_used, dept_used = (int(item) for item in raw)
        if released == -1:
            raise QuotaReservationConflict("idempotent quota refund cost changed")
        return QuotaRelease(bool(released), QuotaUsage(app_used, dept_used))

    async def refund(
        self,
        *,
        app_id: int,
        dept: str,
        category: str,
        date_key: str,
        cost: int,
    ) -> QuotaUsage:
        if cost < 0:
            raise ValueError("refund cost must be non-negative")
        keys = self._keys(app_id, dept, category, date_key)
        raw = await self.redis.eval(REFUND_LUA, 3, *keys, str(cost))
        app_used, dept_used = (int(item) for item in raw)
        return QuotaUsage(app_used, dept_used)

    async def refund_once(
        self,
        *,
        app_id: int,
        dept: str,
        category: str,
        date_key: str,
        cost: int,
        event_id: str,
        marker_ttl_s: int,
    ) -> QuotaRelease:
        """驳回/过期/取消等事件使用稳定 event_id，重复执行不会重复回补。"""

        if cost < 0 or not event_id or marker_ttl_s < 1:
            raise ValueError("invalid idempotent quota refund")
        app_key, dept_key, volume_key = self._keys(app_id, dept, category, date_key)
        marker_key = f"quota:refund:{event_id}"
        raw = await self.redis.eval(
            REFUND_ONCE_LUA,
            4,
            app_key,
            dept_key,
            volume_key,
            marker_key,
            str(cost),
            str(marker_ttl_s),
        )
        released, app_used, dept_used = (int(item) for item in raw)
        return QuotaRelease(bool(released), QuotaUsage(app_used, dept_used))
