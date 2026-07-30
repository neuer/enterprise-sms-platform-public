from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from app.core.auth.accounts import SecurityPrincipal

NOW = datetime(2026, 7, 17, 9, tzinfo=UTC)
OPERATION_ID = "c0a80101-0000-4000-8000-000000000081"
SENTINEL = "formal-key-private-13800138000-" + "a" * 64
ADMIN = SecurityPrincipal(1, 10, "admin", "平台部", "admin")


class FakeMappings:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self.rows)

    def one_or_none(self) -> dict[str, object] | None:
        assert len(self.rows) <= 1
        return self.rows[0] if self.rows else None


class FakeResult:
    def __init__(
        self,
        rows: list[dict[str, object]] | None = None,
        *,
        scalar: object | None = None,
    ) -> None:
        self.rows = rows or []
        self.scalar = scalar

    def mappings(self) -> FakeMappings:
        return FakeMappings(self.rows)

    def scalar_one(self) -> object:
        assert self.scalar is not None
        return self.scalar


class FakeConnection:
    def __init__(self, results: list[FakeResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, Any]] = []
        self.begin_calls = 0

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

    def begin(self) -> FakeContext:
        self.connection.begin_calls += 1
        return FakeContext(self.connection)

    def connect(self) -> FakeContext:
        return FakeContext(self.connection)

    async def dispose(self) -> None:
        return None


def row(
    *,
    status: str = "requested",
    safe_code: str | None = None,
    vendor_code: int | None = None,
):
    return {
        "id": OPERATION_ID,
        "operation_type": "activate",
        "actor": "admin",
        "actor_account_id": ADMIN.account_id,
        "actor_identity_id": ADMIN.identity_id,
        "status": status,
        "safe_code": safe_code,
        "vendor_code": vendor_code,
        "batch_no": None,
        "checkpoint_id": None,
        "requested_at": NOW,
        "completed_at": NOW if status in {"succeeded", "failed"} else None,
    }


def repository(results: list[FakeResult]):
    from app.services.vendor_test_operation_repository import (
        SqlVendorTestOperationRepository,
    )

    connection = FakeConnection(results)
    repo = SqlVendorTestOperationRepository()
    repo._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]
    return repo, connection


@pytest.mark.asyncio
async def test_reserve_start_inserts_requested_audit_with_safe_fields_only() -> None:
    repo, connection = repository(
        [
            FakeResult(),
            FakeResult(),
            FakeResult(),
            FakeResult([row()]),
            FakeResult(),
        ]
    )

    record = await repo.reserve_start(
        OPERATION_ID,
        "activate",
        principal=ADMIN,
        conflicting_types=frozenset({"activate", "pause"}),
    )

    assert record.status == "requested"
    insert_sql, insert_params = connection.calls[3]
    assert "INSERT INTO vendor_test_operation" in insert_sql
    assert "request_body" not in insert_sql and "payload" not in insert_sql
    audit_sql, audit_params = connection.calls[4]
    assert "INSERT INTO audit_log" in audit_sql
    assert json.loads(audit_params["after"]) == {
        "count": 1,
        "operation_id": OPERATION_ID,
    }
    assert SENTINEL not in repr(connection.calls)
    assert "phone" not in repr(audit_params).lower()


