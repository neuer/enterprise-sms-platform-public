"""callback 单次投递领取、重试序列与 dead 告警状态机。"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from app.core.correlation import correlation_scope
from app.services.callback import DeliveryOutcome
from app.services.callback_authority import CallbackAuthorityBusy

RETRY_DELAYS_S = (60, 300, 900, 3600, 3600)
CALLBACK_LEASE_SECONDS = 30
CALLBACK_AUTHORITY_BUSY_DELAY_S = 1
PERMANENT_CALLBACK_STATUSES = frozenset({400, 401, 403, 404, 410, 422})
RETRYABLE_CALLBACK_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class CallbackLeaseLost(RuntimeError):
    """当前 callback worker 的 fencing token 已失效。"""


def classify_callback_http_status(status: int) -> str:
    """集中分类回调响应：success / retryable / permanent。"""

    if 200 <= status < 300:
        return "success"
    if status in RETRYABLE_CALLBACK_STATUSES or 500 <= status < 600:
        return "retryable"
    if status in PERMANENT_CALLBACK_STATUSES or 400 <= status < 500:
        return "permanent"
    return "retryable"


@dataclass(frozen=True, slots=True)
class CallbackClaim:
    task_id: int
    app_id: int
    event_id: UUID
    event: str
    retry_count: int
    lease_id: UUID
    lease_expires_at: datetime
    correlation_id: UUID | None = None


class CallbackStateRepository(Protocol):
    async def claim(
        self,
        task_id: int,
        *,
        lease_seconds: int,
    ) -> CallbackClaim | None: ...

    async def heartbeat(
        self,
        task_id: int,
        lease_id: UUID,
        *,
        lease_seconds: int,
    ) -> bool: ...

    async def mark_done(self, task_id: int, lease_id: UUID, http_code: int) -> None: ...

    async def mark_authority_busy(
        self,
        task_id: int,
        lease_id: UUID,
        *,
        retry_count: int,
        delay_s: int,
    ) -> None: ...

    async def mark_retry(
        self,
        task_id: int,
        lease_id: UUID,
        *,
        retry_count: int,
        delay_s: int,
        http_code: int | None,
        error: str | None,
    ) -> None: ...

    async def mark_dead(
        self,
        task_id: int,
        lease_id: UUID,
        *,
        retry_count: int,
        http_code: int | None,
        error: str | None,
    ) -> None: ...


class DeliveryPort(Protocol):
    async def deliver(self, task_id: int, lease_id: UUID) -> DeliveryOutcome: ...


class AlertEmitter(Protocol):
    async def emit(
        self,
        *,
        alert_type: str,
        level: str,
        title: str,
        detail: dict[str, Any],
        dedup_key: str,
    ) -> None: ...


class CallbackWorker:
    """一次调用执行一次 HTTP 尝试；UUID 租约续租并 fencing 所有终态写入。"""

    def __init__(
        self,
        repository: CallbackStateRepository,
        delivery: DeliveryPort,
        alerts: AlertEmitter,
        *,
        retry_delays_s: tuple[int, ...] = RETRY_DELAYS_S,
        lease_seconds: int = CALLBACK_LEASE_SECONDS,
        heartbeat_interval_s: float | None = None,
    ) -> None:
        if lease_seconds < 3:
            raise ValueError("callback lease must be at least 3 seconds")
        self.repository = repository
        self.delivery = delivery
        self.alerts = alerts
        self.retry_delays_s = retry_delays_s
        self.lease_seconds = lease_seconds
        self.heartbeat_interval_s = heartbeat_interval_s or lease_seconds / 3

    async def process(self, task_id: int) -> int:
        claimed = await self.repository.claim(
            task_id,
            lease_seconds=self.lease_seconds,
        )
        if claimed is None:
            return 0
        with correlation_scope(claimed.correlation_id or claimed.event_id):
            return await self._process_claim(claimed)

    async def _process_claim(self, claimed: CallbackClaim) -> int:
        """在 callback_task 固化的关联上下文内执行一次投递状态机。"""

        task_id = claimed.task_id
        stopped = asyncio.Event()
        lease_lost = asyncio.Event()

        async def maintain_lease() -> None:
            while not stopped.is_set():
                try:
                    await asyncio.wait_for(
                        stopped.wait(),
                        timeout=self.heartbeat_interval_s,
                    )
                    return
                except TimeoutError:
                    pass
                try:
                    renewed = await self.repository.heartbeat(
                        task_id,
                        claimed.lease_id,
                        lease_seconds=self.lease_seconds,
                    )
                except Exception:
                    renewed = False
                if not renewed:
                    lease_lost.set()
                    return

        heartbeat_task = asyncio.create_task(maintain_lease())
        delivery_task = asyncio.create_task(
            self.delivery.deliver(task_id, claimed.lease_id)
        )
        lost_task = asyncio.create_task(lease_lost.wait())
        try:
            done, _pending = await asyncio.wait(
                {delivery_task, lost_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if lost_task in done and lease_lost.is_set():
                delivery_task.cancel()
                with suppress(asyncio.CancelledError):
                    await delivery_task
                raise CallbackLeaseLost("callback lease lost during delivery")
            outcome = await delivery_task
            if lease_lost.is_set():
                raise CallbackLeaseLost("callback lease lost during delivery")
        finally:
            stopped.set()
            lost_task.cancel()
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await lost_task
            with suppress(asyncio.CancelledError):
                await heartbeat_task

        if outcome.success and outcome.http_code is not None:
            await self.repository.mark_done(
                task_id,
                claimed.lease_id,
                outcome.http_code,
            )
            return 1
        if outcome.error == CallbackAuthorityBusy.__name__:
            await self.repository.mark_authority_busy(
                task_id,
                claimed.lease_id,
                retry_count=claimed.retry_count,
                delay_s=CALLBACK_AUTHORITY_BUSY_DELAY_S,
            )
            return 1
        if (
            outcome.http_code is not None
            and classify_callback_http_status(outcome.http_code) == "permanent"
        ):
            await self.repository.mark_dead(
                task_id,
                claimed.lease_id,
                retry_count=claimed.retry_count,
                http_code=outcome.http_code,
                error=outcome.error or "permanent_failure",
            )
            await self.alerts.emit(
                alert_type="callback_dead",
                level="crit",
                title="结果回调永久失败",
                detail={
                    "callback_task_id": claimed.task_id,
                    "app_id": claimed.app_id,
                    "event": claimed.event,
                    "failure_kind": "permanent_failure",
                    "http_code": outcome.http_code,
                },
                dedup_key=f"callback_permanent:{claimed.task_id}:{outcome.http_code}",
            )
            return 1
        if claimed.retry_count < len(self.retry_delays_s):
            await self.repository.mark_retry(
                task_id,
                claimed.lease_id,
                retry_count=claimed.retry_count,
                delay_s=self.retry_delays_s[claimed.retry_count],
                http_code=outcome.http_code,
                error=outcome.error,
            )
            return 1
        await self.repository.mark_dead(
            task_id,
            claimed.lease_id,
            retry_count=claimed.retry_count,
            http_code=outcome.http_code,
            error=outcome.error,
        )
        await self.alerts.emit(
            alert_type="callback_dead",
            level="crit",
            title="结果回调重试耗尽",
            detail={
                "callback_task_id": claimed.task_id,
                "app_id": claimed.app_id,
                "event": claimed.event,
                "failure_kind": "retries_exhausted",
            },
            dedup_key=f"callback_dead:{claimed.task_id}",
        )
        return 1
