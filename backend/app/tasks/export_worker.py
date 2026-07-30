"""bulk 队列单个 export_task 构建入口。"""

from __future__ import annotations

from app.core.worker_runtime import run_worker_async
from app.services.crypto import CryptoService
from app.services.export_file import ExportFileCodec
from app.services.export_repository import SqlExportRepository
from app.services.export_worker import ExportWorker
from app.settings import get_settings
from app.tasks import celery_app


async def _build(task_id: int) -> int:
    settings = get_settings()
    crypto = CryptoService.from_settings(settings)
    repository = SqlExportRepository(settings)
    return await ExportWorker(
        repository,
        ExportFileCodec(crypto, settings.export_storage_dir),
        crypto,
    ).process(task_id)


@celery_app.task(name="app.tasks.build_export")  # type: ignore[untyped-decorator]
def build_export(task_id: int) -> int:
    """Celery 参数只含 export_task.id。"""

    return run_worker_async(_build(task_id))
