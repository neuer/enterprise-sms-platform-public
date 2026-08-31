from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.jobtrack import JobRunSnapshot, JobSpec
from app.services.current_alerts import (
    ControlCurrentFacts,
    CurrentAlertService,
    CurrentJobFact,
    DatabaseCurrentFacts,
    RawSpillAlertFact,
    UsageDriftFact,
)
from app.services.runtime_policy import RuntimePolicy

NOW = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
SPECS = (
    JobSpec("poll_balance", 600),
    JobSpec("poll_report", 60),
    JobSpec("reconcile_usage_projection", 300),
)


def healthy_job(name: str, age_seconds: int = 10) -> CurrentJobFact:
    started = NOW - timedelta(seconds=age_seconds)
    return CurrentJobFact(
        name,
        JobRunSnapshot(name, started, started + timedelta(seconds=1), "success"),
        ("success",),
        started,
    )


def database_facts(**overrides: object) -> DatabaseCurrentFacts:
    values: dict[str, object] = {
        "policy": RuntimePolicy.from_mapping({}),
        "jobs": tuple(healthy_job(spec.job_name) for spec in SPECS),
        "usage_drift": (
            UsageDriftFact("quota", 0, 0, NOW - timedelta(seconds=10)),
            UsageDriftFact("frequency", 0, 0, NOW - timedelta(seconds=10)),
        ),
        "balance": 20_000,
        "balance_checked_at": NOW - timedelta(seconds=10),
        "uncertain_overdue": 0,
        "uncertain_since": None,
        "callback_dead": 0,
        "callback_dead_since": None,
        "outbox_dead": 0,
        "outbox_dead_since": None,
        "outbox_active": 0,
        "outbox_oldest_active_at": None,
        "raw_manual": 0,
        "raw_manual_since": None,
        "raw_spill_alerts": (),
    }
    values.update(overrides)
    return DatabaseCurrentFacts(**values)  # type: ignore[arg-type]


class FakeRepository:
    def __init__(
        self,
        database: DatabaseCurrentFacts | BaseException,
        control: ControlCurrentFacts | BaseException,
    ) -> None:
        self.database = database
        self.control = control

    async def load_database(self, specs: tuple[JobSpec, ...]) -> DatabaseCurrentFacts:
        if isinstance(self.database, BaseException):
            raise self.database
        return self.database

    async def load_control(self) -> ControlCurrentFacts:
        if isinstance(self.control, BaseException):
            raise self.control
        return self.control


@pytest.mark.asyncio
async def test_current_alerts_are_derived_from_live_facts_and_sorted_by_severity() -> None:
    facts = database_facts(
        uncertain_overdue=2,
        uncertain_since=NOW - timedelta(hours=30),
        callback_dead=1,
        callback_dead_since=NOW - timedelta(hours=2),
        outbox_active=4,
        outbox_oldest_active_at=NOW - timedelta(seconds=301),
    )
    result = await CurrentAlertService(
        FakeRepository(facts, ControlCurrentFacts("999", None, 3)),
        SPECS,
        clock=lambda: NOW,
    ).get()

    assert result.complete is True
    assert result.unknown_sources == ()
    keys = [item.key for item in result.items]
    assert keys == [
        "uncertain_overdue",
        "callback_dead",
        "queue_paused",
        "vendor_consecutive_failure",
        "outbox_backlog",
    ]
    assert all(item.checked_at.tzinfo is not None for item in result.items)


@pytest.mark.asyncio
async def test_source_failure_is_unknown_without_hiding_other_source_alerts() -> None:
    result = await CurrentAlertService(
        FakeRepository(RuntimeError("db down"), ControlCurrentFacts("999", None, 0)),
        SPECS,
        clock=lambda: NOW,
    ).get()

    assert result.complete is False
    assert result.unknown_sources == ("postgresql",)
    assert [item.key for item in result.items] == ["queue_paused"]


@pytest.mark.asyncio
async def test_stale_usage_and_balance_are_unknown_not_green() -> None:
    stale = NOW - timedelta(hours=2)
    facts = database_facts(
        usage_drift=(
            UsageDriftFact("quota", 0, 0, stale),
            UsageDriftFact("frequency", 0, 0, stale),
        ),
        balance_checked_at=stale,
    )
    result = await CurrentAlertService(
        FakeRepository(facts, ControlCurrentFacts(None, None, 0)),
        SPECS,
        clock=lambda: NOW,
    ).get()

    assert result.complete is False
    assert result.items == ()
    assert result.unknown_sources == ("usage_projection", "balance")


@pytest.mark.asyncio
async def test_raw_spill_alert_clears_only_after_a_later_successful_poll() -> None:
    alert_at = NOW - timedelta(minutes=5)
    active = database_facts(
        jobs=(
            healthy_job("poll_balance"),
            CurrentJobFact(
                "poll_report",
                healthy_job("poll_report").latest,
                ("success",),
                alert_at - timedelta(seconds=1),
            ),
            healthy_job("reconcile_usage_projection"),
        ),
        raw_spill_alerts=(RawSpillAlertFact("vendor_raw_spill_failed", "report", alert_at),),
    )
    recovered = database_facts(
        raw_spill_alerts=active.raw_spill_alerts,
        jobs=tuple(
            CurrentJobFact(job.job_name, job.latest, job.recent_statuses, NOW)
            if job.job_name == "poll_report"
            else job
            for job in active.jobs
        ),
    )
    repository = FakeRepository(active, ControlCurrentFacts(None, None, 0))
    service = CurrentAlertService(repository, SPECS, clock=lambda: NOW)

    assert [item.key for item in (await service.get()).items] == [
        "vendor_raw_spill_failed:report"
    ]
    repository.database = recovered
    assert (await service.get()).items == ()
