from __future__ import annotations

import asyncio
import base64
import os
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.runtime_resources import bind_connection_system_audit
from app.services.crypto import CryptoService
from app.services.reconcile_repository import SqlRecoveryRepository
from app.tasks.send import FinalizeKind
from app.tasks.send_repository import SqlChunkStore
from scripts_support.maintain_partitions import maintain

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)

_AES = base64.b64encode(b"v" * 32).decode()
_HMAC = base64.b64encode(b"v" * 32).decode()
_BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _crypto() -> CryptoService:
    return CryptoService.from_secret_values(_AES, _HMAC)


def _settings(database_url: Any) -> Any:
    return cast(Any, SimpleNamespace(database_url=database_url))


def _store(database_url: Any) -> SqlChunkStore:
    return SqlChunkStore(_crypto(), settings=_settings(database_url), redis=object())


def child_finalize() -> None:
    """独立进程调用原子终结，供父进程在锁等待中 SIGKILL。"""

    Path(os.environ["SMS_FINALIZE_READY"]).write_text("ready", encoding="utf-8")

    async def _run() -> None:
        database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
        report = await _store(database_url).finalize_vendor_attempt(
            int(os.environ["SMS_FINALIZE_ATTEMPT_ID"]),
            int(os.environ["SMS_FINALIZE_CHUNK_ID"]),
            expected_generation=int(os.environ["SMS_FINALIZE_GENERATION"]),
            result="submitted",
            vendor_task_id="task-kill",
        )
        raise AssertionError(f"killed child must not commit {report.kind}")

    asyncio.run(_run())


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
                        "name": f"finalize-{nonce}",
                        "api_key_hash": "b" * 64,
                        "api_key_prefix": nonce[:8],
                    },
                )
            ).scalar_one()
        )


async def _seed_chunk(
    engine: Any,
    *,
    app_id: int,
    nonce: str,
    index: int,
    status: str = "submitting",
    vendor_task_id: str | None = None,
    submitted_at: bool = False,
    message_status: str = "pending",
    batch_status: str = "sending",
) -> tuple[int, int]:
    custom_id = f"{nonce[:24]}{index:08d}"
    protected = _crypto().protect_phone("13800138000")
    async with engine.begin() as connection:
        batch_id = int(
            (
                await connection.execute(
                    text(
                        """
                        INSERT INTO sms_batch(
                          batch_no,channel,app_id,dept,content,
                          display_content_enc,send_content_enc,status,total
                        ) VALUES(
                          :batch_no,'api',:app_id,'平台部','[encrypted]',
                          :content_enc,:content_enc,:status,1
                        ) RETURNING id
                        """
                    ),
                    {
                        "batch_no": custom_id,
                        "app_id": app_id,
                        "content_enc": b"cipher-content",
                        "status": batch_status,
                    },
                )
            ).scalar_one()
        )
        chunk_id = int(
            (
                await connection.execute(
                    text(
                        """
                        INSERT INTO sms_chunk(
                          batch_id,chunk_no,custom_id,vendor_task_id,
                          phone_count,status,submitted_at,submitting_since
                        ) VALUES(
                          :batch_id,1,:custom_id,:vendor_task_id,
                          1,:status,
                          CASE WHEN :submitted_at THEN now() ELSE NULL END,
                          now()
                        ) RETURNING id
                        """
                    ),
                    {
                        "batch_id": batch_id,
                        "custom_id": custom_id,
                        "vendor_task_id": vendor_task_id,
                        "status": status,
                        "submitted_at": submitted_at,
                    },
                )
            ).scalar_one()
        )
        await connection.execute(
            text(
                """
                INSERT INTO sms_message(
                  batch_id,chunk_id,phone_enc,phone_hmac,phone_mask,
                  key_version,status
                ) VALUES(
                  :batch_id,:chunk_id,:phone_enc,:phone_hmac,:phone_mask,
                  :key_version,:message_status
                )
                """
            ),
            {
                "batch_id": batch_id,
                "chunk_id": chunk_id,
                "phone_enc": protected.phone_enc,
                "phone_hmac": protected.phone_hmac,
                "phone_mask": protected.phone_mask,
                "key_version": protected.key_version,
                "message_status": message_status,
            },
        )
    return batch_id, chunk_id


