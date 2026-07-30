from typing import Any, cast

from app.core.jobtrack import JobSpec
from app.tasks.callback import deliver_callback, dispatch_callbacks


def test_callback_dispatcher_is_tracked_and_delivery_uses_reference_only_signature() -> None:
    assert cast(Any, dispatch_callbacks).run.job_spec == JobSpec("dispatch_callbacks", 30)
    assert list(cast(Any, deliver_callback).run.__annotations__) == [
        "task_id",
        "outbox_event_id",
        "return",
    ]
