from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Iterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.services import outbox_repository as outbox_repository_module
from app.services.outbox import (
    OutboxClaim,
    OutboxDispatcher,
    OutboxEventSpec,
    OutboxExecutor,
    OutboxLease,
    OutboxLeaseLost,
    validate_spec,
)
from app.services.outbox_queue import CeleryOutboxPublisher
from app.services.outbox_repository import SqlOutboxRepository
from app.services.sign_repository import SqlSignRepository
from app.services.template_repository import SqlTemplateRepository
from app.tasks import celery_app
from app.tasks import outbox as outbox_task_module

WAIT_FOR_CALLS = 0


def event_spec(**overrides: Any) -> OutboxEventSpec:
    values: dict[str, Any] = {
        "event_type": "batch.ready",
        "aggregate_type": "sms_batch",
        "aggregate_id": "BATCH-1",
        "task_name": "app.tasks.send.process_batch",
        "queue": "realtime",
        "args": ("BATCH-1",),
        "dedup_key": "batch.ready:BATCH-1",
    }
    values.update(overrides)
    return OutboxEventSpec(**values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("aggregate_id", "13800138000"),
        ("dedup_key", "batch:13800138000"),
        ("args", ("短信正文", {"body": "hello"})),
        ("args", ("credential", {"secret": "value"})),
        ("args", (True,)),
        ("args", (None,)),
        ("queue", "arbitrary"),
        ("task_name", "os.system"),
        ("task_name", "app.tasks.housekeeping.cleanup"),
        ("correlation_id", UUID(int=0)),
    ],
)
def test_outbox_contract_rejects_pii_secret_body_and_route_injection(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError):
        validate_spec(event_spec(**{field: value}))


def test_outbox_contract_accepts_database_references_only() -> None:
    validate_spec(event_spec(args=(12, "BATCH-1")))


def test_outbox_contract_allows_only_fixed_manual_job_references() -> None:
    request_id = "c0a80101-0000-4000-8000-000000000139"
    spec = event_spec(
        event_type="job.trigger",
        aggregate_type="job",
        aggregate_id="expire_approvals",
        task_name="app.tasks.outbox.trigger_job",
        args=("app.tasks.expire_approvals",),
        dedup_key=f"job.trigger:expire_approvals:{request_id}",
    )

    validate_spec(spec)
    with pytest.raises(ValueError, match="manual job"):
        validate_spec(replace(spec, args=("app.tasks.send.process_chunk",)))


@pytest.mark.parametrize(
    ("task_name", "event_type", "aggregate_type"),
    [
        ("app.tasks.bind_template", "template.bind", "sms_template"),
        ("app.tasks.bind_sign", "sign.bind", "sms_sign"),
    ],
)
def test_outbox_contract_allows_only_exact_vendor_binding_references(
    task_name: str,
    event_type: str,
    aggregate_type: str,
) -> None:
    request_id = "c0a80101-0000-4000-8000-000000000139"
    spec = event_spec(
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id="13800138000",
        task_name=task_name,
        args=(13800138000,),
        dedup_key=f"{event_type}:13800138000:{request_id}",
        max_attempts=1,
    )

    validate_spec(spec)
    with pytest.raises(ValueError, match="vendor binding"):
        validate_spec(replace(spec, args=(13800138001,)))
    with pytest.raises(ValueError, match="vendor binding"):
        validate_spec(replace(spec, queue="bulk"))


def test_outbox_contract_allows_only_exact_template_sync_references() -> None:
    spec = event_spec(
        event_type="template.sync",
        aggregate_type="sms_template",
        aggregate_id="13800138000",
        task_name="app.tasks.sync_template",
        queue="realtime",
        args=(13800138000,),
        dedup_key="template.sync:13800138000:29772480",
        max_attempts=3,
    )

    validate_spec(spec)
    for malformed in (
        replace(spec, event_type="job.trigger"),
        replace(spec, aggregate_id="13800138001"),
        replace(spec, args=(13800138001,)),
        replace(spec, queue="bulk"),
        replace(spec, max_attempts=12),
        replace(spec, dedup_key="template.sync:13800138000:0"),
    ):
        with pytest.raises(ValueError, match="template sync"):
            validate_spec(malformed)


