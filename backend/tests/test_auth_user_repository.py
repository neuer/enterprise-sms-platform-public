from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from app.core.auth.accounts import (
    AccountNotFound,
    AccountSourceConflict,
    LocalAccountRecord,
    PlatformAccount,
)
from app.core.auth.backends import AuthenticatedIdentity, InvalidCredentials
from app.core.auth.users import SqlUserRepository
from app.services.admin_invariant import ADMIN_INVARIANT_LOCK_ID


def account_row(**updates: object) -> dict[str, object]:
    row: dict[str, object] = {
        "account_id": 8,
        "identity_id": 18,
        "provider_code": "local",
        "login_name": "operator01",
        "normalized_login_name": "operator01",
        "display_name": "本地操作员",
        "dept": "业务一部",
        "role": "operator",
        "security_version": 3,
        "account_status": 1,
        "identity_status": 1,
        "provider_enabled": True,
        "must_change_password": True,
        "password_hash": "$argon2id$v=19$placeholder",
    }
    row.update(updates)
    return row


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

    def one_or_none(self) -> dict[str, object] | None:
        assert len(self.rows) <= 1
        return self.rows[0] if self.rows else None

    def one(self) -> dict[str, object]:
        assert len(self.rows) == 1
        return self.rows[0]

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self.rows)

    def scalar_one(self) -> object:
        return self.scalar

    def scalar_one_or_none(self) -> object:
        return self.scalar


class FakeConnection:
    def __init__(self, results: list[FakeResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, Any]] = []

    async def execute(self, statement: object, params: Any = None) -> FakeResult:
        sql = str(statement)
        if "txid_current()" in sql:
            return FakeResult([{"database_user": "sms_auth", "txid": 42}])
        self.calls.append((sql, params))
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
        self.disposed = False

    def begin(self) -> FakeContext:
        return FakeContext(self.connection)

    def connect(self) -> FakeContext:
        return FakeContext(self.connection)

    async def dispose(self) -> None:
        self.disposed = True


def repository(results: list[FakeResult]) -> tuple[SqlUserRepository, FakeConnection, FakeEngine]:
    connection = FakeConnection(results)
    engine = FakeEngine(connection)
    value = SqlUserRepository()
    value._engine = lambda: engine  # type: ignore[method-assign]
    return value, connection, engine


@pytest.mark.asyncio
async def test_local_login_projection_uses_provider_and_normalized_global_name() -> None:
    repo, connection, engine = repository([FakeResult([account_row()])])

    record = await repo.find_local_account("operator01")

    assert record == LocalAccountRecord(
        account=PlatformAccount(
            account_id=8,
            identity_id=18,
            provider_code="local",
            login_name="operator01",
            normalized_login_name="operator01",
            display_name="本地操作员",
            dept="业务一部",
            role="operator",
            security_version=3,
            account_enabled=True,
            identity_enabled=True,
            provider_enabled=True,
            must_change_password=True,
        ),
        password_hash="$argon2id$v=19$placeholder",
    )
    sql, params = connection.calls[0]
    assert "JOIN auth_identity" in sql and "JOIN auth_provider" in sql
    assert "JOIN local_credential" in sql
    assert "ap.code='local'" in sql
    assert "ai.normalized_login_name=:normalized_login_name" in sql
    assert params == {"normalized_login_name": "operator01"}
    assert engine.disposed


@pytest.mark.asyncio
async def test_create_local_account_identity_credential_and_audit_are_one_transaction() -> None:
    created_row = account_row(
        account_id=9,
        identity_id=19,
        login_name="new.user",
        normalized_login_name="new.user",
        display_name="新用户",
        role="viewer",
        password_hash="$argon2id$v=19$new",
    )
    repo, connection, engine = repository(
        [
            FakeResult(scalar=1),
            FakeResult(scalar=9),
            FakeResult(scalar=19),
            FakeResult(),
            FakeResult(),
            FakeResult([created_row]),
        ]
    )

    account = await repo.create_local_account(
        login_name="New.User",
        display_name="新用户",
        dept="业务一部",
        role="viewer",
        password_hash="$argon2id$v=19$new",
        actor="admin",
        ip="10.0.0.8",
    )

    assert account.account_id == 9 and account.identity_id == 19
    provider_sql, _ = connection.calls[0]
    assert "auth_provider" in provider_sql and "FOR SHARE" in provider_sql
    identity_sql, identity_params = connection.calls[2]
    assert "INSERT INTO auth_identity" in identity_sql
    assert identity_params["login_name"] == "new.user"
    assert identity_params["external_subject"] == "local:new.user"
    credential_sql, credential_params = connection.calls[3]
    assert "INSERT INTO local_credential" in credential_sql
    assert credential_params["password_hash"] == "$argon2id$v=19$new"
    audit_sql, audit_params = connection.calls[4]
    assert "local_account_create" in audit_sql
    assert "'role',CAST(:target_role AS text)" in audit_sql
    assert audit_params["object_id"] == "9"
    assert "$argon2id" not in str(audit_params)
    assert engine.disposed


