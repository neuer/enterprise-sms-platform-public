"""bulk 后台工作共享单个非阻塞准入槽，给发送保留 prefork 执行资源。"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from functools import wraps

from celery import current_task

from app.core.bounded_executor import BoundedWorkScope, bounded_work_scope
from app.core.jobtrack import SqlJobMonitorLease
from app.core.worker_runtime import (
    WorkerCancellationPending,
    defer_worker_cleanup,
    run_worker_async,
)

_ACTIVE: ContextVar[bool] = ContextVar("bulk_background_admitted", default=False)


class BulkTaskBusy(RuntimeError):
    """已有 bulk 后台任务运行；延期前未领取任何业务事实。"""


class SqlBulkTaskLease(SqlJobMonitorLease):
    """复用专用 PG 会话 advisory 锁；进程/连接退出即释放，无过期尾部。"""

    LOCK_NAME = "sms-platform:bulk-background-admission"


def bulk_task_admission[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    """在 tracked_job 外层准入；争用时由 Celery 延期，既有业务租约保持权威。"""

    @wraps(function)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        if _ACTIVE.get():
            return function(*args, **kwargs)
        with bounded_work_scope() as work:
            return admitted(work, *args, **kwargs)

    def admitted(work: BoundedWorkScope, *args: P.args, **kwargs: P.kwargs) -> R:
        lease = SqlBulkTaskLease()

        async def release_after_work() -> None:
            await work.wait_finished()
            await lease.release()

        token = None
        release_inline = True
        try:
            if not run_worker_async(lease.try_acquire()):
                # 只把稳定异常类型交给 Celery；不在执行槽内等待锁，不记录业务成功。
                busy = BulkTaskBusy("bulk background slot busy")
                if not current_task:
                    # 手动 job.trigger 通过 OutboxExecutor 调用 task.run，无 Celery task 上下文。
                    # 异常交回现有 Outbox 失败/重投租约，仍不能把本次未执行记为成功。
                    raise busy
                raise current_task.retry(exc=busy, countdown=30, max_retries=60)
            token = _ACTIVE.set(True)
            return function(*args, **kwargs)
        except WorkerCancellationPending as error:
            release_inline = False
            defer_worker_cleanup(error, release_after_work())
            raise
        finally:
            if token is not None:
                _ACTIVE.reset(token)
            if release_inline:
                if work.has_pending:
                    defer_worker_cleanup(None, release_after_work())
                else:
                    run_worker_async(lease.release())

    return wrapper
