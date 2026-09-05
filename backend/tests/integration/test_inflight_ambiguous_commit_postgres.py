from __future__ import annotations

import asyncio
import base64
import os
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.apikey import ApiAppContext
from app.core.auth.accounts import ApplicationPrincipal
from app.core.auth.principal_context import audit_principal_scope
from app.core.correlation import correlation_scope
from app.core.runtime_resources import (
    _set_audit_transaction_context,
    bind_connection_system_audit,
)
from app.services.crypto import CryptoService
from app.services.idempotency import IdempotencyCoordinator
from app.services.pipeline import (
    BatchCommand,
    BatchResponse,
    PipelineConfig,
    SendPipeline,
    SendRequest,
    StoredBatch,
)
from app.services.pipeline_repository import SqlPipelineStore
from app.services.send_inflight import (
    reconcile_in_flight_reservations,
    release_unbound_acceptance_reservation,
    resolve_ambiguous_acceptance_commit,
)
from scripts_support.maintain_partitions import maintain

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ or "AUTH_GUARD_REDIS_URL" not in os.environ,
    reason="requires isolated migrated PostgreSQL and Redis 7",
)

_AES = base64.b64encode(b"v" * 32).decode()
_HMAC = base64.b64encode(b"v" * 32).decode()


def _crypto() -> CryptoService:
    return CryptoService.from_secret_values(_AES, _HMAC)


class EngineBoundStore(SqlPipelineStore):
    """测试用本地 engine，避免跨用例复用 database_engine 事件循环。"""

    def __init__(self, engine: Any, settings: Any) -> None:
        super().__init__(settings=settings)
        self._bound_engine = engine
        sync_engine = engine.sync_engine
        if not getattr(sync_engine, "_sms_inflight_audit_begin", False):
            event.listen(sync_engine, "begin", _set_audit_transaction_context)
            sync_engine._sms_inflight_audit_begin = True

    def _engine(self) -> Any:
        return self._bound_engine


class CommitAckLossStore(EngineBoundStore):
    async def sensitive_hits(self, content: str) -> list[str]:
        return []

    async def blacklisted(self, phone_hmacs: set[str]) -> set[str]:
        return set()

    async def save(self, command: Any) -> StoredBatch:
        await super().save(command)
        raise OperationalError("simulated commit ack loss", {}, Exception("ack"))


class FakeFrequency:
    async def allow(self, category: str, **_values: Any) -> bool:
        return True


class FakeQuota:
    async def reserve(self, **_values: Any) -> None:
        return None

    async def refund(self, **_values: Any) -> None:
        return None

    async def refund_reservation(self, **_values: Any) -> None:
        return None


class FakePublisher:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    async def enqueue(self, batch_no: str, queue: str) -> None:
        self.events.append((batch_no, queue))


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
                        "name": f"inflight-{nonce}",
                        "api_key_hash": "b" * 64,
                        "api_key_prefix": nonce[:8],
                    },
                )
            ).scalar_one()
        )


def _command(
    *,
    app_id: int,
    biz_id: str,
    fingerprint: str,
    inflight_id: int,
    inflight_generation: int,
) -> BatchCommand:
    protected = _crypto().protect_phone("13800138000")
    return BatchCommand(
        batch_no=uuid4().hex,
        app_id=app_id,
        dept="平台部",
        category="notice",
        channel="api",
        display_content_enc=b"display",
        send_content_enc=b"send",
        sign_name=None,
        template_id=None,
        biz_id=biz_id,
        segments=1,
        quota_cost=1,
        status="queued",
        deferred_reason=None,
        scheduled_at=None,
        removed_duplicate=0,
        removed_blacklist=0,
        removed_freq=0,
        principal=ApplicationPrincipal(app_id, "inflight-app", "平台部"),
        approval_expire_hours=24,
        approval_threshold=None,
        is_test=False,
        consent_confirmed=False,
        remark=None,
        resend_of=None,
        usage_reservation_id=None,
        import_reservation_id=None,
        messages=(protected,),
        scope_kind="app",
        scope_id=str(app_id),
        request_hash=fingerprint,
        request_hash_key_version=1,
        inflight_reservation_id=inflight_id,
        inflight_reservation_generation=inflight_generation,
    )


