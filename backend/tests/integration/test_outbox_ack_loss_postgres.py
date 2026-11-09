"""真实 PG/Redis 验证发布确认丢失后的重复投递与消费所有权。"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import text

from app.services import runtime_heartbeat
from app.services.outbox import (
    OutboxClaim,
    OutboxDispatcher,
    OutboxEventSpec,
    OutboxExecutor,
    OutboxLease,
)
from app.services.outbox_repository import SqlOutboxRepository, enqueue_outbox
from tests.integration.test_inflight_split_capacity_postgres import (
    _insert_app,
    _seed_parent,
)
from tests.integration.test_inflight_split_capacity_postgres import (
    split_env as split_env,
)

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ or "AUTH_GUARD_REDIS_URL" not in os.environ,
    reason="requires isolated migrated PostgreSQL and Redis",
)


@pytest.mark.asyncio
async def test_broker_accepts_then_ack_is_lost_duplicate_delivery_has_one_effect(
    split_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """发送到真实 Redis 后注入确认丢失；PG 消费租约只允许一次 Mock 副作用。"""
    engine = split_env
    app_id = await _insert_app(engine, uuid4().hex[:16], limit=8)
    _, chunk_id, _ = await _seed_parent(engine, app_id=app_id, limit=8)
    async with engine.begin() as connection:
        batch_no = str(
            await connection.scalar(
                text(
                    "SELECT batch_no FROM sms_batch "
                    "WHERE id=(SELECT batch_id FROM sms_chunk WHERE id=:id)"
                ),
                {"id": chunk_id},
            )
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
            ),
        )
    repository = SqlOutboxRepository(cast(Any, SimpleNamespace(database_url=engine.url)))
    repository._engine = lambda: engine  # type: ignore[method-assign]

    async def heartbeat(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(runtime_heartbeat, "touch_runtime_heartbeat", heartbeat)
    redis_url = os.environ["AUTH_GUARD_REDIS_URL"]
    assert urlsplit(redis_url).hostname in {"127.0.0.1", "localhost"}
    broker = Redis.from_url(redis_url, decode_responses=True)
    queue = f"test:outbox-ack:{uuid4()}"

    class AckLossPublisher:
        calls = 0

        async def publish(self, event: OutboxLease) -> None:
            assert event.event_id == event_id
            await broker.rpush(queue, str(event.event_id))
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError("synthetic acknowledgement lost after acceptance")

    async def make_due() -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE outbox_event SET next_attempt_at='1970-01-01 UTC' WHERE id=:id"),
                {"id": event_id},
            )

    async def state() -> str:
        async with engine.connect() as connection:
            return str(
                await connection.scalar(
                    text("SELECT state FROM outbox_event WHERE id=:id"),
                    {"id": event_id},
                )
            )

    publisher = AckLossPublisher()
    dispatcher = OutboxDispatcher(repository, publisher, batch_size=1, publish_concurrency=1)
    mock_effects: list[str] = []

    async def mock_delivery(claim: OutboxClaim) -> int:
        assert claim.event_id == event_id and claim.args == (batch_no,)
        mock_effects.append(batch_no)
        return 1

    try:
        await make_due()
        assert await dispatcher.dispatch_once() == 0
        assert await broker.llen(queue) == 1
        assert await state() == "pending"
        await make_due()
        assert await dispatcher.dispatch_once() == 1
        assert await state() == "published" and mock_effects == []
        assert await broker.llen(queue) == 2
        executor = OutboxExecutor(repository, lease_seconds=15)
        results = []
        for _ in range(2):
            delivered = await broker.lpop(queue)
            assert delivered == str(event_id)
            results.append(
                await executor.run(
                    UUID(delivered),
                    expected_type="batch.ready",
                    effect=mock_delivery,
                )
            )
        assert results == [1, 0]
        assert mock_effects == [batch_no]
        assert publisher.calls == 2 and await state() == "completed"
    finally:
        await broker.delete(queue)
        await broker.aclose()
