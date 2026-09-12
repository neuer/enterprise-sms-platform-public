"""映射差量更新和数据库事务去重；仅使用隔离合成 PostgreSQL。"""

from __future__ import annotations

import asyncio
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
from app.services.auth_provider import (
    ExternalRoleMapping,
    StaleProviderDraft,
    role_mapping_revision,
)
from app.services.auth_provider_repository import SqlAuthProviderRepository
from tests.integration.test_ops_audit_postgres import _create_admin
from tests.integration.test_ops_audit_postgres import accept_runtime as accept_runtime

pytestmark = pytest.mark.skipif(
    "SECURITY_SESSION_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


@pytest.mark.asyncio
async def test_thousand_accounts_and_hundred_mappings_invalidate_once_per_transaction(
    accept_runtime: tuple[AsyncEngine, URL],
) -> None:
    owner, _ = accept_runtime
    principal = await _create_admin(owner, login="mapping-" + uuid4().hex[:12])
    code = uuid4().hex
    provider = 0
    accounts: list[int] = []
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
                        "INSERT INTO auth_provider(code,name,kind,enabled) "
                        "VALUES(:code,'synthetic','ldap',true) RETURNING id"
                    ),
                    {"code": code},
                )
            )
            accounts = list(
                (
                    await connection.execute(
                        text(
                            "INSERT INTO user_account(display_name,role,role_override) "
                            "SELECT 'synthetic','viewer',false FROM generate_series(1,1000) "
                            "RETURNING id"
                        )
                    )
                ).scalars()
            )
            await connection.execute(
                text(
                    "INSERT INTO auth_identity(account_id,provider_id,login_name,"
                    "normalized_login_name,external_subject,source_groups) "
                    "SELECT id,:provider,'mapping-'||id,'mapping-'||id,'mapping-'||id,"
                    "ARRAY['group-0'] FROM unnest(CAST(:accounts AS bigint[])) AS id"
                ),
                {"provider": provider, "accounts": accounts},
            )
        repository = SqlAuthProviderRepository(
            cast(Any, SimpleNamespace(database_url_for=lambda _: owner.url))
        )

        async def versions() -> dict[int, int]:
            async with owner.connect() as connection:
                return dict(
                    (
                        await connection.execute(
                            text("SELECT id,security_version FROM user_account WHERE id=ANY(:ids)"),
                            {"ids": accounts},
                        )
                    ).all()
                )

        expected: tuple[ExternalRoleMapping, ...] = ()

        async def save(items: tuple[ExternalRoleMapping, ...]) -> None:
            nonlocal expected
            with audit_principal_scope(principal), correlation_scope(uuid4()):
                await repository.replace_role_mappings(
                    code,
                    items,
                    expected_revision=role_mapping_revision(expected),
                    authorization=AdminAuthorization(
                        principal.account_id,
                        principal.identity_id,
                        1,
                        "local",
                        AdminIntent("provider_role_mapping_change", str(code), "synthetic"),
                    ),
                    actor=principal.login_name,
                    ip="127.0.0.1",
                )

            expected = items

        baseline = await versions()
        original = tuple(
            ExternalRoleMapping(f"group-{i}", "viewer", "synthetic") for i in range(100)
        )
        await save(original)
        inserted = await versions()
        assert all(inserted[key] == value + 1 for key, value in baseline.items())
        # Reordering an unchanged set must not update mapping rows or invalidate a session.
        async with owner.connect() as connection:
            row_versions = list(
                (
                    await connection.execute(
                        text(
                            "SELECT id,xmin::text FROM external_role_mapping "
                            "WHERE provider_id=:id ORDER BY id"
                        ),
                        {"id": provider},
                    )
                ).all()
            )
        await save(tuple(reversed(original)))
        assert await versions() == inserted
        async with owner.connect() as connection:
            assert (
                list(
                    (
                        await connection.execute(
                            text(
                                "SELECT id,xmin::text FROM external_role_mapping "
                                "WHERE provider_id=:id ORDER BY id"
                            ),
                            {"id": provider},
                        )
                    ).all()
                )
                == row_versions
            )
        changed = tuple(
            ExternalRoleMapping(f"group-{i}", "operator", "other") for i in range(50, 150)
        )
        await save(changed)
        with (
            pytest.raises(StaleProviderDraft),
            audit_principal_scope(principal),
            correlation_scope(uuid4()),
        ):
            await repository.replace_role_mappings(
                code,
                original,
                expected_revision=role_mapping_revision(original),
                authorization=AdminAuthorization(
                    principal.account_id,
                    principal.identity_id,
                    1,
                    "local",
                    AdminIntent("provider_role_mapping_change", str(code), "synthetic"),
                ),
                actor=principal.login_name,
                ip="127.0.0.1",
            )
        replaced = await versions()
        assert all(replaced[key] == value + 1 for key, value in inserted.items())

        async def competing(department: str) -> str:
            items = tuple(
                ExternalRoleMapping(item.external_group, item.role, department) for item in changed
            )
            try:
                with audit_principal_scope(principal), correlation_scope(uuid4()):
                    await repository.replace_role_mappings(
                        code,
                        items,
                        expected_revision=role_mapping_revision(changed),
                        authorization=AdminAuthorization(
                            principal.account_id,
                            principal.identity_id,
                            1,
                            "local",
                            AdminIntent("provider_role_mapping_change", str(code), "synthetic"),
                        ),
                        actor=principal.login_name,
                        ip="127.0.0.1",
                    )
                return "saved"
            except StaleProviderDraft:
                return "conflict"

        outcomes = await asyncio.wait_for(asyncio.gather(competing("left"), competing("right")), 5)
        assert sorted(outcomes) == ["conflict", "saved"]
        raced = await versions()
        assert all(raced[key] == value + 1 for key, value in replaced.items())
        replaced = raced
        # Direct runtime DML remains guarded, including a user-supplied GUC attempt.
        async with owner.begin() as connection:
            await connection.execute(text("SET LOCAL ROLE sms_auth"))
            await connection.execute(text("SET LOCAL app.skip_security_invalidation='true'"))
            await connection.execute(
                text("UPDATE external_role_mapping SET dept='direct' WHERE provider_id=:id"),
                {"id": provider},
            )
            await connection.execute(
                text("UPDATE external_role_mapping SET role='viewer' WHERE provider_id=:id"),
                {"id": provider},
            )
        direct = await versions()
        assert all(direct[key] == value + 1 for key, value in replaced.items())
        # A failed transaction rolls back both the mapping and its dedup fact.
        async with owner.connect() as connection:
            transaction = await connection.begin()
            await connection.execute(
                text("UPDATE external_role_mapping SET dept='rollback' WHERE provider_id=:id"),
                {"id": provider},
            )
            await transaction.rollback()
        assert await versions() == direct
        async with owner.begin() as connection:
            await connection.execute(
                text("UPDATE external_role_mapping SET dept='next' WHERE provider_id=:id"),
                {"id": provider},
            )
        final = await versions()
        assert all(final[key] == value + 1 for key, value in direct.items())
        async with owner.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM role_mapping_invalidation WHERE provider_id=:id"),
                    {"id": provider},
                )
                == 1
            )
            for role in [
                "sms_auth",
                "sms_accept",
                "sms_send",
                "sms_callback",
                "sms_export",
                "sms_scheduler",
                "sms_metrics",
            ]:
                assert not await connection.scalar(
                    text(
                        "SELECT has_table_privilege(:role,'role_mapping_invalidation',"
                        "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE')"
                    ),
                    {"role": role},
                )
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
            ids = [*accounts, principal.account_id]
            await connection.execute(
                text("DELETE FROM auth_identity WHERE account_id=ANY(:ids)"), {"ids": ids}
            )
            await connection.execute(
                text("DELETE FROM user_account WHERE id=ANY(:ids)"), {"ids": ids}
            )
            await connection.execute(
                text("DELETE FROM auth_provider WHERE id=:id"), {"id": provider}
            )
