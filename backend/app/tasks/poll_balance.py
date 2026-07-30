"""厂商余额定时巡检任务。"""

from __future__ import annotations

from app.core.jobtrack import tracked_job
from app.core.worker_runtime import run_worker_async
from app.services.alert_repository import SqlAlertService
from app.services.balance import BalanceMonitor
from app.services.balance_repository import SqlBalanceRepository
from app.settings import get_settings
from app.tasks import celery_app
from app.vendor.zhihui import ZhihuiClient


async def _poll() -> int:
    settings = get_settings()
    async with ZhihuiClient.from_settings(settings) as vendor:
        return await BalanceMonitor(
            SqlBalanceRepository(settings),
            vendor,
            SqlAlertService(settings),
        ).poll()


@celery_app.task(name="app.tasks.poll_balance")  # type: ignore[untyped-decorator]
@tracked_job("poll_balance", expect_interval_s=600)
def poll_balance() -> int:
    return run_worker_async(_poll())
