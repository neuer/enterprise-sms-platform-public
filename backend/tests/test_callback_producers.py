from __future__ import annotations

import base64
from collections.abc import Iterator
from datetime import datetime
from typing import Any, TypedDict, cast
from uuid import UUID

import pytest

import app.services.approval_repository as approval_repository_module
import app.services.scheduling_repository as scheduling_repository_module
from app.core.auth.accounts import SecurityPrincipal
from app.services.approval_repository import SqlApprovalRepository
from app.services.batch_query import BatchAccessScope
from app.services.callback_repository import (
    enqueue_batch_finished,
    enqueue_message_report,
    report_event_key,
)
from app.services.crypto import CryptoService, EncryptionContext
from app.services.scheduling_repository import SqlSchedulingRepository

APPROVER = SecurityPrincipal(2, 20, "approver01", "研发部", "approver")


class FakeResult:
    def __init__(
        self,
        scalar: object = None,
        rows: list[dict[str, object]] | None = None,
        scalar_values: list[object] | None = None,
    ) -> None:
        self.scalar = scalar
        self.rows = rows or []
        self.scalar_values = scalar_values or []

    def scalar_one_or_none(self) -> object:
        return self.scalar

    def mappings(self) -> FakeResult:
        return self

    def scalars(self) -> Iterator[object]:
        return iter(self.scalar_values)

    def one(self) -> dict[str, object]:
        assert len(self.rows) == 1
        return self.rows[0]

    def one_or_none(self) -> dict[str, object] | None:
        assert len(self.rows) <= 1
        return self.rows[0] if self.rows else None

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self.rows)


class ReportEventArgs(TypedDict):
    message_id: int
    message_created_at: datetime
    custom_id: str
    report_status: int
    report_desc: str
    report_time: datetime


class FakeConnection:
    def __init__(self, results: list[FakeResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, Any]] = []

    async def execute(self, statement: object, params: Any = None) -> FakeResult:
        self.calls.append((str(statement), params))
        return self.results.pop(0)

    async def scalar(self, statement: object, params: Any = None) -> object:
        self.calls.append((str(statement), params))
        return self.results.pop(0).scalar


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

    def begin(self) -> FakeContext:
        return FakeContext(self.connection)

    def connect(self) -> FakeContext:
        return FakeContext(self.connection)

    async def dispose(self) -> None:
        return None


def test_report_event_key_is_stable_non_pii_and_binds_all_event_fields() -> None:
    created_at = datetime.fromisoformat("2026-07-01T23:59:59.123456+08:00")
    report_time = datetime.fromisoformat("2026-07-02T00:00:01.654321+08:00")
    values: ReportEventArgs = {
        "message_id": 21,
        "message_created_at": created_at,
        "custom_id": "custom-1",
        "report_status": 1,
        "report_desc": "DELIVRD",
        "report_time": report_time,
    }
    first = report_event_key(**values)
    assert first == report_event_key(**values)
    assert len(first) == 64
    assert "DELIVRD" not in first and "custom-1" not in first
    assert (
        report_event_key(
            **cast(
                ReportEventArgs,
                {
                    **values,
                    "custom_id": "  custom-1  ",
                },
            )
        )
        == first
    )
    for changed in (
        {"report_status": 2},
        {"report_desc": "FAILED"},
        {"custom_id": "custom-2"},
        {"report_time": created_at},
    ):
        assert report_event_key(**cast(ReportEventArgs, {**values, **changed})) != first


@pytest.mark.asyncio
async def test_batch_finished_task_is_idempotent_reference_only() -> None:
    connection = FakeConnection([FakeResult()])

    await enqueue_batch_finished(connection, 8)

    sql, params = connection.calls[0]
    assert "pg_advisory_xact_lock" in sql
    assert "batch.finished" in sql
    assert "WHERE CAST(:source_report_event_key AS char(64)) IS NOT NULL" in sql
    assert "WHERE NOT EXISTS" not in sql
    assert "OR NOT EXISTS" in sql
    assert "ON CONFLICT DO NOTHING" in sql
    assert "source_report_event_key" in sql
    assert params["batch_id"] == "8"
    assert params["source_report_event_key"] is None
    assert isinstance(params["correlation_id"], UUID)
    lowered = sql.casefold()
    assert "payload" not in lowered and "body" not in lowered
    assert "phone_enc" not in lowered and "phone_hmac" not in lowered


