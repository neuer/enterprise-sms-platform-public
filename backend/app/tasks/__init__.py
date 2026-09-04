"""Celery 应用与任务注册入口。"""

import logging
import os
from importlib import import_module
from typing import Any

from celery import Celery
from celery.signals import (
    task_failure,
    task_postrun,
    task_prerun,
    worker_process_init,
    worker_process_shutdown,
)
from kombu.exceptions import (  # type: ignore[import-untyped]
    OperationalError as BrokerOperationalError,
)
from redis.exceptions import RedisError
from sqlalchemy.exc import OperationalError as DatabaseOperationalError

from app.core.bounded_executor import close_bounded_executor
from app.core.correlation import (
    TASK_HEADER,
    bind_correlation_id,
    current_correlation_id,
    parse_correlation_id,
    reset_correlation_id,
)
from app.core.runtime_resources import (
    configure_runtime_resources,
    discard_inherited_runtime_resources,
)
from app.core.worker_runtime import close_worker_runtime, start_worker_runtime
from app.settings import get_settings

settings = get_settings()
TASK_MODULES = (
    "app.tasks.send",
    "app.tasks.poll_report",
    "app.tasks.poll_reply",
    "app.tasks.reconcile",
    "app.tasks.approval",
    "app.tasks.scheduling",
    "app.tasks.scheduler",
    "app.tasks.template",
    "app.tasks.sign",
    "app.tasks.poll_balance",
    "app.tasks.anomaly",
    "app.tasks.callback",
    "app.tasks.callback_worker",
    "app.tasks.outbox",
    "app.tasks.export",
    "app.tasks.export_worker",
    "app.tasks.stats",
    "app.tasks.housekeeping",
    "app.tasks.usage_projection",
    "app.tasks.imports",
    "app.tasks.security_daily",
)
broker_url = (
    settings.redis_broker_url
    if settings.sms_component in {"worker", "beat", "background"}
    else None
)
redis_ssl_options = settings.redis_tls_options if broker_url is not None else None
app = Celery("sms_platform", broker=broker_url, backend=broker_url)
app.conf.update(
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    imports=TASK_MODULES,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_track_started=True,
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=200,
    worker_max_memory_per_child=512_000,
    task_soft_time_limit=570,
    task_time_limit=600,
    task_routes={
        "app.tasks.poll_report": {"queue": "realtime-report"},
    },
    broker_transport_options={"visibility_timeout": 3600},
    broker_use_ssl=redis_ssl_options or False,
    redis_backend_use_ssl=redis_ssl_options,
)

# 只重试可合理恢复的连接/超时故障；ProgrammingError、权限错误和业务错误必须
# 立即失败并进入 job_run/告警，避免把配置缺陷伪装成“自愈”。
TRANSIENT_TASK_ERRORS = (
    DatabaseOperationalError,
    BrokerOperationalError,
    RedisError,
    TimeoutError,
)


def background_task_options(*, soft_time_limit: int, time_limit: int) -> dict[str, Any]:
    """返回后台任务的有限重试与超时策略。"""

    if soft_time_limit <= 0 or time_limit <= soft_time_limit:
        raise ValueError("time limits must be positive and hard limit must exceed soft limit")
    return {
        "acks_late": True,
        "reject_on_worker_lost": True,
        "autoretry_for": TRANSIENT_TASK_ERRORS,
        "retry_backoff": True,
        "retry_backoff_max": 300,
        "retry_jitter": True,
        "max_retries": 3,
        "soft_time_limit": soft_time_limit,
        "time_limit": time_limit,
    }


celery_app = app
_task_correlation_tokens: dict[str, Any] = {}
LOGGER = logging.getLogger(__name__)

from app.tasks.scheduler import (  # noqa: E402
    STARTUP_SCHEDULE_ENV,
    decode_startup_schedule,
)

startup_schedule = decode_startup_schedule(os.environ.get(STARTUP_SCHEDULE_ENV))
if startup_schedule:
    app.conf.beat_schedule = startup_schedule


@task_prerun.connect  # type: ignore[untyped-decorator]
def bind_task_correlation(task_id: str | None = None, task: Any = None, **_: object) -> None:
    """优先使用生产者消息头；旧消息以 Celery task ID 建立独立关联。"""

    headers = getattr(getattr(task, "request", None), "headers", None) or {}
    candidate = headers.get(TASK_HEADER)
    correlation_id, token = bind_correlation_id(candidate or task_id)
    if task_id is not None:
        _task_correlation_tokens[task_id] = token
    if task is not None:
        task.request.correlation_id = str(correlation_id)


@task_postrun.connect  # type: ignore[untyped-decorator]
def reset_task_correlation(task_id: str | None = None, **_: object) -> None:
    """任务结束即清理 ContextVar，杜绝同一 worker 后续任务继承。"""

    if task_id is None:
        return
    token = _task_correlation_tokens.pop(task_id, None)
    if token is not None:
        reset_correlation_id(token)


@task_failure.connect  # type: ignore[untyped-decorator]
def log_task_failure(
    task_id: str | None = None,
    exception: BaseException | None = None,
    traceback: Any = None,
    sender: Any = None,
    **_: object,
) -> None:
    """未知任务异常记录堆栈和安全关联字段，不记录任务参数。"""

    request_id = getattr(getattr(sender, "request", None), "correlation_id", None)
    correlation_id = parse_correlation_id(request_id) or current_correlation_id()
    LOGGER.error(
        "unhandled_task_exception",
        extra={
            "correlation_id": str(correlation_id),
            "task_id": task_id,
            "task_name": getattr(sender, "name", None),
            "error_type": type(exception).__name__ if exception is not None else "Exception",
        },
        exc_info=((type(exception), exception, traceback) if exception is not None else None),
    )


@worker_process_shutdown.connect  # type: ignore[untyped-decorator]
def close_worker_resources(**_: object) -> None:
    """prefork 子进程退出时关闭共享连接池、HTTP 辅助线程与执行器。"""

    close_worker_runtime()
    close_bounded_executor()


@worker_process_init.connect  # type: ignore[untyped-decorator]
def initialize_worker_resources(**_: object) -> None:
    """fork 后建立子进程独立 loop；父进程连接绝不进入 worker。"""

    discard_inherited_runtime_resources()
    configure_runtime_resources(get_settings(), component="worker")
    start_worker_runtime()
    from app.services.runtime_heartbeat import start_send_worker_heartbeats

    start_send_worker_heartbeats()


def register_task_modules() -> None:
    """让 API 心跳巡检加载与 Celery worker 相同的 tracked_job 声明。"""

    for module_name in TASK_MODULES:
        import_module(module_name)


__all__ = ["TASK_MODULES", "app", "celery_app", "register_task_modules"]
