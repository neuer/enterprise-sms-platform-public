"""callback_task PostgreSQL due 扫描与 callback 队列投递。"""

from __future__ import annotations

from app.core.bounded_executor import run_bounded
from app.core.jobtrack import tracked_job
from app.core.worker_runtime import run_worker_async
from app.services.callback_repository import SqlCallbackRepository
from app.tasks import celery_app
from app.tasks.callback_worker import deliver_callback


async def _dispatch() -> int:
    task_ids = await SqlCallbackRepository().due_ids()
    for task_id in task_ids:
        await run_bounded(
            celery_app.send_task,
            "app.tasks.deliver_callback",
            args=[task_id],
            queue="callback",
            ignore_result=True,
            timeout_s=3,
        )
    return len(task_ids)


@celery_app.task(name="app.tasks.dispatch_callbacks")  # type: ignore[untyped-decorator]
@tracked_job("dispatch_callbacks", expect_interval_s=30)
def dispatch_callbacks() -> int:
    return run_worker_async(_dispatch())


__all__ = ["deliver_callback", "dispatch_callbacks"]