@pytest.mark.asyncio
async def test_force_logout_targets_numeric_account_id_and_audits_same_id() -> None:
    repo, connection, engine = repository([FakeResult(scalar=8), FakeResult()])
    actor = PlatformAccount(
        1,
        11,
        "local",
        "admin",
        "admin",
        "管理员",
        "平台部",
        "admin",
        7,
        True,
        True,
    )

    await repo.invalidate_sessions(actor=actor, account_id=8, ip="10.0.0.9")

    update_sql, update_params = connection.calls[0]
    assert "user_account" in update_sql
    assert "security_version = security_version + 1" in update_sql
    assert update_params == {"account_id": 8}
    audit_sql, audit_params = connection.calls[1]
    assert "force_logout" in audit_sql
    assert audit_params["object_id"] == "8"
    assert engine.disposed


@pytest.mark.asyncio
async def test_force_logout_rejects_missing_account_without_audit() -> None:
    repo, connection, _ = repository([FakeResult()])
    actor = PlatformAccount(
        1,
        11,
        "local",
        "admin",
        "admin",
        "管理员",
        "平台部",
        "admin",
        7,
        True,
        True,
    )

    with pytest.raises(AccountNotFound):
        await repo.invalidate_sessions(actor=actor, account_id=999, ip="10.0.0.9")

    assert len(connection.calls) == 1


@pytest.mark.asyncio
async def test_security_session_loader_reads_stable_account_and_identity() -> None:
    repo, connection, engine = repository([FakeResult([account_row()])])

    projection = await repo.load_security_session(42, 77)
    assert projection.security_version == 3
    assert projection.active

    sql, params = connection.calls[0]
    assert "FROM user_account" in sql
    assert "local_credential" not in sql
    assert "password_hash" not in sql
    assert params == {"account_id": 42, "identity_id": 77}
    assert engine.disposed


@pytest.mark.asyncio
async def test_password_change_updates_hash_and_version_without_hash_in_audit() -> None:
    repo, connection, engine = repository([FakeResult(scalar=18), FakeResult(), FakeResult()])

    await repo.change_local_password(
        account_id=8,
        identity_id=18,
        password_hash="$argon2id$v=19$new-secret-hash",
        actor="admin",
        ip="10.0.0.8",
    )

    credential_sql, credential_params = connection.calls[0]
    assert "UPDATE local_credential" in credential_sql
    assert "must_change_password=FALSE" in credential_sql
    assert credential_params["password_hash"] == "$argon2id$v=19$new-secret-hash"
    assert "security_version=security_version+1" in connection.calls[1][0]
    audit_sql, audit_params = connection.calls[2]
    assert "local_password_change" in audit_sql
    assert audit_params["account_id"] == 8
    assert audit_params["identity_id"] == 18
    assert "$argon2id" not in str(audit_params)
    assert engine.disposed


@pytest.mark.asyncio
async def test_password_change_token_is_stored_as_bound_hash_only() -> None:
    repo, connection, engine = repository([FakeResult(scalar=91)])
    expires_at = datetime(2026, 7, 29, 9, 10, tzinfo=UTC)

    await repo.create_password_change_token(
        token_hash="a" * 64,
        account_id=8,
        identity_id=18,
        provider_code="local",
        login_name="operator01",
        security_version=3,
        expires_at=expires_at,
    )

    sql, params = connection.calls[0]
    assert "INSERT INTO password_change_token" in sql
    assert "lc.must_change_password=TRUE" in sql
    assert params == {
        "token_hash": "a" * 64,
        "account_id": 8,
        "identity_id": 18,
        "provider_code": "local",
        "login_name": "operator01",
        "security_version": 3,
        "expires_at": expires_at,
    }
    assert "jwt" not in str(params).casefold()
    assert engine.disposed


