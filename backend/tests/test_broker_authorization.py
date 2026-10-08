"""后台职责矩阵与消费入口必须同时失败关闭。"""

from __future__ import annotations

from itertools import product
from types import SimpleNamespace
from typing import Any

import pytest
from celery.exceptions import Reject

from app.core.broker_authorization import (
    PRODUCER_ROLES,
    TASK_ROLES,
    WORKER_QUEUES,
    BrokerTask,
    TaskBoundaryError,
    broker_transport_options,
    check_task_boundary,
)

pytestmark = pytest.mark.authorization


@pytest.mark.parametrize("role,task", list(product(WORKER_QUEUES, TASK_ROLES)))
def test_worker_task_matrix(role: str, task: str) -> None:
    args = (
        next(
            name
            for name, roles in TASK_ROLES.items()
            if role in roles and name != "app.tasks.outbox.trigger_job"
        ),
    )
    if role in TASK_ROLES[task]:
        check_task_boundary(role, task, args=args, kwargs={}, routing_key=WORKER_QUEUES[role])
    else:
        with pytest.raises(TaskBoundaryError):
            check_task_boundary(role, task, args=args, kwargs={}, routing_key=WORKER_QUEUES[role])


@pytest.mark.parametrize("role", ["", "unknown", *PRODUCER_ROLES])
def test_publishers_and_unknown_roles_cannot_execute(role: str) -> None:
    with pytest.raises(TaskBoundaryError):
        check_task_boundary(
            role, "app.tasks.send.process_chunk", args=(1,), kwargs={}, routing_key="realtime"
        )


@pytest.mark.parametrize(
    "target",
    [
        "app.tasks.send.process_chunk",
        "app.tasks.outbox.trigger_job",
        "celery.accumulate",
        "unknown",
        None,
    ],
)
def test_manual_wrapper_cannot_expand_callback_capability(target: object) -> None:
    with pytest.raises(TaskBoundaryError):
        check_task_boundary(
            "callback",
            "app.tasks.outbox.trigger_job",
            args=(),
            kwargs={"task_name": target},
            routing_key="callback",
        )


def test_delivery_queue_and_builtin_task_are_not_authority() -> None:
    for name, queue in (
        ("app.tasks.dispatch_callbacks", "realtime"),
        ("celery.accumulate", "callback"),
    ):
        with pytest.raises(TaskBoundaryError):
            check_task_boundary("callback", name, args=(), kwargs={}, routing_key=queue)


def test_all_registered_application_tasks_inherit_guard_and_have_policy() -> None:
    from app.tasks import celery_app, register_task_modules

    register_task_modules()
    actual = {name for name in celery_app.tasks if name.startswith("app.tasks.")}
    assert actual == set(TASK_ROLES)
    for name in celery_app.tasks:
        assert isinstance(celery_app.tasks[name], BrokerTask)
    assert celery_app.conf.task_ignore_result is True
    assert celery_app.conf.task_store_errors_even_if_ignored is False
    assert celery_app.conf.worker_enable_remote_control is False
    assert celery_app.conf.task_create_missing_queues is False
    from app.tasks.scheduler import build_beat_schedule

    for item in build_beat_schedule({}).values():
        task, queue = item["task"], item["options"]["queue"]
        role = next(key for key, value in WORKER_QUEUES.items() if value == queue)
        check_task_boundary(role, task, args=(), kwargs={}, routing_key=queue)


def test_confirmation_namespaces_do_not_overlap() -> None:
    options = [broker_transport_options(role) for role in WORKER_QUEUES]
    for key in ("unacked_key", "unacked_index_key", "unacked_mutex_key"):
        assert len({item[key] for item in options}) == len(options)
    assert all(item["priority_steps"] == [0] for item in options)
    assert broker_transport_options("")["unacked_key"] == "unacked:unconfigured"
    with pytest.raises(ValueError):
        broker_transport_options("unknown")


@pytest.mark.parametrize("denied", ["task", "queue", "canvas", "component", "role"])
def test_celery_execution_hook_rejects_before_business_code(
    monkeypatch: pytest.MonkeyPatch,
    denied: str,
) -> None:
    from app import settings as module

    config = SimpleNamespace(
        sms_component="worker", environment="test", redis_broker_role="callback"
    )
    monkeypatch.setattr(module, "get_settings", lambda: config)
    task = BrokerTask()
    task.name = "app.tasks.dispatch_callbacks"
    request: dict[str, Any] = {"delivery_info": {"routing_key": "callback"}}
    if denied == "task":
        task.name = "app.tasks.send.process_chunk"
    elif denied == "queue":
        request["delivery_info"] = {"routing_key": "realtime"}
    elif denied == "canvas":
        request["chain"] = [{"task": "app.tasks.send.process_chunk"}]
    elif denied == "component":
        config.sms_component = "api"
    else:
        config.redis_broker_role = ""
    from app.tasks import celery_app

    task.bind(celery_app)
    task.push_request(**request)
    try:
        with pytest.raises(Reject) as caught:
            task.before_start("synthetic-task", (), {})
        assert caught.value.requeue is False
    finally:
        task.pop_request()


def test_allowed_hook_and_explicit_eager_test_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import settings as module
    from app.tasks import celery_app

    config = SimpleNamespace(
        sms_component="worker", environment="test", redis_broker_role="callback"
    )
    monkeypatch.setattr(module, "get_settings", lambda: config)
    task = BrokerTask()
    task.name = "app.tasks.dispatch_callbacks"
    task.bind(celery_app)
    task.push_request(delivery_info={"routing_key": "callback"})
    try:
        task.before_start("synthetic-task", (), {})
        config.sms_component = "api"
        task.request.is_eager = True
        task.before_start("synthetic-task", (), {})
        config.environment = "production"
        with pytest.raises(Reject):
            task.before_start("synthetic-task", (), {})
    finally:
        task.pop_request()
