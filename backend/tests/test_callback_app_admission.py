"""回调故障的应用边界、固定内存和全局保护合同。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest

from app.services.send_admission import (
    AppCallbackFacts,
    SendAdmissionGuard,
    SendAdmissionLimits,
    SendAdmissionRejected,
    SendAdmissionUnavailable,
    evaluate_capacity,
)
from app.services.send_admission_repository import SqlSendAdmissionRepository
from tests.test_send_admission import FakeConnection, FakeEngine, FakeFacts, facts


class AppFactsRepository(FakeFacts):
    def __init__(self) -> None:
        super().__init__(facts(outbox_shared_active=0))
        self.callbacks: dict[int, AppCallbackFacts] = {}
        self.app_loads = 0
        self.app_error: Exception | None = None
        self.release: asyncio.Event | None = None

    async def load_app_callbacks(self, app_id: int) -> AppCallbackFacts:
        self.app_loads += 1
        if self.release is not None:
            await self.release.wait()
        if self.app_error is not None:
            raise self.app_error
        return self.callbacks.get(app_id, AppCallbackFacts(0, 0))


async def authorize(guard: SendAdmissionGuard, app_id: int, category: str = "notice") -> None:
    await guard.authorize(app_id=app_id, category=category, channel="api", recipient_count=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("callback", [AppCallbackFacts(200, 0), AppCallbackFacts(0, 20)])
@pytest.mark.parametrize("category", ["notice", "verify", "market"])
async def test_callback_failure_isolated_to_its_application(
    callback: AppCallbackFacts, category: str
) -> None:
    repository = AppFactsRepository()
    repository.callbacks[1] = callback
    guard = SendAdmissionGuard(repository)
    with pytest.raises(SendAdmissionRejected) as rejected:
        await authorize(guard, 1, category)
    assert (rejected.value.reason, rejected.value.retry_after_s) == ("app_callback_backlog", 5)
    await authorize(guard, 2, category)
    assert repository.app_loads == 2
    assert repository.payload == facts(outbox_shared_active=0)


@pytest.mark.asyncio
async def test_application_boundary_and_fresh_recovery_do_not_affect_global_epoch() -> None:
    repository = AppFactsRepository()
    repository.callbacks[1] = AppCallbackFacts(199, 19)
    now = [0.0]
    guard = SendAdmissionGuard(repository, callback_clock=lambda: now[0])
    await authorize(guard, 1)
    original = await guard.snapshot()
    repository.callbacks[1] = AppCallbackFacts(200, 20)
    await authorize(guard, 1)  # 合同允许最长五秒观察窗口。
    now[0] = 5.0
    with pytest.raises(SendAdmissionRejected, match="发送通道"):
        await authorize(guard, 1)
    repository.callbacks[1] = AppCallbackFacts(199, 19)
    now[0] = 10.0
    await authorize(guard, 1)
    assert await guard.snapshot() is original
    assert repository.app_loads == 3


@pytest.mark.asyncio
async def test_expired_allow_fails_closed_while_other_app_cache_remains_valid() -> None:
    repository = AppFactsRepository()
    now = [0.0]
    guard = SendAdmissionGuard(repository, callback_clock=lambda: now[0])
    await authorize(guard, 1)
    now[0] = 3.0
    await authorize(guard, 2)
    now[0] = 5.0
    repository.app_error = TimeoutError("synthetic database deadline")
    with pytest.raises(SendAdmissionUnavailable):
        await authorize(guard, 1)
    assert 1 not in guard._callback_cache
    await authorize(guard, 2)


@pytest.mark.asyncio
async def test_callback_single_flight_and_cache_cardinality_are_bounded() -> None:
    repository = AppFactsRepository()
    repository.release = asyncio.Event()
    guard = SendAdmissionGuard(repository)
    requests = [asyncio.create_task(authorize(guard, 1)) for _ in range(20)]
    await asyncio.sleep(0)
    repository.release.set()
    await asyncio.gather(*requests)
    assert repository.app_loads == 1
    for app_id in range(2, 280):
        await authorize(guard, app_id)
    assert len(guard._callback_cache) == 256
    assert 1 not in guard._callback_cache
    assert len({id(guard._callback_lock) for _ in range(280)}) == 1


@pytest.mark.asyncio
async def test_callback_query_and_wait_timeout_do_not_leave_cache() -> None:
    repository = AppFactsRepository()
    repository.release = asyncio.Event()
    guard = SendAdmissionGuard(repository, load_timeout_s=0.02)
    results = await asyncio.gather(authorize(guard, 1), authorize(guard, 2), return_exceptions=True)
    assert all(isinstance(result, SendAdmissionUnavailable) for result in results)
    assert not guard._callback_cache
    assert not guard._callback_lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("app_id", [0, -1, True, "1", None])
async def test_api_rejects_invalid_application_context(app_id: Any) -> None:
    repository = AppFactsRepository()
    with pytest.raises(SendAdmissionUnavailable):
        await authorize(SendAdmissionGuard(repository), app_id)
    assert repository.loads == repository.app_loads == 0


@pytest.mark.asyncio
async def test_server_web_zero_app_is_global_only_but_uat_web_app_is_checked() -> None:
    repository = AppFactsRepository()
    repository.callbacks[1] = AppCallbackFacts(200, 0)
    guard = SendAdmissionGuard(repository)
    await guard.authorize(app_id=0, category="notice", channel="web", recipient_count=1)
    assert repository.app_loads == 0
    with pytest.raises(SendAdmissionRejected) as rejected:
        await guard.authorize(app_id=1, category="notice", channel="web", recipient_count=1)
    assert rejected.value.reason == "app_callback_backlog"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"outbox_active": 2000},
        {"callback_dead": 200},
        {"outbox_oldest_age_s": 600},
        {"dispatcher_heartbeat_stale": True},
        {"realtime_paused": True, "bulk_paused": True},
    ],
)
async def test_global_hard_and_infrastructure_protection_precede_application_gate(
    overrides: dict[str, Any],
) -> None:
    repository = AppFactsRepository()
    repository.payload = facts(outbox_shared_active=0, **overrides)
    with pytest.raises(SendAdmissionRejected):
        await authorize(SendAdmissionGuard(repository), 2)
    assert repository.app_loads == 0


def test_global_soft_isolation_keeps_total_hard_hysteresis() -> None:
    callback_only = facts(outbox_active=200, outbox_shared_active=0, callback_dead=20)
    assert evaluate_capacity(callback_only).state == "open"
    assert evaluate_capacity(callback_only, previous_state="degraded").state == "open"
    assert evaluate_capacity(replace(callback_only, outbox_shared_active=200)).state == "degraded"
    assert (
        evaluate_capacity(replace(callback_only, outbox_active=1000), previous_state="closed").state
        == "closed"
    )
    assert (
        evaluate_capacity(replace(callback_only, callback_dead=100), previous_state="closed").state
        == "closed"
    )
    assert evaluate_capacity(replace(callback_only, outbox_oldest_age_s=300)).state == "degraded"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"app_callback_active": 0},
        {"app_callback_active": 2000},
        {"app_callback_dead": 0},
        {"app_callback_dead": 200},
    ],
)
def test_application_safety_caps_are_below_global_caps(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="global hard cap"):
        SendAdmissionLimits(**kwargs)


@pytest.mark.asyncio
async def test_repository_uses_bound_app_id_and_includes_future_pending_tasks() -> None:
    connection = FakeConnection({"active": 200, "dead": 20})
    repository = SqlSendAdmissionRepository(settings=object(), redis=object())  # type: ignore[arg-type]
    repository._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]
    assert await repository.load_app_callbacks(17) == AppCallbackFacts(200, 20)
    sql, params = connection.calls[0]
    assert params == {"app_id": 17}
    assert "app_id=:app_id" in sql and "pending','retrying','dead" in sql
    assert "next_retry_at" not in sql and "outbox_event" not in sql
    for bad_id in (0, -1, True):
        with pytest.raises(ValueError):
            await repository.load_app_callbacks(bad_id)


@pytest.mark.parametrize("active,dead", [(-1, 0), (0, -1), (True, 0), ("0", 0)])
def test_malformed_callback_facts_are_not_zero(active: Any, dead: Any) -> None:
    with pytest.raises(ValueError):
        AppCallbackFacts(active, dead)
