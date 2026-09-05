from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

import app.tasks.send as send_module
from app.services.vendor_control_state import VendorControlStateUnavailable
from app.services.vendor_test_budget import SubmissionClaim, SubmissionClaimStatus
from app.services.vendor_test_guard import VendorTestRecipientDenied
from app.tasks.send import (
    ChunkPayload,
    FinalizeKind,
    FinalizeReport,
    SendWorker,
)
from app.vendor.routing import PRIMARY_VENDOR_ID, VendorAttempt, VendorRouter
from app.vendor.zhihui import VendorApiError, VendorProtocolError, VendorTransportError


class FakeGateway:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = iter(outcomes)
        self.calls = 0

    async def send(self, *args: Any, **kwargs: Any) -> str:
        self.calls += 1
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return str(outcome)


class FakeBucket:
    def __init__(self) -> None:
        self.acquires = 0
        self.refunds = 0
        self.refund_epochs: list[int] = []

    async def acquire(self, **kwargs: Any) -> int | None:
        self.acquires += 1
        return 1000

    async def refund(self, *, lease_epoch: int, **kwargs: Any) -> None:
        self.refunds += 1
        self.refund_epochs.append(lease_epoch)


class TokenWaitObserved(RuntimeError):
    pass


class FakeStore:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []
        self.failed_messages: list[str] = []
        self.split_chunks: list[ChunkPayload] = []
        self.claimed = True
        self.claim_counts: list[int] = []
        self.claim_segments: list[int] = []
        self.claim_result = SubmissionClaim(SubmissionClaimStatus.CLAIMED)
        self.paused = False

    async def claim_submission(
        self,
        chunk_id: int,
        expected_retry_count: int,
        segments: int,
        *,
        enforce_live_test_budget: bool,
    ) -> SubmissionClaim:
        self.claim_counts.append(expected_retry_count)
        self.claim_segments.append(segments)
        self.events.append(("submitting", chunk_id))
        if not self.claimed:
            return SubmissionClaim(SubmissionClaimStatus.STALE)
        return self.claim_result

    async def mark_submitting(self, chunk_id: int, expected_retry_count: int) -> bool:
        self.claim_counts.append(expected_retry_count)
        self.events.append(("submitting", chunk_id))
        return self.claimed

    async def mark_submitted(self, chunk_id: int, task_id: str) -> None:
        self.events.append(("submitted", (chunk_id, task_id)))

    async def complete_vendor_attempt(
        self,
        attempt_id: int,
        *,
        outcome: str,
        safe_to_failover: bool = False,
        vendor_code: int | None = None,
    ) -> bool:
        self.events.append(("attempt", (attempt_id, outcome)))
        return True

    async def finalize_vendor_attempt(
        self,
        attempt_id: int,
        chunk_id: int,
        *,
        expected_generation: int,
        result: str,
        vendor_task_id: str | None = None,
        vendor_code: int | None = None,
        safe_to_failover: bool = False,
        retry_delay_s: int | None = None,
        expected_retry_count: int | None = None,
        batch_id: int | None = None,
        balance_blocked: bool = False,
    ) -> FinalizeReport:
        if result == "submitted":
            await self.mark_submitted(chunk_id, vendor_task_id or "")
        elif result == "uncertain":
            await self.mark_uncertain(chunk_id)
        elif result == "retry_scheduled":
            scheduled = await self.schedule_retry(
                chunk_id,
                vendor_code or 0,
                expected_retry_count or 0,
                retry_delay_s or 1,
            )
            if not scheduled:
                return FinalizeReport(FinalizeKind.LOST_CAS, result)
        elif result == "delayed":
            await self.delay(chunk_id, vendor_code or 0, retry_delay_s or 1)
        elif result == "paused":
            if balance_blocked:
                await self.balance_blocked(batch_id or 0, chunk_id)
            else:
                await self.pause_blocked(chunk_id, vendor_code or 0)
        elif result in {"rejected", "failed"} and not safe_to_failover:
            await self.mark_failed(chunk_id, vendor_code or 0, result)
        await self.complete_vendor_attempt(
            attempt_id,
            outcome=result,
            safe_to_failover=safe_to_failover,
            vendor_code=vendor_code,
        )
        return FinalizeReport(FinalizeKind.APPLIED, result)

    async def mark_failed(self, chunk_id: int, code: int, message: str) -> None:
        self.failed_messages.append(message)
        self.events.append(("failed", (chunk_id, code)))

    async def mark_uncertain(self, chunk_id: int) -> None:
        self.events.append(("uncertain", chunk_id))

    async def schedule_retry(
        self,
        chunk_id: int,
        code: int,
        expected_retry_count: int,
        delay_s: int,
    ) -> bool:
        self.events.append(("retrying", (chunk_id, code)))
        return True

    async def delay(self, chunk_id: int, code: int, delay_s: int) -> None:
        self.events.append(("delay", (chunk_id, code, delay_s)))

    async def balance_blocked(self, batch_id: int, chunk_id: int) -> None:
        self.events.append(("balance", (batch_id, chunk_id)))

    async def pause_blocked(self, chunk_id: int, code: int) -> None:
        self.events.append(("pause_blocked", (chunk_id, code)))

    async def pause_queues(self, code: int) -> None:
        self.events.append(("pause", code))

    async def split_once(self, chunk: ChunkPayload) -> list[ChunkPayload]:
        self.events.append(("split", chunk.chunk_id))
        return self.split_chunks

    async def reject_disallowed_recipient(self, chunk_id: int, denied_count: int) -> None:
        self.events.append(("guard_denied", (chunk_id, denied_count)))

    async def defer_daily_limit(
        self,
        chunk_id: int,
        lane: str,
        reset_at: datetime,
    ) -> None:
        self.events.append(("daily_defer", (chunk_id, lane, reset_at)))

    async def pause_daily_limit(self, lane: str, reset_at: datetime) -> None:
        self.events.append(("daily_pause", (lane, reset_at)))

    async def pause_control_agent_stale(self) -> None:
        self.events.append(("control_pause", None))

    async def release_control_claim(self, chunk_id: int) -> None:
        self.events.append(("control_release", chunk_id))

    async def release_unsent(self, chunk_id: int) -> None:
        self.events.append(("release_unsent", chunk_id))

    async def is_paused(self, lane: str) -> bool:
        return self.paused


