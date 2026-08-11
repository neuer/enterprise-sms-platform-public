from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from app.services.callback import DeliveryOutcome
from app.services.callback_authority import CallbackAuthorityBusy
from app.services.callback_worker import (
    CALLBACK_AUTHORITY_BUSY_DELAY_S,
    RETRY_DELAYS_S,
    CallbackClaim,
    CallbackLeaseLost,
    CallbackWorker,
    classify_callback_http_status,
)

EVENT_ID = UUID("10000000-0000-4000-8000-000000000009")
LEASE_ID = UUID("20000000-0000-4000-8000-000000000009")


class FakeRepository:
    def __init__(
        self,
        claim: CallbackClaim | None,
        *,
        heartbeat_ok: bool = True,
    ) -> None:
        self.claim_value = claim
        self.heartbeat_ok = heartbeat_ok
        self.events: list[tuple[str, Any]] = []

    async def claim(
        self,
        task_id: int,
        *,
        lease_seconds: int,
    ) -> CallbackClaim | None:
        assert lease_seconds >= 3
        self.events.append(("claim", task_id))
        return self.claim_value

    async def heartbeat(
        self,
        task_id: int,
        lease_id: UUID,
        *,
        lease_seconds: int,
    ) -> bool:
        assert task_id == 9 and lease_id == LEASE_ID and lease_seconds >= 3
        self.events.append(("heartbeat", task_id))
        return self.heartbeat_ok

    async def mark_done(
        self,
        task_id: int,
        lease_id: UUID,
        http_code: int,
    ) -> None:
        assert lease_id == LEASE_ID
        self.events.append(("done", (task_id, http_code)))

    async def mark_authority_busy(
        self,
        task_id: int,
        lease_id: UUID,
        *,
        retry_count: int,
        delay_s: int,
    ) -> None:
        assert lease_id == LEASE_ID
        self.events.append(("authority_busy", (task_id, retry_count, delay_s)))

    async def mark_retry(
        self,
        task_id: int,
        lease_id: UUID,
        *,
        retry_count: int,
        delay_s: int,
        http_code: int | None,
        error: str | None,
    ) -> None:
        assert lease_id == LEASE_ID
        self.events.append(
            ("retry", (task_id, retry_count, delay_s, http_code, error))
        )

    async def mark_dead(
        self,
        task_id: int,
        lease_id: UUID,
        *,
        retry_count: int,
        http_code: int | None,
        error: str | None,
    ) -> None:
        assert lease_id == LEASE_ID
        self.events.append(("dead", (task_id, retry_count, http_code, error)))


class FakeDelivery:
    def __init__(self, outcome: DeliveryOutcome) -> None:
        self.outcome = outcome
        self.calls: list[int] = []

    async def deliver(self, task_id: int, lease_id: UUID) -> DeliveryOutcome:
        assert lease_id == LEASE_ID
        self.calls.append(task_id)
        return self.outcome


class FakeAlerts:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, **values: Any) -> None:
        self.events.append(values)


