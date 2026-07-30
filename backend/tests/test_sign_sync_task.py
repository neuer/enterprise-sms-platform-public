from typing import Any, cast

from app.core.jobtrack import JobSpec
from app.tasks.sign import sync_signs


def test_sign_sync_task_declares_ten_minute_heartbeat() -> None:
    assert cast(Any, sync_signs).run.job_spec == JobSpec("sync_signs", 600)
