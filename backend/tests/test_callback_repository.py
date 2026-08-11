from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

import app.services.callback_repository as callback_repository_module
from app.core.auth.accounts import SecurityPrincipal
from app.services.callback_authority import CallbackAuthorityBusy, ensure_callback_authority_idle
from app.services.callback_repository import (
    CallbackRetryConflict,
    CallbackTaskNotFound,
    SqlCallbackRepository,
    enqueue_batch_finished,
)
from app.services.callback_worker import CallbackClaim

EVENT_ID = UUID("10000000-0000-4000-8000-000000000009")
LEASE_ID = UUID("20000000-0000-4000-8000-000000000009")
CORRELATION_ID = UUID("30000000-0000-4000-8000-000000000009")
ADMIN = SecurityPrincipal(1, 10, "admin", "平台部", "admin")


class FakeResult:
    def __init__(
        self,
        *,
        rows: list[dict[str, object]] | None = None,
        scalars: list[object] | None = None,
    ) -> None:
        self.rows = rows or []
        self.scalar_values = scalars or []

    def mappings(self) -> FakeResult:
        return self

    def one_or_none(self) -> dict[str, object] | None:
        assert len(self.rows) <= 1
        return self.rows[0] if self.rows else None

    def one(self) -> dict[str, object]:
        assert len(self.rows) == 1
        return self.rows[0]

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self.rows)

    def scalars(self) -> list[object]:
        return self.scalar_values

    def scalar_one(self) -> object:
        assert len(self.scalar_values) == 1
        return self.scalar_values[0]

    def scalar_one_or_none(self) -> object | None:
        assert len(self.scalar_values) <= 1
        return self.scalar_values[0] if self.scalar_values else None


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


def bind(repository: SqlCallbackRepository, connection: FakeConnection) -> None:
    repository._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_batch_finished_uses_text_binding_and_casts_back_to_bigint() -> None:
    connection = FakeConnection([FakeResult()])
    await enqueue_batch_finished(connection, 8)

    sql, params = connection.calls[0]
    assert "b.id=CAST(:batch_id AS bigint)" in sql
    # The first occurrence participates in text concatenation, so asyncpg infers
    # a text bind parameter.  Normalizing here prevents an int codec failure.
    assert params["batch_id"] == "8"
    assert params["source_report_event_key"] is None
    assert isinstance(params["correlation_id"], UUID)
    assert "callback_secret_enc" in sql
    assert "callback_secret_key_version" in sql
    assert "signature_version" in sql


@pytest.mark.asyncio
async def test_claim_atomically_sets_uuid_lease_and_returns_retry_ordinal() -> None:
    repository = SqlCallbackRepository()
    expires_at = datetime(2026, 7, 12, 8, 1, tzinfo=UTC)
    connection = FakeConnection(
        [
            FakeResult(
                rows=[
                    {
                        "id": 9,
                        "app_id": 7,
                        "event_id": EVENT_ID,
                        "correlation_id": CORRELATION_ID,
                        "event": "batch.finished",
                        "retry_count": 2,
                        "lease_id": LEASE_ID,
                        "lease_expires_at": expires_at,
                        "takeover": True,
                    }
                ]
            ),
            FakeResult(),
        ]
    )
    bind(repository, connection)

    assert await repository.claim(9, lease_seconds=30) == CallbackClaim(
        9,
        7,
        EVENT_ID,
        "batch.finished",
        2,
        LEASE_ID,
        expires_at,
        CORRELATION_ID,
    )
    sql, params = connection.calls[0]
    assert "lease_id=:lease_id" in sql
    assert "make_interval" in sql
    assert "status='pending'" in sql
    assert "next_retry_at<=now()" in sql
    assert params["task_id"] == 9
    assert params["lease_seconds"] == 30
    assert isinstance(params["lease_id"], UUID)
    assert connection.calls[1][1]["event_type"] == "takeover"


