"""批次终态快照：旧计数不能改写已投递事件，补发必须是新 event_id。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

import app.services.callback_batch_event as recovery_module
from app.core.auth.accounts import SecurityPrincipal
from app.services.callback_batch_event import (
    CallbackSnapshotUnavailable,
    FinishedRecoveryRejected,
    FinishedRecoveryService,
    batch_from_snapshot,
)
from app.services.callback_retry_eligibility import (
    RETRY_BLOCK_REASON_SQL,
    diagnosis_sql,
)

EVENT_ID = UUID("40000000-0000-4000-8000-000000000021")
CORRELATION_ID = UUID("50000000-0000-4000-8000-000000000021")
ADMIN = SecurityPrincipal(1, 10, "admin", "平台部", "admin")
ROOT = Path(__file__).resolve().parents[2]


class FakeResult:
    def __init__(self, rows: list[dict[str, object]] | None = None) -> None:
        self.rows = rows or []

    def mappings(self) -> FakeResult:
        return self

    def one_or_none(self) -> dict[str, object] | None:
        assert len(self.rows) <= 1
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

    async def __aexit__(self, *_exc: object) -> None:
        return None


class FakeEngine:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def begin(self) -> FakeContext:
        return FakeContext(self.connection)

    async def dispose(self) -> None:
        return None


class StubSettings:
    def database_url_for(self, role: str) -> str:
        assert role == "callback"
        return "postgresql+asyncpg://callback"


def _service(
    connection: FakeConnection,
    monkeypatch: pytest.MonkeyPatch,
) -> FinishedRecoveryService:
    monkeypatch.setattr(
        recovery_module,
        "database_engine",
        lambda _url: FakeEngine(connection),
    )
    return FinishedRecoveryService(StubSettings())  # type: ignore[arg-type]


def test_snapshot_body_uses_only_the_frozen_row() -> None:
    early = datetime(2026, 7, 12, 8, 0, tzinfo=UTC)
    late = datetime(2026, 7, 13, 9, 0, tzinfo=UTC)
    first = batch_from_snapshot(
        {
            "batch_no": "BATCH-1",
            "biz_id": "biz-1",
            "category": "notice",
            "status": "completed",
            "total": 3,
            "delivered": 1,
            "failed": 2,
            "unknown": 0,
            "finished_at": early,
            "legacy_unavailable": False,
            "manual_resolution_required": False,
        }
    )
    second = batch_from_snapshot(
        {
            "batch_no": "BATCH-1",
            "biz_id": "biz-1",
            "category": "notice",
            "status": "completed",
            "total": 3,
            "delivered": 3,
            "failed": 0,
            "unknown": 0,
            "finished_at": late,
            "legacy_unavailable": False,
            "manual_resolution_required": False,
        }
    )

    assert first.delivered == 1 and first.finished_at == early
    assert second.delivered == 3 and second.finished_at == late
    assert first.delivered == 1


def test_legacy_row_is_not_delivered_even_when_counts_are_present() -> None:
    with pytest.raises(CallbackSnapshotUnavailable):
        batch_from_snapshot(
            {
                "batch_no": "BATCH-1",
                "biz_id": None,
                "category": "notice",
                "status": "completed",
                "total": 9,
                "delivered": 9,
                "failed": 0,
                "unknown": 0,
                "finished_at": datetime(2026, 7, 12, 8, 0, tzinfo=UTC),
                "legacy_unavailable": True,
                "manual_resolution_required": False,
            }
        )


def test_legacy_reason_is_evaluated_before_dead_state() -> None:
    assert "e.legacy_unavailable" in RETRY_BLOCK_REASON_SQL
    assert RETRY_BLOCK_REASON_SQL.index("legacy_snapshot_unavailable") < (
        RETRY_BLOCK_REASON_SQL.index("not_dead")
    )
    assert "callback_batch_event e" in diagnosis_sql()


def test_migration_does_not_copy_current_counts_onto_attempted_events() -> None:
    text = (ROOT / "backend/migrations/versions/0134_callback_batch_event.py").read_text(
        encoding="utf-8"
    )
    legacy = text.split("legacy marker", 1)[1].split("UPDATE callback_task", 1)[0]
    assert "total" not in legacy
    assert "updated_at" not in text
    assert "t.created_at" in legacy
    assert "retry_count=0" in text
    assert "last_http_code IS NULL" in text
    assert "LegacySnapshotUnavailable" in text
    assert "status IN ('pending','retrying')" in text


@pytest.mark.asyncio
async def test_recovery_inserts_a_new_event_without_updating_the_old_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = {"id": 9, "batch_id": 8, "event": "batch.finished", "last_error": None}
    connection = FakeConnection(
        [
            FakeResult([task]),
            FakeResult([{"id": 8}]),
            FakeResult([task]),
            FakeResult([{"legacy_unavailable": True}]),
            FakeResult(),
            FakeResult([{"id": 21, "event_id": EVENT_ID, "correlation_id": CORRELATION_ID}]),
        ]
    )
    outbox: list[object] = []
    audits: list[object] = []

    async def record_outbox(_connection: object, spec: object) -> None:
        outbox.append(spec)

    async def record_audit(_connection: object, event: object) -> None:
        audits.append(event)

    monkeypatch.setattr(recovery_module, "enqueue_outbox", record_outbox)
    monkeypatch.setattr(recovery_module, "insert_audit", record_audit)
    result = await _service(connection, monkeypatch).recover(9, principal=ADMIN)

    assert result.task_id == 21 and result.event_id == EVENT_ID and result.created is True
    assert len(connection.calls) == 6
    sqls = [call[0] for call in connection.calls]
    batch_lock = next(i for i, sql in enumerate(sqls) if "lock_callback_idempotency_batch" in sql)
    task_lock = next(
        i for i, sql in enumerate(sqls) if "callback_task t" in sql and "FOR UPDATE" in sql
    )
    assert batch_lock < task_lock
    sql = "\n".join(sqls)
    assert "INSERT INTO callback_task" in sql
    assert "INSERT INTO callback_batch_event" in sql
    assert "recovered_from_task_id" in sql
    assert "clock_timestamp()" in sql
    assert "UPDATE callback_batch_event" not in sql
    assert "updated_at" not in sql
    assert len(outbox) == 1
    assert outbox[0].dedup_key == "callback:21:attempt:0"  # type: ignore[attr-defined]
    after = audits[0].after  # type: ignore[attr-defined]
    assert after == {
        "source_task_id": 9,
        "event_id": str(EVENT_ID),
        "created": True,
    }
    assert "phone" not in str(after)


@pytest.mark.asyncio
async def test_second_recovery_returns_the_existing_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = {"id": 9, "batch_id": 8, "event": "batch.finished", "last_error": None}
    connection = FakeConnection(
        [
            FakeResult([task]),
            FakeResult([{"id": 8}]),
            FakeResult([task]),
            FakeResult([{"legacy_unavailable": True}]),
            FakeResult([{"id": 21, "event_id": EVENT_ID}]),
        ]
    )
    outbox: list[object] = []

    async def record_outbox(_connection: object, spec: object) -> None:
        outbox.append(spec)

    async def record_audit(_connection: object, _event: object) -> None:
        return None

    monkeypatch.setattr(recovery_module, "enqueue_outbox", record_outbox)
    monkeypatch.setattr(recovery_module, "insert_audit", record_audit)
    result = await _service(connection, monkeypatch).recover(9, principal=ADMIN)

    assert result.created is False and result.task_id == 21
    assert outbox == []
    assert len(connection.calls) == 5
    assert all("INSERT" not in call[0] for call in connection.calls)


@pytest.mark.asyncio
async def test_current_snapshot_cannot_be_recovered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = {"id": 9, "batch_id": 8, "event": "batch.finished", "last_error": None}
    connection = FakeConnection(
        [
            FakeResult([task]),
            FakeResult([{"id": 8}]),
            FakeResult([task]),
            FakeResult([{"legacy_unavailable": False}]),
        ]
    )
    with pytest.raises(FinishedRecoveryRejected, match="已有冻结汇总") as blocked:
        await _service(connection, monkeypatch).recover(9, principal=ADMIN)
    assert blocked.value.reason == "not_legacy"
    assert len(connection.calls) == 4


@pytest.mark.asyncio
async def test_revoked_legacy_recovery_stops_before_snapshot_or_existing_event_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """撤销优先于快照缺失；不能借补发入口重新创建已撤销的事件。"""

    task = {
        "id": 9,
        "batch_id": 8,
        "event": "batch.finished",
        "last_error": "CallbackConfigRevoked",
    }
    connection = FakeConnection(
        [
            FakeResult([task]),
            FakeResult([{"id": 8}]),
            FakeResult([task]),
        ]
    )
    with pytest.raises(FinishedRecoveryRejected) as blocked:
        await _service(connection, monkeypatch).recover(9, principal=ADMIN)
    assert blocked.value.reason == "config_revoked"
    assert len(connection.calls) == 3
    assert all("INSERT" not in sql for sql, _ in connection.calls)
