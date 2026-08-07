"""真实 PostgreSQL 审计落库的高风险操作契约测试。"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.audit import AuditEvent, insert_audit
from app.core.auth.jwt import JwtClaims
from app.core.correlation import correlation_scope
from app.services.app_repository import SqlAppRepository
from app.services.auth_provider import ProviderTestResult
from app.services.auth_provider_repository import SqlAuthProviderRepository
from app.services.blacklist import BlacklistEntry
from app.services.blacklist_repository import SqlBlacklistRepository
from app.services.crypto import CryptoService
from app.services.export_step_up import ExportStepUpService
from app.services.sensitive_repository import SqlSensitiveWordRepository
from app.services.sign_repository import SqlSignRepository
from app.services.template_repository import SqlTemplateRepository
from app.services.user_repository import SqlUserManagementRepository

pytestmark = pytest.mark.skipif(
    "SECURITY_SESSION_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


class FakeAuth:
    async def reauthenticate_current(
        self,
        claims: JwtClaims,
        password: str,
        ip: str,
    ) -> None:
        assert claims.account_id > 0
        assert password == "correct-password"
        assert ip == "127.0.0.1"


class FakeStepUpStore:
    def __init__(self) -> None:
        self.sets: list[tuple[str, str, int]] = []

    async def set(self, key: str, value: str, *, ex: int) -> None:
        self.sets.append((key, value, ex))

    async def eval(self, script: str, numkeys: int, *args: object) -> int:
        del script, numkeys, args
        return 0


@pytest.mark.asyncio
async def test_export_step_up_persists_real_audit_row() -> None:
    database_url = make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    public_id = uuid4()
    claims = JwtClaims(
        8,
        18,
        "local",
        "operator01",
        "测试操作员",
        "平台部",
        "approver",
        1,
        "jti-step-up",
        "session-step-up",
    )

    async def audit_sink(event: AuditEvent) -> None:
        async with engine.begin() as connection:
            await insert_audit(connection, event)

    service = ExportStepUpService(
        FakeAuth(),
        FakeStepUpStore(),
        audit_sink=audit_sink,
    )
    try:
        with correlation_scope(uuid4()):
            token = await service.issue(
                claims=claims,
                password="correct-password",
                ip="127.0.0.1",
                public_id=public_id,
            )
        assert token
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT action, object_type, object_id, actor_account_id, role
                        FROM audit_log
                        WHERE action='export_step_up' AND object_id=:object_id
                        """
                    ),
                    {"object_id": str(public_id)},
                )
            ).mappings().one()
        assert row["action"] == "export_step_up"
        assert row["object_type"] == "export_task"
        assert int(row["actor_account_id"]) == 8
        assert row["role"] == "approver"
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "DELETE FROM audit_log WHERE action='export_step_up' AND object_id=:object_id"
                ),
                {"object_id": str(public_id)},
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_app_create_and_key_rotation_persist_real_audit_rows() -> None:
    database_url = make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    repository = SqlAppRepository(
        cast(Any, SimpleNamespace(database_url=database_url))
    )
    app_name = f"audit-runtime-{uuid4().hex[:12]}"
    app_id: int | None = None
    try:
        with correlation_scope(uuid4()):
            app_id = await repository.create(
                name=app_name,
                dept="平台部",
                api_key_hash="a" * 64,
                api_key_prefix="aud",
                allowed_categories=["notice"],
                default_sign="【青鸾】",
                daily_quota=100,
                rate_limit_per_min=10,
                blacklist_check=True,
                freq_override=None,
                allowed_ips=[],
                callback_url=None,
                callback_secret_enc=None,
                callback_report_enabled=False,
                actor="audit-admin",
                ip="127.0.0.1",
            )
            await repository.rotate_key(
                app_id,
                api_key_hash="b" * 64,
                api_key_prefix="aud",
                old_key_expires_at=datetime.now(UTC) + timedelta(hours=1),
                actor="audit-admin",
                ip="127.0.0.1",
            )
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT action, actor, object_type, object_id
                        FROM audit_log
                        WHERE object_type='app'
                          AND object_id=CAST(:app_id AS text)
                        ORDER BY id
                        """
                    ),
                    {"app_id": app_id},
                )
            ).mappings().all()
        actions = [str(row["action"]) for row in rows]
        assert actions == ["app_create", "app_rotate_key"]
        assert all(str(row["actor"]) == "audit-admin" for row in rows)
        assert all(str(row["object_type"]) == "app" for row in rows)
    finally:
        async with engine.begin() as connection:
            if app_id is not None:
                await connection.execute(
                    text(
                        "DELETE FROM audit_log "
                        "WHERE object_type='app' AND object_id=CAST(:app_id AS text)"
                    ),
                    {"app_id": app_id},
                )
                await connection.execute(
                    text("DELETE FROM app WHERE id=:app_id"),
                    {"app_id": app_id},
                )
        await engine.dispose()


@pytest.mark.asyncio
async def test_auth_provider_lifecycle_persists_real_audit_rows() -> None:
    database_url = make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    repository = SqlAuthProviderRepository(
        cast(Any, SimpleNamespace(database_url=database_url))
    )
    code = f"audit-{uuid4().hex[:10]}"
    config: dict[str, object] = {
        "server": "ldaps://dc01.example.com:636",
        "base_dn": "DC=example,DC=com",
        "bind_dn": "CN=reader,DC=example,DC=com",
        "user_search_filter": "(uid={username})",
        "username_attribute": "uid",
        "display_name_attribute": "displayName",
        "dept_attribute": "department",
        "subject_attribute": "objectGUID",
        "group_attribute": "memberOf",
        "connect_timeout_s": 5.0,
        "receive_timeout_s": 10.0,
    }
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO auth_provider(code,name,kind,enabled,draft_config,draft_version)
                    VALUES (:code,:name,'ldap',FALSE,'{}',1)
                    """
                ),
                {"code": code, "name": "审计测试 AD"},
            )
        with correlation_scope(uuid4()):
            saved = await repository.save_draft(
                code,
                config,
                actor="audit-admin",
                ip="127.0.0.1",
            )
            await repository.record_test(
                code,
                saved.draft_version,
                ProviderTestResult(True, "OK"),
                actor="audit-admin",
                ip="127.0.0.1",
            )
            await repository.activate(code, actor="audit-admin", ip="127.0.0.1")
            await repository.disable(code, actor="audit-admin", ip="127.0.0.1")
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT action, actor, object_type, object_id
                        FROM audit_log
                        WHERE object_type='auth_provider' AND object_id=:code
                        ORDER BY id
                        """
                    ),
                    {"code": code},
                )
            ).mappings().all()
        assert [str(row["action"]) for row in rows] == [
            "auth_provider_save_draft",
            "auth_provider_test",
            "auth_provider_activate",
            "auth_provider_disable",
        ]
        assert all(str(row["actor"]) == "audit-admin" for row in rows)
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "DELETE FROM audit_log WHERE object_type='auth_provider' AND object_id=:code"
                ),
                {"code": code},
            )
            await connection.execute(
                text("DELETE FROM auth_provider WHERE code=:code"),
                {"code": code},
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_local_account_create_persists_real_audit_row() -> None:
    database_url = make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    repository = SqlUserManagementRepository(
        cast(Any, SimpleNamespace(database_url=database_url))
    )
    username = f"audit-{uuid4().hex[:12]}"
    account_id: int | None = None
    identity_id: int | None = None
    try:
        with correlation_scope(uuid4()):
            record = await repository.create_local(
                username=username,
                display_name="审计测试账号",
                dept="平台部",
                role="operator",
                password_hash="not-a-real-password-hash",
                actor="audit-admin",
                ip="127.0.0.1",
            )
        account_id = record.account_id
        identity_id = record.identity_id
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT action, actor, object_type, object_id, role
                        FROM audit_log
                        WHERE action='local_account_create'
                          AND object_type='user_account'
                          AND object_id=CAST(:account_id AS text)
                        """
                    ),
                    {"account_id": account_id},
                )
            ).mappings().one()
        assert row["action"] == "local_account_create"
        assert row["actor"] == "audit-admin"
        assert int(row["object_id"]) == account_id
    finally:
        async with engine.begin() as connection:
            if account_id is not None:
                await connection.execute(
                    text(
                        "DELETE FROM audit_log WHERE object_type='user_account' "
                        "AND object_id=CAST(:account_id AS text)"
                    ),
                    {"account_id": account_id},
                )
                if identity_id is not None:
                    await connection.execute(
                        text("DELETE FROM local_credential WHERE identity_id=:identity_id"),
                        {"identity_id": identity_id},
                    )
                    await connection.execute(
                        text("DELETE FROM auth_identity WHERE id=:identity_id"),
                        {"identity_id": identity_id},
                    )
                await connection.execute(
                    text("DELETE FROM user_account WHERE id=:account_id"),
                    {"account_id": account_id},
                )
        await engine.dispose()


