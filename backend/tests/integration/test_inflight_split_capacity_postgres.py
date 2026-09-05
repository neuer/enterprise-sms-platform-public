from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.correlation import correlation_scope
from app.core.runtime_resources import (
    _set_audit_transaction_context,
    bind_connection_system_audit,
)
from app.services.pipeline import InFlightLimitExceeded
from app.services.send_inflight import (
    InFlightReservation,
    bind_in_flight_reservation,
    materialize_in_flight_reservation,
    reconcile_split_occupancy_drift,
    release_in_flight_reservation,
    reserve_in_flight_chunks,
)
from app.tasks.send import ChunkPayload
from app.tasks.send_repository import complete_vendor_split, retry_capacity_blocked_splits
from scripts_support.maintain_partitions import maintain

_BACKEND_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


async def _prepare_db(engine: Any) -> None:
    async with engine.begin() as connection:
        await bind_connection_system_audit(
            connection,
            actor_name="partition-maintenance",
            action="partition.maintenance",
            producer_domain="api",
        )
        await maintain(connection, future_months=3)


async def _insert_app(engine: Any, nonce: str, *, limit: int = 200) -> int:
    async with engine.begin() as connection:
        app_id = int(
            (
                await connection.execute(
                    text(
                        """
                        INSERT INTO app(
                          name,dept,api_key_hash,api_key_prefix,created_by,
                          max_in_flight_chunks
                        ) VALUES(
                          :name,'平台部',:api_key_hash,:api_key_prefix,'test',
                          :limit
                        ) RETURNING id
                        """
                    ),
                    {
                        "name": f"split-{nonce}",
                        "api_key_hash": "c" * 64,
                        "api_key_prefix": nonce[:8],
                        "limit": limit,
                    },
                )
            ).scalar_one()
        )
    return app_id


async def _balance(engine: Any, app_id: int) -> int:
    async with engine.connect() as connection:
        value = (
            await connection.execute(
                text(
                    "SELECT reserved_chunks FROM send_inflight_balance WHERE app_id=:app_id"
                ),
                {"app_id": app_id},
            )
        ).scalar_one()
    return int(value)


async def _reservation_chunks(engine: Any, reservation_id: int) -> int:
    async with engine.connect() as connection:
        value = (
            await connection.execute(
                text(
                    """
                    SELECT reserved_chunks
                    FROM send_inflight_reservation
                    WHERE id=:id
                    """
                ),
                {"id": reservation_id},
            )
        ).scalar_one()
    return int(value)


async def _chunk_status(engine: Any, chunk_id: int) -> str:
    async with engine.connect() as connection:
        return str(
            (
                await connection.execute(
                    text("SELECT status FROM sms_chunk WHERE id=:id"),
                    {"id": chunk_id},
                )
            ).scalar_one()
        )


async def _drop_occupancy_triggers(connection: Any) -> None:
    await connection.execute(
        text("DROP TRIGGER IF EXISTS trg_sms_chunk_inflight_occupancy ON sms_chunk")
    )
    await connection.execute(
        text(
            "DROP TRIGGER IF EXISTS trg_send_inflight_reservation_occupancy "
            "ON send_inflight_reservation"
        )
    )


async def _restore_occupancy_triggers(connection: Any) -> None:
    await connection.execute(
        text(
            """
            CREATE CONSTRAINT TRIGGER trg_sms_chunk_inflight_occupancy
            AFTER INSERT OR UPDATE OR DELETE ON sms_chunk
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW
            EXECUTE FUNCTION check_send_inflight_chunk_occupancy()
            """
        )
    )
    await connection.execute(
        text(
            """
            CREATE CONSTRAINT TRIGGER trg_send_inflight_reservation_occupancy
            AFTER INSERT OR UPDATE OR DELETE ON send_inflight_reservation
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW
            EXECUTE FUNCTION check_send_inflight_chunk_occupancy()
            """
        )
    )