@pytest.mark.asyncio
async def test_reserve_start_locks_checks_conflicts_and_requests_in_one_transaction() -> None:
    repo, connection = repository(
        [
            FakeResult(),
            FakeResult(),
            FakeResult(),
            FakeResult([row()]),
            FakeResult(),
        ]
    )

    record = await repo.reserve_start(
        OPERATION_ID,
        "activate",
        principal=ADMIN,
        conflicting_types=frozenset({"activate", "pause"}),
    )

    assert record.status == "requested"
    assert connection.begin_calls == 1
    lifecycle_sql, lifecycle_params = connection.calls[0]
    assert "pg_advisory_xact_lock" in lifecycle_sql
    assert lifecycle_params == {"lock_name": "vendor-test-lifecycle"}
    existing_sql, existing_params = connection.calls[1]
    assert "WHERE id=CAST(:id AS uuid) FOR UPDATE" in existing_sql
    assert existing_params == {"id": OPERATION_ID}
    conflict_sql, conflict_params = connection.calls[2]
    assert "status IN ('requested','running')" in conflict_sql
    assert "id<>CAST(:id AS uuid)" in conflict_sql
    assert set(conflict_params.values()) == {
        OPERATION_ID,
        "activate",
        "pause",
    }
    assert "INSERT INTO vendor_test_operation" in connection.calls[3][0]
    assert "INSERT INTO audit_log" in connection.calls[4][0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation_type", "conflicting_type"),
    [
        ("install_credentials", "reset_configuration"),
        ("rotate_credentials", "reset_configuration"),
        ("reset_configuration", "install_credentials"),
        ("reset_configuration", "rotate_credentials"),
    ],
)
async def test_reserve_start_atomically_rejects_credential_reset_conflicts(
    operation_type: str,
    conflicting_type: str,
) -> None:
    from app.services.vendor_test_operation import (
        CONTROL_OPERATION_TYPES,
        OPERATION_TYPES,
        VendorTestOperationConflict,
    )

    repo, connection = repository(
        [
            FakeResult(),
            FakeResult(),
            FakeResult([{"id": "c0a80101-0000-4000-8000-000000000099"}]),
        ]
    )

    with pytest.raises(VendorTestOperationConflict, match="正在执行"):
        await repo.reserve_start(
            OPERATION_ID,
            operation_type,
            principal=ADMIN,
            conflicting_types=(
                OPERATION_TYPES
                if operation_type == "reset_configuration"
                else CONTROL_OPERATION_TYPES
            ),
        )

    assert "pg_advisory_xact_lock" in connection.calls[0][0]
    conflict_sql, conflict_params = connection.calls[2]
    assert "status IN ('requested','running')" in conflict_sql
    assert conflicting_type in conflict_params.values()
    assert all(
        "INSERT INTO vendor_test_operation" not in sql
        for sql, _params in connection.calls
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "conflicting_types",
    [
        frozenset(),
        frozenset({"activate", "not_an_operation"}),
    ],
)
async def test_reserve_start_rejects_empty_or_unknown_conflict_sets(
    conflicting_types: frozenset[str],
) -> None:
    from app.services.vendor_test_operation import VendorTestOperationConflict
    from app.services.vendor_test_operation_repository import (
        SqlVendorTestOperationRepository,
    )

    repo = SqlVendorTestOperationRepository()
    repo._engine = lambda: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("invalid conflict set must fail before opening DB")
    )

    with pytest.raises(VendorTestOperationConflict):
        await repo.reserve_start(
            OPERATION_ID,
            "activate",
            principal=ADMIN,
            conflicting_types=conflicting_types,
        )


@pytest.mark.asyncio
async def test_reserve_start_same_id_replay_still_checks_immutable_contract() -> None:
    from app.services.vendor_test_operation import VendorTestOperationConflict

    existing = row()
    existing["operation_type"] = "pause"
    repo, connection = repository(
        [
            FakeResult(),
            FakeResult([existing]),
        ]
    )

    with pytest.raises(VendorTestOperationConflict, match="已被占用"):
        await repo.reserve_start(
            OPERATION_ID,
            "activate",
            principal=ADMIN,
            conflicting_types=frozenset({"activate", "pause"}),
        )

    assert "FOR UPDATE" in connection.calls[1][0]
    assert len(connection.calls) == 2


def test_repository_exposes_no_unlocked_public_request_entrypoint() -> None:
    from app.services.vendor_test_operation_repository import (
        SqlVendorTestOperationRepository,
    )

    assert not hasattr(SqlVendorTestOperationRepository, "request")


@pytest.mark.asyncio
async def test_claim_control_running_is_control_only_requested_cas() -> None:
    from app.services.vendor_test_operation import CONTROL_OPERATION_TYPES

    running = row(status="running")
    repo, connection = repository([FakeResult([running])])

    claimed = await repo.claim_control_running(OPERATION_ID)

    assert claimed is not None
    assert claimed.status == "running"
    sql, params = connection.calls[0]
    assert "UPDATE vendor_test_operation SET" in sql
    assert "status='running'" in sql
    assert "status='requested'" in sql
    assert "operation_type IN" in sql
    assert "uat_send" not in sql
    assert "lease_expires_at" not in sql
    assert all(operation_type in sql for operation_type in CONTROL_OPERATION_TYPES)
    assert params == {"id": OPERATION_ID}