def test_outbox_contract_allows_only_exact_sign_adoption_references() -> None:
    request_id = "c0a80101-0000-4000-8000-000000000139"
    spec = event_spec(
        event_type="sign.adopt",
        aggregate_type="sms_sign",
        aggregate_id="9",
        task_name="app.tasks.adopt_sign",
        queue="realtime",
        args=(9, 112074),
        dedup_key=f"sign.adopt:9:{request_id}",
        max_attempts=3,
    )

    validate_spec(spec)
    for malformed in (
        replace(spec, event_type="job.trigger"),
        replace(spec, aggregate_id="10"),
        replace(spec, args=(10, 112074)),
        replace(spec, args=(9, 0)),
        replace(spec, args=(9, 2_147_483_648)),
        replace(spec, queue="bulk"),
        replace(spec, max_attempts=12),
        replace(spec, dedup_key="sign.adopt:9:not-a-uuid"),
    ):
        with pytest.raises(ValueError, match="sign adoption"):
            validate_spec(malformed)


def test_outbox_contract_accepts_phone_shaped_digits_inside_opaque_batch_id() -> None:
    batch_no = "13800138000" + "a" * 21

    validate_spec(
        event_spec(
            aggregate_id=batch_no,
            args=(batch_no,),
            dedup_key=f"batch.ready:{batch_no}",
        )
    )
    validate_spec(
        event_spec(
            event_type="quota.compensation",
            aggregate_id=batch_no,
            task_name="app.tasks.outbox.compensate_quota",
            args=(
                7,
                "平台部",
                "notice",
                "20260726",
                1,
                f"batch:{batch_no}:cancelled",
            ),
            dedup_key=f"batch:{batch_no}:cancelled",
        )
    )


def test_outbox_contract_accepts_phone_shaped_digits_inside_usage_uuid() -> None:
    reservation_id = "fa39c47a-4a50-468f-85ca-b12345678901"

    validate_spec(
        event_spec(
            event_type="usage.release",
            aggregate_type="usage_reservation",
            aggregate_id=reservation_id,
            task_name="app.tasks.outbox.release_usage",
            args=(reservation_id,),
            dedup_key=f"usage.release:{reservation_id}",
        )
    )


@pytest.mark.parametrize(
    ("helper", "module_name", "event_type", "task_name"),
    [
        (
            SqlTemplateRepository._enqueue_binding,
            "app.services.template_repository",
            "template.bind",
            "app.tasks.bind_template",
        ),
        (
            SqlSignRepository._enqueue_binding,
            "app.services.sign_repository",
            "sign.bind",
            "app.tasks.bind_sign",
        ),
    ],
)
@pytest.mark.asyncio
async def test_vendor_repository_emits_only_stable_id_binding_intent(
    helper: Any,
    module_name: str,
    event_type: str,
    task_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[OutboxEventSpec] = []

    async def capture(_connection: object, spec: OutboxEventSpec) -> UUID:
        captured.append(spec)
        return uuid4()

    module = __import__(module_name, fromlist=["enqueue_outbox"])
    monkeypatch.setattr(module, "enqueue_outbox", capture)

    await helper(object(), 7)

    assert len(captured) == 1
    spec = captured[0]
    assert spec.event_type == event_type
    assert spec.task_name == task_name
    assert spec.queue == "realtime"
    assert spec.aggregate_id == "7"
    assert spec.args == (7,)
    assert spec.max_attempts == 1
    validate_spec(spec)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event_type", "usage.changed"),
        ("aggregate_type", "sms_batch"),
        ("aggregate_id", "13800138000"),
        ("args", ("13800138000",)),
        ("dedup_key", "usage.release:13800138000"),
    ],
)
def test_outbox_contract_rejects_malformed_usage_release_reference(
    field: str,
    value: object,
) -> None:
    reservation_id = "fa39c47a-4a50-468f-85ca-b12345678901"
    spec = {
        "event_type": "usage.release",
        "aggregate_type": "usage_reservation",
        "aggregate_id": reservation_id,
        "task_name": "app.tasks.outbox.release_usage",
        "args": (reservation_id,),
        "dedup_key": f"usage.release:{reservation_id}",
    }
    spec[field] = value

    with pytest.raises(ValueError, match="usage release"):
        validate_spec(event_spec(**spec))


