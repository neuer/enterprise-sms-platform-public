"""仅由 Mypy 检查，不执行数据库或 Celery 操作。"""
from typing import assert_type

from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.runtime_resources import ManagedAsyncEngine, database_engine
from app.tasks.send_repository import SqlChunkStore
from app.tasks.send_repository_contracts import (
    SendAttemptDependencies,
    SendPayloadDependencies,
    SendTransactionEngine,
)


def require_payload(value: SendPayloadDependencies) -> None:
    pass


def require_attempt(value: SendAttemptDependencies) -> None:
    pass


def real_store_satisfies_mixin_contracts(store: SqlChunkStore) -> None:
    require_payload(store)
    require_attempt(store)
    assert_type(store._engine(), SendTransactionEngine)


async def connection_types_are_preserved(engine: ManagedAsyncEngine) -> None:
    assert_type(database_engine("postgresql+asyncpg://synthetic"), ManagedAsyncEngine)
    async with engine.connect() as connection:
        assert_type(connection, AsyncConnection)
    async with engine.begin() as connection:
        assert_type(connection, AsyncConnection)
    transaction_engine: SendTransactionEngine = engine
    await transaction_engine.dispose()
