"""Issue #673 共用夹具：两套测试 adapter 与忠实状态机仓储。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.services.vendor_test_budget import SubmissionClaim, SubmissionClaimStatus
from app.tasks.send import FinalizeKind, FinalizeReport, classify_finalize_conflict
from app.vendor.failover import (
    InvokeAuthorization,
    InvokeClaim,
    InvokeClaimKind,
    NextAction,
    already_handled_reason,
    claim_denied_reason,
    historical_safe_reject_repairable,
    next_action_after_safe_reject,
    submitting_timeout_includes,
)
from app.vendor.routing import (
    ROUTE_POLICY_VERSION,
    VendorAttempt,
    VendorRecord,
    default_vendor_registry,
)


def vendor_record(
    vendor_id: str,
    *,
    categories: frozenset[str] | None = None,
    enabled: bool = True,
    adapter: str | None = None,
) -> VendorRecord:
    adapter_id = adapter if adapter is not None else vendor_id
    return VendorRecord(
        vendor_id=vendor_id,
        enabled=enabled,
        categories=categories or frozenset({"verify", "notice", "market"}),
        adapter=adapter_id,
        token_bucket_key="ratelimit:vendor",
        pause_prefix="queue:paused",
        credential_domain=vendor_id,
    )


def two_vendor_registry(
    *,
    secondary_categories: frozenset[str] | None = None,
    secondary_adapter: str | None = "secondary",
    secondary_enabled: bool = True,
) -> tuple[VendorRecord, ...]:
    return (
        vendor_record("zhihui"),
        vendor_record(
            "secondary",
            categories=secondary_categories,
            enabled=secondary_enabled,
            adapter=secondary_adapter,
        ),
    )


class CountingGateway:
    """可编程供应商 Stub；只计数真实 send()。"""

    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0
        self.custom_ids: list[str] = []

    async def send(self, *args: Any, **kwargs: Any) -> str:
        self.calls += 1
        self.custom_ids.append(str(kwargs.get("custom_id") or ""))
        if not self.outcomes:
            raise AssertionError("gateway exhausted")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return str(outcome)


class CountingBucket:
    def __init__(self, *, deny_vendor: str | None = None) -> None:
        self.acquires = 0
        self.refunds = 0
        self.deny_vendor = deny_vendor
        self.acquired_vendors: list[str | None] = []

    async def acquire(self, **kwargs: Any) -> int | None:
        self.acquires += 1
        vendor_id = kwargs.get("vendor_id")
        self.acquired_vendors.append(vendor_id)
        if self.deny_vendor is not None and vendor_id == self.deny_vendor:
            return None
        return 1000 + self.acquires

    async def refund(self, *, lease_epoch: int, **kwargs: Any) -> None:
        self.refunds += 1


@dataclass
class _Attempt:
    id: int
    chunk_id: int
    vendor_id: str
    generation: int
    outcome: str
    safe_to_failover: bool = False
    vendor_code: int | None = None


@dataclass
class _Chunk:
    id: int
    batch_id: int
    status: str
    route_generation: int = 1
    next_vendor: str | None = None
    route_policy_version: int = ROUTE_POLICY_VERSION
    failover_from_attempt_id: int | None = None
    retry_not_before: datetime | None = None
    submitting_since: datetime | None = None
    retry_count: int = 0
    category: str = "notice"


@dataclass
class _Batch:
    id: int
    status: str = "sending"
    category: str = "notice"
    failed: int = 0
    delivered: int = 0
    callback_count: int = 0
    inflight_releases: int = 0
    failed_finalize_count: int = 0
    message_status: str = "pending"


@dataclass
class FaithfulFailoverStore:
    """与 SqlChunkStore 同一套 CAS/后续动作合同的内存状态机。"""

    registry: tuple[VendorRecord, ...] = field(default_factory=default_vendor_registry)
    adapter_ids: frozenset[str] | None = None
    paused: bool = False
    claim_gate: asyncio.Event | None = None
    raise_after_apply: bool = False
    now: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.adapter_ids is None:
            self.adapter_ids = frozenset(
                item.adapter for item in self.registry if item.adapter
            )
        self._lock = asyncio.Lock()
        self.batches: dict[int, _Batch] = {}
        self.chunks: dict[int, _Chunk] = {}
        self.attempts: dict[int, _Attempt] = {}
        self.attempt_seq = 0
        self.outbox: list[tuple[str, int]] = []
        self.events: list[tuple[str, Any]] = []
        self.vendor_calls_allowed = True

    def seed(
        self,
        *,
        chunk_id: int = 3,
        batch_id: int = 2,
        status: str = "pending",
        category: str = "notice",
        batch_status: str = "sending",
    ) -> tuple[int, int]:
        self.batches[batch_id] = _Batch(
            id=batch_id, status=batch_status, category=category
        )
        self.chunks[chunk_id] = _Chunk(
            id=chunk_id,
            batch_id=batch_id,
            status=status,
            category=category,
            submitting_since=self.now if status == "submitting" else None,
        )
        return batch_id, chunk_id

    def _attempts_for(self, chunk_id: int) -> tuple[VendorAttempt, ...]:
        rows = sorted(
            (item for item in self.attempts.values() if item.chunk_id == chunk_id),
            key=lambda item: item.generation,
        )
        return tuple(
            VendorAttempt(
                item.vendor_id,
                item.generation,
                item.outcome,
                item.safe_to_failover,
                item.vendor_code,
            )
            for item in rows
        )

    def _report(self, attempt: _Attempt, chunk: _Chunk, requested: str) -> FinalizeReport:
        kind = classify_finalize_conflict(
            attempt_outcome=attempt.outcome,
            chunk_status=chunk.status,
            requested=requested,
        )
        next_action = None
        next_vendor = None
        if chunk.status == "failed":
            next_action = NextAction.FAILED.value
        elif chunk.status == "failover_pending":
            next_action = NextAction.FAILOVER_PENDING.value
            next_vendor = chunk.next_vendor
        elif chunk.status == "retrying":
            next_action = NextAction.RETRYING.value
        elif chunk.status == "balance_blocked":
            next_action = NextAction.BALANCE_BLOCKED.value
        return FinalizeReport(
            kind,
            requested,
            next_action=next_action,
            next_vendor=next_vendor,
            route_generation=chunk.route_generation,
            previous_attempt_id=chunk.failover_from_attempt_id,
            route_policy_version=chunk.route_policy_version,
            chunk_status=chunk.status,
        )

    async def claim_submission(
        self,
        chunk_id: int,
        expected_retry_count: int,
        segments: int,
        *,
        enforce_live_test_budget: bool,
    ) -> SubmissionClaim:
        chunk = self.chunks[chunk_id]
        batch = self.batches[chunk.batch_id]
        if chunk.status not in {"pending", "retrying"}:
            return SubmissionClaim(SubmissionClaimStatus.STALE)
        if chunk.retry_count != expected_retry_count:
            return SubmissionClaim(SubmissionClaimStatus.STALE)
        if batch.status not in {"queued", "sending"}:
            return SubmissionClaim(SubmissionClaimStatus.STALE)
        chunk.status = "submitting"
        chunk.submitting_since = self.now
        self.events.append(("submitting", chunk_id))
        return SubmissionClaim(SubmissionClaimStatus.CLAIMED)

    async def begin_vendor_invoke(
        self,
        chunk_id: int,
        *,
        vendor_id: str,
        adapter_id: str,
        reason: str,
    ) -> Any:
        async with self._lock:
            chunk = self.chunks[chunk_id]
            if chunk.status in {
                "uncertain",
                "unknown_terminal",
                "inconsistent",
                "failed",
                "submitted",
            }:
                raise RuntimeError("vendor attempt blocked by terminal chunk")
            if chunk.status != "submitting":
                raise RuntimeError("vendor attempt not first-invoke")
            history = [
                item
                for item in self.attempts.values()
                if item.chunk_id == chunk_id
            ]
            if any(
                item.outcome in {"submitted", "uncertain", "invoking", "inconsistent"}
                for item in history
            ):
                raise RuntimeError("vendor attempt already irreversible")
            self.attempt_seq += 1
            generation = max((item.generation for item in history), default=0) + 1
            row = _Attempt(
                self.attempt_seq, chunk_id, vendor_id, generation, "invoking"
            )
            self.attempts[row.id] = row
            chunk.route_generation = generation
            self.events.append(("begin", (vendor_id, generation)))
            return type("Row", (), {"id": row.id, "generation": generation})()

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
        async with self._lock:
            attempt = self.attempts.get(attempt_id)
            chunk = self.chunks.get(chunk_id)
            if attempt is None or chunk is None or attempt.chunk_id != chunk_id:
                return FinalizeReport(FinalizeKind.STATE_CORRUPTION, result)
            if attempt.generation != expected_generation:
                return FinalizeReport(FinalizeKind.STATE_CORRUPTION, result)
            if attempt.outcome != "invoking":
                return self._report(attempt, chunk, result)
            attempt.outcome = result
            attempt.safe_to_failover = safe_to_failover
            attempt.vendor_code = vendor_code
            if result == "submitted":
                if chunk.status != "submitting":
                    return FinalizeReport(FinalizeKind.LOST_CAS, result)
                chunk.status = "submitted"
                self.batches[chunk.batch_id].message_status = "sent"
                report = FinalizeReport(
                    FinalizeKind.APPLIED, result, chunk_status="submitted"
                )
            elif result == "uncertain":
                if chunk.status != "submitting":
                    return FinalizeReport(FinalizeKind.LOST_CAS, result)
                chunk.status = "uncertain"
                report = FinalizeReport(
                    FinalizeKind.APPLIED, result, chunk_status="uncertain"
                )
            elif result in {"retry_scheduled", "delayed", "paused"}:
                if chunk.status != "submitting":
                    return FinalizeReport(FinalizeKind.LOST_CAS, result)
                if result == "paused" and balance_blocked:
                    chunk.status = "retrying"
                    self.batches[chunk.batch_id].status = "balance_blocked"
                    next_action = NextAction.BALANCE_BLOCKED.value
                else:
                    chunk.status = "retrying"
                    next_action = NextAction.RETRYING.value
                report = FinalizeReport(
                    FinalizeKind.APPLIED,
                    result,
                    next_action=next_action,
                    chunk_status=chunk.status,
                )
            elif result in {"rejected", "failed"}:
                batch = self.batches[chunk.batch_id]
                if result == "failed" or not safe_to_failover:
                    persisted = type(
                        "P",
                        (),
                        {
                            "action": NextAction.FAILED,
                            "next_vendor": None,
                            "route_generation": expected_generation,
                            "previous_attempt_id": attempt_id,
                            "route_policy_version": chunk.route_policy_version,
                        },
                    )()
                else:
                    persisted = next_action_after_safe_reject(
                        attempts=self._attempts_for(chunk_id),
                        category=batch.category,
                        previous_attempt_id=attempt_id,
                        records=self.registry,
                        adapter_ids=self.adapter_ids,
                        policy_version=chunk.route_policy_version,
                    )
                if chunk.status != "submitting":
                    return FinalizeReport(FinalizeKind.LOST_CAS, result)
                if persisted.action is NextAction.FAILOVER_PENDING:
                    chunk.status = "failover_pending"
                    chunk.next_vendor = persisted.next_vendor
                    chunk.route_generation = expected_generation
                    chunk.failover_from_attempt_id = attempt_id
                    chunk.submitting_since = None
                    chunk.retry_not_before = self.now
                    self.outbox.append(("chunk.ready", chunk_id))
                    report = FinalizeReport(
                        FinalizeKind.APPLIED,
                        result,
                        next_action=NextAction.FAILOVER_PENDING.value,
                        next_vendor=persisted.next_vendor,
                        route_generation=expected_generation,
                        previous_attempt_id=attempt_id,
                        route_policy_version=chunk.route_policy_version,
                        chunk_status="failover_pending",
                    )
                else:
                    self._finish_failed(chunk, batch)
                    report = FinalizeReport(
                        FinalizeKind.APPLIED,
                        result,
                        next_action=NextAction.FAILED.value,
                        route_generation=expected_generation,
                        previous_attempt_id=attempt_id,
                        route_policy_version=chunk.route_policy_version,
                        chunk_status="failed",
                    )
            else:
                report = FinalizeReport(FinalizeKind.APPLIED, result)
            if self.raise_after_apply:
                self.raise_after_apply = False
                raise RuntimeError("commit result unknown")
            return report

    def _finish_failed(self, chunk: _Chunk, batch: _Batch) -> None:
        chunk.status = "failed"
        chunk.next_vendor = None
        chunk.failover_from_attempt_id = None
        chunk.submitting_since = None
        batch.message_status = "failed"
        batch.failed = 1
        batch.status = "completed"
        batch.failed_finalize_count += 1
        batch.callback_count = 1
        batch.inflight_releases = 1

    async def claim_next_vendor_invoke(
        self,
        chunk_id: int,
        *,
        expected_route_generation: int,
        previous_attempt_id: int,
        expected_next_vendor: str,
        expected_route_policy_version: int,
    ) -> InvokeClaim:
        if self.claim_gate is not None:
            await self.claim_gate.wait()
        async with self._lock:
            chunk = self.chunks.get(chunk_id)
            previous = self.attempts.get(previous_attempt_id)
            if chunk is None:
                return InvokeClaim(InvokeClaimKind.DENIED, reason="chunk_missing")
            batch = self.batches[chunk.batch_id]
            history = [
                item
                for item in self.attempts.values()
                if item.chunk_id == chunk_id
            ]
            invoking = next(
                (item for item in history if item.outcome == "invoking"), None
            )
            handled = already_handled_reason(
                chunk_status=chunk.status,
                invoking_vendor=invoking.vendor_id if invoking else None,
                expected_next_vendor=expected_next_vendor,
                invoking_generation=invoking.generation if invoking else None,
                expected_route_generation=expected_route_generation,
            )
            if handled is not None and invoking is not None:
                return InvokeClaim(
                    InvokeClaimKind.ALREADY_HANDLED,
                    authorization=InvokeAuthorization(
                        invoking.id,
                        invoking.generation,
                        invoking.vendor_id,
                        invoking.vendor_id,
                        expected_route_policy_version,
                    ),
                    reason=handled,
                )
            previous_attempt = None
            if previous is not None:
                previous_attempt = VendorAttempt(
                    previous.vendor_id,
                    previous.generation,
                    previous.outcome,
                    previous.safe_to_failover,
                    previous.vendor_code,
                )
            retry_due = (
                chunk.retry_not_before is None or chunk.retry_not_before <= self.now
            )
            denied = claim_denied_reason(
                chunk_status=chunk.status,
                batch_status=batch.status,
                expected_route_generation=expected_route_generation,
                actual_route_generation=chunk.route_generation,
                previous_attempt=previous_attempt,
                previous_attempt_chunk_id=(
                    previous.chunk_id if previous is not None else None
                ),
                chunk_id=chunk_id,
                expected_next_vendor=expected_next_vendor,
                persisted_next_vendor=chunk.next_vendor,
                expected_route_policy_version=expected_route_policy_version,
                actual_route_policy_version=chunk.route_policy_version,
                retry_due=retry_due,
                blocking_outcomes=frozenset(item.outcome for item in history),
                category=batch.category,
                records=self.registry,
                adapter_ids=self.adapter_ids,
            )
            if denied is not None:
                return InvokeClaim(InvokeClaimKind.DENIED, reason=denied)
            self.attempt_seq += 1
            generation = expected_route_generation + 1
            row = _Attempt(
                self.attempt_seq, chunk_id, expected_next_vendor, generation, "invoking"
            )
            self.attempts[row.id] = row
            chunk.status = "submitting"
            chunk.route_generation = generation
            chunk.submitting_since = self.now
            self.events.append(("claim_next", (expected_next_vendor, generation)))
            return InvokeClaim(
                InvokeClaimKind.AUTHORIZED,
                authorization=InvokeAuthorization(
                    row.id,
                    generation,
                    expected_next_vendor,
                    expected_next_vendor,
                    expected_route_policy_version,
                ),
                reason="authorized",
            )

    async def load_authoritative_next_action(
        self,
        chunk_id: int,
        *,
        attempt_id: int,
        expected_generation: int,
    ) -> FinalizeReport | None:
        attempt = self.attempts.get(attempt_id)
        chunk = self.chunks.get(chunk_id)
        if attempt is None or chunk is None:
            return None
        if attempt.chunk_id != chunk_id or attempt.generation != expected_generation:
            return None
        return self._report(attempt, chunk, attempt.outcome)

    async def list_vendor_attempts(self, chunk_id: int) -> tuple[VendorAttempt, ...]:
        return self._attempts_for(chunk_id)

    async def mark_failed(self, chunk_id: int, code: int, message: str) -> None:
        chunk = self.chunks[chunk_id]
        self._finish_failed(chunk, self.batches[chunk.batch_id])
        self.events.append(("failed", (chunk_id, code)))

    async def mark_uncertain(self, chunk_id: int) -> None:
        self.chunks[chunk_id].status = "uncertain"
        self.events.append(("uncertain", chunk_id))

    async def mark_submitted(self, chunk_id: int, task_id: str) -> None:
        self.chunks[chunk_id].status = "submitted"
        self.events.append(("submitted", (chunk_id, task_id)))

    async def schedule_retry(
        self, chunk_id: int, code: int, expected_retry_count: int, delay_s: int
    ) -> bool:
        self.chunks[chunk_id].status = "retrying"
        return True

    async def delay(self, chunk_id: int, code: int, delay_s: int) -> None:
        self.chunks[chunk_id].status = "retrying"

    async def balance_blocked(self, batch_id: int, chunk_id: int) -> None:
        self.chunks[chunk_id].status = "retrying"
        self.batches[batch_id].status = "balance_blocked"

    async def pause_blocked(self, chunk_id: int, code: int) -> None:
        self.chunks[chunk_id].status = "retrying"

    async def pause_queues(self, code: int) -> None:
        self.paused = True
        self.events.append(("pause", code))

    async def split_once(self, chunk: Any) -> list[Any]:
        return []

    async def reject_disallowed_recipient(self, chunk_id: int, denied_count: int) -> None:
        return None

    async def defer_daily_limit(self, chunk_id: int, lane: str, reset_at: datetime) -> None:
        return None

    async def pause_daily_limit(self, lane: str, reset_at: datetime) -> None:
        return None

    async def pause_control_agent_stale(self) -> None:
        return None

    async def release_control_claim(self, chunk_id: int) -> None:
        return None

    async def release_unsent(self, chunk_id: int) -> None:
        return None

    async def is_paused(self, lane: str) -> bool:
        return self.paused

    def reconcile_stalled(self, *, submitting_timeout: bool = True) -> list[int]:
        """镜像恢复器：修复历史组合，failover_pending 不转 uncertain。"""

        recovered: list[int] = []
        for chunk in list(self.chunks.values()):
            attempts = self._attempts_for(chunk.id)
            if historical_safe_reject_repairable(
                chunk_status=chunk.status, attempts=attempts
            ):
                last = [
                    item
                    for item in self.attempts.values()
                    if item.chunk_id == chunk.id
                ][-1]
                persisted = next_action_after_safe_reject(
                    attempts=attempts,
                    category=self.batches[chunk.batch_id].category,
                    previous_attempt_id=last.id,
                    records=self.registry,
                    adapter_ids=self.adapter_ids,
                    policy_version=chunk.route_policy_version,
                )
                if persisted.action is NextAction.FAILOVER_PENDING:
                    chunk.status = "failover_pending"
                    chunk.next_vendor = persisted.next_vendor
                    chunk.failover_from_attempt_id = last.id
                    chunk.submitting_since = None
                    self.outbox.append(("chunk.ready", chunk.id))
                else:
                    self._finish_failed(chunk, self.batches[chunk.batch_id])
                recovered.append(chunk.id)
                continue
            if (
                submitting_timeout
                and submitting_timeout_includes(chunk.status)
                and chunk.submitting_since is not None
                and chunk.submitting_since <= self.now - timedelta(minutes=5)
            ):
                last = attempts[-1] if attempts else None
                leftover = (
                    last is not None
                    and last.outcome == "rejected"
                    and last.safe_to_failover
                    and not any(
                        item.outcome
                        in {"submitted", "uncertain", "invoking", "inconsistent"}
                        for item in attempts
                    )
                )
                if leftover:
                    continue
                chunk.status = "uncertain"
                if last is not None and last.outcome == "invoking":
                    for item in self.attempts.values():
                        if item.chunk_id == chunk.id and item.outcome == "invoking":
                            item.outcome = "uncertain"
                recovered.append(chunk.id)
        return recovered

    def recovery_work(self) -> list[int]:
        """只重调度同一 generation 的 pending 动作，不调用供应商。"""

        due = []
        for chunk in self.chunks.values():
            if chunk.status == "failover_pending" and (
                chunk.retry_not_before is None or chunk.retry_not_before <= self.now
            ):
                due.append(chunk.id)
                self.outbox.append(("chunk.ready", chunk.id))
        return due
