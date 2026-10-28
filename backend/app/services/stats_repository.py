"""stat_daily 的 PostgreSQL 原子快照聚合。"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.core.runtime_resources import ManagedAsyncEngine, database_engine
from app.services.stats import STAT_TEST_SCOPE_VERSION
from app.settings import Settings, get_settings

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _as_date(value: object) -> date:
    """把驱动返回的日期或午夜时间归一成 date。"""

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise TypeError("stat date must be a date")


# 每轮只补有限个历史日；每个日期仍是独立事务，中断后可从标记继续。
SCOPE_BACKFILL_LIMIT = 7

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
      SELECT b.app_id,b.dept,b.category,b.segments,b.is_test,t.total,
        t.delivered,t.failed,t.unknown_cnt
      FROM message_totals t
      JOIN sms_batch b ON b.id=t.batch_id
    )
    INSERT INTO stat_daily(
      stat_date,dim_type,dim_value,category,test_scope,total,total_segments,
      delivered,failed,unknown_cnt
    )
    SELECT :stat_date,d.dim_type,d.dim_value,c.category,s.test_scope,
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
    CROSS JOIN LATERAL (VALUES
      (CAST('all' AS varchar(8))),
      (CAST(CASE WHEN f.is_test THEN 'test' ELSE 'normal' END AS varchar(8)))
    ) AS s(test_scope)
    WHERE d.dim_type<>'app' OR f.app_id IS NOT NULL
    GROUP BY d.dim_type,d.dim_value,c.category,s.test_scope
    """
)

MARKER_SQL = text(
    """
    INSERT INTO stat_scope_day(stat_date, aggregation_version, completed_at)
    VALUES (:stat_date, :scope_version, clock_timestamp())
    ON CONFLICT (stat_date) DO UPDATE
    SET aggregation_version=EXCLUDED.aggregation_version,
        completed_at=EXCLUDED.completed_at
    """
)

BACKFILL_SQL = text(
    """
    WITH bounds AS (
      SELECT LEAST(
        (SELECT min(stat_date) FROM stat_daily),
        (SELECT min((created_at AT TIME ZONE 'Asia/Shanghai')::date) FROM sms_message)
      ) AS horizon_start,
      (clock_timestamp() AT TIME ZONE 'Asia/Shanghai')::date AS today
    )
    SELECT days.stat_date::date AS stat_date
    FROM bounds
    CROSS JOIN LATERAL generate_series(
      CASE WHEN bounds.horizon_start IS NULL THEN DATE '0001-01-02'
           ELSE bounds.horizon_start END,
      CASE WHEN bounds.horizon_start IS NULL THEN DATE '0001-01-01'
           ELSE bounds.today END,
      interval '1 day'
    ) AS days(stat_date)
    WHERE bounds.horizon_start IS NOT NULL
      AND NOT EXISTS (
        SELECT 1 FROM stat_scope_day marker
        WHERE marker.stat_date=days.stat_date::date
          AND marker.aggregation_version>=:scope_version
      )
      AND (
        EXISTS (
          SELECT 1 FROM sms_message message
          WHERE message.created_at>=(
            days.stat_date::date::timestamp AT TIME ZONE 'Asia/Shanghai'
          )
            AND message.created_at<(
              (days.stat_date::date + 1)::timestamp AT TIME ZONE 'Asia/Shanghai'
            )
        )
        OR NOT EXISTS (
          SELECT 1 FROM stat_daily snapshot
          WHERE snapshot.stat_date=days.stat_date::date
        )
      )
    ORDER BY days.stat_date
    LIMIT :limit
    """
)


class SqlStatsRepository:
    """使用按日期 advisory lock 在单事务内完整替换日报快照。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _engine(self) -> ManagedAsyncEngine:
        return database_engine(self.settings.database_url)

    async def aggregate_day(self, stat_date: date) -> int:
        """在同一把日锁和同一事务内写入 all 与 test/normal，并记下完成标记。

        无消息的日期不产生汇总行，但仍写入标记，避免把空日当成未补算。
        失败则整日回滚，不会留下只有部分 test_scope 的快照。
        """

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
                    text("DELETE FROM stat_dirty_date WHERE stat_date=:stat_date"),
                    {"stat_date": stat_date},
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
                await connection.execute(
                    MARKER_SQL,
                    {
                        "stat_date": stat_date,
                        "scope_version": STAT_TEST_SCOPE_VERSION,
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
                    text("SELECT stat_date FROM stat_dirty_date ORDER BY stat_date LIMIT :limit"),
                    {"limit": limit},
                )
                return tuple(result.scalars())
        finally:
            await engine.dispose()

    async def list_scope_backfill_dates(
        self, *, limit: int = SCOPE_BACKFILL_LIMIT
    ) -> tuple[date, ...]:
        """返回尚未标记、且能从现存消息或空日安全重算的日期。

        只剩 all 历史行、消息已不在的日期不在此列：重算会把长期汇总写成 0。
        这些日期的测试/普通筛选保持未就绪，全部口径仍读原来的 all 行。
        """

        if limit < 1:
            raise ValueError("scope backfill limit must be positive")
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    BACKFILL_SQL,
                    {"scope_version": STAT_TEST_SCOPE_VERSION, "limit": limit},
                )
                return tuple(_as_date(row) for row in result.scalars())
        finally:
            await engine.dispose()
