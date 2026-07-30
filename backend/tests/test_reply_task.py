from typing import Any, cast

from app.core.jobtrack import JobSpec
from app.tasks.poll_reply import poll_reply


def test_reply_task_declares_default_five_minute_heartbeat() -> None:
    assert cast(Any, poll_reply).run.job_spec == JobSpec("poll_reply", 300)
