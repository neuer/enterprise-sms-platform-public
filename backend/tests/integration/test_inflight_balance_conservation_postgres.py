from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.runtime_resources import (
    _set_audit_transaction_context,
    bind_connection_system_audit,
)
from app.services.pipeline import InFlightLimitExceeded
from app.services.pipeline_repository import SqlPipelineStore
from app.services.send_inflight import (
    InFlightInvariantViolation,
    apply_inflight_delta,
    reconcile_inflight_balance_conservation,
)
from scripts_support.maintain_partitions import maintain

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


class EngineBoundStore(SqlPipelineStore):
    def __init__(self, engine: Any, settings: Any) -> None:
        super().__init__(settings=settings)
        self._bound_engine = engine
        sync_engine = engine.sync_engine
        if not getattr(sync_engine, "_sms_inflight_audit_begin", False):
            event.listen(sync_engine, "begin", _set_audit_transaction_context)
            sync_engine._sms_inflight_audit_begin = True

    def _engine(self) -> Any:
        return self._bound_engine


async def _prepare_db(engine: Any) -> None:
    async with engine.begin() as connection:
        await bind_connection_system_audit(
            connection,
            actor_name="partition-maintenance",
            action="partition.maintenance",
            producer_domain="api",
        )
        await maintain(connection, future_months=3)


async def _insert_app(engine: Any, nonce: str) -> int:
    async with engine.begin() as connection:
        return int(
            (
                await connection.execute(
                    text(
                        """
                        INSERT INTO app(
                          name,dept,api_key_hash,api_key_prefix,created_by
                        ) VALUES(
                          :name,'平台部',:api_key_hash,:api_key_prefix,'test'
                        ) RETURNING id
                        """
                    ),
                    {
                        "name": f"consv-{nonce}",
                        "api_key_hash": "c" * 64,
                        "api_key_prefix": nonce[:8],
                    },
                )
            ).scalar_one()
        )


async def _reservation(engine: Any, reservation_id: int) -> dict[str, Any]:
    async with engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    text(
                        """
                    SELECT state, generation, reserved_chunks, batch_id
                    FROM send_inflight_reservation
                    WHERE id=:id
                    """
                    ),
                    {"id": reservation_id},
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def _balance(engine: Any, app_id: int) -> int | None:
    async with engine.connect() as connection:
        value = (
            await connection.execute(
                text(
                    """
                    SELECT reserved_chunks
                    FROM send_inflight_balance
                    WHERE app_id=:app_id
                    """
                ),
                {"app_id": app_id},
            )
        ).scalar_one_or_none()
    return None if value is None else int(value)


async def _drop_conservation_triggers(connection: Any) -> None:
    await connection.execute(
        text(
            "DROP TRIGGER IF EXISTS trg_send_inflight_reservation_conservation "
            "ON send_inflight_reservation"
        )
    )
    await connection.execute(
        text(
            "DROP TRIGGER IF EXISTS trg_send_inflight_balance_conservation ON send_inflight_balance"
        )
    )


async def _restore_conservation_triggers(connection: Any) -> None:
    await connection.execute(
        text(
            """
            CREATE CONSTRAINT TRIGGER trg_send_inflight_reservation_conservation
            AFTER INSERT OR UPDATE OR DELETE ON send_inflight_reservation
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW
            EXECUTE FUNCTION check_send_inflight_balance_conservation()
            """
        )
    )
    await connection.execute(
        text(
            """
            CREATE CONSTRAINT TRIGGER trg_send_inflight_balance_conservation
            AFTER INSERT OR UPDATE OR DELETE ON send_inflight_balance
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW
            EXECUTE FUNCTION check_send_inflight_balance_conservation()
            """
        )
    )


@pytest_asyncio.fixture
async def conservation_env() -> Any:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    engine = create_async_engine(database_url, hide_parameters=True)
    await _prepare_db(engine)
    nonce = uuid4().hex[:16]
    app_id = await _insert_app(engine, nonce)
    store = EngineBoundStore(
        engine,
        settings=cast(Any, SimpleNamespace(database_url=database_url)),
    )
    try:
        yield engine, store, app_id
    finally:
        if getattr(engine.sync_engine, "_sms_inflight_audit_begin", False):
            event.remove(engine.sync_engine, "begin", _set_audit_transaction_context)
            engine.sync_engine._sms_inflight_audit_begin = False
        await engine.dispose()


@pytest.mark.asyncio
async def test_reserve_release_keeps_balance_equal_to_active_sum(
    conservation_env: tuple[Any, SqlPipelineStore, int],
) -> None:
    engine, store, app_id = conservation_env
    reserved = await store.reserve_in_flight_chunks(app_id, 3, 200)
    assert await _balance(engine, app_id) == 3
    released = await store.release_in_flight_reservation(
        reserved.id, reserved.generation, "orphan-expired", app_id
    )
    assert released is True
    assert (await _reservation(engine, reserved.id))["state"] == "released"
    assert await _balance(engine, app_id) == 0
    assert (
        await store.release_in_flight_reservation(
            reserved.id, reserved.generation, "orphan-expired", app_id
        )
        is False
    )
    assert await _balance(engine, app_id) == 0


