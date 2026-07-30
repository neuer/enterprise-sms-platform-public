from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from app.services.stats_repository import SqlStatsRepository


class FakeResult:
    def __init__(self, rowcount: int = 0) -> None:
        self.rowcount = rowcount


class FakeConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def execute(self, statement: object, params: Any = None) -> FakeResult:
        self.calls.append((str(statement), params))
        return FakeResult(8 if len(self.calls) == 3 else 0)


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

    def begin(self) -> FakeContext:
        return FakeContext(self.connection)

    async def dispose(self) -> None:
        self.disposed = True


@pytest.mark.asyncio
async def test_day_rebuild_is_locked_atomic_and_contains_all_rollups() -> None:
    repository = SqlStatsRepository()
    connection = FakeConnection()
    engine = FakeEngine(connection)
    repository._engine = lambda: engine  # type: ignore[method-assign]

    assert await repository.aggregate_day(date(2026, 7, 12)) == 8

    assert len(connection.calls) == 3
    lock_sql, lock_params = connection.calls[0]
    assert "pg_advisory_xact_lock" in lock_sql
    assert lock_params == {"lock_key": date(2026, 7, 12).toordinal()}
    delete_sql, delete_params = connection.calls[1]
    assert "DELETE FROM stat_daily" in delete_sql
    assert delete_params == {"stat_date": date(2026, 7, 12)}

    insert_sql, params = connection.calls[2]
    assert "INSERT INTO stat_daily" in insert_sql
    assert "('app', CAST(f.app_id AS text))" in insert_sql
    assert "('dept', f.dept)" in insert_sql
    assert "('all', '')" in insert_sql
    assert "(f.category), ('all')" in insert_sql
    assert "sum(f.segments)" in insert_sql
    assert "f.status IN ('unknown','other')" in insert_sql
    assert params == {
        "stat_date": date(2026, 7, 12),
        "start_at": datetime(2026, 7, 11, 16, 0, tzinfo=UTC),
        "end_at": datetime(2026, 7, 12, 16, 0, tzinfo=UTC),
    }
    assert engine.disposed
