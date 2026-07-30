"""stat_daily 的 PostgreSQL 原子快照聚合。"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.core.runtime_resources import database_engine
from app.settings import Settings, get_settings

SHANGHAI = ZoneInfo("Asia/Shanghai")

AGGREGATE_SQL = text(
    """
    WITH facts AS (
      SELECT b.app_id,b.dept,b.category,b.segments,m.status
      FROM sms_message m
      JOIN sms_batch b ON b.id=m.batch_id
      WHERE m.created_at>=:start_at AND m.created_at<:end_at
    )
    INSERT INTO stat_daily(
      stat_date,dim_type,dim_value,category,total,total_segments,
      delivered,failed,unknown_cnt
    )
    SELECT :stat_date,d.dim_type,d.dim_value,c.category,
      CAST(count(*) AS integer),
      CAST(sum(f.segments) AS integer),
      CAST(count(*) FILTER (WHERE f.status='delivered') AS integer),
      CAST(count(*) FILTER (WHERE f.status='failed') AS integer),
      CAST(count(*) FILTER (WHERE f.status IN ('unknown','other')) AS integer)
    FROM facts f
    CROSS JOIN LATERAL (VALUES
      ('app', CAST(f.app_id AS text)),
      ('dept', f.dept),
      ('all', '')
    ) AS d(dim_type,dim_value)
    CROSS JOIN LATERAL (VALUES (f.category), ('all')) AS c(category)
    WHERE d.dim_type<>'app' OR f.app_id IS NOT NULL
    GROUP BY d.dim_type,d.dim_value,c.category
    """
)


class SqlStatsRepository:
    """使用按日期 advisory lock 在单事务内完整替换日报快照。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def aggregate_day(self, stat_date: date) -> int:
        start_local = datetime.combine(stat_date, time.min, tzinfo=SHANGHAI)
        start_at = start_local.astimezone(UTC)
        end_at = (start_local + timedelta(days=1)).astimezone(UTC)
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("SELECT pg_advisory_xact_lock(:lock_key)"),
                    {"lock_key": stat_date.toordinal()},
                )
                await connection.execute(
                    text("DELETE FROM stat_daily WHERE stat_date=:stat_date"),
                    {"stat_date": stat_date},
                )
                result = await connection.execute(
                    AGGREGATE_SQL,
                    {
                        "stat_date": stat_date,
                        "start_at": start_at,
                        "end_at": end_at,
                    },
                )
                return int(result.rowcount or 0)
        finally:
            await engine.dispose()
