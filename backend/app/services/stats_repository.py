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
    WITH message_totals AS (
      SELECT m.batch_id,count(*) total,
        count(*) FILTER (WHERE m.status='delivered') delivered,
        count(*) FILTER (WHERE m.status='failed') failed,
        count(*) FILTER (WHERE m.status IN ('unknown','other')) unknown_cnt
      FROM sms_message m
      WHERE m.created_at>=:start_at AND m.created_at<:end_at
      GROUP BY m.batch_id
    ), facts AS (
      SELECT b.app_id,b.dept,b.category,b.segments,t.total,
        t.delivered,t.failed,t.unknown_cnt
      FROM message_totals t
      JOIN sms_batch b ON b.id=t.batch_id
    )
    INSERT INTO stat_daily(
      stat_date,dim_type,dim_value,category,total,total_segments,
      delivered,failed,unknown_cnt
    )
    SELECT :stat_date,d.dim_type,d.dim_value,c.category,
      CAST(sum(f.total) AS integer),
      CAST(sum(f.segments*f.total) AS integer),
      CAST(sum(f.delivered) AS integer),
      CAST(sum(f.failed) AS integer),
      CAST(sum(f.unknown_cnt) AS integer)
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

    async def list_dirty_dates(self, *, limit: int = 40) -> tuple[date, ...]:
        """晚到回执标记的待补算归属日；限量防止单轮聚合无界。"""

        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        "SELECT stat_date FROM stat_dirty_date "
                        "ORDER BY stat_date LIMIT :limit"
                    ),
                    {"limit": limit},
                )
                return tuple(result.scalars())
        finally:
            await engine.dispose()

    async def clear_dirty_date(self, stat_date: date) -> None:
        engine = self._engine()
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM stat_dirty_date WHERE stat_date=:stat_date"),
                    {"stat_date": stat_date},
                )
        finally:
            await engine.dispose()