async def _insert_invoking_attempt(engine: Any, chunk_id: int) -> int:
    async with engine.begin() as connection:
        return int(
            (
                await connection.execute(
                    text(
                        """
                        INSERT INTO sms_vendor_attempt(
                          chunk_id, vendor_id, generation, outcome
                        ) VALUES (
                          :chunk_id, 'zhihui', 1, 'invoking'
                        )
                        RETURNING id
                        """
                    ),
                    {"chunk_id": chunk_id},
                )
            ).scalar_one()
        )


async def _attempt_snapshot(engine: Any, chunk_id: int) -> dict[str, Any]:
    async with engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    text(
                        """
                        SELECT outcome, vendor_code
                        FROM sms_vendor_attempt
                        WHERE chunk_id=:chunk_id
                        ORDER BY generation DESC
                        LIMIT 1
                        """
                    ),
                    {"chunk_id": chunk_id},
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def _seed_parent(
    engine: Any,
    *,
    app_id: int,
    limit: int,
    status: str = "submitting",
    phones: int = 4,
) -> tuple[int, int, ChunkPayload]:
    nonce = uuid4().hex
    batch_no = nonce
    custom_id = nonce
    async with engine.begin() as connection:
        reserved = await reserve_in_flight_chunks(
            connection,
            app_id=app_id,
            estimated=1,
            limit=limit,
        )
        batch_id = int(
            (
                await connection.execute(
                    text(
                        """
                        INSERT INTO sms_batch(
                          batch_no,category,channel,app_id,dept,content,
                          display_content_enc,send_content_enc,status,total,
                          segments
                        ) VALUES(
                          :batch_no,'notice','api',:app_id,'平台部','[encrypted]',
                          :cipher,:cipher,'sending',:phones,1
                        ) RETURNING id
                        """
                    ),
                    {
                        "batch_no": batch_no,
                        "app_id": app_id,
                        "cipher": b"synthetic-ciphertext",
                        "phones": phones,
                    },
                )
            ).scalar_one()
        )
        await bind_in_flight_reservation(
            connection,
            reservation_id=reserved.id,
            generation=reserved.generation,
            batch_id=batch_id,
            app_id=app_id,
        )
        chunk_id = int(
            (
                await connection.execute(
                    text(
                        """
                        INSERT INTO sms_chunk(
                          batch_id,chunk_no,custom_id,phone_count,status
                        ) VALUES (
                          :batch_id,1,:custom_id,:phones,:status
                        ) RETURNING id
                        """
                    ),
                    {
                        "batch_id": batch_id,
                        "custom_id": custom_id,
                        "phones": phones,
                        "status": status,
                    },
                )
            ).scalar_one()
        )
        await connection.execute(
            text(
                """
                INSERT INTO usage_chunk_allocation (
                  chunk_id, batch_id, recipient_count, segment_count,
                  request_count, app_id
                ) VALUES (
                  :chunk_id, :batch_id, :phones, :phones, 1, :app_id
                )
                """
            ),
            {
                "chunk_id": chunk_id,
                "batch_id": batch_id,
                "phones": phones,
                "app_id": app_id,
            },
        )
        for index in range(phones):
            await connection.execute(
                text(
                    """
                    INSERT INTO sms_message(
                      batch_id,chunk_id,phone_enc,phone_hmac,phone_mask,key_version
                    ) VALUES (
                      :batch_id,:chunk_id,:phone_enc,:phone_hmac,:phone_mask,1
                    )
                    """
                ),
                {
                    "batch_id": batch_id,
                    "chunk_id": chunk_id,
                    "phone_enc": b"synthetic-phone",
                    "phone_hmac": f"{index:064x}",
                    "phone_mask": "138****0000",
                },
            )
        await materialize_in_flight_reservation(
            connection,
            batch_id=batch_id,
            actual_chunks=1,
            limit=limit,
        )
    payload = ChunkPayload(
        chunk_id=chunk_id,
        batch_id=batch_id,
        custom_id=custom_id,
        phones=tuple("1" * 11 for _ in range(phones)),
        content="",
        template_id="",
        sign_name="",
    )
    return reserved.id, chunk_id, payload


