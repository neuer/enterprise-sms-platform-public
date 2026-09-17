"""业务数据生命周期每日清理任务。"""

from __future__ import annotations

from app.core.bounded_executor import run_bounded
from app.core.bulk_admission import bulk_task_admission
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
    if result.has_more:
        # 本轮提交后释放 bulk 槽；下一轮重新读取剩余事实，重复投递亦幂等。
        await run_bounded(
            celery_app.send_task,
            "app.tasks.housekeeping",
            queue="bulk",
            countdown=30,
            ignore_result=True,
            timeout_s=3,
        )
    return result.total


@celery_app.task(
    name="app.tasks.housekeeping",
    **background_task_options(soft_time_limit=900, time_limit=960),
)  # type: ignore[untyped-decorator]
@bulk_task_admission
@tracked_job("housekeeping", expect_interval_s=86400)
def housekeeping() -> int:
    return run_worker_async(_run())


__all__ = ["housekeeping"]
