"""事务性 Outbox 的无 PII 事件合同、dispatcher 与执行租约。"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from app.core.correlation import correlation_scope

LOGGER = logging.getLogger(__name__)

PHONE_IN_TEXT = re.compile(r"(?<!\d)1\d{10}(?!\d)")
BATCH_REFERENCE = re.compile(r"^[0-9a-f]{32}$")
UUID_REFERENCE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
STRUCTURED_REFERENCE = re.compile(
    r"^(?:"
    r"batch[.]ready:[0-9a-f]{32}|"
    r"scheduled:[0-9a-f]{32}:ready|"
    r"batch:[0-9a-f]{32}:cancelled|"
    r"approval:[1-9][0-9]*:(?:approved|rejected|expired)|"
    r"callback:[1-9][0-9]*:attempt:[0-9]+|"
    r"alert:[1-9][0-9]*:(?:wecom|smtp)|"
    r"usage[.]release:[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}"
    r")$"
)
TASK_NAMES = {
    "app.tasks.send.process_batch",
    "app.tasks.deliver_callback",
    "app.tasks.outbox.compensate_quota",
    "app.tasks.outbox.deliver_alert",
    "app.tasks.outbox.release_usage",
    "app.tasks.outbox.trigger_job",
}
MANUAL_JOB_TASK_NAMES = frozenset(
    {
        "app.tasks.poll_report",
        "app.tasks.poll_reply",
        "app.tasks.reconcile",
        "app.tasks.expire_approvals",
        "app.tasks.dispatch_scheduled",
        "app.tasks.sync_templates",
        "app.tasks.sync_signs",
        "app.tasks.poll_balance",
        "app.tasks.anomaly_scan",
        "app.tasks.dispatch_callbacks",
        "app.tasks.dispatch_exports",
        "app.tasks.dispatch_imports",
        "app.tasks.cleanup_exports",
        "app.tasks.aggregate_stats",
        "app.tasks.housekeeping",
        "app.tasks.reconcile_usage_projection",
    }
)
FORBIDDEN_KEYS = {
    "phone",
    "phones",
    "mobile",
    "mobiles",
    "phone_enc",
    "phone_hmac",
    "content",
    "body",
    "secret",
    "password",
}
ALLOWED_QUEUES = {"realtime", "bulk", "callback"}


class OutboxLeaseLost(RuntimeError):
    """租约或 fencing token 已失效，禁止继续更新事件。"""


class OutboxContractConflict(RuntimeError):
    """同一 dedup_key 对应了不同事件合同。"""


@dataclass(frozen=True, slots=True)
class OutboxEventSpec:
    event_type: str
    aggregate_type: str
    aggregate_id: str
    task_name: str
    queue: str
    args: tuple[str | int, ...]
    dedup_key: str
    max_attempts: int = 12
    correlation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class OutboxLease:
    event_id: UUID
    lease_id: UUID
    event_type: str
    task_name: str
    queue: str
    args: tuple[str | int, ...]
    attempts: int
    correlation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class OutboxClaim:
    event_id: UUID
    lease_id: UUID
    event_type: str
    args: tuple[str | int, ...]
    correlation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class OutboxStats:
    pending: int
    published: int
    processing: int
    dead: int
    failed_attempts: int
    oldest_age_seconds: int


@dataclass(frozen=True, slots=True)
class OutboxEventRecord:
    """运维中心用无 PII 事件元数据；args/dedup_key/correlation_id 不对外。"""

    id: UUID
    event_type: str
    aggregate_type: str
    aggregate_id: str
    task_name: str
    queue: str
    state: str
    attempts: int
    max_attempts: int
    failure_count: int
    last_error: str | None
    next_attempt_at: datetime
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class OutboxEventPage:
    """事件分页结果；字段与 ops.OpsPage 对齐但不引入其 jobtrack 依赖。"""

    items: tuple[OutboxEventRecord, ...]
    total: int
    page: int
    page_size: int


def _assert_safe(value: Any, *, key: str | None = None) -> None:
    if key is not None and key.casefold() in FORBIDDEN_KEYS:
        raise ValueError("outbox args contain a forbidden field")
    if isinstance(value, dict):
        for nested_key, nested_value in value.items():
            _assert_safe(nested_value, key=str(nested_key))
        return
    if isinstance(value, (list, tuple)):
        for nested_value in value:
            _assert_safe(nested_value)
        return
    if isinstance(value, str):
        if BATCH_REFERENCE.fullmatch(value) or STRUCTURED_REFERENCE.fullmatch(value):
            return
        if PHONE_IN_TEXT.search(value):
            raise ValueError("outbox args contain a phone number")
    if not isinstance(value, (str, int, bool, type(None))):
        raise ValueError("outbox args must be JSON scalar references")


def validate_spec(spec: OutboxEventSpec) -> None:
    """验证稳定引用合同；拒绝 PII、正文、凭据和任意队列/任务注入。"""

    if spec.correlation_id is not None and (
        not isinstance(spec.correlation_id, UUID) or spec.correlation_id.int == 0
    ):
        raise ValueError("invalid outbox correlation_id")
    if not 1 <= len(spec.event_type) <= 64:
        raise ValueError("invalid outbox event_type")
    if not 1 <= len(spec.aggregate_type) <= 32:
        raise ValueError("invalid outbox aggregate_type")
    if not 1 <= len(spec.aggregate_id) <= 128:
        raise ValueError("invalid outbox aggregate_id")
    if spec.task_name not in TASK_NAMES:
        raise ValueError("invalid outbox task_name")
    if spec.queue not in ALLOWED_QUEUES:
        raise ValueError("invalid outbox queue")
    if not 1 <= len(spec.dedup_key) <= 192:
        raise ValueError("invalid outbox dedup_key")
    if not 1 <= spec.max_attempts <= 100:
        raise ValueError("invalid outbox max_attempts")
    usage_release_reference = (
        spec.task_name == "app.tasks.outbox.release_usage"
        and spec.event_type == "usage.release"
        and spec.aggregate_type == "usage_reservation"
        and UUID_REFERENCE.fullmatch(spec.aggregate_id) is not None
        and spec.args == (spec.aggregate_id,)
        and spec.dedup_key == f"usage.release:{spec.aggregate_id}"
    )
    manual_job_reference = (
        spec.task_name == "app.tasks.outbox.trigger_job"
        and spec.event_type == "job.trigger"
        and spec.aggregate_type == "job"
        and spec.aggregate_id == spec.args[0].rsplit(".", 1)[-1]
        and spec.dedup_key.startswith(f"job.trigger:{spec.aggregate_id}:")
        and UUID_REFERENCE.fullmatch(spec.dedup_key.rsplit(":", 1)[-1]) is not None
        if len(spec.args) == 1
        and isinstance(spec.args[0], str)
        and spec.args[0] in MANUAL_JOB_TASK_NAMES
        else False
    )
    if spec.task_name == "app.tasks.outbox.release_usage" and not usage_release_reference:
        raise ValueError("invalid usage release outbox contract")
    if spec.task_name == "app.tasks.outbox.trigger_job" and not manual_job_reference:
        raise ValueError("invalid manual job outbox contract")
    if not usage_release_reference and not manual_job_reference:
        _assert_safe(spec.aggregate_id)
        _assert_safe(spec.args)
        _assert_safe(spec.dedup_key)
    if not isinstance(spec.args, tuple) or any(
        isinstance(item, bool) or not isinstance(item, (str, int))
        for item in spec.args
    ):
        raise ValueError("outbox args must be string or integer references")


class OutboxRepository(Protocol):
    async def lease_due(self, *, limit: int, lease_seconds: int) -> list[OutboxLease]: ...

    async def mark_published(self, event_id: UUID, lease_id: UUID) -> None: ...

    async def mark_publish_failed(
        self,
        event_id: UUID,
        lease_id: UUID,
        error_type: str,
    ) -> None: ...

    async def claim_execution(
        self,
        event_id: UUID,
        *,
        lease_seconds: int,
    ) -> OutboxClaim | None: ...

    async def heartbeat(self, event_id: UUID, lease_id: UUID, *, lease_seconds: int) -> bool: ...

    async def complete(self, event_id: UUID, lease_id: UUID) -> None: ...

    async def fail_execution(
        self,
        event_id: UUID,
        lease_id: UUID,
        error_type: str,
    ) -> None: ...


class OutboxPublisher(Protocol):
    async def publish(self, event: OutboxLease) -> None: ...


class OutboxDispatcher:
    """以数据库租约领取事件；publish 结果只通过 fencing CAS 回写。"""

    def __init__(
        self,
        repository: OutboxRepository,
        publisher: OutboxPublisher,
        *,
        lease_seconds: int = 60,
        batch_size: int = 100,
    ) -> None:
        if lease_seconds < 5 or not 1 <= batch_size <= 1000:
            raise ValueError("invalid outbox dispatcher settings")
        self.repository = repository
        self.publisher = publisher
        self.lease_seconds = lease_seconds
        self.batch_size = batch_size

    async def dispatch_once(self) -> int:
        leases = await self.repository.lease_due(
            limit=self.batch_size,
            lease_seconds=self.lease_seconds,
        )
        published = 0
        for event in leases:
            try:
                await self.publisher.publish(event)
            except Exception as exc:
                LOGGER.error(
                    "outbox_publish_failed",
                    extra={
                        "correlation_id": str(event.correlation_id or event.event_id),
                        "outbox_event_id": str(event.event_id),
                        "task_name": event.task_name,
                        "error_type": type(exc).__name__,
                    },
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
                await self.repository.mark_publish_failed(
                    event.event_id,
                    event.lease_id,
                    type(exc).__name__,
                )
                continue
            await self.repository.mark_published(event.event_id, event.lease_id)
            published += 1
        return published


class OutboxExecutor:
    """消费者按 event ID 领取执行租约并周期续租；重复投递只执行一次。"""

    def __init__(
        self,
        repository: OutboxRepository,
        *,
        lease_seconds: int = 300,
    ) -> None:
        if lease_seconds < 15:
            raise ValueError("invalid outbox execution lease")
        self.repository = repository
        self.lease_seconds = lease_seconds

    async def run(
        self,
        event_id: UUID,
        *,
        expected_type: str,
        effect: Callable[[OutboxClaim], Awaitable[int]],
    ) -> int:
        claim = await self.repository.claim_execution(
            event_id,
            lease_seconds=self.lease_seconds,
        )
        if claim is None:
            return 0
        if claim.event_type != expected_type:
            await self.repository.fail_execution(
                claim.event_id,
                claim.lease_id,
                "OutboxEventTypeMismatch",
            )
            return 0
        correlation_id = claim.correlation_id or claim.event_id
        heartbeat_stopped = asyncio.Event()
        lease_lost = asyncio.Event()

        async def maintain_lease() -> None:
            heartbeat_interval = max(5.0, self.lease_seconds / 3)
            while not heartbeat_stopped.is_set():
                try:
                    await asyncio.wait_for(
                        heartbeat_stopped.wait(),
                        timeout=heartbeat_interval,
                    )
                    return
                except TimeoutError:
                    pass
                try:
                    renewed = await self.repository.heartbeat(
                        claim.event_id,
                        claim.lease_id,
                        lease_seconds=self.lease_seconds,
                    )
                except Exception:
                    lease_lost.set()
                    return
                if not renewed:
                    lease_lost.set()
                    return

        heartbeat_task = asyncio.create_task(maintain_lease())
        try:
            with correlation_scope(correlation_id):
                result = await effect(claim)
            if lease_lost.is_set():
                raise OutboxLeaseLost("outbox execution lease was lost")
        except Exception as exc:
            if not lease_lost.is_set():
                await self.repository.fail_execution(
                    claim.event_id,
                    claim.lease_id,
                    type(exc).__name__,
                )
            raise
        finally:
            heartbeat_stopped.set()
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
        await self.repository.complete(claim.event_id, claim.lease_id)
        return result
