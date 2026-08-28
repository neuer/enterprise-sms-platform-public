"""待审核签名状态同步任务。"""

from __future__ import annotations

from uuid import UUID

from app.core.jobtrack import tracked_job
from app.core.worker_runtime import run_worker_async
from app.services.outbox import OutboxClaim, OutboxExecutor
from app.services.outbox_repository import SqlOutboxRepository
from app.services.sign_management import SignManagementService
from app.services.sign_repository import SqlSignRepository
from app.settings import get_settings
from app.tasks import celery_app
from app.vendor.zhihui import ZhihuiClient


async def _sync() -> int:
    settings = get_settings()
    async with ZhihuiClient.from_settings(settings) as vendor:
        return await SignManagementService(SqlSignRepository(settings), vendor).sync_pending()


async def _bind(sign_id: int, event_id: str) -> int:
    settings = get_settings()

    async def effect(claim: OutboxClaim) -> int:
        if claim.args != (sign_id,):
            raise ValueError("sign binding outbox args mismatch")
        async with ZhihuiClient.from_settings(settings) as vendor:
            return await SignManagementService(
                SqlSignRepository(settings),
                vendor,
            ).bind(sign_id)

    return await OutboxExecutor(SqlOutboxRepository(settings)).run(
        UUID(event_id),
        expected_type="sign.bind",
        effect=effect,
    )


async def _adopt(sign_id: int, vendor_sign_id: int, event_id: str) -> int:
    settings = get_settings()

    async def effect(claim: OutboxClaim) -> int:
        if claim.args != (sign_id, vendor_sign_id):
            raise ValueError("sign adoption outbox args mismatch")
        async with ZhihuiClient.from_settings(settings) as vendor:
            return await SignManagementService(
                SqlSignRepository(settings),
                vendor,
            ).adopt_existing(sign_id, vendor_sign_id)

    return await OutboxExecutor(SqlOutboxRepository(settings)).run(
        UUID(event_id),
        expected_type="sign.adopt",
        effect=effect,
    )


@celery_app.task(name="app.tasks.sync_signs")  # type: ignore[untyped-decorator]
@tracked_job("sync_signs", expect_interval_s=600)
def sync_signs() -> int:
    return run_worker_async(_sync())


@celery_app.task(name="app.tasks.bind_sign")  # type: ignore[untyped-decorator]
def bind_sign(sign_id: int, outbox_event_id: str) -> int:
    """只接受无敏感内容的签名主键与 Outbox event ID。"""

    return run_worker_async(_bind(sign_id, outbox_event_id))


@celery_app.task(name="app.tasks.adopt_sign")  # type: ignore[untyped-decorator]
def adopt_sign(sign_id: int, vendor_sign_id: int, outbox_event_id: str) -> int:
    """只接受本地签名主键、正整数厂商 ID 与 Outbox event ID。"""

    return run_worker_async(_adopt(sign_id, vendor_sign_id, outbox_event_id))