@pytest_asyncio.fixture
async def split_env() -> Any:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    engine = create_async_engine(database_url, hide_parameters=True)
    await _prepare_db(engine)
    sync_engine = engine.sync_engine
    if not getattr(sync_engine, "_sms_inflight_audit_begin", False):
        event.listen(sync_engine, "begin", _set_audit_transaction_context)
        sync_engine._sms_inflight_audit_begin = True
    try:
        yield engine
    finally:
        if getattr(sync_engine, "_sms_inflight_audit_begin", False):
            event.remove(sync_engine, "begin", _set_audit_transaction_context)
            sync_engine._sms_inflight_audit_begin = False
        await engine.dispose()


@pytest.mark.asyncio
async def test_split_expands_reservation_and_balance_by_one(split_env: Any) -> None:
    engine = split_env
    nonce = uuid4().hex[:16]
    app_id = await _insert_app(engine, nonce, limit=8)
    reservation_id, parent_id, payload = await _seed_parent(engine, app_id=app_id, limit=8)
    assert await _balance(engine, app_id) == 1
    with correlation_scope(uuid4()):
        async with engine.begin() as connection:
            children = await complete_vendor_split(connection, payload)
    assert len(children) == 2
    assert await _chunk_status(engine, parent_id) == "failed"
    assert await _reservation_chunks(engine, reservation_id) == 2
    assert await _balance(engine, app_id) == 2
    async with engine.connect() as connection:
        statuses = list(
            (
                await connection.execute(
                    text("SELECT status FROM sms_chunk WHERE id=ANY(:ids) ORDER BY id"),
                    {"ids": children},
                )
            ).scalars()
        )
        allocations = int(
            (
                await connection.execute(
                    text(
                        """
                        SELECT COALESCE(SUM(recipient_count),0)
                        FROM usage_chunk_allocation
                        WHERE chunk_id=ANY(:ids)
                        """
                    ),
                    {"ids": [parent_id, *children]},
                )
            ).scalar_one()
        )
        requests = int(
            (
                await connection.execute(
                    text(
                        """
                        SELECT COALESCE(SUM(request_count),0)
                        FROM usage_chunk_allocation
                        WHERE chunk_id=ANY(:ids)
                        """
                    ),
                    {"ids": [parent_id, *children]},
                )
            ).scalar_one()
        )
        parent_counts = (
            (
                await connection.execute(
                    text(
                        """
                        SELECT request_count, recipient_count, segment_count
                        FROM usage_chunk_allocation
                        WHERE chunk_id=:id
                        """
                    ),
                    {"id": parent_id},
                )
            )
            .mappings()
            .one()
        )
        outbox = int(
            (
                await connection.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM outbox_event
                        WHERE event_type='chunk.ready'
                          AND aggregate_id=ANY(:ids)
                        """
                    ),
                    {"ids": [str(item) for item in children]},
                )
            ).scalar_one()
        )
        occupying = int(
            (
                await connection.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM sms_chunk
                        WHERE batch_id=:batch_id
                          AND status = ANY (send_chunk_occupying_states())
                        """
                    ),
                    {"batch_id": payload.batch_id},
                )
            ).scalar_one()
        )
    assert statuses == ["pending", "pending"]
    assert allocations == 4
    assert requests == 1
    assert int(parent_counts["request_count"]) == 1
    assert int(parent_counts["recipient_count"]) == 0
    assert int(parent_counts["segment_count"]) == 0
    assert outbox == 2
    assert occupying == 2


