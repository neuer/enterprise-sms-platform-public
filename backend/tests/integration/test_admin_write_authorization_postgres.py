"""隔离 PostgreSQL：权限撤销与管理写入的提交顺序，以及临时管理员交接。"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.auth.accounts import SecurityPrincipal
from app.core.auth.admin_authorization import AdminAuthorization
from app.core.auth.principal_context import audit_principal_scope
from app.core.correlation import correlation_scope
from app.core.errors import ApiError
from app.services.admin_invariant import ensure_effective_admin, lock_admin_invariant
from app.services.admin_step_up import AdminIntent
from app.services.auth_provider import role_mapping_revision
from app.services.auth_provider_repository import SqlAuthProviderRepository
from app.services.user_management import LastAdminProtected
from app.services.user_repository import SqlUserManagementRepository

pytestmark = pytest.mark.skipif(
    "SECURITY_SESSION_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


@pytest.fixture
async def context():
    engine = create_async_engine(make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"]))
    suffix = uuid4().hex
    accounts = []
    identities = []
    async with engine.begin() as c:
        provider = await c.scalar(text("SELECT id FROM auth_provider WHERE code='local'"))
        for index in range(2):
            account = await c.scalar(
                text(
                    "INSERT INTO user_account(role,role_override) VALUES('admin',true) RETURNING id"
                )
            )
            identity = await c.scalar(
                text(
                    "INSERT INTO "
                    "auth_identity(account_id,provider_id,login_name,normalized_login_name,"
                    "external_subject) VALUES(:a,:p,:n,:n,:n) RETURNING id"
                ),
                {"a": account, "p": provider, "n": suffix + str(index)},
            )
            await c.execute(
                text(
                    "INSERT INTO "
                    "local_credential(identity_id,password_hash,must_change_password) "
                    "VALUES(:i,'synthetic-hash',false)"
                ),
                {"i": identity},
            )
            accounts.append(int(account))
            identities.append(int(identity))
        await c.execute(
            text(
                "INSERT INTO "
                "auth_provider(code,name,kind,enabled,draft_config,draft_version,tested_version) "
                "VALUES(:code,'synthetic','ldap',false,'{}',1,1)"
            ),
            {"code": suffix},
        )
    principal = SecurityPrincipal(accounts[0], identities[0], suffix + "0", "", "admin")
    settings = cast(Any, SimpleNamespace(database_url_for=lambda _: engine.url))
    try:
        yield engine, principal, accounts[1], suffix, settings
    finally:
        async with engine.begin() as c:
            # This fixture owns only synthetic rows in the isolated test database.
            await c.execute(
                text("DELETE FROM audit_log WHERE actor_account_id=:a"), {"a": principal.account_id}
            )
            await c.execute(
                text(
                    "DELETE FROM external_role_mapping WHERE provider_id=(SELECT id FROM "
                    "auth_provider WHERE code=:code)"
                ),
                {"code": suffix},
            )
            await c.execute(text("DELETE FROM auth_provider WHERE code=:code"), {"code": suffix})
            await c.execute(
                text(
                    "DELETE FROM local_credential WHERE identity_id IN (SELECT id FROM "
                    "auth_identity WHERE normalized_login_name LIKE :prefix)"
                ),
                {"prefix": suffix + "%"},
            )
            await c.execute(
                text("DELETE FROM auth_identity WHERE normalized_login_name LIKE :prefix"),
                {"prefix": suffix + "%"},
            )
            await c.execute(
                text("DELETE FROM user_account WHERE id=ANY(:ids) OR display_name=:name"),
                {"ids": accounts, "name": suffix},
            )
        await engine.dispose()


OPERATIONS = ["create", "role", "status", "reset", "draft", "activate", "disable", "mappings"]


async def write(context, operation):
    _, principal, account, code, settings = context
    users = SqlUserManagementRepository(settings)
    providers = SqlAuthProviderRepository(settings)
    name = {
        "create": "user_create_admin",
        "role": "user_role_change",
        "status": "user_status_change",
        "reset": "user_password_reset",
        "draft": "provider_save_draft",
        "activate": "provider_enable_disable",
        "disable": "provider_enable_disable",
        "mappings": "provider_role_mapping_change",
    }[operation]
    target = (
        "new"
        if operation == "create"
        else str(account)
        if operation in ("role", "status", "reset")
        else code
    )
    authorization = AdminAuthorization(
        principal.account_id,
        principal.identity_id,
        1,
        "local",
        AdminIntent(name, target, "synthetic"),
    )
    kwargs = dict(authorization=authorization, actor=principal.login_name, ip="127.0.0.1")
    with audit_principal_scope(principal), correlation_scope(uuid4()):
        if operation == "create":
            return await users.create_local(
                username=code + "new",
                display_name=code,
                dept="",
                role="admin",
                password_hash="synthetic-hash",
                **kwargs,
            )
        if operation == "role":
            return await users.set_role(account, "viewer", True, **kwargs)
        if operation == "status":
            return await users.set_status(
                account, 0, actor_account_id=principal.account_id, **kwargs
            )
        if operation == "reset":
            return await users.reset_local_password(account, "synthetic-new-hash", **kwargs)
        if operation == "draft":
            return await providers.save_draft(code, {}, **kwargs)
        if operation == "activate":
            return await providers.activate(code, expected_draft_version=1, **kwargs)
        if operation == "disable":
            return await providers.disable(code, expected_draft_version=1, **kwargs)
        return await providers.replace_role_mappings(
            code, (), expected_revision=role_mapping_revision(()), **kwargs
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
async def test_revocation_commits_before_every_admin_write(context, operation):
    engine, principal, *_ = context
    async with engine.begin() as c:
        await lock_admin_invariant(c)
        await c.execute(
            text("UPDATE user_account SET security_version=security_version+1 WHERE id=:id"),
            {"id": principal.account_id},
        )
        pending = asyncio.create_task(write(context, operation))
        await asyncio.sleep(0)
    with pytest.raises(ApiError, match="管理员权限已变化"):
        await pending
    async with engine.connect() as c:
        assert (
            await c.scalar(
                text("SELECT count(*) FROM audit_log WHERE actor_account_id=:id"),
                {"id": principal.account_id},
            )
            == 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
async def test_write_lock_precedes_revocation(context, operation, monkeypatch):
    from app.core.auth.admin_authorization import lock_admin_authorization

    engine, principal, *_ = context
    acquired, release = asyncio.Event(), asyncio.Event()

    async def held(*args, **kwargs):
        await lock_admin_authorization(*args, **kwargs)
        acquired.set()
        await release.wait()

    module = (
        "user_repository"
        if operation in ("create", "role", "status", "reset")
        else "auth_provider_repository"
    )
    monkeypatch.setattr(f"app.services.{module}.lock_admin_authorization", held)
    pending = asyncio.create_task(write(context, operation))
    await asyncio.wait_for(acquired.wait(), 5)

    async def revoke():
        async with engine.begin() as c:
            await lock_admin_invariant(c)
            await c.execute(
                text("UPDATE user_account SET security_version=security_version+1 WHERE id=:id"),
                {"id": principal.account_id},
            )

    revoked = asyncio.create_task(revoke())
    await asyncio.sleep(0)
    assert not revoked.done()
    release.set()
    await pending
    await revoked


@pytest.mark.asyncio
async def test_temporary_or_missing_local_credentials_cannot_replace_durable_admin(context):
    engine, principal, target, _, settings = context
    async with engine.begin() as c:
        statuses = list(await c.execute(text("SELECT id,status FROM user_account")))
        await c.execute(
            text("UPDATE user_account SET status=0 WHERE id<>:id"), {"id": principal.account_id}
        )
    try:
        grant = AdminAuthorization(
            principal.account_id,
            principal.identity_id,
            1,
            "local",
            AdminIntent("user_password_reset", str(principal.account_id), "synthetic"),
        )
        with (
            audit_principal_scope(principal),
            correlation_scope(uuid4()),
            pytest.raises(LastAdminProtected),
        ):
            await SqlUserManagementRepository(settings).reset_local_password(
                principal.account_id,
                "new-hash",
                authorization=grant,
                actor=principal.login_name,
                ip="127.0.0.1",
            )
        async with engine.begin() as c:
            assert (
                await c.scalar(
                    text("SELECT security_version FROM user_account WHERE id=:id"),
                    {"id": principal.account_id},
                )
                == 1
            )
            await c.execute(text("UPDATE user_account SET status=1 WHERE id=:id"), {"id": target})
            await c.execute(
                text(
                    "UPDATE local_credential SET "
                    "must_change_password=true,"
                    "temporary_password_expires_at=now()+interval '1 hour' "
                    "WHERE identity_id=:id"
                ),
                {"id": principal.identity_id},
            )
            await c.execute(
                text(
                    "DELETE FROM local_credential WHERE identity_id IN (SELECT id FROM "
                    "auth_identity WHERE account_id=:id)"
                ),
                {"id": target},
            )
            with pytest.raises(LastAdminProtected):
                await ensure_effective_admin(c)
    finally:
        async with engine.begin() as c:
            for account, status in statuses:
                await c.execute(
                    text("UPDATE user_account SET status=:status WHERE id=:id"),
                    {"id": account, "status": status},
                )