def test_outbox_repository_always_uses_process_shared_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared_engine = object()
    settings = type("SettingsStub", (), {"database_url": "postgresql+asyncpg://test"})()
    monkeypatch.setattr(
        outbox_repository_module,
        "database_engine",
        lambda _database_url: shared_engine,
    )

    assert SqlOutboxRepository(settings)._engine() is shared_engine
    assert SqlOutboxRepository(settings, pooled=True)._engine() is shared_engine


@pytest.mark.asyncio
async def test_quota_compensation_uses_fresh_celery_loop_redis_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients: list[Any] = []
    event_id = uuid4()
    compensation_id = "approval:3:rejected"
    expected_args = (7, "平台部", "notice", "20260726", 1, compensation_id)

    class FakeRedisClient:
        def __init__(self) -> None:
            self.closed = False
            self.eval_calls: list[tuple[object, ...]] = []

        async def eval(self, *args: object) -> list[int]:
            self.eval_calls.append(args)
            return [1, 0, 0]

        async def aclose(self) -> None:
            self.closed = True

    class FakeRedis:
        @staticmethod
        def from_url(redis_url: str, **options: object) -> FakeRedisClient:
            assert redis_url == "redis://test"
            assert options == {"decode_responses": True}
            client = FakeRedisClient()
            clients.append(client)
            return client

    class FakeExecutor:
        def __init__(self, _repository: object) -> None:
            pass

        async def run(
            self,
            selected_event_id: UUID,
            *,
            expected_type: str,
            effect: Any,
        ) -> int:
            assert selected_event_id == event_id
            assert expected_type == "quota.compensation"
            return await effect(
                OutboxClaim(event_id, uuid4(), "quota.compensation", expected_args)
            )

    settings = type(
        "SettingsStub",
        (),
        {
            "database_url": "postgresql+asyncpg://test",
            "redis_control_url": "redis://test",
        },
    )()
    monkeypatch.setattr(outbox_task_module, "Redis", FakeRedis)
    monkeypatch.setattr(outbox_task_module, "OutboxExecutor", FakeExecutor)
    monkeypatch.setattr(outbox_task_module, "get_settings", lambda: settings)

    assert (
        await outbox_task_module._compensate_quota(
            7,
            "平台部",
            "notice",
            "20260726",
            1,
            compensation_id,
            str(event_id),
        )
        == 1
    )
    assert len(clients) == 1
    assert clients[0].closed is True
    assert len(clients[0].eval_calls) == 1


class FakeRepository:
    def __init__(self, leases: list[OutboxLease] | None = None) -> None:
        self.leases = leases or []
        self.claim: OutboxClaim | None = None
        self.events: list[tuple[str, object]] = []
        self.heartbeat_result = True
        self.heartbeat_called = asyncio.Event()

    async def lease_due(self, *, limit: int, lease_seconds: int) -> list[OutboxLease]:
        self.events.append(("lease", (limit, lease_seconds)))
        return self.leases

    async def mark_published(self, event_id: UUID, lease_id: UUID) -> None:
        self.events.append(("published", (event_id, lease_id)))

    async def mark_publish_failed(
        self,
        event_id: UUID,
        lease_id: UUID,
        error_type: str,
    ) -> None:
        self.events.append(("publish_failed", (event_id, lease_id, error_type)))

    async def claim_execution(
        self,
        event_id: UUID,
        *,
        lease_seconds: int,
    ) -> OutboxClaim | None:
        self.events.append(("claim", (event_id, lease_seconds)))
        return self.claim

    async def heartbeat(
        self,
        event_id: UUID,
        lease_id: UUID,
        *,
        lease_seconds: int,
    ) -> bool:
        self.events.append(("heartbeat", (event_id, lease_id, lease_seconds)))
        self.heartbeat_called.set()
        return self.heartbeat_result

    async def complete(self, event_id: UUID, lease_id: UUID) -> None:
        self.events.append(("complete", (event_id, lease_id)))

    async def fail_execution(
        self,
        event_id: UUID,
        lease_id: UUID,
        error_type: str,
    ) -> None:
        self.events.append(("execution_failed", (event_id, lease_id, error_type)))


