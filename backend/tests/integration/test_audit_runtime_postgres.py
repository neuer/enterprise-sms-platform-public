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
from app.services.export_step_up import ExportStepUpService

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
