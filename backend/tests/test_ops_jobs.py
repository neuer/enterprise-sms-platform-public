from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from app.core.jobtrack import JobSpec
from app.services import ops_dispatch as ops_dispatch_module
from app.services.ops import JobNotFound, JobOpsService, JobRecord, JobRoute
from app.services.ops_dispatch import OutboxBatchSender, OutboxJobSender, TemplateSyncSender
from app.services.ops_repository import SqlOpsRepository

NOW = datetime(2026, 7, 12, 8, 0, tzinfo=UTC)


class FakeResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def mappings(self) -> FakeResult:
        return self

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self.rows)


class FakeConnection:
    def __init__(self, result: FakeResult) -> None:
        self.result = result
        self.calls: list[tuple[str, Any]] = []

    async def execute(self, statement: object, params: Any = None) -> FakeResult:
        self.calls.append((str(statement), params))
        return self.result


class FakeContext:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, *_: object) -> None:
        return None


class FakeEngine:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def connect(self) -> FakeContext:
        return FakeContext(self.connection)

    async def dispose(self) -> None:
        return None


class FakeTransactionEngine:
    def __init__(self) -> None:
        self.connection = object()
        self.disposed = False

    def begin(self) -> FakeContext:
        return FakeContext(self.connection)  # type: ignore[arg-type]

    async def dispose(self) -> None:
        self.disposed = True


@pytest.mark.asyncio
async def test_job_list_uses_specs_and_returns_health_statistics() -> None:
    connection = FakeConnection(
        FakeResult(
            [
                {
                    "job_name": "poll_report",
                    "last_run_at": NOW,
                    "last_status": "success",
                    "last_duration_ms": 120,
                    "last_items": 9,
                    "success_rate_24h": 0.75,
                    "stalled": False,
                }
            ]
        )
    )
    repo = SqlOpsRepository()
    repo._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]

    items = await repo.list_jobs((JobSpec("poll_report", 60),), now=NOW)

    assert items[0].last_items == 9 and items[0].success_rate_24h == 0.75
    sql, params = connection.calls[0]
    normalized_sql = " ".join(sql.split())
    assert "unnest( CAST(:job_names AS text[]),CAST(:intervals AS integer[]) )" in normalized_sql
    assert "status IN ('success','failed')" in sql
    assert "expect_interval_s*2" in sql
    assert params["job_names"] == ["poll_report"]


class FakeJobRepository:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def list_jobs(
        self, specs: Sequence[JobSpec], *, now: datetime
    ) -> tuple[JobRecord, ...]:
        return ()

    async def audit_job_trigger(self, job_name: str, *, actor: str, ip: str) -> None:
        self.events.append(f"audit:{job_name}:{actor}:{ip}")


class FakeSender:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def send(self, task_name: str, queue: str) -> None:
        self.events.append(f"send:{task_name}:{queue}")


@pytest.mark.asyncio
async def test_job_trigger_is_allowlisted_audited_then_sent_to_fixed_queue() -> None:
    events: list[str] = []
    service = JobOpsService(
        FakeJobRepository(events),
        FakeSender(events),
        {"poll_report": JobSpec("poll_report", 60)},
        {"poll_report": JobRoute("app.tasks.poll_report", "realtime")},
        clock=lambda: NOW,
    )

    await service.trigger("poll_report", actor="admin01", ip="10.0.0.8")

    assert events == [
        "audit:poll_report:admin01:10.0.0.8",
        "send:app.tasks.poll_report:realtime",
    ]


@pytest.mark.asyncio
async def test_unknown_or_untracked_job_is_never_dispatched() -> None:
    events: list[str] = []
    service = JobOpsService(
        FakeJobRepository(events),
        FakeSender(events),
        {"poll_report": JobSpec("poll_report", 60)},
        {"poll_report": JobRoute("app.tasks.poll_report", "realtime")},
        clock=lambda: NOW,
    )

    with pytest.raises(JobNotFound):
        await service.trigger("app.tasks.send.process_chunk", actor="admin01", ip="10.0.0.8")

    assert events == []


