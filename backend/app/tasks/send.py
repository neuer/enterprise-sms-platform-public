"""realtime/bulk 共用的厂商提交状态机。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Protocol
from uuid import UUID

from app.core.ratelimit import TokenBucket
from app.core.runtime_resources import redis_client
from app.core.worker_runtime import run_worker_async
from app.services.alert_repository import SqlAlertService
from app.services.billing import calculate_quota_cost
from app.services.crypto import CryptoService
from app.services.outbox import OutboxClaim, OutboxExecutor
from app.services.outbox_repository import SqlOutboxRepository
from app.services.vendor_alert import RedisVendorAlertMonitor
from app.services.vendor_control_state import (
    VendorControlStateGuard,
    VendorControlStateUnavailable,
)
from app.services.vendor_test_budget import SubmissionClaim, SubmissionClaimStatus
from app.services.vendor_test_guard import VendorTestRecipientDenied
from app.settings import get_settings
from app.tasks import celery_app
from app.vendor.routing import (
    PRIMARY_VENDOR_ID,
    RouteRequest,
    VendorAttempt,
    VendorHealth,
    VendorRouter,
)
from app.vendor.zhihui import (
    VendorApiError,
    VendorProtocolError,
    VendorTransportError,
    ZhihuiClient,
)

LOGGER = logging.getLogger(__name__)


class SendQueuePaused(RuntimeError):
    """发送队列已暂停；child Outbox 必须失败重试，不得 complete。"""


class SubmitOutcome(StrEnum):
    """单次 submit() 的诊断结果；权威业务量仍以 PostgreSQL 状态为准。"""

    SUBMITTED = "submitted"
    RETRY_SCHEDULED = "retry_scheduled"
    DELAYED = "delayed"
    PAUSED = "paused"
    REJECTED = "rejected"
    STALE = "stale"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    SPLIT = "split"
    NO_OP_ALREADY_TERMINAL = "no_op_already_terminal"


class FinalizeKind(StrEnum):
    """供应商原子终结的 CAS 分类；调用方不得忽略。"""

    APPLIED = "applied"
    ALREADY_FINALIZED_SAME_RESULT = "already_finalized_same_result"
    FINALIZED_DIFFERENT_RESULT = "finalized_different_result"
    RECOVERY_MARKED_UNCERTAIN = "recovery_marked_uncertain"
    STATE_CORRUPTION = "state_corruption"
    LOST_CAS = "lost_cas"


@dataclass(frozen=True, slots=True)
class FinalizeReport:
    """finalize_vendor_attempt 的权威结果。"""

    kind: FinalizeKind
    result: str


SUBMIT_OUTCOME_LABELS = tuple(item.value for item in SubmitOutcome)


def classify_finalize_conflict(
    *,
    attempt_outcome: str,
    chunk_status: str,
    requested: str,
) -> FinalizeKind:
    """CAS 未更新 invoking 时，按权威组合分类，禁止把不同结果报成成功。"""

    if attempt_outcome == requested:
        if requested == "submitted" and chunk_status == "submitted":
            return FinalizeKind.ALREADY_FINALIZED_SAME_RESULT
        if requested == "uncertain" and chunk_status == "uncertain":
            return FinalizeKind.ALREADY_FINALIZED_SAME_RESULT
        if requested in {"retry_scheduled", "delayed", "paused"} and chunk_status == "retrying":
            return FinalizeKind.ALREADY_FINALIZED_SAME_RESULT
        if requested in {"rejected", "failed"} and chunk_status == "failed":
            return FinalizeKind.ALREADY_FINALIZED_SAME_RESULT
        if requested == "rejected" and chunk_status == "submitting":
            return FinalizeKind.ALREADY_FINALIZED_SAME_RESULT
        return FinalizeKind.STATE_CORRUPTION
    if attempt_outcome == "uncertain" or chunk_status == "uncertain":
        return FinalizeKind.RECOVERY_MARKED_UNCERTAIN
    if requested == "submitted" and chunk_status == "submitted":
        return FinalizeKind.STATE_CORRUPTION
    if attempt_outcome in {"submitted", "uncertain", "failed", "inconsistent"}:
        return FinalizeKind.FINALIZED_DIFFERENT_RESULT
    return FinalizeKind.STATE_CORRUPTION


def submit_outcome_from_finalize(
    report: FinalizeReport,
    requested: SubmitOutcome,
) -> SubmitOutcome:
    """CAS 失败时不得报告 SUBMITTED。"""

    if report.kind in {
        FinalizeKind.APPLIED,
        FinalizeKind.ALREADY_FINALIZED_SAME_RESULT,
    }:
        return requested
    if report.kind is FinalizeKind.RECOVERY_MARKED_UNCERTAIN:
        return SubmitOutcome.UNCERTAIN
    if requested is SubmitOutcome.RETRY_SCHEDULED:
        return SubmitOutcome.STALE
    return SubmitOutcome.UNCERTAIN


@dataclass(frozen=True, slots=True)
class ChunkTaskResult:
    """chunk 任务诊断；processed 只表示 Outbox 是否完成这次执行。"""

    processed: int
    outcome: SubmitOutcome | None


def chunk_result_metrics(result: ChunkTaskResult) -> dict[str, int]:
    """Celery 诊断计数；submitted 仅在 mark_submitted 完成后为 1。"""

    payload = {label: 0 for label in SUBMIT_OUTCOME_LABELS}
    payload["processed_chunks"] = result.processed
    payload["planned_chunks"] = 0
    payload["submitted"] = 1 if result.outcome is SubmitOutcome.SUBMITTED else 0
    if result.outcome is not None:
        payload[result.outcome.value] = 1
    return payload


def batch_plan_metrics(planned_chunks: int) -> dict[str, int]:
    """批次任务只报告规划数，不得把规划数叫成 submitted。"""

    payload = {label: 0 for label in SUBMIT_OUTCOME_LABELS}
    payload["processed_chunks"] = 0
    payload["planned_chunks"] = planned_chunks
    payload["submitted"] = 0
    return payload


@dataclass(frozen=True, slots=True)
class ChunkPayload:
    """仅存在于 worker 内存的受控明文提交载荷。"""

    chunk_id: int
    batch_id: int
    custom_id: str
    phones: tuple[str, ...]
    content: str
    template_id: str
    sign_name: str
    retry_count: int = 0
    denied_recipient_count: int = 0
    selected_vendor: str = PRIMARY_VENDOR_ID
    route_generation: int = 1


class Gateway(Protocol):
    async def send(
        self,
        mobiles: tuple[str, ...],
        content: str,
        *,
        template_id: str,
        sign_name: str,
        custom_id: str,
    ) -> str: ...


class Bucket(Protocol):
    async def acquire(
        self,
        *,
        lane: Literal["realtime", "bulk"] | str,
        vendor_qps: int,
        reserved_realtime_qps: int,
        now_ms: int | None = None,
        vendor_id: str | None = None,
    ) -> int | None: ...

    async def refund(
        self,
        *,
        vendor_qps: int,
        lease_epoch: int,
        vendor_id: str | None = None,
    ) -> None: ...


class ChunkStore(Protocol):
    async def claim_submission(
        self,
        chunk_id: int,
        expected_retry_count: int,
        segments: int,
        *,
        enforce_live_test_budget: bool,
    ) -> SubmissionClaim: ...

    async def mark_submitted(self, chunk_id: int, task_id: str) -> None: ...

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
    ) -> FinalizeReport: ...

    async def mark_failed(self, chunk_id: int, code: int, message: str) -> None: ...

    async def mark_uncertain(self, chunk_id: int) -> None: ...

    async def schedule_retry(
        self,
        chunk_id: int,
        code: int,
        expected_retry_count: int,
        delay_s: int,
    ) -> bool: ...

    async def delay(self, chunk_id: int, code: int, delay_s: int) -> None: ...

    async def balance_blocked(self, batch_id: int, chunk_id: int) -> None: ...

    async def pause_blocked(self, chunk_id: int, code: int) -> None: ...

    async def pause_queues(self, code: int) -> None: ...

    async def split_once(self, chunk: ChunkPayload) -> list[ChunkPayload]: ...

    async def reject_disallowed_recipient(
        self,
        chunk_id: int,
        denied_count: int,
    ) -> None: ...

    async def defer_daily_limit(
        self,
        chunk_id: int,
        lane: str,
        reset_at: datetime,
    ) -> None: ...

    async def pause_daily_limit(self, lane: str, reset_at: datetime) -> None: ...

    async def pause_control_agent_stale(self) -> None: ...

    async def release_control_claim(self, chunk_id: int) -> None: ...

    async def release_unsent(self, chunk_id: int) -> None: ...

    async def is_paused(self, lane: str) -> bool: ...


class RecipientGuard(Protocol):
    def require_allowed(self, phones: tuple[str, ...]) -> None: ...


class ControlGuard(Protocol):
    def require_fresh(self) -> object: ...


class VendorAlertMonitor(Protocol):
    async def record_failure(self, *, code: int, chunk_id: int, batch_id: int) -> None: ...

    async def record_success(self) -> None: ...


class NoopVendorAlertMonitor:
    async def record_failure(self, *, code: int, chunk_id: int, batch_id: int) -> None:
        return None

    async def record_success(self) -> None:
        return None


class SendWorker:
    """执行单分片提交；transport 异常永不进入任何自动重试分支。"""

    def __init__(
        self,
        gateway: Gateway,
        store: ChunkStore,
        bucket: Bucket,
        *,
        monitor: VendorAlertMonitor | None = None,
        recipient_guard: RecipientGuard | None = None,
        control_guard: ControlGuard | None = None,
        enforce_live_test_budget: bool = False,
        enforce_live_test_recipients: bool = False,
        vendor_qps: int = 5,
        reserved_realtime_qps: int = 2,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        gateways: Mapping[str, Gateway] | None = None,
        router: VendorRouter | None = None,
        health: Callable[[], Awaitable[tuple[VendorHealth, ...]]] | None = None,
    ) -> None:
        self.gateway = gateway
        self.store = store
        self.bucket = bucket
        self.gateways = dict(gateways or {})
        self.router = router or VendorRouter()
        self.health = health
        self.monitor = monitor or NoopVendorAlertMonitor()
        if (
            enforce_live_test_budget
            and recipient_guard is None
            and not enforce_live_test_recipients
        ):
            raise ValueError("live-test budget requires recipient enforcement")
        self.recipient_guard = recipient_guard
        self.control_guard = control_guard
        self.enforce_live_test_budget = enforce_live_test_budget
        self.enforce_live_test_recipients = enforce_live_test_recipients
        self.vendor_qps = vendor_qps
        self.reserved_realtime_qps = reserved_realtime_qps
        self.sleeper = sleeper

    async def _record_failure(self, chunk: ChunkPayload, code: int) -> None:
        try:
            await self.monitor.record_failure(
                code=code,
                chunk_id=chunk.chunk_id,
                batch_id=chunk.batch_id,
            )
        except Exception as exc:
            LOGGER.error(
                "vendor failure monitor unavailable",
                extra={"error_type": type(exc).__name__, "vendor_code": code},
            )

    async def _record_success(self) -> None:
        try:
            await self.monitor.record_success()
        except Exception as exc:
            LOGGER.error(
                "vendor success monitor unavailable",
                extra={"error_type": type(exc).__name__},
            )

    async def _token(
        self,
        lane: Literal["realtime", "bulk"],
        vendor_id: str = PRIMARY_VENDOR_ID,
    ) -> int | None:
        while True:
            try:
                lease_epoch = await self.bucket.acquire(
                    lane=lane,
                    vendor_qps=self.vendor_qps,
                    reserved_realtime_qps=self.reserved_realtime_qps,
                    vendor_id=vendor_id,
                )
            except TypeError:
                lease_epoch = await self.bucket.acquire(
                    lane=lane,
                    vendor_qps=self.vendor_qps,
                    reserved_realtime_qps=self.reserved_realtime_qps,
                )
            if lease_epoch is not None:
                return lease_epoch
            # 等令牌期间可能发生熔断暂停；每轮复查，暂停即停发（#349）。
            if await self.store.is_paused(lane):
                return None
            await self.sleeper(0.05)

    async def _refund_token(
        self,
        lease_epoch: int,
        vendor_id: str = PRIMARY_VENDOR_ID,
    ) -> None:
        try:
            try:
                await self.bucket.refund(
                    vendor_qps=self.vendor_qps,
                    lease_epoch=lease_epoch,
                    vendor_id=vendor_id,
                )
            except TypeError:
                await self.bucket.refund(
                    vendor_qps=self.vendor_qps,
                    lease_epoch=lease_epoch,
                )
        except Exception as exc:
            LOGGER.error(
                "vendor token refund unavailable",
                extra={"error_type": type(exc).__name__},
            )

    async def _guard_chunk(self, chunk: ChunkPayload) -> bool:
        if self.enforce_live_test_recipients and chunk.denied_recipient_count:
            await self.store.reject_disallowed_recipient(
                chunk.chunk_id,
                chunk.denied_recipient_count,
            )
            return False
        if self.recipient_guard is None:
            return True
        try:
            self.recipient_guard.require_allowed(chunk.phones)
        except VendorTestRecipientDenied as error:
            await self.store.reject_disallowed_recipient(
                chunk.chunk_id,
                error.denied_count,
            )
            return False
        return True

    async def _control_ready(
        self,
        chunk: ChunkPayload,
        *,
        claimed: bool,
        lease_epoch: int | None = None,
        vendor_id: str = PRIMARY_VENDOR_ID,
    ) -> bool:
        if self.control_guard is None:
            return True
        try:
            self.control_guard.require_fresh()
        except VendorControlStateUnavailable as error:
            pause_error: Exception | None = None
            if error.requires_critical_pause:
                try:
                    await self.store.pause_control_agent_stale()
                except Exception as caught:
                    pause_error = caught
            try:
                if claimed:
                    await self.store.release_control_claim(chunk.chunk_id)
            finally:
                if lease_epoch is not None:
                    await self._refund_token(lease_epoch, vendor_id)
            if pause_error is not None:
                raise pause_error from None
            return False
        return True

    async def _claim_after_token(
        self,
        chunk: ChunkPayload,
        lane: Literal["realtime", "bulk"],
        retry_index: int,
        lease_epoch: int,
        vendor_id: str = PRIMARY_VENDOR_ID,
    ) -> SubmissionClaimStatus:
        segments = calculate_quota_cost(
            f"{chunk.sign_name}{chunk.content}",
            recipient_count=len(chunk.phones),
        )
        claim = await self.store.claim_submission(
            chunk.chunk_id,
            retry_index,
            segments,
            enforce_live_test_budget=self.enforce_live_test_budget,
        )
        if claim.status is SubmissionClaimStatus.CLAIMED:
            return claim.status
        await self._refund_token(lease_epoch, vendor_id)
        if claim.status is SubmissionClaimStatus.DAILY_LIMIT:
            if claim.reset_at is None:
                raise RuntimeError("daily-limit claim missing reset_at")
            await self.store.defer_daily_limit(chunk.chunk_id, lane, claim.reset_at)
            await self.store.pause_daily_limit(lane, claim.reset_at)
        return claim.status

    def _gateway_for(self, vendor_id: str) -> Gateway:
        mapped = self.gateways.get(vendor_id)
        if mapped is not None:
            return mapped
        if vendor_id == PRIMARY_VENDOR_ID:
            return self.gateway
        raise LookupError(vendor_id)

    async def _load_attempts(self, chunk: ChunkPayload) -> tuple[VendorAttempt, ...]:
        loader = getattr(self.store, "list_vendor_attempts", None)
        if loader is None:
            return ()
        return tuple(await loader(chunk.chunk_id))

    async def _health_snapshot(self, *, platform_paused: bool) -> tuple[VendorHealth, ...]:
        if self.health is not None:
            snapshot = await self.health()
        else:
            snapshot = tuple(
                VendorHealth(vendor_id, available=True)
                for vendor_id in self.router.registered_ids()
            )
        if not platform_paused:
            return snapshot
        return tuple(
            VendorHealth(
                item.vendor_id,
                available=item.available and item.vendor_id != PRIMARY_VENDOR_ID,
                pause_reason="platform_paused"
                if item.vendor_id == PRIMARY_VENDOR_ID
                else item.pause_reason,
            )
            for item in snapshot
        )

    async def _record_attempt(
        self,
        chunk: ChunkPayload,
        *,
        vendor_id: str,
        generation: int,
        outcome: str,
        safe_to_failover: bool = False,
        vendor_code: int | None = None,
    ) -> None:
        recorder = getattr(self.store, "record_vendor_attempt", None)
        if recorder is None:
            return
        await recorder(
            chunk.chunk_id,
            vendor_id=vendor_id,
            generation=generation,
            outcome=outcome,
            safe_to_failover=safe_to_failover,
            vendor_code=vendor_code,
        )

    async def _apply_terminal_api_error(
        self,
        chunk: ChunkPayload,
        error: VendorApiError,
        *,
        persist_chunk: bool = True,
    ) -> SubmitOutcome:
        policy = error.policy
        if policy.balance_blocked:
            if persist_chunk:
                await self.store.balance_blocked(chunk.batch_id, chunk.chunk_id)
            await self.store.pause_queues(error.code)
            await self._record_failure(chunk, error.code)
            return SubmitOutcome.PAUSED
        if policy.delay_s is not None:
            if persist_chunk:
                await self.store.delay(chunk.chunk_id, error.code, policy.delay_s)
            return SubmitOutcome.DELAYED
        if policy.pause_queues:
            if persist_chunk:
                await self.store.pause_blocked(chunk.chunk_id, error.code)
            await self.store.pause_queues(error.code)
            await self._record_failure(chunk, error.code)
            return SubmitOutcome.PAUSED
        if persist_chunk:
            await self.store.mark_failed(
                chunk.chunk_id,
                error.code,
                error.safe_message,
            )
        await self._record_failure(chunk, error.code)
        return SubmitOutcome.FAILED

    async def _persist_attempt(
        self,
        chunk: ChunkPayload,
        *,
        vendor_id: str,
        generation: int,
        outcome: str,
        attempt_id: int | None = None,
        safe_to_failover: bool = False,
        vendor_code: int | None = None,
    ) -> None:
        completer = getattr(self.store, "complete_vendor_attempt", None)
        if attempt_id is not None and completer is not None:
            await completer(
                attempt_id,
                outcome=outcome,
                safe_to_failover=safe_to_failover,
                vendor_code=vendor_code,
            )
            return
        await self._record_attempt(
            chunk,
            vendor_id=vendor_id,
            generation=generation,
            outcome=outcome,
            safe_to_failover=safe_to_failover,
            vendor_code=vendor_code,
        )

    async def _finalize_invoke(
        self,
        chunk: ChunkPayload,
        *,
        vendor_id: str,
        generation: int,
        attempt_id: int | None,
        result: str,
        vendor_task_id: str | None = None,
        vendor_code: int | None = None,
        safe_to_failover: bool = False,
        retry_delay_s: int | None = None,
        expected_retry_count: int | None = None,
        balance_blocked: bool = False,
    ) -> FinalizeReport | None:
        """有 invoking 行时走原子终结；否则保持旧的分步路径。"""

        if attempt_id is None:
            return None
        return await self.store.finalize_vendor_attempt(
            attempt_id,
            chunk.chunk_id,
            expected_generation=generation,
            result=result,
            vendor_task_id=vendor_task_id,
            vendor_code=vendor_code,
            safe_to_failover=safe_to_failover,
            retry_delay_s=retry_delay_s,
            expected_retry_count=expected_retry_count,
            batch_id=chunk.batch_id,
            balance_blocked=balance_blocked,
        )

    async def submit(
        self,
        chunk: ChunkPayload,
        *,
        lane: Literal["realtime", "bulk"],
        allow_split: bool = True,
    ) -> SubmitOutcome:
        retry_index = chunk.retry_count
        attempts = list(await self._load_attempts(chunk))
        claimed = False
        lease_epoch: int | None = None
        lease_vendor = PRIMARY_VENDOR_ID
        while True:
            platform_paused = await self.store.is_paused(lane)
            health = await self._health_snapshot(platform_paused=platform_paused)
            decision = self.router.decide(
                RouteRequest(
                    registered=self.router.registered_ids(),
                    attempts=tuple(attempts),
                    health=health,
                )
            )
            if decision.action == "terminal_uncertain":
                return SubmitOutcome.UNCERTAIN
            if decision.action != "invoke" or decision.vendor_id is None:
                if not claimed:
                    if platform_paused or decision.action in {"hold", "exhausted"}:
                        return SubmitOutcome.PAUSED
                    return SubmitOutcome.FAILED
                return SubmitOutcome.FAILED
            vendor_id = decision.vendor_id
            generation = decision.generation
            try:
                gateway = self._gateway_for(vendor_id)
            except LookupError:
                await self._record_attempt(
                    chunk,
                    vendor_id=vendor_id,
                    generation=generation,
                    outcome="cancelled_before_invoke",
                )
                return SubmitOutcome.FAILED
            if not claimed:
                if not await self._guard_chunk(chunk):
                    return SubmitOutcome.REJECTED
                if not await self._control_ready(chunk, claimed=False):
                    return SubmitOutcome.PAUSED
                lease_epoch = await self._token(lane, vendor_id)
                lease_vendor = vendor_id
                if lease_epoch is None:
                    return SubmitOutcome.PAUSED
                claim_status = await self._claim_after_token(
                    chunk,
                    lane,
                    retry_index,
                    lease_epoch,
                    vendor_id,
                )
                if claim_status is not SubmissionClaimStatus.CLAIMED:
                    if claim_status is SubmissionClaimStatus.DAILY_LIMIT:
                        return SubmitOutcome.DELAYED
                    return SubmitOutcome.STALE
                claimed = True
                if not await self._control_ready(
                    chunk,
                    claimed=True,
                    lease_epoch=lease_epoch,
                    vendor_id=vendor_id,
                ):
                    return SubmitOutcome.PAUSED
            begin = getattr(self.store, "begin_vendor_invoke", None)
            attempt_id: int | None = None
            vendor_invoked = False
            if begin is not None:
                try:
                    started = await begin(
                        chunk.chunk_id,
                        vendor_id=vendor_id,
                        adapter_id=vendor_id,
                        reason=decision.reason,
                    )
                except RuntimeError:
                    return SubmitOutcome.UNCERTAIN
                attempt_id = int(started["id"]) if isinstance(started, dict) else int(started.id)
                generation = (
                    int(started["generation"])
                    if isinstance(started, dict)
                    else int(started.generation)
                )
            try:
                vendor_invoked = True
                task_id = await gateway.send(
                    chunk.phones,
                    chunk.content,
                    template_id=chunk.template_id,
                    sign_name=chunk.sign_name,
                    custom_id=chunk.custom_id,
                )
            except (VendorTransportError, VendorProtocolError):
                report = await self._finalize_invoke(
                    chunk,
                    vendor_id=vendor_id,
                    generation=generation,
                    attempt_id=attempt_id,
                    result="uncertain",
                )
                if report is None:
                    await self._persist_attempt(
                        chunk,
                        vendor_id=vendor_id,
                        generation=generation,
                        outcome="uncertain",
                        attempt_id=attempt_id,
                    )
                    await self.store.mark_uncertain(chunk.chunk_id)
                    return SubmitOutcome.UNCERTAIN
                return submit_outcome_from_finalize(report, SubmitOutcome.UNCERTAIN)
            except VendorApiError as error:
                policy = error.policy
                if policy.retry_delays_s and retry_index < len(policy.retry_delays_s):
                    delay = policy.retry_delays_s[retry_index]
                    report = await self._finalize_invoke(
                        chunk,
                        vendor_id=vendor_id,
                        generation=generation,
                        attempt_id=attempt_id,
                        result="retry_scheduled",
                        vendor_code=error.code,
                        retry_delay_s=delay,
                        expected_retry_count=retry_index,
                    )
                    if report is not None:
                        return submit_outcome_from_finalize(
                            report,
                            SubmitOutcome.RETRY_SCHEDULED,
                        )
                    if not await self.store.schedule_retry(
                        chunk.chunk_id,
                        error.code,
                        retry_index,
                        delay,
                    ):
                        return SubmitOutcome.STALE
                    await self._persist_attempt(
                        chunk,
                        vendor_id=vendor_id,
                        generation=generation,
                        outcome="retry_scheduled",
                        attempt_id=attempt_id,
                        vendor_code=error.code,
                    )
                    return SubmitOutcome.RETRY_SCHEDULED
                if policy.shrink_batch_once and allow_split:
                    children = await self.store.split_once(chunk)
                    if children:
                        for child in children:
                            await self.submit(child, lane=lane, allow_split=False)
                        return SubmitOutcome.SPLIT
                if policy.delay_s is not None:
                    report = await self._finalize_invoke(
                        chunk,
                        vendor_id=vendor_id,
                        generation=generation,
                        attempt_id=attempt_id,
                        result="delayed",
                        vendor_code=error.code,
                        retry_delay_s=policy.delay_s,
                    )
                    if report is not None:
                        return submit_outcome_from_finalize(report, SubmitOutcome.DELAYED)
                    await self.store.delay(chunk.chunk_id, error.code, policy.delay_s)
                    await self._persist_attempt(
                        chunk,
                        vendor_id=vendor_id,
                        generation=generation,
                        outcome="delayed",
                        attempt_id=attempt_id,
                        vendor_code=error.code,
                    )
                    return SubmitOutcome.DELAYED
                hold_like = policy.balance_blocked or policy.pause_queues
                report = await self._finalize_invoke(
                    chunk,
                    vendor_id=vendor_id,
                    generation=generation,
                    attempt_id=attempt_id,
                    result="paused" if hold_like else "rejected",
                    vendor_code=error.code,
                    safe_to_failover=policy.safe_to_failover,
                    balance_blocked=policy.balance_blocked,
                )
                if report is None:
                    await self._persist_attempt(
                        chunk,
                        vendor_id=vendor_id,
                        generation=generation,
                        outcome="paused" if hold_like else "rejected",
                        attempt_id=attempt_id,
                        safe_to_failover=policy.safe_to_failover,
                        vendor_code=error.code,
                    )
                if hold_like:
                    return await self._apply_terminal_api_error(
                        chunk,
                        error,
                        persist_chunk=report is None,
                    )
                attempts.append(
                    VendorAttempt(
                        vendor_id,
                        generation,
                        "rejected",
                        policy.safe_to_failover,
                        error.code,
                    )
                )
                if policy.safe_to_failover:
                    failover = self.router.decide(
                        RouteRequest(
                            registered=self.router.registered_ids(),
                            attempts=tuple(attempts),
                            health=health,
                        )
                    )
                    if (
                        failover.action == "invoke"
                        and failover.vendor_id is not None
                        and failover.vendor_id != vendor_id
                    ):
                        vendor_id = failover.vendor_id
                        generation = failover.generation
                        lease_epoch = await self._token(lane, vendor_id)
                        lease_vendor = vendor_id
                        if lease_epoch is None:
                            return await self._apply_terminal_api_error(
                                chunk,
                                error,
                                persist_chunk=report is None,
                            )
                        continue
                return await self._apply_terminal_api_error(
                    chunk,
                    error,
                    persist_chunk=report is None,
                )
            except Exception:
                if vendor_invoked:
                    report = await self._finalize_invoke(
                        chunk,
                        vendor_id=vendor_id,
                        generation=generation,
                        attempt_id=attempt_id,
                        result="uncertain",
                    )
                    if report is None:
                        await self._persist_attempt(
                            chunk,
                            vendor_id=vendor_id,
                            generation=generation,
                            outcome="uncertain",
                            attempt_id=attempt_id,
                        )
                        await self.store.mark_uncertain(chunk.chunk_id)
                else:
                    await self.store.release_unsent(chunk.chunk_id)
                    if lease_epoch is not None:
                        await self._refund_token(lease_epoch, lease_vendor)
                raise
            else:
                report = await self._finalize_invoke(
                    chunk,
                    vendor_id=vendor_id,
                    generation=generation,
                    attempt_id=attempt_id,
                    result="submitted",
                    vendor_task_id=task_id,
                )
                if report is not None:
                    outcome = submit_outcome_from_finalize(report, SubmitOutcome.SUBMITTED)
                    if outcome is SubmitOutcome.SUBMITTED:
                        await self._record_success()
                    return outcome
                try:
                    await self.store.mark_submitted(chunk.chunk_id, task_id)
                except Exception:
                    await self._persist_attempt(
                        chunk,
                        vendor_id=vendor_id,
                        generation=generation,
                        outcome="uncertain",
                        attempt_id=attempt_id,
                    )
                    await self.store.mark_uncertain(chunk.chunk_id)
                    return SubmitOutcome.UNCERTAIN
                await self._persist_attempt(
                    chunk,
                    vendor_id=vendor_id,
                    generation=generation,
                    outcome="submitted",
                    attempt_id=attempt_id,
                )
                await self._record_success()
                return SubmitOutcome.SUBMITTED


async def _components() -> tuple[SendWorker, Any, ZhihuiClient, int]:
    """按 worker 启动时配置构造运行组件，凭据只从 secret 文件读取。"""

    from app.tasks.send_repository import SqlChunkStore

    settings = get_settings()
    crypto = CryptoService.from_settings(settings)
    redis: Any = redis_client(settings.redis_control_url)
    store = SqlChunkStore(crypto, settings, redis)
    batch_size, vendor_qps, reserved = await store.load_worker_config()
    control_guard = VendorControlStateGuard() if settings.vendor_live_test else None
    if control_guard is not None:
        try:
            control_guard.require_fresh()
        except VendorControlStateUnavailable as error:
            if error.requires_critical_pause:
                await store.pause_control_agent_stale()
            raise RuntimeError("真实联调控制状态不可用") from None
    gateway = ZhihuiClient.from_settings(settings)
    worker = SendWorker(
        gateway,
        store,
        TokenBucket(redis),
        monitor=RedisVendorAlertMonitor(redis, SqlAlertService(settings)),
        control_guard=control_guard,
        enforce_live_test_budget=settings.vendor_live_test,
        enforce_live_test_recipients=settings.vendor_live_test,
        vendor_qps=vendor_qps,
        reserved_realtime_qps=reserved,
        gateways={PRIMARY_VENDOR_ID: gateway},
    )
    return worker, store, gateway, batch_size


async def _process_batch(batch_no: str) -> int:
    _worker, store, gateway, batch_size = await _components()
    try:
        chunk_ids, _lane = await store.prepare_chunks(batch_no, batch_size)
        return len(chunk_ids)
    finally:
        await gateway.aclose()


async def _process_batch_event(batch_no: str, event_id: str) -> int:
    async def effect(claim: OutboxClaim) -> int:
        if claim.args != (batch_no,):
            raise ValueError("batch outbox args mismatch")
        return await _process_batch(batch_no)

    return await OutboxExecutor(SqlOutboxRepository()).run(
        UUID(event_id),
        expected_type="batch.ready",
        effect=effect,
    )


async def _run_chunk(
    chunk_id: int,
    *,
    fail_closed_on_pause: bool = False,
) -> ChunkTaskResult:
    worker, store, gateway, _ = await _components()
    try:
        loaded = await store.load_chunk(chunk_id)
        if loaded is None:
            return ChunkTaskResult(0, SubmitOutcome.NO_OP_ALREADY_TERMINAL)
        chunk, lane = loaded
        from app.services.runtime_heartbeat import touch_runtime_heartbeat

        await touch_runtime_heartbeat(
            "send-bulk" if lane == "bulk" else "send-realtime"
        )
        if await store.is_paused(lane):
            if fail_closed_on_pause:
                raise SendQueuePaused("send queue is paused")
            return ChunkTaskResult(0, SubmitOutcome.PAUSED)
        outcome = await worker.submit(chunk, lane=lane)
        if outcome is SubmitOutcome.SUBMITTED:
            await touch_runtime_heartbeat(
                "send-bulk" if lane == "bulk" else "send-realtime",
                success=True,
            )
        return ChunkTaskResult(1, outcome)
    finally:
        await gateway.aclose()


async def _process_chunk(
    chunk_id: int, *, fail_closed_on_pause: bool = False
) -> ChunkTaskResult:
    result = await _run_chunk(chunk_id, fail_closed_on_pause=fail_closed_on_pause)
    if fail_closed_on_pause and result.outcome is SubmitOutcome.PAUSED:
        raise SendQueuePaused("send queue is paused")
    return result


async def _process_chunk_event(chunk_id: int, event_id: str) -> ChunkTaskResult:
    async def effect(claim: OutboxClaim) -> ChunkTaskResult:
        if claim.args != (chunk_id,):
            raise ValueError("chunk outbox args mismatch")
        return await _process_chunk(chunk_id, fail_closed_on_pause=True)

    raw = await OutboxExecutor(SqlOutboxRepository()).run(
        UUID(event_id),
        expected_type="chunk.ready",
        effect=effect,
    )
    if isinstance(raw, ChunkTaskResult):
        return raw
    return ChunkTaskResult(1, SubmitOutcome.NO_OP_ALREADY_TERMINAL)


@celery_app.task(name="app.tasks.send.process_batch")  # type: ignore[untyped-decorator]
def process_batch(batch_no: str, outbox_event_id: str | None = None) -> dict[str, int]:
    """批次任务返回规划计数；不得把规划数解释为供应商 submitted。"""

    if outbox_event_id is None:
        planned = run_worker_async(_process_batch(batch_no))
    else:
        planned = run_worker_async(_process_batch_event(batch_no, outbox_event_id))
    return batch_plan_metrics(planned)


@celery_app.task(name="app.tasks.send.process_chunk")  # type: ignore[untyped-decorator]
def process_chunk(chunk_id: int, outbox_event_id: str | None = None) -> dict[str, int]:
    """chunk 任务返回诊断计数；Outbox 完成数仍由内部 int 效果决定。"""

    if outbox_event_id is None:
        result = run_worker_async(_run_chunk(chunk_id))
        return chunk_result_metrics(result)
    result = run_worker_async(_process_chunk_event(chunk_id, outbox_event_id))
    return chunk_result_metrics(result)
