"""应用×类别发送量异常定时扫描任务。"""

from __future__ import annotations

from typing import Any

from redis.asyncio import Redis

from app.core.jobtrack import tracked_job
from app.core.worker_runtime import run_worker_async
from app.services.alert_repository import SqlAlertService
from app.services.anomaly import AnomalyService
from app.services.anomaly_repository import SqlAnomalyRepository
from app.settings import get_settings
from app.tasks import background_task_options, celery_app


async def _scan() -> int:
    settings = get_settings()
    redis: Any = Redis.from_url(settings.redis_control_url, decode_responses=True)
    try:
        return await AnomalyService(
            SqlAnomalyRepository(settings, redis),
            SqlAlertService(settings),
        ).scan()
    finally:
        await redis.aclose()


@celery_app.task(
    name="app.tasks.anomaly_scan",
    **background_task_options(soft_time_limit=120, time_limit=150),
)  # type: ignore[untyped-decorator]
@tracked_job("anomaly_scan", expect_interval_s=3600)
def anomaly_scan() -> int:
    """Celery 同步入口不携带任何应用数据或 PII。"""

    return run_worker_async(_scan())
