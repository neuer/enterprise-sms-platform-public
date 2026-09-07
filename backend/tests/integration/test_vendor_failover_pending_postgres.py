"""Issue #673：真实 SqlChunkStore / reconcile 上的 failover_pending 合同。"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.services.reconcile_repository import SqlRecoveryRepository
from app.tasks.send import (
    ChunkPayload,
    FinalizeKind,
    SendWorker,
    SubmitOutcome,
    followup_allowed,
)
from app.tasks.send_repository import SqlChunkStore
from app.vendor.failover import InvokeClaimKind, NextAction
from app.vendor.routing import PRIMARY_VENDOR_ID, VendorRouter
from app.vendor.zhihui import VendorApiError
from tests.integration.test_vendor_attempt_finalize_postgres import (
    _cleanup,
    _crypto,
    _insert_app,
    _prepare_db,
    _seed_chunk,
    _settings,
    _snapshot,
    _store,
)
from tests.vendor_failover_r5_support import (
    CountingBucket,
    CountingGateway,
    two_vendor_registry,
)

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


class MemoryRedis:
    def __init__(self) -> None:
        self.data: dict[str, object] = {}

    async def mget(self, keys: list[str]) -> list[object]:
        return [self.data.get(key) for key in keys]

    async def get(self, key: str) -> object:
        return self.data.get(key)

    async def set(self, key: str, value: object, ex: int | None = None) -> None:
        self.data[key] = value

    async def eval(self, script: str, n: int, *args: object) -> int:
        keys = args[:n]
        value = args[n] if len(args) > n else "1"
        for key in keys:
            self.data[str(key)] = value
        return 1


def _worker_store(database_url: Any, registry: Any = None) -> SqlChunkStore:
    return SqlChunkStore(
        _crypto(),
        settings=_settings(database_url),
        redis=MemoryRedis(),
        registry=registry,
    )


def _payload(chunk_id: int, batch_id: int, nonce: str, index: int, **kwargs: Any) -> ChunkPayload:
    return ChunkPayload(
        chunk_id=chunk_id,
        batch_id=batch_id,
        custom_id=f"{nonce[:24]}{index:08d}",
        phones=("13800138000",),
        content="通知内容",
        template_id="",
        sign_name="【青鸾】",
        **kwargs,
    )


async def _outbox_count(engine: Any, chunk_id: int) -> int:
    async with engine.connect() as connection:
        return int(
            (
                await connection.execute(
                    text(
                        """
                        SELECT count(*) FROM outbox_event
                        WHERE aggregate_type='sms_chunk'
                          AND aggregate_id=:id
                          AND event_type='chunk.ready'
                        """
                    ),
                    {"id": str(chunk_id)},
                )
            ).scalar_one()
        )


async def _callback_count(engine: Any, batch_id: int) -> int:
    async with engine.connect() as connection:
        return int(
            (
                await connection.execute(
                    text("SELECT count(*) FROM callback_task WHERE batch_id=:id"),
                    {"id": batch_id},
                )
            ).scalar_one()
        )


async def _cleanup_extra(engine: Any, batch_ids: list[int], chunk_ids: list[int]) -> None:
    async with engine.begin() as connection:
        if chunk_ids:
            await connection.execute(
                text(
                    """
                    DELETE FROM outbox_event
                    WHERE aggregate_type='sms_chunk'
                      AND aggregate_id=ANY(:ids)
                    """
                ),
                {"ids": [str(item) for item in chunk_ids]},
            )
        if batch_ids:
            await connection.execute(
                text("DELETE FROM callback_task WHERE batch_id=ANY(:ids)"),
                {"ids": batch_ids},
            )


@pytest.mark.asyncio
async def test_single_vendor_safe_reject_finishes_chunk_and_batch() -> None:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    nonce = uuid4().hex
    app_id = None
    batch_ids: list[int] = []
    chunk_ids: list[int] = []
    try:
        await _prepare_db(engine)
        app_id = await _insert_app(engine, nonce)
        batch_id, chunk_id = await _seed_chunk(
            engine, app_id=app_id, nonce=nonce, index=1
        )
        batch_ids.append(batch_id)
        chunk_ids.append(chunk_id)
        store = _store(database_url)
        attempt = await store.begin_vendor_invoke(
            chunk_id, vendor_id="zhihui", adapter_id="zhihui", reason="primary"
        )
        report = await store.finalize_vendor_attempt(
            attempt.id,
            chunk_id,
            expected_generation=attempt.generation,
            result="rejected",
            vendor_code=1002,
            safe_to_failover=True,
        )
        assert report.kind is FinalizeKind.APPLIED
        assert report.next_action == NextAction.FAILED.value
        state = await _snapshot(engine, chunk_id)
        assert state["chunk_status"] == "failed"
        assert state["message_status"] == "failed"
        assert state["outcome"] == "rejected"
        async with engine.connect() as connection:
            batch_status = (
                await connection.execute(
                    text("SELECT status FROM sms_batch WHERE id=:id"),
                    {"id": batch_id},
                )
            ).scalar_one()
        assert str(batch_status) == "completed"
        with pytest.raises(RuntimeError, match="terminal|first-invoke"):
            await store.begin_vendor_invoke(
                chunk_id, vendor_id="secondary", adapter_id="secondary", reason="x"
            )
    finally:
        await _cleanup_extra(engine, batch_ids, chunk_ids)
        await _cleanup(engine, app_id=app_id, batch_ids=batch_ids, chunk_ids=chunk_ids)
        await engine.dispose()


@pytest.mark.asyncio
async def test_no_next_vendor_never_leaves_rejected_submitting() -> None:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    nonce = uuid4().hex
    app_id = None
    batch_ids: list[int] = []
    chunk_ids: list[int] = []
    try:
        await _prepare_db(engine)
        app_id = await _insert_app(engine, nonce)
        batch_id, chunk_id = await _seed_chunk(
            engine, app_id=app_id, nonce=nonce, index=1
        )
        batch_ids.append(batch_id)
        chunk_ids.append(chunk_id)
        store = _store(database_url)
        attempt = await store.begin_vendor_invoke(
            chunk_id, vendor_id="zhihui", adapter_id="zhihui", reason="primary"
        )
        await store.finalize_vendor_attempt(
            attempt.id,
            chunk_id,
            expected_generation=attempt.generation,
            result="rejected",
            vendor_code=1001,
            safe_to_failover=True,
        )
        state = await _snapshot(engine, chunk_id)
        assert state["outcome"] == "rejected"
        assert state["chunk_status"] != "submitting"
        assert state["chunk_status"] == "failed"
        leftover_batch, leftover_id = await _seed_chunk(
            engine, app_id=app_id, nonce=nonce, index=2
        )
        batch_ids.append(leftover_batch)
        chunk_ids.append(leftover_id)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO sms_vendor_attempt(
                      chunk_id,vendor_id,generation,outcome,adapter_id,
                      safe_to_failover,vendor_code,invoke_started_at
                    ) VALUES (
                      :chunk_id,'zhihui',1,'rejected','zhihui',
                      true,1002,now()
                    )
                    """
                ),
                {"chunk_id": leftover_id},
            )
            await store.repair_legacy_safe_reject_submitting(connection)
        leftover = await _snapshot(engine, leftover_id)
        assert leftover["chunk_status"] == "failed"
        assert leftover["message_status"] == "failed"
    finally:
        await _cleanup_extra(engine, batch_ids, chunk_ids)
        await _cleanup(engine, app_id=app_id, batch_ids=batch_ids, chunk_ids=chunk_ids)
        await engine.dispose()


