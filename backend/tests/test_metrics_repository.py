from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from app.services.metrics import MetricsFacts
from app.services.metrics_repository import SqlMetricsRepository


class FakeResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

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

    async def scalar(self, statement: object, params: Any = None) -> object:
        result = await self.execute(statement, params)
        if not result.rows:
            return 0
        return next(iter(result.rows[0].values()))


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
async def test_repository_loads_all_metrics_from_aggregate_facts_only() -> None:
    connection = FakeConnection(
        [
            FakeResult([{"category": "verify", "rate": 1.5}]),
            FakeResult([{"code": "999", "count": 2}]),
            FakeResult(
                [
                    {
                        "uncertain": 3,
                        "uncertain_active": 1,
                        "uncertain_overdue": 2,
                        "unknown_terminal": 4,
                        "manual_resolved": 5,
                        "late_evidence": 6,
                        "callback_retrying": 4,
                        "callback_dead": 5,
                        "callback_stalled": 2,
                        "export_stalled": 1,
                    }
                ]
            ),
            FakeResult(
                [
                    {"queue": "bulk", "count": 9},
                    {"queue": "realtime", "count": 3},
                ]
            ),
            FakeResult(
                [
                    {
                        "task_kind": "callback",
                        "event_type": "fencing_miss",
                        "count": 3,
                    },
                    {
                        "task_kind": "export",
                        "event_type": "takeover",
                        "count": 2,
                    },
                ]
            ),
            FakeResult([{"category": "market", "count": 6}]),
            FakeResult([{"source": "report", "lag_seconds": 7.5}]),
            FakeResult(
                [
                    {
                        "kind": "frequency",
                        "mismatched_dimensions": 2,
                        "absolute_delta": 3,
                    },
                    {
                        "kind": "quota",
                        "mismatched_dimensions": 0,
                        "absolute_delta": 0,
                    },
                ]
            ),
            FakeResult(
                [
                    {"outcome": "submitted", "count": 4},
                    {"outcome": "uncertain", "count": 1},
                ]
            ),
            FakeResult(
                [
                    {"replay_eligibility": "automatic", "count": 4},
                    {"replay_eligibility": "manual", "count": 2},
                    {"replay_eligibility": "never", "count": 7},
                ]
            ),
            FakeResult([{"count": 0}]),
            FakeResult(
                [
                    {
                        "source_channel": "web",
                        "action": "resend_new_batch",
                        "result": "applied",
                        "count": 2,
                    }
                ]
            ),
            FakeResult([{"kind": "usage_subject_invalid", "count": 1}]),
            FakeResult([{"oldest": 12.0}]),
            FakeResult([{"count": 3}]),
        ]
    )
    engine = FakeEngine(connection)
    repository = SqlMetricsRepository()
    repository._engine = lambda: engine  # type: ignore[method-assign]

    facts = await repository.load()

    assert facts == MetricsFacts(
        send_rates=(("verify", 1.5),),
        vendor_errors=(("999", 2),),
        uncertain=3,
        uncertain_lifecycle=(
            ("active", 1),
            ("overdue", 2),
            ("unknown_terminal", 4),
            ("manual_resolved", 5),
            ("late_evidence", 6),
        ),
        callback_failures=(("retrying", 4), ("dead", 5)),
        frequency_filtered=(("market", 6),),
        poll_lags=(("report", 7.5),),
        usage_projection_mismatches=(("frequency", 2), ("quota", 0)),
        usage_projection_absolute_delta=(("frequency", 3), ("quota", 0)),
        worker_stalled_leases=(("callback", 2), ("export", 1)),
        worker_lease_events=(
            ("callback", "fencing_miss", 3),
            ("export", "takeover", 2),
        ),
        queue_depths=(("bulk", 9), ("realtime", 3)),
        raw_replay_eligibility=(
            ("automatic", 4),
            ("manual", 2),
            ("never", 7),
        ),
        send_submit_outcomes=(("submitted", 4), ("uncertain", 1)),
        uncertain_effects=(("web", "resend_new_batch", "applied", 2),),
        uncertain_effect_usage_subject_errors=(("usage_subject_invalid", 1),),
        uncertain_effect_oldest_pending_seconds=12.0,
        uncertain_effect_child_recovered=3,
    )
    assert not engine.disposed
    sql = "\n".join(call[0] for call in connection.calls).casefold()
    assert "interval '5 minutes'" in sql and "submitted_at" in sql
    assert "status in ('failed','retrying')" in sql and "vendor_code" in sql
    assert "status='uncertain'" in sql
    assert "interval '24 hours'" in sql
    assert "late_evidence_at" in sql
    assert "sms_uncertain_resolution" in sql
    assert "unknown_terminal" in sql
    assert "callback_task" in sql and "status='dead'" in sql
    assert "from outbox_event" in sql
    assert "state in ('pending','leased','published','processing')" in sql
    assert "outbox_oldest_age" in sql
    assert "created_at" in sql
    assert facts.send_admission_state == "open"
    assert facts.outbox_oldest_age_seconds == 0.0
    assert "asia/shanghai" in sql and "removed_freq" in sql
    assert "poll_report" in sql and "poll_reply" in sql and "status='success'" in sql
    assert "replay_eligibility" in sql and "count(replay_eligibility)" in sql
    assert "system_replay_audit_state" in sql
    assert "sms_vendor_attempt" in sql and "outcome" in sql
    assert "sms_uncertain_child" in sql and "recovered" in sql
    assert "source_channel" in sql and "effect_error" in sql
    for forbidden in ("phone_enc", "phone_hmac", "phone_mask", "vendor_msg", "content"):
        assert forbidden not in sql


@pytest.mark.asyncio
async def test_repository_clamps_clock_skew_and_orders_labels() -> None:
    connection = FakeConnection(
        [
            FakeResult(
                [
                    {"category": "market", "rate": -0.1},
                    {"category": "notice", "rate": 2},
                ]
            ),
            FakeResult(
                [
                    {"code": "5002", "count": 2},
                    {"code": "999", "count": 1},
                ]
            ),
            FakeResult(
                [
                    {
                        "uncertain": 0,
                        "uncertain_active": 0,
                        "uncertain_overdue": 0,
                        "unknown_terminal": 0,
                        "manual_resolved": 0,
                        "late_evidence": 0,
                        "callback_retrying": 0,
                        "callback_dead": 0,
                    }
                ]
            ),
            FakeResult([]),
            FakeResult([]),
            FakeResult([]),
            FakeResult(
                [
                    {"source": "reply", "lag_seconds": -3},
                    {"source": "report", "lag_seconds": 4},
                ]
            ),
            FakeResult([]),
            FakeResult([]),
            FakeResult([]),
            FakeResult([{"count": 0}]),
            FakeResult([]),
            FakeResult([]),
            FakeResult([{"oldest": 0}]),
            FakeResult([{"count": 0}]),
        ]
    )
    repository = SqlMetricsRepository()
    repository._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]

    facts = await repository.load()

    assert facts.send_rates == (("market", 0.0), ("notice", 2.0))
    assert facts.vendor_errors == (("5002", 2), ("999", 1))
    assert facts.poll_lags == (("reply", 0.0), ("report", 4.0))
