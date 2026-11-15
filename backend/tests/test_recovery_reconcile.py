from __future__ import annotations

import pytest

from app.services.reconcile import RecoveryReconciler, RecoveryWork


class FakeRepository:
    async def stalled(self) -> list[RecoveryWork]:
        return [
            RecoveryWork("batch", "batch-1", None, "notice"),
            RecoveryWork("chunk", "batch-2", 9, "market"),
        ]


@pytest.mark.asyncio
async def test_recovery_counts_only_repository_committed_work() -> None:
    assert await RecoveryReconciler(FakeRepository()).run_once() == 2


@pytest.mark.asyncio
async def test_recovery_failure_propagates_without_claiming_success() -> None:
    class FailedRepository:
        async def stalled(self) -> list[RecoveryWork]:
            raise RuntimeError("outbox transaction rolled back")

    with pytest.raises(RuntimeError, match="rolled back"):
        await RecoveryReconciler(FailedRepository()).run_once()