def claim(retry_count: int = 0) -> CallbackClaim:
    return CallbackClaim(
        9,
        7,
        EVENT_ID,
        "batch.finished",
        retry_count,
        LEASE_ID,
        datetime(2026, 7, 28, 8, 1, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_duplicate_task_that_cannot_claim_does_not_deliver() -> None:
    delivery = FakeDelivery(DeliveryOutcome(True, 200))

    assert await CallbackWorker(FakeRepository(None), delivery, FakeAlerts()).process(9) == 0
    assert delivery.calls == []


@pytest.mark.asyncio
async def test_successful_2xx_marks_done() -> None:
    repository = FakeRepository(claim())

    assert await CallbackWorker(
        repository,
        FakeDelivery(DeliveryOutcome(True, 204)),
        FakeAlerts(),
    ).process(9) == 1
    assert ("done", (9, 204)) in repository.events


@pytest.mark.asyncio
async def test_authority_contention_defers_without_consuming_delivery_retry() -> None:
    repository = FakeRepository(claim(4))

    await CallbackWorker(
        repository,
        FakeDelivery(DeliveryOutcome(False, None, CallbackAuthorityBusy.__name__)),
        FakeAlerts(),
    ).process(9)

    assert (
        "authority_busy",
        (9, 4, CALLBACK_AUTHORITY_BUSY_DELAY_S),
    ) in repository.events
    assert all(name not in {"retry", "dead", "done"} for name, _ in repository.events)


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_count", range(5))
async def test_failure_schedules_each_required_retry_delay(retry_count: int) -> None:
    repository = FakeRepository(claim(retry_count))
    alerts = FakeAlerts()

    await CallbackWorker(
        repository,
        FakeDelivery(DeliveryOutcome(False, 500)),
        alerts,
    ).process(9)

    assert (
        "retry",
        (9, retry_count, RETRY_DELAYS_S[retry_count], 500, None),
    ) in repository.events
    assert alerts.events == []


@pytest.mark.asyncio
async def test_fifth_retry_failure_marks_dead_and_emits_log_sink_alert() -> None:
    repository = FakeRepository(claim(5))
    alerts = FakeAlerts()

    await CallbackWorker(
        repository,
        FakeDelivery(DeliveryOutcome(False, None, "TimeoutError")),
        alerts,
    ).process(9)

    assert ("dead", (9, 5, None, "TimeoutError")) in repository.events
    assert alerts.events == [
        {
            "alert_type": "callback_dead",
            "level": "crit",
            "title": "结果回调重试耗尽",
            "detail": {
                "callback_task_id": 9,
                "app_id": 7,
                "event": "batch.finished",
                "failure_kind": "retries_exhausted",
            },
            "dedup_key": "callback_dead:9",
        }
    ]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 422])
@pytest.mark.asyncio
async def test_permanent_4xx_marks_dead_without_retry(status: int) -> None:
    repository = FakeRepository(claim())
    alerts = FakeAlerts()

    await CallbackWorker(
        repository,
        FakeDelivery(DeliveryOutcome(False, status)),
        alerts,
    ).process(9)

    assert ("dead", (9, 0, status, "permanent_failure")) in repository.events
    assert alerts.events == [
        {
            "alert_type": "callback_dead",
            "level": "crit",
            "title": "结果回调永久失败",
            "detail": {
                "callback_task_id": 9,
                "app_id": 7,
                "event": "batch.finished",
                "failure_kind": "permanent_failure",
                "http_code": status,
            },
            "dedup_key": f"callback_permanent:9:{status}",
        }
    ]


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
@pytest.mark.asyncio
async def test_retryable_statuses_keep_retry_schedule(status: int) -> None:
    repository = FakeRepository(claim(1))

    await CallbackWorker(
        repository,
        FakeDelivery(DeliveryOutcome(False, status)),
        FakeAlerts(),
    ).process(9)

    assert (
        "retry",
        (9, 1, RETRY_DELAYS_S[1], status, None),
    ) in repository.events


@pytest.mark.asyncio
async def test_classify_callback_http_status_is_conservative() -> None:
    assert classify_callback_http_status(200) == "success"
    assert classify_callback_http_status(400) == "permanent"
    assert classify_callback_http_status(429) == "retryable"
    assert classify_callback_http_status(599) == "retryable"


@pytest.mark.asyncio
async def test_worker_uses_runtime_retry_schedule() -> None:
    repository = FakeRepository(claim(1))

    await CallbackWorker(
        repository,
        FakeDelivery(DeliveryOutcome(False, 503)),
        FakeAlerts(),
        retry_delays_s=(7, 11),
    ).process(9)

    assert ("retry", (9, 1, 11, 503, None)) in repository.events


@pytest.mark.asyncio
async def test_heartbeat_loss_cancels_delivery_and_prevents_state_update() -> None:
    repository = FakeRepository(claim(), heartbeat_ok=False)

    class SlowDelivery:
        cancelled = False

        async def deliver(self, task_id: int, lease_id: UUID) -> DeliveryOutcome:
            assert task_id == 9 and lease_id == LEASE_ID
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            return DeliveryOutcome(True, 200)

    delivery = SlowDelivery()
    worker = CallbackWorker(
        repository,
        delivery,
        FakeAlerts(),
        lease_seconds=3,
        heartbeat_interval_s=0.001,
    )

    with pytest.raises(CallbackLeaseLost):
        await worker.process(9)

    assert delivery.cancelled
    assert ("heartbeat", 9) in repository.events
    assert all(name not in {"done", "retry", "dead"} for name, _ in repository.events)
