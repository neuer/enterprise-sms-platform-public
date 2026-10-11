"""发送结果事务重试、未知提交与固定错误分类边界。"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.exc import DBAPIError

from app.services.send_db_errors import (
    RolledBackSendDeadlock,
    SendCasLost,
    send_db_reason,
    send_sqlstate,
)
from app.tasks.send import FinalizeKind, FinalizeReport
from tests.test_send_repository import chunk_store


class DriverError(Exception):
    def __init__(self, state: str) -> None:
        self.sqlstate = state
        super().__init__("synthetic-sensitive-sql-and-credential")


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (SendCasLost("private"), "cas_lost"),
        (DriverError("40P01"), "db_deadlock"),
        (DriverError("55P03"), "db_timeout"),
        (DriverError("57014"), "db_timeout"),
        (TimeoutError("private"), "db_timeout"),
        (DriverError("08006"), "db_connection"),
        (ConnectionError("private"), "db_connection"),
        (DriverError("23514"), "unexpected"),
        (ValueError("private"), "unexpected"),
    ],
)
def test_fixed_error_classification(error: BaseException, reason: str) -> None:
    assert send_db_reason(error) == reason


def test_sqlstate_traverses_driver_wrappers_and_handles_cycles() -> None:
    error = DBAPIError("private sql", {"secret": "private"}, DriverError("40P01"))
    assert send_sqlstate(error) == "40P01"
    assert send_db_reason(error) == "db_deadlock"
    error.orig = error
    assert send_sqlstate(error) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failures", [1, 3])
async def test_only_confirmed_rollback_is_retried_with_bounded_attempts(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failures: int
) -> None:
    store = chunk_store()
    calls: list[dict[str, Any]] = []

    async def once(*args: Any, **kwargs: Any):
        calls.append(kwargs)
        if len(calls) <= failures:
            raise RolledBackSendDeadlock("synthetic-sensitive-sql-and-credential")
        return FinalizeReport(FinalizeKind.APPLIED, "submitted"), None

    monkeypatch.setattr(store, "_finalize_vendor_attempt_once", once)
    if failures == 3:
        with pytest.raises(RolledBackSendDeadlock):
            await store.finalize_vendor_attempt(
                9, 7, expected_generation=2, result="submitted", vendor_task_id="private-task"
            )
    else:
        report = await store.finalize_vendor_attempt(
            9, 7, expected_generation=2, result="submitted", vendor_task_id="private-task"
        )
        assert report.kind is FinalizeKind.APPLIED
    assert len(calls) == min(3, failures + 1)
    assert all(call == calls[0] for call in calls)
    assert "synthetic-sensitive" not in caplog.text
    assert "private-task" not in caplog.text
    assert all(record.reason == "db_deadlock" for record in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ConnectionError("private"), TimeoutError("private")])
async def test_unknown_commit_or_timeout_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    store = chunk_store()
    calls = 0

    async def once(*args: Any, **kwargs: Any):
        nonlocal calls
        calls += 1
        raise error

    monkeypatch.setattr(store, "_finalize_vendor_attempt_once", once)
    with pytest.raises(type(error)):
        await store.finalize_vendor_attempt(9, 7, expected_generation=2, result="submitted")
    assert calls == 1


@pytest.mark.asyncio
async def test_postcommit_delivery_deadlock_never_repeats_result_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = chunk_store()
    calls = 0

    async def once(*args: Any, **kwargs: Any):
        nonlocal calls
        calls += 1
        return FinalizeReport(FinalizeKind.APPLIED, "delayed"), (7, "notice", 1)

    async def enqueue(*args: Any, **kwargs: Any):
        raise RolledBackSendDeadlock("postcommit delivery failure")

    monkeypatch.setattr(store, "_finalize_vendor_attempt_once", once)
    monkeypatch.setattr(store, "_enqueue_retry", enqueue)
    with pytest.raises(RolledBackSendDeadlock):
        await store.finalize_vendor_attempt(9, 7, expected_generation=2, result="delayed")
    assert calls == 1


@pytest.fixture(autouse=True)
def no_external_send_observation(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*args: Any, **kwargs: Any):
        raise ConnectionError("synthetic observation unavailable")

    monkeypatch.setattr("app.services.send_db_events.database_engine", unavailable)
