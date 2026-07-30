"""待审核签名状态同步任务。"""

from __future__ import annotations

from app.core.jobtrack import tracked_job
from app.core.worker_runtime import run_worker_async
from app.services.sign_management import SignManagementService
from app.services.sign_repository import SqlSignRepository
from app.settings import get_settings
from app.tasks import celery_app
from app.vendor.zhihui import ZhihuiClient


async def _sync() -> int:
    settings = get_settings()
    async with ZhihuiClient.from_settings(settings) as vendor:
        return await SignManagementService(SqlSignRepository(settings), vendor).sync_pending()


@celery_app.task(name="app.tasks.sync_signs")  # type: ignore[untyped-decorator]
@tracked_job("sync_signs", expect_interval_s=600)
def sync_signs() -> int:
    return run_worker_async(_sync())