class FakeVendorMonitor:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    async def record_failure(self, *, code: int, chunk_id: int, batch_id: int) -> None:
        self.events.append(("failure", (code, chunk_id, batch_id)))

    async def record_success(self) -> None:
        self.events.append(("success", None))


def chunk() -> ChunkPayload:
    return ChunkPayload(
        chunk_id=3,
        batch_id=2,
        custom_id="a" * 32,
        phones=("13800138000",),
        content="验证码123456",
        template_id="",
        sign_name="【青鸾】",
    )


class AllowGuard:
    def __init__(self, denied_count: int = 0) -> None:
        self.denied_count = denied_count
        self.calls: list[tuple[str, ...]] = []

    def require_allowed(self, phones: tuple[str, ...]) -> None:
        self.calls.append(phones)
        if self.denied_count:
            raise VendorTestRecipientDenied(self.denied_count)


class ControlGuard:
    def __init__(self, outcomes: list[bool] | None = None) -> None:
        self.outcomes = iter(outcomes or [True])
        self.calls = 0

    def require_fresh(self) -> object:
        self.calls += 1
        if not next(self.outcomes):
            raise VendorControlStateUnavailable(
                "真实联调控制状态不可用",
                requires_critical_pause=True,
            )
        return object()


@pytest.mark.asyncio
async def test_stale_control_state_blocks_before_claim_vendor_and_uncertain() -> None:
    gateway = FakeGateway(["must-not-send"])
    store = FakeStore()
    bucket = FakeBucket()

    await SendWorker(
        gateway,
        store,
        bucket,
        control_guard=ControlGuard([False]),
    ).submit(chunk(), lane="realtime")

    assert store.events == [("control_pause", None)]
    assert store.claim_segments == []
    assert bucket.acquires == 0
    assert gateway.calls == 0
    assert not any(event[0] == "uncertain" for event in store.events)


@pytest.mark.asyncio
async def test_state_that_stales_after_claim_is_released_retryable_not_uncertain() -> None:
    gateway = FakeGateway(["must-not-send"])
    store = FakeStore()
    bucket = FakeBucket()

    await SendWorker(
        gateway,
        store,
        bucket,
        control_guard=ControlGuard([True, False]),
    ).submit(chunk(), lane="realtime")

    assert store.events == [
        ("submitting", 3),
        ("control_pause", None),
        ("control_release", 3),
    ]
    assert bucket.refund_epochs == [1000]
    assert gateway.calls == 0
    assert not any(event[0] == "uncertain" for event in store.events)


