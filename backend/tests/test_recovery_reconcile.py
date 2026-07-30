from __future__ import annotations

import pytest

from app.services.reconcile import RecoveryReconciler, RecoveryWork


class FakeRepository:
    async def stalled(self) -> list[RecoveryWork]:
        return [
            RecoveryWork("batch", "batch-1", None, "notice"),
            RecoveryWork("chunk", "batch-2", 9, "market"),
        ]


class FakePublisher:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def enqueue(self, batch_no: str, queue: str) -> None:
        self.calls.append(("batch", batch_no, queue))

    async def enqueue_chunk(self, chunk_id: int, queue: str) -> None:
        self.calls.append(("chunk", chunk_id, queue))


@pytest.mark.asyncio
async def test_recovery_requeues_only_repository_selected_fact_source_work() -> None:
    publisher = FakePublisher()
    assert await RecoveryReconciler(FakeRepository(), publisher).run_once() == 2
    assert publisher.calls == [
        ("batch", "batch-1", "realtime"),
        ("chunk", 9, "bulk"),
    ]
