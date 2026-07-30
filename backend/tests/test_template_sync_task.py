from typing import Any, cast

from app.core.jobtrack import JobSpec
from app.tasks.template import sync_templates


def test_template_sync_task_declares_ten_minute_heartbeat() -> None:
    assert cast(Any, sync_templates).run.job_spec == JobSpec("sync_templates", 600)
