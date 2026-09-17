"""临时凭据截止、独立短令牌和历史一次性宽限的数据库回归。"""

from __future__ import annotations

import importlib
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.auth.backends import InvalidCredentials
from app.core.auth.temporary_password import TEMPORARY_PASSWORD_EXPIRY_SQL
from tests.integration.test_daily_password_cas_postgres import _create_local, _users

pytestmark = pytest.mark.skipif(
    "SECURITY_SESSION_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


def _name() -> str:
    return "temp-" + uuid4().hex.translate(str.maketrans("0123456789", "ghijklmnop"))


@pytest.mark.asyncio
async def test_expired_password_cannot_mint_token_but_issued_token_can_complete() -> None:
    engine = create_async_engine(make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"]))
    users = _users()
    name = _name()
    account_id, identity_id, version, _ = await _create_local(engine, name, must_change=True)
    params = dict(
        account_id=account_id, identity_id=identity_id, provider_code="local", login_name=name
    )
    try:
        assert (await users.find_local_account(name)).temporary_password_valid
        await users.create_password_change_token(
            **params,
            token_hash="a" + uuid4().hex + "b" * 31,
            security_version=version,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        token_hash = "c" + uuid4().hex + "d" * 31
        await users.create_password_change_token(
            **params,
            token_hash=token_hash,
            security_version=version,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        async with engine.begin() as connection:
            await connection.execute(
                text("""
                UPDATE local_credential SET temporary_password_expires_at=clock_timestamp()
                WHERE identity_id=:id
            """),
                {"id": identity_id},
            )
        assert not (await users.find_local_account(name)).temporary_password_valid
        with pytest.raises(InvalidCredentials):
            await users.create_password_change_token(
                **params,
                token_hash="e" + uuid4().hex + "f" * 31,
                security_version=version,
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
        claim = await users.claim_password_change_token(**params, token_hash=token_hash)
        await users.consume_password_change_and_update(
            **params,
            token_id=claim.token_id,
            lease_id=claim.lease_id,
            password_hash="new-test-hash",
            actor=name,
            ip="127.0.0.1",
        )
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text("""
                SELECT must_change_password,temporary_password_expires_at
                FROM local_credential WHERE identity_id=:id
            """),
                    {"id": identity_id},
                )
            ).one()
            assert tuple(row) == (False, None)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_shared_deadline_uses_database_policy_and_normal_password_is_unchanged() -> None:
    engine = create_async_engine(make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"]))
    users = _users()
    name = _name()
    await _create_local(engine, name)
    try:
        record = await users.find_local_account(name)
        assert not record.account.must_change_password
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                for hours in (1, 24, 168):
                    await connection.execute(
                        text(
                            "UPDATE sys_config SET value=:hours "
                            "WHERE key='local_temporary_password_ttl_hours'"
                        ),
                        {"hours": str(hours)},
                    )
                    deadline, now = (
                        await connection.execute(
                            text(f"SELECT {TEMPORARY_PASSWORD_EXPIRY_SQL}, clock_timestamp()")
                        )
                    ).one()
                    assert hours * 3600 - 1 < (deadline - now).total_seconds() <= hours * 3600
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_grants_historical_password_one_grace_without_extending_existing(
    monkeypatch,
) -> None:
    engine = create_async_engine(make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"]))
    name = _name()
    _, identity_id, _, _ = await _create_local(engine, name, must_change=True)
    migration = importlib.import_module("migrations.versions.0119_temporary_password_expiry")
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(
                    text(
                        "ALTER TABLE local_credential DROP CONSTRAINT "
                        "ck_local_temporary_password_expiry"
                    )
                )
                await connection.execute(
                    text(
                        "UPDATE local_credential SET temporary_password_expires_at=NULL "
                        "WHERE identity_id=:id"
                    ),
                    {"id": identity_id},
                )

                def upgrade(sync):
                    monkeypatch.setattr(
                        migration,
                        "op",
                        SimpleNamespace(execute=lambda sql: sync.execute(text(sql))),
                    )
                    migration.upgrade()

                await connection.run_sync(upgrade)
                first, now = (
                    await connection.execute(
                        text(
                            "SELECT temporary_password_expires_at,clock_timestamp() "
                            "FROM local_credential WHERE identity_id=:id"
                        ),
                        {"id": identity_id},
                    )
                ).one()
                assert 86390 < (first - now).total_seconds() <= 86400
                await connection.run_sync(upgrade)
                second = await connection.scalar(
                    text(
                        "SELECT temporary_password_expires_at "
                        "FROM local_credential WHERE identity_id=:id"
                    ),
                    {"id": identity_id},
                )
                assert first == second

                def downgrade(sync):
                    monkeypatch.setattr(
                        migration,
                        "op",
                        SimpleNamespace(execute=lambda sql: sync.execute(text(sql))),
                    )
                    migration.downgrade()

                with pytest.raises(DBAPIError, match="temporary password downgrade unsafe"):
                    async with connection.begin_nested():
                        await connection.run_sync(downgrade)
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
