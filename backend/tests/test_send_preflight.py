"""实际 worker 的初次、重试、备用及任务重建均经过最终预检。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import app.tasks.send as send
from app.services.vendor_control_state import (
    VendorControlState,
    VendorControlStateGuard,
    VendorControlStateUnavailable,
)
from app.tasks.send import SendWorker, SubmitOutcome
from app.vendor.failover import InvokeClaim, InvokeClaimKind
from app.vendor.routing import VendorHealth, VendorRouter
from app.vendor.zhihui import VendorTransportError
from tests.test_send_worker import FakeBucket, FakeGateway, FakeStore, chunk
from tests.vendor_failover_r5_support import two_vendor_registry


class Store(FakeStore):
    def __init__(self, status: str = "failover_pending") -> None:
        super().__init__()
        self.chunk_status = status
        self.attempt_seq = 1
        self.denied = False
        self.payload = replace(
            chunk(), status=status, next_vendor="secondary", failover_from_attempt_id=1
        )

    async def refresh_invoke_payload(self, _id: int) -> Any:
        return replace(
            self.payload,
            denied_recipient_count=int(self.denied),
            phones=() if self.denied else self.payload.phones,
        )

    async def load_chunk(self, _id: int) -> Any:
        return self.payload, "realtime"


class Bucket(FakeBucket):
    def __init__(self, after_acquire: Any = None) -> None:
        super().__init__()
        self.after_acquire = after_acquire
        self.refunded_vendors: list[str] = []

    async def acquire(self, **kwargs: Any) -> int:
        result = await super().acquire(**kwargs)
        if self.after_acquire:
            self.after_acquire()
        return result

    async def refund(self, **kwargs: Any) -> None:
        self.refunded_vendors.append(kwargs["vendor_id"])
        await super().refund(**kwargs)


class Gateway(FakeGateway):
    async def aclose(self) -> None:
        pass


def worker(store: Store, bucket: Bucket, **kwargs: Any) -> tuple[SendWorker, Gateway, Gateway]:
    primary, backup = Gateway(["primary"]), Gateway(["backup"])
    return (
        SendWorker(
            primary,
            store,
            bucket,
            gateways={"zhihui": primary, "secondary": backup},
            router=VendorRouter(("zhihui", "secondary")),
            registry=two_vendor_registry(),
            **kwargs,
        ),
        primary,
        backup,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "retrying", "failover_pending"])
async def test_legal_modes_still_invoke_expected_gateway(status: str) -> None:
    store, bucket = Store(status), Bucket()
    service, primary, backup = worker(store, bucket)
    result = await service.submit(store.payload, lane="realtime")
    assert result is SubmitOutcome.SUBMITTED
    assert (primary.calls, backup.calls) == ((0, 1) if status == "failover_pending" else (1, 0))
    assert bucket.refunds == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "retrying", "failover_pending"])
@pytest.mark.parametrize("change", ["pause", "recipients", "control"])
async def test_claim_completion_rechecks_before_http(
    status: str, change: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, bucket = Store(status), Bucket()
    heartbeat = datetime(2040, 1, 1, tzinfo=UTC)
    now = heartbeat
    guard = VendorControlStateGuard(clock=lambda: now)
    state = VendorControlState("controlled", heartbeat, True, 1, None, 100)
    monkeypatch.setattr(guard, "_read", lambda: state)
    name = "claim_next_vendor_invoke" if status == "failover_pending" else "claim_submission"
    claim = getattr(store, name)

    async def changed_claim(*args: Any, **kwargs: Any) -> Any:
        nonlocal now
        result = await claim(*args, **kwargs)
        if change == "pause":
            store.paused = True
        elif change == "recipients":
            store.denied = True
        else:
            now += timedelta(seconds=31)
        return result

    monkeypatch.setattr(store, name, changed_claim)
    service, primary, backup = worker(
        store, bucket, enforce_live_test_recipients=True, control_guard=guard
    )
    result = await service.submit(store.payload, lane="realtime")
    expected = "rejected" if change == "recipients" else "paused"
    assert result.value == expected and primary.calls == backup.calls == 0
    assert ("attempt", (2, expected)) in store.events
    assert bucket.refunded_vendors == ["secondary" if status == "failover_pending" else "zhihui"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["pause", "recipients", "control"])
async def test_task_recovery_rechecks_after_token(
    change: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store()
    stale = False

    def change_state() -> None:
        nonlocal stale
        if change == "pause":
            store.paused = True
        elif change == "recipients":
            store.denied = True
        else:
            stale = True

    class Control:
        def require_fresh(self) -> None:
            if stale:
                raise VendorControlStateUnavailable("stale", requires_critical_pause=True)

    bucket = Bucket(change_state)
    service, primary, backup = worker(
        store, bucket, enforce_live_test_recipients=True, control_guard=Control()
    )

    async def components() -> Any:
        return service, store, primary, 500

    async def heartbeat(*_args: Any, **_kwargs: Any) -> None:
        pass

    monkeypatch.setattr(send, "_components", components)
    monkeypatch.setattr("app.services.runtime_heartbeat.touch_runtime_heartbeat", heartbeat)
    result = await send._run_chunk(3)
    assert result.outcome is (
        SubmitOutcome.REJECTED if change == "recipients" else SubmitOutcome.PAUSED
    )
    assert primary.calls == backup.calls == 0
    assert store.attempt_seq == 1
    assert bucket.refunded_vendors == ["secondary"]


@pytest.mark.asyncio
async def test_primary_health_failure_does_not_pause_eligible_backup() -> None:
    async def health() -> Any:
        return (VendorHealth("zhihui", False), VendorHealth("secondary", True))

    store, bucket = Store(), Bucket()
    service, primary, backup = worker(store, bucket, health=health)
    assert await service.submit(store.payload, lane="realtime") is SubmitOutcome.SUBMITTED
    assert primary.calls == 0 and backup.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["missing", "category", "cas_loser"])
async def test_ineligible_or_stale_backup_never_invokes(mode: str) -> None:
    store, bucket = Store(), Bucket()
    service, primary, backup = worker(store, bucket)
    if mode == "missing":
        del service.gateways["secondary"]
    elif mode == "category":
        service.registry = two_vendor_registry(secondary_categories=frozenset({"market"}))
    else:

        async def loser(*_args: Any, **_kwargs: Any) -> InvokeClaim:
            return InvokeClaim(InvokeClaimKind.ALREADY_HANDLED)

        store.claim_next_vendor_invoke = loser
    result = await service.submit(store.payload, lane="realtime")
    assert result is (SubmitOutcome.STALE if mode == "cas_loser" else SubmitOutcome.PAUSED)
    assert primary.calls == backup.calls == 0
    assert bucket.refunds == (1 if mode == "cas_loser" else 0)


@pytest.mark.asyncio
async def test_cancel_during_final_preflight_refunds_once_without_claim() -> None:
    entered = asyncio.Event()
    block = False
    store = Store()

    def acquired() -> None:
        nonlocal block
        block = True

    async def paused(_lane: str) -> bool:
        if block:
            entered.set()
            await asyncio.Event().wait()
        return False

    store.is_paused = paused
    bucket = Bucket(acquired)
    service, primary, backup = worker(store, bucket)
    owner = asyncio.create_task(service.submit(store.payload, lane="realtime"))
    await entered.wait()
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert store.chunk_status == "failover_pending" and store.attempt_seq == 1
    assert primary.calls == backup.calls == 0
    assert bucket.refunded_vendors == ["secondary"]


@pytest.mark.asyncio
async def test_after_invoke_timeout_is_uncertain_without_refund() -> None:
    store, bucket = Store(), Bucket()
    service, primary, backup = worker(store, bucket)
    backup.outcomes = iter([VendorTransportError("timeout")])
    assert await service.submit(store.payload, lane="realtime") is SubmitOutcome.UNCERTAIN
    assert backup.calls == 1 and primary.calls == 0 and bucket.refunds == 0


@pytest.mark.asyncio
async def test_cancel_after_claim_before_http_settles_and_refunds() -> None:
    entered = asyncio.Event()
    store, bucket = Store(), Bucket()

    async def paused(_lane: str) -> bool:
        if store.chunk_status == "submitting":
            entered.set()
            await asyncio.Event().wait()
        return False

    store.is_paused = paused
    service, primary, backup = worker(store, bucket)
    task = asyncio.create_task(service.submit(store.payload, lane="realtime"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ("attempt", (2, "paused")) in store.events
    assert primary.calls == backup.calls == 0
    assert bucket.refunded_vendors == ["secondary"]


@pytest.mark.asyncio
async def test_cancellation_after_gateway_entry_is_uncertain_without_refund() -> None:
    entered = asyncio.Event()
    store, bucket = Store(), Bucket()
    service, primary, backup = worker(store, bucket)

    async def sending(*_args: Any, **_kwargs: Any) -> str:
        backup.calls += 1
        entered.set()
        await asyncio.Event().wait()
        return "unreachable"

    backup.send = sending
    task = asyncio.create_task(service.submit(store.payload, lane="realtime"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert backup.calls == 1 and primary.calls == 0 and bucket.refunds == 0
    assert any(event[0] == "uncertain" for event in store.events)