@pytest.mark.asyncio
async def test_batch_finished_report_revision_is_bound_to_event_key() -> None:
    connection = FakeConnection([FakeResult()])

    await enqueue_batch_finished(
        connection,
        8,
        source_report_event_key="a" * 64,
    )

    assert connection.calls[0][1]["batch_id"] == "8"
    assert connection.calls[0][1]["source_report_event_key"] == "a" * 64
    assert isinstance(connection.calls[0][1]["correlation_id"], UUID)


@pytest.mark.asyncio
async def test_message_report_appends_reference_to_current_pending_minute_bucket() -> None:
    connection = FakeConnection([FakeResult(), FakeResult("a" * 64), FakeResult(12)])
    created_at = datetime.fromisoformat("2026-07-12T08:00:00+08:00")

    await enqueue_message_report(
        connection, batch_id=8, message_id=21, created_at=created_at, event_key="a" * 64
    )

    assert len(connection.calls) == 3
    lock_sql, lock_params = connection.calls[0]
    dedup_sql, _ = connection.calls[1]
    update_sql, params = connection.calls[2]
    assert "pg_advisory_xact_lock" in lock_sql
    assert "INSERT INTO callback_report_event" in dedup_sql
    assert "ON CONFLICT(event_key) DO NOTHING" in dedup_sql
    assert "message_status,report_desc,report_time" in dedup_sql
    assert "status='pending'" not in dedup_sql
    assert lock_params == {"batch_id": "8"}
    assert "array_append(message_ids" in update_sql
    assert "array_append(message_times" in update_sql
    assert "cardinality(message_ids)<500" in update_sql
    assert "status='pending'" in update_sql
    assert "callback_report_enabled=true" in update_sql
    assert params == {
        "batch_id": 8,
        "message_id": 21,
        "created_at": created_at,
        "event_key": "a" * 64,
        "message_status": "unknown",
        "report_desc": "",
        "report_time": created_at,
    }


@pytest.mark.asyncio
async def test_message_report_creates_reference_only_task_when_bucket_is_full_or_missing() -> None:
    connection = FakeConnection(
        [FakeResult(), FakeResult("a" * 64), FakeResult(None), FakeResult()]
    )
    created_at = datetime.fromisoformat("2026-07-12T08:00:00+08:00")

    await enqueue_message_report(
        connection, batch_id=8, message_id=21, created_at=created_at, event_key="a" * 64
    )

    insert_sql, params = connection.calls[3]
    assert "message.report" in insert_sql
    assert "ARRAY[:message_id]" in insert_sql
    assert "ARRAY[:created_at]" in insert_sql
    assert params["event_key"] == "a" * 64
    lowered = insert_sql.casefold()
    assert "payload" not in lowered and "body" not in lowered
    assert "phone_enc" not in lowered and "phone_hmac" not in lowered


@pytest.mark.asyncio
async def test_message_report_replay_skips_reference_in_any_task_state() -> None:
    connection = FakeConnection([FakeResult(), FakeResult(None)])
    event_time = datetime.fromisoformat("2026-07-12T08:00:00+08:00")

    await enqueue_message_report(
        connection,
        batch_id=8,
        message_id=21,
        created_at=event_time,
        event_key="a" * 64,
    )

    assert len(connection.calls) == 2
    dedup_sql = connection.calls[1][0]
    assert "t.status" not in dedup_sql
    assert "ON CONFLICT(event_key) DO NOTHING" in dedup_sql


@pytest.mark.asyncio
async def test_message_report_twice_in_one_transaction_appends_once() -> None:
    connection = FakeConnection(
        [FakeResult(), FakeResult("a" * 64), FakeResult(12), FakeResult(), FakeResult(None)]
    )
    event_time = datetime.fromisoformat("2026-07-12T08:00:00+08:00")

    for _ in range(2):
        await enqueue_message_report(
            connection,
            batch_id=8,
            message_id=21,
            created_at=event_time,
            event_key="a" * 64,
        )

    updates = [sql for sql, _params in connection.calls if "array_append" in sql]
    assert len(updates) == 1
    assert "pg_advisory_xact_lock" in connection.calls[0][0]
    assert "pg_advisory_xact_lock" in connection.calls[3][0]


@pytest.mark.asyncio
async def test_different_report_time_is_a_distinct_callback_event() -> None:
    connection = FakeConnection(
        [
            FakeResult(),
            FakeResult("a" * 64),
            FakeResult(12),
            FakeResult(),
            FakeResult("b" * 64),
            FakeResult(12),
        ]
    )
    first = datetime.fromisoformat("2026-07-12T08:00:00+08:00")
    second = datetime.fromisoformat("2026-07-12T08:01:00+08:00")

    await enqueue_message_report(
        connection, batch_id=8, message_id=21, created_at=first, event_key="a" * 64
    )
    await enqueue_message_report(
        connection, batch_id=8, message_id=21, created_at=second, event_key="b" * 64
    )

    updates = [params for sql, params in connection.calls if "array_append" in sql]
    assert [params["created_at"] for params in updates] == [first, second]


