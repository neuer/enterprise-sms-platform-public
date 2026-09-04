"""submit() 结果枚举与批次任务返回值不得把处理数叫成 submitted。"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_send_worker import (  # noqa: E402
    AllowGuard,
    FakeBucket,
    FakeGateway,
    FakeStore,
    chunk,
)

from app.services.vendor_test_budget import SubmissionClaim, SubmissionClaimStatus
from app.tasks.send import (
    ChunkTaskResult,
    SendWorker,
    SubmitOutcome,
    batch_plan_metrics,
    chunk_result_metrics,
    process_batch,
)
from app.vendor.codes import SAFE_TO_FAILOVER_CODES, policy_for
from app.vendor.routing import PRIMARY_VENDOR_ID, VendorHealth, VendorRouter
from app.vendor.zhihui import VendorApiError, VendorTransportError


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        ("submitted", SubmitOutcome.SUBMITTED),
        ("paused", SubmitOutcome.PAUSED),
        ("rejected", SubmitOutcome.REJECTED),
        ("stale", SubmitOutcome.STALE),
        ("delayed", SubmitOutcome.DELAYED),
        ("retry", SubmitOutcome.RETRY_SCHEDULED),
        ("failed", SubmitOutcome.FAILED),
        ("uncertain", SubmitOutcome.UNCERTAIN),
        ("split", SubmitOutcome.SPLIT),
    ],
)
async def test_submit_outcome_covers_every_branch(
    setup: str,
    expected: SubmitOutcome,
) -> None:
    store = FakeStore()
    gateway: FakeGateway
    worker_kwargs: dict[str, Any] = {}
    payload = chunk()
    if setup == "submitted":
        gateway = FakeGateway(["task-ok"])
    elif setup == "paused":
        store.paused = True
        gateway = FakeGateway(["must-not-send"])
    elif setup == "rejected":
        gateway = FakeGateway(["must-not-send"])
        worker_kwargs["recipient_guard"] = AllowGuard(1)
        worker_kwargs["enforce_live_test_budget"] = True
    elif setup == "stale":
        store.claimed = False
        gateway = FakeGateway(["must-not-send"])
    elif setup == "delayed":
        store.claim_result = SubmissionClaim(
            SubmissionClaimStatus.DAILY_LIMIT,
            datetime(2026, 9, 5, tzinfo=UTC),
        )
        gateway = FakeGateway(["must-not-send"])
        worker_kwargs["recipient_guard"] = AllowGuard()
        worker_kwargs["enforce_live_test_budget"] = True
    elif setup == "retry":
        gateway = FakeGateway([VendorApiError(5002, "fast")])
    elif setup == "failed":
        gateway = FakeGateway([VendorApiError(9, "fail")])
    elif setup == "uncertain":
        gateway = FakeGateway([VendorTransportError("timeout")])
    else:
        from app.tasks.send import ChunkPayload

        payload = ChunkPayload(
            3, 2, "a" * 32, ("13800138000", "13900139000"), "通知", "", ""
        )
        store.split_chunks = [
            ChunkPayload(4, 2, "a" * 24 + "00000002", ("13800138000",), "通知", "", ""),
            ChunkPayload(5, 2, "a" * 24 + "00000003", ("13900139000",), "通知", "", ""),
        ]
        gateway = FakeGateway([VendorApiError(1006, "too many"), "task-4", "task-5"])

    outcome = await SendWorker(gateway, store, FakeBucket(), **worker_kwargs).submit(
        payload,
        lane="realtime",
    )
    assert outcome is expected


def test_batch_task_metrics_never_count_planned_as_submitted() -> None:
    payload = batch_plan_metrics(4)
    assert payload["planned_chunks"] == 4
    assert payload["submitted"] == 0
    assert payload["processed_chunks"] == 0
    assert "submitted" in payload


def test_chunk_metrics_count_submitted_only_after_mark_submitted() -> None:
    submitted = chunk_result_metrics(ChunkTaskResult(1, SubmitOutcome.SUBMITTED))
    paused = chunk_result_metrics(ChunkTaskResult(1, SubmitOutcome.PAUSED))
    assert submitted["submitted"] == 1
    assert submitted["processed_chunks"] == 1
    assert paused["submitted"] == 0
    assert paused["paused"] == 1


def test_process_batch_celery_wrapper_returns_named_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(coro: object) -> int:
        close = getattr(coro, "close", None)
        if close is not None:
            close()
        return 3

    monkeypatch.setattr("app.tasks.send.run_worker_async", fake_run)
    payload = process_batch.run("batch-1")
    assert payload["planned_chunks"] == 3
    assert payload["submitted"] == 0


def test_safe_to_failover_excludes_timeout_and_backoff_codes() -> None:
    assert 1002 in SAFE_TO_FAILOVER_CODES
    assert 999 in SAFE_TO_FAILOVER_CODES
    assert policy_for(1002).safe_to_failover is True
    assert policy_for(429).safe_to_failover is False
    assert policy_for(5002).safe_to_failover is False
    assert policy_for(5001).safe_to_failover is False
    assert policy_for(987654).safe_to_failover is False


@pytest.mark.asyncio
async def test_pre_invoke_unavailability_uses_secondary_without_calling_primary() -> None:
    primary = FakeGateway(["must-not-send"])
    secondary = FakeGateway(["task-b"])
    store = FakeStore()
    outcome = await SendWorker(
        primary,
        store,
        FakeBucket(),
        gateways={PRIMARY_VENDOR_ID: primary, "secondary": secondary},
        router=VendorRouter((PRIMARY_VENDOR_ID, "secondary")),
        health=lambda: _health(False, True),
    ).submit(chunk(), lane="realtime")
    assert outcome is SubmitOutcome.SUBMITTED
    assert primary.calls == 0
    assert secondary.calls == 1
    assert ("submitted", (3, "task-b")) in store.events


@pytest.mark.asyncio
async def test_safe_reject_failsover_to_secondary() -> None:
    primary = FakeGateway([VendorApiError(1002, "bad content")])
    secondary = FakeGateway(["task-b"])
    store = FakeStore()
    outcome = await SendWorker(
        primary,
        store,
        FakeBucket(),
        gateways={PRIMARY_VENDOR_ID: primary, "secondary": secondary},
        router=VendorRouter((PRIMARY_VENDOR_ID, "secondary")),
        health=lambda: _health(True, True),
    ).submit(chunk(), lane="realtime")
    assert outcome is SubmitOutcome.SUBMITTED
    assert primary.calls == 1
    assert secondary.calls == 1
    assert ("failed", (3, 1002)) not in store.events
    assert ("submitted", (3, "task-b")) in store.events


@pytest.mark.asyncio
async def test_timeout_never_calls_secondary() -> None:
    primary = FakeGateway([VendorTransportError("timeout")])
    secondary = FakeGateway(["must-not-send"])
    store = FakeStore()
    outcome = await SendWorker(
        primary,
        store,
        FakeBucket(),
        gateways={PRIMARY_VENDOR_ID: primary, "secondary": secondary},
        router=VendorRouter((PRIMARY_VENDOR_ID, "secondary")),
        health=lambda: _health(True, True),
    ).submit(chunk(), lane="realtime")
    assert outcome is SubmitOutcome.UNCERTAIN
    assert primary.calls == 1
    assert secondary.calls == 0
    assert ("uncertain", 3) in store.events


async def _health(primary_up: bool, secondary_up: bool) -> tuple[VendorHealth, ...]:
    return (
        VendorHealth(PRIMARY_VENDOR_ID, primary_up),
        VendorHealth("secondary", secondary_up),
    )