@pytest.mark.asyncio
async def test_split_at_limit_blocks_without_children(split_env: Any) -> None:
    engine = split_env
    nonce = uuid4().hex[:16]
    app_id = await _insert_app(engine, nonce, limit=1)
    reservation_id, parent_id, payload = await _seed_parent(engine, app_id=app_id, limit=1)
    with correlation_scope(uuid4()):
        async with engine.begin() as connection:
            children = await complete_vendor_split(connection, payload)
    assert children == []
    assert await _chunk_status(engine, parent_id) == "split_capacity_blocked"
    assert await _reservation_chunks(engine, reservation_id) == 1
    assert await _balance(engine, app_id) == 1
    async with engine.connect() as connection:
        extras = int(
            (
                await connection.execute(
                    text(
                        "SELECT COUNT(*) FROM sms_chunk WHERE parent_chunk_id=:id"
                    ),
                    {"id": parent_id},
                )
            ).scalar_one()
        )
    assert extras == 0


@pytest.mark.asyncio
async def test_split_parent_cas_miss_leaves_capacity_unchanged(split_env: Any) -> None:
    engine = split_env
    nonce = uuid4().hex[:16]
    app_id = await _insert_app(engine, nonce, limit=8)
    _reservation_id, parent_id, payload = await _seed_parent(engine, app_id=app_id, limit=8)
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE sms_chunk SET status='submitted' WHERE id=:id"),
            {"id": parent_id},
        )
    with correlation_scope(uuid4()):
        async with engine.begin() as connection:
            children = await complete_vendor_split(connection, payload)
    assert children == []
    assert await _chunk_status(engine, parent_id) == "submitted"
    assert await _balance(engine, app_id) == 1


@pytest.mark.asyncio
async def test_split_replay_returns_same_children(split_env: Any) -> None:
    engine = split_env
    nonce = uuid4().hex[:16]
    app_id = await _insert_app(engine, nonce, limit=8)
    reservation_id, _parent_id, payload = await _seed_parent(engine, app_id=app_id, limit=8)
    with correlation_scope(uuid4()):
        async with engine.begin() as connection:
            first = await complete_vendor_split(connection, payload)
        async with engine.begin() as connection:
            second = await complete_vendor_split(connection, payload)
    assert first == second
    assert len(first) == 2
    assert await _reservation_chunks(engine, reservation_id) == 2
    assert await _balance(engine, app_id) == 2


@pytest.mark.asyncio
async def test_split_and_new_reserve_contend_for_last_slot(split_env: Any) -> None:
    engine = split_env
    nonce = uuid4().hex[:16]
    app_id = await _insert_app(engine, nonce, limit=2)
    _reservation_id, _parent_id, payload = await _seed_parent(engine, app_id=app_id, limit=2)

    async def do_split() -> list[int]:
        with correlation_scope(uuid4()):
            async with engine.begin() as connection:
                return await complete_vendor_split(connection, payload)

    async def do_reserve() -> object:
        async with engine.begin() as connection:
            return await reserve_in_flight_chunks(
                connection,
                app_id=app_id,
                estimated=1,
                limit=2,
            )

    split_result, reserve_result = await asyncio.gather(
        do_split(),
        do_reserve(),
        return_exceptions=True,
    )
    assert await _balance(engine, app_id) == 2
    winners = 0
    if isinstance(split_result, list) and len(split_result) == 2:
        winners += 1
    elif split_result == []:
        assert await _chunk_status(engine, _parent_id) == "split_capacity_blocked"
    else:
        raise AssertionError(f"unexpected split result: {split_result!r}")
    if isinstance(reserve_result, InFlightReservation):
        winners += 1
    elif isinstance(reserve_result, InFlightLimitExceeded):
        pass
    else:
        raise AssertionError(f"unexpected reserve result: {reserve_result!r}")
    assert winners == 1


