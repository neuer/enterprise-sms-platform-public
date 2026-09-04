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


SUBMIT_OUTCOME_LABELS = tuple(item.value for item in SubmitOutcome)


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
    ) -> int | None: ...

    async def refund(self, *, vendor_qps: int, lease_epoch: int) -> None: ...


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

    async def _token(self, lane: Literal["realtime", "bulk"]) -> int | None:
        while True:
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

    async def _refund_token(self, lease_epoch: int) -> None:
        try:
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
                    await self._refund_token(lease_epoch)
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
        await self._refund_token(lease_epoch)
        if claim.status is SubmissionClaimStatus.DAILY_LIMIT:
            if claim.reset_at is None:
                raise RuntimeError("daily-limit claim missing reset_at")
            await self.store.defer_daily_limit(chunk.chunk_id, lane, claim.reset_at)
            await self.store.pause_daily_limit(lane, claim.reset_at)
        return claim.status

    def _gateway_for(self, vendor_id: str) -> Gateway:
        return self.gateways.get(vendor_id, self.gateway)

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
    ) -> SubmitOutcome:
        policy = error.policy
        if policy.balance_blocked:
            await self.store.balance_blocked(chunk.batch_id, chunk.chunk_id)
            await self.store.pause_queues(error.code)
            await self._record_failure(chunk, error.code)
            return SubmitOutcome.PAUSED
        if policy.delay_s is not None:
            await self.store.delay(chunk.chunk_id, error.code, policy.delay_s)
            return SubmitOutcome.DELAYED
        if policy.pause_queues:
            await self.store.pause_blocked(chunk.chunk_id, error.code)
            await self.store.pause_queues(error.code)
            await self._record_failure(chunk, error.code)
            return SubmitOutcome.PAUSED
        await self.store.mark_failed(
            chunk.chunk_id,
            error.code,
            error.safe_message,
        )
        await self._record_failure(chunk, error.code)
        return SubmitOutcome.FAILED

    async def submit(
        self,
        chunk: ChunkPayload,
        *,
        lane: Literal["realtime", "bulk"],
        allow_split: bool = True,
    ) -> SubmitOutcome:
        retry_index = chunk.retry_count
        vendor_id = chunk.selected_vendor or PRIMARY_VENDOR_ID
        generation = max(1, chunk.route_generation)
        attempts: list[VendorAttempt] = []
        claimed = False
        lease_epoch: int | None = None
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
            if decision.action == "invoke" and decision.vendor_id is not None:
                vendor_id = decision.vendor_id
                generation = decision.generation
            elif not claimed:
                if decision.action == "terminal_uncertain":
                    return SubmitOutcome.UNCERTAIN
                if platform_paused or decision.action in {"hold", "exhausted"}:
                    return SubmitOutcome.PAUSED
                return SubmitOutcome.FAILED
            if not claimed:
                if not await self._guard_chunk(chunk):
                    return SubmitOutcome.REJECTED
                if not await self._control_ready(chunk, claimed=False):
                    return SubmitOutcome.PAUSED
                lease_epoch = await self._token(lane)
                if lease_epoch is None:
                    return SubmitOutcome.PAUSED
                claim_status = await self._claim_after_token(
                    chunk,
                    lane,
                    retry_index,
                    lease_epoch,
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
                ):
                    return SubmitOutcome.PAUSED
            vendor_invoked = False
            try:
                vendor_invoked = True
                task_id = await self._gateway_for(vendor_id).send(
                    chunk.phones,
                    chunk.content,
                    template_id=chunk.template_id,
                    sign_name=chunk.sign_name,
                    custom_id=chunk.custom_id,
                )
            except (VendorTransportError, VendorProtocolError):
                await self._record_attempt(
                    chunk,
                    vendor_id=vendor_id,
                    generation=generation,
                    outcome="uncertain",
                )
                await self.store.mark_uncertain(chunk.chunk_id)
                return SubmitOutcome.UNCERTAIN
            except VendorApiError as error:
                policy = error.policy
                if policy.retry_delays_s and retry_index < len(policy.retry_delays_s):
                    delay = policy.retry_delays_s[retry_index]
                    if not await self.store.schedule_retry(
                        chunk.chunk_id,
                        error.code,
                        retry_index,
                        delay,
                    ):
                        return SubmitOutcome.STALE
                    await self._record_attempt(
                        chunk,
                        vendor_id=vendor_id,
                        generation=generation,
                        outcome="retry_scheduled",
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
                    await self.store.delay(chunk.chunk_id, error.code, policy.delay_s)
                    await self._record_attempt(
                        chunk,
                        vendor_id=vendor_id,
                        generation=generation,
                        outcome="delayed",
                        vendor_code=error.code,
                    )
                    return SubmitOutcome.DELAYED
                await self._record_attempt(
                    chunk,
                    vendor_id=vendor_id,
                    generation=generation,
                    outcome="rejected",
                    safe_to_failover=policy.safe_to_failover,
                    vendor_code=error.code,
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
                        if lease_epoch is not None:
                            await self._refund_token(lease_epoch)
                        vendor_id = failover.vendor_id
                        generation = failover.generation
                        lease_epoch = await self._token(lane)
                        if lease_epoch is None:
                            return await self._apply_terminal_api_error(chunk, error)
                        continue
                return await self._apply_terminal_api_error(chunk, error)
            except Exception:
                if vendor_invoked:
                    await self._record_attempt(
                        chunk,
                        vendor_id=vendor_id,
                        generation=generation,
                        outcome="uncertain",
                    )
                    await self.store.mark_uncertain(chunk.chunk_id)
                else:
                    await self.store.release_unsent(chunk.chunk_id)
                raise
            else:
                try:
                    await self.store.mark_submitted(chunk.chunk_id, task_id)
                except Exception:
                    await self._record_attempt(
                        chunk,
                        vendor_id=vendor_id,
                        generation=generation,
                        outcome="uncertain",
                    )
                    await self.store.mark_uncertain(chunk.chunk_id)
                    return SubmitOutcome.UNCERTAIN
                await self._record_attempt(
                    chunk,
                    vendor_id=vendor_id,
                    generation=generation,
                    outcome="submitted",
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
            return ChunkTaskResult(0, None)
        chunk, lane = loaded
        if await store.is_paused(lane):
            if fail_closed_on_pause:
                raise SendQueuePaused("send queue is paused")
            return ChunkTaskResult(0, SubmitOutcome.PAUSED)
        outcome = await worker.submit(chunk, lane=lane)
        return ChunkTaskResult(1, outcome)
    finally:
        await gateway.aclose()


async def _process_chunk(chunk_id: int, *, fail_closed_on_pause: bool = False) -> int:
    result = await _run_chunk(chunk_id, fail_closed_on_pause=fail_closed_on_pause)
    if fail_closed_on_pause and result.outcome is SubmitOutcome.PAUSED:
        raise SendQueuePaused("send queue is paused")
    return result.processed


async def _process_chunk_event(chunk_id: int, event_id: str) -> int:
    async def effect(claim: OutboxClaim) -> int:
        if claim.args != (chunk_id,):
            raise ValueError("chunk outbox args mismatch")
        return await _process_chunk(chunk_id, fail_closed_on_pause=True)

    return await OutboxExecutor(SqlOutboxRepository()).run(
        UUID(event_id),
        expected_type="chunk.ready",
        effect=effect,
    )


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
    processed = run_worker_async(_process_chunk_event(chunk_id, outbox_event_id))
    return chunk_result_metrics(ChunkTaskResult(processed, None))
