from app.core.jobtrack import JOB_SPECS
from app.tasks import TASK_MODULES, register_task_modules


def test_api_can_register_same_tracked_task_modules_as_celery_workers() -> None:
    register_task_modules()

    assert "app.tasks.stats" in TASK_MODULES
    assert "app.tasks.imports" in TASK_MODULES
    assert {
        "poll_report",
        "reconcile",
        "aggregate_stats",
        "dispatch_imports",
    } <= JOB_SPECS.keys()