@pytest.mark.asyncio
async def test_state_updates_store_only_safe_code_and_error_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SqlCallbackRepository()
    connection = FakeConnection(
        [
            FakeResult(scalars=[3]),
            FakeResult(scalars=[9]),
            FakeResult(scalars=[10]),
            FakeResult(),
        ]
    )
    bind(repository, connection)
    outbox_events: list[tuple[Any, int]] = []

    async def outbox(
        _connection: object,
        spec: Any,
        *,
        available_delay_seconds: int = 0,
    ) -> object:
        outbox_events.append((spec, available_delay_seconds))
        return object()

    monkeypatch.setattr(callback_repository_module, "enqueue_outbox", outbox)

    await repository.mark_retry(
        9,
        LEASE_ID,
        retry_count=2,
        delay_s=900,
        http_code=500,
        error="TimeoutError",
    )
    await repository.mark_done(9, LEASE_ID, 204)
    await repository.mark_dead(
        10,
        LEASE_ID,
        retry_count=5,
        http_code=None,
        error="ConnectError",
    )

    retry_sql, retry_params = connection.calls[0]
    assert "make_interval(secs=>:delay_s)" in retry_sql
    assert retry_params == {
        "task_id": 9,
        "lease_id": LEASE_ID,
        "retry_count": 2,
        "delay_s": 900,
        "http_code": 500,
        "error": "TimeoutError",
    }
    assert len(outbox_events) == 1
    assert outbox_events[0][0].event_type == "callback.ready"
    assert outbox_events[0][0].dedup_key == "callback:9:attempt:3"
    assert outbox_events[0][1] == 900
    assert "status='done'" in connection.calls[1][0]
    assert "status='dead'" in connection.calls[2][0]
    assert "worker_lease_event" in connection.calls[3][0]


@pytest.mark.asyncio
async def test_authority_contention_deferral_preserves_retry_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SqlCallbackRepository()
    connection = FakeConnection([FakeResult(scalars=[9])])
    bind(repository, connection)
    outbox_events: list[tuple[Any, int]] = []

    async def outbox(
        _connection: object,
        spec: Any,
        *,
        available_delay_seconds: int = 0,
    ) -> object:
        outbox_events.append((spec, available_delay_seconds))
        return object()

    monkeypatch.setattr(callback_repository_module, "enqueue_outbox", outbox)

    await repository.mark_authority_busy(
        9,
        LEASE_ID,
        retry_count=4,
        delay_s=1,
    )

    sql, params = connection.calls[0]
    assert "retry_count=retry_count+1" not in sql
    assert "retry_count=:retry_count" in sql
    assert "last_error='CallbackAuthorityBusy'" in sql
    assert params["retry_count"] == 4
    assert outbox_events[0][0].dedup_key == f"callback:9:authority-busy:{LEASE_ID}"
    assert outbox_events[0][1] == 1


@pytest.mark.asyncio
async def test_batch_material_loads_secret_ciphertext_and_aggregate_without_body_column() -> None:
    repository = SqlCallbackRepository()
    finished = datetime(2026, 7, 12, 8, 0, tzinfo=UTC)
    task_row: dict[str, object] = {
        "id": 9,
        "event_id": EVENT_ID,
            "correlation_id": CORRELATION_ID,
            "app_id": 7,
            "app_name": "callback-app",
            "event": "batch.finished",
        "url": "http://callback.internal/hook",
            "callback_secret_enc": b"packed-secret",
            "callback_secret_key_version": 1,
            "signature_version": 1,
        "batch_id": 8,
        "message_ids": [],
        "message_times": [],
    }
    batch_row = {
        "batch_no": "BATCH-1",
        "biz_id": "biz-1",
        "category": "notice",
        "status": "completed",
        "total": 3,
        "delivered": 2,
        "failed": 1,
        "unknown": 0,
        "finished_at": finished,
    }
    connection = FakeConnection([FakeResult(rows=[task_row]), FakeResult(rows=[batch_row])])
    bind(repository, connection)

    material = await repository.load_material(9, LEASE_ID)

    assert material is not None and material.batch is not None
    assert material.task.callback_secret_enc == b"packed-secret"
    assert material.task.event_id == EVENT_ID
    assert material.batch.batch_no == "BATCH-1"
    assert all(
        "body" not in sql.casefold() and "payload" not in sql.casefold()
        for sql, _ in connection.calls
    )
    task_sql = connection.calls[0][0]
    assert "a.status=1" in task_sql
    assert "a.callback_url=t.url" in task_sql
    assert "a.callback_secret_enc=t.callback_secret_enc" in task_sql