@pytest.mark.asyncio
async def test_safe_reject_persists_one_failover_action() -> None:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    nonce = uuid4().hex
    app_id = None
    batch_ids: list[int] = []
    chunk_ids: list[int] = []
    try:
        await _prepare_db(engine)
        app_id = await _insert_app(engine, nonce)
        batch_id, chunk_id = await _seed_chunk(
            engine, app_id=app_id, nonce=nonce, index=1
        )
        batch_ids.append(batch_id)
        chunk_ids.append(chunk_id)
        store = _store(database_url, registry=two_vendor_registry())
        attempt = await store.begin_vendor_invoke(
            chunk_id, vendor_id="zhihui", adapter_id="zhihui", reason="primary"
        )
        first = await store.finalize_vendor_attempt(
            attempt.id,
            chunk_id,
            expected_generation=attempt.generation,
            result="rejected",
            vendor_code=1002,
            safe_to_failover=True,
        )
        second = await store.finalize_vendor_attempt(
            attempt.id,
            chunk_id,
            expected_generation=attempt.generation,
            result="rejected",
            vendor_code=1002,
            safe_to_failover=True,
        )
        assert first.kind is FinalizeKind.APPLIED
        assert first.next_action == NextAction.FAILOVER_PENDING.value
        assert first.next_vendor == "secondary"
        state = await _snapshot(engine, chunk_id)
        assert state["chunk_status"] == "failover_pending"
        assert state["next_vendor"] == "secondary"
        assert await _outbox_count(engine, chunk_id) == 1
        assert second.kind is FinalizeKind.ALREADY_FINALIZED_SAME_RESULT
        assert followup_allowed(second)
        assert await _outbox_count(engine, chunk_id) == 1
        with pytest.raises(RuntimeError, match="first-invoke|terminal"):
            await store.begin_vendor_invoke(
                chunk_id, vendor_id="secondary", adapter_id="secondary", reason="x"
            )
    finally:
        await _cleanup_extra(engine, batch_ids, chunk_ids)
        await _cleanup(engine, app_id=app_id, batch_ids=batch_ids, chunk_ids=chunk_ids)
        await engine.dispose()


