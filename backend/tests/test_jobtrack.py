from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest

import app.core.jobtrack as jobtrack_module
from app.core.jobtrack import (
    JobHealthMonitor,
    JobHeartbeatService,
    JobRunSnapshot,
    JobSpec,
    JobTracker,
    SqlJobMonitorLease,
    consecutive_failed_count,
    job_is_stalled,
    job_stalled_since,
    tracked_job,
)
from app.settings import Settings


class FakeRepository:
    def __init__(self) -> None:
        self.started: list[tuple[str, datetime]] = []
        self.finished: list[dict[str, Any]] = []
        self.latest_by_name: dict[str, JobRunSnapshot] = {}
        self.failures_by_name: dict[str, int] = {}

    async def start(self, job_name: str, started_at: datetime) -> int:
        self.started.append((job_name, started_at))
        return len(self.started)

    async def finish(
        self,
        run_id: int,
        *,
        finished_at: datetime,
        duration_ms: int,
        items: int,
        status: str,
        error: str | None,
    ) -> None:
        self.finished.append(
            {
                "run_id": run_id,
                "finished_at": finished_at,
                "duration_ms": duration_ms,
                "items": items,
                "status": status,
                "error": error,
            }
        )

    async def latest(self, job_name: str) -> JobRunSnapshot | None:
        return self.latest_by_name.get(job_name)

    async def consecutive_failures(self, job_name: str, *, limit: int) -> int:
        return min(self.failures_by_name.get(job_name, 0), limit)


class FakeAlertSink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(
        self,
        *,
        alert_type: str,
        level: str,
        title: str,
        detail: dict[str, Any],
        dedup_key: str,
    ) -> None:
        self.events.append(
            {
                "alert_type": alert_type,
                "level": level,
                "title": title,
                "detail": detail,
                "dedup_key": dedup_key,
            }
        )


class FakeInspector:
    def __init__(self) -> None:
        self.calls = 0

    async def inspect_once(self, specs: Any = None) -> None:
        self.calls += 1


class FakeLease:
    def __init__(self, *, available: bool) -> None:
        self.available = available
        self.acquired = False
        self.attempts = 0
        self.releases = 0

    async def try_acquire(self) -> bool:
        self.attempts += 1
        if self.acquired:
            return True
        self.acquired = self.available
        return self.acquired

    async def release(self) -> None:
        if self.acquired:
            self.releases += 1
        self.acquired = False


def clock(*values: datetime) -> Any:
    moments: Iterator[datetime] = iter(values)
    return lambda: next(moments)


def test_tracked_sync_job_records_success_and_item_count() -> None:
    repo = FakeRepository()
    start = datetime(2026, 7, 11, 8, 0, tzinfo=UTC)
    tracker = JobTracker(repo, clock=clock(start, start + timedelta(milliseconds=125)))

    @tracked_job("sync_probe", expect_interval_s=60, tracker=tracker)
    def sync_probe() -> int:
        return 7

    assert sync_probe() == 7
    assert repo.started == [("sync_probe", start)]
    assert repo.finished[0] == {
        "run_id": 1,
        "finished_at": start + timedelta(milliseconds=125),
        "duration_ms": 125,
        "items": 7,
        "status": "success",
        "error": None,
    }
    assert vars(sync_probe)["job_spec"] == JobSpec("sync_probe", 60)


def test_tracked_sync_job_may_run_asyncio_based_celery_body() -> None:
    """追踪 I/O 复用 worker loop，任务同步入口仍可运行独立异步工具。"""

    repo = FakeRepository()
    start = datetime(2026, 7, 11, 8, 0, tzinfo=UTC)
    tracker = JobTracker(repo, clock=clock(start, start + timedelta(seconds=1)))

    @tracked_job("asyncio_body_probe", expect_interval_s=60, tracker=tracker)
    def asyncio_body_probe() -> int:
        async def body() -> int:
            return 3

        return asyncio.run(body())

    assert asyncio_body_probe() == 3
    assert repo.finished[0]["status"] == "success"
    assert repo.finished[0]["items"] == 3


@pytest.mark.asyncio
async def test_tracked_async_job_records_failure_without_swallowing() -> None:
    repo = FakeRepository()
    start = datetime(2026, 7, 11, 8, 0, tzinfo=UTC)
    tracker = JobTracker(repo, clock=clock(start, start + timedelta(seconds=2)))

    @tracked_job("async_probe", expect_interval_s=30, tracker=tracker)
    async def async_probe() -> None:
        raise RuntimeError("号码 13800138000 拉取失败")

    with pytest.raises(RuntimeError, match="13800138000"):
        await async_probe()
    assert repo.finished[0]["status"] == "failed"
    assert repo.finished[0]["items"] == 0
    assert "138****8000" in repo.finished[0]["error"]
    assert "13800138000" not in repo.finished[0]["error"]


