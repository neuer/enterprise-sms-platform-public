"""厂商上行回复 raw-first 定时轮询任务。"""

from __future__ import annotations

from app.core.jobtrack import tracked_job
from app.core.worker_runtime import run_worker_async
from app.services.crypto import CryptoService
from app.services.reply_ingest import ReplyIngestService
from app.services.reply_repository import SqlReplyRepository
from app.settings import get_settings
from app.tasks import celery_app
from app.vendor.zhihui import ZhihuiClient


async def _poll() -> int:
    settings = get_settings()
    async with ZhihuiClient.from_settings(settings) as vendor:
        return await ReplyIngestService(
            vendor,
            SqlReplyRepository(settings),
            CryptoService.from_settings(settings),
        ).poll_once()


@celery_app.task(name="app.tasks.poll_reply")  # type: ignore[untyped-decorator]
@tracked_job("poll_reply", expect_interval_s=300)
def poll_reply() -> int:
    """Celery 入口不携带回复、手机号或 raw payload。"""

    return run_worker_async(_poll())