@pytest.mark.asyncio
async def test_token_unavailable_keeps_failover_pending() -> None:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    nonce = uuid4().hex
    app_id = None
    batch_ids: list[int] = []
    chunk_ids: list[int] = []
    try:
        await _prepare_db(engine)
        app_id = await _insert_app(engine, nonce)
        batch_id, chunk_id = await _seed_chunk(
            engine, app_id=app_id, nonce=nonce, index=1, status="pending"
        )
        batch_ids.append(batch_id)
        chunk_ids.append(chunk_id)
        store = _worker_store(database_url, two_vendor_registry())
        primary = CountingGateway([VendorApiError(1003, "template")])
        secondary = CountingGateway(["must-not-send"])
        bucket = CountingBucket(deny_vendor="secondary")

        async def sleeper(_: float) -> None:
            await store.redis.set("queue:paused:realtime", "1")
            await store.redis.set("queue:paused:bulk", "1")

        outcome = await SendWorker(
            primary,
            store,
            bucket,
            gateways={PRIMARY_VENDOR_ID: primary, "secondary": secondary},
            router=VendorRouter((PRIMARY_VENDOR_ID, "secondary")),
            registry=two_vendor_registry(),
            sleeper=sleeper,
        ).submit(_payload(chunk_id, batch_id, nonce, 1, status="pending"), lane="realtime")
        assert outcome is SubmitOutcome.PAUSED
        state = await _snapshot(engine, chunk_id)
        assert state["chunk_status"] == "failover_pending"
        assert state["next_vendor"] == "secondary"
        assert secondary.calls == 0
    finally:
        await _cleanup_extra(engine, batch_ids, chunk_ids)
        await _cleanup(engine, app_id=app_id, batch_ids=batch_ids, chunk_ids=chunk_ids)
        await engine.dispose()


