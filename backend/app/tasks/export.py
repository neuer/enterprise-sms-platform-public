"""导出任务数据库兜底投递与密文文件生命周期清理。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.core.bounded_executor import run_bounded
from app.core.jobtrack import tracked_job
from app.core.worker_runtime import run_worker_async
from app.services.crypto import CryptoService
from app.services.export_file import ExportFileCodec
from app.services.export_reconcile import (
    ExportReconcileRepository,
    reconcile_export_storage,
)
from app.services.export_repository import SqlExportRepository
from app.settings import get_settings
from app.tasks import background_task_options, celery_app
from app.tasks.export_worker import build_export


class DispatchRepository(Protocol):
    async def pending_ids(self) -> list[int]: ...


class ExportSender(Protocol):
    async def send(self, task_id: int) -> None: ...


class CeleryExportSender:
    async def send(self, task_id: int) -> None:
        await run_bounded(
            celery_app.send_task,
            "app.tasks.build_export",
            args=[task_id],
            queue="bulk",
            ignore_result=True,
            timeout_s=3,
        )


async def dispatch_exports_once(
    repository: DispatchRepository,
    sender: ExportSender,
) -> int:
    task_ids = await repository.pending_ids()
    for task_id in task_ids:
        await sender.send(task_id)
    return len(task_ids)


async def cleanup_exports_once(
    repository: ExportReconcileRepository,
    codec: ExportFileCodec,
    *,
    now: datetime | None = None,
) -> int:
    stats = await reconcile_export_storage(repository, codec, now=now)
    return stats.total


async def _dispatch() -> int:
    return await dispatch_exports_once(SqlExportRepository(), CeleryExportSender())


async def _cleanup() -> int:
    settings = get_settings()
    return await cleanup_exports_once(
        SqlExportRepository(settings),
        ExportFileCodec(CryptoService.from_settings(settings), settings.export_storage_dir),
    )


@celery_app.task(
    name="app.tasks.dispatch_exports",
    **background_task_options(soft_time_limit=120, time_limit=150),
)  # type: ignore[untyped-decorator]
@tracked_job("dispatch_exports", expect_interval_s=60)
def dispatch_exports() -> int:
    return run_worker_async(_dispatch())


@celery_app.task(
    name="app.tasks.cleanup_exports",
    **background_task_options(soft_time_limit=300, time_limit=360),
)  # type: ignore[untyped-decorator]
@tracked_job("cleanup_exports", expect_interval_s=3600)
def cleanup_exports() -> int:
    return run_worker_async(_cleanup())


__all__ = ["build_export", "cleanup_exports", "dispatch_exports"]