@pytest.mark.asyncio
async def test_health_monitor_emits_stalled_and_three_failure_alerts() -> None:
    now = datetime(2026, 7, 11, 8, 10, tzinfo=UTC)
    repo = FakeRepository()
    repo.latest_by_name["poll_report"] = JobRunSnapshot(
        job_name="poll_report",
        started_at=now - timedelta(seconds=121),
        finished_at=now - timedelta(seconds=120),
        status="failed",
    )
    repo.failures_by_name["poll_report"] = 3
    sink = FakeAlertSink()
    monitor = JobHealthMonitor(repo, sink, clock=lambda: now)

    await monitor.inspect_once([JobSpec("poll_report", 60)])

    assert {event["alert_type"] for event in sink.events} == {"job_stalled", "job_failed"}
    assert all(event["detail"]["job_name"] == "poll_report" for event in sink.events)
    assert all("phone" not in str(event["detail"]).lower() for event in sink.events)
    stalled = next(event for event in sink.events if event["alert_type"] == "job_stalled")
    assert stalled["dedup_key"] == "job_stalled:poll_report:2026-07-11T08:07:59+00:00"


@pytest.mark.asyncio
async def test_health_monitor_starts_new_incident_after_job_recovers() -> None:
    now = datetime(2026, 7, 11, 8, 10, tzinfo=UTC)
    repo = FakeRepository()
    sink = FakeAlertSink()
    monitor = JobHealthMonitor(repo, sink, clock=lambda: now)
    spec = JobSpec("dispatch_callbacks", 30)

    repo.latest_by_name[spec.job_name] = JobRunSnapshot(
        job_name=spec.job_name,
        started_at=now - timedelta(seconds=121),
        finished_at=now - timedelta(seconds=120),
        status="success",
    )
    await monitor.inspect_once([spec])
    first_key = sink.events[-1]["dedup_key"]

    repo.latest_by_name[spec.job_name] = JobRunSnapshot(
        job_name=spec.job_name,
        started_at=now - timedelta(seconds=61),
        finished_at=now - timedelta(seconds=60),
        status="success",
    )
    await monitor.inspect_once([spec])

    assert sink.events[-1]["dedup_key"] != first_key


@pytest.mark.asyncio
async def test_health_monitor_does_not_alert_for_healthy_job() -> None:
    now = datetime(2026, 7, 11, 8, 10, tzinfo=UTC)
    repo = FakeRepository()
    repo.latest_by_name["reconcile"] = JobRunSnapshot(
        job_name="reconcile",
        started_at=now - timedelta(seconds=30),
        finished_at=now - timedelta(seconds=29),
        status="success",
    )
    sink = FakeAlertSink()
    monitor = JobHealthMonitor(repo, sink, clock=lambda: now)

    await monitor.inspect_once([JobSpec("reconcile", 60)])

    assert sink.events == []


@pytest.mark.asyncio
async def test_monitor_treats_never_seen_job_as_stalled() -> None:
    repo = FakeRepository()
    sink = FakeAlertSink()
    monitor = JobHealthMonitor(repo, sink)

    await monitor.inspect_once([JobSpec("never_seen", 60)])

    assert [event["alert_type"] for event in sink.events] == ["job_stalled"]


def test_consecutive_failed_count_keeps_running_in_stalled_domain() -> None:
    assert consecutive_failed_count(["running", "running", "running"]) == 0
    assert consecutive_failed_count(["running", "success"]) == 0
    assert consecutive_failed_count(["failed", "failed", "success"]) == 2
    assert consecutive_failed_count(["success", "failed"]) == 0


@pytest.mark.parametrize(
    ("status", "threshold_seconds"),
    (("success", 120), ("failed", 120), ("running", 60)),
)
def test_job_stalled_since_uses_strict_threshold_in_utc(
    status: str,
    threshold_seconds: int,
) -> None:
    now = datetime(2026, 7, 11, 8, 10, tzinfo=UTC)
    started_at = (now - timedelta(seconds=threshold_seconds)).astimezone(
        timezone(timedelta(hours=8))
    )
    latest = JobRunSnapshot(
        job_name="poll_report",
        started_at=started_at,
        finished_at=None if status == "running" else started_at + timedelta(seconds=1),
        status=status,
    )
    spec = JobSpec("poll_report", 60)

    assert job_stalled_since(latest, spec, now=now) is None
    assert job_is_stalled(latest, spec, now=now) is False

    expected = started_at.astimezone(UTC) + timedelta(seconds=threshold_seconds)
    overdue_at = now + timedelta(microseconds=1)
    assert job_stalled_since(latest, spec, now=overdue_at) == expected
    assert job_is_stalled(latest, spec, now=overdue_at) is True