@pytest.mark.asyncio
async def test_stale_worker_cannot_invoke_after_chunk_uncertain() -> None:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    nonce = uuid4().hex
    app_id = None
    batch_ids: list[int] = []
    chunk_ids: list[int] = []
    try:
        await _prepare_db(engine)
        app_id = await _insert_app(engine, nonce)
        batch_id, chunk_id = await _seed_chunk(
            engine, app_id=app_id, nonce=nonce, index=1, status="pending"
        )
        batch_ids.append(batch_id)
        chunk_ids.append(chunk_id)
        store = _worker_store(database_url, two_vendor_registry())
        gate = asyncio.Event()
        real_finalize = store.finalize_vendor_attempt

        async def gated_finalize(*args: Any, **kwargs: Any) -> Any:
            await gate.wait()
            return await real_finalize(*args, **kwargs)

        store.finalize_vendor_attempt = gated_finalize  # type: ignore[method-assign]
        primary = CountingGateway([VendorApiError(1002, "bad content")])
        secondary = CountingGateway(["must-not-send"])
        worker = SendWorker(
            primary,
            store,
            CountingBucket(),
            gateways={PRIMARY_VENDOR_ID: primary, "secondary": secondary},
            router=VendorRouter((PRIMARY_VENDOR_ID, "secondary")),
            registry=two_vendor_registry(),
        )
        task = asyncio.create_task(
            worker.submit(_payload(chunk_id, batch_id, nonce, 1, status="pending"), lane="realtime")
        )
        for _ in range(200):
            state = await _snapshot(engine, chunk_id)
            if state.get("outcome") == "invoking":
                break
            await asyncio.sleep(0.05)
        else:
            gate.set()
            await task
            raise AssertionError("worker did not persist invoking before barrier")
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE sms_chunk
                    SET submitting_since=now()-interval '10 minutes'
                    WHERE id=:id
                    """
                ),
                {"id": chunk_id},
            )
        await SqlRecoveryRepository(_settings(database_url)).stalled()
        state = await _snapshot(engine, chunk_id)
        assert state["chunk_status"] == "uncertain"
        gate.set()
        outcome = await task
        assert outcome is SubmitOutcome.UNCERTAIN
        assert secondary.calls == 0
    finally:
        if not gate.is_set():
            gate.set()
        await _cleanup_extra(engine, batch_ids, chunk_ids)
        await _cleanup(engine, app_id=app_id, batch_ids=batch_ids, chunk_ids=chunk_ids)
        await engine.dispose()


@pytest.mark.asyncio
async def test_finalize_conflict_blocks_all_followup_vendor_calls() -> None:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    nonce = uuid4().hex
    app_id = None
    batch_ids: list[int] = []
    chunk_ids: list[int] = []
    try:
        await _prepare_db(engine)
        app_id = await _insert_app(engine, nonce)
        batch_id, chunk_id = await _seed_chunk(
            engine, app_id=app_id, nonce=nonce, index=1
        )
        batch_ids.append(batch_id)
        chunk_ids.append(chunk_id)
        store = _store(database_url, registry=two_vendor_registry())
        attempt = await store.begin_vendor_invoke(
            chunk_id, vendor_id="zhihui", adapter_id="zhihui", reason="primary"
        )
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE sms_chunk SET status='uncertain' WHERE id=:id"),
                {"id": chunk_id},
            )
            await connection.execute(
                text(
                    """
                    UPDATE sms_vendor_attempt SET outcome='uncertain'
                    WHERE id=:id
                    """
                ),
                {"id": attempt.id},
            )
        report = await store.finalize_vendor_attempt(
            attempt.id,
            chunk_id,
            expected_generation=attempt.generation,
            result="rejected",
            vendor_code=1002,
            safe_to_failover=True,
        )
        assert report.kind is FinalizeKind.RECOVERY_MARKED_UNCERTAIN
        assert not followup_allowed(report)
        claim = await store.claim_next_vendor_invoke(
            chunk_id,
            expected_route_generation=attempt.generation,
            previous_attempt_id=attempt.id,
            expected_next_vendor="secondary",
            expected_route_policy_version=1,
        )
        assert claim.kind is InvokeClaimKind.DENIED
        with pytest.raises(RuntimeError, match="terminal|first-invoke"):
            await store.begin_vendor_invoke(
                chunk_id, vendor_id="secondary", adapter_id="secondary", reason="x"
            )
    finally:
        await _cleanup_extra(engine, batch_ids, chunk_ids)
        await _cleanup(engine, app_id=app_id, batch_ids=batch_ids, chunk_ids=chunk_ids)
        await engine.dispose()


@pytest.mark.asyncio
async def test_duplicate_failover_delivery_creates_one_next_attempt() -> None:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    nonce = uuid4().hex
    app_id = None
    batch_ids: list[int] = []
    chunk_ids: list[int] = []
    try:
        await _prepare_db(engine)
        app_id = await _insert_app(engine, nonce)
        batch_id, chunk_id = await _seed_chunk(
            engine, app_id=app_id, nonce=nonce, index=1
        )
        batch_ids.append(batch_id)
        chunk_ids.append(chunk_id)
        store = _store(database_url, registry=two_vendor_registry())
        attempt = await store.begin_vendor_invoke(
            chunk_id, vendor_id="zhihui", adapter_id="zhihui", reason="primary"
        )
        report = await store.finalize_vendor_attempt(
            attempt.id,
            chunk_id,
            expected_generation=attempt.generation,
            result="rejected",
            vendor_code=1002,
            safe_to_failover=True,
        )
        first, second = await asyncio.gather(
            store.claim_next_vendor_invoke(
                chunk_id,
                expected_route_generation=int(report.route_generation or 1),
                previous_attempt_id=int(report.previous_attempt_id or attempt.id),
                expected_next_vendor="secondary",
                expected_route_policy_version=1,
            ),
            store.claim_next_vendor_invoke(
                chunk_id,
                expected_route_generation=int(report.route_generation or 1),
                previous_attempt_id=int(report.previous_attempt_id or attempt.id),
                expected_next_vendor="secondary",
                expected_route_policy_version=1,
            ),
        )
        kinds = {first.kind, second.kind}
        assert InvokeClaimKind.AUTHORIZED in kinds
        assert kinds - {InvokeClaimKind.AUTHORIZED} <= {
            InvokeClaimKind.ALREADY_HANDLED,
            InvokeClaimKind.DENIED,
        }
        async with engine.connect() as connection:
            invoking = int(
                (
                    await connection.execute(
                        text(
                            """
                            SELECT count(*) FROM sms_vendor_attempt
                            WHERE chunk_id=:id AND outcome='invoking'
                              AND vendor_id='secondary'
                            """
                        ),
                        {"id": chunk_id},
                    )
                ).scalar_one()
            )
        assert invoking == 1
    finally:
        await _cleanup_extra(engine, batch_ids, chunk_ids)
        await _cleanup(engine, app_id=app_id, batch_ids=batch_ids, chunk_ids=chunk_ids)
        await engine.dispose()


@pytest.mark.asyncio
async def test_commit_result_unknown_reloads_authoritative_next_action() -> None:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    nonce = uuid4().hex
    app_id = None
    batch_ids: list[int] = []
    chunk_ids: list[int] = []
    try:
        await _prepare_db(engine)
        app_id = await _insert_app(engine, nonce)
        batch_id, chunk_id = await _seed_chunk(
            engine, app_id=app_id, nonce=nonce, index=1, status="pending"
        )
        batch_ids.append(batch_id)
        chunk_ids.append(chunk_id)
        store = _worker_store(database_url, two_vendor_registry())
        real = store.finalize_vendor_attempt
        raised = False

        async def boom(*args: Any, **kwargs: Any) -> Any:
            nonlocal raised
            report = await real(*args, **kwargs)
            if not raised:
                raised = True
                raise RuntimeError("commit result unknown")
            return report

        store.finalize_vendor_attempt = boom  # type: ignore[method-assign]
        primary = CountingGateway([VendorApiError(1002, "bad content")])
        secondary = CountingGateway(["task-b"])
        outcome = await SendWorker(
            primary,
            store,
            CountingBucket(),
            gateways={PRIMARY_VENDOR_ID: primary, "secondary": secondary},
            router=VendorRouter((PRIMARY_VENDOR_ID, "secondary")),
            registry=two_vendor_registry(),
        ).submit(_payload(chunk_id, batch_id, nonce, 1, status="pending"), lane="realtime")
        assert outcome is SubmitOutcome.SUBMITTED
        assert primary.calls == 1
        assert secondary.calls == 1
        state = await _snapshot(engine, chunk_id)
        assert state["chunk_status"] == "submitted"
    finally:
        await _cleanup_extra(engine, batch_ids, chunk_ids)
        await _cleanup(engine, app_id=app_id, batch_ids=batch_ids, chunk_ids=chunk_ids)
        await engine.dispose()


@pytest.mark.asyncio
async def test_failover_pending_is_excluded_from_submitting_timeout() -> None:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    nonce = uuid4().hex
    app_id = None
    batch_ids: list[int] = []
    chunk_ids: list[int] = []
    try:
        await _prepare_db(engine)
        app_id = await _insert_app(engine, nonce)
        batch_id, chunk_id = await _seed_chunk(
            engine, app_id=app_id, nonce=nonce, index=1
        )
        batch_ids.append(batch_id)
        chunk_ids.append(chunk_id)
        store = _store(database_url, registry=two_vendor_registry())
        attempt = await store.begin_vendor_invoke(
            chunk_id, vendor_id="zhihui", adapter_id="zhihui", reason="primary"
        )
        await store.finalize_vendor_attempt(
            attempt.id,
            chunk_id,
            expected_generation=attempt.generation,
            result="rejected",
            vendor_code=1002,
            safe_to_failover=True,
        )
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE sms_chunk
                    SET submitting_since=now()-interval '30 minutes'
                    WHERE id=:id
                    """
                ),
                {"id": chunk_id},
            )
            await connection.execute(
                text(
                    """
                    UPDATE sms_batch
                    SET updated_at=now()-interval '10 minutes'
                    WHERE id=:id
                    """
                ),
                {"id": batch_id},
            )
        work = await SqlRecoveryRepository(_settings(database_url)).stalled()
        state = await _snapshot(engine, chunk_id)
        assert state["chunk_status"] == "failover_pending"
        assert state["outcome"] == "rejected"
        assert any(item.chunk_id == chunk_id for item in work)
        async with engine.connect() as connection:
            secondary_invoking = int(
                (
                    await connection.execute(
                        text(
                            """
                            SELECT count(*) FROM sms_vendor_attempt
                            WHERE chunk_id=:id AND vendor_id='secondary'
                            """
                        ),
                        {"id": chunk_id},
                    )
                ).scalar_one()
            )
        assert secondary_invoking == 0
    finally:
        await _cleanup_extra(engine, batch_ids, chunk_ids)
        await _cleanup(engine, app_id=app_id, batch_ids=batch_ids, chunk_ids=chunk_ids)
        await engine.dispose()


