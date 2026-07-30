from __future__ import annotations

from typing import Any, cast

from app.core.jobtrack import JOB_SPECS, JobSpec
from app.tasks.scheduler import build_beat_schedule


def test_housekeeping_task_is_registered_tracked_and_fixed_to_bulk_queue() -> None:
    from app.tasks.housekeeping import housekeeping

    assert cast(Any, housekeeping).name == "app.tasks.housekeeping"
    assert JOB_SPECS["housekeeping"] == JobSpec("housekeeping", 86400)
    assert build_beat_schedule({})["housekeeping"] == {
        "task": "app.tasks.housekeeping",
        "schedule": 86400,
        "options": {"queue": "bulk"},
    }
