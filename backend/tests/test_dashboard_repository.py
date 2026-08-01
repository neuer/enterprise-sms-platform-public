from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any

import pytest

from app.services.dashboard_repository import SqlDashboardRepository


class FakeResult:
    def __init__(
        self,
        *,
        rows: list[dict[str, object]] | None = None,
        scalar: object = None,
    ) -> None:
        self.rows = rows or []
        self.scalar = scalar

    def mappings(self) -> FakeResult:
        return self

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self.rows)

    def scalar_one_or_none(self) -> object:
        return self.scalar

    def scalar_one(self) -> object:
        return self.scalar

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
        self.disposed = False

    def connect(self) -> FakeContext:
        return FakeContext(self.connection)

    async def dispose(self) -> None:
        self.disposed = True


class FakeRedis:
    def __init__(self, tokens: str | None = "3") -> None:
        self.tokens = tokens
        self.calls: list[tuple[str, str]] = []

    async def hget(self, key: str, field: str) -> str | None:
        self.calls.append((key, field))
        return self.tokens


class NeverRedis:
    async def hget(self, _key: str, _field: str) -> str:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_repository_loads_scoped_business_and_global_operational_facts() -> None:
    now = datetime(2026, 7, 12, 3, 0, tzinfo=UTC)
    connection = FakeConnection(
        [
            FakeResult(rows=[{
                "category": "notice", "total": 2, "total_segments": 3,
                "delivered": 1, "failed": 1, "unknown_cnt": 0,
            }]),
            FakeResult(scalar=9000),
            FakeResult(rows=[{"stat_date": date(2026, 7, 12), "balance": 9000}]),
            FakeResult(scalar=2),
            FakeResult(rows=[{"level": "warn", "title": "余额较低", "created_at": now}]),
            FakeResult(rows=[{"uncertain": 1, "unmatched": 3, "callback_dead": 4}]),
            FakeResult(rows=[
                {"queue": "realtime", "count": 4},
                {"queue": "bulk", "count": 9},
            ]),
            FakeResult(
                rows=[
                    {
                        "job_name": "poll_report",
                        "last_run_at": now,
                        "last_status": "success",
                    }
                ]
            ),
            FakeResult(rows=[
                {"key": "vendor_qps", "value": "8"},
                {"key": "reserved_realtime_qps", "value": "3"},
                {"key": "balance_alert_threshold", "value": "8800"},
                {"key": "test_send_max", "value": "5"},
            ]),
        ]
    )
    engine = FakeEngine(connection)
    repository = SqlDashboardRepository()
    repository._engine = lambda: engine  # type: ignore[method-assign]
    repository.redis = FakeRedis()  # type: ignore[attr-defined]

    facts = await repository.load(
        "业务一部",
        date(2026, 7, 12),
        include_operations=True,
    )

    assert facts.categories[0].total_segments == 3
    assert facts.pending_approvals == 2 and facts.operations is not None
    assert facts.operations.current_balance == 9000
    assert (
        facts.operations.uncertain,
        facts.operations.unmatched,
        facts.operations.callback_dead,
    ) == (1, 3, 4)
    assert facts.operations.realtime_queue == 4 and facts.operations.bulk_queue == 9
    assert facts.operations.qps_used == 5 and facts.operations.qps_rate == 8
    assert facts.operations.reserved_realtime_qps == 3
    assert facts.operations.channel_stale is False
    assert facts.operations.balance_alert_threshold == 8800
    assert facts.test_send_max == 5
    stats_sql, stats_params = connection.calls[0]
    assert "dim_type=:dim_type" in stats_sql
    assert stats_params["dim_type"] == "dept" and stats_params["dim_value"] == "业务一部"
    balance_sql = connection.calls[2][0]
    assert "DISTINCT ON" in balance_sql and "Asia/Shanghai" in balance_sql
    pending_sql = connection.calls[3][0]
    assert "CAST(:scope_dept AS varchar(128)) IS NULL" in pending_sql
    count_sql = connection.calls[5][0]
    assert "unmatched_report" in count_sql and "callback_task" in count_sql
    assert "sms_chunk" in count_sql and "app.dept" in count_sql
    assert repository.redis.calls == [("ratelimit:vendor", "tokens")]  # type: ignore[attr-defined]
    assert engine.disposed