@pytest.mark.asyncio
async def test_next_vendor_requires_category_and_adapter_match() -> None:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    nonce = uuid4().hex
    app_id = None
    batch_ids: list[int] = []
    chunk_ids: list[int] = []
    try:
        await _prepare_db(engine)
        app_id = await _insert_app(engine, nonce)
        batch_id, chunk_id = await _seed_chunk(
            engine, app_id=app_id, nonce=nonce, index=1, category="market"
        )
        batch_ids.append(batch_id)
        chunk_ids.append(chunk_id)
        store = _store(
            database_url,
            registry=two_vendor_registry(secondary_categories=frozenset({"notice"})),
        )
        attempt = await store.begin_vendor_invoke(
            chunk_id, vendor_id="zhihui", adapter_id="zhihui", reason="primary"
        )
        report = await store.finalize_vendor_attempt(
            attempt.id,
            chunk_id,
            expected_generation=attempt.generation,
            result="rejected",
            vendor_code=1002,
            safe_to_failover=True,
        )
        assert report.next_action == NextAction.FAILED.value
        state = await _snapshot(engine, chunk_id)
        assert state["chunk_status"] == "failed"
    finally:
        await _cleanup_extra(engine, batch_ids, chunk_ids)
        await _cleanup(engine, app_id=app_id, batch_ids=batch_ids, chunk_ids=chunk_ids)
        await engine.dispose()


