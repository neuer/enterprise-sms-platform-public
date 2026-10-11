"""透明 Argon2 升级的真实仓储、会话与并发修改边界。"""
from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from uuid import uuid4

import pytest
from argon2 import PasswordHasher
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.auth.backends import InvalidCredentials, SessionStateUnavailable
from app.core.auth.jwt import JwtService
from app.core.auth.local import LocalPasswordProvider
from app.core.auth.passwords import LocalPasswordHasher
from app.core.correlation import correlation_scope
from app.core.runtime_resources import close_runtime_resources
from tests.integration.test_daily_password_cas_postgres import _create_local, _users
from tests.integration.test_security_session_postgres import MemoryStore, claims

pytestmark = pytest.mark.skipif(
    "SECURITY_SESSION_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)
PASSWORD = "Synthetic@Rehash123"


@pytest.fixture
async def context():
    engine = create_async_engine(make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"]))
    users = _users()
    login = f"rehash-{uuid4().hex[:16]}"
    aid, iid, _, _ = await _create_local(engine, login)
    encoded = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash(PASSWORD)
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE local_credential SET password_hash=:hash WHERE identity_id=:id"),
            {"hash": encoded, "id": iid},
        )
    record = await users.find_local_account(login)
    assert record is not None
    with correlation_scope(uuid4()):
        yield engine, users, record
    await close_runtime_resources()
    await engine.dispose()


async def snapshot(engine, identity_id):
    async with engine.connect() as connection:
        return dict((await connection.execute(text("""
            SELECT lc.password_hash,lc.credential_version,lc.must_change_password,
              lc.temporary_password_expires_at,lc.password_changed_at,ua.security_version
            FROM local_credential lc
            JOIN auth_identity ai ON ai.id=lc.identity_id
            JOIN user_account ua ON ua.id=ai.account_id WHERE lc.identity_id=:id
        """), {"id": identity_id})).mappings().one())


@pytest.mark.asyncio
async def test_real_provider_upgrades_old_hash_and_keeps_existing_access_session(context):
    engine, users, record = context
    hasher = LocalPasswordHasher()
    provider = LocalPasswordProvider(users, hasher)
    tokens = JwtService("synthetic-rehash-session-key-long-enough", MemoryStore(),
                        security_session_loader=users.load_security_session)
    token = tokens.issue(claims(record.account))
    before = await snapshot(engine, record.account.identity_id)
    identity = await provider.authenticate(record.account.login_name, PASSWORD)
    after = await snapshot(engine, record.account.identity_id)
    assert identity.account == record.account
    assert after["password_hash"] != before["password_hash"]
    assert hasher.verify(after["password_hash"], PASSWORD)
    assert not hasher.needs_rehash(after["password_hash"])
    assert {k: v for k, v in after.items() if k != "password_hash"} == {
        k: v for k, v in before.items() if k != "password_hash"
    }
    assert (await tokens.verify(token)).security_version == record.account.security_version
    await provider.authenticate(record.account.login_name, PASSWORD)
    assert await snapshot(engine, record.account.identity_id) == after
    async with engine.connect() as connection:
        audits = (await connection.execute(text("""
            SELECT after_val FROM audit_log
            WHERE action='local_password_rehash' AND actor_account_id=:id
        """), {"id": record.account.account_id})).scalars().all()
    assert audits == [{"provider_code": "local", "upgraded": True}]


@pytest.mark.asyncio
async def test_wrong_password_does_not_rehash(context):
    engine, users, record = context
    before = await snapshot(engine, record.account.identity_id)
    with pytest.raises(InvalidCredentials):
        await LocalPasswordProvider(users, LocalPasswordHasher()).authenticate(
            record.account.login_name, "Wrong@Synthetic123",
        )
    assert await snapshot(engine, record.account.identity_id) == before


@pytest.mark.asyncio
async def test_temporary_hash_upgrade_preserves_expiry_and_first_change_token(context):
    engine, users, record = context
    async with engine.begin() as connection:
        await connection.execute(text("""
            UPDATE local_credential SET must_change_password=TRUE,
              temporary_password_expires_at=now()+interval '1 hour'
            WHERE identity_id=:id
        """), {"id": record.account.identity_id})
    record = await users.find_local_account(record.account.login_name)
    before = await snapshot(engine, record.account.identity_id)
    from datetime import UTC, datetime, timedelta
    token_hash = uuid4().hex * 2
    await users.create_password_change_token(
        token_hash=token_hash, account_id=record.account.account_id,
        identity_id=record.account.identity_id, provider_code="local",
        login_name=record.account.login_name, security_version=record.account.security_version,
        expires_at=datetime.now(UTC)+timedelta(minutes=5),
    )
    result = await LocalPasswordProvider(users, LocalPasswordHasher()).authenticate(
        record.account.login_name, PASSWORD,
    )
    assert result.account.must_change_password
    after = await snapshot(engine, record.account.identity_id)
    assert before.pop("password_hash") != after.pop("password_hash")
    assert after == before
    claim = await users.claim_password_change_token(
        token_hash=token_hash, account_id=record.account.account_id,
        identity_id=record.account.identity_id, provider_code="local",
        login_name=record.account.login_name,
    )
    assert claim.issued_credential_version == record.credential_version


@pytest.mark.parametrize("mutation", ["reset", "change", "account", "identity", "version",
                                      "hash_only", "expiry", "provider"])
@pytest.mark.asyncio
async def test_concurrent_authority_change_wins_while_rehash_cpu_is_inflight(
    context, monkeypatch, mutation,
):
    engine, users, record = context
    ready, proceed = asyncio.Event(), asyncio.Event()
    import app.core.auth.local as local
    original = local.run_bounded

    async def bounded(function, *args, **kwargs):
        if function.__name__ == "hash":
            ready.set()
            await proceed.wait()
        return await original(function, *args, **kwargs)

    monkeypatch.setattr(local, "run_bounded", bounded)
    task = asyncio.create_task(LocalPasswordProvider(users, LocalPasswordHasher()).authenticate(
        record.account.login_name, PASSWORD,
    ))
    await asyncio.wait_for(ready.wait(), 5)
    try:
        if mutation == "change":
            await users.change_local_password(
                account_id=record.account.account_id, identity_id=record.account.identity_id,
                password_hash="concurrent-new", actor=record.account.login_name, ip="127.0.0.1",
                expected_security_version=record.account.security_version,
                expected_credential_version=record.credential_version,
            )
        else:
            sql = {
                "reset": """UPDATE local_credential SET password_hash='concurrent-reset',
                    credential_version=credential_version+1,must_change_password=TRUE,
                    temporary_password_expires_at=now()+interval '24 hours'
                    WHERE identity_id=:iid""",
                "account": "UPDATE user_account SET status=0 WHERE id=:aid",
                "identity": "UPDATE auth_identity SET status=0 WHERE id=:iid",
                "version": """UPDATE user_account SET security_version=security_version+1
                    WHERE id=:aid""",
                "hash_only": """UPDATE local_credential SET password_hash='concurrent'
                    WHERE identity_id=:iid""",
                "expiry": """UPDATE local_credential SET must_change_password=TRUE,
                    temporary_password_expires_at=now()-interval '1 second'
                    WHERE identity_id=:iid""",
                "provider": "UPDATE auth_provider SET enabled=FALSE WHERE code='local'",
            }[mutation]
            async with engine.begin() as connection:
                await connection.execute(text(sql), {"aid": record.account.account_id,
                                                     "iid": record.account.identity_id})
        before = await snapshot(engine, record.account.identity_id)
        proceed.set()
        with pytest.raises(InvalidCredentials):
            await asyncio.wait_for(task, 5)
        assert await snapshot(engine, record.account.identity_id) == before
    finally:
        proceed.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if mutation == "provider":
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE auth_provider SET enabled=TRUE WHERE code='local'")
                )


