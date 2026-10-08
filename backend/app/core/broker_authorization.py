"""后台任务职责边界：固定队列、独立确认空间和消费端白名单。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from celery import Task
from celery.exceptions import Reject

WORKER_QUEUES = {
    "realtime": "realtime",
    "report": "realtime-report",
    "bulk": "bulk",
    "callback": "callback",
}
PRODUCER_ROLES = frozenset({"beat", "dispatcher"})
TASK_ROLES: dict[str, frozenset[str]] = {
    **dict.fromkeys(
        (
            "app.tasks.anomaly_scan",
            "app.tasks.expire_approvals",
            "app.tasks.expire_report_timeouts",
            "app.tasks.poll_balance",
            "app.tasks.poll_reply",
            "app.tasks.reconcile",
            "app.tasks.dispatch_scheduled",
            "app.tasks.sync_signs",
            "app.tasks.bind_sign",
            "app.tasks.adopt_sign",
            "app.tasks.sync_templates",
            "app.tasks.sync_template",
            "app.tasks.bind_template",
            "app.tasks.reconcile_usage_projection",
            "app.tasks.outbox.compensate_quota",
            "app.tasks.outbox.release_usage",
            "app.tasks.outbox.apply_uncertain_effect",
        ),
        frozenset({"realtime"}),
    ),
    **dict.fromkeys(
        (
            "app.tasks.dispatch_exports",
            "app.tasks.cleanup_exports",
            "app.tasks.build_export",
            "app.tasks.process_import",
            "app.tasks.dispatch_imports",
            "app.tasks.housekeeping",
            "app.tasks.aggregate_stats",
            "app.tasks.security_daily_generate",
        ),
        frozenset({"bulk"}),
    ),
    **dict.fromkeys(
        (
            "app.tasks.dispatch_callbacks",
            "app.tasks.deliver_callback",
            "app.tasks.outbox.deliver_alert",
        ),
        frozenset({"callback"}),
    ),
    "app.tasks.poll_report": frozenset({"report"}),
    "app.tasks.send.process_batch": frozenset({"realtime", "bulk"}),
    "app.tasks.send.process_chunk": frozenset({"realtime", "bulk"}),
    "app.tasks.outbox.trigger_job": frozenset(WORKER_QUEUES),
}


class TaskBoundaryError(ValueError):
    """仅输出固定错误，不回显队列载荷、任务参数或凭据。"""


def check_task_boundary(
    role: str,
    task_name: str,
    *,
    args: Sequence[object],
    kwargs: Mapping[str, object],
    routing_key: object,
) -> None:
    """主体来自部署配置；消息自报队列或嵌套任务不能扩张执行能力。"""

    if (
        role not in WORKER_QUEUES
        or role not in TASK_ROLES.get(task_name, frozenset())
        or routing_key != WORKER_QUEUES[role]
    ):
        raise TaskBoundaryError("task is outside this worker capability")
    if task_name == "app.tasks.outbox.trigger_job":
        target = args[0] if args else kwargs.get("task_name")
        if (
            not isinstance(target, str)
            or target == task_name
            or role not in TASK_ROLES.get(target, frozenset())
        ):
            raise TaskBoundaryError("manual job is outside this worker capability")


def broker_transport_options(role: str) -> dict[str, Any]:
    """各消费者确认/可见性恢复记录独立，避免低权限记录被其他消费者恢复。"""

    if role and role not in {*WORKER_QUEUES, *PRODUCER_ROLES}:
        raise ValueError("invalid broker capability")
    namespace = role or "unconfigured"
    return {
        "visibility_timeout": 3600,
        # 业务优先级由 realtime/bulk 两条队列承担，不创建隐式优先级后缀队列。
        "priority_steps": [0],
        "unacked_key": f"unacked:{namespace}",
        "unacked_index_key": f"unacked_index:{namespace}",
        "unacked_mutex_key": f"unacked_mutex:{namespace}",
    }


class BrokerTask(Task):  # type: ignore[misc]
    """Celery 真正执行任务前拒绝职责外任务；不能使用会吞异常的 signal 代替。"""

    abstract = True

    def before_start(self, task_id: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        from app.settings import get_settings

        settings = get_settings()
        if (
            self.request.is_eager
            and settings.environment in {"development", "test"}
            and settings.sms_component != "worker"
        ):
            return
        if settings.sms_component != "worker":
            raise Reject("task execution requires a worker capability", requeue=False)
        try:
            check_task_boundary(
                settings.redis_broker_role,
                self.name,
                args=args,
                kwargs=kwargs,
                routing_key=(self.request.delivery_info or {}).get("routing_key"),
            )
            # 当前平台不使用 canvas。拒绝消息夹带的链式任务，防止跨能力后续调用。
            if any(
                (
                    self.request.callbacks,
                    self.request.errbacks,
                    self.request.chain,
                    self.request.chord,
                )
            ):
                raise TaskBoundaryError("task canvas is not permitted")
        except TaskBoundaryError:
            raise Reject("task capability denied", requeue=False) from None
