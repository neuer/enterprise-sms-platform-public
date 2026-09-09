"""每个集成测试模块独占从迁移基线克隆的一次性数据库。"""
from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy.engine import make_url


@pytest.fixture(scope="module", autouse=True)
def isolated_module_database():
    """仅在官方一次性容器入口内克隆，避免模块之间污染队列和 Outbox。"""
    baseline = os.environ.get("SMS_ISOLATED_TEST_DATABASE")
    if not baseline:
        yield
        return
    keys = [name for name in os.environ if name.endswith("_POSTGRES_DSN")]
    original = {name: os.environ[name] for name in keys}
    owner = make_url(original["OUTBOX_POSTGRES_DSN"])
    if owner.database != baseline or owner.host != "127.0.0.1":
        raise RuntimeError("isolated test database binding mismatch")
    database = f"gate_{uuid4().hex}"

    async def database_command(statement: str) -> None:
        connection = await asyncpg.connect(
            host=owner.host, port=owner.port, user=owner.username,
            password=owner.password, database="postgres",
        )
        try:
            await connection.execute(statement)
        finally:
            await connection.close()

    # 名称来源于官方入口和 UUID；转义标识符，不拼接凭据。
    quoted_baseline = '"' + baseline.replace('"', '""') + '"'
    asyncio.run(database_command(f'CREATE DATABASE "{database}" TEMPLATE {quoted_baseline}'))
    try:
        for name, value in original.items():
            os.environ[name] = make_url(value).set(database=database).render_as_string(
                hide_password=False,
            )
        yield
    finally:
        os.environ.update(original)
        asyncio.run(database_command(f'DROP DATABASE "{database}" WITH (FORCE)'))


@pytest.fixture(autouse=True)
async def isolated_runtime_resources():
    """用例结束时关闭共享池，不能跨 pytest 事件循环复用连接。"""
    from app.core import runtime_resources as resources

    budgets = dict(resources._BUDGETS)
    component = resources._RUNTIME_COMPONENT
    try:
        yield
    finally:
        await resources.close_runtime_resources()
        resources._BUDGETS.clear()
        resources._BUDGETS.update(budgets)
        resources._RUNTIME_COMPONENT = component
        resources._DATABASE_METRICS.clear()
