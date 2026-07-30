"""定时批次到点投递任务。"""

from __future__ import annotations

from typing import cast

from redis.asyncio import Redis

from app.core.jobtrack import tracked_job
from app.core.worker_runtime import run_worker_async
from app.services.queue import CeleryQueuePublisher
from app.services.quota import QuotaRedis, QuotaService
from app.services.scheduling import SchedulingService
from app.services.scheduling_repository import SqlSchedulingRepository
from app.settings import get_settings
from app.tasks import celery_app


async def _dispatch_due() -> int:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_control_url, decode_responses=True)
    try:
        return await SchedulingService(
            SqlSchedulingRepository(settings),
            QuotaService(cast(QuotaRedis, redis)),
            CeleryQueuePublisher(),
        ).dispatch_due()
    finally:
        await redis.aclose()


@celery_app.task(name="app.tasks.dispatch_scheduled")  # type: ignore[untyped-decorator]
@tracked_job("dispatch_scheduled", expect_interval_s=60)
def dispatch_scheduled() -> int:
    return run_worker_async(_dispatch_due())
