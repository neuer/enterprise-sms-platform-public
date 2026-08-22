"""厂商状态报告轮询任务。"""

from __future__ import annotations

import logging

from app.core.jobtrack import tracked_job
from app.core.redis_lock import HeartbeatLock
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

LOGGER = logging.getLogger(__name__)


async def _alert_lease_lost(alerts: SqlAlertService, source: str) -> None:
    try:
        await alerts.emit(
            alert_type="poll_lock_expired",
            level="warn",
            title="轮询锁在处理期间过期，已被其他实例接管",
            detail={"source": source},
            dedup_key=f"poll_lock_expired:{source}",
        )
    except Exception as exc:
        LOGGER.error(
            "poll lock alert unavailable",
            extra={"source": source, "error_type": type(exc).__name__},
        )


async def _poll() -> int:
    settings = get_settings()
    redis = redis_client(settings.redis_control_url)
    lock = HeartbeatLock(
        redis.lock("lock:poll:report", timeout=90, blocking_timeout=0),
        ttl_s=90,
        beat_s=30,
    )
    if not await lock.acquire():
        return 0
    gateway = None
    alerts = SqlAlertService(settings)
    try:
        gateway = ZhihuiClient.from_settings(settings)
        repository = SqlReportRepository(settings)
        count = await ReportIngestService(
            gateway,
            repository,
            CryptoService.from_settings(settings),
            alerts=alerts,
            spill=RawSpillStore.from_settings(settings),
        ).poll_once()
        await repository.expire_unknown(await repository.report_timeout_hours())
        return count
    finally:
        if gateway is not None:
            await gateway.aclose()
        if not await lock.release():
            await _alert_lease_lost(alerts, "report")


@celery_app.task(name="app.tasks.poll_report")  # type: ignore[untyped-decorator]
@tracked_job("poll_report", expect_interval_s=60)
def poll_report() -> int:
    return run_worker_async(_poll())
