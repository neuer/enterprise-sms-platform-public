"""API-SEND-R5-01：safe reject 后续动作与调用前 fencing。"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from app.tasks.send import (
    ChunkPayload,
    FinalizeKind,
    SendWorker,
    SubmitOutcome,
    followup_allowed,
)
from app.vendor.codes import policy_for
from app.vendor.failover import (
    InvokeClaimKind,
    NextAction,
    followup_for_code,
    submitting_timeout_includes,
)
from app.vendor.routing import PRIMARY_VENDOR_ID, VendorRouter, default_vendor_registry
from app.vendor.zhihui import VendorApiError
from tests.vendor_failover_r5_support import (
    CountingBucket,
    CountingGateway,
    FaithfulFailoverStore,
    two_vendor_registry,
)


def _payload(
    store: FaithfulFailoverStore,
    chunk_id: int = 3,
    *,
    status: str | None = None,
) -> ChunkPayload:
    chunk = store.chunks[chunk_id]
    return ChunkPayload(
        chunk_id=chunk.id,
        batch_id=chunk.batch_id,
        custom_id="a" * 32,
        phones=("13800138000",),
        content="通知内容",
        template_id="",
        sign_name="【青鸾】",
        status=status or chunk.status,
        category=chunk.category,
        next_vendor=chunk.next_vendor,
        route_generation=chunk.route_generation,
        route_policy_version=chunk.route_policy_version,
        failover_from_attempt_id=chunk.failover_from_attempt_id,
    )


def _worker(
    store: FaithfulFailoverStore,
    primary: CountingGateway,
    secondary: CountingGateway | None = None,
    *,
    bucket: CountingBucket | None = None,
    sleeper=None,
) -> SendWorker:
    gateways = {PRIMARY_VENDOR_ID: primary}
    registered = (PRIMARY_VENDOR_ID,)
    if secondary is not None:
        gateways["secondary"] = secondary
        registered = (PRIMARY_VENDOR_ID, "secondary")
    kwargs: dict = {}
    if sleeper is not None:
        kwargs["sleeper"] = sleeper
    return SendWorker(
        primary,
        store,
        bucket or CountingBucket(),
        gateways=gateways,
        router=VendorRouter(registered),
        registry=store.registry,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_single_vendor_safe_reject_finishes_chunk_and_batch() -> None:
    store = FaithfulFailoverStore(registry=default_vendor_registry())
    store.seed(status="pending")
    primary = CountingGateway([VendorApiError(1002, "bad content")])
    outcome = await _worker(store, primary).submit(_payload(store), lane="realtime")
    assert outcome is SubmitOutcome.FAILED
    chunk = store.chunks[3]
    batch = store.batches[2]
    assert chunk.status == "failed"
    assert batch.message_status == "failed"
    assert batch.status == "completed"
    assert [item.outcome for item in store.attempts.values()] == ["rejected"]
    assert chunk.status != "uncertain"
    assert primary.calls == 1


@pytest.mark.asyncio
async def test_no_next_vendor_never_leaves_rejected_submitting() -> None:
    store = FaithfulFailoverStore(registry=default_vendor_registry())
    store.seed(status="submitting")
    begun = await store.begin_vendor_invoke(
        3, vendor_id="zhihui", adapter_id="zhihui", reason="primary"
    )
    report = await store.finalize_vendor_attempt(
        begun.id,
        3,
        expected_generation=begun.generation,
        result="rejected",
        vendor_code=1001,
        safe_to_failover=True,
    )
    assert report.kind is FinalizeKind.APPLIED
    assert report.next_action == NextAction.FAILED.value
    assert store.chunks[3].status == "failed"
    assert store.attempts[begun.id].outcome == "rejected"
    assert store.chunks[3].status != "submitting"
    with pytest.raises(RuntimeError, match="terminal|first-invoke"):
        await store.begin_vendor_invoke(
            3, vendor_id="secondary", adapter_id="secondary", reason="stale"
        )
    store.seed(chunk_id=8, batch_id=9, status="submitting")
    store.attempt_seq += 1
    from tests.vendor_failover_r5_support import _Attempt

    store.attempts[store.attempt_seq] = _Attempt(
        store.attempt_seq, 8, "zhihui", 1, "rejected", True, 1002
    )
    store.reconcile_stalled()
    assert store.chunks[8].status == "failed"
    assert store.batches[9].status == "completed"


@pytest.mark.asyncio
async def test_safe_reject_persists_one_failover_action() -> None:
    store = FaithfulFailoverStore(registry=two_vendor_registry())
    store.seed(status="submitting")
    begun = await store.begin_vendor_invoke(
        3, vendor_id="zhihui", adapter_id="zhihui", reason="primary"
    )
    first = await store.finalize_vendor_attempt(
        begun.id,
        3,
        expected_generation=begun.generation,
        result="rejected",
        vendor_code=1002,
        safe_to_failover=True,
    )
    second = await store.finalize_vendor_attempt(
        begun.id,
        3,
        expected_generation=begun.generation,
        result="rejected",
        vendor_code=1002,
        safe_to_failover=True,
    )
    assert first.kind is FinalizeKind.APPLIED
    assert first.next_action == NextAction.FAILOVER_PENDING.value
    assert first.next_vendor == "secondary"
    assert store.chunks[3].status == "failover_pending"
    assert store.chunks[3].next_vendor == "secondary"
    assert store.outbox == [("chunk.ready", 3)]
    assert second.kind is FinalizeKind.ALREADY_FINALIZED_SAME_RESULT
    assert followup_allowed(second)
    assert store.outbox == [("chunk.ready", 3)]


@pytest.mark.asyncio
async def test_token_unavailable_keeps_failover_pending() -> None:
    store = FaithfulFailoverStore(registry=two_vendor_registry())
    store.seed(status="pending")
    primary = CountingGateway([VendorApiError(1003, "template")])
    secondary = CountingGateway(["must-not-send"])
    bucket = CountingBucket(deny_vendor="secondary")

    async def sleeper(_: float) -> None:
        store.paused = True

    outcome = await _worker(
        store, primary, secondary, bucket=bucket, sleeper=sleeper
    ).submit(_payload(store), lane="realtime")
    assert outcome is SubmitOutcome.PAUSED
    assert store.chunks[3].status == "failover_pending"
    assert store.chunks[3].next_vendor == "secondary"
    assert secondary.calls == 0
    assert bucket.refunds == 0


@pytest.mark.asyncio
async def test_stale_worker_cannot_invoke_after_chunk_uncertain() -> None:
    store = FaithfulFailoverStore(registry=two_vendor_registry())
    store.seed(status="pending")
    primary = CountingGateway([VendorApiError(1002, "bad content")])
    secondary = CountingGateway(["must-not-send"])
    gate = asyncio.Event()
    store.claim_gate = gate
    worker = _worker(store, primary, secondary)
    task = asyncio.create_task(worker.submit(_payload(store), lane="realtime"))
    for _ in range(80):
        if store.chunks[3].status == "failover_pending":
            break
        await asyncio.sleep(0.01)
    assert store.chunks[3].status == "failover_pending"
    store.chunks[3].status = "uncertain"
    gate.set()
    outcome = await task
    assert outcome is SubmitOutcome.UNCERTAIN
    assert secondary.calls == 0
    assert not any(
        item.vendor_id == "secondary" and item.outcome == "invoking"
        for item in store.attempts.values()
    )


@pytest.mark.asyncio
async def test_finalize_conflict_blocks_all_followup_vendor_calls() -> None:
    store = FaithfulFailoverStore(registry=two_vendor_registry())
    store.seed(status="submitting")
    begun = await store.begin_vendor_invoke(
        3, vendor_id="zhihui", adapter_id="zhihui", reason="primary"
    )
    store.chunks[3].status = "uncertain"
    store.attempts[begun.id].outcome = "uncertain"
    secondary = CountingGateway(["must-not-send"])
    report = await store.finalize_vendor_attempt(
        begun.id,
        3,
        expected_generation=begun.generation,
        result="rejected",
        vendor_code=1002,
        safe_to_failover=True,
    )
    assert report.kind is FinalizeKind.RECOVERY_MARKED_UNCERTAIN
    assert not followup_allowed(report)
    claim = await store.claim_next_vendor_invoke(
        3,
        expected_route_generation=begun.generation,
        previous_attempt_id=begun.id,
        expected_next_vendor="secondary",
        expected_route_policy_version=1,
    )
    assert claim.kind is InvokeClaimKind.DENIED
    assert secondary.calls == 0


@pytest.mark.asyncio
async def test_duplicate_failover_delivery_creates_one_next_attempt() -> None:
    store = FaithfulFailoverStore(registry=two_vendor_registry())
    store.seed(status="submitting")
    begun = await store.begin_vendor_invoke(
        3, vendor_id="zhihui", adapter_id="zhihui", reason="primary"
    )
    report = await store.finalize_vendor_attempt(
        begun.id,
        3,
        expected_generation=begun.generation,
        result="rejected",
        vendor_code=1002,
        safe_to_failover=True,
    )
    first, second = await asyncio.gather(
        store.claim_next_vendor_invoke(
            3,
            expected_route_generation=int(report.route_generation or 1),
            previous_attempt_id=int(report.previous_attempt_id or begun.id),
            expected_next_vendor="secondary",
            expected_route_policy_version=1,
        ),
        store.claim_next_vendor_invoke(
            3,
            expected_route_generation=int(report.route_generation or 1),
            previous_attempt_id=int(report.previous_attempt_id or begun.id),
            expected_next_vendor="secondary",
            expected_route_policy_version=1,
        ),
    )
    kinds = {first.kind, second.kind}
    assert InvokeClaimKind.AUTHORIZED in kinds
    assert kinds - {InvokeClaimKind.AUTHORIZED} <= {
        InvokeClaimKind.ALREADY_HANDLED,
        InvokeClaimKind.DENIED,
    }
    invoking = [
        item
        for item in store.attempts.values()
        if item.outcome == "invoking" and item.vendor_id == "secondary"
    ]
    assert len(invoking) == 1


@pytest.mark.asyncio
async def test_commit_result_unknown_reloads_authoritative_next_action() -> None:
    store = FaithfulFailoverStore(registry=two_vendor_registry())
    store.seed(status="pending")
    store.raise_after_apply = True
    primary = CountingGateway([VendorApiError(1002, "bad content")])
    secondary = CountingGateway(["task-b"])
    outcome = await _worker(store, primary, secondary).submit(
        _payload(store), lane="realtime"
    )
    assert outcome is SubmitOutcome.SUBMITTED
    assert primary.calls == 1
    assert secondary.calls == 1
    rejected = [item for item in store.attempts.values() if item.outcome == "rejected"]
    submitted = [
        item for item in store.attempts.values() if item.outcome == "submitted"
    ]
    assert len(rejected) == 1
    assert len(submitted) == 1
    assert submitted[0].vendor_id == "secondary"


@pytest.mark.asyncio
async def test_failover_pending_is_excluded_from_submitting_timeout() -> None:
    store = FaithfulFailoverStore(registry=two_vendor_registry())
    store.seed(status="submitting")
    begun = await store.begin_vendor_invoke(
        3, vendor_id="zhihui", adapter_id="zhihui", reason="primary"
    )
    await store.finalize_vendor_attempt(
        begun.id,
        3,
        expected_generation=begun.generation,
        result="rejected",
        vendor_code=1002,
        safe_to_failover=True,
    )
    store.chunks[3].submitting_since = store.now - timedelta(minutes=30)
    store.reconcile_stalled()
    assert store.chunks[3].status == "failover_pending"
    assert submitting_timeout_includes("failover_pending") is False
    assert store.recovery_work() == [3]
    assert not any(item.vendor_id == "secondary" for item in store.attempts.values())


@pytest.mark.asyncio
async def test_next_vendor_requires_category_and_adapter_match() -> None:
    store = FaithfulFailoverStore(
        registry=two_vendor_registry(secondary_categories=frozenset({"notice"}))
    )
    store.seed(status="submitting", category="market")
    begun = await store.begin_vendor_invoke(
        3, vendor_id="zhihui", adapter_id="zhihui", reason="primary"
    )
    report = await store.finalize_vendor_attempt(
        begun.id,
        3,
        expected_generation=begun.generation,
        result="rejected",
        vendor_code=1002,
        safe_to_failover=True,
    )
    assert report.next_action == NextAction.FAILED.value
    assert store.chunks[3].status == "failed"

    store = FaithfulFailoverStore(
        registry=two_vendor_registry(secondary_adapter=""),
        adapter_ids=frozenset({"zhihui"}),
    )
    store.seed(status="submitting")
    begun = await store.begin_vendor_invoke(
        3, vendor_id="zhihui", adapter_id="zhihui", reason="primary"
    )
    report = await store.finalize_vendor_attempt(
        begun.id,
        3,
        expected_generation=begun.generation,
        result="rejected",
        vendor_code=1002,
        safe_to_failover=True,
    )
    assert store.chunks[3].status == "failed"
    claim = await store.claim_next_vendor_invoke(
        3,
        expected_route_generation=1,
        previous_attempt_id=begun.id,
        expected_next_vendor="secondary",
        expected_route_policy_version=1,
    )
    assert claim.kind is InvokeClaimKind.DENIED


@pytest.mark.asyncio
async def test_hold_like_codes_preserve_explicit_pause_policy() -> None:
    assert followup_for_code(999) == "hold"
    assert followup_for_code(1000) == "hold"
    assert followup_for_code(1002) == "failover_or_fail"
    assert policy_for(999).safe_to_failover is True
    store = FaithfulFailoverStore(registry=two_vendor_registry())
    store.seed(status="pending")
    primary = CountingGateway([VendorApiError(999, "no balance")])
    secondary = CountingGateway(["must-not-send"])
    outcome = await _worker(store, primary, secondary).submit(
        _payload(store), lane="realtime"
    )
    assert outcome is SubmitOutcome.PAUSED
    assert store.chunks[3].status == "retrying"
    assert store.batches[2].status == "balance_blocked"
    assert store.chunks[3].status != "failover_pending"
    assert secondary.calls == 0


@pytest.mark.asyncio
async def test_failure_completion_keeps_usage_callback_inflight_idempotent() -> None:
    store = FaithfulFailoverStore(registry=default_vendor_registry())
    store.seed(status="submitting")
    begun = await store.begin_vendor_invoke(
        3, vendor_id="zhihui", adapter_id="zhihui", reason="primary"
    )
    first = await store.finalize_vendor_attempt(
        begun.id,
        3,
        expected_generation=begun.generation,
        result="rejected",
        vendor_code=1002,
        safe_to_failover=True,
    )
    second = await store.finalize_vendor_attempt(
        begun.id,
        3,
        expected_generation=begun.generation,
        result="rejected",
        vendor_code=1002,
        safe_to_failover=True,
    )
    batch = store.batches[2]
    assert first.next_action == NextAction.FAILED.value
    assert second.kind is FinalizeKind.ALREADY_FINALIZED_SAME_RESULT
    assert batch.failed_finalize_count == 1
    assert batch.callback_count == 1
    assert batch.inflight_releases == 1
    assert batch.message_status == "failed"
    assert batch.status == "completed"
    third = await store.finalize_vendor_attempt(
        begun.id,
        3,
        expected_generation=begun.generation,
        result="rejected",
        vendor_code=1002,
        safe_to_failover=True,
    )
    assert third.kind is FinalizeKind.ALREADY_FINALIZED_SAME_RESULT
    assert batch.failed_finalize_count == 1
    assert batch.callback_count == 1
    assert batch.inflight_releases == 1