@pytest.mark.asyncio
async def test_approval_expiry_repository_commits_state_and_callback_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = {
        "batch_id": 8,
        "approval_id": 3,
        "batch_no": "a" * 32,
        "applicant": "operator01",
        "applicant_account_id": 1,
        "applicant_identity_id": 10,
        "app_id": None,
        "dept": "业务一部",
        "quota_date": "20260712",
        "quota_cost": 1,
        "category": "market",
        "status": "expired",
        "batch_status": "pending_approval",
    }
    connection = FakeConnection(
        [FakeResult(scalar_values=[3]), FakeResult(rows=[row]), FakeResult()]
    )
    repository = SqlApprovalRepository()
    repository._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]
    callback_batches: list[int] = []
    outbox_events: list[Any] = []

    async def callback(_connection: object, batch_id: int) -> None:
        callback_batches.append(batch_id)

    async def outbox(_connection: object, spec: Any, **_: object) -> object:
        outbox_events.append(spec)
        return object()

    async def release(_connection: object, **_: object) -> bool:
        return False

    monkeypatch.setattr(approval_repository_module, "enqueue_batch_finished", callback)
    monkeypatch.setattr(approval_repository_module, "enqueue_outbox", outbox)
    monkeypatch.setattr(
        approval_repository_module,
        "request_usage_release_for_batch",
        release,
    )

    expired = await repository.expire_due()

    assert len(expired) == 1 and callback_batches == [8]
    assert len(outbox_events) == 1
    assert outbox_events[0].event_type == "quota.compensation"
    assert outbox_events[0].args == (
        0,
        "业务一部",
        "market",
        "20260712",
        1,
        "approval:3:expired",
    )
    assert outbox_events[0].dedup_key == "approval:3:expired"
    sql = " ".join(call[0] for call in connection.calls)
    assert "INSERT INTO alert_log" not in sql


@pytest.mark.asyncio
async def test_approval_expiry_failure_isolated_to_one_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    successful = {
        "batch_id": 9,
        "approval_id": 4,
        "batch_no": "b" * 32,
        "applicant": "operator02",
        "applicant_account_id": 2,
        "applicant_identity_id": 20,
        "app_id": 7,
        "dept": "业务二部",
        "quota_date": "20260712",
        "quota_cost": 1,
        "category": "notice",
        "status": "expired",
        "batch_status": "pending_approval",
    }

    class IsolatedFailureConnection(FakeConnection):
        async def execute(self, statement: object, params: Any = None) -> FakeResult:
            sql = str(statement)
            self.calls.append((sql, params))
            if "SELECT id FROM approval" in sql:
                return FakeResult(scalar_values=[3, 4])
            if "UPDATE approval p SET status='expired'" in sql:
                if params["approval_id"] == 3:
                    raise RuntimeError("synthetic item failure")
                return FakeResult(rows=[successful])
            return FakeResult()

    connection = IsolatedFailureConnection([])
    repository = SqlApprovalRepository()
    repository._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]
    callbacks: list[int] = []

    async def callback(_connection: object, batch_id: int) -> None:
        callbacks.append(batch_id)

    async def release(_connection: object, **_: object) -> bool:
        return True

    monkeypatch.setattr(approval_repository_module, "enqueue_batch_finished", callback)
    monkeypatch.setattr(
        approval_repository_module,
        "request_usage_release_for_batch",
        release,
    )

    expired = await repository.expire_due()

    assert [item.approval_id for item in expired] == [4]
    assert callbacks == [9]


