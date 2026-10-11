"""故意错误的静态输入；门禁必须拒绝，禁止作为运行时替身导入。"""
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.runtime_resources import ManagedAsyncEngine
from app.tasks.send_repository_contracts import SendAttemptDependencies, SendPayloadDependencies


def require_attempt(value: SendAttemptDependencies) -> None:
    pass


class MissingDependencies:
    pass


def missing_capabilities(value: MissingDependencies) -> None:
    require_attempt(value)


class WrongPayload(SendAttemptDependencies):
    async def _payload(self, connection: AsyncConnection, chunk_id: int) -> str:
        return "incorrect payload return type"


class WrongQueue(SendPayloadDependencies):
    @staticmethod
    async def _enqueue_chunk_ready(
        connection: AsyncConnection, chunk_ids: list[int], lane: str,
    ) -> str:
        return "incorrect queue return type"


async def engine_cannot_hide_wrong_connection_types(engine: ManagedAsyncEngine) -> None:
    value: int = await engine.connect().__aenter__()
    del value
    engine.nonexistent_engine_capability()
