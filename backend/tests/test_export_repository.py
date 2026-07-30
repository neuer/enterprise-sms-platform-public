from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from app.core.auth.accounts import SecurityPrincipal
from app.services.export import ExportFilterSet
from app.services.export_repository import ExportClaim, SqlExportRepository

PUBLIC_ID = UUID("c0a80101-0000-4000-8000-000000000134")
LEASE_ID = UUID("20000000-0000-4000-8000-000000000009")
APPROVER = SecurityPrincipal(21, 121, "approver-a", "平台部", "approver")
VIEWER = SecurityPrincipal(11, 111, "viewer-a", "平台部", "viewer")


class FakeResult:
    def __init__(
        self,
        *,
        rows: list[dict[str, object]] | None = None,
        scalar: object | None = None,
    ) -> None:
        self.rows = rows or []
        self.scalar = scalar

    def mappings(self) -> FakeResult:
        return self

    def first(self) -> dict[str, object] | None:
        return self.rows[0] if self.rows else None

    def one(self) -> dict[str, object]:
        assert len(self.rows) == 1
        return self.rows[0]

    def one_or_none(self) -> dict[str, object] | None:
        assert len(self.rows) <= 1
        return self.rows[0] if self.rows else None

    def scalar_one(self) -> object:
        return self.scalar

    def scalar_one_or_none(self) -> object | None:
        return self.scalar

    def scalars(self) -> list[object]:
        return [self.scalar] if self.scalar is not None else []


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


def bind(repository: SqlExportRepository, connection: FakeConnection) -> None:
    repository._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]


def filters() -> ExportFilterSet:
    return ExportFilterSet(
        datetime(2026, 7, 1, tzinfo=UTC),
        None,
        "notice",
        "delivered",
        7,
        "BATCH-1",
        ("a" * 64,),
        "平台部",
    )


@pytest.mark.asyncio
async def test_count_is_bounded_at_100001_and_uses_only_safe_filters() -> None:
    repository = SqlExportRepository()
    connection = FakeConnection([FakeResult(scalar=100_001)])
    bind(repository, connection)

    assert await repository.count_rows(filters()) == 100_001
    sql, params = connection.calls[0]
    assert "LIMIT 100001" in sql and "b.dept=:scope_dept" in sql
    assert "phone_hmac=ANY" in sql
    assert "CAST(:scope_dept AS varchar(128)) IS NULL" in sql
    assert "CAST(:start AS timestamptz) IS NULL" in sql
    assert "CAST(:end AS timestamptz) IS NULL" in sql
    assert "CAST(:category AS varchar(8)) IS NULL" in sql
    assert "CAST(:status AS varchar(10)) IS NULL" in sql
    assert "CAST(:app_id AS bigint) IS NULL" in sql
    assert "CAST(:batch_no AS char(32)) IS NULL" in sql
    assert "13800138000" not in repr(params)


@pytest.mark.asyncio
async def test_unmatched_count_uses_only_unmatched_hmac_and_time_filters() -> None:
    repository = SqlExportRepository()
    connection = FakeConnection([FakeResult(scalar=1)])
    bind(repository, connection)

    assert await repository.count_rows(replace(filters(), dataset="unmatched")) == 1

    sql, params = connection.calls[0]
    assert "FROM unmatched_report u" in sql
    assert "u.phone_hmac=ANY" in sql
    assert "sms_message" not in sql and "sms_batch" not in sql
    assert params["phone_hmacs"] == ["a" * 64]


@pytest.mark.asyncio
async def test_create_persists_safe_filter_and_audits_without_hmac_or_phone() -> None:
    now = datetime(2026, 7, 12, tzinfo=UTC)
    row = {
        "id": 9,
        "public_id": PUBLIC_ID,
        "status": "pending",
        "decrypted": True,
        "row_count": None,
        "file_path": None,
        "expires_at": None,
        "created_at": now,
    }
    repository = SqlExportRepository()
    connection = FakeConnection([FakeResult(rows=[row]), FakeResult()])
    bind(repository, connection)

    task = await repository.create(
        principal=APPROVER,
        filters=filters(),
        decrypted=True,
    )

    assert task.id == 9 and task.public_id == PUBLIC_ID and task.decrypted
    assert "13800138000" not in repr(connection.calls)
    assert "phone_hmacs" in connection.calls[0][1]["filters"]
    create_sql, create_params = connection.calls[0]
    assert "creator_account_id" in create_sql
    assert "scope_resolved" in create_sql
    assert create_params["creator_account_id"] == 21
    assert create_params["creator_identity_id"] == 121
    assert create_params["scope_dept"] == "平台部"
    audit_sql, audit_params = connection.calls[1]
    assert "INSERT INTO audit_log" in audit_sql
    assert "CAST(:decrypted AS boolean)" in audit_sql
    assert "CAST(:batch_no AS text)" in audit_sql
    assert "CAST(:dataset AS text)" in audit_sql
    assert audit_params == {
        "actor": "approver-a",
        "actor_account_id": 21,
        "actor_identity_id": 121,
        "actor_role": "approver",
        "public_id": str(PUBLIC_ID),
        "decrypted": True,
        "batch_no": "BATCH-1",
        "dataset": "message",
        "scope_dept": "平台部",
    }
    assert "a" * 64 not in repr(audit_params)


