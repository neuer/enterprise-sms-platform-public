"""日常改密 security/credential version CAS 的真实 PostgreSQL 屏障合同。"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.auth.backends import InvalidCredentials
from app.core.auth.users import AuthContextChanged, SqlUserRepository
from app.core.runtime_resources import close_runtime_resources

pytestmark = pytest.mark.skipif(
    "SECURITY_SESSION_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


async def _create_local(
    engine: Any,
    login_name: str,
    *,
    must_change: bool = False,
) -> tuple[int, int, int, int]:
    async with engine.begin() as connection:
        provider_id = int(
            (
                await connection.execute(text("SELECT id FROM auth_provider WHERE code='local'"))
            ).scalar_one()
        )
        account_id = int(
            (
                await connection.execute(
                    text(
                        """
                        INSERT INTO user_account(display_name,dept,role,role_override,status)
                        VALUES(:display_name,'测试部','operator',TRUE,1)
                        RETURNING id
                        """
                    ),
                    {"display_name": login_name},
                )
            ).scalar_one()
        )
        identity_id = int(
            (
                await connection.execute(
                    text(
                        """
                        INSERT INTO auth_identity(
                          account_id,provider_id,login_name,
                          normalized_login_name,external_subject,status
                        ) VALUES(
                          :account_id,:provider_id,:login_name,
                          :login_name,:external_subject,1
                        ) RETURNING id
                        """
                    ),
                    {
                        "account_id": account_id,
                        "provider_id": provider_id,
                        "login_name": login_name,
                        "external_subject": f"local:{login_name}",
                    },
                )
            ).scalar_one()
        )
        await connection.execute(
            text(
                """
                INSERT INTO local_credential(
                  identity_id,password_hash,must_change_password
                ) VALUES(:identity_id,'old-hash',:must_change)
                """
            ),
            {"identity_id": identity_id, "must_change": must_change},
        )
        row = (
            await connection.execute(
                text(
                    """
                    SELECT ua.security_version,lc.credential_version
                    FROM user_account ua
                    JOIN local_credential lc ON lc.identity_id=:identity_id
                    WHERE ua.id=:account_id
                    """
                ),
                {"account_id": account_id, "identity_id": identity_id},
            )
        ).mappings().one()
    return account_id, identity_id, int(row["security_version"]), int(row["credential_version"])


def _users() -> SqlUserRepository:
    database_url = make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"])
    return SqlUserRepository(
        cast(Any, SimpleNamespace(database_url_for=lambda _role: database_url))
    )


async def _admin_reset_password(
    engine: Any,
    *,
    account_id: int,
    identity_id: int,
    password_hash: str,
) -> None:
    """复现管理员重置的版本递增、强制改密与未消费令牌吊销，不绕过 CAS 合同。"""

    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                UPDATE local_credential SET
                  password_hash=:password_hash,must_change_password=TRUE,
                  credential_version=credential_version+1,
                  password_changed_at=NULL,updated_at=now()
                WHERE identity_id=:identity_id
                """
            ),
            {"identity_id": identity_id, "password_hash": password_hash},
        )
        await connection.execute(
            text(
                """
                UPDATE user_account SET security_version=security_version+1,
                  updated_at=now() WHERE id=:account_id
                """
            ),
            {"account_id": account_id},
        )
        await connection.execute(
            text(
                """
                UPDATE password_change_token SET
                  status='revoked',
                  processing_lease_id=NULL,
                  processing_lease_expires_at=NULL
                WHERE account_id=:account_id
                  AND status IN ('available','processing')
                """
            ),
            {"account_id": account_id},
        )


@pytest.mark.asyncio
async def test_admin_password_reset_wins_against_inflight_user_change() -> None:
    database_url = make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    users = _users()
    login_name = f"cas-admin-win-{os.getpid()}"
    account_id, identity_id, security_version, credential_version = await _create_local(
        engine, login_name
    )
    ready = asyncio.Event()

    async def inflight() -> None:
        await ready.wait()
        await users.change_local_password(
            account_id=account_id,
            identity_id=identity_id,
            password_hash="user-new-hash",
            actor=login_name,
            ip="10.0.0.8",
            expected_security_version=security_version,
            expected_credential_version=credential_version,
        )

    task = asyncio.create_task(inflight())
    await _admin_reset_password(
        engine,
        account_id=account_id,
        identity_id=identity_id,
        password_hash="admin-temp-hash",
    )
    ready.set()
    with pytest.raises(AuthContextChanged):
        await task
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    """
                    SELECT password_hash,must_change_password,credential_version
                    FROM local_credential WHERE identity_id=:identity_id
                    """
                ),
                {"identity_id": identity_id},
            )
        ).mappings().one()
        audits = await connection.scalar(
            text(
                """
                SELECT COUNT(*) FROM audit_log
                WHERE action='local_password_change' AND object_id=:object_id
                """
            ),
            {"object_id": str(account_id)},
        )
    assert row["password_hash"] == "admin-temp-hash"
    assert row["must_change_password"] is True
    assert int(row["credential_version"]) == credential_version + 1
    assert int(audits) == 0
    await close_runtime_resources()
    await engine.dispose()