async def _snapshot(engine: Any, chunk_id: int) -> dict[str, Any]:
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    """
                    SELECT c.status AS chunk_status, c.vendor_task_id,
                           c.route_generation, a.outcome, a.generation,
                           a.safe_to_failover, a.vendor_id,
                           (
                             SELECT m.status FROM sms_message m
                             WHERE m.chunk_id=c.id
                             ORDER BY m.id LIMIT 1
                           ) AS message_status
                    FROM sms_chunk c
                    LEFT JOIN sms_vendor_attempt a ON a.chunk_id=c.id
                    WHERE c.id=:chunk_id
                    ORDER BY a.generation DESC NULLS LAST
                    LIMIT 1
                    """
                ),
                {"chunk_id": chunk_id},
            )
        ).mappings().one()
    return dict(row)


async def _cleanup(
    engine: Any,
    *,
    app_id: int | None,
    batch_ids: list[int],
    chunk_ids: list[int],
) -> None:
    async with engine.begin() as connection:
        if chunk_ids:
            await connection.execute(
                text("DELETE FROM sms_vendor_attempt WHERE chunk_id=ANY(:ids)"),
                {"ids": chunk_ids},
            )
            await connection.execute(
                text("DELETE FROM sms_message WHERE chunk_id=ANY(:ids)"),
                {"ids": chunk_ids},
            )
            await connection.execute(
                text("DELETE FROM sms_chunk WHERE id=ANY(:ids)"),
                {"ids": chunk_ids},
            )
        if batch_ids:
            await connection.execute(
                text("DELETE FROM sms_batch WHERE id=ANY(:ids)"),
                {"ids": batch_ids},
            )
        if app_id is not None:
            await connection.execute(
                text("DELETE FROM app WHERE id=:app_id"),
                {"app_id": app_id},
            )


@pytest.mark.asyncio
async def test_finalize_submitted_is_atomic_on_postgres() -> None:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    nonce = uuid4().hex
    app_id: int | None = None
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
            result="submitted",
            vendor_task_id="task-ok",
        )
        assert report.kind is FinalizeKind.APPLIED
        state = await _snapshot(engine, chunk_id)
        assert state["outcome"] == "submitted"
        assert state["chunk_status"] == "submitted"
        assert state["message_status"] == "sent"
        assert state["vendor_task_id"] is not None
        with pytest.raises(RuntimeError, match="irreversible"):
            await store.begin_vendor_invoke(
                chunk_id, vendor_id="secondary", adapter_id="zhihui", reason="failover"
            )

        batch_id, stuck_id = await _seed_chunk(
            engine, app_id=app_id, nonce=nonce, index=2, status="failed"
        )
        batch_ids.append(batch_id)
        chunk_ids.append(stuck_id)
        stuck = await store.begin_vendor_invoke(
            stuck_id, vendor_id="zhihui", adapter_id="zhihui", reason="primary"
        )
        lost = await store.finalize_vendor_attempt(
            stuck.id,
            stuck_id,
            expected_generation=stuck.generation,
            result="submitted",
            vendor_task_id="task-lost",
        )
        assert lost.kind is FinalizeKind.LOST_CAS
        stuck_state = await _snapshot(engine, stuck_id)
        assert stuck_state["outcome"] == "invoking"
        assert stuck_state["chunk_status"] == "failed"
        assert stuck_state["message_status"] == "pending"
    finally:
        await _cleanup(engine, app_id=app_id, batch_ids=batch_ids, chunk_ids=chunk_ids)
        await engine.dispose()