@pytest.mark.asyncio
async def test_fail_unclaimed_control_is_requested_only_cas_with_safe_audit() -> None:
    failed = row(status="failed", safe_code="CONTROL_OPERATION_NOT_FOUND")
    repo, connection = repository([FakeResult([failed]), FakeResult()])

    record = await repo.fail_unclaimed_control(
        OPERATION_ID,
        safe_code="CONTROL_OPERATION_NOT_FOUND",
    )

    assert record is not None
    assert record.status == "failed"
    sql, params = connection.calls[0]
    assert "status='requested'" in sql
    assert "operation_type IN" in sql
    assert "status IN ('requested','running')" not in sql
    assert params == {
        "id": OPERATION_ID,
        "safe_code": "CONTROL_OPERATION_NOT_FOUND",
    }
    _, audit_params = connection.calls[1]
    assert json.loads(audit_params["after"]) == {
        "count": 1,
        "operation_id": OPERATION_ID,
    }


@pytest.mark.asyncio
async def test_fail_unclaimed_control_loses_to_running_claim_without_audit() -> None:
    repo, connection = repository([FakeResult()])

    record = await repo.fail_unclaimed_control(
        OPERATION_ID,
        safe_code="CONTROL_OPERATION_NOT_FOUND",
    )

    assert record is None
    assert len(connection.calls) == 1


@pytest.mark.asyncio
async def test_completion_updates_terminal_state_and_audits_only_safe_metadata() -> None:
    repo, connection = repository(
        [FakeResult([row(status="failed", safe_code="CONTROL_COMMAND_FAILED")]), FakeResult()]
    )

    completed = await repo.complete(
        OPERATION_ID,
        status="failed",
        safe_code="CONTROL_COMMAND_FAILED",
    )

    assert completed.status == "failed"
    update_sql, update_params = connection.calls[0]
    assert "UPDATE vendor_test_operation" in update_sql
    assert update_params["safe_code"] == "CONTROL_COMMAND_FAILED"
    audit_sql, audit_params = connection.calls[1]
    assert "INSERT INTO audit_log" in audit_sql
    assert json.loads(audit_params["after"]) == {
        "count": 1,
        "operation_id": OPERATION_ID,
    }
    assert SENTINEL not in repr(connection.calls)


@pytest.mark.asyncio
async def test_attach_uat_batch_updates_only_safe_reference_and_audits() -> None:
    attached_row = row(status="running")
    attached_row["operation_type"] = "uat_send"
    attached_row["batch_no"] = "batch-uat"
    repo, connection = repository([FakeResult([attached_row]), FakeResult()])

    record = await repo.attach_batch(OPERATION_ID, batch_no="batch-uat")

    assert record.status == "running"
    assert record.batch_no == "batch-uat"
    update_sql, update_params = connection.calls[0]
    assert "UPDATE vendor_test_operation" in update_sql
    assert update_params == {"id": OPERATION_ID, "batch_no": "batch-uat"}
    _, audit_params = connection.calls[1]
    assert json.loads(audit_params["after"]) == {
        "batch_no": "batch-uat",
        "count": 1,
        "operation_id": OPERATION_ID,
    }


@pytest.mark.asyncio
async def test_uat_reservation_sets_bounded_database_lease() -> None:
    uat_row = row()
    uat_row["operation_type"] = "uat_send"
    repo, connection = repository(
        [
            FakeResult(),
            FakeResult(),
            FakeResult(),
            FakeResult([uat_row]),
            FakeResult(),
        ]
    )

    await repo.reserve_start(
        OPERATION_ID,
        "uat_send",
        principal=ADMIN,
        conflicting_types=frozenset({"uat_send", "reset_configuration"}),
    )

    insert_sql, insert_params = connection.calls[3]
    assert "lease_expires_at" in insert_sql
    assert "make_interval(secs=>:lease_seconds)" in insert_sql
    assert insert_params["lease_seconds"] == 60


@pytest.mark.asyncio
async def test_uat_heartbeat_cannot_resurrect_an_expired_or_attached_lease() -> None:
    running = row(status="running")
    running["operation_type"] = "uat_send"
    repo, connection = repository([FakeResult([running])])

    assert await repo.heartbeat(OPERATION_ID) is True

    sql, params = connection.calls[0]
    assert "lease_expires_at > now()" in sql
    assert "batch_no IS NULL" in sql
    assert "status IN ('requested','running')" in sql
    assert "make_interval(secs=>:lease_seconds)" in sql
    assert params == {"id": OPERATION_ID, "lease_seconds": 60}


