"""Outbox 消费者：配额补偿等稳定引用副作用。"""

from __future__ import annotations

import asyncio
from uuid import UUID

from redis.asyncio import Redis

from app.core.worker_runtime import run_worker_async
from app.services.alert import AlertEvent, SmtpChannel, WeComChannel
from app.services.alert_repository import SqlAlertRepository
from app.services.outbox import MANUAL_JOB_TASK_NAMES, OutboxClaim, OutboxExecutor
from app.services.outbox_repository import SqlOutboxRepository
from app.services.quota import QuotaService
from app.services.usage_ledger import UsageLedgerService
from app.settings import get_settings
from app.tasks import celery_app


async def _compensate_quota(
    app_id: int,
    dept: str,
    category: str,
    date_key: str,
    cost: int,
    compensation_id: str,
    event_id: str,
) -> int:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_control_url, decode_responses=True)

    async def effect(claim: OutboxClaim) -> int:
        expected = (app_id, dept, category, date_key, cost, compensation_id)
        if claim.args != expected:
            raise ValueError("quota compensation outbox args mismatch")
        released = await QuotaService(redis).refund_once(
            app_id=app_id,
            dept=dept,
            category=category,
            date_key=date_key,
            cost=cost,
            event_id=compensation_id,
            marker_ttl_s=172800,
        )
        return int(released.released)

    try:
        return await OutboxExecutor(SqlOutboxRepository(settings)).run(
            UUID(event_id),
            expected_type="quota.compensation",
            effect=effect,
        )
    finally:
        await redis.aclose()


async def _deliver_alert(
    alert_id: int,
    channel: str,
    event_id: str,
) -> int:
    settings = get_settings()
    repository = SqlAlertRepository(settings)

    async def effect(claim: OutboxClaim) -> int:
        if claim.args != (alert_id, channel):
            raise ValueError("alert delivery outbox args mismatch")
        row = await repository.load_event(alert_id)
        if row is None:
            raise ValueError("alert event unavailable")
        event = AlertEvent(
            alert_type=str(row["alert_type"]),
            level=str(row["level"]),
            title=str(row["title"]),
            detail=dict(row["detail"] or {}),
            dedup_key=str(row["dedup_key"] or f"alert:{alert_id}"),
        )
        routing = await repository.load_routing()
        if channel == "wecom":
            if not routing.wecom_webhook:
                return 0
            await WeComChannel().send(routing.wecom_webhook, event)
            return 1
        if channel == "smtp":
            if routing.smtp is None:
                return 0
            await SmtpChannel(settings.alert_smtp_allowed_host_set).send(
                routing.smtp,
                event,
            )
            return 1
        raise ValueError("unsupported alert delivery channel")

    return await OutboxExecutor(SqlOutboxRepository(settings)).run(
        UUID(event_id),
        expected_type="alert.delivery",
        effect=effect,
    )


async def _release_usage(
    reservation_id: str,
    event_id: str,
) -> int:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_control_url, decode_responses=True)

    async def effect(claim: OutboxClaim) -> int:
        if claim.args != (reservation_id,):
            raise ValueError("usage release outbox args mismatch")
        return await UsageLedgerService(
            redis,
            settings,
            pooled=False,
        ).apply_release(UUID(reservation_id))

    try:
        return await OutboxExecutor(SqlOutboxRepository(settings)).run(
            UUID(event_id),
            expected_type="usage.release",
            effect=effect,
        )
    finally:
        await redis.aclose()