@pytest.mark.asyncio
async def test_pause_persistence_failure_still_releases_claim_and_refunds_token() -> None:
    class FailingPauseStore(FakeStore):
        async def pause_control_agent_stale(self) -> None:
            self.events.append(("control_pause", None))
            raise ConnectionError("redis unavailable")

    gateway = FakeGateway(["must-not-send"])
    store = FailingPauseStore()
    bucket = FakeBucket()

    with pytest.raises(ConnectionError, match="redis unavailable"):
        await SendWorker(
            gateway,
            store,
            bucket,
            control_guard=ControlGuard([True, False]),
        ).submit(chunk(), lane="realtime")

    assert store.events == [
        ("submitting", 3),
        ("control_pause", None),
        ("control_release", 3),
    ]
    assert bucket.refund_epochs == [1000]
    assert gateway.calls == 0
    assert not any(event[0] == "uncertain" for event in store.events)


@pytest.mark.asyncio
async def test_live_guard_denies_legacy_chunk_before_token_claim_or_vendor() -> None:
    gateway = FakeGateway(["must-not-send"])
    store = FakeStore()
    bucket = FakeBucket()
    guard = AllowGuard(1)

    await SendWorker(
        gateway,
        store,
        bucket,
        recipient_guard=guard,
        enforce_live_test_budget=True,
    ).submit(chunk(), lane="realtime")

    assert guard.calls == [("13800138000",)]
    assert store.events == [("guard_denied", (3, 1))]
    assert store.claim_segments == []
    assert bucket.acquires == 0
    assert gateway.calls == 0


@pytest.mark.asyncio
async def test_live_worker_rejects_recipient_disabled_in_postgres_before_vendor() -> None:
    denied = ChunkPayload(
        chunk_id=3,
        batch_id=2,
        custom_id="a" * 32,
        phones=(),
        content="验证码123456",
        template_id="",
        sign_name="【青鸾】",
        denied_recipient_count=1,
    )
    gateway = FakeGateway(["must-not-send"])
    store = FakeStore()
    bucket = FakeBucket()

    await SendWorker(
        gateway,
        store,
        bucket,
        enforce_live_test_budget=True,
        enforce_live_test_recipients=True,
    ).submit(denied, lane="realtime")

    assert store.events == [("guard_denied", (3, 1))]
    assert bucket.acquires == 0
    assert gateway.calls == 0


@pytest.mark.asyncio
async def test_worker_uses_unique_billing_implementation_for_atomic_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    billing_calls: list[tuple[str, int]] = []

    def calculate(final_content: str, *, recipient_count: int) -> int:
        billing_calls.append((final_content, recipient_count))
        return 7

    monkeypatch.setattr(send_module, "calculate_quota_cost", calculate)
    store = FakeStore()
    guard = AllowGuard()

    await SendWorker(
        FakeGateway(["task-ok"]),
        store,
        FakeBucket(),
        recipient_guard=guard,
        enforce_live_test_budget=True,
    ).submit(chunk(), lane="realtime")

    assert billing_calls == [("【青鸾】验证码123456", 1)]
    assert store.claim_segments == [7]
    assert ("submitted", (3, "task-ok")) in store.events


@pytest.mark.asyncio
async def test_daily_limit_refunds_token_defers_chunk_and_never_calls_vendor() -> None:
    reset_at = datetime(2026, 7, 17, tzinfo=UTC)
    store = FakeStore()
    store.claim_result = SubmissionClaim(SubmissionClaimStatus.DAILY_LIMIT, reset_at)
    gateway = FakeGateway(["must-not-send"])
    bucket = FakeBucket()

    await SendWorker(
        gateway,
        store,
        bucket,
        recipient_guard=AllowGuard(),
        enforce_live_test_budget=True,
    ).submit(chunk(), lane="bulk")

    assert bucket.refund_epochs == [1000]
    assert ("daily_defer", (3, "bulk", reset_at)) in store.events
    assert ("daily_pause", ("bulk", reset_at)) in store.events
    assert gateway.calls == 0


