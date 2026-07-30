"""配额/频控事实投影漂移巡检与崩溃预留恢复。"""

from __future__ import annotations

from redis.asyncio import Redis

from app.core.jobtrack import tracked_job
from app.core.worker_runtime import run_worker_async
from app.services.usage_ledger import UsageLedgerService
from app.settings import get_settings
from app.tasks import celery_app


async def _reconcile() -> int:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_control_url, decode_responses=True)
    try:
        service = UsageLedgerService(redis, settings, pooled=False)
        recovered = await service.recover_orphans()
        drift = await service.measure_drift()
        return recovered + drift.mismatches
    finally:
        await redis.aclose()


@celery_app.task(name="app.tasks.reconcile_usage_projection")  # type: ignore[untyped-decorator]
@tracked_job("usage_projection_reconcile", expect_interval_s=300)
def reconcile_usage_projection() -> int:
    """只输出聚合数量；手机号 HMAC 维度不进入任务结果或日志。"""

    return run_worker_async(_reconcile())


__all__ = ["reconcile_usage_projection"]
