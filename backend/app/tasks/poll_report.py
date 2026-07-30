"""厂商状态报告轮询任务。"""

from __future__ import annotations

from app.core.jobtrack import tracked_job
from app.core.worker_runtime import run_worker_async
from app.services.alert_repository import SqlAlertService
from app.services.crypto import CryptoService
from app.services.report_ingest import ReportIngestService
from app.services.report_repository import SqlReportRepository
from app.settings import get_settings
from app.tasks import celery_app
from app.vendor.zhihui import ZhihuiClient


async def _poll() -> int:
    settings = get_settings()
    gateway = ZhihuiClient.from_settings(settings)
    repository = SqlReportRepository(settings)
    try:
        count = await ReportIngestService(
            gateway,
            repository,
            CryptoService.from_settings(settings),
            alerts=SqlAlertService(settings),
        ).poll_once()
        await repository.expire_unknown(await repository.report_timeout_hours())
        return count
    finally:
        await gateway.aclose()


@celery_app.task(name="app.tasks.poll_report")  # type: ignore[untyped-decorator]
@tracked_job("poll_report", expect_interval_s=60)
def poll_report() -> int:
    return run_worker_async(_poll())
