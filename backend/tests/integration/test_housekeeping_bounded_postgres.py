"""仅在官方隔离 PostgreSQL 入口运行的清理分页与准入验收。"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.bulk_admission import SqlBulkTaskLease
from app.services.housekeeping import HousekeepingService, ImportFileStore, LifecyclePolicy
from app.services.housekeeping_repository import SqlHousekeepingRepository
from app.services.outbox import OutboxEventSpec, OutboxLeaseLost
from app.services.outbox_repository import SqlOutboxRepository, enqueue_outbox

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


@pytest.mark.asyncio
async def test_import_cleanup_bounds_children_preserves_active_parse_and_resumes(
    tmp_path: Any,
) -> None:
    url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    owner = create_async_engine(url)
    runtime = create_async_engine(url)

    @event.listens_for(runtime.sync_engine, "connect")
    def runtime_role(connection: Any, _record: Any) -> None:
        cursor = connection.cursor()
        cursor.execute("SET ROLE sms_send")
        cursor.close()

    repo = SqlHousekeepingRepository(cast(Any, SimpleNamespace(database_url=url)))
    repo._engine = lambda: runtime  # type: ignore[method-assign]
    cutoff = datetime.now(UTC)
    nonce = uuid4().hex
    ids: list[int] = []
    expired_file = tmp_path / f"{nonce}.csv"
    expired_file.write_text("phone_mask,reason\n", encoding="utf-8")
    try:
        async with owner.begin() as connection:
            for state in ("expired", "active", "fresh"):
                result = await connection.execute(
                    text("""
                    INSERT INTO import_task(creator,filename,expires_at,invalid_file,
                      parse_status,parse_lease_id,parse_lease_expires_at,source_file,source_size)
                    VALUES(:creator,'upload.csv',:expiry,:invalid_file,:parse_status,
                      :lease_id,:lease_expiry,:source_file,:source_size) RETURNING id
                """),
                    {
                        "creator": f"cleanup-{nonce}",
                        "expiry": cutoff + timedelta(days=1 if state == "fresh" else -1),
                        "invalid_file": expired_file.name if state == "expired" else None,
                        "parse_status": "processing" if state == "active" else "ready",
                        "lease_id": uuid4() if state == "active" else None,
                        "lease_expiry": cutoff + timedelta(minutes=5)
                        if state == "active"
                        else None,
                        "source_file": "active.smsx" if state == "active" else None,
                        "source_size": 0 if state == "active" else None,
                    },
                )
                ids.append(int(result.scalar_one()))
            for index, import_id in enumerate(ids):
                await connection.execute(
                    text("""
                    INSERT INTO import_phone(
                      import_task_id,phone_enc,phone_hmac,phone_mask,source_row)
                    SELECT :import_id,decode(repeat('ab',32),'hex'),lpad(n::text,64,'0'),
                      '138****0000',n FROM generate_series(1,:rows) n
                """),
                    {"import_id": import_id, "rows": 1203 if index == 0 else 3},
                )

        assert await repo.finish_import(ids[0], cutoff=cutoff) == 0
        page = await repo.cleanup_page(
            "import_phones", LifecyclePolicy(90, 90, 30), cutoff=cutoff, cursor=None, limit=127
        )
        assert page.affected == 127
        assert expired_file.exists()

        real_cleanup = repo.cleanup_page
        attempts = 0

        async def fail_second_page(table: str, policy: LifecyclePolicy, **kwargs: Any) -> Any:
            nonlocal attempts
            if table == "import_phones":
                attempts += 1
                if attempts == 2:
                    raise RuntimeError("synthetic interruption after committed page")
            return await real_cleanup(table, policy, **kwargs)

        repo.cleanup_page = fail_second_page  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            await HousekeepingService(repo, ImportFileStore(tmp_path), batch_size=127).run(
                cutoff=cutoff
            )
        async with owner.connect() as connection:
            remaining = int(
                (
                    await connection.execute(
                        text("SELECT count(*) FROM import_phone WHERE import_task_id=:id"),
                        {"id": ids[0]},
                    )
                ).scalar_one()
            )
        assert remaining == 1203 - 2 * 127
        assert expired_file.exists()
        repo.cleanup_page = real_cleanup  # type: ignore[method-assign]
        result = await HousekeepingService(repo, ImportFileStore(tmp_path), batch_size=127).run(
            cutoff=cutoff
        )
        assert result.imports == 1
        assert not expired_file.exists()
        async with owner.connect() as connection:
            retained = (
                (
                    await connection.execute(
                        text("SELECT id FROM import_task WHERE id=ANY(:ids) ORDER BY id"),
                        {"ids": ids},
                    )
                )
                .scalars()
                .all()
            )
            assert retained == ids[1:]
            assert (
                int(
                    (
                        await connection.execute(
                            text(
                                "SELECT count(*) FROM import_phone WHERE import_task_id=ANY(:ids)"
                            ),
                            {"ids": ids},
                        )
                    ).scalar_one()
                )
                == 6
            )
            assert not bool(
                (
                    await connection.execute(
                        text("SELECT has_table_privilege('sms_send','audit_log','DELETE')")
                    )
                ).scalar_one()
            )
    finally:
        await runtime.dispose()
        async with owner.begin() as connection:
            await connection.execute(
                text("DELETE FROM import_task WHERE id=ANY(:ids)"), {"ids": ids}
            )
        await owner.dispose()


@pytest.mark.asyncio
async def test_housekeeping_usage_child_pages_and_unexpired_or_uncertain_facts() -> None:
    url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    engine = create_async_engine(url)
    repo = SqlHousekeepingRepository(cast(Any, SimpleNamespace(database_url=url)))
    repo._engine = lambda: engine  # type: ignore[method-assign]
    cutoff = datetime.now(UTC)
    reservation_ids = [uuid4(), uuid4(), uuid4()]
    subject_ids: list[Any] = []
    try:
        async with engine.begin() as connection:
            for index, reservation_id in enumerate(reservation_ids):
                await connection.execute(
                    text("""
                    INSERT INTO usage_reservation(
                      id,request_key,app_id,dept,category,usage_date,state)
                    VALUES(:id,:key,0,'cleanup','verify',:day,:state)
                """),
                    {
                        "id": reservation_id,
                        "key": f"acceptance:{reservation_id}",
                        "day": (cutoff - timedelta(days=120)).date(),
                        "state": "uncertain" if index == 1 else "released",
                    },
                )
            for _ in range(13):
                subject_id = uuid4()
                subject_ids.append(subject_id)
                await connection.execute(
                    text("""
                    INSERT INTO usage_frequency_subject(id,projection_hmac,created_at)
                    VALUES(:id,:hmac,:old)
                """),
                    {
                        "id": subject_id,
                        "hmac": uuid4().hex + uuid4().hex,
                        "old": cutoff - timedelta(days=120),
                    },
                )
                await connection.execute(
                    text("""
                    INSERT INTO usage_frequency_entry(
                      reservation_id,subject_id,category,window_kind,
                      window_key,usage_date,projection_key,counted,expires_at)
                    VALUES(:reservation,:subject,'verify','day','20200101',:day,:key,true,:expiry)
                """),
                    {
                        "reservation": reservation_ids[0],
                        "subject": subject_id,
                        "day": (cutoff - timedelta(days=120)).date(),
                        "key": f"freq:v:{uuid4().hex}",
                        "expiry": cutoff - timedelta(days=119),
                    },
                )
            for index, reservation_id in enumerate(reservation_ids):
                await connection.execute(
                    text("""
                    INSERT INTO usage_quota_entry(reservation_id,dimension_kind,dimension_value,
                      usage_date,amount,projection_key,expires_at)
                    VALUES(:id,'app','0',:day,1,'quota:app:0:20200101',:expiry)
                """),
                    {
                        "id": reservation_id,
                        "day": (cutoff - timedelta(days=120)).date(),
                        "expiry": cutoff + timedelta(days=1 if index == 2 else -119),
                    },
                )
        policy = LifecyclePolicy(90, 90, 30)
        cursor = None
        sizes: list[int] = []
        while True:
            page = await repo.cleanup_page(
                "usage_frequency", policy, cutoff=cutoff, cursor=cursor, limit=5
            )
            if not page.affected:
                break
            sizes.append(page.affected)
            cursor = page.cursor
        assert sizes == [5, 5, 3]
        assert (
            await repo.cleanup_page("usage", policy, cutoff=cutoff, cursor=None, limit=5)
        ).affected == 0
        assert (
            await repo.cleanup_page("usage_quota", policy, cutoff=cutoff, cursor=None, limit=5)
        ).affected == 1
        assert (
            await repo.cleanup_page("usage", policy, cutoff=cutoff, cursor=None, limit=5)
        ).affected == 1
        async with engine.connect() as connection:
            kept = set(
                (
                    await connection.execute(
                        text("SELECT id FROM usage_reservation WHERE id=ANY(:ids)"),
                        {"ids": reservation_ids},
                    )
                ).scalars()
            )
            assert kept == set(reservation_ids[1:])
            assert (
                int(
                    (
                        await connection.execute(
                            text(
                                "SELECT count(*) FROM usage_quota_entry "
                                "WHERE reservation_id=ANY(:ids)"
                            ),
                            {"ids": reservation_ids},
                        )
                    ).scalar_one()
                )
                == 2
            )
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM usage_reservation WHERE id=ANY(:ids)"), {"ids": reservation_ids}
            )
            await connection.execute(
                text("DELETE FROM usage_frequency_subject WHERE id=ANY(:ids)"), {"ids": subject_ids}
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_bulk_admission_contends_across_sessions_and_expired_publisher_cannot_mark() -> None:
    url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    settings = cast(Any, SimpleNamespace(database_url=url))
    first, second = SqlBulkTaskLease(settings), SqlBulkTaskLease(settings)
    engine = create_async_engine(url)
    repo = SqlOutboxRepository(settings)
    event_id = None
    try:
        assert await first.try_acquire()
        assert not await second.try_acquire()
        await first.release()
        assert await second.try_acquire()
        await second.release()
        reference = uuid4().hex
        async with engine.begin() as connection:
            event_id = await enqueue_outbox(
                connection,
                OutboxEventSpec(
                    "batch.ready",
                    "sms_batch",
                    reference,
                    "app.tasks.send.process_batch",
                    "bulk",
                    (reference,),
                    f"batch.ready:{reference}",
                ),
            )
        leased = (await repo.lease_due(limit=1, lease_seconds=60))[0]
        assert leased.event_id == event_id
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE outbox_event SET lease_expires_at=now()-interval '1 second' "
                    "WHERE id=:id"
                ),
                {"id": event_id},
            )
        with pytest.raises(OutboxLeaseLost):
            await repo.mark_published(event_id, leased.lease_id)
        with pytest.raises(OutboxLeaseLost):
            await repo.mark_publish_failed(event_id, leased.lease_id, "SyntheticFailure")
        replacement = (await repo.lease_due(limit=1, lease_seconds=60))[0]
        assert replacement.lease_id != leased.lease_id
        claim = await repo.claim_execution(event_id, lease_seconds=60)
        assert claim is not None
        # 消费者抢先进入 processing/completed 时，dispatcher 只接受现状，不覆盖。
        await repo.mark_published(event_id, replacement.lease_id)
        await repo.complete(event_id, claim.lease_id)
        await repo.mark_publish_failed(event_id, replacement.lease_id, "LateFailure")
    finally:
        await first.release()
        await second.release()
        if event_id is not None:
            async with engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM outbox_event WHERE id=:id"), {"id": event_id}
                )
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["acquire_commit", "release"])
async def test_admission_lock_failure_discards_managed_pool_session(
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    """实际 PG session 锁已取得时取消/失败，第二个独立 pool 仍可重新准入。"""
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncConnection

    from app.core import jobtrack
    from app.core.runtime_resources import DEFAULT_BUDGETS, ManagedAsyncEngine

    url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    first_engine, second_engine = create_async_engine(url), create_async_engine(url)
    managed_first = ManagedAsyncEngine(first_engine, "worker", DEFAULT_BUDGETS["worker"])
    managed_second = ManagedAsyncEngine(second_engine, "worker", DEFAULT_BUDGETS["worker"])
    settings = cast(Any, SimpleNamespace(database_url=url))
    first, second = SqlBulkTaskLease(settings), SqlBulkTaskLease(settings)
    monkeypatch.setattr(jobtrack, "database_engine", lambda _: managed_first)
    try:
        if failure_stage == "acquire_commit":
            real_commit = AsyncConnection.commit

            async def interrupted_commit(connection: AsyncConnection) -> None:
                await real_commit(connection)
                raise asyncio.CancelledError("synthetic lost acquire response")

            with monkeypatch.context() as patch:
                patch.setattr(AsyncConnection, "commit", interrupted_commit)
                with pytest.raises(asyncio.CancelledError):
                    await first.try_acquire()
        else:
            assert await first.try_acquire()
            real_execute = AsyncConnection.execute

            async def fail_unlock(
                connection: AsyncConnection, statement: Any, *args: Any, **kwargs: Any
            ) -> Any:
                if "pg_advisory_unlock" in str(statement):
                    raise RuntimeError("synthetic unlock failure")
                return await real_execute(connection, statement, *args, **kwargs)

            with monkeypatch.context() as patch:
                patch.setattr(AsyncConnection, "execute", fail_unlock)
                with pytest.raises(RuntimeError, match="synthetic unlock"):
                    await first.release()
        monkeypatch.setattr(jobtrack, "database_engine", lambda _: managed_second)
        assert await second.try_acquire()
    finally:
        await first.release()
        await second.release()
        await first_engine.dispose()
        await second_engine.dispose()


@pytest.mark.asyncio
async def test_raw_cleanup_retains_uncertain_unprocessed_claim_and_pending_audit() -> None:
    url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    engine = create_async_engine(url)
    repo = SqlHousekeepingRepository(cast(Any, SimpleNamespace(database_url=url)))
    repo._engine = lambda: engine  # type: ignore[method-assign]
    cutoff = datetime.now(UTC)
    reference = uuid4().hex
    batch_id: int | None = None
    raw_ids: list[int] = []
    states = [
        "expired",
        "expired",
        "expired",
        "uncertain",
        "unprocessed",
        "active_claim",
        "pending_audit",
        "fresh",
    ]
    try:
        async with engine.begin() as connection:
            audit_count = int(
                (await connection.execute(text("SELECT count(*) FROM audit_log"))).scalar_one()
            )
            batch_id = int(
                (
                    await connection.execute(
                        text("""
                INSERT INTO sms_batch(
                  batch_no,channel,dept,status,display_content_enc,send_content_enc)
                VALUES(:reference,'web','cleanup','sending',:encrypted,:encrypted) RETURNING id
            """),
                        {"reference": reference, "encrypted": bytes.fromhex("ab" * 32)},
                    )
                ).scalar_one()
            )
            await connection.execute(
                text("""
                INSERT INTO sms_chunk(batch_id,chunk_no,custom_id,phone_count,status)
                VALUES(:id,1,:custom,1,'uncertain')
            """),
                {"id": batch_id, "custom": reference},
            )
            for state in states:
                result = await connection.execute(
                    text("""
                    INSERT INTO raw_vendor_log(source,payload_enc,payload_sha256,custom_ids,
                      processed,parse_state,replay_eligibility,fetched_at,
                      processing_lease_id,processing_lease_expires_at,system_replay_audit_state)
                    VALUES('report',:encrypted,:digest,:customs,:processed,:parse,'never',:fetched,
                      :lease_id,:lease_expiry,:audit_state) RETURNING id
                """),
                    {
                        "encrypted": bytes.fromhex("ab" * 32),
                        "digest": "a" * 64,
                        "customs": [reference] if state == "uncertain" else [],
                        "processed": state != "unprocessed",
                        "parse": "unattempted" if state == "unprocessed" else "processed",
                        "fetched": cutoff - timedelta(days=1 if state == "fresh" else 120),
                        "lease_id": uuid4() if state == "active_claim" else None,
                        "lease_expiry": cutoff + timedelta(minutes=5)
                        if state == "active_claim"
                        else None,
                        "audit_state": "pending" if state == "pending_audit" else None,
                    },
                )
                raw_ids.append(int(result.scalar_one()))
        sizes: list[int] = []
        cursor = None
        while True:
            page = await repo.cleanup_page(
                "raw", LifecyclePolicy(90, 90, 30), cutoff=cutoff, cursor=cursor, limit=2
            )
            if not page.affected:
                break
            sizes.append(page.affected)
            cursor = page.cursor
        assert sizes == [2, 1]
        async with engine.connect() as connection:
            remaining = set(
                (
                    await connection.execute(
                        text("SELECT id FROM raw_vendor_log WHERE id=ANY(:ids)"), {"ids": raw_ids}
                    )
                ).scalars()
            )
            assert remaining == set(raw_ids[3:])
            assert (
                int((await connection.execute(text("SELECT count(*) FROM audit_log"))).scalar_one())
                == audit_count
            )
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM raw_vendor_log WHERE id=ANY(:ids)"), {"ids": raw_ids}
            )
            if batch_id is not None:
                await connection.execute(
                    text("DELETE FROM sms_chunk WHERE batch_id=:id"), {"id": batch_id}
                )
                await connection.execute(
                    text("DELETE FROM sms_batch WHERE id=:id"), {"id": batch_id}
                )
        await engine.dispose()
