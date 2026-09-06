from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.jobtrack import JOB_SPECS, JobHealthMonitor, JobRunSnapshot, JobSpec
from app.services.report_timeout import (
    ReportTimeoutService,
    parse_report_timeout_hours,
    validate_sweep_bounds,
)
from app.services.runtime_policy import InvalidRuntimePolicy
from app.tasks import celery_app, register_task_modules
from app.tasks.scheduler import apply_job_interval_overrides, build_beat_schedule

ROOT = Path(__file__).resolve().parents[2]


def test_timeout_task_builds_no_vendor_or_raw_spill_dependencies() -> None:
    timeout_src = (
        Path(__file__).resolve().parents[1] / "app" / "services" / "report_timeout.py"
    ).read_text(encoding="utf-8")
    task_src = (
        Path(__file__).resolve().parents[1] / "app" / "tasks" / "expire_report_timeouts.py"
    ).read_text(encoding="utf-8")
    poll_src = (
        Path(__file__).resolve().parents[1] / "app" / "tasks" / "poll_report.py"
    ).read_text(encoding="utf-8")
    for source in (timeout_src, task_src):
        assert "ZhihuiClient" not in source
        assert "RawSpillStore" not in source
        assert "lock:poll:report" not in source
        assert "poll_once" not in source
    assert "expire_unknown" not in poll_src
    assert "expire_due" not in poll_src
    ReportTimeoutService(
        repository=SimpleNamespace(),  # type: ignore[arg-type]
        settings=SimpleNamespace(database_url="postgresql+asyncpg://unused"),
    )


def test_timeout_task_has_independent_schedule_and_consumer() -> None:
    register_task_modules()
    schedule = build_beat_schedule({})
    item = schedule["expire-report-timeouts"]
    assert item["task"] == "app.tasks.expire_report_timeouts"
    assert item["schedule"] == 60
    assert item["options"]["queue"] == "realtime"
    assert celery_app.conf.task_routes.get("app.tasks.expire_report_timeouts") != {
        "queue": "realtime-report"
    }
    compose = (ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")
    assert "celery -A app.tasks worker -Q realtime " in compose
    assert "celery -A app.tasks worker -Q realtime-report -c 1" in compose
    assert JOB_SPECS["expire_report_timeouts"] == JobSpec("expire_report_timeouts", 60)


def test_report_timeout_hours_policy_is_fail_closed() -> None:
    with pytest.raises(InvalidRuntimePolicy, match="缺失"):
        parse_report_timeout_hours(None)
    with pytest.raises(InvalidRuntimePolicy, match="正整数"):
        parse_report_timeout_hours("0")
    with pytest.raises(InvalidRuntimePolicy, match="正整数"):
        parse_report_timeout_hours("abc")
    with pytest.raises(InvalidRuntimePolicy, match="720"):
        parse_report_timeout_hours("721")
    assert parse_report_timeout_hours("48") == 48
    with pytest.raises(InvalidRuntimePolicy, match="batch_limit"):
        validate_sweep_bounds(batch_limit=0)
    with pytest.raises(InvalidRuntimePolicy, match="max_round_seconds"):
        validate_sweep_bounds(max_round_seconds=0)


@pytest.mark.asyncio
async def test_timeout_job_heartbeat_is_independent_of_poll_success() -> None:
    register_task_modules()
    now = datetime(2026, 9, 6, 4, tzinfo=UTC)

    class Repository:
        async def latest(self, job_name: str) -> JobRunSnapshot | None:
            if job_name == "poll_report":
                return JobRunSnapshot(
                    "poll_report",
                    now - timedelta(seconds=10),
                    now - timedelta(seconds=9),
                    "success",
                )
            return None

        async def consecutive_failures(self, job_name: str, *, limit: int) -> int:
            return 0

    class Alerts:
        def __init__(self) -> None:
            self.events: list[dict[str, Any]] = []

        async def emit(self, **values: object) -> None:
            self.events.append(dict(values))

    alerts = Alerts()
    await JobHealthMonitor(Repository(), alerts, clock=lambda: now).inspect_once(
        [JobSpec("poll_report", 60), JobSpec("expire_report_timeouts", 60)]
    )
    names = [str(event["detail"]["job_name"]) for event in alerts.events]
    assert "expire_report_timeouts" in names
    assert "poll_report" not in names


def test_startup_schedule_can_override_timeout_heartbeat() -> None:
    register_task_modules()
    original = dict(JOB_SPECS)
    try:
        apply_job_interval_overrides(
            build_beat_schedule({"report_timeout_scan_seconds": "45"})
        )
        assert JOB_SPECS["expire_report_timeouts"] == JobSpec("expire_report_timeouts", 45)
        assert JOB_SPECS["poll_report"] == JobSpec("poll_report", 60)
    finally:
        JOB_SPECS.clear()
        JOB_SPECS.update(original)