@pytest.mark.asyncio
async def test_split_then_release_keeps_non_negative_conservation(
    split_env: Any,
) -> None:
    engine = split_env
    nonce = uuid4().hex[:16]
    app_id = await _insert_app(engine, nonce, limit=8)
    reservation_id, _parent_id, payload = await _seed_parent(engine, app_id=app_id, limit=8)
    with correlation_scope(uuid4()):
        async with engine.begin() as connection:
            children = await complete_vendor_split(connection, payload)
    assert len(children) == 2
    async with engine.begin() as connection:
        generation = int(
            (
                await connection.execute(
                    text(
                        "SELECT generation FROM send_inflight_reservation WHERE id=:id"
                    ),
                    {"id": reservation_id},
                )
            ).scalar_one()
        )
        await release_in_flight_reservation(
            connection,
            reservation_id=reservation_id,
            generation=generation,
            reason="batch-completed",
        )
    assert await _balance(engine, app_id) == 0


@pytest.mark.asyncio
async def test_occupancy_drift_expands_only(split_env: Any) -> None:
    engine = split_env
    nonce = uuid4().hex[:16]
    app_id = await _insert_app(engine, nonce, limit=8)
    reservation_id, parent_id, payload = await _seed_parent(engine, app_id=app_id, limit=8)
    async with engine.begin() as connection:
        await _drop_occupancy_triggers(connection)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                UPDATE sms_chunk SET status='failed', vendor_code=1006
                WHERE id=:id
                """
            ),
            {"id": parent_id},
        )
        await connection.execute(
            text(
                """
                INSERT INTO sms_chunk(batch_id,chunk_no,custom_id,phone_count,status)
                VALUES
                  (:batch_id,2,:custom_a,2,'pending'),
                  (:batch_id,3,:custom_b,2,'pending')
                """
            ),
            {
                "batch_id": payload.batch_id,
                "custom_a": uuid4().hex,
                "custom_b": uuid4().hex,
            },
        )
    async with engine.begin() as connection:
        await _restore_occupancy_triggers(connection)
    assert await _reservation_chunks(engine, reservation_id) == 1
    async with engine.begin() as connection:
        repaired = await reconcile_split_occupancy_drift(connection)
    assert repaired == 1
    assert await _reservation_chunks(engine, reservation_id) == 2
    assert await _balance(engine, app_id) == 2
    async with engine.begin() as connection:
        again = await reconcile_split_occupancy_drift(connection)
    assert again == 0
    assert await _reservation_chunks(engine, reservation_id) == 2


@pytest.mark.asyncio
async def test_blocked_split_retries_after_capacity_release(split_env: Any) -> None:
    engine = split_env
    nonce = uuid4().hex[:16]
    app_id = await _insert_app(engine, nonce, limit=1)
    reservation_id, parent_id, payload = await _seed_parent(engine, app_id=app_id, limit=1)
    with correlation_scope(uuid4()):
        async with engine.begin() as connection:
            assert await complete_vendor_split(connection, payload) == []
    assert await _chunk_status(engine, parent_id) == "split_capacity_blocked"
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE app SET max_in_flight_chunks=8 WHERE id=:id"),
            {"id": app_id},
        )
    with correlation_scope(uuid4()):
        async with engine.begin() as connection:
            retried = await retry_capacity_blocked_splits(connection)
    assert retried == 1
    assert await _chunk_status(engine, parent_id) == "failed"
    assert await _reservation_chunks(engine, reservation_id) == 2
    assert await _balance(engine, app_id) == 2


@pytest.mark.asyncio
async def test_split_exception_after_expand_rolls_back_whole_transaction(
    split_env: Any,
) -> None:
    engine = split_env
    nonce = uuid4().hex[:16]
    app_id = await _insert_app(engine, nonce, limit=8)
    reservation_id, parent_id, payload = await _seed_parent(engine, app_id=app_id, limit=8)
    from app.services.send_inflight import expand_in_flight_for_split

    with correlation_scope(uuid4()), pytest.raises(RuntimeError, match="killed"):
        async with engine.begin() as connection:
            await expand_in_flight_for_split(
                connection,
                batch_id=payload.batch_id,
                delta=1,
                limit=8,
            )
            raise RuntimeError("killed")
    assert await _chunk_status(engine, parent_id) == "submitting"
    assert await _reservation_chunks(engine, reservation_id) == 1
    assert await _balance(engine, app_id) == 1


@pytest.mark.asyncio
async def test_concurrent_parent_splits_each_expand_once(split_env: Any) -> None:
    engine = split_env
    nonce = uuid4().hex[:16]
    app_id = await _insert_app(engine, nonce, limit=8)
    first = await _seed_parent(engine, app_id=app_id, limit=8)
    second = await _seed_parent(engine, app_id=app_id, limit=8)
    assert await _balance(engine, app_id) == 2

    async def do_split(payload: ChunkPayload) -> list[int]:
        with correlation_scope(uuid4()):
            async with engine.begin() as connection:
                return await complete_vendor_split(connection, payload)

    first_children, second_children = await asyncio.gather(
        do_split(first[2]),
        do_split(second[2]),
    )
    assert len(first_children) == 2
    assert len(second_children) == 2
    assert set(first_children).isdisjoint(second_children)
    assert await _balance(engine, app_id) == 4
    assert await _reservation_chunks(engine, first[0]) == 2
    assert await _reservation_chunks(engine, second[0]) == 2


@pytest.mark.asyncio
async def test_concurrent_parent_splits_last_slot_blocks_one(split_env: Any) -> None:
    engine = split_env
    nonce = uuid4().hex[:16]
    app_id = await _insert_app(engine, nonce, limit=3)
    first = await _seed_parent(engine, app_id=app_id, limit=3)
    second = await _seed_parent(engine, app_id=app_id, limit=3)

    async def do_split(payload: ChunkPayload) -> list[int]:
        with correlation_scope(uuid4()):
            async with engine.begin() as connection:
                return await complete_vendor_split(connection, payload)

    outcomes = await asyncio.gather(do_split(first[2]), do_split(second[2]))
    winners = [item for item in outcomes if len(item) == 2]
    blocked = [item for item in outcomes if item == []]
    assert len(winners) == 1
    assert len(blocked) == 1
    assert await _balance(engine, app_id) == 3
    statuses = {
        await _chunk_status(engine, first[1]),
        await _chunk_status(engine, second[1]),
    }
    assert statuses == {"failed", "split_capacity_blocked"}


@pytest.mark.asyncio
async def test_old_style_split_without_expand_fails_commit(split_env: Any) -> None:
    engine = split_env
    nonce = uuid4().hex[:16]
    app_id = await _insert_app(engine, nonce, limit=8)
    reservation_id, parent_id, payload = await _seed_parent(engine, app_id=app_id, limit=8)
    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE sms_chunk SET status='failed' WHERE id=:id"),
                {"id": parent_id},
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO sms_chunk(batch_id,chunk_no,custom_id,phone_count,status)
                    VALUES
                      (:batch_id,2,:custom_a,2,'pending'),
                      (:batch_id,3,:custom_b,2,'pending')
                    """
                ),
                {
                    "batch_id": payload.batch_id,
                    "custom_a": uuid4().hex,
                    "custom_b": uuid4().hex,
                },
            )
    assert await _chunk_status(engine, parent_id) == "submitting"
    assert await _reservation_chunks(engine, reservation_id) == 1
    assert await _balance(engine, app_id) == 1