async def _trigger_job(
    task_name: str,
    event_id: str,
    *,
    sign_id: int | None = None,
) -> int:
    async def effect(claim: OutboxClaim) -> int:
        exact_sign_sync = (
            task_name == "app.tasks.sync_signs"
            and sign_id is not None
            and not isinstance(sign_id, bool)
            and 1 <= sign_id <= 2_147_483_647
        )
        expected_args = (task_name, sign_id) if exact_sign_sync else (task_name,)
        if (
            claim.args != expected_args
            or task_name not in MANUAL_JOB_TASK_NAMES
            or (sign_id is not None and not exact_sign_sync)
        ):
            raise ValueError("manual job outbox args mismatch")
        task = celery_app.tasks.get(task_name)
        if task is None:
            raise ValueError("manual job task is unavailable")
        if exact_sign_sync:
            assert sign_id is not None
            result = await _sync_exact_sign(sign_id)
        else:
            result = await asyncio.to_thread(task.run)
        return int(result or 0)

    return await OutboxExecutor(SqlOutboxRepository(get_settings())).run(
        UUID(event_id),
        expected_type="job.trigger",
        effect=effect,
    )


async def _sync_exact_sign(sign_id: int) -> int:
    """执行单条人工同步，不冒充 beat 全量任务心跳。"""

    from app.tasks.sign import _sync

    return await _sync(sign_id)


@celery_app.task(name="app.tasks.outbox.compensate_quota")  # type: ignore[untyped-decorator]
def compensate_quota(
    app_id: int,
    dept: str,
    category: str,
    date_key: str,
    cost: int,
    compensation_id: str,
    outbox_event_id: str,
) -> int:
    """参数只含配额维度、计费条数与 event ID，不含手机号或正文。"""

    return run_worker_async(
        _compensate_quota(
            app_id,
            dept,
            category,
            date_key,
            cost,
            compensation_id,
            outbox_event_id,
        )
    )


@celery_app.task(name="app.tasks.outbox.release_usage")  # type: ignore[untyped-decorator]
def release_usage(
    reservation_id: str,
    outbox_event_id: str,
) -> int:
    """只携带 reservation UUID；绝对投影重复执行仍保持幂等。"""

    return run_worker_async(_release_usage(reservation_id, outbox_event_id))


@celery_app.task(name="app.tasks.outbox.deliver_alert")  # type: ignore[untyped-decorator]
def deliver_alert(
    alert_id: int,
    channel: str,
    outbox_event_id: str,
) -> int:
    """任务参数只含 alert_log.id、渠道名与 event ID。"""

    return run_worker_async(_deliver_alert(alert_id, channel, outbox_event_id))


async def _apply_uncertain_effect(resolution_id: int, event_id: str) -> int:
    from app.services.crypto import CryptoService
    from app.services.uncertain_resolution import UncertainResolutionService

    async def effect(claim: OutboxClaim) -> int:
        if claim.args != (resolution_id,):
            raise ValueError("uncertain effect outbox args mismatch")
        await UncertainResolutionService(
            CryptoService.from_settings(get_settings())
        ).apply_effect(resolution_id)
        return 1

    return await OutboxExecutor(SqlOutboxRepository(get_settings())).run(
        UUID(event_id),
        expected_type="uncertain.effect",
        effect=effect,
    )


@celery_app.task(name="app.tasks.outbox.apply_uncertain_effect")  # type: ignore[untyped-decorator]
def apply_uncertain_effect(resolution_id: int, outbox_event_id: str) -> int:
    """只携带 resolution id；重复投递保持幂等。"""

    return run_worker_async(_apply_uncertain_effect(resolution_id, outbox_event_id))


@celery_app.task(name="app.tasks.outbox.trigger_job")  # type: ignore[untyped-decorator]
def trigger_job(
    task_name: str,
    reference_or_event_id: int | str,
    outbox_event_id: str | None = None,
) -> int:
    """只执行固定任务白名单；event ID 提供持久化租约与重复投递幂等。"""

    if outbox_event_id is None:
        if not isinstance(reference_or_event_id, str):
            raise ValueError("invalid manual job outbox reference")
        return run_worker_async(_trigger_job(task_name, reference_or_event_id))
    if not isinstance(reference_or_event_id, int) or isinstance(
        reference_or_event_id, bool
    ):
        raise ValueError("invalid manual job outbox reference")
    return run_worker_async(
        _trigger_job(
            task_name,
            outbox_event_id,
            sign_id=reference_or_event_id,
        )
    )