@pytest.mark.asyncio
async def test_hold_like_codes_preserve_explicit_pause_policy() -> None:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    nonce = uuid4().hex
    app_id = None
    batch_ids: list[int] = []
    chunk_ids: list[int] = []
    try:
        await _prepare_db(engine)
        app_id = await _insert_app(engine, nonce)
        batch_id, chunk_id = await _seed_chunk(
            engine, app_id=app_id, nonce=nonce, index=1, status="pending"
        )
        batch_ids.append(batch_id)
        chunk_ids.append(chunk_id)
        store = _worker_store(database_url, two_vendor_registry())
        primary = CountingGateway([VendorApiError(999, "no balance")])
        secondary = CountingGateway(["must-not-send"])
        outcome = await SendWorker(
            primary,
            store,
            CountingBucket(),
            gateways={PRIMARY_VENDOR_ID: primary, "secondary": secondary},
            router=VendorRouter((PRIMARY_VENDOR_ID, "secondary")),
            registry=two_vendor_registry(),
        ).submit(_payload(chunk_id, batch_id, nonce, 1, status="pending"), lane="realtime")
        assert outcome is SubmitOutcome.PAUSED
        state = await _snapshot(engine, chunk_id)
        assert state["chunk_status"] == "retrying"
        assert state["chunk_status"] != "failover_pending"
        async with engine.connect() as connection:
            batch_status = (
                await connection.execute(
                    text("SELECT status FROM sms_batch WHERE id=:id"),
                    {"id": batch_id},
                )
            ).scalar_one()
        assert str(batch_status) == "balance_blocked"
        assert secondary.calls == 0
    finally:
        await _cleanup_extra(engine, batch_ids, chunk_ids)
        await _cleanup(engine, app_id=app_id, batch_ids=batch_ids, chunk_ids=chunk_ids)
        await engine.dispose()


