"""可跟踪模板的厂商审核状态同步任务。"""

from __future__ import annotations

from uuid import UUID

from app.core.jobtrack import tracked_job
from app.core.worker_runtime import run_worker_async
from app.services.outbox import OutboxClaim, OutboxExecutor
from app.services.outbox_repository import SqlOutboxRepository
from app.services.template_management import TemplateManagementService
from app.services.template_repository import SqlTemplateRepository
from app.settings import get_settings
from app.tasks import celery_app
from app.vendor.zhihui import ZhihuiClient


async def _sync() -> int:
    settings = get_settings()
    async with ZhihuiClient.from_settings(settings) as vendor:
        return await TemplateManagementService(
            SqlTemplateRepository(settings),
            vendor,
        ).sync_pending()


async def _sync_one(template_id: int, event_id: str) -> int:
    settings = get_settings()

    async def effect(claim: OutboxClaim) -> int:
        if claim.args != (template_id,):
            raise ValueError("template sync outbox args mismatch")
        async with ZhihuiClient.from_settings(settings) as vendor:
            return await TemplateManagementService(
                SqlTemplateRepository(settings),
                vendor,
            ).sync_pending(template_id)

    return await OutboxExecutor(SqlOutboxRepository(settings)).run(
        UUID(event_id),
        expected_type="template.sync",
        effect=effect,
    )


async def _bind(template_id: int, event_id: str) -> int:
    settings = get_settings()

    async def effect(claim: OutboxClaim) -> int:
        if claim.args != (template_id,):
            raise ValueError("template binding outbox args mismatch")
        async with ZhihuiClient.from_settings(settings) as vendor:
            return await TemplateManagementService(
                SqlTemplateRepository(settings),
                vendor,
            ).bind(template_id)

    return await OutboxExecutor(SqlOutboxRepository(settings)).run(
        UUID(event_id),
        expected_type="template.bind",
        effect=effect,
    )


@celery_app.task(name="app.tasks.sync_templates")  # type: ignore[untyped-decorator]
@tracked_job("sync_templates", expect_interval_s=600)
def sync_templates() -> int:
    return run_worker_async(_sync())


@celery_app.task(name="app.tasks.sync_template")  # type: ignore[untyped-decorator]
def sync_template(template_id: int, outbox_event_id: str) -> int:
    """只同步 Outbox 合同绑定的单个模板主键。"""

    return run_worker_async(_sync_one(template_id, outbox_event_id))


@celery_app.task(name="app.tasks.bind_template")  # type: ignore[untyped-decorator]
def bind_template(template_id: int, outbox_event_id: str) -> int:
    """只接受无敏感内容的模板主键与 Outbox event ID。"""

    return run_worker_async(_bind(template_id, outbox_event_id))
