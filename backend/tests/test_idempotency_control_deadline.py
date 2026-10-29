"""控制 Redis 无响应时幂等入口有界失败，恢复后仍保留权威归属。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import app.services.idempotency as idem
from app.services.app_ratelimit import ControlPlaneUnavailable
from tests.test_acceptance_controls import FakeClaimRepository, FakeRedis


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["lookup", "inspect", "remember"])
async def test_unresponsive_control_returns_unavailable_then_recovers(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    redis, repository = FakeRedis(), FakeClaimRepository()
    coordinator = idem.IdempotencyCoordinator(redis, repository)
    scope, biz = idem.IdempotencyScope("app", "7"), "bounded-control"
    monkeypatch.setattr(idem, "IDEMPOTENCY_CONTROL_TIMEOUT_S", 0.02, raising=False)
    finished = asyncio.Event()
    method = "set" if operation == "remember" else "get"
    original = getattr(redis, method)

    async def stalled(*_args: Any, **_kwargs: Any) -> Any:
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    monkeypatch.setattr(redis, method, stalled)
    async with asyncio.timeout(0.5):
        with pytest.raises(ControlPlaneUnavailable):
            if operation == "remember":
                await coordinator.remember(scope, biz, "original")
            else:
                await getattr(coordinator, operation)(scope, biz)
    assert finished.is_set()
    assert repository.rows == {} and repository.batches == set()
    monkeypatch.setattr(redis, method, original)
    assert await coordinator.lookup(scope, biz) is None
    token = await coordinator.claim(scope, biz)
    assert token is not None
    assert await coordinator.claim(scope, biz) is None


@pytest.mark.asyncio
async def test_caller_cancellation_remains_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    redis, repository = FakeRedis(), FakeClaimRepository()
    started = asyncio.Event()

    async def stalled(_key: str) -> None:
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(redis, "get", stalled)
    coordinator = idem.IdempotencyCoordinator(redis, repository)
    task = asyncio.create_task(coordinator.lookup(idem.IdempotencyScope("app", "7"), "cancel"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not repository.rows