@pytest.mark.asyncio
async def test_initial_password_change_consumes_token_and_updates_password_atomically() -> None:
    lease_id = uuid4()
    repo, connection, engine = repository(
        [
            FakeResult(scalar=91),
            FakeResult(scalar=18),
            FakeResult(),
            FakeResult(),
            FakeResult(),
            FakeResult(),
        ]
    )

    await repo.consume_password_change_and_update(
        token_id=91,
        lease_id=lease_id,
        account_id=8,
        identity_id=18,
        provider_code="local",
        login_name="operator01",
        password_hash="$argon2id$v=19$new-secret-hash",
        actor="operator01",
        ip="10.0.0.8",
    )

    lock_sql, lock_params = connection.calls[0]
    assert "FOR UPDATE OF pct,ua" in lock_sql
    assert "pct.status='processing'" in lock_sql
    assert "pct.processing_lease_id=:lease_id" in lock_sql
    assert "pct.processing_lease_expires_at>now()" in lock_sql
    assert "pct.expires_at>now()" in lock_sql
    assert "pct.issued_security_version=ua.security_version" in lock_sql
    assert lock_params["token_id"] == 91
    assert lock_params["lease_id"] == lease_id
    credential_sql, credential_params = connection.calls[1]
    assert "must_change_password=FALSE" in credential_sql
    assert credential_params["password_hash"] == "$argon2id$v=19$new-secret-hash"
    assert "security_version=security_version+1" in connection.calls[2][0]
    consume_sql, consume_params = connection.calls[3]
    assert "status=CASE WHEN id=:token_id THEN 'consumed' ELSE 'revoked' END" in (
        consume_sql
    )
    assert consume_params == {"token_id": 91, "account_id": 8}
    context_sql, context_params = connection.calls[4]
    assert "sms.audit_subject_kind" in context_sql
    assert context_params["account_id"] == "8"
    audit_sql, audit_params = connection.calls[5]
    assert "local_password_change" in audit_sql
    assert "$argon2id" not in str(audit_params)
    assert engine.disposed


@pytest.mark.asyncio
async def test_initial_password_change_rejects_used_or_stale_token_without_updates() -> None:
    repo, connection, engine = repository([FakeResult()])

    with pytest.raises(InvalidCredentials, match="改密令牌"):
        await repo.consume_password_change_and_update(
            token_id=91,
            lease_id=uuid4(),
            account_id=8,
            identity_id=18,
            provider_code="local",
            login_name="operator01",
            password_hash="$argon2id$v=19$new-secret-hash",
            actor="operator01",
            ip="10.0.0.8",
        )

    assert len(connection.calls) == 1
    assert engine.disposed


@pytest.mark.asyncio
async def test_initial_password_change_claims_and_releases_fenced_lease() -> None:
    lease_expires_at = datetime(2026, 7, 29, 9, 10, 30, tzinfo=UTC)
    repo, connection, engine = repository(
        [
            FakeResult(
                [
                    {
                        "id": 91,
                        "status": "available",
                        "processing_lease_expires_at": None,
                        "lease_active": None,
                        "password_hash": "$argon2id$v=19$current",
                    }
                ]
            ),
            FakeResult(scalar=lease_expires_at),
            FakeResult(scalar=91),
        ]
    )

    claim = await repo.claim_password_change_token(
        token_hash="b" * 64,
        account_id=8,
        identity_id=18,
        provider_code="local",
        login_name="operator01",
    )
    released = await repo.release_password_change_token(
        token_id=claim.token_id,
        lease_id=claim.lease_id,
    )

    assert claim.token_id == 91
    assert claim.lease_expires_at == lease_expires_at
    assert claim.current_password_hash == "$argon2id$v=19$current"
    select_sql, select_params = connection.calls[0]
    assert "FOR UPDATE OF pct,ua" in select_sql
    assert "pct.token_hash=:token_hash" in select_sql
    assert select_params["token_hash"] == "b" * 64
    claim_sql, claim_params = connection.calls[1]
    assert "status='processing'" in claim_sql
    assert "INTERVAL '30 seconds'" in claim_sql
    assert claim_params["lease_id"] == claim.lease_id
    release_sql, release_params = connection.calls[2]
    assert "status='available'" in release_sql
    assert "processing_lease_id=:lease_id" in release_sql
    assert release_params == {"token_id": 91, "lease_id": claim.lease_id}
    assert released
    assert engine.disposed