@pytest.mark.asyncio
async def test_finalize_uncertain_and_rejects_on_postgres() -> None:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    nonce = uuid4().hex
    app_id: int | None = None
    batch_ids: list[int] = []
    chunk_ids: list[int] = []
    try:
        await _prepare_db(engine)
        app_id = await _insert_app(engine, nonce)
        store = _store(database_url)

        batch_id, uncertain_id = await _seed_chunk(
            engine, app_id=app_id, nonce=nonce, index=1
        )
        batch_ids.append(batch_id)
        chunk_ids.append(uncertain_id)
        attempt = await store.begin_vendor_invoke(
            uncertain_id, vendor_id="zhihui", adapter_id="zhihui", reason="primary"
        )
        report = await store.finalize_vendor_attempt(
            attempt.id,
            uncertain_id,
            expected_generation=attempt.generation,
            result="uncertain",
        )
        assert report.kind is FinalizeKind.APPLIED
        state = await _snapshot(engine, uncertain_id)
        assert state["outcome"] == "uncertain"
        assert state["chunk_status"] == "uncertain"
        with pytest.raises(RuntimeError, match="irreversible"):
            await store.begin_vendor_invoke(
                uncertain_id, vendor_id="secondary", adapter_id="zhihui", reason="failover"
            )

        batch_id, safe_id = await _seed_chunk(
            engine, app_id=app_id, nonce=nonce, index=2
        )
        batch_ids.append(batch_id)
        chunk_ids.append(safe_id)
        attempt = await store.begin_vendor_invoke(
            safe_id, vendor_id="zhihui", adapter_id="zhihui", reason="primary"
        )
        report = await store.finalize_vendor_attempt(
            attempt.id,
            safe_id,
            expected_generation=attempt.generation,
            result="rejected",
            vendor_code=1002,
            safe_to_failover=True,
        )
        assert report.kind is FinalizeKind.APPLIED
        state = await _snapshot(engine, safe_id)
        assert state["outcome"] == "rejected"
        assert state["chunk_status"] == "submitting"
        assert state["safe_to_failover"] is True
        next_attempt = await store.begin_vendor_invoke(
            safe_id, vendor_id="secondary", adapter_id="zhihui", reason="failover"
        )
        assert next_attempt.generation == attempt.generation + 1
        assert next_attempt.vendor_id == "secondary"

        batch_id, unsafe_id = await _seed_chunk(
            engine, app_id=app_id, nonce=nonce, index=3
        )
        batch_ids.append(batch_id)
        chunk_ids.append(unsafe_id)
        attempt = await store.begin_vendor_invoke(
            unsafe_id, vendor_id="zhihui", adapter_id="zhihui", reason="primary"
        )
        report = await store.finalize_vendor_attempt(
            attempt.id,
            unsafe_id,
            expected_generation=attempt.generation,
            result="rejected",
            vendor_code=1001,
            safe_to_failover=False,
        )
        assert report.kind is FinalizeKind.APPLIED
        state = await _snapshot(engine, unsafe_id)
        assert state["outcome"] == "rejected"
        assert state["chunk_status"] == "failed"
        assert state["message_status"] == "failed"
    finally:
        await _cleanup(engine, app_id=app_id, batch_ids=batch_ids, chunk_ids=chunk_ids)
        await engine.dispose()


