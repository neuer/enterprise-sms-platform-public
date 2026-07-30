"""待审核模板状态同步任务。"""

from __future__ import annotations

from app.core.jobtrack import tracked_job
from app.core.worker_runtime import run_worker_async
from app.services.template_management import TemplateManagementService
from app.services.template_repository import SqlTemplateRepository
from app.settings import get_settings
from app.tasks import celery_app
from app.vendor.zhihui import ZhihuiClient


async def _sync() -> int:
    settings = get_settings()
    async with ZhihuiClient.from_settings(settings) as vendor:
        return await TemplateManagementService(
            SqlTemplateRepository(settings),
            vendor,
        ).sync_pending()


@celery_app.task(name="app.tasks.sync_templates")  # type: ignore[untyped-decorator]
@tracked_job("sync_templates", expect_interval_s=600)
def sync_templates() -> int:
    return run_worker_async(_sync())