@pytest.mark.asyncio
async def test_sensitive_word_add_and_delete_persist_real_audit_rows() -> None:
    database_url = make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    repository = SqlSensitiveWordRepository(
        cast(Any, SimpleNamespace(database_url=database_url))
    )
    word = f"audit-word-{uuid4().hex[:8]}"
    word_id: int | None = None
    try:
        with correlation_scope(uuid4()):
            result = await repository.add_many([word], actor="audit-admin")
        assert result.created
        word_id = result.created[0].id
        with correlation_scope(uuid4()):
            await repository.delete(word_id, actor="audit-admin")
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT action, actor, object_type
                        FROM audit_log
                        WHERE action IN ('sensitive_word_add','sensitive_word_delete')
                          AND actor='audit-admin'
                        ORDER BY id
                        """
                    )
                )
            ).mappings().all()
        assert [str(row["action"]) for row in rows] == [
            "sensitive_word_add",
            "sensitive_word_delete",
        ]
        assert all(str(row["actor"]) == "audit-admin" for row in rows)
    finally:
        async with engine.begin() as connection:
            if word_id is not None:
                await connection.execute(
                    text("DELETE FROM sensitive_word WHERE id=:word_id"),
                    {"word_id": word_id},
                )
            await connection.execute(
                text(
                    "DELETE FROM audit_log WHERE action LIKE 'sensitive_word_%' "
                    "AND actor='audit-admin'"
                )
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_blacklist_upsert_and_delete_persist_real_audit_rows() -> None:
    database_url = make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    repository = SqlBlacklistRepository(
        cast(Any, SimpleNamespace(database_url=database_url))
    )
    crypto = CryptoService.from_secret_values(
        "a" * 32,
        "b" * 32,
    )
    phone = f"139{str(uuid4().int)[:8]}"
    protected = crypto.protect_phone(phone)
    entry = BlacklistEntry(
        phone_hmac=protected.phone_hmac,
        phone_enc=protected.phone_enc,
        phone_mask=protected.phone_mask,
        key_version=protected.key_version,
        source="integration",
        remark="audit-runtime",
        created_at=datetime.now(UTC),
    )
    try:
        with correlation_scope(uuid4()):
            await repository.upsert_many([entry], actor="audit-admin", source="integration")
            await repository.delete(entry.phone_hmac, actor="audit-admin")
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT action, actor, object_type
                        FROM audit_log
                        WHERE action IN ('blacklist_add','blacklist_delete')
                          AND actor='audit-admin'
                        ORDER BY id
                        """
                    )
                )
            ).mappings().all()
        assert [str(row["action"]) for row in rows] == [
            "blacklist_add",
            "blacklist_delete",
        ]
        assert all(str(row["actor"]) == "audit-admin" for row in rows)
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM blacklist WHERE phone_hmac=:phone_hmac"),
                {"phone_hmac": entry.phone_hmac},
            )
            await connection.execute(
                text(
                    "DELETE FROM audit_log WHERE action LIKE 'blacklist_%' "
                    "AND actor='audit-admin'"
                )
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_template_and_sign_create_delete_persist_real_audit_rows() -> None:
    database_url = make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    settings = cast(Any, SimpleNamespace(database_url=database_url))
    template_repository = SqlTemplateRepository(settings)
    sign_repository = SqlSignRepository(settings)
    template_id: int | None = None
    sign_id: int | None = None
    template_name = f"audit-template-{uuid4().hex[:8]}"
    sign_name = f"【审计签名{uuid4().hex[:6]}】"
    try:
        with correlation_scope(uuid4()):
            template = await template_repository.create(
                name=template_name,
                content="验证码{1}",
                var_specs=[{"index": 1, "max_len": 6}],
                dept="平台部",
                vendor_template_id=0,
                actor="audit-admin",
            )
            template_id = template.id
            sign = await sign_repository.create(
                name=sign_name,
                vendor_sign_id="0",
                actor="audit-admin",
            )
            sign_id = sign.id
            await template_repository.delete(template_id, actor="audit-admin")
            await sign_repository.delete(sign_id, actor="audit-admin")
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT action, actor, object_type
                        FROM audit_log
                        WHERE action IN (
                          'template_create','template_delete','sign_create','sign_delete'
                        ) AND actor='audit-admin'
                        ORDER BY id
                        """
                    )
                )
            ).mappings().all()
        assert [str(row["action"]) for row in rows] == [
            "template_create",
            "template_delete",
            "sign_create",
            "sign_delete",
        ]
    finally:
        async with engine.begin() as connection:
            if template_id is not None:
                await connection.execute(
                    text("DELETE FROM sms_template WHERE id=:template_id"),
                    {"template_id": template_id},
                )
            if sign_id is not None:
                await connection.execute(
                    text("DELETE FROM sms_sign WHERE id=:sign_id"),
                    {"sign_id": sign_id},
                )
            await connection.execute(
                text(
                    "DELETE FROM audit_log WHERE action LIKE 'template_%' "
                    "OR action LIKE 'sign_%' AND actor='audit-admin'"
                )
            )
        await engine.dispose()
