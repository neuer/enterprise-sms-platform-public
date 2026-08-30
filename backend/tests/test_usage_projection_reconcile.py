from __future__ import annotations

import pytest

from app.services.usage_ledger import (
    RECONCILE_REBUILD_ACTOR,
    UsageDrift,
    UsageProjectionUnavailable,
    reconcile_usage_facts,
)


class FakeUsageLedger:
    def __init__(
        self,
        *,
        recovered: int,
        drifts: list[UsageDrift],
        rebuild_error: Exception | None = None,
    ) -> None:
        self.recovered = recovered
        self.drifts = list(drifts)
        self.rebuild_error = rebuild_error
        self.rebuild_actors: list[str] = []

    async def recover_orphans(self, *, older_than_seconds: int = 600) -> int:
        del older_than_seconds
        return self.recovered

    async def measure_drift(self) -> UsageDrift:
        return self.drifts.pop(0)

    async def rebuild(self, *, actor: str = "system:usage-projection") -> int:
        if self.rebuild_error is not None:
            raise self.rebuild_error
        self.rebuild_actors.append(actor)
        return 0


@pytest.mark.asyncio
async def test_reconcile_skips_rebuild_when_projection_matches() -> None:
    service = FakeUsageLedger(
        recovered=2,
        drifts=[UsageDrift(0, 0, 0, 0)],
    )

    assert await reconcile_usage_facts(service) == 2
    assert service.rebuild_actors == []
    assert service.drifts == []


@pytest.mark.asyncio
async def test_reconcile_rebuilds_on_drift_then_remeasures() -> None:
    service = FakeUsageLedger(
        recovered=1,
        drifts=[
            UsageDrift(4, 12, 1, 1),
            UsageDrift(0, 0, 0, 0),
        ],
    )

    assert await reconcile_usage_facts(service) == 1
    assert service.rebuild_actors == [RECONCILE_REBUILD_ACTOR]
    assert service.drifts == []


@pytest.mark.asyncio
async def test_reconcile_returns_residual_mismatches_after_rebuild() -> None:
    service = FakeUsageLedger(
        recovered=0,
        drifts=[
            UsageDrift(1, 3, 0, 0),
            UsageDrift(1, 3, 0, 0),
        ],
    )

    assert await reconcile_usage_facts(service) == 1
    assert service.rebuild_actors == [RECONCILE_REBUILD_ACTOR]


@pytest.mark.asyncio
async def test_reconcile_propagates_rebuild_failure() -> None:
    service = FakeUsageLedger(
        recovered=0,
        drifts=[UsageDrift(1, 1, 0, 0)],
        rebuild_error=UsageProjectionUnavailable("usage projection write unavailable"),
    )

    with pytest.raises(UsageProjectionUnavailable):
        await reconcile_usage_facts(service)
    assert service.rebuild_actors == []
