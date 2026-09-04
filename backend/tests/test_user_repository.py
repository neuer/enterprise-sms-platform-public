from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from app.services.user_management import (
    LastAdminProtected,
    SelfDisableDenied,
    UserQuery,
)
from app.services.user_repository import SqlUserManagementRepository

NOW = datetime(2026, 7, 16, 8, tzinfo=UTC)


def row(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "account_id": 8,
        "identity_id": 18,
        "provider_id": 2,
        "provider_code": "ad",
        "username": "operator01",
        "display_name": "目录操作员",
        "dept": "业务一部",
        "role": "operator",
        "role_override": False,
        "status": 1,
        "identity_status": 1,
        "must_change_password": None,
        "source_groups": ["sms-operators", "sms-approvers"],
        "last_synced_at": NOW,
        "last_login_at": NOW,
        "security_version": 4,
    }
    value.update(updates)
    return value


class FakeResult:
    def __init__(
        self,
        rows: list[dict[str, object]] | None = None,
        scalar: object = None,
    ) -> None:
        self.rows = rows or []
        self.scalar = scalar

    def mappings(self) -> FakeResult:
        return self

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self.rows)

    def one_or_none(self) -> dict[str, object] | None:
        assert len(self.rows) <= 1
        return self.rows[0] if self.rows else None

    def one(self) -> dict[str, object]:
        assert len(self.rows) == 1
        return self.rows[0]

    def scalar_one(self) -> object:
        return self.scalar

    def scalar_one_or_none(self) -> object:
        return self.scalar


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


def repository(
    results: list[FakeResult],
) -> tuple[SqlUserManagementRepository, FakeConnection]:
    connection = FakeConnection(results)
    value = SqlUserManagementRepository()
    value._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]
    return value, connection


@pytest.mark.asyncio
async def test_user_list_filters_joined_account_identity_and_credential_projection() -> None:
    repo, connection = repository([FakeResult(scalar=1), FakeResult([row()])])

    page = await repo.list(UserQuery("操作' OR 1=1", "ad", "operator", 1, 2, 20))

    assert page.total == 1 and page.items[0].provider_code == "ad"
    sql, params = connection.calls[1]
    assert "JOIN auth_identity" in sql and "JOIN auth_provider" in sql
    assert "LEFT JOIN local_credential" in sql
    assert "ORDER BY ua.updated_at DESC,ua.id DESC" in sql
    assert "操作' OR 1=1" not in sql
    assert params["keyword"] == "%操作' OR 1=1%"
    assert params["provider_code"] == "ad"
    assert params["status"] == 1 and params["offset"] == 20


@pytest.mark.asyncio
async def test_last_active_admin_cannot_be_demoted_and_rows_are_locked() -> None:
    current = row(role="admin", account_id=1, identity_id=11)
    repo, connection = repository(
        [FakeResult(), FakeResult([current]), FakeResult(), FakeResult()]
    )

    with pytest.raises(LastAdminProtected):
        await repo.set_role(
            1,
            "viewer",
            True,
            actor="admin",
            ip="10.0.0.8",
        )

    assert "pg_advisory_xact_lock" in connection.calls[0][0]
    assert "FOR UPDATE OF ua" in connection.calls[1][0]
    assert "external_role_mapping" in connection.calls[3][0]
    assert "FOR UPDATE OF ua,ai,ap" in connection.calls[3][0]
    assert len(connection.calls) == 4


@pytest.mark.asyncio
async def test_ad_role_follow_uses_external_mapping_and_increments_security_version() -> None:
    repo, connection = repository(
        [
            FakeResult(),
            FakeResult([row()]),
            FakeResult(
                [
                        {
                            "external_group": "sms-operators",
                            "role": "operator",
                            "dept": "业务一部",
                        },
                        {
                            "external_group": "sms-approvers",
                            "role": "approver",
                            "dept": "业务一部",
                        },
                ]
            ),
            FakeResult(),
            FakeResult(scalar=8),
            FakeResult(),
        ]
    )

    changed = await repo.set_role(
        8,
        "viewer",
        False,
        actor="admin",
        ip="10.0.0.8",
    )

    assert changed.role == "approver" and changed.security_version == 5
    update_sql, update_params = connection.calls[3]
    assert "security_version=security_version+1" in update_sql.replace(" ", "")
    assert update_params["account_id"] == 8
    assert update_params["role"] == "approver"
    assert update_params["dept"] == "业务一部"
    audit_params = connection.calls[5][1]
    assert audit_params["object_id"] == "8"
    assert set(audit_params) == {
        "actor",
        "ip",
        "object_id",
        "before_role",
        "before_override",
        "after_role",
        "after_override",
    }


