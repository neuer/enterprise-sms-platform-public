from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any

import pytest

from app.services.current_alerts import DatabaseCurrentFacts
from app.services.dashboard_repository import SqlDashboardRepository, _remaining_tokens
from app.services.runtime_policy import RuntimePolicy


def test_remaining_tokens_rejects_corrupt_snapshot_but_accepts_idle_bucket() -> None:
    assert _remaining_tokens(None, 8) == 8
    with pytest.raises(ValueError, match="token snapshot is invalid"):
        _remaining_tokens("not-a-number", 8)


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

    def first(self) -> dict[str, object] | None:
        return self.rows[0] if self.rows else None


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
            FakeResult(rows=[
                {"stat_date": date(2026, 7, 11), "category": "notice", "total": 5},
                {"stat_date": date(2026, 7, 12), "category": "notice", "total": 2},
            ]),
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
    assert [(item.stat_date, item.category, item.total) for item in facts.trend] == [
        (date(2026, 7, 11), "notice", 5),
        (date(2026, 7, 12), "notice", 2),
    ]
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
    trend_sql, trend_params = connection.calls[1]
    assert "BETWEEN :start_date AND :today" in trend_sql
    assert trend_params["start_date"] == date(2026, 7, 6)
    assert trend_params["dim_type"] == "dept" and trend_params["dim_value"] == "业务一部"
    balance_sql = connection.calls[3][0]
    assert "DISTINCT ON" in balance_sql and "Asia/Shanghai" in balance_sql
    pending_sql = connection.calls[4][0]
    assert "CAST(:scope_dept AS varchar(128)) IS NULL" in pending_sql
    count_sql = connection.calls[6][0]
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
async def test_repository_treats_absent_token_snapshot_as_idle_full_bucket() -> None:
    connection = FakeConnection(
        [
            FakeResult(rows=[]),
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
    assert facts.operations.qps_used == 0
    assert facts.operations.channel_stale is False
    assert facts.operations.degraded_reason is None


@pytest.mark.asyncio
async def test_repository_treats_zero_queue_and_qps_usage_as_real_zero() -> None:
    connection = FakeConnection(
        [
            FakeResult(rows=[]),
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
    assert len(connection.calls) == 3
    sql = "\n".join(item[0] for item in connection.calls)
    for global_table in (
        "balance_snapshot",
        "alert_log",
        "job_run",
        "unmatched_report",
        "callback_task",
    ):
        assert global_table not in sql


@pytest.mark.asyncio
async def test_current_facts_reuse_halves_dashboard_queries_and_preserves_unknown_balance() -> None:
    current = DatabaseCurrentFacts(
        policy=RuntimePolicy.from_mapping({"vendor_qps": "8", "reserved_realtime_qps": "3"}),
        jobs=(), usage_drift=(), balance=None, balance_checked_at=None,
        uncertain_overdue=1, uncertain_since=None, callback_dead=4,
        callback_dead_since=None, outbox_dead=0, outbox_dead_since=None,
        outbox_active=13, outbox_oldest_active_at=None,
        raw_manual=0, raw_manual_since=None, raw_spill_alerts=(),
        uncertain=2, realtime_queue=4, bulk_queue=9,
    )
    connection = FakeConnection([
        FakeResult(rows=[]), FakeResult(rows=[]), FakeResult(rows=[]),
        FakeResult(scalar=2), FakeResult(scalar=3),
    ])
    repository = SqlDashboardRepository()
    repository._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]
    repository.redis = FakeRedis()  # type: ignore[attr-defined]

    facts = await repository.load(
        None, date(2026, 7, 12), include_operations=True,
        current_facts=current, include_legacy_alerts=False,
    )

    assert len(connection.calls) == 5
    assert facts.pending_approvals == 2
    assert facts.operations is not None
    assert facts.operations.current_balance is None
    assert (facts.operations.uncertain, facts.operations.callback_dead) == (2, 4)
    assert (facts.operations.realtime_queue, facts.operations.bulk_queue) == (4, 9)
    assert facts.operations.unmatched == 3
    assert facts.operations.alerts == ()
    assert facts.operations.qps_used == 5
    sql = "\n".join(statement for statement, _ in connection.calls)
    for unused_table in (
        "alert_log", "job_run", "sms_chunk", "callback_task", "outbox_event", "sys_config",
    ):
        assert unused_table not in sql
    balance_sql, balance_params = connection.calls[2]
    assert "fetched_at>=:start_at AND fetched_at<:end_at" in balance_sql
    assert balance_params == {
        "start_at": datetime(2026, 6, 28, 16, tzinfo=UTC),
        "end_at": datetime(2026, 7, 12, 16, tzinfo=UTC),
    }
    with pytest.raises(ValueError, match="unscoped"):
        await repository.load(
            "部门", date(2026, 7, 12), include_operations=True, current_facts=current,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("balance", [None, 0, 9000])
async def test_lightweight_balance_reads_only_latest_snapshot(balance: int | None) -> None:
    checked = datetime(2026, 7, 12, tzinfo=UTC)
    connection = FakeConnection([FakeResult(
        rows=[] if balance is None else [{"balance": balance, "fetched_at": checked}]
    )])
    repository = SqlDashboardRepository()
    repository._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]
    repository.redis = NeverRedis()  # type: ignore[attr-defined]

    snapshot = await repository.load_balance()

    assert snapshot.current_balance == balance
    assert snapshot.checked_at == (checked if balance is not None else None)
    assert len(connection.calls) == 1
    assert "FROM balance_snapshot" in connection.calls[0][0]
    assert "LIMIT 1" in connection.calls[0][0]