@pytest.mark.asyncio
async def test_tracked_but_not_allowlisted_job_is_404_without_audit() -> None:
    events: list[str] = []
    service = JobOpsService(
        FakeJobRepository(events),
        FakeSender(events),
        {"process_chunk": JobSpec("process_chunk", 60)},
        {"process_chunk": JobRoute("app.tasks.send.process_chunk", "realtime")},
        clock=lambda: NOW,
    )

    with pytest.raises(JobNotFound):
        await service.trigger("process_chunk", actor="admin01", ip="10.0.0.8")

    assert events == []


@pytest.mark.asyncio
async def test_security_daily_generate_manual_trigger_is_allowlisted() -> None:
    events: list[str] = []
    service = JobOpsService(
        FakeJobRepository(events),
        FakeSender(events),
        {"security_daily_generate": JobSpec("security_daily_generate", 60)},
        {
            "security_daily_generate": JobRoute(
                "app.tasks.security_daily_generate", "bulk"
            )
        },
        clock=lambda: NOW,
    )

    await service.trigger("security_daily_generate", actor="admin01", ip="10.0.0.8")

    assert events == [
        "audit:security_daily_generate:admin01:10.0.0.8",
        "send:app.tasks.security_daily_generate:bulk",
    ]


def test_every_beat_job_spec_and_route_key_pair_is_triggerable() -> None:
    """跟踪名、路由键与允许清单三方一致，杜绝手动触发死路（#345/#346）。"""

    from app.core.jobtrack import JOB_SPECS
    from app.services.outbox import MANUAL_JOB_TASK_NAMES
    from app.tasks import register_task_modules
    from app.tasks.scheduler import build_beat_schedule

    register_task_modules()
    for value in build_beat_schedule({}).values():
        task_name = str(value["task"])
        if task_name not in MANUAL_JOB_TASK_NAMES:
            continue
        route_key = task_name.rsplit(".", 1)[-1]
        assert route_key in JOB_SPECS, f"beat 任务 {task_name} 的路由键缺 JOB_SPECS 项"


@pytest.mark.asyncio
async def test_ops_senders_persist_unique_outbox_requests_without_broker_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engines: list[FakeTransactionEngine] = []
    specs: list[Any] = []

    def engine(_database_url: object) -> FakeTransactionEngine:
        selected = FakeTransactionEngine()
        engines.append(selected)
        return selected

    async def enqueue(_connection: object, spec: Any) -> None:
        specs.append(spec)

    monkeypatch.setattr(ops_dispatch_module, "database_engine", engine)
    monkeypatch.setattr(ops_dispatch_module, "enqueue_outbox", enqueue)
    settings = type("SettingsStub", (), {"database_url": "postgresql://test"})()

    await OutboxJobSender(settings).send("app.tasks.expire_approvals", "realtime")
    await TemplateSyncSender(settings, clock=lambda: NOW).send_template(7)
    await TemplateSyncSender(settings, clock=lambda: NOW).send_template(7)
    await OutboxBatchSender(settings).send_batch("a" * 32, "realtime")

    job, template_sync, repeated_template_sync, batch = specs
    assert job.task_name == "app.tasks.outbox.trigger_job"
    assert job.args == ("app.tasks.expire_approvals",)
    assert job.dedup_key.startswith("job.trigger:expire_approvals:")
    assert template_sync.event_type == "template.sync"
    assert template_sync.aggregate_type == "sms_template"
    assert template_sync.aggregate_id == "7"
    assert template_sync.task_name == "app.tasks.sync_template"
    assert template_sync.queue == "realtime"
    assert template_sync.args == (7,)
    assert template_sync.max_attempts == 3
    assert repeated_template_sync.dedup_key == template_sync.dedup_key
    assert batch.task_name == "app.tasks.send.process_batch"
    assert batch.args == ("a" * 32,)
    assert batch.dedup_key.startswith("batch.ready:")
    assert batch.dedup_key != "batch.ready:" + "a" * 32
    assert all(selected.disposed for selected in engines)