@pytest.mark.asyncio
async def test_repository_rejects_self_disable_before_update() -> None:
    repo, connection = repository(
        [FakeResult(), FakeResult([row(account_id=1, role="admin")])]
    )

    with pytest.raises(SelfDisableDenied):
        await repo.set_status(
            1,
            0,
            actor_account_id=1,
            actor="admin",
            ip="10.0.0.8",
        )

    assert len(connection.calls) == 2
    assert "pg_advisory_xact_lock" in connection.calls[0][0]
    assert "FOR UPDATE" in connection.calls[1][0]


@pytest.mark.asyncio
async def test_repository_rejects_disabling_last_active_admin() -> None:
    repo, connection = repository(
        [
            FakeResult(),
            FakeResult([row(account_id=2, role="admin")]),
            FakeResult(),
            FakeResult(),
        ]
    )

    with pytest.raises(LastAdminProtected):
        await repo.set_status(
            2,
            0,
            actor_account_id=1,
            actor="other.admin",
            ip="10.0.0.8",
        )

    assert "pg_advisory_xact_lock" in connection.calls[0][0]
    assert "FOR UPDATE" in connection.calls[1][0]
    assert "external_role_mapping" in connection.calls[3][0]
    assert len(connection.calls) == 4


@pytest.mark.asyncio
async def test_local_password_reset_forces_change_and_audit_excludes_hash() -> None:
    local = row(
        provider_code="local",
        role_override=True,
        must_change_password=False,
        source_groups=[],
        last_synced_at=None,
    )
    repo, connection = repository(
        [
            FakeResult([local]),
            FakeResult(),
            FakeResult(),
            FakeResult(),
            FakeResult(),
        ]
    )

    changed = await repo.reset_local_password(
        8,
        "$argon2id$v=19$new-hash",
        actor="admin",
        ip="10.0.0.8",
    )

    assert changed.must_change_password and changed.security_version == 5
    assert "must_change_password=TRUE" in connection.calls[1][0]
    assert "credential_version=credential_version+1" in connection.calls[1][0].replace(" ", "")
    assert "security_version=security_version+1" in connection.calls[2][0].replace(" ", "")
    assert "password_change_token" in connection.calls[3][0]
    audit_sql, audit_params = connection.calls[4]
    assert "local_password_reset" in audit_sql
    assert "'credential_change_required',TRUE" in audit_sql
    assert "'must_change_password',TRUE" not in audit_sql
    assert "$argon2id" not in str(audit_params)


@pytest.mark.asyncio
async def test_local_create_is_single_transaction_and_audit_has_no_hash() -> None:
    created = row(
        account_id=9,
        identity_id=19,
        provider_code="local",
        username="new.user",
        display_name="新用户",
        role="viewer",
        role_override=True,
        must_change_password=True,
        source_groups=[],
        last_synced_at=None,
    )
    repo, connection = repository(
        [
            FakeResult(scalar=1),
            FakeResult(scalar=9),
            FakeResult(scalar=19),
            FakeResult(),
            FakeResult(),
            FakeResult([created]),
        ]
    )

    value = await repo.create_local(
        username="new.user",
        display_name="新用户",
        dept="业务一部",
        role="viewer",
        password_hash="$argon2id$v=19$new-hash",
        actor="admin",
        ip="10.0.0.8",
    )

    assert value.account_id == 9 and value.provider_code == "local"
    assert "INSERT INTO user_account" in connection.calls[1][0]
    assert "INSERT INTO auth_identity" in connection.calls[2][0]
    assert "INSERT INTO local_credential" in connection.calls[3][0]
    audit_sql, audit_params = connection.calls[4]
    assert "local_account_create" in audit_sql
    assert "'role',CAST(:target_role AS text)" in audit_sql
    assert "$argon2id" not in str(audit_params)
    assert connection.calls[2][1]["normalized_login_name"] == "new.user"
