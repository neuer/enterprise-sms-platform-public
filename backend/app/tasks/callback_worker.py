"""独立 callback 队列的单任务投递入口。"""

from __future__ import annotations

from functools import lru_cache
from uuid import UUID

from app.core.worker_runtime import run_worker_async
from app.services.alert_repository import SqlAlertService
from app.services.app_management import CallbackUrlValidator
from app.services.callback import (
    CallbackDelivery,
    HttpxCallbackTransport,
    build_callback_ssl_context,
)
from app.services.callback_repository import SqlCallbackRepository
from app.services.callback_worker import CallbackWorker
from app.services.crypto import CryptoService
from app.services.outbox import OutboxClaim, OutboxExecutor
from app.services.outbox_repository import SqlOutboxRepository
from app.services.runtime_policy import SqlRuntimePolicyLoader
from app.settings import get_settings
from app.tasks import celery_app


@lru_cache
def _callback_transport() -> HttpxCallbackTransport:
    """每个 callback worker 进程复用一个有界连接池。"""

    settings = get_settings()
    return HttpxCallbackTransport(
        ssl_context=build_callback_ssl_context(
            ca_file=settings.callback_ca_certs_file,
            cert_file=settings.callback_mtls_cert_file,
            key_file=settings.callback_mtls_key_file,
        )
    )


async def _deliver(task_id: int) -> int:
    settings = get_settings()
    policy = await SqlRuntimePolicyLoader(settings).load()
    repository = SqlCallbackRepository(settings)
    validator = CallbackUrlValidator(
        policy.callback_allow_cidrs,
        allow_http=settings.environment != "production",
    )
    return await CallbackWorker(
        repository,
        CallbackDelivery(
            repository,
            CryptoService.from_settings(settings),
            validator,
            _callback_transport(),
            timeout_s=policy.callback_timeout_seconds,
        ),
        SqlAlertService(settings),
        retry_delays_s=policy.callback_retry_schedule,
    ).process(task_id)


async def _deliver_event(task_id: int, event_id: str) -> int:
    async def effect(claim: OutboxClaim) -> int:
        if claim.args != (task_id,):
            raise ValueError("callback outbox args mismatch")
        return await _deliver(task_id)

    return await OutboxExecutor(SqlOutboxRepository()).run(
        UUID(event_id),
        expected_type="callback.ready",
        effect=effect,
    )


@celery_app.task(name="app.tasks.deliver_callback")  # type: ignore[untyped-decorator]
def deliver_callback(task_id: int, outbox_event_id: str | None = None) -> int:
    """新任务携带 callback_task.id+event ID；单参数旧合同仍兼容。"""

    if outbox_event_id is None:
        return run_worker_async(_deliver(task_id))
    return run_worker_async(_deliver_event(task_id, outbox_event_id))
