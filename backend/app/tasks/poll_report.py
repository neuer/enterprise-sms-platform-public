"""厂商状态报告轮询任务。"""

from __future__ import annotations

from app.core.jobtrack import tracked_job
from app.core.runtime_resources import redis_client
from app.core.worker_runtime import run_worker_async
from app.services.alert_repository import SqlAlertService
from app.services.crypto import CryptoService
from app.services.raw_spill import RawSpillStore
from app.services.report_ingest import ReportIngestService
from app.services.report_repository import SqlReportRepository
from app.settings import get_settings
from app.tasks import celery_app
from app.vendor.zhihui import ZhihuiClient


async def _poll() -> int:
    settings = get_settings()
    redis = redis_client(settings.redis_control_url)
    lock = redis.lock("lock:poll:report", timeout=90, blocking_timeout=0)
    if not await lock.acquire(blocking=False):
        return 0
    gateway = None
    try:
        gateway = ZhihuiClient.from_settings(settings)
        repository = SqlReportRepository(settings)
        count = await ReportIngestService(
            gateway,
            repository,
            CryptoService.from_settings(settings),
            alerts=SqlAlertService(settings),
            spill=RawSpillStore(settings.raw_spill_dir),
        ).poll_once()
        await repository.expire_unknown(await repository.report_timeout_hours())
        return count
    finally:
        if gateway is not None:
            await gateway.aclose()
        await lock.release()


@celery_app.task(name="app.tasks.poll_report")  # type: ignore[untyped-decorator]
@tracked_job("poll_report", expect_interval_s=60)
def poll_report() -> int:
    return run_worker_async(_poll())
