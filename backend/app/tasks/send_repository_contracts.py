"""发送仓储 Mixin 的静态能力合同，不提供任何覆盖真实 MRO 的运行桩。"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncConnection

from app.services.crypto import CryptoService
from app.settings import Settings
from app.tasks.send import ChunkPayload
from app.vendor.routing import VendorRecord


class SendTransactionEngine(Protocol):
    """发送事务只借用已测量连接上下文，不持有共享池生命周期。"""

    def connect(self) -> AbstractAsyncContextManager[AsyncConnection]: ...

    def begin(self) -> AbstractAsyncContextManager[AsyncConnection]: ...


class SendPayloadDependencies(Protocol):
    """载荷装配所需的加密、配置、事务和持久队列能力。"""

    crypto: CryptoService
    settings: Settings

    def _engine(self) -> SendTransactionEngine: ...

    @staticmethod
    def lane_for(category: str) -> str: ...

    @staticmethod
    async def _enqueue_chunk_ready(
        connection: AsyncConnection, chunk_ids: list[int], lane: str,
    ) -> None: ...


class SendAttemptDependencies(SendPayloadDependencies, Protocol):
    """厂商尝试增加的路由、载荷、预算和失败持久化能力。"""

    registry: tuple[VendorRecord, ...]
    adapter_ids: frozenset[str]

    @staticmethod
    async def _enqueue_retry(chunk_id: int, lane: str, countdown: int) -> None: ...

    async def _payload(self, connection: AsyncConnection, chunk_id: int) -> ChunkPayload: ...

    async def _finalize_failed_messages(
        self, connection: AsyncConnection, chunk_id: int, batch_id: int,
    ) -> None: ...
