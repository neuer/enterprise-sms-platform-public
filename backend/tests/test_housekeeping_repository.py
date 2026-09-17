from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from app.services.housekeeping import LifecyclePolicy
from app.services.housekeeping_repository import PLANS, SqlHousekeepingRepository


class FakeResult:
    def __init__(self, rows: list[dict[str, object]] | None = None) -> None:
        self.rows = rows or []

    def mappings(self) -> FakeResult:
        return self

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self.rows)

    def one(self) -> dict[str, object]:
        assert len(self.rows) == 1
        return self.rows[0]


class FakeConnection:
    def __init__(self, results: list[FakeResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, Any]] = []

    async def execute(self, statement: object, params: Any = None) -> FakeResult:
        self.calls.append((str(statement), params))
        return self.results.pop(0)


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

    def connect(self) -> FakeContext:
        return FakeContext(self.connection)

    def begin(self) -> FakeContext:
        return FakeContext(self.connection)

    async def dispose(self) -> None:
        return None


def repository(
    results: list[FakeResult],
) -> tuple[SqlHousekeepingRepository, FakeConnection]:
    connection = FakeConnection(results)
    repo = SqlHousekeepingRepository()
    repo._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]
    return repo, connection


@pytest.mark.asyncio
async def test_repository_loads_policy_and_lists_safe_import_file_metadata() -> None:
    repo, connection = repository(
        [
            FakeResult(
                [
                    {"key": "raw_log_retention_days", "value": "91"},
                    {"key": "unmatched_retention_days", "value": "92"},
                    {"key": "job_history_days", "value": "31"},
                ]
            ),
            FakeResult(
                [
                    {
                        "id": 4,
                        "invalid_file": "safe.csv",
                        "source_file": "source.smsx",
                    }
                ]
            ),
        ]
    )

    assert await repo.policy() == LifecyclePolicy(91, 92, 31)
    imports = await repo.expired_imports(cutoff=datetime.now(UTC), after_id=0, limit=50)

    assert imports[0].invalid_file == "safe.csv"
    assert imports[0].source_file == "source.smsx"
    assert "reservation_expires_at<=CAST(:cutoff AS timestamptz)" in connection.calls[1][0]
    assert "phone_enc" not in connection.calls[1][0].lower()
    assert "LIMIT :limit" in connection.calls[1][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("table", [table for table in PLANS if table != "idempotency"])
async def test_cleanup_pages_are_bounded_and_use_a_separate_transaction(table: str) -> None:
    plan = PLANS[table]
    key = {name: 7 if kind in {"bigint", "smallint"} else "key" for name, kind in plan.keys}
    repo, connection = repository([FakeResult(), FakeResult(), FakeResult([key])])
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    cursor = tuple(key.values())
    result = await repo.cleanup_page(
        table, LifecyclePolicy(90, 90, 30), cutoff=cutoff, cursor=cursor, limit=5
    )
    sql, params = connection.calls[-1]
    assert result.affected == 1
    assert result.cursor == cursor
    assert "LIMIT :limit" in sql
    assert "audit_log" not in sql
    assert "now()" not in sql
    assert params["cutoff"] == cutoff
    assert params["limit"] == 5
    assert "after_0" in params
    assert "SET LOCAL statement_timeout='5s'" in connection.calls[1][0]


def test_cleanup_parent_guards_prevent_unbounded_cascade_and_active_fact_deletion() -> None:
    assert "usage_frequency_entry e" in PLANS["usage"].predicate
    assert "usage_quota_entry e" in PLANS["usage"].predicate
    assert "usage_chunk_allocation" in PLANS["usage"].predicate
    assert "usage_frequency_alias a" in PLANS["usage_subject"].predicate
    assert "status='uncertain'" in PLANS["raw"].predicate
    assert "processed = TRUE" in PLANS["raw"].predicate
    assert "status<>'running'" in PLANS["jobs"].predicate
    assert "expires_at>CAST(:cutoff AS timestamptz)" in PLANS["usage_frequency"].predicate


@pytest.mark.asyncio
async def test_idempotency_cleanup_rechecks_protection_after_bounded_batch_locks() -> None:
    repo, connection = repository([
        FakeResult(), FakeResult(), FakeResult([{"id": 7}]), FakeResult(), FakeResult([{"id": 7}]),
    ])
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    result = await repo.cleanup_page(
        "idempotency", LifecyclePolicy(90, 90, 30), cutoff=cutoff, cursor=(6,), limit=5,
    )
    assert result.affected == result.counts.idempotency == 1
    assert result.cursor == (7,)
    selection, params = connection.calls[2]
    deletion, values = connection.calls[4]
    assert "LIMIT :limit FOR UPDATE OF b SKIP LOCKED" in selection
    assert params["cutoff"] == cutoff and params["limit"] == 5
    assert "result_expires_at=i.expires_at" in connection.calls[3][0]
    assert "callback_task" in deletion and "now()" in deletion
    assert values == {"ids": [7], "cutoff": cutoff}
