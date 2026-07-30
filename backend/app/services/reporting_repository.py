"""stat_daily 日/周/月安全聚合查询。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from app.core.runtime_resources import database_engine
from app.services.reporting import ReportingQuery, ReportingTotals
from app.settings import Settings, get_settings

BUCKET_SQL = {
    "day": "s.stat_date",
    "week": "date_trunc('week', s.stat_date)::date",
    "month": "date_trunc('month', s.stat_date)::date",
}


class SqlReportingRepository:
    """仅从固定白名单选择 SQL 片段，所有用户值保持绑定参数。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _engine(self) -> Any:
        return database_engine(self.settings.database_url)

    async def query(self, query: ReportingQuery) -> tuple[ReportingTotals, ...]:
        bucket = BUCKET_SQL[query.granularity]
        if query.group_by == "app":
            dimension_select = "s.dim_value, a.name dim_label"
            join = "JOIN app a ON CAST(a.id AS text)=s.dim_value"
            scope = "(CAST(:scope_dept AS varchar(128)) IS NULL OR a.dept=:scope_dept)"
            dim_type = "app"
            dimension_group = "s.dim_value,a.name"
        else:
            dimension_select = "s.dim_value, s.dim_value dim_label"
            join = ""
            scope = "(CAST(:scope_dept AS varchar(128)) IS NULL OR s.dim_value=:scope_dept)"
            dim_type = "dept"
            dimension_group = "s.dim_value"
        statement = text(
            f"""
            SELECT {bucket} period_start,{dimension_select},
              CAST(sum(s.total) AS integer) total,
              CAST(sum(s.total_segments) AS integer) total_segments,
              CAST(sum(s.delivered) AS integer) delivered,
              CAST(sum(s.failed) AS integer) failed,
              CAST(sum(s.unknown_cnt) AS integer) unknown_cnt
            FROM stat_daily s {join}
            WHERE s.dim_type='{dim_type}' AND s.category=:category
              AND s.stat_date BETWEEN :start AND :end AND {scope}
            GROUP BY {bucket},{dimension_group}
            ORDER BY period_start,dim_label
            """
        )
        engine = self._engine()
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    statement,
                    {
                        "start": query.start,
                        "end": query.end,
                        "category": query.category,
                        "scope_dept": query.scope_dept,
                    },
                )
                return tuple(
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
                    for row in result.mappings()
                )
        finally:
            await engine.dispose()