@pytest.mark.asyncio
async def test_account_disable_wins_against_inflight_password_change() -> None:
    database_url = make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    users = _users()
    login_name = f"cas-account-off-{os.getpid()}"
    account_id, identity_id, security_version, credential_version = await _create_local(
        engine, login_name
    )
    ready = asyncio.Event()

    async def inflight() -> None:
        await ready.wait()
        await users.change_local_password(
            account_id=account_id,
            identity_id=identity_id,
            password_hash="stale-hash",
            actor=login_name,
            ip="10.0.0.8",
            expected_security_version=security_version,
            expected_credential_version=credential_version,
        )

    task = asyncio.create_task(inflight())
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE user_account SET status=0,security_version=security_version+1 WHERE id=:id"
            ),
            {"id": account_id},
        )
    ready.set()
    with pytest.raises(AuthContextChanged):
        await task
    await close_runtime_resources()
    await engine.dispose()


@pytest.mark.asyncio
async def test_identity_disable_wins_against_inflight_password_change() -> None:
    database_url = make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    users = _users()
    login_name = f"cas-identity-off-{os.getpid()}"
    account_id, identity_id, security_version, credential_version = await _create_local(
        engine, login_name
    )
    ready = asyncio.Event()

    async def inflight() -> None:
        await ready.wait()
        await users.change_local_password(
            account_id=account_id,
            identity_id=identity_id,
            password_hash="stale-hash",
            actor=login_name,
            ip="10.0.0.8",
            expected_security_version=security_version,
            expected_credential_version=credential_version,
        )

    task = asyncio.create_task(inflight())
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE auth_identity SET status=0 WHERE id=:id"),
            {"id": identity_id},
        )
    ready.set()
    with pytest.raises(AuthContextChanged):
        await task
    await close_runtime_resources()
    await engine.dispose()


@pytest.mark.asyncio
async def test_provider_disable_wins_against_inflight_password_change() -> None:
    database_url = make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    users = _users()
    login_name = f"cas-provider-off-{os.getpid()}"
    account_id, identity_id, security_version, credential_version = await _create_local(
        engine, login_name
    )
    ready = asyncio.Event()

    async def inflight() -> None:
        await ready.wait()
        await users.change_local_password(
            account_id=account_id,
            identity_id=identity_id,
            password_hash="stale-hash",
            actor=login_name,
            ip="10.0.0.8",
            expected_security_version=security_version,
            expected_credential_version=credential_version,
        )

    task = asyncio.create_task(inflight())
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE auth_provider SET enabled=FALSE WHERE code='local'"))
    try:
        ready.set()
        with pytest.raises(AuthContextChanged):
            await task
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE auth_provider SET enabled=TRUE WHERE code='local'")
            )
        await close_runtime_resources()
        await engine.dispose()


@pytest.mark.asyncio
async def test_force_logout_security_version_change_blocks_inflight_password_change() -> None:
    database_url = make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    users = _users()
    login_name = f"cas-force-logout-{os.getpid()}"
    account_id, identity_id, security_version, credential_version = await _create_local(
        engine, login_name
    )
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE user_account SET security_version=security_version+1 WHERE id=:id"),
            {"id": account_id},
        )
    with pytest.raises(AuthContextChanged):
        await users.change_local_password(
            account_id=account_id,
            identity_id=identity_id,
            password_hash="stale-hash",
            actor=login_name,
            ip="10.0.0.8",
            expected_security_version=security_version,
            expected_credential_version=credential_version,
        )
    await close_runtime_resources()
    await engine.dispose()


