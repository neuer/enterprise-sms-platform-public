"""Alembic 异步迁移环境。"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.settings import get_settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def set_migration_deadlines() -> None:
    """从迁移事务首条操作起限制锁等待和单条 SQL 的执行时间。"""

    context.execute("SET LOCAL lock_timeout='5s'")
    context.execute("SET LOCAL statement_timeout='30s'")


def run_migrations_offline() -> None:
    """在不建立连接时生成 SQL。"""

    context.configure(
        url=get_settings().database_owner_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        set_migration_deadlines()
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """在同步桥接连接上执行迁移。"""

    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        set_migration_deadlines()
        context.run_migrations()


async def run_async_migrations() -> None:
    """使用 asyncpg 建立迁移连接。"""

    configuration: dict[str, Any] = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = get_settings().database_owner_url
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=None,
    )
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
