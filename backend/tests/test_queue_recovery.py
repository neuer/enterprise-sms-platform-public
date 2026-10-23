from __future__ import annotations

import inspect
import json
from collections.abc import Iterator
from typing import Any

import pytest

from app.core.auth.accounts import SecurityPrincipal
from app.core.correlation import correlation_scope
from app.services.ops import (
    PausedBatch,
    QueueRecoveryService,
    QueueResumeCheck,
    QueueResumeConflict,
    QueueSnapshot,
)
from app.services.ops_repository import SqlOpsRepository

ADMIN = SecurityPrincipal(1, 10, "admin01", "平台部", "admin")


class FakeRepository:
    def __init__(
        self,
        snapshot: QueueSnapshot,
        events: list[str],
        *,
        batches: tuple[PausedBatch, ...] | None = None,
        clear_ok: bool = True,
    ) -> None:
        self.snapshot = snapshot
        self.events = events
        self.batches = (
            batches
            if batches is not None
            else (
                PausedBatch("NOTICE-1", "notice"),
                PausedBatch("MARKET-1", "market"),
            )
        )
        self.clear_ok = clear_ok
        self.last_force: bool | None = None
        self.last_check: QueueResumeCheck | None = None
        self.cleared_with: QueueSnapshot | None = None

    async def queue_snapshot(self) -> QueueSnapshot:
        return self.snapshot

    async def resume_batches(
        self,
        *,
        actor: str,
        ip: str,
        principal: SecurityPrincipal,
        force: bool,
        check: QueueResumeCheck,
    ) -> tuple[PausedBatch, ...]:
        self.last_force = force
        self.last_check = check
        self.events.append(f"db:{actor}:{ip}:{principal.account_id}")
        return self.batches

    async def clear_queue_pauses(self, snapshot: QueueSnapshot) -> bool:
        self.events.append("clear")
        self.cleared_with = snapshot
        return self.clear_ok


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
        await service.resume(force=False, actor="admin01", ip="10.0.0.8", principal=ADMIN)

    assert events == []


@pytest.mark.asyncio
async def test_queue_resume_accepts_generation_fenced_balance_claims() -> None:
    events: list[str] = []
    service = QueueRecoveryService(
        FakeRepository(QueueSnapshot("999:4", "999:4", 20000, 10000), events),
        FakeSender(events),
    )

    result = await service.resume(
        force=False, actor="admin01", ip="10.0.0.8", principal=ADMIN
    )

    assert result.paused_codes == ("999",)
    assert events[:2] == ["db:admin01:10.0.0.8:1", "clear"]


@pytest.mark.asyncio
async def test_non_balance_pause_requires_explicit_force() -> None:
    events: list[str] = []
    service = QueueRecoveryService(
        FakeRepository(QueueSnapshot("1000", "1000", 20000, 10000), events),
        FakeSender(events),
    )

    with pytest.raises(QueueResumeConflict, match="force"):
        await service.resume(force=False, actor="admin01", ip="10.0.0.8", principal=ADMIN)


@pytest.mark.asyncio
async def test_forced_resume_updates_db_clears_both_keys_then_routes_batches() -> None:
    events: list[str] = []
    service = QueueRecoveryService(
        FakeRepository(QueueSnapshot("1000", "1000", 5000, 10000), events),
        FakeSender(events),
    )

    result = await service.resume(force=True, actor="admin01", ip="10.0.0.8", principal=ADMIN)

    assert result.resumed_batches == 2
    assert result.paused_codes == ("1000",)
    assert events == [
        "db:admin01:10.0.0.8:1",
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
async def test_resume_repository_binds_live_principal_before_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bound: list[dict[str, object]] = []

    async def fake_bind(connection: object, **kwargs: object) -> None:
        bound.append({"connection": connection, **kwargs})

    monkeypatch.setattr(
        "app.services.ops_repository.bind_connection_audit_subject",
        fake_bind,
    )
    connection = FakeConnection()
    repository = SqlOpsRepository()
    repository._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]

    with correlation_scope():
        batches = await repository.resume_batches(
            actor="admin01",
            ip="10.0.0.8",
            principal=ADMIN,
            force=False,
            check=QueueResumeCheck(
                force=False,
                realtime_pause_reason="balance",
                bulk_pause_reason="balance",
                balance_known=True,
                balance_meets_threshold=True,
            ),
        )

    assert batches == (PausedBatch("NOTICE-1", "notice"),)
    assert bound == [
        {
            "connection": connection,
            "subject_kind": "human",
            "actor_name": "admin01",
            "account_id": 1,
            "identity_id": 10,
        }
    ]
    update_sql = connection.calls[0][0]
    assert "WHERE status='balance_blocked'" in update_sql
    audit_sql, audit_params = connection.calls[1]
    assert "INSERT INTO audit_log" in audit_sql
    assert "actor_subject_kind" in audit_sql
    assert "actor_account_id" in audit_sql
    assert "jsonb_build_object" not in audit_sql
    assert audit_params["action"] == "queue_resume"
    assert audit_params["object_type"] == "queue"
    assert audit_params["object_id"] == "all"
    assert audit_params["account_id"] == 1
    assert audit_params["identity_id"] == 10
    assert audit_params["actor"] == "admin01"
    payload = json.loads(str(audit_params["after"]))
    assert payload == {
        "force": False,
        "realtime_pause_reason": "balance",
        "bulk_pause_reason": "balance",
        "balance_known": True,
        "balance_meets_threshold": True,
        "resumed_batches": 1,
        "effect": "db_batches_requeued",
    }
    assert "claim" not in payload
    assert "realtime_claim" not in payload
    assert "phone" not in str(audit_params).lower()
    assert "phone" not in str(audit_params).lower()


