"""发送准入快照：复用当前告警同一组 Outbox/uncertain/callback 与暂停键。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from sqlalchemy import text

from app.core.runtime_resources import database_engine, redis_client
from app.services.send_admission import SendAdmissionFacts
from app.settings import Settings, get_settings


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
                      (SELECT EXISTS (
                         SELECT 1 FROM (
                           VALUES ('send-realtime'),('send-bulk'),
                                  ('outbox-dispatcher')
                         ) AS required(component)
                         WHERE NOT EXISTS (
                           SELECT 1 FROM send_runtime_heartbeat h
                           WHERE h.component=required.component
                             AND h.lease_until >= now()
                         )
                       )) heartbeat_stale
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
        return SendAdmissionFacts(
            outbox_active=max(0, int(row["outbox_active"] or 0)),
            outbox_oldest_age_s=max(0, int(oldest or 0)),
            outbox_dead=max(0, int(row["outbox_dead"] or 0)),
            uncertain_overdue=max(0, int(row["uncertain_overdue"] or 0)),
            callback_dead=max(0, int(row["callback_dead"] or 0)),
            realtime_paused=bool(values[0]),
            bulk_paused=bool(values[1]),
            vendor_failures=failures,
            heartbeat_stale=(
                bool(getattr(self.settings, "is_production", False))
                and int(row["heartbeat_stale"] or 0) > 0
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
                    "title": titles.get(state, "发送通道状态变化"),
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
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    "dedup_key": f"send_admission:{state}:{reason}",
                },
            )