@pytest.mark.asyncio
async def test_release_rolls_back_when_balance_below_amount(
    conservation_env: tuple[Any, SqlPipelineStore, int],
) -> None:
    engine, store, app_id = conservation_env
    reserved = await store.reserve_in_flight_chunks(app_id, 5, 200)
    async with engine.begin() as connection:
        await _drop_conservation_triggers(connection)
        await connection.execute(
            text(
                """
                UPDATE send_inflight_balance
                SET reserved_chunks=3
                WHERE app_id=:app_id
                """
            ),
            {"app_id": app_id},
        )
        await _restore_conservation_triggers(connection)
    with pytest.raises(InFlightInvariantViolation):
        await store.release_in_flight_reservation(
            reserved.id, reserved.generation, "orphan-expired", app_id
        )
    assert (await _reservation(engine, reserved.id))["state"] == "reserved"
    assert await _balance(engine, app_id) == 3


@pytest.mark.asyncio
async def test_materialize_shrink_rolls_back_when_balance_missing(
    conservation_env: tuple[Any, SqlPipelineStore, int],
) -> None:
    engine, store, app_id = conservation_env
    reserved = await store.reserve_in_flight_chunks(app_id, 5, 200)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                UPDATE send_inflight_reservation
                SET state='batch_bound', bound_at=now()
                WHERE id=:id
                """
            ),
            {"id": reserved.id},
        )
        await _drop_conservation_triggers(connection)
        await connection.execute(
            text("DELETE FROM send_inflight_balance WHERE app_id=:app_id"),
            {"app_id": app_id},
        )
        await _restore_conservation_triggers(connection)
    with pytest.raises(InFlightInvariantViolation):
        async with engine.begin() as connection:
            await apply_inflight_delta(
                connection,
                operation="materialize",
                app_id=app_id,
                delta=-3,
                reservation_id=reserved.id,
                generation=reserved.generation,
                expected_states=frozenset({"batch_bound"}),
                next_state="materialized",
                next_reserved_chunks=2,
                materialized_chunks=2,
                limit=200,
            )
    row = await _reservation(engine, reserved.id)
    assert row["state"] == "batch_bound"
    assert int(row["reserved_chunks"]) == 5


@pytest.mark.asyncio
async def test_old_writer_release_without_balance_is_rejected(
    conservation_env: tuple[Any, SqlPipelineStore, int],
) -> None:
    engine, store, app_id = conservation_env
    reserved = await store.reserve_in_flight_chunks(app_id, 2, 200)
    with pytest.raises(IntegrityError, match="send_inflight balance"):
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE send_inflight_reservation
                    SET state='released',
                        released_at=now(),
                        release_reason='orphan-expired',
                        generation=generation + 1
                    WHERE id=:id
                    """
                ),
                {"id": reserved.id},
            )
    assert (await _reservation(engine, reserved.id))["state"] == "reserved"
    assert await _balance(engine, app_id) == 2


@pytest.mark.asyncio
async def test_reconcile_repairs_high_balance_and_clears_block(
    conservation_env: tuple[Any, SqlPipelineStore, int],
) -> None:
    engine, store, app_id = conservation_env
    reserved = await store.reserve_in_flight_chunks(app_id, 2, 200)
    async with engine.begin() as connection:
        await _drop_conservation_triggers(connection)
        await connection.execute(
            text(
                """
                UPDATE send_inflight_balance
                SET reserved_chunks=9,
                    conservation_blocked_at=now()
                WHERE app_id=:app_id
                """
            ),
            {"app_id": app_id},
        )
        await _restore_conservation_triggers(connection)
    with pytest.raises(InFlightInvariantViolation, match="blocked"):
        await store.reserve_in_flight_chunks(app_id, 1, 200)
    async with engine.begin() as connection:
        repaired = await reconcile_inflight_balance_conservation(connection)
    assert repaired >= 1
    assert await _balance(engine, app_id) == 2
    extra = await store.reserve_in_flight_chunks(app_id, 1, 200)
    assert extra.id != reserved.id
    assert await _balance(engine, app_id) == 3


@pytest.mark.asyncio
async def test_split_delta_expands_occupancy_atomically(
    conservation_env: tuple[Any, SqlPipelineStore, int],
) -> None:
    engine, store, app_id = conservation_env
    reserved = await store.reserve_in_flight_chunks(app_id, 1, 200)
    async with engine.begin() as connection:
        changed = await apply_inflight_delta(
            connection,
            operation="split",
            app_id=app_id,
            delta=1,
            reservation_id=reserved.id,
            generation=reserved.generation,
            expected_states=frozenset({"reserved"}),
            next_reserved_chunks=2,
            limit=200,
        )
    assert changed is True
    assert (await _reservation(engine, reserved.id))["reserved_chunks"] == 2
    assert await _balance(engine, app_id) == 2


@pytest.mark.asyncio
async def test_concurrent_last_capacity_has_one_winner(
    conservation_env: tuple[Any, SqlPipelineStore, int],
) -> None:
    engine, store, app_id = conservation_env
    first = await store.reserve_in_flight_chunks(app_id, 1, 1)
    with pytest.raises(InFlightLimitExceeded):
        await store.reserve_in_flight_chunks(app_id, 1, 1)
    assert await _balance(engine, app_id) == 1
    assert (await _reservation(engine, first.id))["state"] == "reserved"