class FakePublisher:
    def __init__(self, failing: set[UUID] | None = None) -> None:
        self.failing = failing or set()
        self.published: list[UUID] = []

    async def publish(self, event: OutboxLease) -> None:
        if event.event_id in self.failing:
            raise ConnectionError("broker unavailable")
        self.published.append(event.event_id)


def lease(event_id: UUID | None = None) -> OutboxLease:
    return OutboxLease(
        event_id or uuid4(),
        uuid4(),
        "batch.ready",
        "app.tasks.send.process_batch",
        "realtime",
        ("BATCH-1",),
        1,
    )


@pytest.mark.asyncio
async def test_dispatcher_records_publish_success_and_broker_failure() -> None:
    good = lease()
    bad = lease()
    repository = FakeRepository([good, bad])
    publisher = FakePublisher({bad.event_id})

    assert await OutboxDispatcher(repository, publisher).dispatch_once() == 1
    assert publisher.published == [good.event_id]
    assert ("published", (good.event_id, good.lease_id)) in repository.events
    assert (
        "publish_failed",
        (bad.event_id, bad.lease_id, "ConnectionError"),
    ) in repository.events


@pytest.mark.asyncio
async def test_duplicate_delivery_without_execution_claim_has_no_effect() -> None:
    repository = FakeRepository()
    called = False

    async def effect(_: OutboxClaim) -> int:
        nonlocal called
        called = True
        return 1

    result = await OutboxExecutor(repository).run(
        uuid4(),
        expected_type="batch.ready",
        effect=effect,
    )

    assert result == 0
    assert called is False


@pytest.mark.asyncio
async def test_execution_failure_is_persisted_by_safe_exception_type() -> None:
    repository = FakeRepository()
    repository.claim = OutboxClaim(uuid4(), uuid4(), "batch.ready", ("BATCH-1",))

    async def effect(_: OutboxClaim) -> int:
        raise TimeoutError("may contain sensitive details")

    with pytest.raises(TimeoutError):
        await OutboxExecutor(repository).run(
            repository.claim.event_id,
            expected_type="batch.ready",
            effect=effect,
        )

    assert (
        "execution_failed",
        (
            repository.claim.event_id,
            repository.claim.lease_id,
            "TimeoutError",
        ),
    ) in repository.events


async def immediate_first_heartbeat(
    awaitable: Awaitable[bool],
    **options: float,
) -> bool:
    global WAIT_FOR_CALLS
    assert options["timeout"] > 0
    WAIT_FOR_CALLS += 1
    if WAIT_FOR_CALLS == 1:
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        raise TimeoutError
    return await awaitable


@pytest.mark.asyncio
async def test_long_execution_renews_lease_before_completing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    global WAIT_FOR_CALLS
    repository = FakeRepository()
    repository.claim = OutboxClaim(uuid4(), uuid4(), "batch.ready", ("BATCH-1",))
    WAIT_FOR_CALLS = 0
    monkeypatch.setattr(asyncio, "wait_for", immediate_first_heartbeat)

    async def effect(_: OutboxClaim) -> int:
        await repository.heartbeat_called.wait()
        return 1

    assert (
        await OutboxExecutor(repository, lease_seconds=15).run(
            repository.claim.event_id,
            expected_type="batch.ready",
            effect=effect,
        )
        == 1
    )
    assert any(event[0] == "heartbeat" for event in repository.events)
    assert any(event[0] == "complete" for event in repository.events)


@pytest.mark.asyncio
async def test_lost_heartbeat_fencing_prevents_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    global WAIT_FOR_CALLS
    repository = FakeRepository()
    repository.claim = OutboxClaim(uuid4(), uuid4(), "batch.ready", ("BATCH-1",))
    repository.heartbeat_result = False
    WAIT_FOR_CALLS = 0
    monkeypatch.setattr(asyncio, "wait_for", immediate_first_heartbeat)

    async def effect(_: OutboxClaim) -> int:
        await repository.heartbeat_called.wait()
        return 1

    with pytest.raises(OutboxLeaseLost):
        await OutboxExecutor(repository, lease_seconds=15).run(
            repository.claim.event_id,
            expected_type="batch.ready",
            effect=effect,
        )
    assert not any(event[0] == "complete" for event in repository.events)
    assert not any(event[0] == "execution_failed" for event in repository.events)


