"""业务数据生命周期每日清理任务。"""

from __future__ import annotations

from app.core.jobtrack import tracked_job
from app.core.worker_runtime import run_worker_async
from app.services.housekeeping import HousekeepingService, ImportFileStore
from app.services.housekeeping_repository import SqlHousekeepingRepository
from app.settings import get_settings
from app.tasks import background_task_options, celery_app


async def _run() -> int:
    settings = get_settings()
    result = await HousekeepingService(
        SqlHousekeepingRepository(settings),
        ImportFileStore(settings.import_storage_dir),
    ).run()
    return result.total


@celery_app.task(
    name="app.tasks.housekeeping",
    **background_task_options(soft_time_limit=900, time_limit=960),
)  # type: ignore[untyped-decorator]
@tracked_job("housekeeping", expect_interval_s=86400)
def housekeeping() -> int:
    return run_worker_async(_run())


__all__ = ["housekeeping"]