@pytest.mark.asyncio
async def test_message_material_uses_id_and_created_at_for_partition_lookup() -> None:
    repository = SqlCallbackRepository()
    created_at = datetime(2026, 7, 12, 8, 0, tzinfo=UTC)
    task_row = {
        "id": 9,
        "event_id": EVENT_ID,
            "correlation_id": CORRELATION_ID,
            "app_id": 7,
            "app_name": "callback-app",
            "event": "message.report",
        "url": "http://callback.internal/hook",
            "callback_secret_enc": b"packed-secret",
            "callback_secret_key_version": 1,
            "signature_version": 1,
        "batch_id": 8,
        "message_ids": [21],
        "message_times": [created_at],
        "event_keys": ["a" * 64],
    }
    message_row: dict[str, object] = {
        "batch_no": "BATCH-1",
        "biz_id": None,
            "phone_enc": b"ciphertext",
            "phone_hmac": "a" * 64,
        "key_version": 1,
        "status": "delivered",
        "report_desc": "DELIVRD",
        "report_time": created_at,
    }
    connection = FakeConnection([FakeResult(rows=[task_row]), FakeResult(rows=[message_row])])
    bind(repository, connection)

    material = await repository.load_material(9, LEASE_ID)

    assert material is not None and material.message_report is not None
    sql = connection.calls[1][0]
    assert "m.id=r.id AND m.created_at=r.created_at" in sql
    assert "e.message_status status" in sql
    assert "e.report_desc" in sql and "e.report_time" in sql


@pytest.mark.asyncio
async def test_due_scan_returns_only_database_references() -> None:
    repository = SqlCallbackRepository()
    connection = FakeConnection([FakeResult(scalars=[3, 5])])
    bind(repository, connection)

    assert await repository.due_ids() == [3, 5]
    sql, _ = connection.calls[0]
    assert "status='pending'" in sql and "next_retry_at<=now()" in sql
    assert "lease_expires_at<=now()" in sql
    assert "phone" not in sql.casefold() and "body" not in sql.casefold()


@pytest.mark.asyncio
async def test_admin_page_returns_only_safe_summary_fields() -> None:
    repository = SqlCallbackRepository()
    row = {
        "id": 9,
        "event_id": EVENT_ID,
        "correlation_id": CORRELATION_ID,
        "app_id": 7,
        "app_name": "通知应用",
        "event": "message.report",
        "batch_no": "BATCH-1",
        "reference_count": 2,
        "status": "dead",
        "retry_count": 5,
        "next_retry_at": None,
        "lease_id": None,
        "lease_expires_at": None,
        "takeover_count": 2,
        "stalled": False,
        "last_http_code": 500,
        "last_error": "ConnectError",
        "created_at": datetime(2026, 7, 12, 8, 0, tzinfo=UTC),
        "finished_at": datetime(2026, 7, 12, 9, 0, tzinfo=UTC),
    }
    connection = FakeConnection(
        [FakeResult(scalars=[1]), FakeResult(rows=[row]), FakeResult(scalars=[3])]
    )
    bind(repository, connection)

    page = await repository.list_page(
        status="dead",
        app_id=7,
        event="message.report",
        batch_no="BATCH_1%",
        page=2,
        size=50,
    )

    assert page == {"total": 1, "dead_total": 3, "items": [row]}
    assert connection.calls[0][1]["offset"] == 50
    sql = " ".join(call[0] for call in connection.calls)
    assert "CAST(:status AS varchar(10))" in sql
    assert "CAST(:app_id AS bigint)" in sql
    assert "CAST(:event AS varchar(20))" in sql
    assert "trim(b.batch_no) ILIKE :batch_no" in sql
    # LIKE 通配符必须被转义，批次号子串两端补 %
    assert connection.calls[0][1]["batch_no"] == "%BATCH\\_1\\%%"
    assert "WHERE status='dead'" in connection.calls[2][0]
    assert "callback_url" not in sql and "callback_secret" not in sql and "phone" not in sql


@pytest.mark.asyncio
async def test_manual_dead_retry_is_audited_and_rejects_invalid_state() -> None:
    repository = SqlCallbackRepository()
    success = FakeConnection([FakeResult(scalars=[9]), FakeResult(), FakeResult()])
    bind(repository, success)
    await repository.manual_retry(9, principal=ADMIN)
    retry_sql = success.calls[0][0]
    assert "FROM app" in retry_sql
    assert "callback_url" in retry_sql and "callback_secret_enc" in retry_sql
    assert "CallbackConfigRevoked" in retry_sql
    assert "INSERT INTO audit_log" in success.calls[1][0]
    assert "worker_lease_event" in success.calls[2][0]
    assert success.calls[1][1]["actor"] == "admin"
    assert success.calls[1][1]["account_id"] == 1
    assert success.calls[1][1]["identity_id"] == 10
    assert isinstance(success.calls[1][1]["correlation_id"], UUID)

    conflict = FakeConnection([FakeResult(), FakeResult(scalars=["retrying"])])
    bind(repository, conflict)
    with pytest.raises(CallbackRetryConflict):
        await repository.manual_retry(10, principal=ADMIN)

    missing = FakeConnection([FakeResult(), FakeResult()])
    bind(repository, missing)
    with pytest.raises(CallbackTaskNotFound):
        await repository.manual_retry(11, principal=ADMIN)