@pytest.mark.asyncio
async def test_two_concurrent_daily_password_changes_have_exactly_one_winner() -> None:
    database_url = make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    users = _users()
    login_name = f"cas-two-winners-{os.getpid()}"
    account_id, identity_id, security_version, credential_version = await _create_local(
        engine, login_name
    )
    barrier = asyncio.Barrier(2)

    async def attempt(hash_value: str) -> str:
        await barrier.wait()
        await users.change_local_password(
            account_id=account_id,
            identity_id=identity_id,
            password_hash=hash_value,
            actor=login_name,
            ip="10.0.0.8",
            expected_security_version=security_version,
            expected_credential_version=credential_version,
        )
        return hash_value

    results = await asyncio.gather(
        attempt("winner-a"),
        attempt("winner-b"),
        return_exceptions=True,
    )
    successes = [item for item in results if isinstance(item, str)]
    failures = [item for item in results if isinstance(item, AuthContextChanged)]
    assert len(successes) == 1
    assert len(failures) == 1
    async with engine.connect() as connection:
        current = await connection.scalar(
            text("SELECT password_hash FROM local_credential WHERE identity_id=:id"),
            {"id": identity_id},
        )
        audits = await connection.scalar(
            text(
                """
                SELECT COUNT(*) FROM audit_log
                WHERE action='local_password_change' AND object_id=:object_id
                """
            ),
            {"object_id": str(account_id)},
        )
    assert current in {"winner-a", "winner-b"}
    assert int(audits) == 1
    await close_runtime_resources()
    await engine.dispose()


@pytest.mark.asyncio
async def test_stale_password_change_does_not_clear_must_change_password() -> None:
    database_url = make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    users = _users()
    login_name = f"cas-must-change-{os.getpid()}"
    account_id, identity_id, security_version, credential_version = await _create_local(
        engine, login_name
    )
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                UPDATE local_credential
                SET must_change_password=TRUE,credential_version=credential_version+1
                WHERE identity_id=:id
                """
            ),
            {"id": identity_id},
        )
    with pytest.raises(AuthContextChanged):
        await users.change_local_password(
            account_id=account_id,
            identity_id=identity_id,
            password_hash="stale-hash",
            actor=login_name,
            ip="10.0.0.8",
            expected_security_version=security_version,
            expected_credential_version=credential_version,
        )
    async with engine.connect() as connection:
        flag = await connection.scalar(
            text("SELECT must_change_password FROM local_credential WHERE identity_id=:id"),
            {"id": identity_id},
        )
    assert flag is True
    await close_runtime_resources()
    await engine.dispose()


@pytest.mark.asyncio
async def test_credential_version_increments_on_every_password_mutation() -> None:
    database_url = make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    users = _users()
    login_name = f"cas-versions-{os.getpid()}"
    account_id, identity_id, security_version, credential_version = await _create_local(
        engine, login_name
    )
    await users.change_local_password(
        account_id=account_id,
        identity_id=identity_id,
        password_hash="first-hash",
        actor=login_name,
        ip="10.0.0.8",
        expected_security_version=security_version,
        expected_credential_version=credential_version,
    )
    await _admin_reset_password(
        engine,
        account_id=account_id,
        identity_id=identity_id,
        password_hash="reset-hash",
    )
    async with engine.connect() as connection:
        version = await connection.scalar(
            text("SELECT credential_version FROM local_credential WHERE identity_id=:id"),
            {"id": identity_id},
        )
    assert int(version) == credential_version + 2
    await close_runtime_resources()
    await engine.dispose()


@pytest.mark.asyncio
async def test_initial_password_token_is_invalidated_by_admin_reset() -> None:
    database_url = make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    users = _users()
    login_name = f"cas-token-revoked-{os.getpid()}"
    account_id, identity_id, security_version, _credential_version = await _create_local(
        engine, login_name, must_change=True
    )
    token_hash = "c" * 64
    await users.create_password_change_token(
        token_hash=token_hash,
        account_id=account_id,
        identity_id=identity_id,
        provider_code="local",
        login_name=login_name,
        security_version=security_version,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    await _admin_reset_password(
        engine,
        account_id=account_id,
        identity_id=identity_id,
        password_hash="reset-hash",
    )
    with pytest.raises(InvalidCredentials):
        await users.claim_password_change_token(
            token_hash=token_hash,
            account_id=account_id,
            identity_id=identity_id,
            provider_code="local",
            login_name=login_name,
        )
    await close_runtime_resources()
    await engine.dispose()