@pytest.mark.asyncio
async def test_claim_reclaims_stale_running_task_with_database_lease() -> None:
    repository = SqlExportRepository()
    expires_at = datetime(2026, 7, 12, 8, 15, tzinfo=UTC)
    connection = FakeConnection(
        [
            FakeResult(
                rows=[
                    {
                        "id": 9,
                        "filters": filters().safe_json(),
                        "decrypted": False,
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

    claim = await repository.claim(9, lease_seconds=900)

    assert claim == ExportClaim(9, filters(), False, LEASE_ID, expires_at)
    sql = connection.calls[0][0]
    assert "lease_expires_at<=now()" in sql
    assert "status='running'" in sql and "lease_id=:lease_id" in sql
    assert "worker_lease_event" in connection.calls[1][0]


@pytest.mark.asyncio
async def test_status_access_uses_uuid_stable_subject_and_explicit_scope_matrix() -> None:
    now = datetime(2026, 7, 12, tzinfo=UTC)
    row = {
        "id": 9,
        "public_id": PUBLIC_ID,
        "status": "done",
        "decrypted": False,
        "row_count": 2,
        "file_path": "/safe/export-9.smsx",
        "expires_at": now,
        "created_at": now,
    }
    repository = SqlExportRepository()
    connection = FakeConnection([FakeResult(rows=[row]), FakeResult(scalar=9)])
    bind(repository, connection)

    assert (
        await repository.get_accessible(
            PUBLIC_ID,
            principal=VIEWER,
            retention_days=7,
        )
    ) is not None
    access_sql, access_params = connection.calls[0]
    assert "public_id=:public_id" in access_sql
    assert "creator_account_id IS NOT NULL" in access_sql
    assert "scope_resolved" in access_sql
    assert ":actor_role='approver'" in access_sql
    assert ":actor_role IN ('operator','viewer')" in access_sql
    assert "creator=:actor OR :elevated" not in access_sql
    assert access_params == {
        "public_id": str(PUBLIC_ID),
        "actor_account_id": 11,
        "actor_role": "viewer",
        "actor_dept": "平台部",
        "retention_days": 7,
    }
    await repository.mark_done(
        9,
        lease_id=LEASE_ID,
        file_path="/safe/export-9.smsx",
        row_count=2,
    )
    assert "status='done'" in connection.calls[1][0]
    assert "lease_id=:lease_id" in connection.calls[1][0]
    assert "lease_expires_at>now()" in connection.calls[1][0]


@pytest.mark.asyncio
async def test_download_authorization_and_pii_free_audit_share_one_transaction() -> None:
    now = datetime(2026, 7, 12, tzinfo=UTC)
    row = {
        "id": 9,
        "public_id": PUBLIC_ID,
        "status": "done",
        "decrypted": True,
        "row_count": 2,
        "file_path": "/safe/export-9.smsx",
        "expires_at": now,
        "scope_dept": "平台部",
        "created_at": now,
    }
    repository = SqlExportRepository()
    connection = FakeConnection([FakeResult(rows=[row]), FakeResult()])
    bind(repository, connection)

    task = await repository.get_downloadable_and_audit(
        PUBLIC_ID,
        principal=APPROVER,
        ip="10.0.0.8",
        retention_days=7,
    )

    assert task is not None and task.public_id == PUBLIC_ID
    select_sql, select_params = connection.calls[0]
    assert "status='done'" in select_sql
    assert "scope_resolved" in select_sql
    assert "FOR SHARE" in select_sql
    assert select_params["actor_account_id"] == 21
    audit_sql, audit_params = connection.calls[1]
    assert "INSERT INTO audit_log" in audit_sql
    assert "'export_download'" in audit_sql
    assert "'actor_account_id'" in audit_sql
    assert audit_params == {
        "actor": "approver-a",
        "actor_account_id": 21,
        "actor_identity_id": 121,
        "actor_role": "approver",
        "ip": "10.0.0.8",
        "public_id": str(PUBLIC_ID),
        "scope_dept": "平台部",
        "decrypted": True,
        "row_count": 2,
    }
    assert "phone" not in repr(audit_params).casefold()
