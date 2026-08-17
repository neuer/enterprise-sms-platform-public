"""PostgreSQL 事实源停滞任务恢复。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.services.category import queue_for_category


@dataclass(frozen=True, slots=True)
class RecoveryWork:
    kind: str
    batch_no: str
    chunk_id: int | None
    category: str


class RecoveryRepository(Protocol):
    async def stalled(self) -> list[RecoveryWork]: ...


class RecoveryPublisher(Protocol):
    async def enqueue(self, batch_no: str, queue: str) -> None: ...

    async def enqueue_chunk(self, chunk_id: int, queue: str) -> None: ...


class RecoveryReconciler:
    def __init__(self, repository: RecoveryRepository, publisher: RecoveryPublisher) -> None:
        self.repository = repository
        self.publisher = publisher

    async def run_once(self) -> int:
        work = await self.repository.stalled()
        for item in work:
            queue = queue_for_category(item.category)
            if item.kind == "batch":
                await self.publisher.enqueue(item.batch_no, queue)
            elif item.chunk_id is not None:
                await self.publisher.enqueue_chunk(item.chunk_id, queue)
        return len(work)
