"""PostgreSQL 事实源停滞任务恢复。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RecoveryWork:
    kind: str
    batch_no: str
    chunk_id: int | None
    category: str


class RecoveryRepository(Protocol):
    """只返回已在同一事务内持久化或复位 Outbox 事件的恢复工作。"""

    async def stalled(self) -> list[RecoveryWork]: ...


class RecoveryReconciler:
    def __init__(self, repository: RecoveryRepository) -> None:
        self.repository = repository

    async def run_once(self) -> int:
        work = await self.repository.stalled()
        return len(work)