@pytest.mark.asyncio
async def test_duplicate_delivery_that_cannot_claim_chunk_never_calls_vendor() -> None:
    trace: list[str] = []

    class TracingBucket(FakeBucket):
        async def acquire(self, **kwargs: Any) -> int | None:
            trace.append("token")
            return 12345

        async def refund(self, *, lease_epoch: int, **kwargs: Any) -> None:
            trace.append(f"refund:{lease_epoch}")

    class TracingStore(FakeStore):
        async def claim_submission(
            self,
            chunk_id: int,
            expected_retry_count: int,
            segments: int,
            *,
            enforce_live_test_budget: bool,
        ) -> SubmissionClaim:
            trace.append("claim")
            return await super().claim_submission(
                chunk_id,
                expected_retry_count,
                segments,
                enforce_live_test_budget=enforce_live_test_budget,
            )

    gateway = FakeGateway(["must-not-send"])
    store = TracingStore()
    store.claimed = False
    await SendWorker(gateway, store, TracingBucket()).submit(chunk(), lane="realtime")
    assert trace == ["token", "claim", "refund:12345"]
    assert gateway.calls == 0


@pytest.mark.asyncio
async def test_waiting_for_token_does_not_claim_or_create_stale_submitting() -> None:
    class EmptyBucket(FakeBucket):
        async def acquire(self, **kwargs: Any) -> int | None:
            return None

    async def observe_wait(_seconds: float) -> None:
        raise TokenWaitObserved

    store = FakeStore()
    worker = SendWorker(
        FakeGateway(["must-not-send"]),
        store,
        EmptyBucket(),
        sleeper=observe_wait,
    )

    with pytest.raises(TokenWaitObserved):
        await worker.submit(chunk(), lane="realtime")

    assert store.events == []


@pytest.mark.asyncio
async def test_retry_is_persisted_without_sleeping_in_current_worker() -> None:
    class RetryBucket(FakeBucket):
        def __init__(self) -> None:
            self.calls = 0

        async def acquire(self, **kwargs: Any) -> int | None:
            self.calls += 1
            return 1000 if self.calls == 1 else None

    async def observe_wait(seconds: float) -> None:
        raise AssertionError(f"current worker must not sleep for a persisted retry: {seconds}")

    store = FakeStore()
    bucket = RetryBucket()
    worker = SendWorker(
        FakeGateway([VendorApiError(5002, "fast")]),
        store,
        bucket,
        sleeper=observe_wait,
    )

    await worker.submit(chunk(), lane="realtime")

    assert store.events == [
        ("submitting", 3),
        ("retrying", (3, 5002)),
    ]
    assert bucket.calls == 1


@pytest.mark.asyncio
async def test_stale_payload_retry_count_cannot_claim_and_refunds_same_epoch() -> None:
    class FencedStore(FakeStore):
        async def claim_submission(
            self,
            chunk_id: int,
            expected_retry_count: int,
            segments: int,
            *,
            enforce_live_test_budget: bool,
        ) -> SubmissionClaim:
            await super().claim_submission(
                chunk_id,
                expected_retry_count,
                segments,
                enforce_live_test_budget=enforce_live_test_budget,
            )
            return SubmissionClaim(
                SubmissionClaimStatus.CLAIMED
                if expected_retry_count == 5
                else SubmissionClaimStatus.STALE
            )

    stale = ChunkPayload(
        3,
        2,
        "a" * 32,
        ("13800138000",),
        "通知",
        "",
        "【青鸾】",
        retry_count=4,
    )
    bucket = FakeBucket()
    gateway = FakeGateway(["must-not-send"])
    store = FencedStore()

    await SendWorker(gateway, store, bucket).submit(stale, lane="realtime")

    assert store.claim_counts == [4]
    assert bucket.refund_epochs == [1000]
    assert gateway.calls == 0


@pytest.mark.asyncio
async def test_transport_error_becomes_uncertain_without_retry() -> None:
    gateway = FakeGateway([VendorTransportError("timeout")])
    store = FakeStore()
    worker = SendWorker(gateway, store, FakeBucket())

    await worker.submit(chunk(), lane="realtime")

    assert gateway.calls == 1
    assert ("uncertain", 3) in store.events
    assert not any(event[0] == "failed" for event in store.events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        VendorProtocolError("vendor HTTP status 503"),
        VendorProtocolError("vendor response is not JSON"),
        VendorProtocolError("vendor Send.data is invalid"),
    ],
)
async def test_protocol_error_becomes_uncertain_without_retry_or_failure(
    error: VendorProtocolError,
) -> None:
    gateway = FakeGateway([error])
    store = FakeStore()

    await SendWorker(gateway, store, FakeBucket()).submit(chunk(), lane="realtime")

    assert gateway.calls == 1
    assert store.events == [("submitting", 3), ("uncertain", 3)]


