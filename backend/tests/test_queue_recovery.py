from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from app.services.ops import (
    PausedBatch,
    QueueRecoveryService,
    QueueResumeConflict,
    QueueSnapshot,
)
from app.services.ops_repository import SqlOpsRepository


class FakeRepository:
    def __init__(self, snapshot: QueueSnapshot, events: list[str]) -> None:
        self.snapshot = snapshot
        self.events = events

    async def queue_snapshot(self) -> QueueSnapshot:
        return self.snapshot

    async def resume_batches(self, *, actor: str, ip: str) -> tuple[PausedBatch, ...]:
        self.events.append(f"db:{actor}:{ip}")
        return (
            PausedBatch("NOTICE-1", "notice"),
            PausedBatch("MARKET-1", "market"),
        )

    async def clear_queue_pauses(self) -> None:
        self.events.append("clear")


class FakeSender:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def send_batch(self, batch_no: str, lane: str) -> None:
        self.events.append(f"send:{batch_no}:{lane}")


@pytest.mark.asyncio
async def test_queue_resume_rejects_low_balance_without_force() -> None:
    events: list[str] = []
    service = QueueRecoveryService(
        FakeRepository(QueueSnapshot("999", "999", 5000, 10000), events),
        FakeSender(events),
    )

    with pytest.raises(QueueResumeConflict, match="余额"):
        await service.resume(force=False, actor="admin01", ip="10.0.0.8")

    assert events == []


@pytest.mark.asyncio
async def test_non_balance_pause_requires_explicit_force() -> None:
    events: list[str] = []
    service = QueueRecoveryService(
        FakeRepository(QueueSnapshot("1000", "1000", 20000, 10000), events),
        FakeSender(events),
    )

    with pytest.raises(QueueResumeConflict, match="force"):
        await service.resume(force=False, actor="admin01", ip="10.0.0.8")


@pytest.mark.asyncio
async def test_forced_resume_updates_db_clears_both_keys_then_routes_batches() -> None:
    events: list[str] = []
    service = QueueRecoveryService(
        FakeRepository(QueueSnapshot("1000", "1000", 5000, 10000), events),
        FakeSender(events),
    )

    result = await service.resume(force=True, actor="admin01", ip="10.0.0.8")

    assert result.resumed_batches == 2
    assert result.paused_codes == ("1000",)
    assert events == [
        "db:admin01:10.0.0.8",
        "clear",
        "send:NOTICE-1:realtime",
        "send:MARKET-1:bulk",
    ]


class FakeScalarResult:
    def __init__(
        self,
        rows: list[dict[str, object]] | None = None,
        scalar: object = None,
    ) -> None:
        self.rows = rows or []
        self.scalar = scalar

    def mappings(self) -> FakeScalarResult:
        return self

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self.rows)

    def scalar_one_or_none(self) -> object:
        return self.scalar


class FakeConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.results: list[FakeScalarResult] = [
            FakeScalarResult(rows=[{"batch_no": "NOTICE-1", "category": "notice"}]),
            FakeScalarResult(),
        ]

    async def execute(self, statement: object, params: Any = None) -> FakeScalarResult:
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

    def begin(self) -> FakeContext:
        return FakeContext(self.connection)

    async def dispose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_resume_repository_guards_state_and_audits_count_only() -> None:
    connection = FakeConnection()
    repository = SqlOpsRepository()
    repository._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]

    batches = await repository.resume_batches(actor="admin01", ip="10.0.0.8")

    assert batches == (PausedBatch("NOTICE-1", "notice"),)
    update_sql = connection.calls[0][0]
    assert "WHERE status='balance_blocked'" in update_sql
    audit_sql, audit_params = connection.calls[1]
    assert "queue_resume" in audit_sql and "resumed_batches" in audit_sql
    assert "phone" not in str(audit_params).lower()