async def _save(
    store: SqlPipelineStore,
    command: BatchCommand,
) -> StoredBatch:
    principal = ApplicationPrincipal(command.app_id or 0, "inflight-app", "平台部")
    with audit_principal_scope(principal), correlation_scope(uuid4()):
        return await store.save(command)


async def _reservation(engine: Any, reservation_id: int) -> dict[str, Any]:
    async with engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    text(
                        """
                    SELECT state, generation, batch_id, release_reason, reserved_chunks
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


async def _balance(engine: Any, app_id: int) -> int:
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
        ).scalar_one()
    return int(value)


@pytest_asyncio.fixture
async def inflight_env() -> Any:
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
async def test_successful_commit_keeps_bound_reservation(
    inflight_env: tuple[Any, SqlPipelineStore, int],
) -> None:
    engine, store, app_id = inflight_env
    reserved = await store.reserve_in_flight_chunks(app_id, 1, 200)
    stored = await _save(
        store,
        _command(
            app_id=app_id,
            biz_id=f"ok-{uuid4().hex[:12]}",
            fingerprint="a" * 64,
            inflight_id=reserved.id,
            inflight_generation=reserved.generation,
        ),
    )
    assert stored.idempotent is False
    row = await _reservation(engine, reserved.id)
    assert row["state"] == "batch_bound"
    assert row["batch_id"] is not None
    assert await _balance(engine, app_id) == 1
    released = await store.release_unbound_acceptance_reservation(
        reserved.id,
        reserved.generation,
        app_id,
    )
    assert released is False
    assert (await _reservation(engine, reserved.id))["state"] == "batch_bound"
    assert await _balance(engine, app_id) == 1


@pytest.mark.asyncio
async def test_pre_commit_failure_releases_unbound_reservation(
    inflight_env: tuple[Any, SqlPipelineStore, int],
) -> None:
    engine, store, app_id = inflight_env
    reserved = await store.reserve_in_flight_chunks(app_id, 1, 200)
    assert await _balance(engine, app_id) == 1
    released = await store.release_unbound_acceptance_reservation(
        reserved.id,
        reserved.generation,
        app_id,
    )
    assert released is True
    row = await _reservation(engine, reserved.id)
    assert row["state"] == "released"
    assert row["release_reason"] == "acceptance-failed"
    assert row["batch_id"] is None
    assert await _balance(engine, app_id) == 0


@pytest.mark.asyncio
async def test_commit_ack_loss_resolves_to_expected_batch(
    inflight_env: tuple[Any, SqlPipelineStore, int],
) -> None:
    engine, store, app_id = inflight_env
    reserved = await store.reserve_in_flight_chunks(app_id, 1, 200)
    fingerprint = "b" * 64
    biz_id = f"ack-{uuid4().hex[:12]}"
    command = _command(
        app_id=app_id,
        biz_id=biz_id,
        fingerprint=fingerprint,
        inflight_id=reserved.id,
        inflight_generation=reserved.generation,
    )
    with pytest.raises(OperationalError, match="ack loss"):
        await _save(CommitAckLossStore(engine, store.settings), command)
    async with engine.begin() as connection:
        resolution = await resolve_ambiguous_acceptance_commit(
            connection,
            reservation_id=reserved.id,
            generation=reserved.generation,
            app_id=app_id,
            scope_kind="app",
            scope_id=str(app_id),
            biz_id=biz_id,
            request_hash=fingerprint,
        )
    assert resolution.kind == "BOUND_TO_EXPECTED_BATCH"
    assert resolution.batch_no == command.batch_no
    released = await store.release_unbound_acceptance_reservation(
        reserved.id,
        reserved.generation,
        app_id,
    )
    assert released is False
    assert (await _reservation(engine, reserved.id))["state"] == "batch_bound"
    assert await _balance(engine, app_id) == 1


@pytest.mark.asyncio
async def test_unknown_commit_when_database_is_closed(
    inflight_env: tuple[Any, SqlPipelineStore, int],
) -> None:
    engine, raw_store, app_id = inflight_env
    store = cast(EngineBoundStore, raw_store)
    reserved = await store.reserve_in_flight_chunks(app_id, 1, 200)
    # dispose() 会重建连接池，不能模拟“暂时不可查询”。
    unreachable = create_async_engine(
        "postgresql+asyncpg://sms_accept:invalid@127.0.0.1:1/sms",
        hide_parameters=True,
        connect_args={"timeout": 1},
    )
    store._bound_engine = unreachable
    try:
        resolution = await store.resolve_ambiguous_acceptance_commit(
            reservation_id=reserved.id,
            generation=reserved.generation,
            app_id=app_id,
            scope_kind="app",
            scope_id=str(app_id),
            biz_id="gone",
            request_hash="c" * 64,
        )
    finally:
        await unreachable.dispose()
        store._bound_engine = engine
    assert resolution.kind == "UNKNOWN"


@pytest.mark.asyncio
async def test_conflicting_fingerprint_is_not_released(
    inflight_env: tuple[Any, SqlPipelineStore, int],
) -> None:
    engine, store, app_id = inflight_env
    reserved = await store.reserve_in_flight_chunks(app_id, 1, 200)
    biz_id = f"cf-{uuid4().hex[:12]}"
    await _save(
        store,
        _command(
            app_id=app_id,
            biz_id=biz_id,
            fingerprint="d" * 64,
            inflight_id=reserved.id,
            inflight_generation=reserved.generation,
        ),
    )
    async with engine.begin() as connection:
        resolution = await resolve_ambiguous_acceptance_commit(
            connection,
            reservation_id=reserved.id,
            generation=reserved.generation,
            app_id=app_id,
            scope_kind="app",
            scope_id=str(app_id),
            biz_id=biz_id,
            request_hash="e" * 64,
        )
        released = await release_unbound_acceptance_reservation(
            connection,
            reservation_id=reserved.id,
            generation=reserved.generation,
            app_id=app_id,
        )
    assert resolution.kind == "BOUND_TO_CONFLICTING_BATCH"
    assert released is False
    assert (await _reservation(engine, reserved.id))["state"] == "batch_bound"
    assert await _balance(engine, app_id) == 1


@pytest.mark.asyncio
async def test_old_wide_acceptance_failed_cannot_release_bound(
    inflight_env: tuple[Any, SqlPipelineStore, int],
) -> None:
    engine, store, app_id = inflight_env
    reserved = await store.reserve_in_flight_chunks(app_id, 1, 200)
    await _save(
        store,
        _command(
            app_id=app_id,
            biz_id=f"old-{uuid4().hex[:12]}",
            fingerprint="f" * 64,
            inflight_id=reserved.id,
            inflight_generation=reserved.generation,
        ),
    )
    wide = await store.release_in_flight_reservation(
        reserved.id,
        reserved.generation,
        "acceptance-failed",
        app_id,
    )
    assert wide is False
    async with engine.begin() as connection:
        with pytest.raises(IntegrityError, match="acceptance-failed"):
            await connection.execute(
                text(
                    """
                    UPDATE send_inflight_reservation
                    SET state='released',
                        released_at=now(),
                        release_reason='acceptance-failed',
                        generation=generation + 1
                    WHERE id=:id AND state='batch_bound'
                    """
                ),
                {"id": reserved.id},
            )
    assert (await _reservation(engine, reserved.id))["state"] == "batch_bound"
    assert await _balance(engine, app_id) == 1


@pytest.mark.asyncio
async def test_concurrent_resolve_agrees_on_bound_batch(
    inflight_env: tuple[Any, SqlPipelineStore, int],
) -> None:
    engine, store, app_id = inflight_env
    reserved = await store.reserve_in_flight_chunks(app_id, 1, 200)
    fingerprint = "1" * 64
    biz_id = f"race-{uuid4().hex[:12]}"
    stored = await _save(
        store,
        _command(
            app_id=app_id,
            biz_id=biz_id,
            fingerprint=fingerprint,
            inflight_id=reserved.id,
            inflight_generation=reserved.generation,
        ),
    )

    async def _resolve() -> str:
        async with engine.begin() as connection:
            resolution = await resolve_ambiguous_acceptance_commit(
                connection,
                reservation_id=reserved.id,
                generation=reserved.generation,
                app_id=app_id,
                scope_kind="app",
                scope_id=str(app_id),
                biz_id=biz_id,
                request_hash=fingerprint,
            )
        assert resolution.kind == "BOUND_TO_EXPECTED_BATCH"
        assert resolution.batch_no == stored.batch_no
        return str(resolution.batch_no)

    first, second = await asyncio.gather(_resolve(), _resolve())
    assert first == second == stored.batch_no
    assert await _balance(engine, app_id) == 1


@pytest.mark.asyncio
async def test_reconcile_restores_historical_acceptance_failed(
    inflight_env: tuple[Any, SqlPipelineStore, int],
) -> None:
    engine, store, app_id = inflight_env
    reserved = await store.reserve_in_flight_chunks(app_id, 1, 200)
    await _save(
        store,
        _command(
            app_id=app_id,
            biz_id=f"rep-{uuid4().hex[:12]}",
            fingerprint="2" * 64,
            inflight_id=reserved.id,
            inflight_generation=reserved.generation,
        ),
    )
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "ALTER TABLE send_inflight_reservation "
                "DROP CONSTRAINT ck_send_inflight_acceptance_failed_unbound"
            )
        )
        await connection.execute(
            text(
                "DROP TRIGGER trg_send_inflight_reject_bound_acceptance_failed "
                "ON send_inflight_reservation"
            )
        )
        await connection.execute(
            text(
                """
                UPDATE send_inflight_reservation
                SET state='released',
                    released_at=now(),
                    release_reason='acceptance-failed',
                    generation=generation + 1
                WHERE id=:id
                """
            ),
            {"id": reserved.id},
        )
        await connection.execute(
            text(
                """
                UPDATE send_inflight_balance
                SET reserved_chunks=reserved_chunks - 1
                WHERE app_id=:app_id
                """
            ),
            {"app_id": app_id},
        )
        await connection.execute(
            text(
                """
                ALTER TABLE send_inflight_reservation
                  ADD CONSTRAINT ck_send_inflight_acceptance_failed_unbound
                  CHECK (
                    release_reason IS DISTINCT FROM 'acceptance-failed'
                    OR batch_id IS NULL
                  ) NOT VALID
                """
            )
        )
        await connection.execute(
            text(
                """
                CREATE TRIGGER trg_send_inflight_reject_bound_acceptance_failed
                BEFORE UPDATE ON send_inflight_reservation
                FOR EACH ROW
                EXECUTE FUNCTION reject_bound_acceptance_failed()
                """
            )
        )
    assert (await _reservation(engine, reserved.id))["state"] == "released"
    assert await _balance(engine, app_id) == 0
    async with engine.begin() as connection:
        repaired = await reconcile_in_flight_reservations(connection)
    assert repaired >= 1
    row = await _reservation(engine, reserved.id)
    assert row["state"] == "batch_bound"
    assert row["release_reason"] is None
    assert await _balance(engine, app_id) == 1


@pytest.mark.asyncio
async def test_pipeline_commit_ack_loss_keeps_capacity(
    inflight_env: tuple[Any, SqlPipelineStore, int],
) -> None:
    engine, store, app_id = inflight_env
    from redis.asyncio import Redis

    redis = Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"], decode_responses=True)
    loss_store = CommitAckLossStore(engine, store.settings)
    coordinator = IdempotencyCoordinator(redis, loss_store)
    pipeline = SendPipeline(
        store=loss_store,
        idempotency=coordinator,
        crypto=_crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        config=PipelineConfig(),
    )
    app = ApiAppContext(app_id, "inflight-app", "平台部", frozenset({"notice"}))
    request = SendRequest(
        "notice",
        ["13800138000"],
        content="通知",
        biz_id=f"pl-{uuid4().hex[:12]}",
    )
    principal = ApplicationPrincipal(app_id, "inflight-app", "平台部")
    try:
        with audit_principal_scope(principal), correlation_scope(uuid4()):
            result = await pipeline.accept(app, request)
    finally:
        await redis.aclose()
    assert isinstance(result, BatchResponse)
    assert result.accepted == 1
    async with engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    text(
                        """
                    SELECT r.state, r.batch_id, b.reserved_chunks
                    FROM send_inflight_reservation r
                    JOIN send_inflight_balance b ON b.app_id=r.app_id
                    WHERE r.app_id=:app_id
                    ORDER BY r.id DESC
                    LIMIT 1
                    """
                    ),
                    {"app_id": app_id},
                )
            )
            .mappings()
            .one()
        )
    assert row["state"] == "batch_bound"
    assert row["batch_id"] is not None
    assert int(row["reserved_chunks"]) == 1