@pytest.mark.asyncio
async def test_celery_publisher_fixes_task_id_to_outbox_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[dict[str, object]] = []

    def send_task(task_name: str, **values: object) -> None:
        sent.append({"task_name": task_name, **values})

    monkeypatch.setattr(celery_app, "send_task", send_task)
    event = lease()

    await CeleryOutboxPublisher().publish(event)

    assert sent == [
        {
            "task_name": "app.tasks.send.process_batch",
            "args": ["BATCH-1", str(event.event_id)],
            "queue": "realtime",
            "task_id": str(event.event_id),
            "headers": {"correlation_id": str(event.event_id)},
            "ignore_result": True,
        }
    ]


class _FakeResult:
    def __init__(
        self,
        *,
        scalar: object = None,
        rows: list[dict[str, object]] | None = None,
    ) -> None:
        self.scalar = scalar
        self.rows = rows or []

    def scalar_one(self) -> object:
        return self.scalar

    def mappings(self) -> _FakeResult:
        return self

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self.rows)


class _FakeConnection:
    def __init__(self, results: list[_FakeResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, Any]] = []

    async def execute(self, statement: object, params: Any = None) -> _FakeResult:
        self.calls.append((str(statement), params))
        return self.results.pop(0)


class _FakeContext:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _FakeConnection:
        return self.connection

    async def __aexit__(self, *_: object) -> None:
        return None


class _FakeEngine:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection

    def connect(self) -> _FakeContext:
        return _FakeContext(self.connection)


@pytest.mark.asyncio
async def test_list_events_sql_filters_state_dead_first_and_hides_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    moment = datetime(2026, 7, 12, 8, 0, tzinfo=UTC)
    connection = _FakeConnection(
        [
            _FakeResult(scalar=1),
            _FakeResult(
                rows=[
                    {
                        "id": "c0a80101-0000-4000-8000-000000000134",
                        "event_type": "usage.release",
                        "aggregate_type": "usage_reservation",
                        "aggregate_id": "c0a80101-0000-4000-8000-000000000134",
                        "task_name": "app.tasks.outbox.release_usage",
                        "queue": "realtime",
                        "state": "dead",
                        "attempts": 12,
                        "max_attempts": 12,
                        "failure_count": 3,
                        "last_error": "BrokerTimeout",
                        "next_attempt_at": moment,
                        "created_at": moment,
                        "updated_at": moment,
                    }
                ]
            ),
        ]
    )
    monkeypatch.setattr(
        outbox_repository_module,
        "database_engine",
        lambda _database_url: _FakeEngine(connection),
    )
    settings = type("SettingsStub", (), {"database_url": "postgresql+asyncpg://test"})()
    repository = SqlOutboxRepository(settings)

    page = await repository.list_events("dead", 2, 20)

    assert page.total == 1 and page.page == 2 and page.page_size == 20
    item = page.items[0]
    assert item.id == UUID("c0a80101-0000-4000-8000-000000000134")
    assert item.state == "dead" and item.last_error == "BrokerTimeout"
    assert item.attempts == 12 and item.max_attempts == 12
    count_sql, count_params = connection.calls[0]
    rows_sql, rows_params = connection.calls[1]
    assert "count(*)" in count_sql
    assert "CAST(:state AS varchar(16))" in count_sql
    for sql in (count_sql, rows_sql):
        assert "args" not in sql
        assert "dedup_key" not in sql
        assert "correlation_id" not in sql
    assert "(state='dead') DESC" in rows_sql
    assert "updated_at DESC" in rows_sql
    expected_params = {"state": "dead", "limit": 20, "offset": 20}
    assert count_params == expected_params
    assert rows_params == expected_params


@pytest.mark.asyncio
async def test_list_events_rejects_invalid_page() -> None:
    settings = type("SettingsStub", (), {"database_url": "postgresql+asyncpg://test"})()
    repository = SqlOutboxRepository(settings)
    with pytest.raises(ValueError, match="invalid outbox event page"):
        await repository.list_events(None, 0, 20)
    with pytest.raises(ValueError, match="invalid outbox event page"):
        await repository.list_events(None, 1, 101)
