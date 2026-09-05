"""发送准入快照：复用当前告警同一组 Outbox/uncertain/callback 与暂停键。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from sqlalchemy import text

from app.core.runtime_resources import database_engine, redis_client
from app.services.send_admission import (
    SendAdmissionFacts,
    SendAdmissionLimits,
    SendAdmissionUnavailable,
)
from app.settings import Settings, get_settings

RECOVERY_BUDGET_LUA = """
local t = redis.call('TIME')
local sec = tostring(t[1])
local bk = KEYS[1] .. ':' .. sec .. ':b'
local rk = KEYS[1] .. ':' .. sec .. ':r'
local sk = KEYS[1] .. ':' .. sec .. ':s'
local b = tonumber(redis.call('INCRBY', bk, ARGV[1]))
local r = tonumber(redis.call('INCRBY', rk, ARGV[2]))
local s = tonumber(redis.call('INCRBY', sk, ARGV[3]))
redis.call('EXPIRE', bk, 3)
redis.call('EXPIRE', rk, 3)
redis.call('EXPIRE', sk, 3)
if b > tonumber(ARGV[4]) or r > tonumber(ARGV[5]) or s > tonumber(ARGV[6]) then
  return 0
end
return 1
"""


class AdmissionControlConflict(RuntimeError):
    """CAS 败者读到新鲜胜者；不等于控制面不可用。"""

    def __init__(self, winner: dict[str, Any]) -> None:
        self.winner = winner
        super().__init__("admission control conflict")


class SqlSendAdmissionRepository:
    """只读积压聚合 + 状态变化写入 alert_log；不读手机号、正文或 broker。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        redis: Any | None = None,
        database_timeout_s: float = 2.0,
        control_timeout_s: float = 1.0,
    ) -> None:
        self.settings = settings or get_settings()
        self.redis = redis if redis is not None else redis_client(
            self.settings.redis_control_url
        )
        self.database_timeout_s = database_timeout_s
        self.control_timeout_s = control_timeout_s

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url, component="api")

    async def load(self) -> SendAdmissionFacts:
        """同一组 SQL/Redis 键与 current-alerts 对齐；uncertain 逾期固定 24 小时。"""

        engine = self._engine()
        async with asyncio.timeout(self.database_timeout_s), engine.connect() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT
                      (SELECT count(*) FROM outbox_event
                       WHERE state IN ('pending','leased','published','processing'))
                        outbox_active,
                      (SELECT EXTRACT(EPOCH FROM (now()-min(created_at)))
                       FROM outbox_event
                       WHERE state IN ('pending','leased','published','processing'))
                        outbox_oldest_age_s,
                      (SELECT count(*) FROM outbox_event
                       WHERE state='dead') outbox_dead,
                      (SELECT count(*) FROM sms_chunk
                       WHERE status='uncertain'
                         AND uncertain_since <= now()-interval '24 hours')
                        uncertain_overdue,
                      (SELECT count(*) FROM callback_task
                       WHERE status='dead') callback_dead,
                      (SELECT NOT EXISTS (
                         SELECT 1 FROM send_runtime_heartbeat h
                         WHERE h.component='send-realtime'
                           AND h.lease_until >= now()
                       )) realtime_heartbeat_stale,
                      (SELECT NOT EXISTS (
                         SELECT 1 FROM send_runtime_heartbeat h
                         WHERE h.component='send-bulk'
                           AND h.lease_until >= now()
                       )) bulk_heartbeat_stale,
                      (SELECT NOT EXISTS (
                         SELECT 1 FROM send_runtime_heartbeat h
                         WHERE h.component='outbox-dispatcher'
                           AND h.lease_until >= now()
                       )) dispatcher_heartbeat_stale
                    """
                )
            )
            row = result.mappings().one()

        async with asyncio.timeout(self.control_timeout_s):
            values = await self.redis.mget(
                "queue:paused:realtime",
                "queue:paused:bulk",
                "alert:vendor:consecutive_failures",
            )
        try:
            failures = int(values[2]) if values[2] is not None else 0
        except (TypeError, ValueError):
            raise ValueError("vendor failure counter is invalid") from None
        if failures < 0:
            raise ValueError("vendor failure counter is invalid")
        oldest = row["outbox_oldest_age_s"]
        enforce_heartbeat = bool(getattr(self.settings, "is_production", False))
        return SendAdmissionFacts(
            outbox_active=max(0, int(row["outbox_active"] or 0)),
            outbox_oldest_age_s=max(0, int(oldest or 0)),
            outbox_dead=max(0, int(row["outbox_dead"] or 0)),
            uncertain_overdue=max(0, int(row["uncertain_overdue"] or 0)),
            callback_dead=max(0, int(row["callback_dead"] or 0)),
            realtime_paused=bool(values[0]),
            bulk_paused=bool(values[1]),
            vendor_failures=failures,
            realtime_heartbeat_stale=(
                enforce_heartbeat and int(row["realtime_heartbeat_stale"] or 0) > 0
            ),
            bulk_heartbeat_stale=(
                enforce_heartbeat and int(row["bulk_heartbeat_stale"] or 0) > 0
            ),
            dispatcher_heartbeat_stale=(
                enforce_heartbeat and int(row["dispatcher_heartbeat_stale"] or 0) > 0
            ),
        )

    async def load_control_state(self) -> dict[str, Any] | None:
        engine = self._engine()
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT state, reason_code, state_epoch, hold_until,
                               valid_until, now() AS db_now
                        FROM send_admission_state WHERE scope='send'
                        """
                    )
                )
            ).mappings().one_or_none()
        return dict(row) if row is not None else None

    async def save_control_state(
        self,
        *,
        state: str,
        reason: str,
        hold_until: Any,
        epoch: int,
    ) -> dict[str, Any]:
        if state == "open" and hold_until is not None:
            raise ValueError("open admission state cannot carry a recovery hold")
        if reason == "recovery_hold" and state != "degraded":
            raise ValueError("recovery_hold must be a degraded admission state")
        engine = self._engine()
        async with engine.begin() as connection:
            updated = await connection.execute(
                text(
                    """
                    INSERT INTO send_admission_state (
                      scope, state, reason_code, state_epoch, hold_until,
                      valid_until, observed_at, updated_at
                    ) VALUES (
                      'send', :state, :reason, :epoch, :hold_until,
                      now() + interval '15 seconds', now(), now()
                    )
                    ON CONFLICT (scope) DO UPDATE SET
                      state=EXCLUDED.state,
                      reason_code=EXCLUDED.reason_code,
                      state_epoch=CASE
                        WHEN send_admission_state.state IS DISTINCT FROM EXCLUDED.state
                          OR send_admission_state.reason_code
                             IS DISTINCT FROM EXCLUDED.reason_code
                          OR send_admission_state.hold_until
                             IS DISTINCT FROM EXCLUDED.hold_until
                        THEN send_admission_state.state_epoch + 1
                        ELSE send_admission_state.state_epoch
                      END,
                      hold_until=EXCLUDED.hold_until,
                      valid_until=EXCLUDED.valid_until,
                      observed_at=now(),
                      updated_at=now()
                    WHERE (
                      send_admission_state.valid_until < now()
                      OR send_admission_state.state_epoch = :epoch
                    )
                    AND (
                      send_admission_state.valid_until < now()
                      OR (
                        send_admission_state.state
                          IS NOT DISTINCT FROM EXCLUDED.state
                        AND send_admission_state.reason_code
                          IS NOT DISTINCT FROM EXCLUDED.reason_code
                        AND send_admission_state.hold_until
                          IS NOT DISTINCT FROM EXCLUDED.hold_until
                      )
                      OR (
                        CASE send_admission_state.state
                          WHEN 'closed' THEN 2
                          WHEN 'degraded' THEN 1
                          ELSE 0
                        END
                        <=
                        CASE EXCLUDED.state
                          WHEN 'closed' THEN 2
                          WHEN 'degraded' THEN 1
                          ELSE 0
                        END
                      )
                    )
                    RETURNING state, reason_code, state_epoch, hold_until,
                              valid_until, now() AS db_now
                    """
                ),
                {
                    "state": state,
                    "reason": reason,
                    "epoch": epoch,
                    "hold_until": hold_until,
                },
            )
            row = updated.mappings().one_or_none()
            if row is not None:
                saved = dict(row)
                saved["outcome"] = "saved"
                return saved
            winner_row = (
                await connection.execute(
                    text(
                        """
                        SELECT state, reason_code, state_epoch, hold_until,
                               valid_until, now() AS db_now
                        FROM send_admission_state WHERE scope='send'
                        """
                    )
                )
            ).mappings().one_or_none()
        if winner_row is None:
            raise RuntimeError("admission control state missing after conflict")
        winner = dict(winner_row)
        winner["outcome"] = "adopted"
        raise AdmissionControlConflict(winner)

    async def consume_recovery_budget(
        self,
        *,
        epoch: int,
        batches: int,
        recipients: int,
        segments: int,
        limits: SendAdmissionLimits,
    ) -> bool:
        """按当前 state_epoch 消费共享恢复预算；旧 generation 键不可复用。"""

        if epoch < 1 or batches < 1 or recipients < 1 or segments < 1:
            raise ValueError("recovery budget inputs must be positive")
        try:
            async with asyncio.timeout(max(self.control_timeout_s, 2.0)):
                allowed = await self.redis.eval(
                    RECOVERY_BUDGET_LUA,
                    1,
                    f"admission:recovery:{epoch}",
                    str(batches),
                    str(recipients),
                    str(segments),
                    str(limits.recovery_accept_batches_per_second),
                    str(limits.recovery_accept_recipients_per_second),
                    str(limits.recovery_accept_segments_per_second),
                )
        except Exception as exc:
            raise SendAdmissionUnavailable() from exc
        return bool(int(allowed))

    async def record_transition(
        self,
        *,
        previous: str | None,
        state: str,
        reason: str,
        version: int,
        facts: SendAdmissionFacts,
    ) -> None:
        """状态变化写入 alert_log；四小时内同键去重，渠道仅 log-sink。"""

        level = "crit" if state == "closed" else "warn" if state == "degraded" else "info"
        titles = {
            "closed": "发送通道已关闭",
            "degraded": "发送通道已降级",
            "open": "发送通道已恢复",
        }
        reason_titles = {
            "dispatcher_heartbeat_stale": "outbox dispatcher stale",
            "send_lanes_heartbeat_stale": "send lanes heartbeat stale",
            "realtime_heartbeat_stale": "realtime sender stale",
            "bulk_heartbeat_stale": "bulk sender stale",
        }
        engine = self._engine()
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO alert_log(
                      alert_type,level,title,detail,channels,dedup_key
                    )
                    SELECT
                      'send_admission',:level,:title,
                      CAST(:detail AS jsonb),'log-sink',:dedup_key
                    WHERE NOT EXISTS (
                      SELECT 1 FROM alert_log
                      WHERE dedup_key=:dedup_key
                        AND created_at>=now()-interval '4 hours'
                    )
                    """
                ),
                {
                    "level": level,
                    "title": reason_titles.get(reason, titles.get(state, "发送通道状态变化")),
                    "detail": json.dumps(
                        {
                            "previous": previous,
                            "state": state,
                            "reason": reason,
                            "version": version,
                            "outbox_active": facts.outbox_active,
                            "outbox_oldest_age_s": facts.outbox_oldest_age_s,
                            "outbox_dead": facts.outbox_dead,
                            "uncertain_overdue": facts.uncertain_overdue,
                            "callback_dead": facts.callback_dead,
                            "realtime_paused": facts.realtime_paused,
                            "bulk_paused": facts.bulk_paused,
                            "vendor_failures": facts.vendor_failures,
                            "realtime_heartbeat_stale": facts.realtime_heartbeat_stale,
                            "bulk_heartbeat_stale": facts.bulk_heartbeat_stale,
                            "dispatcher_heartbeat_stale": facts.dispatcher_heartbeat_stale,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    "dedup_key": f"send_admission:{state}:{reason}",
                },
            )
