from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest

from app.services.reporting import ReportingQuery
from app.services.reporting_repository import SqlReportingRepository


class FakeResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def mappings(self) -> FakeResult:
        return self

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self.rows)


class FakeConnection:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, Any]] = []

    async def execute(self, statement: object, params: Any = None) -> FakeResult:
        self.calls.append((str(statement), params))
        return FakeResult(self.rows)


class FakeContext:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, *_: object) -> None:
        return None


class FakeEngine:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.disposed = False

    def connect(self) -> FakeContext:
        return FakeContext(self.connection)

    async def dispose(self) -> None:
        self.disposed = True


@pytest.mark.asyncio
async def test_app_week_query_uses_fixed_bucket_join_and_department_scope() -> None:
    connection = FakeConnection([
        {
            "period_start": date(2026, 7, 6), "dim_value": "7", "dim_label": "OA应用",
            "total": 10, "total_segments": 12, "delivered": 8, "failed": 2,
            "unknown_cnt": 1,
        }
    ])
    engine = FakeEngine(connection)
    repository = SqlReportingRepository()
    repository._engine = lambda: engine  # type: ignore[method-assign]

    rows = await repository.query(
        ReportingQuery(
            "week", "app", "notice", date(2026, 7, 1), date(2026, 7, 12), "业务一部"
        )
    )

    assert rows[0].dim_label == "OA应用" and rows[0].total_segments == 12
    sql, params = connection.calls[0]
    assert "date_trunc('week', s.stat_date)::date" in sql
    assert "JOIN app a ON CAST(a.id AS text)=s.dim_value" in sql
    assert "a.dept=:scope_dept" in sql
    assert "sum(s.total_segments)" in sql
    assert params == {
        "start": date(2026, 7, 1), "end": date(2026, 7, 12),
        "category": "notice", "scope_dept": "业务一部",
    }
    assert engine.disposed


@pytest.mark.asyncio
async def test_dept_month_query_never_interpolates_user_values() -> None:
    connection = FakeConnection([])
    repository = SqlReportingRepository()
    repository._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]
    query = ReportingQuery(
        "month", "dept", "all", date(2026, 1, 1), date(2026, 7, 12), "研发部' OR 1=1"
    )

    assert await repository.query(query) == ()
    sql, params = connection.calls[0]
    assert "date_trunc('month', s.stat_date)::date" in sql
    assert "s.dim_type='dept'" in sql and "s.dim_value=:scope_dept" in sql
    assert query.scope_dept is not None and query.scope_dept not in sql
    assert params["scope_dept"] == query.scope_dept
