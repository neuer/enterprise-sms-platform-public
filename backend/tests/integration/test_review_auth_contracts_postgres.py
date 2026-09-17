"""后续账号合同的实际数据库回归；仅使用隔离数据库和合成主体。"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.auth.admin_authorization import AdminAuthorization
from app.core.auth.principal_context import audit_principal_scope
from app.core.correlation import correlation_scope
from app.services.admin_step_up import AdminIntent
from app.services.user_repository import SqlUserManagementRepository
from tests.integration.test_ops_audit_postgres import _create_admin
from tests.integration.test_ops_audit_postgres import accept_runtime as accept_runtime

pytestmark = pytest.mark.skipif(
    "SECURITY_SESSION_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


@pytest.mark.asyncio
async def test_restored_mapping_response_and_audit_match_committed_department(
    accept_runtime: tuple[AsyncEngine, URL],
) -> None:
    owner, _ = accept_runtime
    principal = await _create_admin(owner, login="review-" + uuid4().hex[:12])
    account = provider = 0
    try:
        async with owner.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO "
                    "local_credential(identity_id,password_hash,must_change_password) "
                    "VALUES(:id,'synthetic-hash',false)"
                ),
                {"id": principal.identity_id},
            )
            provider = int(
                await connection.scalar(
                    text(
                        "INSERT INTO auth_provider(code,name,kind,enabled,active_version) "
                        "VALUES(:code,'synthetic','ldap',true,1) RETURNING id"
                    ),
                    {"code": uuid4().hex},
                )
            )
            account = int(
                await connection.scalar(
                    text(
                        "INSERT INTO user_account(display_name,dept,role,role_override) "
                        "VALUES('synthetic','old-department','viewer',true) RETURNING id"
                    )
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO auth_identity(account_id,provider_id,login_name,"
                    "normalized_login_name,external_subject,source_groups) "
                    "VALUES(:account,:provider,:login,:login,:login,ARRAY['synthetic-group'])"
                ),
                {"account": account, "provider": provider, "login": uuid4().hex},
            )
            await connection.execute(
                text(
                    "INSERT INTO external_role_mapping(provider_id,external_group,role,dept) "
                    "VALUES(:provider,'synthetic-group','operator','new-department')"
                ),
                {"provider": provider},
            )
            before = int(
                await connection.scalar(
                    text("SELECT security_version FROM user_account WHERE id=:id"), {"id": account}
                )
            )
        repository = SqlUserManagementRepository(
            cast(
                Any,
                SimpleNamespace(
                    database_url_for=lambda _: owner.url,
                ),
            )
        )
        with audit_principal_scope(principal), correlation_scope(uuid4()):
            changed = await repository.set_role(
                account,
                "viewer",
                False,
                authorization=AdminAuthorization(
                    principal.account_id,
                    principal.identity_id,
                    1,
                    "local",
                    AdminIntent("user_role_change", str(account), "synthetic"),
                ),
                actor=principal.login_name,
                ip="127.0.0.1",
            )
        async with owner.connect() as connection:
            actual = (
                (
                    await connection.execute(
                        text(
                            "SELECT dept,role,role_override,security_version "
                            "FROM user_account WHERE id=:id"
                        ),
                        {"id": account},
                    )
                )
                .mappings()
                .one()
            )
            audit = (
                (
                    await connection.execute(
                        text(
                            "SELECT before_val,after_val FROM audit_log "
                            "WHERE action='role_override' "
                            "AND object_id=:id AND actor_account_id=:actor"
                        ),
                        {"id": str(account), "actor": principal.account_id},
                    )
                )
                .mappings()
                .one()
            )
        assert changed.dept == actual["dept"] == "new-department"
        assert changed.role == actual["role"] == "operator"
        assert changed.role_override == actual["role_override"] is False
        assert changed.security_version == actual["security_version"] == before + 1
        assert audit["before_val"]["dept"] == "old-department"
        assert audit["after_val"] == dict(actual)
    finally:
        async with owner.begin() as connection:
            await connection.execute(
                text("DELETE FROM local_credential WHERE identity_id=:id"),
                {"id": principal.identity_id},
            )
            await connection.execute(
                text("DELETE FROM audit_log WHERE actor_account_id=:id"),
                {"id": principal.account_id},
            )
            await connection.execute(
                text("DELETE FROM external_role_mapping WHERE provider_id=:id"), {"id": provider}
            )
            await connection.execute(
                text("DELETE FROM auth_identity WHERE account_id IN (:a,:b)"),
                {"a": account, "b": principal.account_id},
            )
            await connection.execute(
                text("DELETE FROM user_account WHERE id IN (:a,:b)"),
                {"a": account, "b": principal.account_id},
            )
            await connection.execute(
                text("DELETE FROM auth_provider WHERE id=:id"), {"id": provider}
            )
