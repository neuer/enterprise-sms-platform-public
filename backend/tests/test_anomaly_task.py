from typing import Any, cast

from app.core.jobtrack import JobSpec
from app.tasks.anomaly import anomaly_scan


def test_anomaly_task_declares_default_hourly_heartbeat() -> None:
    assert cast(Any, anomaly_scan).run.job_spec == JobSpec("anomaly_scan", 3600)