@pytest.mark.asyncio
async def test_backoff_error_schedules_one_durable_retry_and_returns() -> None:
    gateway = FakeGateway([VendorApiError(5002, "fast"), VendorApiError(5003, "qps"), "task-9"])
    store = FakeStore()
    sleeps: list[float] = []

    async def sleeper(seconds: float) -> None:
        sleeps.append(seconds)

    worker = SendWorker(gateway, store, FakeBucket(), sleeper=sleeper)
    await worker.submit(chunk(), lane="realtime")

    assert sleeps == []
    assert gateway.calls == 1
    assert store.events == [
        ("submitting", 3),
        ("retrying", (3, 5002)),
    ]


@pytest.mark.asyncio
async def test_backoff_retry_rechecks_dynamic_allowlist_before_next_vendor_call() -> None:
    class DenySecondGuard(AllowGuard):
        def require_allowed(self, phones: tuple[str, ...]) -> None:
            self.calls.append(phones)
            if len(self.calls) == 2:
                raise VendorTestRecipientDenied(1)

    async def no_wait(_seconds: float) -> None:
        return None

    gateway = FakeGateway([VendorApiError(5002, "fast"), "must-not-send"])
    store = FakeStore()
    guard = DenySecondGuard()

    worker = SendWorker(
        gateway,
        store,
        FakeBucket(),
        recipient_guard=guard,
        enforce_live_test_budget=True,
        sleeper=no_wait,
    )
    await worker.submit(chunk(), lane="realtime")
    retried = ChunkPayload(
        chunk_id=3,
        batch_id=2,
        custom_id="a" * 32,
        phones=("13800138000",),
        content="通知",
        template_id="",
        sign_name="【青鸾】",
        retry_count=1,
    )
    await worker.submit(retried, lane="realtime")

    assert len(guard.calls) == 2
    assert gateway.calls == 1
    assert store.events == [
        ("submitting", 3),
        ("retrying", (3, 5002)),
        ("guard_denied", (3, 1)),
    ]


@pytest.mark.asyncio
async def test_balance_error_blocks_batch_and_pauses_both_queues() -> None:
    store = HistoryStore()
    worker = SendWorker(FakeGateway([VendorApiError(999, "balance")]), store, FakeBucket())
    await worker.submit(chunk(), lane="bulk")
    assert ("balance", (2, 3)) in store.events
    assert ("pause", 999) in store.events
    assert ("attempt", (1, "paused")) in store.events


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [1000, 1009, 5000, 10003, 10004])
async def test_pause_codes_requeue_chunk_instead_of_permanent_failed(code: int) -> None:
    """熔断码与 999 语义对齐：暂停队列 + chunk 回退可重投，不 mark_failed。"""

    store = FakeStore()
    monitor = FakeVendorMonitor()

    await SendWorker(
        FakeGateway([VendorApiError(code, "account issue")]),
        store,
        FakeBucket(),
        monitor=monitor,
    ).submit(chunk(), lane="realtime")

    assert ("pause_blocked", (3, code)) in store.events
    assert ("pause", code) in store.events
    assert all(event[0] != "failed" for event in store.events)
    assert monitor.events == [("failure", (code, 3, 2))]


@pytest.mark.asyncio
async def test_1010_fails_chunk_without_pausing_queues_and_emits_critical_monitor_event() -> None:
    store = FakeStore()
    monitor = FakeVendorMonitor()

    await SendWorker(
        FakeGateway([VendorApiError(1010, "IP not registered")]),
        store,
        FakeBucket(),
        monitor=monitor,
    ).submit(chunk(), lane="realtime")

    assert ("pause", 1010) not in store.events
    assert ("failed", (3, 1010)) in store.events
    assert monitor.events == [("failure", (1010, 3, 2))]


@pytest.mark.asyncio
async def test_delay_and_parameter_errors_are_not_immediately_retried() -> None:
    delayed_store = FakeStore()
    delayed_gateway = FakeGateway([VendorApiError(1011, "service time")])
    await SendWorker(delayed_gateway, delayed_store, FakeBucket()).submit(
        chunk(),
        lane="bulk",
    )
    assert ("delay", (3, 1011, 1800)) in delayed_store.events
    assert delayed_gateway.calls == 1

    failed_store = FakeStore()
    await SendWorker(
        FakeGateway([VendorApiError(1002, "bad content")]),
        failed_store,
        FakeBucket(),
    ).submit(chunk(), lane="realtime")
    assert ("failed", (3, 1002)) in failed_store.events

    locked_store = FakeStore()
    locked_gateway = FakeGateway([VendorApiError(10010, "locked")])
    await SendWorker(locked_gateway, locked_store, FakeBucket()).submit(
        chunk(),
        lane="realtime",
    )
    assert ("delay", (3, 10010, 300)) in locked_store.events
    assert locked_gateway.calls == 1


