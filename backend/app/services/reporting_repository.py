"""stat_daily 日/周/月有界概览与服务端分页查询。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.runtime_resources import ManagedAsyncEngine, database_engine
from app.services.reporting import (
    ReportFilterNotReady,
    ReportingData,
    ReportingDimSummary,
    ReportingQuery,
    ReportingTotals,
    ReportingTrend,
    ReportingTrendSeries,
)
from app.services.stats import STAT_TEST_SCOPE_VERSION, success_rate, success_rate_sql
from app.settings import Settings, get_settings

BUCKET_SQL = {
    "day": "s.stat_date",
    "week": "date_trunc('week', s.stat_date)::date",
    "month": "date_trunc('month', s.stat_date)::date",
}
COUNTS = ("total", "total_segments", "delivered", "failed", "unknown_cnt")
READINESS_SQL = text(
    """
    WITH bounds AS (
      SELECT LEAST(
        (SELECT min(stat_date) FROM stat_daily),
        (SELECT min((created_at AT TIME ZONE 'Asia/Shanghai')::date) FROM sms_message)
      ) AS horizon_start
    )
    SELECT days.stat_date::date AS stat_date
    FROM bounds
    CROSS JOIN LATERAL generate_series(
      CASE WHEN bounds.horizon_start IS NULL THEN DATE '0001-01-02'
           ELSE GREATEST(CAST(:start AS date), bounds.horizon_start) END,
      CASE WHEN bounds.horizon_start IS NULL THEN DATE '0001-01-01'
           ELSE LEAST(
             CAST(:end AS date),
             (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai')::date
           ) END,
      interval '1 day'
    ) AS days(stat_date)
    WHERE bounds.horizon_start IS NOT NULL
      AND NOT EXISTS (
        SELECT 1 FROM stat_scope_day marker
        WHERE marker.stat_date=days.stat_date::date
          AND marker.aggregation_version>=:scope_version
      )
    ORDER BY days.stat_date
    LIMIT 1
    """
)


def _base_sql(query: ReportingQuery) -> str:
    """仅从固定白名单选择 SQL 片段，所有用户值保持绑定参数。"""
    bucket = BUCKET_SQL[query.granularity]
    if query.group_by == "app":
        label = "a.name"
        join = "JOIN app a ON CAST(a.id AS text)=s.dim_value"
        scope = "(CAST(:scope_dept AS varchar(128)) IS NULL OR a.dept=:scope_dept)"
    else:
        label = "s.dim_value"
        join = ""
        scope = "(CAST(:scope_dept AS varchar(128)) IS NULL OR s.dim_value=:scope_dept)"
    return f"""
        SELECT {bucket} period_start,s.dim_value,{label} dim_label,
          {",".join(f"sum(s.{name})::bigint {name}" for name in COUNTS)}
        FROM stat_daily s {join}
        WHERE s.dim_type=:dim_type AND s.category=:category
          AND s.test_scope=:test_scope
          AND s.stat_date BETWEEN :start AND :end AND {scope}
        GROUP BY {bucket},s.dim_value,{label}
    """


def _missing_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise TypeError("missing stat date must be a date")


class SqlReportingRepository:
    """一个只读一致快照内返回至多六个维度、六条紧凑趋势和一页明细。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _engine(self) -> ManagedAsyncEngine:
        return database_engine(self.settings.database_url)

    async def _ensure_filtered_scope_ready(
        self, connection: AsyncConnection, query: ReportingQuery
    ) -> None:
        """测试或普通筛选遇到未标记日期时失败，禁止把缺层当成 0。"""

        if query.test_scope == "all":
            return
        missing = (
            await connection.execute(
                READINESS_SQL,
                {
                    "start": query.start,
                    "end": query.end,
                    "scope_version": STAT_TEST_SCOPE_VERSION,
                },
            )
        ).mappings().first()
        if missing is None:
            return
        raise ReportFilterNotReady(query.start, query.end, _missing_date(missing["stat_date"]))

    async def query(self, query: ReportingQuery) -> ReportingData:
        base = _base_sql(query)
        metric = {"total": "total", "total_segments": "total_segments"}[query.metric]
        direction = {"asc": "ASC", "desc": "DESC"}[query.order]
        sort = {
            "period_start": "period_start",
            "total": "total",
            "total_segments": "total_segments",
            "success_rate": success_rate_sql("delivered", "failed"),
        }[query.sort]
        sums = ",".join(f"sum({name})::bigint {name}" for name in COUNTS)
        # 只合并展示维度；计数、摘要与趋势仍覆盖全部授权事实。
        overview = text(f"""
            WITH base AS ({base}), dimensions AS (
              SELECT dim_value,dim_label,{sums},count(*) detail_count
              FROM base GROUP BY dim_value,dim_label
            ), ranked AS (
              SELECT *,row_number() OVER (
                ORDER BY {metric} DESC,dim_label,dim_value
              ) rank FROM dimensions
            )
            SELECT CASE WHEN rank<=5 THEN dim_value ELSE '' END dim_value,
              CASE WHEN rank<=5 THEN dim_label ELSE '其他' END dim_label,
              rank>5 is_other,{sums},sum(detail_count)::bigint detail_count,
              count(*)::bigint dimension_count,min(rank) rank
            FROM ranked GROUP BY 1,2,3 ORDER BY rank
        """)
        params: dict[str, Any] = {
            "start": query.start,
            "end": query.end,
            "category": query.category,
            "scope_dept": query.scope_dept,
            "dim_type": query.group_by,
            "test_scope": query.test_scope,
        }
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                connection = await connection.execution_options(isolation_level="REPEATABLE READ")
                async with connection.begin():
                    await self._ensure_filtered_scope_ready(connection, query)
                    dimensions = list((await connection.execute(overview, params)).mappings())
                    top_dims = [str(row["dim_value"]) for row in dimensions if not row["is_other"]]
                    trend_params = {**params, "top_dims": top_dims}
                    trend_rows = list(
                        (
                            await connection.execute(
                                text(f"""
                        WITH base AS ({base})
                        SELECT period_start,
                          CASE WHEN dim_value=ANY(CAST(:top_dims AS text[]))
                            THEN dim_value ELSE '' END dim_value,
                          NOT (dim_value=ANY(CAST(:top_dims AS text[]))) is_other,
                          sum(total)::bigint total,sum(total_segments)::bigint total_segments
                        FROM base GROUP BY 1,2,3 ORDER BY period_start
                    """),
                                trend_params,
                            )
                        ).mappings()
                    )
                    rows = list(
                        (
                            await connection.execute(
                                text(f"""
                        WITH base AS ({base})
                        SELECT * FROM base
                        ORDER BY {sort} {direction},dim_label {direction},
                          dim_value {direction},period_start {direction}
                        LIMIT :size OFFSET :offset
                    """),
                                {
                                    **params,
                                    "size": query.size,
                                    "offset": (query.page - 1) * query.size,
                                },
                            )
                        ).mappings()
                    )
            periods = tuple(sorted({row["period_start"] for row in trend_rows}))
            trend_values = {
                (row["period_start"], str(row["dim_value"]), bool(row["is_other"])): (
                    int(row["total"]),
                    int(row["total_segments"]),
                )
                for row in trend_rows
            }
            dim_summary = tuple(
                ReportingDimSummary(
                    str(row["dim_value"]),
                    str(row["dim_label"]),
                    int(row["total"]),
                    int(row["total_segments"]),
                    int(row["delivered"]),
                    int(row["failed"]),
                    int(row["unknown_cnt"]),
                    success_rate(int(row["delivered"]), int(row["failed"])),
                    bool(row["is_other"]),
                )
                for row in dimensions
            )
            return ReportingData(
                dim_summary,
                ReportingTrend(
                    periods,
                    tuple(
                        ReportingTrendSeries(
                            dim.dim_value,
                            dim.dim_label,
                            tuple(
                                trend_values.get((period, dim.dim_value, dim.is_other), (0, 0))[0]
                                for period in periods
                            ),
                            tuple(
                                trend_values.get((period, dim.dim_value, dim.is_other), (0, 0))[1]
                                for period in periods
                            ),
                            dim.is_other,
                        )
                        for dim in dim_summary
                    ),
                ),
                tuple(
                    ReportingTotals(
                        row["period_start"],
                        str(row["dim_value"]),
                        str(row["dim_label"]),
                        int(row["total"]),
                        int(row["total_segments"]),
                        int(row["delivered"]),
                        int(row["failed"]),
                        int(row["unknown_cnt"]),
                    )
                    for row in rows
                ),
                sum(int(row["detail_count"]) for row in dimensions),
                sum(int(row["dimension_count"]) for row in dimensions),
            )
        finally:
            await engine.dispose()