@pytest.mark.asyncio
async def test_approval_list_interpolates_scoped_source_clause() -> None:
    key = base64.b64encode(b"p" * 32).decode()
    crypto = CryptoService.from_secret_values(key, key)
    batch_no = "a" * 32
    row = {
        "id": 3,
        "batch_no": batch_no,
        "category": "market",
                            "applicant": "operator01",
                            "applicant_account_id": 1,
                            "applicant_identity_id": 10,
        "dept": "业务一部",
        "total": 60,
        "display_content_enc": crypto.encrypt_bound_packed_text(
            "审批内容",
            EncryptionContext(
                domain="sms-display-content",
                table="sms_batch",
                column="display_content_enc",
                object_id=batch_no,
            ),
        ),
        "status": "pending",
        "approver": None,
        "reason": None,
        "created_at": datetime.fromisoformat("2026-07-12T08:00:00+08:00"),
    }
    connection = FakeConnection([FakeResult(1), FakeResult(rows=[row])])
    repository = SqlApprovalRepository(crypto=crypto)
    repository._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]

    page = await repository.list_page(status="pending", dept="业务一部", page=1)

    assert page["total"] == 1
    select_sql, params = connection.calls[1]
    assert "{source}" not in select_sql
    assert "FROM approval p JOIN sms_batch b" in select_sql
    assert "AND p.dept ILIKE :dept" in select_sql
    assert params["dept"] == "业务一部"


@pytest.mark.asyncio
async def test_approval_rejection_enqueues_batch_finished_in_same_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[int] = []
    outbox_events: list[Any] = []

    async def enqueue(_connection: object, batch_id: int) -> None:
        events.append(batch_id)

    async def outbox(_connection: object, spec: Any, **_: object) -> object:
        outbox_events.append(spec)
        return object()

    async def release(_connection: object, **_: object) -> bool:
        return False

    monkeypatch.setattr(approval_repository_module, "enqueue_batch_finished", enqueue)
    monkeypatch.setattr(approval_repository_module, "enqueue_outbox", outbox)
    monkeypatch.setattr(
        approval_repository_module,
        "request_usage_release_for_batch",
        release,
    )
    repository = SqlApprovalRepository()
    connection = FakeConnection(
        [
            FakeResult(8),
            FakeResult(),
            FakeResult(),
            FakeResult(
                rows=[
                    {
                        "approval_id": 3,
                        "batch_no": "BATCH-1",
                        "applicant": "operator01",
                        "applicant_account_id": 1,
                        "applicant_identity_id": 10,
                        "app_id": 7,
                        "dept": "研发部",
                        "quota_date": "20260712",
                        "quota_cost": 1,
                        "category": "notice",
                        "status": "rejected",
                        "batch_status": "rejected",
                    }
                ]
            ),
        ]
    )
    repository._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]

    assert (
        await repository.transition(
            3,
            action="reject",
            principal=APPROVER,
            reason="不合规",
        )
        is not None
    )
    assert events == [8]
    assert len(outbox_events) == 1
    assert outbox_events[0].event_type == "quota.compensation"
    assert outbox_events[0].args == (
        7,
        "研发部",
        "notice",
        "20260712",
        1,
        "approval:3:rejected",
    )
    assert outbox_events[0].dedup_key == "approval:3:rejected"
    audit_sql = connection.calls[2][0]
    assert "'decision',CAST(:decision AS text)" in audit_sql
    assert "'actor_account_id',CAST(:actor_account_id AS bigint)" in audit_sql


@pytest.mark.asyncio
async def test_scheduled_cancel_enqueues_batch_finished_in_same_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[int] = []
    outbox_events: list[Any] = []

    async def enqueue(_connection: object, batch_id: int) -> None:
        events.append(batch_id)

    async def outbox(_connection: object, spec: Any, **_: object) -> object:
        outbox_events.append(spec)
        return object()

    async def release(_connection: object, **_: object) -> bool:
        return False

    monkeypatch.setattr(scheduling_repository_module, "enqueue_batch_finished", enqueue)
    monkeypatch.setattr(scheduling_repository_module, "enqueue_outbox", outbox)
    monkeypatch.setattr(
        scheduling_repository_module,
        "request_usage_release_for_batch",
        release,
    )
    repository = SqlSchedulingRepository()
    connection = FakeConnection(
        [
            FakeResult(
                rows=[
                    {
                        "id": 8,
                        "batch_no": "BATCH-1",
                        "category": "market",
                        "app_id": 7,
                        "dept": "研发部",
                        "quota_date": "20260712",
                        "quota_cost": 1,
                    }
                ]
            ),
            FakeResult(),
        ]
    )
    repository._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]

    assert await repository.cancel("BATCH-1", BatchAccessScope(all_departments=True))
    assert events == [8]
    assert len(outbox_events) == 1
    assert outbox_events[0].event_type == "quota.compensation"
    assert outbox_events[0].args == (
        7,
        "研发部",
        "market",
        "20260712",
        1,
        "batch:BATCH-1:cancelled",
    )
    assert outbox_events[0].dedup_key == "batch:BATCH-1:cancelled"
