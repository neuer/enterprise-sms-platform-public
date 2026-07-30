from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from app.services.housekeeping import LifecyclePolicy
from app.services.housekeeping_repository import SqlHousekeepingRepository


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
    imports = await repo.expired_imports()

    assert imports[0].invalid_file == "safe.csv"
    assert imports[0].source_file == "source.smsx"
    assert "reservation_expires_at<=now()" in connection.calls[1][0]
    assert "phone" not in connection.calls[1][0].lower()


@pytest.mark.asyncio
async def test_cleanup_is_one_transaction_and_never_touches_audit_log() -> None:
    row: dict[str, object] = {
        "raw": 2,
        "unmatched": 3,
        "imports": 1,
        "idempotency": 4,
        "jobs": 5,
        "usage": 6,
    }
    repo, connection = repository([FakeResult([row])])

    result = await repo.cleanup(LifecyclePolicy(92, 90, 30), (7,))

    sql, params = connection.calls[0]
    assert result.total == 21
    assert "raw_vendor_log" in sql and "unmatched_report" in sql
    assert "import_task" in sql and "idempotency_record" in sql and "job_run" in sql
    assert "purged_consumed_import_phones" in sql
    assert "payload_purged_at=now()" in sql
    assert "state='ready'" in sql
    assert "state='reserved'" in sql
    assert "reservation_expires_at<=now()" in sql
    assert "callback_report_event" in sql
    event_guard = sql.split("deleted_callback_events AS", maxsplit=1)[1]
    assert "t.status" not in event_guard.split(")\n                        SELECT", maxsplit=1)[0]
    assert "t.event_keys @>" in sql
    assert "ARRAY[e.event_key]::char(64)[]" in sql
    assert sql.count("make_interval(days=>:raw_days)") == 3
    assert "audit_log" not in sql
    assert params == {
        "raw_days": 92,
        "unmatched_days": 90,
        "job_days": 30,
        "usage_days": 90,
        "import_ids": [7],
    }
