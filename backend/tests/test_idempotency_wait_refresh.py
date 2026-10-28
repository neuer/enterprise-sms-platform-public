from __future__ import annotations

from typing import Any

import pytest

from app.services.app_ratelimit import ControlPlaneUnavailable
from app.services.idempotency import (
    IdempotencyCoordinationTimeout,
    IdempotencyCoordinator,
    IdempotencyScope,
)
from tests.test_acceptance_controls import FakeClaimRepository, FakeRedis


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["released", "expired", "missing", "completed"])
@pytest.mark.parametrize("stale_projection", [False, True])
async def test_wait_refreshes_authority_after_owner_changes(
    transition: str, stale_projection: bool,
) -> None:
    redis, repo = FakeRedis(), FakeClaimRepository()
    scope, biz = IdempotencyScope("app", "7"), "wait-owner-change"
    coordinator = IdempotencyCoordinator(redis, repo, wait_attempts=3, wait_interval_s=0)
    token = await coordinator.claim(scope, biz)
    assert token is not None
    sleeps = 0

    async def change_owner(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        row = repo.rows[("app", "7", biz)]
        if transition == "missing":
            del repo.rows[("app", "7", biz)]
        elif transition == "expired":
            row["lease_valid"] = False
        else:
            row["state"] = transition
        if transition == "completed":
            repo.batches.add("original-batch")
        if not stale_projection:
            redis.values.pop(coordinator.claim_key(scope, biz), None)

    coordinator.sleeper = change_owner
    result = await coordinator.wait(scope, biz)
    assert result == ("original-batch" if transition == "completed" else None)
    assert sleeps == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("replace_owner", [False, True])
async def test_wait_cannot_take_over_a_live_current_or_new_generation(replace_owner: bool) -> None:
    redis, repo = FakeRedis(), FakeClaimRepository()
    scope, biz = IdempotencyScope("app", "7"), "wait-live-owner"
    coordinator = IdempotencyCoordinator(redis, repo, wait_attempts=3, wait_interval_s=0)
    token = await coordinator.claim(scope, biz)
    assert token is not None

    async def keep_live(_seconds: float) -> None:
        if replace_owner:
            row = repo.rows[("app", "7", biz)]
            row.update(token="replacement", generation=2)
        redis.values.pop(coordinator.claim_key(scope, biz), None)

    coordinator.sleeper = keep_live
    with pytest.raises(IdempotencyCoordinationTimeout):
        await coordinator.wait(scope, biz)
    assert await coordinator.claim(scope, biz) is None
    assert not repo.batches


@pytest.mark.asyncio
@pytest.mark.parametrize("dependency", ["postgres", "redis"])
async def test_wait_fails_closed_when_authority_or_projection_cannot_be_read(
    dependency: str,
) -> None:
    redis, repo = FakeRedis(), FakeClaimRepository()
    scope, biz = IdempotencyScope("app", "7"), "wait-unavailable"
    coordinator = IdempotencyCoordinator(redis, repo, wait_attempts=3, wait_interval_s=0)
    assert await coordinator.claim(scope, biz) is not None

    async def unavailable(*_args: Any) -> Any:
        raise RuntimeError("synthetic dependency unavailable")

    async def fail_after_first_poll(_seconds: float) -> None:
        if dependency == "postgres":
            repo.load_idempotency_claim = unavailable  # type: ignore[method-assign]
        else:
            redis.get = unavailable  # type: ignore[method-assign]

    coordinator.sleeper = fail_after_first_poll
    with pytest.raises(ControlPlaneUnavailable):
        await coordinator.wait(scope, biz)
    assert repo.rows[("app", "7", biz)]["state"] == "active"
    assert not repo.batches