@pytest.mark.asyncio
async def test_prepare_uat_acceptance_rechecks_lease_after_guard_wait() -> None:
    running = row(status="running")
    running["operation_type"] = "uat_send"
    repo, connection = repository([FakeResult([running])])

    assert await repo.prepare_uat_acceptance(OPERATION_ID) is True

    sql, params = connection.calls[0]
    assert "status='running'" in sql
    assert "lease_expires_at > now()" in sql
    assert "batch_no IS NULL" in sql
    assert "make_interval(secs=>:lease_seconds)" in sql
    assert params == {"id": OPERATION_ID, "lease_seconds": 60}


@pytest.mark.asyncio
async def test_claim_uat_running_requires_this_caller_to_transition_requested() -> None:
    repo, connection = repository([FakeResult()])

    assert await repo.claim_uat_running(OPERATION_ID) is None

    sql, params = connection.calls[0]
    assert "status='requested'" in sql
    assert "operation_type='uat_send'" in sql
    assert "lease_expires_at > now()" in sql
    assert "RETURNING" in sql
    assert params == {"id": OPERATION_ID, "lease_seconds": 60}


@pytest.mark.asyncio
async def test_acceptance_guard_holds_operation_advisory_lock() -> None:
    repo, connection = repository([FakeResult()])

    async with repo.acceptance_guard(OPERATION_ID):
        assert len(connection.calls) == 1

    sql, params = connection.calls[0]
    assert "pg_advisory_xact_lock" in sql
    assert "hashtextextended" in sql
    assert params == {"lock_name": f"vendor-uat-accept:{OPERATION_ID}"}


@pytest.mark.asyncio
async def test_expire_uat_is_atomic_with_guard_and_postgres_batch_truth() -> None:
    failed = row(status="failed", safe_code="UAT_ACCEPTANCE_EXPIRED")
    failed["operation_type"] = "uat_send"
    repo, connection = repository(
        [
            FakeResult(scalar=True),
            FakeResult([failed]),
            FakeResult(),
        ]
    )

    record = await repo.expire_uat_if_stale(
        OPERATION_ID,
        safe_code="UAT_ACCEPTANCE_EXPIRED",
    )

    assert record is not None
    assert record.status == "failed"
    lock_sql, lock_params = connection.calls[0]
    assert "pg_try_advisory_xact_lock" in lock_sql
    assert lock_params == {"lock_name": f"vendor-uat-accept:{OPERATION_ID}"}
    update_sql, update_params = connection.calls[1]
    assert "lease_expires_at <= now()" in update_sql
    assert "batch_no IS NULL" in update_sql
    assert "NOT EXISTS" in update_sql
    assert "FROM sms_batch" in update_sql
    assert "batch.channel='web'" in update_sql
    assert "batch.is_test=true" in update_sql
    assert "batch.app_id IS NOT NULL" in update_sql
    assert update_params["biz_id"].startswith("vuat:")
    assert len(update_params["biz_id"]) == 27
    assert update_params["safe_code"] == "UAT_ACCEPTANCE_EXPIRED"
    assert "INSERT INTO audit_log" in connection.calls[2][0]


@pytest.mark.asyncio
async def test_pending_query_selects_only_safe_columns_and_nonterminal_rows() -> None:
    repo, connection = repository([FakeResult([row(status="running")])])

    records = await repo.pending()

    sql = connection.calls[0][0]
    assert "status IN ('requested','running')" in sql
    assert "payload" not in sql and "phone" not in sql and "secret" not in sql
    assert records[0].operation_id == OPERATION_ID


@pytest.mark.asyncio
async def test_uat_result_maps_vendor_failure_without_loading_message_payload() -> None:
    from app.services.vendor_test_operation import vendor_test_uat_biz_id
    from app.services.vendor_test_uat import UatBatchResult

    repo, connection = repository(
        [
            FakeResult(
                [
                    {
                        "batch_no": "batch-uat",
                        "batch_status": "sending",
                        "chunk_status": "failed",
                        "vendor_code": 1010,
                    }
                ]
            )
        ]
    )

    result = await repo.uat_result(OPERATION_ID, batch_no="batch-uat")

    assert result == UatBatchResult("batch-uat", "failed", "VENDOR_ERROR", 1010)
    sql, params = connection.calls[0]
    assert "sms_batch" in sql and "sms_chunk" in sql
    assert "b.channel='web'" in sql
    assert "b.is_test=true" in sql
    assert "b.app_id IS NOT NULL" in sql
    assert params == {
        "batch_no": "batch-uat",
        "biz_id": vendor_test_uat_biz_id(OPERATION_ID),
    }
    assert "phone" not in sql.casefold()
    assert SENTINEL not in repr(connection.calls)