@pytest.mark.asyncio
async def test_unknown_vendor_code_fails_closed_without_retry() -> None:
    store = FakeStore()
    gateway = FakeGateway([VendorApiError(987654, "unknown"), "must-not-retry"])

    await SendWorker(gateway, store, FakeBucket()).submit(chunk(), lane="realtime")

    assert gateway.calls == 1
    assert ("failed", (3, 987654)) in store.events
    assert not any(event[0] in {"retrying", "delay", "split"} for event in store.events)


@pytest.mark.asyncio
async def test_vendor_reflection_is_replaced_before_chunk_persistence() -> None:
    reflected = "secretKey=credential-value content=验证码839204"
    store = FakeStore()

    await SendWorker(
        FakeGateway([VendorApiError(1002, reflected)]),
        store,
        FakeBucket(),
    ).submit(chunk(), lane="realtime")

    assert store.failed_messages == ["内容格式错误"]
    assert reflected not in "".join(store.failed_messages)


@pytest.mark.asyncio
async def test_1006_splits_once_and_submits_children() -> None:
    store = FakeStore()
    original = ChunkPayload(3, 2, "a" * 32, ("13800138000", "13900139000"), "通知", "", "")
    store.split_chunks = [
        ChunkPayload(4, 2, "a" * 24 + "00000002", ("13800138000",), "通知", "", ""),
        ChunkPayload(5, 2, "a" * 24 + "00000003", ("13900139000",), "通知", "", ""),
    ]
    gateway = FakeGateway([VendorApiError(1006, "too many"), "task-4", "task-5"])
    await SendWorker(gateway, store, FakeBucket()).submit(original, lane="realtime")
    assert gateway.calls == 3
    assert ("split", 3) in store.events
    assert ("submitted", (4, "task-4")) in store.events
    assert ("submitted", (5, "task-5")) in store.events


@pytest.mark.asyncio
async def test_backoff_budget_exhaustion_marks_terminal_failure() -> None:
    store = FakeStore()

    async def no_wait(_: float) -> None:
        return None

    gateway = FakeGateway([VendorApiError(5002, "fast")])
    exhausted = ChunkPayload(
        chunk_id=3,
        batch_id=2,
        custom_id="a" * 32,
        phones=("13800138000",),
        content="通知",
        template_id="",
        sign_name="【青鸾】",
        retry_count=5,
    )
    await SendWorker(gateway, store, FakeBucket(), sleeper=no_wait).submit(
        exhausted,
        lane="realtime",
    )
    assert gateway.calls == 1
    assert ("failed", (3, 5002)) in store.events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("persisted_retry_count", "expected_retry_events", "expected_failed"),
    [(2, 1, False), (4, 1, False), (5, 0, True)],
)
async def test_recovered_chunk_uses_persisted_retry_budget(
    persisted_retry_count: int,
    expected_retry_events: int,
    expected_failed: bool,
) -> None:
    recovered = ChunkPayload(
        chunk_id=3,
        batch_id=2,
        custom_id="a" * 32,
        phones=("13800138000",),
        content="通知",
        template_id="",
        sign_name="【青鸾】",
        retry_count=persisted_retry_count,
    )
    gateway = FakeGateway([VendorApiError(5002, "fast") for _ in range(6)])
    store = FakeStore()
    sleeps: list[float] = []

    async def sleeper(seconds: float) -> None:
        sleeps.append(seconds)

    await SendWorker(gateway, store, FakeBucket(), sleeper=sleeper).submit(
        recovered,
        lane="realtime",
    )

    assert gateway.calls == 1
    assert sleeps == []
    assert sum(event[0] == "retrying" for event in store.events) == expected_retry_events
    assert (("failed", (3, 5002)) in store.events) is expected_failed