@pytest.mark.asyncio
async def test_reconcile_repairs_or_isolates_submitted_invoking_on_postgres() -> None:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    nonce = uuid4().hex
    app_id: int | None = None
    batch_ids: list[int] = []
    chunk_ids: list[int] = []
    try:
        await _prepare_db(engine)
        app_id = await _insert_app(engine, nonce)
        store = _store(database_url)
        recovery = SqlRecoveryRepository(_settings(database_url))
        task_id = _crypto().stable_hmac_fingerprint(
            b"task-repair", domain="vendor-task-id"
        )[1]

        proven_batch, proven_id = await _seed_chunk(
            engine,
            app_id=app_id,
            nonce=nonce,
            index=1,
            status="submitted",
            vendor_task_id=task_id,
            submitted_at=True,
            message_status="sent",
        )
        batch_ids.append(proven_batch)
        chunk_ids.append(proven_id)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO sms_vendor_attempt(
                      chunk_id,vendor_id,generation,outcome,adapter_id,
                      invoke_started_at
                    ) VALUES (
                      :chunk_id,'zhihui',1,'invoking','zhihui',
                      now()-interval '20 minutes'
                    )
                    """
                ),
                {"chunk_id": proven_id},
            )
            await connection.execute(
                text("UPDATE sms_chunk SET route_generation=1 WHERE id=:id"),
                {"id": proven_id},
            )

        unproven_batch, unproven_id = await _seed_chunk(
            engine,
            app_id=app_id,
            nonce=nonce,
            index=2,
            status="submitted",
            submitted_at=True,
            message_status="sent",
        )
        batch_ids.append(unproven_batch)
        chunk_ids.append(unproven_id)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO sms_vendor_attempt(
                      chunk_id,vendor_id,generation,outcome,adapter_id,
                      invoke_started_at
                    ) VALUES (
                      :chunk_id,'zhihui',1,'invoking','zhihui',
                      now()-interval '20 minutes'
                    )
                    """
                ),
                {"chunk_id": unproven_id},
            )
            await connection.execute(
                text("UPDATE sms_chunk SET route_generation=1 WHERE id=:id"),
                {"id": unproven_id},
            )

        fresh_batch, fresh_id = await _seed_chunk(
            engine, app_id=app_id, nonce=nonce, index=3
        )
        batch_ids.append(fresh_batch)
        chunk_ids.append(fresh_id)
        fresh = await store.begin_vendor_invoke(
            fresh_id, vendor_id="zhihui", adapter_id="zhihui", reason="primary"
        )

        stale_batch, stale_submit_id = await _seed_chunk(
            engine, app_id=app_id, nonce=nonce, index=4
        )
        batch_ids.append(stale_batch)
        chunk_ids.append(stale_submit_id)
        stale = await store.begin_vendor_invoke(
            stale_submit_id, vendor_id="zhihui", adapter_id="zhihui", reason="primary"
        )
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE sms_vendor_attempt
                    SET invoke_started_at=now()-interval '16 minutes'
                    WHERE id=:id
                    """
                ),
                {"id": stale.id},
            )

        await recovery.stalled()

        proven = await _snapshot(engine, proven_id)
        assert proven["chunk_status"] == "submitted"
        assert proven["outcome"] == "submitted"

        unproven = await _snapshot(engine, unproven_id)
        assert unproven["chunk_status"] == "submitted"
        assert unproven["outcome"] == "inconsistent"
        with pytest.raises(RuntimeError, match="irreversible"):
            await store.begin_vendor_invoke(
                unproven_id, vendor_id="secondary", adapter_id="zhihui", reason="failover"
            )

        fresh_state = await _snapshot(engine, fresh_id)
        assert fresh_state["chunk_status"] == "submitting"
        assert fresh_state["outcome"] == "invoking"
        assert fresh_state["generation"] == fresh.generation

        stale_state = await _snapshot(engine, stale_submit_id)
        assert stale_state["chunk_status"] == "uncertain"
        assert stale_state["outcome"] == "uncertain"

        conflict = await store.finalize_vendor_attempt(
            fresh.id,
            fresh_id,
            expected_generation=fresh.generation,
            result="submitted",
            vendor_task_id="task-late",
        )
        assert conflict.kind is FinalizeKind.APPLIED
        late = await store.finalize_vendor_attempt(
            fresh.id,
            fresh_id,
            expected_generation=fresh.generation,
            result="submitted",
            vendor_task_id="task-late",
        )
        assert late.kind is FinalizeKind.ALREADY_FINALIZED_SAME_RESULT
        different = await store.finalize_vendor_attempt(
            fresh.id,
            fresh_id,
            expected_generation=fresh.generation,
            result="uncertain",
        )
        assert different.kind is FinalizeKind.FINALIZED_DIFFERENT_RESULT
        state = await _snapshot(engine, fresh_id)
        assert state["chunk_status"] == "submitted"
        assert state["outcome"] == "submitted"
    finally:
        await _cleanup(engine, app_id=app_id, batch_ids=batch_ids, chunk_ids=chunk_ids)
        await engine.dispose()


@pytest.mark.asyncio
async def test_two_finalizers_and_kill_before_commit_on_postgres(
    tmp_path: Path,
) -> None:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    engine = create_async_engine(database_url)
    nonce = uuid4().hex
    app_id: int | None = None
    batch_ids: list[int] = []
    chunk_ids: list[int] = []
    try:
        await _prepare_db(engine)
        app_id = await _insert_app(engine, nonce)
        store = _store(database_url)

        race_batch, race_id = await _seed_chunk(
            engine, app_id=app_id, nonce=nonce, index=1
        )
        batch_ids.append(race_batch)
        chunk_ids.append(race_id)
        race = await store.begin_vendor_invoke(
            race_id, vendor_id="zhihui", adapter_id="zhihui", reason="primary"
        )
        first, second = await asyncio.gather(
            store.finalize_vendor_attempt(
                race.id,
                race_id,
                expected_generation=race.generation,
                result="submitted",
                vendor_task_id="task-race",
            ),
            store.finalize_vendor_attempt(
                race.id,
                race_id,
                expected_generation=race.generation,
                result="submitted",
                vendor_task_id="task-race",
            ),
        )
        kinds = {first.kind, second.kind}
        assert FinalizeKind.APPLIED in kinds
        assert kinds - {FinalizeKind.APPLIED} <= {
            FinalizeKind.ALREADY_FINALIZED_SAME_RESULT
        }
        state = await _snapshot(engine, race_id)
        assert state["chunk_status"] == "submitted"
        assert state["outcome"] == "submitted"
        async with engine.connect() as connection:
            attempt_count = (
                await connection.execute(
                    text(
                        """
                        SELECT count(*) FROM sms_vendor_attempt
                        WHERE chunk_id=:id
                          AND outcome IN ('submitted','uncertain','invoking')
                        """
                    ),
                    {"id": race_id},
                )
            ).scalar_one()
        assert int(attempt_count) == 1

        kill_batch, kill_id = await _seed_chunk(
            engine, app_id=app_id, nonce=nonce, index=2
        )
        batch_ids.append(kill_batch)
        chunk_ids.append(kill_id)
        kill_attempt = await store.begin_vendor_invoke(
            kill_id, vendor_id="zhihui", adapter_id="zhihui", reason="primary"
        )
        ready = tmp_path / "finalize-ready"
        proc: asyncio.subprocess.Process | None = None
        async with engine.connect() as locker:
            trans = await locker.begin()
            await locker.execute(
                text("SELECT id FROM sms_vendor_attempt WHERE id=:id FOR UPDATE"),
                {"id": kill_attempt.id},
            )
            async with engine.connect() as probe:
                await probe.execute(text("SET lock_timeout = '200ms'"))
                with pytest.raises((DBAPIError, OperationalError)):
                    async with probe.begin():
                        await probe.execute(
                            text(
                                "SELECT id FROM sms_vendor_attempt"
                                " WHERE id=:id FOR UPDATE"
                            ),
                            {"id": kill_attempt.id},
                        )
            child_env = os.environ.copy()
            child_env.update(
                {
                    "SMS_FINALIZE_ATTEMPT_ID": str(kill_attempt.id),
                    "SMS_FINALIZE_CHUNK_ID": str(kill_id),
                    "SMS_FINALIZE_GENERATION": str(kill_attempt.generation),
                    "SMS_FINALIZE_READY": str(ready),
                }
            )
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-c",
                    (
                        "from tests.integration.test_vendor_attempt_finalize_postgres "
                        "import child_finalize; child_finalize()"
                    ),
                    cwd=str(_BACKEND_ROOT),
                    env=child_env,
                )
                for _ in range(200):
                    if ready.exists():
                        break
                    if proc.returncode is not None:
                        _stdout, stderr = await proc.communicate()
                        raise AssertionError(
                            f"finalize child exited early: {proc.returncode} {stderr!r}"
                        )
                    await asyncio.sleep(0.05)
                assert ready.exists()
                await asyncio.sleep(0.5)
                assert proc.returncode is None
                proc.send_signal(signal.SIGKILL)
                await proc.wait()
            finally:
                if proc is not None and proc.returncode is None:
                    proc.kill()
                    await proc.wait()
            await trans.rollback()

        killed = await _snapshot(engine, kill_id)
        assert killed["chunk_status"] == "submitting"
        assert killed["outcome"] == "invoking"
        assert killed["message_status"] == "pending"
    finally:
        await _cleanup(engine, app_id=app_id, batch_ids=batch_ids, chunk_ids=chunk_ids)
        await engine.dispose()