def test_callback_authority_mutations_trigger_transactional_task_revocation() -> None:
    root = Path(__file__).parents[2]
    schema = (root / "schema.sql").read_text(encoding="utf-8")
    migration = (
        root / "backend/migrations/versions/0055_callback_revocation.py"
    ).read_text(encoding="utf-8")

    for source in (schema, migration):
        assert "revoke_callback_tasks_on_app_change" in source
        assert "AFTER UPDATE OF callback_url,callback_secret_enc" in source
        assert "status='dead'" in source
        assert "last_error='CallbackConfigRevoked'" in source
        assert "lease_id=NULL" in source and "lease_expires_at=NULL" in source
        assert "status IN ('pending','retrying')" in source
        assert "event='message.report'" in source
        assert "SECURITY DEFINER" in source
        assert "REVOKE ALL ON FUNCTION" in source


@pytest.mark.asyncio
async def test_callback_cidr_policy_is_loaded_from_database() -> None:
    repository = SqlCallbackRepository()
    connection = FakeConnection([FakeResult(scalars=["10.0.0.0/8,192.168.0.0/16"])])
    bind(repository, connection)

    assert await repository.callback_allow_cidrs() == "10.0.0.0/8,192.168.0.0/16"


@pytest.mark.asyncio
async def test_callback_authority_is_acquired_only_for_current_task_and_app_config() -> None:
    repository = SqlCallbackRepository()
    connection = FakeConnection(
        [
            FakeResult(scalars=[7]),
            FakeResult(),
            FakeResult(scalars=[7]),
            FakeResult(),
        ]
    )
    bind(repository, connection)

    assert await repository.acquire_authority(9, LEASE_ID) is True
    await repository.release_authority(9, LEASE_ID)

    acquire_sql = connection.calls[2][0]
    assert "t.status='retrying'" in acquire_sql
    assert "t.lease_id=:lease_id" in acquire_sql
    assert "a.status=1" in acquire_sql
    assert "a.callback_url=t.url" in acquire_sql
    assert "a.callback_secret_enc=t.callback_secret_enc" in acquire_sql
    assert "callback_report_enabled=true" in acquire_sql
    assert "ON CONFLICT DO NOTHING" in acquire_sql
    assert "callback_authority_lease" in connection.calls[3][0]


@pytest.mark.asyncio
async def test_callback_authority_contention_is_distinct_from_revocation() -> None:
    repository = SqlCallbackRepository()
    connection = FakeConnection(
        [
            FakeResult(scalars=[7]),
            FakeResult(),
            FakeResult(),
            FakeResult(scalars=[7]),
        ]
    )
    bind(repository, connection)

    with pytest.raises(CallbackAuthorityBusy):
        await repository.acquire_authority(9, LEASE_ID)

    assert "expires_at>now()" in connection.calls[3][0]

    revoked_repository = SqlCallbackRepository()
    revoked_connection = FakeConnection(
        [FakeResult(scalars=[7]), FakeResult(), FakeResult(), FakeResult()]
    )
    bind(revoked_repository, revoked_connection)

    assert await revoked_repository.acquire_authority(9, LEASE_ID) is False


@pytest.mark.asyncio
async def test_callback_authority_final_confirmation_locks_app_and_renews_lease() -> None:
    repository = SqlCallbackRepository()
    connection = FakeConnection(
        [
            FakeResult(scalars=[7]),
            FakeResult(scalars=[7]),
        ]
    )
    bind(repository, connection)

    assert await repository.confirm_authority(9, LEASE_ID) is True

    assert "FOR UPDATE OF a" in connection.calls[0][0]
    assert "a.callback_url=t.url" in connection.calls[0][0]
    assert "expires_at>now()" in connection.calls[1][0]
    assert "interval '30 seconds'" in connection.calls[1][0]


@pytest.mark.asyncio
async def test_callback_configuration_mutation_rejects_active_authority_lease() -> None:
    class AuthorityConnection(FakeConnection):
        async def scalar(self, statement: object, params: Any = None) -> object | None:
            self.calls.append((str(statement), params))
            return 1

    connection = AuthorityConnection([FakeResult(), FakeResult()])

    with pytest.raises(CallbackAuthorityBusy):
        await ensure_callback_authority_idle(connection, 7)

    assert "FOR UPDATE" in connection.calls[0][0]
    assert "expires_at<=now()" in connection.calls[1][0]
    assert "callback_authority_lease" in connection.calls[2][0]
