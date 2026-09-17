"""真实 session_user 读取策略与权限检查，事务回滚保留隔离夹具原状态。"""

from __future__ import annotations

import os
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.auth.admission_policy import AdmissionPolicyRuntime
from tests.integration.test_auth_r2_postgres import auth_roles as auth_roles  # noqa: F401

pytestmark = pytest.mark.skipif(
    "SECURITY_SESSION_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


async def test_auth_admission_runtime_roles_and_monotonic_revision(
    auth_roles: tuple[Any, ...],
) -> None:
    _owner, auth_url, accept_url, settings = auth_roles
    settings.auth_source_profile_file = None
    runtime = AdmissionPolicyRuntime(settings, object())
    policy = await runtime._postgres()
    assert policy.revision > 0 and policy.limits.global_burst == 8
    auth = create_async_engine(auth_url)
    accept = create_async_engine(accept_url)
    try:
        async with auth.connect() as connection:
            assert await connection.scalar(text("SELECT session_user")) == "sms_auth"
            assert await connection.scalar(
                text("SELECT value FROM sys_config WHERE key='auth_admission_policy'")
            )
            with pytest.raises(DBAPIError):
                await connection.execute(
                    text("UPDATE sys_config SET value=value WHERE key='auth_admission_policy'")
                )
            await connection.rollback()
        async with accept.connect() as connection:
            assert await connection.scalar(text("SELECT session_user")) == "sms_accept"
            revisions = [policy.revision]
            for _ in range(2):
                revision = await connection.scalar(
                    text(
                        "UPDATE sys_config SET value=value,updated_at='2000-01-01T00:00:00Z' "
                        "WHERE key='auth_admission_policy' "
                        "RETURNING (EXTRACT(EPOCH FROM updated_at)*1000000)::bigint"
                    )
                )
                revisions.append(int(revision))
            assert revisions[0] < revisions[1] < revisions[2]
            await connection.rollback()
    finally:
        await auth.dispose()
        await accept.dispose()
