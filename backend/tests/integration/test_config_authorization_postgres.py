"""真实 auth/accept 角色：配置授权锁、版本冲突及审计回滚。"""

from __future__ import annotations

import os
import secrets
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.auth.admin_authorization import AdminAuthorization, hold_admin_authorization
from app.core.errors import ApiError
from app.core.runtime_resources import close_runtime_resources
from app.services.admin import ConfigUpdate, config_write_parameters
from app.services.admin_repository import SqlAdminRepository
from app.services.admin_step_up import admin_intent
from tests.integration.test_export_download_flow import flow_owner, set_role_login  # noqa: F401
from tests.integration.test_ops_audit_postgres import _create_admin

pytestmark = pytest.mark.skipif(
    "EXPORT_AUTH_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


@pytest.fixture
async def config_context(flow_owner, monkeypatch):  # noqa: F811
    """临时登录权限、签名密钥、配置和合成主体均恢复原状。"""
    owner = flow_owner
    password, signing_key = secrets.token_hex(24), secrets.token_bytes(32)
    async with owner.connect() as c:
        roles = (
            await c.execute(
                text(
                    "SELECT rolname,rolcanlogin,rolpassword FROM pg_authid "
                    "WHERE rolname IN ('sms_auth','sms_accept')"
                )
            )
        ).all()
        assert len(roles) == 2
        prior_key = await c.scalar(
            text("SELECT key_material FROM audit_context_signing_key WHERE key_kind='principal'")
        )
        before = (
            (
                await c.execute(
                    text(
                        "SELECT value,updated_at,updated_by FROM sys_config WHERE key='vendor_qps'"
                    )
                )
            )
            .mappings()
            .one()
        )
    principal = await _create_admin(owner, login="config-" + uuid4().hex[:12])
    try:
        async with owner.begin() as c:
            for role, _, _ in roles:
                await set_role_login(c, role, password, True)
            await c.execute(
                text(
                    "INSERT INTO audit_context_signing_key(key_kind,key_material) "
                    "VALUES('principal',:key) ON CONFLICT(key_kind) "
                    "DO UPDATE SET key_material=EXCLUDED.key_material"
                ),
                {"key": signing_key},
            )
        monkeypatch.setattr("app.core.runtime_resources._audit_context_key", lambda _: signing_key)
        urls = {
            role: owner.url.set(username="sms_" + role, password=password)
            for role in ("auth", "accept")
        }
        settings = SimpleNamespace(
            database_url=urls["accept"],
            database_url_for=lambda role: urls[role],
            alert_smtp_allowed_host_set=frozenset({"smtp"}),
        )
        updates = (ConfigUpdate("vendor_qps", "8", before["updated_at"]),)
        grant = AdminAuthorization(
            principal.account_id,
            principal.identity_id,
            1,
            "local",
            admin_intent("config_update", "default", config_write_parameters(updates)),
        )
        yield owner, principal, settings, updates, grant
    finally:
        await close_runtime_resources()
        async with owner.begin() as c:
            await c.execute(
                text(
                    "UPDATE sys_config SET value=:value,updated_at=:updated_at,"
                    "updated_by=:updated_by "
                    "WHERE key='vendor_qps'"
                ),
                dict(before),
            )
            await c.execute(
                text("DELETE FROM audit_log WHERE actor_account_id=:id"),
                {"id": principal.account_id},
            )
            await c.execute(
                text("DELETE FROM auth_identity WHERE id=:id"), {"id": principal.identity_id}
            )
            await c.execute(
                text("DELETE FROM user_account WHERE id=:id"), {"id": principal.account_id}
            )
            for role, can_login, prior_password in roles:
                await set_role_login(c, role, prior_password, can_login)
            if prior_key is None:
                await c.execute(
                    text("DELETE FROM audit_context_signing_key WHERE key_kind='principal'")
                )
            else:
                await c.execute(
                    text(
                        "UPDATE audit_context_signing_key SET key_material=:key "
                        "WHERE key_kind='principal'"
                    ),
                    {"key": prior_key},
                )


async def snapshot(owner, principal):
    async with owner.connect() as c:
        config = (
            await c.execute(
                text("SELECT value,updated_at,updated_by FROM sys_config WHERE key='vendor_qps'")
            )
        ).one()
        audits = await c.scalar(
            text("SELECT count(*) FROM audit_log WHERE actor_account_id=:id"),
            {"id": principal.account_id},
        )
        return tuple(config), audits


@pytest.mark.asyncio
async def test_real_roles_commit_config_and_audit(config_context):
    owner, principal, settings, updates, grant = config_context
    await SqlAdminRepository(settings).update_configs(
        updates,
        authorization=grant,
        principal=principal,
        ip="127.0.0.1",
    )

    current, audit_count = await snapshot(owner, principal)
    assert current[0] == "8"
    assert audit_count == 1


@pytest.mark.asyncio
async def test_business_authority_function_has_narrow_runtime_permissions(flow_owner):  # noqa: F811
    """受限锁函数不授予业务角色身份表读取、写入或权限转授能力。"""
    signature = "lock_business_authorization(bigint,bigint,bigint,text,text,text,text,text)"
    roles = ("auth", "accept", "send", "callback", "export", "scheduler", "metrics")
    async with flow_owner.connect() as connection:
        for role in roles:
            allowed = await connection.scalar(
                text("SELECT has_function_privilege(:role, :signature, 'EXECUTE')"),
                {"role": "sms_" + role, "signature": signature},
            )
            assert allowed is (role in {"accept", "export"})
        for role in ("sms_accept", "sms_export"):
            for table in ("user_account", "auth_identity", "auth_provider"):
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    assert not await connection.scalar(
                        text("SELECT has_table_privilege(:role, :table, :privilege)"),
                        {"role": role, "table": table, "privilege": privilege},
                    )
        function = (await connection.execute(text("""
            SELECT prosecdef, proconfig FROM pg_proc WHERE oid=CAST(:signature AS regprocedure)
        """), {"signature": signature})).one()
        assert function.prosecdef is True
        assert function.proconfig == ["search_path=pg_catalog"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["revision", "parameters", "revocation"])
async def test_rejected_write_has_no_business_or_audit_effect(config_context, failure):
    owner, principal, settings, updates, grant = config_context
    expected_status = 401
    if failure == "revision":
        updates = (
            ConfigUpdate("vendor_qps", "8", updates[0].expected_updated_at - timedelta(seconds=1)),
        )
        grant = AdminAuthorization(
            principal.account_id,
            principal.identity_id,
            1,
            "local",
            admin_intent("config_update", "default", config_write_parameters(updates)),
        )
        expected_status = 409
    elif failure == "parameters":
        updates = (ConfigUpdate("vendor_qps", "9", updates[0].expected_updated_at),)
    else:
        async with owner.begin() as c:
            await c.execute(
                text("UPDATE user_account SET security_version=security_version+1 WHERE id=:id"),
                {"id": principal.account_id},
            )
    before = await snapshot(owner, principal)
    with pytest.raises(ApiError) as error:
        await SqlAdminRepository(settings).update_configs(
            updates,
            authorization=grant,
            principal=principal,
            ip="127.0.0.1",
        )
    assert error.value.status_code == expected_status
    assert await snapshot(owner, principal) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("entity", ["account", "identity", "provider"])
async def test_authority_share_lock_blocks_concurrent_revocation(config_context, entity):
    owner, principal, settings, updates, grant = config_context
    statements = {
        "account": (
            "UPDATE user_account SET security_version=security_version+1 WHERE id=:id",
            principal.account_id,
        ),
        "identity": ("UPDATE auth_identity SET status=0 WHERE id=:id", principal.identity_id),
        "provider": ("UPDATE auth_provider SET enabled=false WHERE code=:id", "local"),
    }
    sql, target = statements[entity]
    async with hold_admin_authorization(
        settings,
        grant,
        operation="config_update",
        target="default",
        parameters=config_write_parameters(updates),
    ):
        # 直接修改也必须被行锁阻断，不依赖撤权方先获取应用层 advisory lock。
        with pytest.raises(DBAPIError) as error:
            async with owner.begin() as c:
                await c.execute(text("SET LOCAL lock_timeout='100ms'"))
                await c.execute(text(sql), {"id": target})
        assert getattr(error.value.orig, "sqlstate", None) == "55P03"
    async with owner.begin() as c:
        await c.execute(text("SET LOCAL lock_timeout='500ms'"))
        await c.execute(text(sql), {"id": target})
        # 只证明锁释放后可撤权，不保留共享 Provider 的变化。
        await c.rollback()


@pytest.mark.asyncio
async def test_audit_insert_failure_rolls_back_config(config_context):
    owner, principal, settings, updates, grant = config_context
    before = await snapshot(owner, principal)
    async with owner.begin() as c:
        assert await c.scalar(text("SELECT has_table_privilege('sms_accept','audit_log','INSERT')"))
        await c.execute(text("REVOKE INSERT ON audit_log FROM sms_accept"))
    try:
        with pytest.raises(ApiError) as error:
            await SqlAdminRepository(settings).update_configs(
                updates,
                authorization=grant,
                principal=principal,
                ip="127.0.0.1",
            )
        assert error.value.status_code == 503
        assert await snapshot(owner, principal) == before
    finally:
        async with owner.begin() as c:
            await c.execute(text("GRANT INSERT ON audit_log TO sms_accept"))
