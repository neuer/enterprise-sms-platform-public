"""历史配额补偿的幂等回补；现役配额事实账本见 usage_ledger。"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.redis_eval import RedisEval

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

QuotaRedis = RedisEval


class QuotaExceeded(RuntimeError):
    """应用或部门日配额不足，对应 QUOTA_EXCEEDED/429。"""


@dataclass(frozen=True, slots=True)
class QuotaUsage:
    app_used: int
    dept_used: int


@dataclass(frozen=True, slots=True)
class QuotaRelease:
    released: bool
    usage: QuotaUsage


class QuotaService:
    """历史补偿事件以单个 Lua 回补应用与部门额度，重复消费不重复回补。"""

    def __init__(self, redis: QuotaRedis) -> None:
        self.redis = redis

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
