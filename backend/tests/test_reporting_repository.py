from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest

from app.services.reporting import ReportFilterNotReady, ReportingQuery
from app.services.reporting_repository import SqlReportingRepository


class FakeResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def mappings(self) -> FakeResult:
        return self

    def first(self) -> dict[str, object] | None:
        return self.rows[0] if self.rows else None

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self.rows)


class FakeConnection:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.missing: list[dict[str, object]] = []
        self.calls: list[tuple[str, Any]] = []

    async def execution_options(self, **kwargs: object) -> FakeConnection:
        assert kwargs == {"isolation_level": "REPEATABLE READ"}
        return self

    def begin(self) -> FakeContext:
        return FakeContext(self)

    async def execute(self, statement: object, params: Any = None) -> FakeResult:
        sql = str(statement)
        self.calls.append((sql, params))
        if "stat_scope_day" in sql:
            return FakeResult(self.missing)
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
    connection = FakeConnection(
        [
            {
                "period_start": date(2026, 7, 6),
                "dim_value": "7",
                "dim_label": "OA应用",
                "total": 10,
                "total_segments": 12,
                "delivered": 8,
                "failed": 2,
                "unknown_cnt": 1,
                "detail_count": 1,
                "dimension_count": 1,
                "is_other": False,
            }
        ]
    )
    engine = FakeEngine(connection)
    repository = SqlReportingRepository()
    repository._engine = lambda: engine  # type: ignore[method-assign]

    rows = await repository.query(
        ReportingQuery("week", "app", "notice", date(2026, 7, 1), date(2026, 7, 12), "业务一部")
    )

    assert rows.items[0].dim_label == "OA应用" and rows.items[0].total_segments == 12
    sql, params = connection.calls[0]
    assert "date_trunc('week', s.stat_date)::date" in sql
    assert "JOIN app a ON CAST(a.id AS text)=s.dim_value" in sql
    assert "a.dept=:scope_dept" in sql
    assert "sum(s.total_segments)" in sql
    assert params == {
        "dim_type": "app",
        "start": date(2026, 7, 1),
        "end": date(2026, 7, 12),
        "category": "notice",
        "scope_dept": "业务一部",
        "test_scope": "all",
    }
    assert all("s.test_scope=:test_scope" in sql for sql, _ in connection.calls)
    assert all("stat_scope_day" not in sql for sql, _ in connection.calls)
    assert engine.disposed


@pytest.mark.asyncio
async def test_dept_month_query_never_interpolates_user_values() -> None:
    connection = FakeConnection([])
    repository = SqlReportingRepository()
    repository._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]
    query = ReportingQuery(
        "month", "dept", "all", date(2026, 1, 1), date(2026, 7, 12), "研发部' OR 1=1"
    )

    assert (await repository.query(query)).items == ()
    sql, params = connection.calls[0]
    assert "date_trunc('month', s.stat_date)::date" in sql
    assert "s.dim_type=:dim_type" in sql and "s.dim_value=:scope_dept" in sql
    assert query.scope_dept is not None and query.scope_dept not in sql
    assert params["scope_dept"] == query.scope_dept
    assert params["test_scope"] == "all"


@pytest.mark.asyncio
async def test_false_scope_is_one_layer_and_missing_marker_is_not_zero() -> None:
    connection = FakeConnection([])
    connection.missing = [{"stat_date": date(2026, 7, 1)}]
    repository = SqlReportingRepository()
    repository._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]
    query = ReportingQuery(
        "day",
        "dept",
        "all",
        date(2026, 7, 1),
        date(2026, 7, 2),
        None,
        is_test=False,
    )

    with pytest.raises(ReportFilterNotReady, match="筛选数据尚未就绪") as error:
        await repository.query(query)

    assert error.value.missing_date == date(2026, 7, 1)
    assert len(connection.calls) == 1
    assert connection.calls[0][1]["scope_version"] == 1

    connection.missing = []
    connection.calls.clear()
    ready = ReportingQuery(
        "month",
        "app",
        "market",
        date(2026, 7, 1),
        date(2026, 7, 2),
        "业务一部",
        is_test=True,
    )
    assert (await repository.query(ready)).items == ()
    assert connection.calls[0][0].count("stat_scope_day") == 1
    assert all(params["test_scope"] == "test" for _, params in connection.calls[1:])