def test_job_stalled_since_preserves_never_seen_behavior() -> None:
    now = datetime(2026, 7, 11, 8, 10, tzinfo=UTC)
    spec = JobSpec("never_seen", 60)

    assert job_stalled_since(None, spec, now=now) is None
    assert job_is_stalled(None, spec, now=now) is True


@pytest.mark.asyncio
async def test_fresh_running_job_is_not_stalled_but_overdue_running_is() -> None:
    now = datetime(2026, 7, 11, 8, 10, tzinfo=UTC)
    repo = FakeRepository()
    sink = FakeAlertSink()
    monitor = JobHealthMonitor(repo, sink, clock=lambda: now)
    repo.latest_by_name["poll_report"] = JobRunSnapshot(
        job_name="poll_report",
        started_at=now - timedelta(seconds=1),
        finished_at=None,
        status="running",
    )

    await monitor.inspect_once([JobSpec("poll_report", 60)])
    assert sink.events == []

    repo.latest_by_name["poll_report"] = JobRunSnapshot(
        job_name="poll_report",
        started_at=now - timedelta(seconds=61),
        finished_at=None,
        status="running",
    )
    await monitor.inspect_once([JobSpec("poll_report", 60)])
    assert [event["alert_type"] for event in sink.events] == ["job_stalled"]


@pytest.mark.asyncio
async def test_heartbeat_service_skips_scan_without_leadership() -> None:
    inspector = FakeInspector()
    lease = FakeLease(available=False)
    service = JobHeartbeatService(inspector, lease=lease)

    assert await service.inspect_once() is False
    assert inspector.calls == 0
    assert lease.attempts == 1


@pytest.mark.asyncio
async def test_heartbeat_service_holds_and_releases_leadership() -> None:
    inspector = FakeInspector()
    lease = FakeLease(available=True)
    service = JobHeartbeatService(inspector, lease=lease)

    assert await service.inspect_once() is True
    assert await service.inspect_once() is True
    assert inspector.calls == 2
    assert lease.acquired is True

    await service.stop()

    assert lease.acquired is False
    assert lease.releases == 1


@pytest.mark.asyncio
async def test_sql_monitor_lease_uses_managed_engine_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """共享 engine 的 connect 返回异步上下文，不能当作 awaitable 使用。"""

    class ScalarResult:
        def scalar_one(self) -> bool:
            return True

    class Connection:
        def __init__(self) -> None:
            self.statements: list[str] = []
            self.commits = 0

        async def execute(self, statement: Any, parameters: Any) -> ScalarResult:
            del parameters
            self.statements.append(str(statement))
            return ScalarResult()

        async def commit(self) -> None:
            self.commits += 1

    class ConnectionContext:
        def __init__(self, connection: Connection) -> None:
            self.connection = connection
            self.entered = 0
            self.exited = 0

        async def __aenter__(self) -> Connection:
            self.entered += 1
            return self.connection

        async def __aexit__(self, *_: object) -> None:
            self.exited += 1

    class Engine:
        def __init__(self) -> None:
            self.connection = Connection()
            self.context = ConnectionContext(self.connection)
            self.dispose_calls = 0

        def connect(self) -> ConnectionContext:
            return self.context

        async def dispose(self) -> None:
            self.dispose_calls += 1

    engine = Engine()
    monkeypatch.setattr(jobtrack_module, "database_engine", lambda _: engine)
    settings = cast(Settings, SimpleNamespace(database_url="postgresql+asyncpg://example"))
    lease = SqlJobMonitorLease(settings=settings)

    assert await lease.try_acquire() is True
    assert await lease.try_acquire() is True
    assert engine.context.entered == 1

    await lease.release()

    assert engine.context.exited == 1
    assert engine.connection.commits == 2
    assert "pg_try_advisory_lock" in engine.connection.statements[0]
    assert "pg_advisory_unlock" in engine.connection.statements[1]


def test_tracked_job_rejects_invalid_expected_interval() -> None:
    with pytest.raises(ValueError, match="expect_interval_s"):
        tracked_job("bad", expect_interval_s=0)
