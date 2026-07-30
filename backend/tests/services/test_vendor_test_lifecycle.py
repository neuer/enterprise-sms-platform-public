from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from app.core.auth.accounts import SecurityPrincipal

NOW = datetime(2026, 7, 19, 9, tzinfo=UTC)
RESET_ID = "c0a80101-0000-4000-8000-000000000141"
UAT_ID = "c0a80101-0000-4000-8000-000000000142"
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


class LifecycleDatabase:
    def __init__(self) -> None:
        self.lifecycle_lock = asyncio.Lock()
        self.recipient_lock = asyncio.Lock()
        self.active_check_entered = asyncio.Event()
        self.start_attempted = asyncio.Event()
        self.start_acquired = asyncio.Event()
        self.start_acquired_before_delete: bool | None = None
        self.coordinate_concurrent_start = False
        self.recipients = [7]
        self.operations = [
            {
                "id": RESET_ID,
                "operation_type": "reset_configuration",
                "actor": "admin",
                "status": "running",
                "safe_code": None,
                "vendor_code": None,
                "batch_no": None,
                "checkpoint_id": None,
                "requested_at": NOW,
                "completed_at": None,
            }
        ]


class FakeConnection:
    def __init__(self, database: LifecycleDatabase, role: str) -> None:
        self.database = database
        self.role = role
        self.held_locks: list[asyncio.Lock] = []

    async def execute(self, statement: object, params: Any = None) -> FakeResult:
        sql = str(statement)
        if "pg_advisory_xact_lock" in sql:
            lock_name = params["lock_name"]
            lock = (
                self.database.lifecycle_lock
                if lock_name == "vendor-test-lifecycle"
                else self.database.recipient_lock
            )
            if self.role == "operation" and lock_name == "vendor-test-lifecycle":
                self.database.start_attempted.set()
            await lock.acquire()
            self.held_locks.append(lock)
            if self.role == "operation" and lock_name == "vendor-test-lifecycle":
                self.database.start_acquired.set()
            return FakeResult()
        if "SELECT EXISTS" in sql and "operation_type='uat_send'" in sql:
            if self.database.coordinate_concurrent_start:
                self.database.active_check_entered.set()
                await self.database.start_attempted.wait()
            active = any(
                row["operation_type"] == "uat_send"
                and row["status"] in {"requested", "running"}
                for row in self.database.operations
            )
            return FakeResult(scalar=active)
        if "SELECT EXISTS" in sql and "operation_type='reset_configuration'" in sql:
            active = any(
                row["operation_type"] == "reset_configuration"
                and row["status"] in {"requested", "running"}
                for row in self.database.operations
            )
            return FakeResult(scalar=active)
        if "WHERE id=CAST(:id AS uuid) FOR UPDATE" in sql:
            row = next(
                (
                    item
                    for item in self.database.operations
                    if item["id"] == params["id"]
                ),
                None,
            )
            return FakeResult([row] if row is not None else [])
        if "id<>CAST(:id AS uuid)" in sql:
            conflicting_types = {
                value
                for key, value in params.items()
                if key.startswith("conflict_type_")
            }
            row = next(
                (
                    item
                    for item in self.database.operations
                    if item["id"] != params["id"]
                    and item["operation_type"] in conflicting_types
                    and item["status"] in {"requested", "running"}
                ),
                None,
            )
            return FakeResult([{"id": row["id"]}] if row is not None else [])
        if sql.lstrip().startswith("DELETE FROM vendor_test_recipient"):
            self.database.start_acquired_before_delete = (
                self.database.start_acquired.is_set()
            )
            removed = [{"id": item} for item in self.database.recipients]
            self.database.recipients.clear()
            return FakeResult(removed)
        if "vendor_test_recipient_hmac_alias WHERE" in sql:
            return FakeResult(scalar=False)
        if sql.lstrip().startswith("INSERT INTO vendor_test_recipient("):
            recipient_id = max(self.database.recipients, default=0) + 1
            self.database.recipients.append(recipient_id)
            return FakeResult(
                [{"id": recipient_id, "status": "active", "created_at": NOW}]
            )
        if sql.lstrip().startswith("INSERT INTO vendor_test_recipient_hmac_alias"):
            return FakeResult()
        if "INSERT INTO audit_log" in sql:
            return FakeResult()
        raise AssertionError(f"unexpected SQL in {self.role}: {sql}")


class FakeContext:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, *_args: object) -> None:
        for lock in reversed(self.connection.held_locks):
            lock.release()


class FakeEngine:
    def __init__(self, database: LifecycleDatabase, role: str) -> None:
        self.database = database
        self.role = role

    def begin(self) -> FakeContext:
        return FakeContext(FakeConnection(self.database, self.role))

    async def dispose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_purge_holds_lifecycle_lock_across_active_check_and_delete() -> None:
    from app.services.vendor_test_operation import VendorTestOperationConflict
    from app.services.vendor_test_operation_repository import (
        SqlVendorTestOperationRepository,
    )
    from app.services.vendor_test_recipient_repository import (
        SqlVendorTestRecipientRepository,
    )

    database = LifecycleDatabase()
    database.coordinate_concurrent_start = True
    recipients = SqlVendorTestRecipientRepository()
    operations = SqlVendorTestOperationRepository()
    recipients._engine = lambda: FakeEngine(database, "recipient")  # type: ignore[method-assign]
    operations._engine = lambda: FakeEngine(database, "operation")  # type: ignore[method-assign]

    purge_task = asyncio.create_task(recipients.purge_all(actor="admin"))
    await database.active_check_entered.wait()
    start_task = asyncio.create_task(
        operations.reserve_start(
            UAT_ID,
            "uat_send",
            principal=ADMIN,
            conflicting_types=frozenset({"uat_send", "reset_configuration"}),
        )
    )
    start_task.add_done_callback(lambda _task: database.start_attempted.set())

    assert await purge_task == 1
    start_result = (await asyncio.gather(start_task, return_exceptions=True))[0]

    assert isinstance(start_result, VendorTestOperationConflict)
    assert database.start_acquired_before_delete is False
    assert database.recipients == []
    assert all(row["operation_type"] != "uat_send" for row in database.operations)


@pytest.mark.asyncio
async def test_create_after_purge_waits_for_reset_terminal_before_it_is_allowed() -> None:
    from app.services.vendor_test_recipient import (
        RecipientBusy,
        VendorTestRecipientCreate,
    )
    from app.services.vendor_test_recipient_repository import (
        SqlVendorTestRecipientRepository,
    )

    database = LifecycleDatabase()
    recipients = SqlVendorTestRecipientRepository()
    recipients._engine = lambda: FakeEngine(database, "recipient")  # type: ignore[method-assign]
    candidate = VendorTestRecipientCreate(
        label="值班测试机",
        phone_enc=b"ciphertext-only",
        phone_hmac="a" * 64,
        phone_mask="138****8000",
        key_version=2,
    )

    assert await recipients.purge_all(actor="admin") == 1
    assert database.recipients == []

    with pytest.raises(RecipientBusy):
        await recipients.create(candidate, {2: "a" * 64}, actor="admin")

    assert database.recipients == []
    database.operations[0]["status"] = "succeeded"
    created = await recipients.create(candidate, {2: "a" * 64}, actor="admin")

    assert created.status == "active"
    assert database.recipients == [created.id]