@pytest.mark.asyncio
async def test_retry_schedule_uses_persisted_count_and_remaining_delay() -> None:
    class CasStore(FakeStore):
        def __init__(self) -> None:
            super().__init__()
            self.schedules: list[tuple[int, int, int, int]] = []

        async def schedule_retry(
            self,
            chunk_id: int,
            code: int,
            expected_retry_count: int,
            delay_s: int,
        ) -> bool:
            self.schedules.append((chunk_id, code, expected_retry_count, delay_s))
            return False

    recovered = ChunkPayload(
        3,
        2,
        "a" * 32,
        ("13800138000",),
        "通知",
        "",
        "【青鸾】",
        retry_count=4,
    )
    store = CasStore()
    gateway = FakeGateway([VendorApiError(5002, "fast") for _ in range(6)])

    await SendWorker(gateway, store, FakeBucket()).submit(recovered, lane="realtime")

    assert store.schedules == [(3, 5002, 4, 16)]
    assert gateway.calls == 1


@pytest.mark.asyncio
async def test_terminal_vendor_error_is_reported_but_transport_unknown_is_not() -> None:
    monitor = FakeVendorMonitor()
    store = FakeStore()
    await SendWorker(
        FakeGateway([VendorApiError(1000, "auth")]),
        store,
        FakeBucket(),
        monitor=monitor,
    ).submit(chunk(), lane="realtime")

    assert monitor.events == [("failure", (1000, 3, 2))]

    uncertain_monitor = FakeVendorMonitor()
    await SendWorker(
        FakeGateway([VendorTransportError("timeout")]),
        FakeStore(),
        FakeBucket(),
        monitor=uncertain_monitor,
    ).submit(chunk(), lane="realtime")
    assert uncertain_monitor.events == []


@pytest.mark.asyncio
async def test_vendor_success_resets_consecutive_failure_monitor() -> None:
    monitor = FakeVendorMonitor()
    await SendWorker(
        FakeGateway(["task-ok"]),
        FakeStore(),
        FakeBucket(),
        monitor=monitor,
    ).submit(chunk(), lane="realtime")
    assert monitor.events == [("success", None)]


@pytest.mark.asyncio
async def test_successful_vendor_call_with_writeback_failure_is_never_retried() -> None:
    class WritebackFailureStore(FakeStore):
        async def mark_submitted(self, chunk_id: int, task_id: str) -> None:
            raise RuntimeError("database unavailable")

    gateway = FakeGateway(["task-ok", "must-not-retry"])
    store = WritebackFailureStore()

    outcome = await SendWorker(gateway, store, FakeBucket()).submit(
        chunk(),
        lane="realtime",
    )

    assert outcome.value == "uncertain"
    assert gateway.calls == 1
    assert store.events == [("submitting", 3), ("uncertain", 3)]


@pytest.mark.asyncio
async def test_paused_lane_skips_token_claim_and_vendor() -> None:
    store = FakeStore()
    store.paused = True
    gateway = FakeGateway(["must-not-send"])
    bucket = FakeBucket()

    await SendWorker(gateway, store, bucket).submit(chunk(), lane="realtime")

    assert store.events == []
    assert bucket.acquires == 0
    assert gateway.calls == 0


@pytest.mark.asyncio
async def test_unexpected_error_after_vendor_starts_marks_uncertain() -> None:
    store = FakeStore()
    with pytest.raises(RuntimeError, match="vendor exploded"):
        await SendWorker(
            FakeGateway([RuntimeError("vendor exploded")]),
            store,
            FakeBucket(),
        ).submit(chunk(), lane="realtime")

    assert store.events == [("submitting", 3), ("uncertain", 3)]
    assert not any(event[0] == "release_unsent" for event in store.events)


@pytest.mark.asyncio
async def test_token_wait_aborts_when_lane_pauses_mid_wait() -> None:
    class StarvingBucket(FakeBucket):
        async def acquire(self, **kwargs: Any) -> int | None:
            self.acquires += 1
            return None

    class PauseDuringWaitStore(FakeStore):
        def __init__(self) -> None:
            super().__init__()
            self.checks = 0

        async def is_paused(self, lane: str) -> bool:
            self.checks += 1
            return self.checks >= 2

    store = PauseDuringWaitStore()
    gateway = FakeGateway(["must-not-send"])
    await SendWorker(
        gateway,
        store,
        StarvingBucket(),
        recipient_guard=AllowGuard(),
        enforce_live_test_budget=True,
    ).submit(chunk(), lane="realtime")

    assert gateway.calls == 0
    assert store.events == []
    assert store.checks >= 2


