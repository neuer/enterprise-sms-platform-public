"""真实仓储、连接与行锁的可控并发回归；不调用厂商。"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.tasks.send import FinalizeKind
from tests.integration.test_vendor_attempt_finalize_postgres import (
    _cleanup,
    _insert_app,
    _prepare_db,
    _seed_chunk,
    _store,
)

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ, reason="requires isolated migrated PostgreSQL"
)


@pytest.mark.asyncio
async def test_begin_finalize_serializes_actual_locks_for_100_rounds() -> None:
    """先锁定最终化 owner，再证实另一个真实仓储正在等待该行锁，最后放行。"""
    engine = create_async_engine(make_url(os.environ["OUTBOX_POSTGRES_DSN"]))
    url = engine.url
    app_id = None
    batches: list[int] = []
    chunks: list[int] = []
    waits = 0
    try:
        await _prepare_db(engine)
        nonce = uuid4().hex
        app_id = await _insert_app(engine, nonce)
        for index in range(100):
            batch_id, chunk_id = await _seed_chunk(
                engine, app_id=app_id, nonce=nonce, index=index + 1
            )
            batches.append(batch_id)
            chunks.append(chunk_id)
            seed = _store(url)
            attempt = await seed.begin_vendor_invoke(
                chunk_id, vendor_id="zhihui", adapter_id="zhihui", reason="primary"
            )
            await _race_once(engine, url, chunk_id, attempt, index)
            waits += 1
        assert waits == 100
    finally:
        await _cleanup(engine, app_id=app_id, batch_ids=batches, chunk_ids=chunks)
        await engine.dispose()


async def _race_once(engine: Any, url: Any, chunk_id: int, attempt: Any, index: int) -> None:
    held, release, attempted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    waiter_pid: list[int] = []

    class Connection:
        def __init__(self, connection: Any, phase: str) -> None:
            self.connection, self.phase = connection, phase

        async def execute(self, statement: Any, params: Any = None) -> Any:
            sql = " ".join(str(statement).split())
            chunk_lock = "FROM sms_chunk c" in sql and "FOR UPDATE" in sql
            if self.phase == "begin" and chunk_lock:
                attempted.set()
            result = await self.connection.execute(statement, params)
            if self.phase == "finalize" and chunk_lock:
                held.set()
                await asyncio.wait_for(release.wait(), 5)
            return result

    class Engine:
        def __init__(self, phase: str) -> None:
            self.phase = phase

        @asynccontextmanager
        async def begin(self):
            async with engine.begin() as connection:
                await connection.execute(text("SET LOCAL lock_timeout='4s'"))
                if self.phase == "begin":
                    waiter_pid.append(int(await connection.scalar(text("SELECT pg_backend_pid()"))))
                yield Connection(connection, self.phase)

        async def dispose(self) -> None:
            pass

    finalizer, beginner = _store(url), _store(url)
    finalizer._engine = lambda: Engine("finalize")
    beginner._engine = lambda: Engine("begin")
    result = ("submitted", "failed", "rejected")[index % 3]
    finish = asyncio.create_task(
        finalizer.finalize_vendor_attempt(
            attempt.id,
            chunk_id,
            expected_generation=attempt.generation,
            result=result,
            vendor_task_id="synthetic-task" if result == "submitted" else None,
            vendor_code=1002 if result == "rejected" else None,
            safe_to_failover=result == "rejected",
        )
    )
    start: asyncio.Task[Any] | None = None
    try:
        await asyncio.wait_for(held.wait(), 5)
        start = asyncio.create_task(
            beginner.begin_vendor_invoke(
                chunk_id, vendor_id="zhihui", adapter_id="zhihui", reason="duplicate"
            )
        )
        await asyncio.wait_for(attempted.wait(), 5)
        async with asyncio.timeout(3):
            async with engine.connect() as observer:
                # PostgreSQL 行锁状态没有 Event；外层绝对超时限定轮询。
                while not await observer.scalar(  # noqa: ASYNC110
                    text("SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid=:pid AND NOT granted)"),
                    {"pid": waiter_pid[0]},
                ):
                    await asyncio.sleep(0.005)
        release.set()
        report, duplicate = await asyncio.wait_for(
            asyncio.gather(finish, start, return_exceptions=True), 5
        )
        assert not isinstance(report, BaseException), type(report).__name__
        assert report.kind is FinalizeKind.APPLIED
        assert isinstance(duplicate, RuntimeError)
        assert "terminal" in str(duplicate) or "first-invoke" in str(duplicate)
        async with engine.connect() as observer:
            assert (
                await observer.scalar(
                    text("SELECT count(*) FROM sms_vendor_attempt WHERE chunk_id=:id"),
                    {"id": chunk_id},
                )
                == 1
            )
    finally:
        release.set()
        for task in (finish, start):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (finish, start) if task is not None), return_exceptions=True
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("failures", [1, 3])
async def test_real_rolled_back_deadlock_retries_database_without_second_send(
    failures: int,
) -> None:
    """在 attempt 更新之后由真实 PostgreSQL 中止事务，验证回滚与有限落库重试。"""
    from app.tasks.send import SendWorker, SubmitOutcome
    from tests.integration.test_vendor_attempt_finalize_postgres import _snapshot
    from tests.integration.test_vendor_failover_pending_postgres import _payload, _worker_store
    from tests.vendor_failover_r5_support import CountingBucket, CountingGateway

    engine = create_async_engine(make_url(os.environ["OUTBOX_POSTGRES_DSN"]))
    app_id = None
    batches: list[int] = []
    chunks: list[int] = []
    injected = 0
    finalizations = 0

    class Connection:
        def __init__(self, connection: Any) -> None:
            self.connection = connection

        async def execute(self, statement: Any, params: Any = None) -> Any:
            nonlocal injected, finalizations
            result = await self.connection.execute(statement, params)
            sql = " ".join(str(statement).split())
            if "UPDATE sms_vendor_attempt SET outcome=:outcome" in sql:
                finalizations += 1
                if injected < failures:
                    injected += 1
                    await self.connection.execute(
                        text(
                            "DO $$ BEGIN RAISE EXCEPTION 'synthetic' USING ERRCODE='40P01'; END $$"
                        )
                    )
            return result

        def __getattr__(self, name: str) -> Any:
            return getattr(self.connection, name)

    class Engine:
        @asynccontextmanager
        async def begin(self):
            async with engine.begin() as connection:
                yield Connection(connection)

        @asynccontextmanager
        async def connect(self):
            async with engine.connect() as connection:
                yield Connection(connection)

        async def dispose(self) -> None:
            pass

    try:
        await _prepare_db(engine)
        nonce = uuid4().hex
        app_id = await _insert_app(engine, nonce)
        batch_id, chunk_id = await _seed_chunk(
            engine, app_id=app_id, nonce=nonce, index=1, status="pending"
        )
        batches.append(batch_id)
        chunks.append(chunk_id)
        store = _worker_store(engine.url)
        store.settings.alert_smtp_allowed_host_set = frozenset()
        store._engine = Engine
        async with engine.connect() as connection:
            before = int(
                await connection.scalar(
                    text(
                        "SELECT count(reason) FROM send_db_failure_event WHERE phase='finalize' "
                        "AND reason='db_deadlock'"
                    )
                )
            )
        gateway = CountingGateway(["synthetic-task"])
        outcome = await SendWorker(gateway, store, CountingBucket()).submit(
            _payload(chunk_id, batch_id, nonce, 1, status="pending"), lane="realtime"
        )
        assert gateway.calls == 1
        async with engine.begin() as connection:
            await connection.execute(text("SET LOCAL ROLE sms_metrics"))
            after = int(
                await connection.scalar(
                    text(
                        "SELECT count(reason) FROM send_db_failure_event WHERE phase='finalize' "
                        "AND reason='db_deadlock'"
                    )
                )
            )
        assert after - before == failures
        if failures == 3:
            async with engine.connect() as connection:
                assert (
                    await connection.scalar(
                        text(
                            "SELECT count(*) FROM alert_log WHERE alert_type='send_db_failure' "
                            "AND detail->>'reason'='db_deadlock' AND detail->>'phase'='finalize'"
                        )
                    )
                    >= 1
                )
        assert injected == failures
        assert finalizations == min(failures + 1, 3)
        state = await _snapshot(engine, chunk_id)
        if failures == 1:
            assert outcome is SubmitOutcome.SUBMITTED
            assert state["chunk_status"] == "submitted"
            assert state["outcome"] == "submitted"
        else:
            assert outcome is not SubmitOutcome.SUBMITTED
            assert state["chunk_status"] == "submitting"
            assert state["outcome"] == "invoking"
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM sms_vendor_attempt WHERE chunk_id=:id"),
                    {"id": chunk_id},
                )
                == 1
            )
    finally:
        await _cleanup(engine, app_id=app_id, batch_ids=batches, chunk_ids=chunks)
        await engine.dispose()


@pytest.mark.asyncio
async def test_legacy_repair_prelocks_all_chunks_before_shared_batch() -> None:
    """同批次两分片：恢复器等待第二个 chunk 时不得已持有第一个的 batch 锁。"""
    from tests.integration.test_vendor_attempt_finalize_postgres import _snapshot

    engine = create_async_engine(make_url(os.environ["OUTBOX_POSTGRES_DSN"]))
    app_id = None
    batches: list[int] = []
    chunks: list[int] = []
    held, release = asyncio.Event(), asyncio.Event()
    repair_pid: list[int] = []
    tasks: list[asyncio.Task[Any]] = []

    class Connection:
        def __init__(self, connection: Any) -> None:
            self.connection = connection

        async def execute(self, statement: Any, params: Any = None) -> Any:
            result = await self.connection.execute(statement, params)
            sql = " ".join(str(statement).split())
            if "SELECT batch_id FROM sms_chunk" in sql and "FOR UPDATE" in sql:
                held.set()
                await asyncio.wait_for(release.wait(), 5)
            return result

    class Engine:
        @asynccontextmanager
        async def begin(self):
            async with engine.begin() as connection:
                await connection.execute(text("SET LOCAL lock_timeout='4s'"))
                yield Connection(connection)

        async def dispose(self) -> None:
            pass

    try:
        await _prepare_db(engine)
        nonce = uuid4().hex
        app_id = await _insert_app(engine, nonce)
        store = _store(engine.url)
        for index in (1, 2):
            batch_id, chunk_id = await _seed_chunk(engine, app_id=app_id, nonce=nonce, index=index)
            batches.append(batch_id)
            chunks.append(chunk_id)
            attempt = await store.begin_vendor_invoke(
                chunk_id, vendor_id="zhihui", adapter_id="zhihui", reason="primary"
            )
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE sms_vendor_attempt SET outcome='rejected',safe_to_failover=true,"
                        "vendor_code=1002 WHERE id=:id"
                    ),
                    {"id": attempt.id},
                )
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE sms_chunk SET batch_id=:batch,chunk_no=2 WHERE id=:id"),
                {"batch": batches[0], "id": chunks[1]},
            )
            await connection.execute(
                text("UPDATE sms_message SET batch_id=:batch WHERE chunk_id=:id"),
                {"batch": batches[0], "id": chunks[1]},
            )
            await connection.execute(
                text("UPDATE sms_batch SET total=2 WHERE id=:id"), {"id": batches[0]}
            )
        finalizer = _store(engine.url)
        finalizer._engine = Engine
        tasks.append(asyncio.create_task(finalizer.mark_failed(chunks[1], 1002, "synthetic")))
        await asyncio.wait_for(held.wait(), 5)

        async def repair() -> int:
            async with engine.begin() as connection:
                await connection.execute(text("SET LOCAL lock_timeout='4s'"))
                repair_pid.append(int(await connection.scalar(text("SELECT pg_backend_pid()"))))
                return await store.repair_legacy_safe_reject_submitting(connection)

        tasks.append(asyncio.create_task(repair()))
        async with asyncio.timeout(3):
            async with engine.connect() as observer:
                # 使用服务器实际锁等待作为放行条件。
                while not repair_pid or not await observer.scalar(  # noqa: ASYNC110
                    text("SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid=:pid AND NOT granted)"),
                    {"pid": repair_pid[0]},
                ):
                    await asyncio.sleep(0.005)
        release.set()
        outcomes = await asyncio.wait_for(asyncio.gather(*tasks), 5)
        assert outcomes == [None, 1]
        assert (await _snapshot(engine, chunks[0]))["chunk_status"] == "failed"
        assert (await _snapshot(engine, chunks[1]))["chunk_status"] == "failed"
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _cleanup(engine, app_id=app_id, batch_ids=batches, chunk_ids=chunks)
        await engine.dispose()


@pytest.mark.asyncio
async def test_failure_events_enforce_append_only_runtime_permissions() -> None:
    engine = create_async_engine(make_url(os.environ["OUTBOX_POSTGRES_DSN"]))
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                for role in (
                    "sms_auth",
                    "sms_accept",
                    "sms_send",
                    "sms_callback",
                    "sms_export",
                    "sms_scheduler",
                    "sms_metrics",
                ):
                    for privilege in ("UPDATE", "DELETE", "TRUNCATE"):
                        assert not await connection.scalar(
                            text(
                                "SELECT has_table_privilege(:role,"
                                "'send_db_failure_event',:privilege)"
                            ),
                            {"role": role, "privilege": privilege},
                        )
                    assert bool(
                        await connection.scalar(
                            text(
                                "SELECT has_table_privilege(:role,'send_db_failure_event','INSERT')"
                            ),
                            {"role": role},
                        )
                    ) is (role == "sms_send")
                await connection.execute(text("SET LOCAL ROLE sms_send"))
                await connection.execute(
                    text(
                        "INSERT INTO send_db_failure_event(id,phase,reason) "
                        "VALUES(:id,'claim','cas_lost')"
                    ),
                    {"id": uuid4()},
                )
                await connection.execute(text("SET LOCAL ROLE sms_metrics"))
                assert (
                    await connection.scalar(
                        text(
                            "SELECT count(reason) FROM send_db_failure_event "
                            "WHERE phase='claim' AND reason='cas_lost'"
                        )
                    )
                    >= 1
                )
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_applied", [False, True])
async def test_lost_commit_confirmation_reads_fact_without_repeating_send(
    commit_applied: bool,
) -> None:
    from app.tasks.send import SendWorker, SubmitOutcome
    from tests.integration.test_vendor_attempt_finalize_postgres import _snapshot
    from tests.integration.test_vendor_failover_pending_postgres import _payload, _worker_store
    from tests.vendor_failover_r5_support import CountingBucket, CountingGateway

    engine = create_async_engine(make_url(os.environ["OUTBOX_POSTGRES_DSN"]))
    app_id = None
    batches: list[int] = []
    chunks: list[int] = []
    result_transactions = 0

    class Connection:
        def __init__(self, connection: Any) -> None:
            self.connection = connection
            self.finalized = False

        async def execute(self, statement: Any, params: Any = None) -> Any:
            nonlocal result_transactions
            sql = " ".join(str(statement).split())
            if "UPDATE sms_vendor_attempt SET outcome=:outcome" in sql:
                self.finalized = True
                result_transactions += 1
            return await self.connection.execute(statement, params)

        def __getattr__(self, name: str) -> Any:
            return getattr(self.connection, name)

    class Engine:
        @asynccontextmanager
        async def begin(self):
            async with engine.begin() as connection:
                wrapped = Connection(connection)
                yield wrapped
                if wrapped.finalized and not commit_applied:
                    raise ConnectionError("synthetic connection lost before commit")
            if wrapped.finalized and commit_applied:
                raise ConnectionError("synthetic confirmation lost after commit")

        @asynccontextmanager
        async def connect(self):
            async with engine.connect() as connection:
                yield connection

        async def dispose(self) -> None:
            pass

    try:
        await _prepare_db(engine)
        nonce = uuid4().hex
        app_id = await _insert_app(engine, nonce)
        batch_id, chunk_id = await _seed_chunk(
            engine, app_id=app_id, nonce=nonce, index=1, status="pending"
        )
        batches.append(batch_id)
        chunks.append(chunk_id)
        store = _worker_store(engine.url)
        store.settings.alert_smtp_allowed_host_set = frozenset()
        store._engine = Engine
        gateway = CountingGateway(["synthetic-task"])
        outcome = await SendWorker(gateway, store, CountingBucket()).submit(
            _payload(chunk_id, batch_id, nonce, 1, status="pending"), lane="realtime"
        )
        assert gateway.calls == 1
        assert result_transactions == 1
        state = await _snapshot(engine, chunk_id)
        if commit_applied:
            assert outcome is SubmitOutcome.SUBMITTED
            assert state["chunk_status"] == state["outcome"] == "submitted"
        else:
            assert outcome is not SubmitOutcome.SUBMITTED
            assert state["chunk_status"] == "submitting"
            assert state["outcome"] == "invoking"
    finally:
        await _cleanup(engine, app_id=app_id, batch_ids=batches, chunk_ids=chunks)
        await engine.dispose()


@pytest.mark.asyncio
async def test_cross_app_legacy_and_recovery_prelock_complete_balance_set() -> None:
    """chunk/reservation 顺序与 app 顺序相反，两个真实恢复事务仍按 app 锁余额。"""
    from app.services.send_inflight import (
        bind_in_flight_reservation,
        reconcile_in_flight_reservations,
        reserve_in_flight_chunks,
    )

    engine = create_async_engine(make_url(os.environ["OUTBOX_POSTGRES_DSN"]))
    apps: list[int] = []
    batches: list[int] = []
    chunks: list[int] = []
    held, release = asyncio.Event(), asyncio.Event()
    repair_pid: list[int] = []
    tasks: list[asyncio.Task[Any]] = []

    class Connection:
        def __init__(self, connection: Any) -> None:
            self.connection = connection
            self.paused = False

        async def execute(self, statement: Any, params: Any = None) -> Any:
            result = await self.connection.execute(statement, params)
            sql = " ".join(str(statement).split())
            if (
                not self.paused
                and "SELECT reserved_chunks, conservation_blocked_at" in sql
                and params["app_id"] == apps[0]
            ):
                self.paused = True
                held.set()
                await asyncio.wait_for(release.wait(), 5)
            return result

        def __getattr__(self, name: str) -> Any:
            return getattr(self.connection, name)

    try:
        await _prepare_db(engine)
        nonce = uuid4().hex
        apps.extend([await _insert_app(engine, nonce + suffix) for suffix in ("a", "b")])
        assert apps[0] < apps[1]
        store = _store(engine.url)
        # 先创建 B 分片，再创建 A 分片，防止按 chunk 顺序恰好掩盖 balance 逆序。
        for index, app_id in enumerate(reversed(apps), start=1):
            batch_id, chunk_id = await _seed_chunk(engine, app_id=app_id, nonce=nonce, index=index)
            batches.append(batch_id)
            chunks.append(chunk_id)
            attempt = await store.begin_vendor_invoke(
                chunk_id, vendor_id="zhihui", adapter_id="zhihui", reason="primary"
            )
            async with engine.begin() as connection:
                reservation = await reserve_in_flight_chunks(
                    connection, app_id=app_id, estimated=1, limit=100
                )
                await bind_in_flight_reservation(
                    connection,
                    reservation_id=reservation.id,
                    generation=reservation.generation,
                    batch_id=batch_id,
                    app_id=app_id,
                )
                await connection.execute(
                    text(
                        "UPDATE sms_vendor_attempt SET outcome='rejected',safe_to_failover=true,"
                        "vendor_code=1002 WHERE id=:id"
                    ),
                    {"id": attempt.id},
                )
        # 另两个预留只供回收；不与 legacy 事务共享任何 batch/chunk/reservation 行。
        for app_id in apps:
            async with engine.begin() as connection:
                reservation = await reserve_in_flight_chunks(
                    connection, app_id=app_id, estimated=1, limit=100
                )
                await connection.execute(
                    text(
                        "UPDATE send_inflight_reservation SET expires_at=now()-interval '1 minute' "
                        "WHERE id=:id"
                    ),
                    {"id": reservation.id},
                )

        async def recover() -> int:
            async with engine.begin() as connection:
                await connection.execute(text("SET LOCAL lock_timeout='4s'"))
                return await reconcile_in_flight_reservations(Connection(connection))

        async def repair() -> int:
            async with engine.begin() as connection:
                await connection.execute(text("SET LOCAL lock_timeout='4s'"))
                repair_pid.append(int(await connection.scalar(text("SELECT pg_backend_pid()"))))
                return await store.repair_legacy_safe_reject_submitting(connection)

        tasks.append(asyncio.create_task(recover()))
        await asyncio.wait_for(held.wait(), 5)
        tasks.append(asyncio.create_task(repair()))
        async with asyncio.timeout(3):
            async with engine.connect() as observer:
                while not repair_pid or not await observer.scalar(  # noqa: ASYNC110
                    text("SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid=:pid AND NOT granted)"),
                    {"pid": repair_pid[0]},
                ):
                    await asyncio.sleep(0.005)
        release.set()
        outcomes = await asyncio.wait_for(asyncio.gather(*tasks), 5)
        assert outcomes == [2, 2]
        async with engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT reserved_chunks FROM send_inflight_balance "
                            "WHERE app_id=ANY(:ids)"
                        ),
                        {"ids": apps},
                    )
                )
                .scalars()
                .all()
            )
            assert rows == [0, 0]
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if apps:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE sms_batch SET send_inflight_reservation_id=NULL WHERE id=ANY(:ids)"
                    ),
                    {"ids": batches},
                )
                await connection.execute(
                    text("DELETE FROM send_inflight_reservation WHERE app_id=ANY(:ids)"),
                    {"ids": apps},
                )
                await connection.execute(
                    text("DELETE FROM send_inflight_balance WHERE app_id=ANY(:ids)"), {"ids": apps}
                )
        await _cleanup(engine, app_id=None, batch_ids=batches, chunk_ids=chunks)
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM app WHERE id=ANY(:ids)"), {"ids": apps})
        await engine.dispose()
