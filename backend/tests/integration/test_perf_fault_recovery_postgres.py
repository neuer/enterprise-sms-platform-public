"""C18：在受控迁移库执行真实 submitting 超时 SQL，不下发短信。"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.services import reconcile_repository, runtime_heartbeat
from app.services.outbox import (
    OutboxClaim,
    OutboxDispatcher,
    OutboxEventSpec,
    OutboxExecutor,
    OutboxLease,
)
from app.services.outbox_repository import SqlOutboxRepository, enqueue_outbox
from app.services.reconcile_repository import SqlRecoveryRepository
from tests.integration.test_inflight_split_capacity_postgres import (
    _chunk_status,
    _insert_app,
    _seed_parent,
)
from tests.integration.test_inflight_split_capacity_postgres import (
    split_env as split_env,
)

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


@pytest.mark.asyncio
async def test_submitting_timeout_is_uncertain_and_never_requeued(
    split_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = split_env
    app_id = await _insert_app(engine, uuid4().hex[:16], limit=8)
    _, stale_id, _ = await _seed_parent(engine, app_id=app_id, limit=8)
    _, fresh_id, _ = await _seed_parent(engine, app_id=app_id, limit=8)
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE sms_chunk SET submitting_since=now()-interval '6 minutes' WHERE id=:id"),
            {"id": stale_id},
        )
        await connection.execute(
            text("UPDATE sms_chunk SET submitting_since=now() WHERE id=:id"),
            {"id": fresh_id},
        )
    monkeypatch.setattr(reconcile_repository, "database_engine", lambda *_args, **_kw: engine)
    repository = SqlRecoveryRepository(cast(Any, SimpleNamespace(database_url=engine.url)))
    for _ in range(2):
        work = await repository.stalled()
        assert not any(item.chunk_id in {stale_id, fresh_id} for item in work)
        assert await _chunk_status(engine, stale_id) == "uncertain"
        assert await _chunk_status(engine, fresh_id) == "submitting"
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text("SELECT submitting_since, uncertain_since FROM sms_chunk WHERE id=:id"),
                {"id": stale_id},
            )
        ).one()
    assert row.submitting_since is None and row.uncertain_since is not None


@pytest.mark.asyncio
async def test_same_outbox_event_recovers_after_broker_failure_and_executes_once(
    split_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一PG事实经历发布失败和恢复，真实执行租约保证重复投递不重复副作用。"""

    engine = split_env
    app_id = await _insert_app(engine, uuid4().hex[:16], limit=8)
    _, chunk_id, _ = await _seed_parent(engine, app_id=app_id, limit=8)
    async with engine.begin() as connection:
        batch_no = str(
            (
                await connection.execute(
                    text(
                        "SELECT batch.batch_no FROM sms_batch batch "
                        "JOIN sms_chunk chunk ON chunk.batch_id=batch.id WHERE chunk.id=:id"
                    ),
                    {"id": chunk_id},
                )
            ).scalar_one()
        )
        event_id = await enqueue_outbox(
            connection,
            OutboxEventSpec(
                event_type="batch.ready",
                aggregate_type="sms_batch",
                aggregate_id=batch_no,
                task_name="app.tasks.send.process_batch",
                queue="realtime",
                args=(batch_no,),
                dedup_key=f"batch.ready:{batch_no}",
                correlation_id=uuid4(),
            ),
        )

    repository = SqlOutboxRepository(cast(Any, SimpleNamespace(database_url=engine.url)))
    monkeypatch.setattr(repository, "_engine", lambda: engine)

    async def heartbeat(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(runtime_heartbeat, "touch_runtime_heartbeat", heartbeat)

    class RecoveringPublisher:
        unavailable = True

        def __init__(self) -> None:
            self.calls: list[OutboxLease] = []

        async def publish(self, event: OutboxLease) -> None:
            assert event.event_id == event_id
            self.calls.append(event)
            if self.unavailable:
                raise ConnectionError("synthetic broker unavailable")

    publisher = RecoveringPublisher()
    dispatcher = OutboxDispatcher(repository, publisher, batch_size=1, publish_concurrency=1)

    async def make_due() -> None:
        # 只调整本例事件的退避时间，确保共享隔离库的其它待处理事实不被本例消费。
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE outbox_event SET next_attempt_at='1970-01-01 UTC' WHERE id=:id"),
                {"id": event_id},
            )

    async def snapshot() -> dict[str, Any]:
        async with engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            "SELECT event.state,event.attempts,event.failure_count,event.args,"
                            "event.lease_id,batch.remark,batch.status AS batch_status "
                            "FROM outbox_event event JOIN sms_batch batch "
                            "ON batch.batch_no=event.aggregate_id WHERE event.id=:id"
                        ),
                        {"id": event_id},
                    )
                )
                .mappings()
                .one()
            )
        return dict(row)

    original = await snapshot()
    await make_due()
    assert await dispatcher.dispatch_once() == 0
    failed = await snapshot()
    assert failed["state"] == "pending" and failed["lease_id"] is None
    assert failed["attempts"] == 1 and failed["failure_count"] == 1
    assert failed["args"] == [batch_no]
    assert failed["batch_status"] == original["batch_status"]
    assert failed["remark"] == original["remark"]

    publisher.unavailable = False
    await make_due()
    assert await dispatcher.dispatch_once() == 1
    published = await snapshot()
    assert published["state"] == "published"
    assert published["attempts"] == 2 and published["failure_count"] == 1
    assert [event.event_id for event in publisher.calls] == [event_id, event_id]
    assert publisher.calls[0].lease_id != publisher.calls[1].lease_id

    async def effect(claim: OutboxClaim) -> int:
        assert claim.event_id == event_id and claim.args == (batch_no,)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE sms_batch SET remark=COALESCE(remark,'')||'.' WHERE batch_no=:batch_no"
                ),
                {"batch_no": batch_no},
            )
        return 1

    executor = OutboxExecutor(repository, lease_seconds=15)
    assert await executor.run(event_id, expected_type="batch.ready", effect=effect) == 1
    assert await executor.run(event_id, expected_type="batch.ready", effect=effect) == 0
    completed = await snapshot()
    assert completed["state"] == "completed" and completed["lease_id"] is None
    assert completed["remark"] == (original["remark"] or "") + "."
    assert completed["batch_status"] == original["batch_status"]
