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


def run_migrations_offline() -> None:
    """在不建立连接时生成 SQL。"""

    context.configure(
        url=get_settings().database_owner_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """在同步桥接连接上执行迁移。

    每条 revision 单独提交。0001 会装入当前 schema.sql 里的 DEFERRABLE
    守恒触发器；若整列 upgrade 共一个事务，后续 revision 对
    send_inflight_reservation 的 INSERT/ALTER 会碰到 pending trigger events。
    """

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
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
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