@pytest.mark.asyncio
async def test_login_audit_casts_json_provider_for_asyncpg() -> None:
    connection = FakeConnection([FakeResult(), FakeResult()])
    account = PlatformAccount(
        8,
        18,
        "ad",
        "admin01",
        "admin01",
        "目录管理员",
        "平台部",
        "admin",
        1,
        True,
        True,
    )

    await SqlUserRepository._audit_login(connection, account, "10.0.0.8")

    assert "sms.audit_subject_kind" in connection.calls[0][0]
    sql, params = connection.calls[1]
    assert "'provider_code',CAST(:provider_code AS text)" in sql
    assert params["provider_code"] == "ad"


@pytest.mark.asyncio
async def test_valid_external_identity_conflict_is_rejected_before_account_creation() -> None:
    repo, connection, engine = repository(
        [
            FakeResult(),
            FakeResult(scalar=2),
            FakeResult(),
            FakeResult(scalar=88),
            FakeResult(),
            FakeResult(),
        ]
    )
    identity = AuthenticatedIdentity(
        provider_code="ad",
        login_name="Admin",
        external_subject="guid-ad-admin",
        display_name="目录管理员",
        dept="平台部",
        groups=("CN=SMS-Admins",),
    )

    with pytest.raises(AccountSourceConflict):
        await repo.resolve_identity(identity, "10.0.0.8")

    assert connection.calls[0][1] == {"lock_id": ADMIN_INVARIANT_LOCK_ID}
    assert connection.calls[3][1] == {"normalized_login_name": "admin"}
    assert all("INSERT INTO user_account" not in sql for sql, _ in connection.calls)
    system_context_sql, system_context_params = connection.calls[4]
    assert "sms.audit_action" in system_context_sql
    assert system_context_params["actor_name"] == "auth-system"
    assert system_context_params["action"] == "account_source_conflict"
    audit_sql, audit_params = connection.calls[5]
    assert "account_source_conflict" in audit_sql
    assert "'provider_code',CAST(:provider_code AS text)" in audit_sql
    assert audit_params["provider_code"] == "ad"
    assert "guid-ad-admin" not in str(audit_params)
    assert engine.disposed


@pytest.mark.asyncio
async def test_external_login_role_sync_preserves_effective_admin_invariant() -> None:
    authoritative = account_row(
        provider_code="ad",
        login_name="admin",
        normalized_login_name="admin",
        display_name="目录管理员",
        role="admin",
        must_change_password=False,
    )
    repo, connection, engine = repository(
        [
            FakeResult(),
            FakeResult(scalar=2),
            FakeResult(
                [
                    {
                        "account_id": 8,
                        "identity_id": 18,
                        "role": "admin",
                        "role_override": False,
                        "security_version": 3,
                        "account_status": 1,
                        "identity_status": 1,
                    }
                ]
            ),
                FakeResult(
                    [
                        {
                            "external_group": "CN=SMS-Admins",
                            "role": "admin",
                            "dept": "平台部",
                        }
                    ]
                ),
            FakeResult(),
            FakeResult(),
            FakeResult(scalar=8),
            FakeResult([authoritative]),
            FakeResult(),
            FakeResult(),
        ]
    )
    identity = AuthenticatedIdentity(
        provider_code="ad",
        login_name="Admin",
        external_subject="guid-ad-admin",
        display_name="目录管理员",
        dept="业务一部",
        groups=("CN=SMS-Admins",),
    )

    resolved = await repo.resolve_identity(identity, "10.0.0.8")

    assert resolved.role == "admin"
    assert connection.calls[4][1]["dept"] == "平台部"
    assert connection.calls[4][1]["dept"] != identity.dept
    assert connection.calls[0][1] == {"lock_id": ADMIN_INVARIANT_LOCK_ID}
    assert "SELECT ua.id" in connection.calls[6][0]
    assert "external_role_mapping" in connection.calls[6][0]
    assert engine.disposed
