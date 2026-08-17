"""厂商最终失败连续计数与关键错误即时告警。"""

from __future__ import annotations

from typing import Any, Protocol

from app.vendor.codes import policy_for

COUNTER_KEY = "alert:vendor:consecutive_failures"
COUNTER_TTL_S = 86_400
INCREMENT_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ARGV[1])
return count
"""


class AlertEmitter(Protocol):
    async def emit(
        self,
        *,
        alert_type: str,
        level: str,
        title: str,
        detail: dict[str, Any],
        dedup_key: str,
    ) -> None: ...


CRITICAL_EVENTS: dict[int, tuple[str, str]] = {
    999: (
        "balance_blocked",
        "厂商余额不足，发送队列已暂停；恢复入口：运维中心清除队列暂停后恢复批次",
    ),
    1000: (
        "vendor_auth_error",
        "厂商鉴权失败，发送队列已暂停；恢复入口：运维中心清除队列暂停",
    ),
    1010: ("vendor_ip_error", "厂商 IP 校验失败"),
}


class RedisVendorAlertMonitor:
    """Redis 只保存短期连续次数；告警事实仍由 alert_log 持久化。"""

    def __init__(self, redis: Any, alerts: AlertEmitter) -> None:
        self.redis = redis
        self.alerts = alerts

    async def record_failure(self, *, code: int, chunk_id: int, batch_id: int) -> None:
        count = int(
            await self.redis.eval(INCREMENT_SCRIPT, 1, COUNTER_KEY, COUNTER_TTL_S)
        )
        detail = {"vendor_code": code, "chunk_id": chunk_id, "batch_id": batch_id}
        policy = policy_for(code)
        named = CRITICAL_EVENTS.get(code)
        if named is not None:
            alert_type, title = named
            await self.alerts.emit(
                alert_type=alert_type,
                level="crit",
                title=title,
                detail=detail,
                dedup_key=f"{alert_type}:{code}",
            )
        elif policy.alert_level is not None:
            await self.alerts.emit(
                alert_type="vendor_critical_error",
                level=policy.alert_level,
                title=f"厂商关键错误：{policy.description}",
                detail=detail,
                dedup_key=f"vendor_critical_error:{code}",
            )
        if count >= 3:
            await self.alerts.emit(
                alert_type="vendor_consecutive_failure",
                level="crit",
                title="厂商接口连续失败",
                detail=detail | {"consecutive_failures": count},
                dedup_key="vendor_consecutive_failure",
            )

    async def record_success(self) -> None:
        await self.redis.delete(COUNTER_KEY)
