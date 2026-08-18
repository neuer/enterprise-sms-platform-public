from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.jobtrack import JOB_SPECS, JobHealthMonitor, JobRunSnapshot, JobSpec
from app.tasks import register_task_modules
from app.tasks.scheduler import (
    apply_job_interval_overrides,
    build_beat_schedule,
    decode_startup_schedule,
    encode_startup_schedule,
)


def test_beat_schedule_reads_report_interval_once_at_startup() -> None:
    schedule = build_beat_schedule({"report_poll_seconds": "17"})
    assert schedule["poll-report"] == {
        "task": "app.tasks.poll_report",
        "schedule": 17,
        "options": {"queue": "realtime"},
    }
    assert schedule["reconcile"] == {
        "task": "app.tasks.reconcile",
        "schedule": 300,
        "options": {"queue": "realtime"},
    }
    assert schedule["expire-approvals"]["schedule"] == 300
    assert build_beat_schedule({"approval_scan_seconds": "91"})["expire-approvals"] == {
        "task": "app.tasks.expire_approvals",
        "schedule": 91,
        "options": {"queue": "realtime"},
    }
    assert build_beat_schedule({"scheduled_scan_seconds": "29"})["dispatch-scheduled"] == {
        "task": "app.tasks.dispatch_scheduled",
        "schedule": 29,
        "options": {"queue": "realtime"},
    }
    assert schedule["sync-templates"] == {
        "task": "app.tasks.sync_templates",
        "schedule": 600,
        "options": {"queue": "realtime"},
    }
    assert schedule["sync-signs"]["schedule"] == 600
    assert build_beat_schedule({"balance_poll_seconds": "41"})["poll-balance"] == {
        "task": "app.tasks.poll_balance",
        "schedule": 41,
        "options": {"queue": "realtime"},
    }
    assert build_beat_schedule({"anomaly_scan_minutes": "17"})["anomaly-scan"] == {
        "task": "app.tasks.anomaly_scan",
        "schedule": 1020,
        "options": {"queue": "realtime"},
    }
    assert build_beat_schedule({"reply_poll_seconds": "73"})["poll-reply"] == {
        "task": "app.tasks.poll_reply",
        "schedule": 73,
        "options": {"queue": "realtime"},
    }
    assert build_beat_schedule({})["dispatch-callbacks"] == {
        "task": "app.tasks.dispatch_callbacks",
        "schedule": 30,
        "options": {"queue": "callback"},
    }
    assert build_beat_schedule({})["dispatch-imports"] == {
        "task": "app.tasks.dispatch_imports",
        "schedule": 30,
        "options": {"queue": "bulk"},
    }
    assert build_beat_schedule(
        {"usage_projection_reconcile_seconds": "47"}
    )["usage-projection-reconcile"] == {
        "task": "app.tasks.reconcile_usage_projection",
        "schedule": 47,
        "options": {"queue": "realtime"},
    }


def test_beat_schedule_rejects_non_positive_interval() -> None:
    with pytest.raises(ValueError):
        build_beat_schedule({"report_poll_seconds": "0"})


def test_beat_schedule_round_trips_before_celery_scheduler_construction() -> None:
    schedule = build_beat_schedule({"report_poll_seconds": "17"})
    encoded = encode_startup_schedule(schedule)

    assert decode_startup_schedule(encoded) == schedule
    assert decode_startup_schedule(None) == {}
    with pytest.raises(ValueError, match="startup schedule"):
        decode_startup_schedule('["not-a-mapping"]')


def test_startup_schedule_overrides_all_configurable_job_heartbeats() -> None:
    register_task_modules()
    original = dict(JOB_SPECS)
    schedule = build_beat_schedule(
        {
            "report_poll_seconds": "17",
            "reply_poll_seconds": "73",
            "reconcile_interval_min": "7",
            "approval_scan_seconds": "91",
            "scheduled_scan_seconds": "29",
            "balance_poll_seconds": "41",
            "anomaly_scan_minutes": "11",
            "usage_projection_reconcile_seconds": "47",
        }
    )
    try:
        apply_job_interval_overrides(schedule)
        assert {
            name: JOB_SPECS[name].expect_interval_s
            for name in (
                "poll_report",
                "poll_reply",
                "reconcile",
                "expire_approvals",
                "dispatch_scheduled",
                "poll_balance",
                "anomaly_scan",
                "reconcile_usage_projection",
            )
        } == {
            "poll_report": 17,
            "poll_reply": 73,
            "reconcile": 420,
            "expire_approvals": 91,
            "dispatch_scheduled": 29,
            "poll_balance": 41,
            "anomaly_scan": 660,
            "reconcile_usage_projection": 47,
        }
    finally:
        JOB_SPECS.clear()
        JOB_SPECS.update(original)


@pytest.mark.asyncio
async def test_custom_startup_interval_does_not_emit_false_stalled_alert() -> None:
    now = datetime(2026, 7, 13, 8, tzinfo=UTC)

    class Repository:
        async def latest(self, job_name: str) -> JobRunSnapshot:
            return JobRunSnapshot(job_name, now - timedelta(seconds=700), now, "success")

        async def consecutive_failures(self, job_name: str, *, limit: int) -> int:
            return 0

    class Alerts:
        def __init__(self) -> None:
            self.events: list[object] = []

        async def emit(self, **values: object) -> None:
            self.events.append(values)

    alerts = Alerts()
    await JobHealthMonitor(Repository(), alerts, clock=lambda: now).inspect_once(
        [JobSpec("reconcile", 1200)]
    )
    assert alerts.events == []
