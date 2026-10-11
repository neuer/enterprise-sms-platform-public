"""真实 PostgreSQL 首次 seed 建立身份关联，重复执行不创建孤立账号。"""

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.cli import DEV_USERS, generate_dev_app_tokens, seed_commands
from app.services.admin_invariant import ensure_effective_admin, lock_admin_invariant

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


@pytest.mark.asyncio
async def test_first_seed_links_every_identity_and_reseed_preserves_accounts() -> None:
    engine = create_async_engine(os.environ["OUTBOX_POSTGRES_DSN"], hide_parameters=True)
    commands = seed_commands(generate_dev_app_tokens())[:2 + len(DEV_USERS)]
    expected = {user.username: user.role for user in DEV_USERS}
    previous = None
    try:
        for _ in range(2):
            async with engine.begin() as connection:
                await connection.execute(text("SET LOCAL ROLE sms_auth"))
                before = await connection.scalar(text("SELECT count(*) FROM user_account"))
                for statement, parameters in commands:
                    await connection.execute(text(statement), parameters)
                rows = (
                    await connection.execute(text(
                        "SELECT ai.normalized_login_name,ua.id,ua.role,ai.source_groups "
                        "FROM auth_identity ai JOIN user_account ua ON ua.id=ai.account_id "
                        "JOIN auth_provider ap ON ap.id=ai.provider_id "
                        "WHERE ap.code='ad' AND ai.normalized_login_name "
                        "=ANY(CAST(:names AS text[])) ORDER BY ai.normalized_login_name"
                    ), {"names": list(expected)})
                ).all()
                assert {row[0]: row[2] for row in rows} == expected
                assert all(list(row[3]) == [f"mock:{row[2]}"] for row in rows)
                after = await connection.scalar(text("SELECT count(*) FROM user_account"))
                if previous is None:
                    assert after == before + len(DEV_USERS)
                else:
                    assert after == before
                    assert rows == previous
                await lock_admin_invariant(connection)
                await ensure_effective_admin(connection)
                previous = rows
    finally:
        await engine.dispose()