@pytest.mark.asyncio
async def test_resume_repository_fails_closed_when_actor_does_not_match_principal() -> None:
    connection = FakeConnection()
    repository = SqlOpsRepository()
    repository._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]

    with correlation_scope(), pytest.raises(RuntimeError, match="audit principal"):
        await repository.resume_batches(
            actor="other-admin",
            ip="10.0.0.8",
            principal=ADMIN,
            force=True,
            check=QueueResumeCheck(
                force=True,
                realtime_pause_reason="vendor_account",
                bulk_pause_reason="none",
                balance_known=False,
                balance_meets_threshold=False,
            ),
        )

    assert connection.calls == []


def test_resume_repository_source_does_not_use_legacy_unattributed_insert() -> None:
    source = inspect.getsource(SqlOpsRepository.resume_batches)
    assert "bind_connection_audit_subject" in source
    assert "insert_audit" in source
    assert "jsonb_build_object('resumed_batches'" not in source
    assert "queue_snapshot" not in source
    assert "redis" not in source


@pytest.mark.asyncio
async def test_resume_passes_the_same_check_snapshot_without_raw_claims() -> None:
    events: list[str] = []
    repository = FakeRepository(
        QueueSnapshot(
            "999:4",
            "999",
            20000,
            10000,
            realtime_claim="999:4:raw-generation",
            bulk_claim=b"999:8",
        ),
        events,
    )
    service = QueueRecoveryService(repository, FakeSender(events))

    result = await service.resume(
        force=False, actor="admin01", ip="10.0.0.8", principal=ADMIN
    )

    assert result.resumed_batches == 2
    assert repository.last_force is False
    check = repository.last_check
    assert check is not None
    assert check.force is False
    assert check.realtime_pause_reason == "balance"
    assert check.bulk_pause_reason == "balance"
    assert check.balance_known is True
    assert check.balance_meets_threshold is True
    rendered = repr(check)
    assert "raw-generation" not in rendered
    assert repository.cleared_with is repository.snapshot


@pytest.mark.asyncio
async def test_force_resume_records_non_balance_reason_and_low_balance() -> None:
    events: list[str] = []
    repository = FakeRepository(
        QueueSnapshot("1000", None, 1, 10000, realtime_claim="1000:77"),
        events,
        batches=(),
    )
    result = await QueueRecoveryService(repository, FakeSender(events)).resume(
        force=True, actor="admin01", ip="10.0.0.8", principal=ADMIN
    )

    assert result.resumed_batches == 0
    assert repository.last_force is True
    check = repository.last_check
    assert check is not None
    assert check == QueueResumeCheck(
        force=True,
        realtime_pause_reason="vendor_account",
        bulk_pause_reason="none",
        balance_known=True,
        balance_meets_threshold=False,
    )
    assert "77" not in repr(check)
    assert events == ["db:admin01:10.0.0.8:1", "clear",]


@pytest.mark.asyncio
async def test_redis_cleanup_conflict_is_not_described_as_external_success() -> None:
    events: list[str] = []
    repository = FakeRepository(
        QueueSnapshot("1000", "1000", None, 10000, realtime_claim="1000:3"),
        events,
        clear_ok=False,
    )
    service = QueueRecoveryService(repository, FakeSender(events))

    with pytest.raises(QueueResumeConflict, match="暂停状态已更新"):
        await service.resume(force=True, actor="admin01", ip="10.0.0.8", principal=ADMIN)

    assert events == ["db:admin01:10.0.0.8:1", "clear"]
    assert repository.last_check is not None
    assert repository.last_check.force is True
    assert repository.last_check.balance_known is False
    assert "claim" not in repository.last_check.__slots__
