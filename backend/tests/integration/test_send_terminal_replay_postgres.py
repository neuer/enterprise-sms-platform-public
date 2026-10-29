"""终态批次的遗留 batch.ready 必须无副作用完成，不能变成长期积压。"""

from __future__ import annotations

import base64
import os
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.services.crypto import CryptoService
from app.services.outbox import OutboxClaim, OutboxEventSpec, OutboxExecutor
from app.services.outbox_repository import SqlOutboxRepository, enqueue_outbox
from app.tasks.send_repository import SqlChunkStore

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["completed", "completed_unknown", "cancelled", "rejected", "expired"]
)
@pytest.mark.parametrize("category", ["notice", "market"])
async def test_terminal_batch_ready_replay_completes_without_new_work(
    status: str,
    category: str,
) -> None:
    """模拟业务终态先落库、批次规划事件尚未确认的恢复边界。"""
    url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    engine = create_async_engine(url)
    settings = cast(Any, SimpleNamespace(database_url=url))
    secret = base64.b64encode(b"t" * 32).decode()
    store = SqlChunkStore(
        CryptoService.from_secret_values(secret, secret), settings, redis=object()
    )
    store._engine = lambda: engine  # type: ignore[method-assign,return-value]
    repository = SqlOutboxRepository(settings)
    repository._engine = lambda: engine  # type: ignore[method-assign,return-value]
    batch_no = uuid4().hex
    lane = "bulk" if category == "market" else "realtime"
    try:
        async with engine.begin() as connection:
            batch_id = await connection.scalar(
                text("""
                    INSERT INTO sms_batch(batch_no,channel,dept,display_content_enc,
                                          send_content_enc,category,status)
                    VALUES(:batch,'web','synthetic',:sealed,:sealed,:category,:status)
                    RETURNING id
                """),
                {
                    "batch": batch_no,
                    "sealed": b"synthetic-ciphertext",
                    "category": category,
                    "status": status,
                },
            )
            event_id = await enqueue_outbox(
                connection,
                OutboxEventSpec(
                    event_type="batch.ready",
                    aggregate_type="sms_batch",
                    aggregate_id=batch_no,
                    task_name="app.tasks.send.process_batch",
                    queue=lane,
                    args=(batch_no,),
                    dedup_key=f"batch.ready:{batch_no}",
                    correlation_id=uuid4(),
                ),
            )

        leases = await repository.lease_due(limit=1, lease_seconds=60)
        assert len(leases) == 1 and leases[0].event_id == event_id
        await repository.mark_published(event_id, leases[0].lease_id)

        async def effect(claim: OutboxClaim) -> int:
            assert claim.args == (batch_no,)
            chunks, selected_lane = await store.prepare_chunks(batch_no, 500)
            assert selected_lane == lane
            return len(chunks)

        executor = OutboxExecutor(repository)
        assert await executor.run(event_id, expected_type="batch.ready", effect=effect) == 0
        assert await executor.run(event_id, expected_type="batch.ready", effect=effect) == 0
        async with engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text("SELECT state,last_error FROM outbox_event WHERE id=:id"),
                        {"id": event_id},
                    )
                )
                .mappings()
                .one()
            )
            assert row["state"] == "completed" and row["last_error"] is None
            assert (
                await connection.scalar(
                    text("SELECT status FROM sms_batch WHERE id=:id"), {"id": batch_id}
                )
                == status
            )
            for table in ("sms_chunk", "sms_message", "usage_chunk_allocation"):
                assert (
                    await connection.scalar(
                        text(f"SELECT count(*) FROM {table} WHERE batch_id=:id"),
                        {"id": batch_id},
                    )
                    == 0
                )
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM outbox_event WHERE aggregate_id=:batch"),
                    {"batch": batch_no},
                )
                == 1
            )
    finally:
        await engine.dispose()
