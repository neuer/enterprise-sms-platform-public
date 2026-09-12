"""隔离 PostgreSQL 中跨账号、认证源与映射的管理员保护事务竞争。"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.services.admin_invariant import ensure_effective_admin, lock_admin_invariant
from app.services.user_management import LastAdminProtected

pytestmark = pytest.mark.skipif(
    "SECURITY_SESSION_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "left_kind,right_kind",
    [
        ("account", "account"),
        ("provider", "account"),
        ("mapping", "account"),
    ],
)
async def test_competing_admin_reductions_keep_one_effective_admin(
    left_kind: str,
    right_kind: str,
) -> None:
    engine = create_async_engine(make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"]))
    accounts: list[int] = []
    providers: list[int] = []
    statuses: list[tuple[int, int]] = []
    try:
        async with engine.begin() as connection:
            statuses = [
                (int(row.id), int(row.status))
                for row in (await connection.execute(text("SELECT id,status FROM user_account")))
            ]
            await connection.execute(text("UPDATE user_account SET status=0"))
            for _ in range(2):
                code = uuid4().hex
                provider = int(
                    await connection.scalar(
                        text(
                            "INSERT INTO auth_provider(code,name,kind,enabled) "
                            "VALUES(:code,'synthetic','ldap',true) RETURNING id"
                        ),
                        {"code": code},
                    )
                )
                account = int(
                    await connection.scalar(
                        text(
                            "INSERT INTO user_account(role,role_override) "
                            "VALUES('admin',false) RETURNING id"
                        )
                    )
                )
                providers.append(provider)
                accounts.append(account)
                await connection.execute(
                    text(
                        "INSERT INTO auth_identity(account_id,provider_id,login_name,"
                        "normalized_login_name,external_subject,source_groups) "
                        "VALUES(:account,:provider,:login,:login,:login,ARRAY['group'])"
                    ),
                    {"account": account, "provider": provider, "login": code},
                )
                await connection.execute(
                    text(
                        "INSERT INTO external_role_mapping(provider_id,external_group,role,dept) "
                        "VALUES(:provider,'group','admin','synthetic')"
                    ),
                    {"provider": provider},
                )

        first_locked = asyncio.Event()
        second_started = asyncio.Event()
        release_first = asyncio.Event()

        async def reduce(index: int, kind: str) -> str:
            try:
                async with engine.begin() as connection:
                    if index:
                        second_started.set()
                    await lock_admin_invariant(connection)
                    if not index:
                        first_locked.set()
                        await release_first.wait()
                    if kind == "account":
                        await connection.execute(
                            text("UPDATE user_account SET status=0 WHERE id=:id"),
                            {"id": accounts[index]},
                        )
                    elif kind == "provider":
                        await connection.execute(
                            text("UPDATE auth_provider SET enabled=false WHERE id=:id"),
                            {"id": providers[index]},
                        )
                    else:
                        await connection.execute(
                            text("DELETE FROM external_role_mapping WHERE provider_id=:id"),
                            {"id": providers[index]},
                        )
                    await ensure_effective_admin(connection)
                return "committed"
            except LastAdminProtected:
                return "protected"

        first = asyncio.create_task(reduce(0, left_kind))
        await asyncio.wait_for(first_locked.wait(), 3)
        second = asyncio.create_task(reduce(1, right_kind))
        await asyncio.wait_for(second_started.wait(), 3)
        release_first.set()
        assert await asyncio.wait_for(asyncio.gather(first, second), 5) == [
            "committed",
            "protected",
        ]
        async with engine.begin() as connection:
            await ensure_effective_admin(connection)
            assert (
                await connection.scalar(
                    text("SELECT status FROM user_account WHERE id=:id"), {"id": accounts[1]}
                )
                == 1
            )
            assert (
                await connection.scalar(
                    text("SELECT enabled FROM auth_provider WHERE id=:id"), {"id": providers[1]}
                )
                is True
            )
    finally:
        async with engine.begin() as connection:
            for account in accounts:
                await connection.execute(
                    text("DELETE FROM auth_identity WHERE account_id=:id"), {"id": account}
                )
                await connection.execute(
                    text("DELETE FROM user_account WHERE id=:id"), {"id": account}
                )
            for provider in providers:
                await connection.execute(
                    text("DELETE FROM external_role_mapping WHERE provider_id=:id"),
                    {"id": provider},
                )
                await connection.execute(
                    text("DELETE FROM auth_provider WHERE id=:id"), {"id": provider}
                )
            for account, status in statuses:
                await connection.execute(
                    text("UPDATE user_account SET status=:status WHERE id=:id"),
                    {"id": account, "status": status},
                )
        await engine.dispose()