@pytest.mark.asyncio
async def test_failure_completion_keeps_usage_callback_inflight_idempotent() -> None:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    nonce = uuid4().hex
    app_id = None
    batch_ids: list[int] = []
    chunk_ids: list[int] = []
    try:
        await _prepare_db(engine)
        app_id = await _insert_app(engine, nonce)
        batch_id, chunk_id = await _seed_chunk(
            engine, app_id=app_id, nonce=nonce, index=1
        )
        batch_ids.append(batch_id)
        chunk_ids.append(chunk_id)
        store = _store(database_url)
        attempt = await store.begin_vendor_invoke(
            chunk_id, vendor_id="zhihui", adapter_id="zhihui", reason="primary"
        )
        first = await store.finalize_vendor_attempt(
            attempt.id,
            chunk_id,
            expected_generation=attempt.generation,
            result="rejected",
            vendor_code=1002,
            safe_to_failover=True,
        )
        first_callback = await _callback_count(engine, batch_id)
        first_outbox = await _outbox_count(engine, chunk_id)
        second = await store.finalize_vendor_attempt(
            attempt.id,
            chunk_id,
            expected_generation=attempt.generation,
            result="rejected",
            vendor_code=1002,
            safe_to_failover=True,
        )
        assert first.kind is FinalizeKind.APPLIED
        assert first.next_action == NextAction.FAILED.value
        assert second.kind is FinalizeKind.ALREADY_FINALIZED_SAME_RESULT
        state = await _snapshot(engine, chunk_id)
        assert state["chunk_status"] == "failed"
        assert state["message_status"] == "failed"
        assert await _callback_count(engine, batch_id) == first_callback
        assert await _outbox_count(engine, chunk_id) == first_outbox
        async with engine.connect() as connection:
            inflight = int(
                (
                    await connection.execute(
                        text(
                            """
                            SELECT count(*) FROM send_inflight_reservation
                            WHERE batch_id=:id AND state <> 'released'
                            """
                        ),
                        {"id": batch_id},
                    )
                ).scalar_one()
            )
        assert inflight == 0
    finally:
        await _cleanup_extra(engine, batch_ids, chunk_ids)
        await _cleanup(engine, app_id=app_id, batch_ids=batch_ids, chunk_ids=chunk_ids)
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary", ["allowed", "daily_limit", "recipient_disabled", "control_after_claim"]
)
async def test_next_attempt_has_atomic_budget_and_fresh_recipient_check(
    boundary: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    from app.services.crypto import EncryptionContext
    from app.services.vendor_test_budget import SubmissionClaimStatus

    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    engine = create_async_engine(database_url, hide_parameters=True)
    nonce = uuid4().hex
    app_id = batch_id = chunk_id = recipient_id = None
    now = datetime(
        2040,
        1,
        {"allowed": 1, "daily_limit": 2, "recipient_disabled": 3, "control_after_claim": 4}[
            boundary
        ],
        tzinfo=UTC,
    )
    monkeypatch.setattr("app.tasks.send_repository.current_live_test_time", lambda: now)
    try:
        await _prepare_db(engine)
        app_id = await _insert_app(engine, nonce)
        batch_id, chunk_id = await _seed_chunk(
            engine, app_id=app_id, nonce=nonce, index=1, status="pending"
        )
        crypto = _crypto()
        phone = crypto.protect_phone(f"138{int(nonce[:8], 16) % 10**8:08d}")
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE sms_batch SET send_content_enc=:content WHERE id=:id"),
                {
                    "id": batch_id,
                    "content": crypto.encrypt_bound_packed_text(
                        "通知",
                        EncryptionContext(
                            domain="sms-content",
                            table="sms_batch",
                            column="send_content_enc",
                            object_id=f"{nonce[:24]}{1:08d}",
                        ),
                    ),
                },
            )
            await connection.execute(
                text(
                    "UPDATE sms_message SET "
                    "phone_enc=:enc,phone_hmac=:hmac,phone_mask=:mask,key_version=:version WHERE "
                    "chunk_id=:id"
                ),
                {
                    "id": chunk_id,
                    "enc": phone.phone_enc,
                    "hmac": phone.phone_hmac,
                    "mask": phone.phone_mask,
                    "version": phone.key_version,
                },
            )
            recipient_id = await connection.scalar(
                text(
                    "INSERT INTO vendor_test_recipient"
                    "(label,phone_enc,phone_hmac,phone_mask,key_version,created_by) "
                    "VALUES('synthetic',:enc,:hmac,:mask,:version,'test') RETURNING id"
                ),
                {
                    "enc": phone.phone_enc,
                    "hmac": phone.phone_hmac,
                    "mask": phone.phone_mask,
                    "version": phone.key_version,
                },
            )
        store = _worker_store(database_url, two_vendor_registry())
        store.settings.vendor_live_test = True
        first = await store.claim_submission(chunk_id, 0, 1, enforce_live_test_budget=True)
        assert first.status is SubmissionClaimStatus.CLAIMED
        attempt = await store.begin_vendor_invoke(
            chunk_id, vendor_id="zhihui", adapter_id="zhihui", reason="primary"
        )
        report = await store.finalize_vendor_attempt(
            attempt.id,
            chunk_id,
            expected_generation=attempt.generation,
            result="rejected",
            vendor_code=1002,
            safe_to_failover=True,
        )
        async with engine.begin() as connection:
            assert (
                await connection.scalar(
                    text("SELECT status FROM vendor_test_send_attempt WHERE chunk_id=:id"),
                    {"id": chunk_id},
                )
                == "released"
            )
            if boundary == "daily_limit":
                await connection.execute(
                    text(
                        "UPDATE vendor_test_daily_usage SET confirmed_segments=100 WHERE "
                        "usage_date=:day"
                    ),
                    {"day": now.date()},
                )
            if boundary == "recipient_disabled":
                await connection.execute(
                    text(
                        "UPDATE vendor_test_recipient SET "
                        "status='disabled',disabled_at=now(),disabled_by='test' WHERE id=:id"
                    ),
                    {"id": recipient_id},
                )
        kwargs = dict(
            expected_route_generation=report.route_generation,
            previous_attempt_id=attempt.id,
            expected_next_vendor="secondary",
            expected_route_policy_version=report.route_policy_version,
            segments=1,
            enforce_live_test_budget=True,
        )
        claimed = await store.claim_next_vendor_invoke(chunk_id, **kwargs)
        if boundary in {"allowed", "control_after_claim"}:
            assert claimed.kind is InvokeClaimKind.AUTHORIZED
            assert (
                await store.claim_next_vendor_invoke(chunk_id, **kwargs)
            ).kind is InvokeClaimKind.ALREADY_HANDLED
            auth = claimed.authorization
            assert auth is not None
            if boundary == "allowed":
                await store.finalize_vendor_attempt(
                    auth.attempt_id,
                    chunk_id,
                    expected_generation=auth.generation,
                    result="submitted",
                    vendor_task_id="synthetic-b",
                )
            else:
                from app.services.vendor_control_state import VendorControlStateUnavailable

                class StaleControl:
                    def require_fresh(self) -> None:
                        raise VendorControlStateUnavailable(
                            "synthetic stale", requires_critical_pause=True
                        )

                gateway = CountingGateway(["must-not-send"])
                bucket = CountingBucket()
                worker = SendWorker(
                    gateway,
                    store,
                    bucket,
                    gateways={"zhihui": gateway, "secondary": gateway},
                    registry=two_vendor_registry(),
                    router=VendorRouter(("zhihui", "secondary")),
                    control_guard=StaleControl(),
                    enforce_live_test_recipients=True,
                    enforce_live_test_budget=True,
                )
                payload = await store.refresh_invoke_payload(chunk_id)
                outcome = await worker._invoke_authorized(
                    payload,
                    lane="realtime",
                    vendor_id="secondary",
                    generation=auth.generation,
                    attempt_id=auth.attempt_id,
                    allow_split=True,
                    retry_index=0,
                    lease_epoch=1,
                )
                assert outcome is SubmitOutcome.PAUSED and gateway.calls == 0
                assert bucket.refunds == 1
                snapshot = await _snapshot(engine, chunk_id)
                assert snapshot["chunk_status"] == "retrying"
                assert snapshot["outcome"] == "paused"
        else:
            assert claimed.kind is InvokeClaimKind.DENIED
            assert claimed.reason == (
                "daily_limit" if boundary == "daily_limit" else "recipient_denied"
            )
            assert (await _snapshot(engine, chunk_id))["chunk_status"] == "failover_pending"
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT attempt_no,status FROM vendor_test_send_attempt WHERE "
                        "chunk_id=:id ORDER BY attempt_no"
                    ),
                    {"id": chunk_id},
                )
            ).all()
            expected = [(1, "released")]
            if boundary in {"allowed", "control_after_claim"}:
                expected.append((2, "confirmed" if boundary == "allowed" else "released"))
            assert rows == expected
            assert (
                await connection.scalar(
                    text(
                        "SELECT in_flight_segments FROM vendor_test_daily_usage WHERE "
                        "usage_date=:day"
                    ),
                    {"day": now.date()},
                )
                == 0
            )
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM vendor_test_send_attempt WHERE chunk_id=:id"), {"id": chunk_id}
            )
            await connection.execute(
                text("DELETE FROM vendor_test_daily_usage WHERE usage_date=:day"),
                {"day": now.date()},
            )
            await connection.execute(
                text("DELETE FROM vendor_test_recipient WHERE id=:id"), {"id": recipient_id}
            )
        await _cleanup_extra(engine, [batch_id] if batch_id else [], [chunk_id] if chunk_id else [])
        await _cleanup(
            engine,
            app_id=app_id,
            batch_ids=[batch_id] if batch_id else [],
            chunk_ids=[chunk_id] if chunk_id else [],
        )
        await engine.dispose()
