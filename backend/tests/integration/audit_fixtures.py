"""真实审计测试使用有效主体及签名上下文，不依赖固定数据库 ID。"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.core.auth.principal_context import audit_principal_scope
from app.core.correlation import correlation_scope
from tests.integration.test_ops_audit_postgres import _create_admin


@pytest.fixture(autouse=True)
async def live_audit_principal(accept_runtime, monkeypatch, request):
    owner, _ = accept_runtime
    principal = await _create_admin(owner, login="audit-admin")
    if hasattr(request.module, "stable_admin"):
        monkeypatch.setattr(request.module, "stable_admin", lambda: principal)
    async with owner.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO "
                "local_credential(identity_id,password_hash,must_change_password) "
                "VALUES(:id,'synthetic-hash',false)"
            ),
            {"id": principal.identity_id},
        )
    try:
        with audit_principal_scope(principal), correlation_scope():
            yield principal
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
                text("DELETE FROM auth_identity WHERE id=:id"), {"id": principal.identity_id}
            )
            await connection.execute(
                text("DELETE FROM user_account WHERE id=:id"), {"id": principal.account_id}
            )