class HistoryStore(FakeStore):
    def __init__(self, attempts: list[VendorAttempt] | None = None) -> None:
        super().__init__()
        self.attempts = list(attempts or [])
        self.begun: list[tuple[str, int]] = []

    async def list_vendor_attempts(self, chunk_id: int) -> tuple[VendorAttempt, ...]:
        return tuple(self.attempts)

    async def begin_vendor_invoke(
        self,
        chunk_id: int,
        *,
        vendor_id: str,
        adapter_id: str,
        reason: str,
    ) -> Any:
        generation = max((item.generation for item in self.attempts), default=0) + 1
        self.begun.append((vendor_id, generation))
        self.attempts.append(VendorAttempt(vendor_id, generation, "invoking", False))
        return type("Row", (), {"id": generation, "generation": generation})()

    async def complete_vendor_attempt(
        self,
        attempt_id: int,
        *,
        outcome: str,
        safe_to_failover: bool = False,
        vendor_code: int | None = None,
    ) -> bool:
        self.events.append(("attempt", (attempt_id, outcome)))
        return True


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_paused_history_retries_same_vendor_after_resume() -> None:
    store = HistoryStore([VendorAttempt(PRIMARY_VENDOR_ID, 1, "paused", False, 999)])
    gateway = FakeGateway(["task-ok"])
    await SendWorker(gateway, store, FakeBucket()).submit(chunk(), lane="realtime")
    assert store.begun == [(PRIMARY_VENDOR_ID, 2)]
    assert gateway.calls == 1
    assert ("attempt", (2, "submitted")) in store.events


@pytest.mark.asyncio
async def test_retry_task_loads_history_and_allocates_new_generation() -> None:
    store = HistoryStore(
        [VendorAttempt(PRIMARY_VENDOR_ID, 1, "retry_scheduled", False, 5002)]
    )
    gateway = FakeGateway(["task-ok"])
    await SendWorker(gateway, store, FakeBucket()).submit(chunk(), lane="realtime")
    assert store.begun == [(PRIMARY_VENDOR_ID, 2)]
    assert gateway.calls == 1
    assert ("attempt", (2, "submitted")) in store.events


@pytest.mark.asyncio
async def test_invoking_history_does_not_call_vendor() -> None:
    store = HistoryStore([VendorAttempt(PRIMARY_VENDOR_ID, 1, "invoking", False)])
    gateway = FakeGateway(["must-not-send"])
    outcome = await SendWorker(gateway, store, FakeBucket()).submit(
        chunk(), lane="realtime"
    )
    assert outcome.value == "uncertain"
    assert gateway.calls == 0


async def _vendor_health(*pairs: tuple[str, bool]) -> tuple[Any, ...]:
    from app.vendor.routing import VendorHealth

    return tuple(VendorHealth(vendor_id, available) for vendor_id, available in pairs)


@pytest.mark.asyncio
async def test_unregistered_adapter_is_fail_closed_before_http() -> None:
    store = HistoryStore()
    primary = FakeGateway(["must-not-send"])

    async def health() -> tuple[Any, ...]:
        return await _vendor_health((PRIMARY_VENDOR_ID, False), ("secondary", True))

    outcome = await SendWorker(
        primary,
        store,
        FakeBucket(),
        router=VendorRouter((PRIMARY_VENDOR_ID, "secondary")),
        health=health,
        gateways={PRIMARY_VENDOR_ID: primary},
    ).submit(chunk(), lane="realtime")
    assert outcome.value == "failed"
    assert primary.calls == 0
    assert store.begun == []


@pytest.mark.asyncio
async def test_safe_failover_does_not_refund_token_after_invoke() -> None:
    store = HistoryStore()
    primary = FakeGateway([VendorApiError(1002, "format")])
    secondary = FakeGateway(["task-b"])
    bucket = FakeBucket()

    async def health() -> tuple[Any, ...]:
        return await _vendor_health((PRIMARY_VENDOR_ID, True), ("secondary", True))

    await SendWorker(
        primary,
        store,
        bucket,
        router=VendorRouter((PRIMARY_VENDOR_ID, "secondary")),
        health=health,
        gateways={PRIMARY_VENDOR_ID: primary, "secondary": secondary},
    ).submit(chunk(), lane="realtime")
    assert primary.calls == 1
    assert secondary.calls == 1
    assert bucket.refunds == 0
    assert bucket.acquires == 2