@pytest.mark.asyncio
async def test_repository_marks_channel_stale_when_redis_snapshot_times_out() -> None:
    now = datetime(2026, 7, 12, 3, 0, tzinfo=UTC)
    connection = FakeConnection(
        [
            FakeResult(rows=[]),
            FakeResult(scalar=None),
            FakeResult(rows=[]),
            FakeResult(scalar=0),
            FakeResult(rows=[]),
            FakeResult(rows=[{"uncertain": 0, "unmatched": 0, "callback_dead": 0}]),
            FakeResult(rows=[
                {"queue": "realtime", "count": 0},
                {"queue": "bulk", "count": 0},
            ]),
            FakeResult(rows=[]),
            FakeResult(rows=[
                {"key": "vendor_qps", "value": "8"},
                {"key": "reserved_realtime_qps", "value": "3"},
            ]),
        ]
    )
    engine = FakeEngine(connection)
    repository = SqlDashboardRepository(channel_timeout_s=0.001)
    repository._engine = lambda: engine  # type: ignore[method-assign]
    repository.redis = NeverRedis()  # type: ignore[attr-defined]

    facts = await asyncio.wait_for(
        repository.load("业务一部", now.date(), include_operations=True),
        timeout=0.2,
    )

    assert facts.operations is not None and facts.operations.channel_stale is True
    assert facts.operations.qps_used is None
    assert facts.operations.degraded_reason == "redis_unavailable"
    assert facts.operations.realtime_queue == 0
    assert facts.operations.bulk_queue == 0
    assert engine.disposed


@pytest.mark.asyncio
async def test_repository_keeps_queue_facts_but_marks_missing_token_snapshot_incomplete() -> None:
    connection = FakeConnection(
        [
            FakeResult(rows=[]),
            FakeResult(scalar=None),
            FakeResult(rows=[]),
            FakeResult(scalar=0),
            FakeResult(rows=[]),
            FakeResult(rows=[{"uncertain": 0, "unmatched": 0, "callback_dead": 0}]),
            FakeResult(rows=[
                {"queue": "realtime", "count": 4},
                {"queue": "bulk", "count": 9},
            ]),
            FakeResult(rows=[]),
            FakeResult(rows=[
                {"key": "vendor_qps", "value": "8"},
                {"key": "reserved_realtime_qps", "value": "3"},
            ]),
        ]
    )
    engine = FakeEngine(connection)
    repository = SqlDashboardRepository()
    repository._engine = lambda: engine  # type: ignore[method-assign]
    repository.redis = FakeRedis(tokens=None)  # type: ignore[attr-defined]

    facts = await repository.load("业务一部", date(2026, 7, 12), include_operations=True)

    assert facts.operations is not None
    assert (facts.operations.realtime_queue, facts.operations.bulk_queue) == (4, 9)
    assert facts.operations.qps_used is None
    assert facts.operations.channel_stale is True
    assert facts.operations.degraded_reason == "snapshot_incomplete"


@pytest.mark.asyncio
async def test_repository_treats_zero_queue_and_qps_usage_as_real_zero() -> None:
    connection = FakeConnection(
        [
            FakeResult(rows=[]),
            FakeResult(scalar=None),
            FakeResult(rows=[]),
            FakeResult(scalar=0),
            FakeResult(rows=[]),
            FakeResult(rows=[{"uncertain": 0, "unmatched": 0, "callback_dead": 0}]),
            FakeResult(rows=[
                {"queue": "realtime", "count": 0},
                {"queue": "bulk", "count": 0},
            ]),
            FakeResult(rows=[]),
            FakeResult(rows=[
                {"key": "vendor_qps", "value": "8"},
                {"key": "reserved_realtime_qps", "value": "3"},
            ]),
        ]
    )
    engine = FakeEngine(connection)
    repository = SqlDashboardRepository()
    repository._engine = lambda: engine  # type: ignore[method-assign]
    repository.redis = FakeRedis(tokens="8")  # type: ignore[attr-defined]

    facts = await repository.load("业务一部", date(2026, 7, 12), include_operations=True)

    assert facts.operations is not None
    assert (facts.operations.realtime_queue, facts.operations.bulk_queue) == (0, 0)
    assert facts.operations.qps_used == 0
    assert facts.operations.channel_stale is False
    assert facts.operations.degraded_reason is None


@pytest.mark.asyncio
async def test_non_admin_repository_skips_all_global_operational_queries() -> None:
    connection = FakeConnection(
        [
            FakeResult(rows=[]),
            FakeResult(scalar=2),
        ]
    )
    engine = FakeEngine(connection)
    repository = SqlDashboardRepository()
    repository._engine = lambda: engine  # type: ignore[method-assign]
    repository.redis = NeverRedis()  # type: ignore[attr-defined]

    facts = await repository.load(
        "业务一部",
        date(2026, 7, 12),
        include_operations=False,
    )

    assert facts.pending_approvals == 2
    assert facts.operations is None
    assert len(connection.calls) == 2
    sql = "\n".join(item[0] for item in connection.calls)
    for global_table in (
        "balance_snapshot",
        "alert_log",
        "job_run",
        "unmatched_report",
        "callback_task",
    ):
        assert global_table not in sql