@pytest.mark.asyncio
async def test_incomplete_split_child_fails_commit(split_env: Any) -> None:
    engine = split_env
    nonce = uuid4().hex[:16]
    app_id = await _insert_app(engine, nonce, limit=8)
    reservation_id, parent_id, payload = await _seed_parent(engine, app_id=app_id, limit=8)
    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE sms_chunk SET status='failed' WHERE id=:id"),
                {"id": parent_id},
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO sms_chunk(
                      batch_id,chunk_no,custom_id,phone_count,
                      parent_chunk_id,split_generation,child_ordinal
                    ) VALUES (
                      :batch_id,2,:custom_id,2,:parent_id,1,1
                    )
                    """
                ),
                {
                    "batch_id": payload.batch_id,
                    "custom_id": uuid4().hex,
                    "parent_id": parent_id,
                },
            )
    assert await _chunk_status(engine, parent_id) == "submitting"
    assert await _reservation_chunks(engine, reservation_id) == 1
    async with engine.connect() as connection:
        extras = int(
            (
                await connection.execute(
                    text("SELECT COUNT(*) FROM sms_chunk WHERE parent_chunk_id=:id"),
                    {"id": parent_id},
                )
            ).scalar_one()
        )
    assert extras == 0


@pytest.mark.asyncio
async def test_split_settles_invoking_vendor_attempt(split_env: Any) -> None:
    engine = split_env
    nonce = uuid4().hex[:16]
    app_id = await _insert_app(engine, nonce, limit=8)
    _reservation_id, parent_id, payload = await _seed_parent(engine, app_id=app_id, limit=8)
    await _insert_invoking_attempt(engine, parent_id)
    with correlation_scope(uuid4()):
        async with engine.begin() as connection:
            children = await complete_vendor_split(connection, payload)
    assert len(children) == 2
    snapshot = await _attempt_snapshot(engine, parent_id)
    assert snapshot["outcome"] == "rejected"
    assert int(snapshot["vendor_code"]) == 1006


@pytest.mark.asyncio
async def test_blocked_split_settles_invoking_vendor_attempt(split_env: Any) -> None:
    engine = split_env
    nonce = uuid4().hex[:16]
    app_id = await _insert_app(engine, nonce, limit=1)
    _reservation_id, parent_id, payload = await _seed_parent(engine, app_id=app_id, limit=1)
    await _insert_invoking_attempt(engine, parent_id)
    with correlation_scope(uuid4()):
        async with engine.begin() as connection:
            assert await complete_vendor_split(connection, payload) == []
    snapshot = await _attempt_snapshot(engine, parent_id)
    assert snapshot["outcome"] == "rejected"
    assert int(snapshot["vendor_code"]) == 1006
    assert await _chunk_status(engine, parent_id) == "split_capacity_blocked"


@pytest.mark.asyncio
async def test_split_and_release_concurrent_keeps_non_negative_balance(
    split_env: Any,
) -> None:
    engine = split_env
    nonce = uuid4().hex[:16]
    app_id = await _insert_app(engine, nonce, limit=8)
    reservation_id, parent_id, payload = await _seed_parent(engine, app_id=app_id, limit=8)

    async def do_split() -> list[int]:
        with correlation_scope(uuid4()):
            async with engine.begin() as connection:
                return await complete_vendor_split(connection, payload)

    async def do_release() -> bool:
        async with engine.begin() as connection:
            generation = int(
                (
                    await connection.execute(
                        text(
                            "SELECT generation FROM send_inflight_reservation WHERE id=:id"
                        ),
                        {"id": reservation_id},
                    )
                ).scalar_one()
            )
            return await release_in_flight_reservation(
                connection,
                reservation_id=reservation_id,
                generation=generation,
                reason="batch-completed",
            )

    await asyncio.gather(do_split(), do_release(), return_exceptions=True)
    assert await _balance(engine, app_id) >= 0
    async with engine.connect() as connection:
        active = int(
            (
                await connection.execute(
                    text(
                        """
                        SELECT COALESCE(SUM(reserved_chunks),0)
                        FROM send_inflight_reservation
                        WHERE app_id=:app_id
                          AND state = ANY (send_inflight_active_states())
                        """
                    ),
                    {"app_id": app_id},
                )
            ).scalar_one()
        )
    assert await _balance(engine, app_id) == active
    assert await _chunk_status(engine, parent_id) in {
        "failed",
        "submitting",
        "split_capacity_blocked",
    }


def child_partial_split() -> None:
    """子进程：扩容并插入一个 child 后挂起，供 SIGKILL 验证未提交半拆分。"""

    import asyncio as child_asyncio

    from sqlalchemy import text as child_text
    from sqlalchemy.engine import make_url as child_make_url
    from sqlalchemy.ext.asyncio import create_async_engine as child_engine

    from app.services.send_inflight import expand_in_flight_for_split

    async def run() -> None:
        engine = child_engine(
            child_make_url(os.environ["OUTBOX_POSTGRES_DSN"]),
            hide_parameters=True,
        )
        ready = Path(os.environ["SMS_SPLIT_KILL_READY"])
        try:
            async with engine.connect() as connection:
                trans = await connection.begin()
                await expand_in_flight_for_split(
                    connection,
                    batch_id=int(os.environ["SMS_SPLIT_BATCH_ID"]),
                    delta=1,
                    limit=int(os.environ["SMS_SPLIT_LIMIT"]),
                )
                await connection.execute(
                    child_text(
                        """
                        UPDATE sms_chunk SET status='failed', vendor_code=1006
                        WHERE id=:id AND status='submitting'
                        """
                    ),
                    {"id": int(os.environ["SMS_SPLIT_PARENT_ID"])},
                )
                await connection.execute(
                    child_text(
                        """
                        INSERT INTO sms_chunk(
                          batch_id,chunk_no,custom_id,phone_count,
                          parent_chunk_id,split_generation,child_ordinal
                        ) VALUES (
                          :batch,2,:custom,2,:parent,1,1
                        )
                        """
                    ),
                    {
                        "batch": int(os.environ["SMS_SPLIT_BATCH_ID"]),
                        "custom": os.environ["SMS_SPLIT_CHILD_CUSTOM"],
                        "parent": int(os.environ["SMS_SPLIT_PARENT_ID"]),
                    },
                )
                ready.write_text("ready", encoding="utf-8")
                while True:
                    await child_asyncio.sleep(1)
                await trans.commit()
        finally:
            await engine.dispose()

    child_asyncio.run(run())


@pytest.mark.asyncio
async def test_kill_after_first_child_does_not_commit_partial_split(
    split_env: Any,
    tmp_path: Path,
) -> None:
    engine = split_env
    nonce = uuid4().hex[:16]
    app_id = await _insert_app(engine, nonce, limit=8)
    reservation_id, parent_id, payload = await _seed_parent(engine, app_id=app_id, limit=8)
    ready = tmp_path / "split-kill-ready"
    child_env = os.environ.copy()
    child_env.update(
        {
            "SMS_SPLIT_KILL_READY": str(ready),
            "SMS_SPLIT_BATCH_ID": str(payload.batch_id),
            "SMS_SPLIT_PARENT_ID": str(parent_id),
            "SMS_SPLIT_LIMIT": "8",
            "SMS_SPLIT_CHILD_CUSTOM": uuid4().hex,
        }
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        (
            "from tests.integration.test_inflight_split_capacity_postgres "
            "import child_partial_split; child_partial_split()"
        ),
        cwd=str(_BACKEND_ROOT),
        env=child_env,
    )
    try:
        for _ in range(200):
            if ready.exists():
                break
            if proc.returncode is not None:
                _stdout, stderr = await proc.communicate()
                raise AssertionError(
                    f"split child exited early: {proc.returncode} {stderr!r}"
                )
            await asyncio.sleep(0.05)
        assert ready.exists()
        proc.send_signal(signal.SIGKILL)
        await proc.wait()
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    assert await _chunk_status(engine, parent_id) == "submitting"
    assert await _reservation_chunks(engine, reservation_id) == 1
    assert await _balance(engine, app_id) == 1
    async with engine.connect() as connection:
        extras = int(
            (
                await connection.execute(
                    text("SELECT COUNT(*) FROM sms_chunk WHERE parent_chunk_id=:id"),
                    {"id": parent_id},
                )
            ).scalar_one()
        )
    assert extras == 0