@pytest.mark.asyncio
async def test_two_upgrade_cas_writers_have_one_winner(context):
    engine, users, record = context
    barrier = asyncio.Barrier(2)

    async def attempt():
        await barrier.wait()
        return await users.upgrade_local_password_hash(expected=record, password_hash="new-hash")

    assert sorted(await asyncio.gather(attempt(), attempt())) == [False, True]
    async with engine.connect() as connection:
        assert await connection.scalar(text("""
            SELECT count(*) FROM audit_log WHERE action='local_password_rehash'
              AND actor_account_id=:id
        """), {"id": record.account.account_id}) == 1


@pytest.mark.asyncio
async def test_audit_database_failure_rolls_back_hash(context, monkeypatch):
    engine, users, record = context
    before = await snapshot(engine, record.account.identity_id)

    async def reject_audit(connection, **kwargs):
        # 在真实事务中制造审计前数据库错误，证明摘要也回滚。
        await connection.execute(text("SELECT 1/0"))

    monkeypatch.setattr("app.core.auth.users.bind_connection_audit_subject", reject_audit)
    with pytest.raises(SessionStateUnavailable):
        await users.upgrade_local_password_hash(expected=record, password_hash="new-hash")
    assert await snapshot(engine, record.account.identity_id) == before


@pytest.mark.asyncio
async def test_expected_identity_and_versions_are_all_required(context):
    engine, users, record = context
    candidates = [
        replace(record, password_hash="wrong"), replace(record, credential_version=999),
        replace(record, account=replace(record.account, security_version=999)),
        replace(record, account=replace(record.account, identity_id=99999999)),
        replace(record, account=replace(record.account, normalized_login_name="another")),
    ]
    for expected in candidates:
        assert not await users.upgrade_local_password_hash(expected=expected, password_hash="new")
    current = await snapshot(engine, record.account.identity_id)
    assert current["password_hash"] == record.password_hash
