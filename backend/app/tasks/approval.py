"""审批过期扫描任务。"""

from __future__ import annotations

from typing import cast

from redis.asyncio import Redis

from app.core.jobtrack import tracked_job
from app.core.worker_runtime import run_worker_async
from app.services.alert_repository import SqlAlertService
from app.services.approval import ApprovalService
from app.services.approval_repository import SqlApprovalRepository
from app.services.queue import CeleryQueuePublisher
from app.services.quota import QuotaRedis, QuotaService
from app.settings import get_settings
from app.tasks import celery_app


async def _expire() -> int:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_control_url, decode_responses=True)
    try:
        return await ApprovalService(
            SqlApprovalRepository(settings),
            QuotaService(cast(QuotaRedis, redis)),
            CeleryQueuePublisher(),
            SqlAlertService(settings),
        ).expire_due()
    finally:
        await redis.aclose()


@celery_app.task(name="app.tasks.expire_approvals")  # type: ignore[untyped-decorator]
@tracked_job("expire_approvals", expect_interval_s=300)
def expire_approvals() -> int:
    return run_worker_async(_expire())
