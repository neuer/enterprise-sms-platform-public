"""统一告警路由配置与 PostgreSQL 原子去重仓储。"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from app.core.runtime_resources import database_engine
from app.services.alert import AlertRouting, AlertService, SmtpRouting, safe_alert_routing
from app.services.outbox import OutboxEventSpec
from app.services.outbox_repository import enqueue_outbox
from app.settings import Settings, get_settings


class SqlAlertRepository:
    """以 alert_log 为事实源，四小时内同 dedup_key 仅声明一次。"""

    uses_outbox = True

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def load_routing(self) -> AlertRouting:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        """
                        SELECT key,value FROM sys_config WHERE key IN (
                          'alert_wecom_webhook','alert_mail_to','alert_smtp_host',
                          'alert_smtp_port','alert_mail_from'
                        )
                        """
                    )
                )
                values = {str(row["key"]): str(row["value"]) for row in result.mappings()}
        finally:
            await engine.dispose()
        recipients = tuple(
            address.strip()
            for address in values.get("alert_mail_to", "").split(",")
            if address.strip()
        )
        smtp = None
        if recipients:
            try:
                port = int(values.get("alert_smtp_port", "25"))
            except ValueError:
                port = 0
            if 1 <= port <= 65_535:
                smtp = SmtpRouting(
                    host=values.get("alert_smtp_host", "smtp").strip() or "smtp",
                    port=port,
                    sender=values.get("alert_mail_from", "sms-platform@localhost").strip()
                    or "sms-platform@localhost",
                    recipients=recipients,
                )
        return safe_alert_routing(
            AlertRouting(
                wecom_webhook=values.get("alert_wecom_webhook", "").strip(),
                smtp=smtp,
            ),
            self.settings.alert_smtp_allowed_host_set,
        )

    async def claim(
        self,
        *,
        alert_type: str,
        level: str,
        title: str,
        detail: dict[str, Any],
        channels: str,
        dedup_key: str,
        dedup_hours: int,
    ) -> int | None:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text(
                        """
                        WITH dedup_lock AS (
                          SELECT pg_advisory_xact_lock(
                            hashtextextended(:dedup_key, 0)
                          )
                        )
                        INSERT INTO alert_log (
                          alert_type,level,title,detail,channels,dedup_key
                        )
                        SELECT :alert_type,:level,:title,CAST(:detail AS jsonb),
                               :channels,:dedup_key
                        FROM dedup_lock
                        WHERE NOT EXISTS (
                          SELECT 1 FROM alert_log
                          WHERE dedup_key = :dedup_key
                            AND created_at > now() - make_interval(hours => :dedup_hours)
                        )
                        RETURNING id
                        """
                    ),
                    {
                        "alert_type": alert_type,
                        "level": level,
                        "title": title,
                        "detail": json.dumps(detail, ensure_ascii=False, sort_keys=True),
                        "channels": channels,
                        "dedup_key": dedup_key,
                        "dedup_hours": dedup_hours,
                    },
                )
                claimed = result.scalar_one_or_none()
                if claimed is not None:
                    for channel in ("wecom", "smtp"):
                        if channel not in channels.split(","):
                            continue
                        await enqueue_outbox(
                            connection,
                            OutboxEventSpec(
                                event_type="alert.delivery",
                                aggregate_type="alert_log",
                                aggregate_id=str(claimed),
                                task_name="app.tasks.outbox.deliver_alert",
                                queue="callback",
                                args=(int(claimed), channel),
                                dedup_key=f"alert:{claimed}:{channel}",
                            ),
                        )
                return int(claimed) if claimed is not None else None
        finally:
            await engine.dispose()

    async def load_event(self, alert_id: int) -> dict[str, Any] | None:
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        """
                        SELECT id,alert_type,level,title,detail,dedup_key
                        FROM alert_log WHERE id=:alert_id
                        """
                    ),
                    {"alert_id": alert_id},
                )
                row = result.mappings().one_or_none()
                return dict(row) if row is not None else None
        finally:
            await engine.dispose()


class SqlAlertService(AlertService):
    """使用 PostgreSQL 配置与 alert_log 的默认统一告警服务。"""

    def __init__(self, settings: Settings | None = None) -> None:
        selected = settings or get_settings()
        super().__init__(
            SqlAlertRepository(selected),
            allowed_smtp_hosts=selected.alert_smtp_allowed_host_set,
        )
