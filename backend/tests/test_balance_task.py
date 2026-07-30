from typing import Any, cast

from app.core.jobtrack import JobSpec
from app.tasks.poll_balance import poll_balance


def test_balance_task_declares_default_ten_minute_heartbeat() -> None:
    assert cast(Any, poll_balance).run.job_spec == JobSpec("poll_balance", 600)
