"""保留无关联身份 Provider 的 owner 级联删除合同；仅隔离数据库。"""
import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.skipif(
    "SECURITY_SESSION_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


@pytest.mark.asyncio
async def test_provider_cascade_remains_legal() -> None:
    engine = create_async_engine(make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"]))
    try:
        async with engine.connect() as conn:
            tx = await conn.begin()
            try:
                provider = await conn.scalar(
                    text(
                        "INSERT INTO auth_provider(code,name,kind,enabled) "
                        "VALUES(:code,'synthetic','ldap',false) RETURNING id"
                    ),
                    {"code": uuid4().hex},
                )
                await conn.execute(
                    text(
                        "INSERT INTO external_role_mapping(provider_id,external_group,role,dept) "
                        "VALUES(:id,'synthetic','viewer','synthetic')"
                    ),
                    {"id": provider},
                )
                await conn.execute(text("DELETE FROM auth_provider WHERE id=:id"), {"id": provider})
            finally:
                await tx.rollback()
    finally:
        await engine.dispose()
