from __future__ import annotations

from typing import Any

import pytest

from app.services.reconcile_repository import (
    STALE_INVOKING_CUTOFF_S,
    stale_invoking_cutoff_is_safe,
)
from app.tasks.send import (
    FinalizeKind,
    FinalizeReport,
    SendWorker,
    SubmitOutcome,
    classify_finalize_conflict,
    submit_outcome_from_finalize,
)
from app.tasks.send_repository import SqlChunkStore
from app.vendor.zhihui import VENDOR_TIMEOUT_S

from tests.test_send_repository import FakeEngine, FakeResult, SequenceConnection, chunk_store
from tests.test_send_worker import FakeBucket, FakeGateway, HistoryStore, chunk


def test_stale_invoking_cutoff_exceeds_vendor_absolute_timeout() -> None:
    assert stale_invoking_cutoff_is_safe()
    assert STALE_INVOKING_CUTOFF_S > VENDOR_TIMEOUT_S + 30 + 60


@pytest.mark.parametrize(
    ("attempt_outcome", "chunk_status", "requested", "expected"),
    [
        ("submitted", "submitted", "submitted", FinalizeKind.ALREADY_FINALIZED_SAME_RESULT),
        ("uncertain", "uncertain", "submitted", FinalizeKind.RECOVERY_MARKED_UNCERTAIN),
        ("submitted", "submitted", "uncertain", FinalizeKind.FINALIZED_DIFFERENT_RESULT),
        ("invoking", "submitting", "submitted", FinalizeKind.STATE_CORRUPTION),
        ("inconsistent", "submitted", "submitted", FinalizeKind.STATE_CORRUPTION),
    ],
)
def test_classify_finalize_conflict(
    attempt_outcome: str,
    chunk_status: str,
    requested: str,
    expected: FinalizeKind,
) -> None:
    assert (
        classify_finalize_conflict(
            attempt_outcome=attempt_outcome,
            chunk_status=chunk_status,
            requested=requested,
        )
        is expected
    )


def test_submit_outcome_from_finalize_never_reports_submitted_on_conflict() -> None:
    assert (
        submit_outcome_from_finalize(
            FinalizeReport(FinalizeKind.RECOVERY_MARKED_UNCERTAIN, "submitted"),
            SubmitOutcome.SUBMITTED,
        )
        is SubmitOutcome.UNCERTAIN
    )
    assert (
        submit_outcome_from_finalize(
            FinalizeReport(FinalizeKind.FINALIZED_DIFFERENT_RESULT, "submitted"),
            SubmitOutcome.SUBMITTED,
        )
        is SubmitOutcome.UNCERTAIN
    )
    assert (
        submit_outcome_from_finalize(
            FinalizeReport(FinalizeKind.LOST_CAS, "retry_scheduled"),
            SubmitOutcome.RETRY_SCHEDULED,
        )
        is SubmitOutcome.STALE
    )


@pytest.mark.asyncio
async def test_recovery_marked_uncertain_is_not_reported_submitted() -> None:
    store = HistoryStore()

    async def already_uncertain(
        attempt_id: int,
        chunk_id: int,
        **kwargs: Any,
    ) -> FinalizeReport:
        return FinalizeReport(FinalizeKind.RECOVERY_MARKED_UNCERTAIN, "submitted")

    store.finalize_vendor_attempt = already_uncertain  # type: ignore[method-assign]
    outcome = await SendWorker(
        FakeGateway(["task-ok"]),
        store,
        FakeBucket(),
    ).submit(chunk(), lane="realtime")
    assert outcome is SubmitOutcome.UNCERTAIN


@pytest.mark.asyncio
async def test_finalize_submitted_updates_attempt_and_chunk_in_one_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = chunk_store()
    connection = SequenceConnection(
        [
            FakeResult(row={"id": 9, "chunk_id": 7, "generation": 2, "outcome": "invoking"}),
            FakeResult(
                row={
                    "id": 7,
                    "status": "submitting",
                    "batch_id": 3,
                    "route_generation": 2,
                    "category": "notice",
                }
            ),
            FakeResult(),
            FakeResult(scalar=9),
            FakeResult(scalar=7),
            FakeResult(rowcount=1),
            FakeResult(),
        ]
    )
    monkeypatch.setattr(store, "_engine", lambda: FakeEngine(connection))

    report = await store.finalize_vendor_attempt(
        9,
        7,
        expected_generation=2,
        result="submitted",
        vendor_task_id="task-1",
    )

    assert report.kind is FinalizeKind.APPLIED
    statements = [sql for sql, _params in connection.calls]
    assert any("sms_vendor_attempt" in sql and "FOR UPDATE" in sql for sql in statements)
    assert any("sms_chunk" in sql and "FOR UPDATE" in sql for sql in statements)
    assert any("outcome=:outcome" in sql for sql in statements)
    assert any("status='submitted'" in sql for sql in statements)
    assert any("sms_message" in sql and "status='sent'" in sql for sql in statements)
    assert all("engine.begin" not in sql for sql in statements)


@pytest.mark.asyncio
async def test_finalize_conflict_does_not_mark_messages_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = chunk_store()
    connection = SequenceConnection(
        [
            FakeResult(row={"id": 9, "chunk_id": 7, "generation": 2, "outcome": "uncertain"}),
            FakeResult(
                row={
                    "id": 7,
                    "status": "uncertain",
                    "batch_id": 3,
                    "route_generation": 2,
                    "category": "notice",
                }
            ),
            FakeResult(),
        ]
    )
    monkeypatch.setattr(store, "_engine", lambda: FakeEngine(connection))

    report = await store.finalize_vendor_attempt(
        9,
        7,
        expected_generation=2,
        result="submitted",
        vendor_task_id="task-1",
    )

    assert report.kind is FinalizeKind.RECOVERY_MARKED_UNCERTAIN
    assert not any("sms_message" in sql for sql, _params in connection.calls)


def test_sql_chunk_store_exposes_atomic_finalize() -> None:
    assert hasattr(SqlChunkStore, "finalize_vendor_attempt")
