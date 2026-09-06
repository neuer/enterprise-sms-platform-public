"""本地回执超时扫描任务：不依赖供应商拉取或 report worker。"""

from __future__ import annotations

from app.core.jobtrack import tracked_job
from app.core.worker_runtime import run_worker_async
from app.services.report_timeout import ReportTimeoutService
from app.tasks import celery_app


async def _expire() -> int:
    result = await ReportTimeoutService().expire_due_reports()
    return result.messages_changed


@celery_app.task(name="app.tasks.expire_report_timeouts")  # type: ignore[untyped-decorator]
@tracked_job("expire_report_timeouts", expect_interval_s=60)
def expire_report_timeouts() -> int:
    return run_worker_async(_expire())
