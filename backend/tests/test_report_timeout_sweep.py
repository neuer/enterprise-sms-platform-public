from __future__ import annotations

import asyncio
import base64
import os
import subprocess
import time
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

import app.services.report_repository as report_repository_module
from app.core.jobtrack import JobTracker
from app.services.crypto import CryptoService
from app.services.report_ingest import ProtectedReport
from app.services.report_repository import SqlReportRepository
from app.services.report_timeout import ReportTimeoutService, ReportTimeoutStorageError

CREATED_AT = datetime(2026, 7, 15, 8, tzinfo=UTC)
MINIMAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS sys_config (
  key VARCHAR(64) PRIMARY KEY,
  value VARCHAR(512) NOT NULL,
  value_type VARCHAR(8) NOT NULL DEFAULT 'int'
);
CREATE TABLE IF NOT EXISTS sms_batch (
  id BIGSERIAL PRIMARY KEY,
  batch_no VARCHAR(32) NOT NULL UNIQUE,
  status VARCHAR(20) NOT NULL,
  delivered INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0,
  unknown_cnt INTEGER NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS sms_chunk (
  id BIGSERIAL PRIMARY KEY,
  batch_id BIGINT NOT NULL REFERENCES sms_batch(id),
  custom_id VARCHAR(32) NOT NULL,
  submitted_at TIMESTAMPTZ,
  status VARCHAR(32) NOT NULL DEFAULT 'submitted',
  selected_vendor VARCHAR(32) NOT NULL DEFAULT 'zhihui',
  late_evidence_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS sms_message (
  id BIGSERIAL,
  batch_id BIGINT NOT NULL,
  chunk_id BIGINT,
  phone_enc BYTEA NOT NULL,
  phone_hmac CHAR(64) NOT NULL,
  phone_mask VARCHAR(11) NOT NULL,
  key_version SMALLINT NOT NULL DEFAULT 1,
  status VARCHAR(10) NOT NULL,
  report_status SMALLINT,
  report_desc VARCHAR(128),
  report_time TIMESTAMPTZ,
  report_event_key CHAR(64),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (id, created_at)
);
CREATE TABLE IF NOT EXISTS stat_dirty_date (
  stat_date DATE PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS send_inflight_reservation (
  id BIGSERIAL PRIMARY KEY,
  batch_id BIGINT,
  generation INTEGER NOT NULL DEFAULT 1,
  state VARCHAR(32) NOT NULL
);
CREATE TABLE IF NOT EXISTS callback_task (
  id BIGSERIAL PRIMARY KEY,
  event VARCHAR(64) NOT NULL,
  batch_id BIGINT NOT NULL,
  source_report_event_key CHAR(64)
);
CREATE TABLE IF NOT EXISTS report_event (
  event_key CHAR(64) PRIMARY KEY,
  raw_id BIGINT,
  vendor_task_id VARCHAR(64) NOT NULL,
  custom_id VARCHAR(64) NOT NULL,
  phone_enc BYTEA NOT NULL,
  phone_hmac CHAR(64) NOT NULL,
  phone_mask VARCHAR(11) NOT NULL,
  key_version SMALLINT NOT NULL DEFAULT 1,
  report_status SMALLINT NOT NULL,
  message_status VARCHAR(10) NOT NULL,
  report_desc VARCHAR(128) NOT NULL DEFAULT '',
  report_time TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS report_event_projection (
  event_key CHAR(64) PRIMARY KEY,
  batch_id BIGINT NOT NULL,
  message_id BIGINT NOT NULL,
  message_created_at TIMESTAMPTZ NOT NULL,
  projection_changed BOOLEAN NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS sms_vendor_attempt (
  chunk_id BIGINT NOT NULL,
  vendor_id VARCHAR(32) NOT NULL,
  outcome VARCHAR(32) NOT NULL
);
"""


def _crypto() -> CryptoService:
    key = base64.b64encode(b"t" * 32).decode()
    return CryptoService.from_secret_values(key, key)


def _protected_phone() -> Any:
    return _crypto().protect_phone("13900001111")


class _SharedEngine:
    def __init__(self, engine: Any) -> None:
        self._engine = engine

    def begin(self) -> Any:
        return self._engine.begin()

    def connect(self) -> Any:
        return self._engine.connect()

    async def dispose(self, *_: object, **__: object) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._engine, name)


class EngineBoundRepository(SqlReportRepository):
    def __init__(self, engine: Any) -> None:
        super().__init__(cast(Any, SimpleNamespace(database_url="postgresql+asyncpg://timeout")))
        self._bound = _SharedEngine(engine)

    def _engine(self) -> Any:
        return self._bound


class MemoryJobRepo:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def start(self, job_name: str, started_at: datetime) -> int:
        self.rows.append({"job_name": job_name, "status": "running"})
        return len(self.rows)

    async def finish(
        self,
        run_id: int,
        *,
        finished_at: datetime,
        duration_ms: int,
        items: int,
        status: str,
        error: str | None,
    ) -> None:
        self.rows[run_id - 1].update({"status": status, "error": error, "items": items})

    async def latest(self, job_name: str) -> Any:
        return None

    async def consecutive_failures(self, job_name: str, *, limit: int) -> int:
        return 0


def _docker(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=check,
        text=True,
        capture_output=True,
    )


def _start_ephemeral_postgres() -> tuple[str, str]:
    name = f"sms-timeout-{uuid4().hex[:8]}"
    _docker(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "-e",
            "POSTGRES_HOST_AUTH_METHOD=trust",
            "-e",
            "POSTGRES_USER=sms",
            "-e",
            "POSTGRES_DB=sms",
            "-p",
            "127.0.0.1::5432",
            "postgres:16-alpine",
        ]
    )
    for _ in range(60):
        ready = _docker(
            ["docker", "exec", name, "pg_isready", "-U", "sms", "-d", "sms"],
            check=False,
        )
        if ready.returncode == 0:
            mapping = _docker(["docker", "port", name, "5432/tcp"]).stdout.strip()
            port = mapping.rsplit(":", 1)[1]
            return name, f"postgresql+asyncpg://sms@127.0.0.1:{port}/sms"
        time.sleep(1)
    _docker(["docker", "rm", "-f", name], check=False)
    raise RuntimeError("temporary PostgreSQL did not become ready")


@pytest.fixture(scope="session")
def timeout_pg_dsn() -> Iterator[str]:
    existing = os.environ.get("REPORT_TIMEOUT_POSTGRES_DSN")
    if existing:
        yield existing
        return
    name, dsn = _start_ephemeral_postgres()
    try:
        yield dsn
    finally:
        _docker(["docker", "rm", "-f", name], check=False)


@pytest_asyncio.fixture
async def timeout_env(
    timeout_pg_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[Any, EngineBoundRepository, ReportTimeoutService, str]]:
    url = make_url(timeout_pg_dsn)
    engine = create_async_engine(url, hide_parameters=True)
    nonce = uuid4().hex[:16]
    async with engine.begin() as connection:
        for statement in (item.strip() for item in MINIMAL_SCHEMA.split(";")):
            if statement:
                await connection.execute(text(statement))
    repository = EngineBoundRepository(engine)

    async def enqueue_finished(connection: Any, batch_id: int, **_: object) -> None:
        await connection.execute(
            text(
                """
                INSERT INTO callback_task(event, batch_id)
                SELECT 'batch.finished', :batch_id
                FROM sms_batch
                WHERE id=:batch_id
                  AND status IN (
                    'completed','completed_unknown','cancelled','expired','rejected'
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM callback_task
                    WHERE event='batch.finished' AND batch_id=:batch_id
                      AND source_report_event_key IS NULL
                  )
                """
            ),
            {"batch_id": batch_id},
        )

    async def enqueue_message(*_args: object, **_kwargs: object) -> None:
        return None

    async def release_inflight(connection: Any, *, batch_id: int, reason: str) -> bool:
        result = await connection.execute(
            text(
                """
                UPDATE send_inflight_reservation
                SET state='released'
                WHERE batch_id=:batch_id AND state <> 'released'
                RETURNING id
                """
            ),
            {"batch_id": batch_id},
        )
        return result.scalar_one_or_none() is not None

    monkeypatch.setattr(report_repository_module, "enqueue_batch_finished", enqueue_finished)
    monkeypatch.setattr(report_repository_module, "enqueue_message_report", enqueue_message)
    monkeypatch.setattr(
        "app.services.send_inflight.request_inflight_release_for_batch",
        release_inflight,
    )
    service = ReportTimeoutService(
        repository,
        settings=cast(Any, SimpleNamespace(database_url=url)),
    )
    try:
        yield engine, repository, service, nonce
    finally:
        async with engine.begin() as connection:
            owned = "SELECT id FROM sms_batch WHERE batch_no LIKE :p"
            await connection.execute(
                text(f"DELETE FROM callback_task WHERE batch_id IN ({owned})"),
                {"p": f"{nonce}%"},
            )
            await connection.execute(
                text(
                    "DELETE FROM send_inflight_reservation "
                    f"WHERE batch_id IN ({owned})"
                ),
                {"p": f"{nonce}%"},
            )
            await connection.execute(
                text(
                    "DELETE FROM report_event_projection "
                    f"WHERE batch_id IN ({owned})"
                ),
                {"p": f"{nonce}%"},
            )
            await connection.execute(
                text(f"DELETE FROM sms_message WHERE batch_id IN ({owned})"),
                {"p": f"{nonce}%"},
            )
            await connection.execute(
                text(f"DELETE FROM sms_chunk WHERE batch_id IN ({owned})"),
                {"p": f"{nonce}%"},
            )
            await connection.execute(
                text("DELETE FROM sms_batch WHERE batch_no LIKE :p"),
                {"p": f"{nonce}%"},
            )
        await engine.dispose()


async def _add_batch(
    engine: Any,
    *,
    nonce: str,
    index: int,
    statuses: list[str],
    due: bool = True,
    hours_offset: str | None = None,
    chunk_status: str = "submitted",
) -> int:
    phone = _protected_phone()
    batch_no = f"{nonce}{index:08d}"[:32]
    custom_id = f"{nonce}{index:08d}"[:32]
    async with engine.begin() as connection:
        batch_id = int(
            (
                await connection.execute(
                    text(
                        """
                        INSERT INTO sms_batch(batch_no,status)
                        VALUES (:batch_no,'sending')
                        RETURNING id
                        """
                    ),
                    {"batch_no": batch_no},
                )
            ).scalar_one()
        )
        if hours_offset is None:
            submitted_sql = (
                "now() - interval '100 hours'" if due else "now() - interval '1 hour'"
            )
        else:
            submitted_sql = f"now() - make_interval(hours=>48) {hours_offset}"
        chunk_id = int(
            (
                await connection.execute(
                    text(
                        f"""
                        INSERT INTO sms_chunk(
                          batch_id,custom_id,submitted_at,status
                        ) VALUES (
                          :batch_id,:custom_id,{submitted_sql},:status
                        ) RETURNING id
                        """
                    ),
                    {"batch_id": batch_id, "custom_id": custom_id, "status": chunk_status},
                )
            ).scalar_one()
        )
        for offset, status in enumerate(statuses):
            await connection.execute(
                text(
                    """
                    INSERT INTO sms_message(
                      batch_id,chunk_id,phone_enc,phone_hmac,phone_mask,
                      key_version,status,created_at
                    ) VALUES (
                      :batch_id,:chunk_id,:phone_enc,:phone_hmac,:phone_mask,
                      :key_version,:status,:created_at
                    )
                    """
                ),
                {
                    "batch_id": batch_id,
                    "chunk_id": chunk_id,
                    "phone_enc": phone.phone_enc,
                    "phone_hmac": phone.phone_hmac,
                    "phone_mask": phone.phone_mask,
                    "key_version": phone.key_version,
                    "status": status,
                    "created_at": CREATED_AT.replace(minute=offset),
                },
            )
        await connection.execute(
            text(
                """
                INSERT INTO send_inflight_reservation(batch_id,generation,state)
                VALUES (:batch_id,1,'materialized')
                """
            ),
            {"batch_id": batch_id},
        )
    return batch_id


async def _statuses(engine: Any, batch_id: int) -> list[str]:
    async with engine.connect() as connection:
        result = await connection.execute(
            text("SELECT status FROM sms_message WHERE batch_id=:id ORDER BY id"),
            {"id": batch_id},
        )
        return [str(value) for value in result.scalars()]


async def _batch_row(engine: Any, batch_id: int) -> dict[str, Any]:
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text("SELECT status,unknown_cnt FROM sms_batch WHERE id=:id"),
                {"id": batch_id},
            )
        ).mappings().one()
        reservation = (
            await connection.execute(
                text("SELECT state FROM send_inflight_reservation WHERE batch_id=:id"),
                {"id": batch_id},
            )
        ).scalar_one()
        callbacks = int(
            (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM callback_task "
                        "WHERE batch_id=:id AND event='batch.finished'"
                    ),
                    {"id": batch_id},
                )
            ).scalar_one()
        )
    return {
        "status": str(row["status"]),
        "unknown_cnt": int(row["unknown_cnt"]),
        "reservation": str(reservation),
        "callbacks": callbacks,
    }


def _bounds(**values: Any) -> dict[str, Any]:
    return {
        "timeout_hours": 48,
        "batch_limit": 10,
        "message_limit_per_batch": 20,
        "max_round_seconds": 10,
        "statement_timeout_ms": 5000,
        "lock_timeout_ms": 1000,
        **values,
    }


@pytest.mark.asyncio
async def test_report_poll_failure_does_not_block_timeout_task(
    timeout_env: tuple[Any, EngineBoundRepository, ReportTimeoutService, str],
) -> None:
    engine, _repository, service, nonce = timeout_env
    batch_id = await _add_batch(engine, nonce=nonce, index=1, statuses=["sent"])

    async def boom(*_args: object, **_kwargs: object) -> int:
        raise ConnectionError("vendor get_report failed")

    with pytest.raises(ConnectionError, match="vendor"):
        await boom()
    result = await service.expire_due_reports(**_bounds(batch_limit=2))
    assert result.messages_changed == 1
    assert await _statuses(engine, batch_id) == ["unknown"]


@pytest.mark.asyncio
async def test_timeout_runs_when_report_task_is_never_executed(
    timeout_env: tuple[Any, EngineBoundRepository, ReportTimeoutService, str],
) -> None:
    engine, _repository, service, nonce = timeout_env
    batch_id = await _add_batch(engine, nonce=nonce, index=2, statuses=["sent"])
    result = await service.expire_due_reports(**_bounds())
    assert result.messages_changed == 1
    assert await _statuses(engine, batch_id) == ["unknown"]


@pytest.mark.asyncio
async def test_timeout_uses_database_deadline_boundary(
    timeout_env: tuple[Any, EngineBoundRepository, ReportTimeoutService, str],
) -> None:
    engine, _repository, service, nonce = timeout_env
    due = await _add_batch(
        engine, nonce=nonce, index=3, statuses=["sent"], hours_offset="- interval '5 seconds'"
    )
    not_due = await _add_batch(
        engine, nonce=nonce, index=4, statuses=["sent"], hours_offset="+ interval '1 hour'"
    )
    await service.expire_due_reports(**_bounds())
    assert await _statuses(engine, due) == ["unknown"]
    assert await _statuses(engine, not_due) == ["sent"]


@pytest.mark.asyncio
async def test_timeout_preserves_delivered_failed_and_pending_messages(
    timeout_env: tuple[Any, EngineBoundRepository, ReportTimeoutService, str],
) -> None:
    engine, _repository, service, nonce = timeout_env
    mixed = await _add_batch(
        engine,
        nonce=nonce,
        index=5,
        statuses=["pending", "sent", "delivered", "failed"],
    )
    await service.expire_due_reports(**_bounds())
    assert await _statuses(engine, mixed) == ["pending", "unknown", "delivered", "failed"]


@pytest.mark.asyncio
async def test_timeout_finishes_batch_callback_and_inflight_atomically(
    timeout_env: tuple[Any, EngineBoundRepository, ReportTimeoutService, str],
) -> None:
    engine, _repository, service, nonce = timeout_env
    batch_id = await _add_batch(engine, nonce=nonce, index=6, statuses=["sent"])
    result = await service.expire_due_reports(**_bounds())
    assert result.batches_changed == 1
    row = await _batch_row(engine, batch_id)
    assert row == {
        "status": "completed",
        "unknown_cnt": 1,
        "reservation": "released",
        "callbacks": 1,
    }


@pytest.mark.asyncio
async def test_duplicate_timeout_tick_is_idempotent(
    timeout_env: tuple[Any, EngineBoundRepository, ReportTimeoutService, str],
) -> None:
    engine, _repository, service, nonce = timeout_env
    batch_id = await _add_batch(engine, nonce=nonce, index=7, statuses=["sent"])
    first = await service.expire_due_reports(**_bounds())
    second = await service.expire_due_reports(**_bounds())
    assert first.messages_changed == 1
    assert second.messages_changed == 0
    row = await _batch_row(engine, batch_id)
    assert row["callbacks"] == 1
    assert row["reservation"] == "released"


@pytest.mark.asyncio
async def test_late_report_after_timeout_preserves_existing_evidence_rules(
    timeout_env: tuple[Any, EngineBoundRepository, ReportTimeoutService, str],
) -> None:
    engine, repository, service, nonce = timeout_env
    batch_id = await _add_batch(
        engine,
        nonce=nonce,
        index=8,
        statuses=["sent"],
        chunk_status="unknown_terminal",
    )
    await service.expire_due_reports(**_bounds())
    async with engine.connect() as connection:
        message = (
            await connection.execute(
                text(
                    """
                    SELECT m.id,m.created_at,m.phone_hmac,c.custom_id
                    FROM sms_message m JOIN sms_chunk c ON c.id=m.chunk_id
                    WHERE m.batch_id=:id
                    """
                ),
                {"id": batch_id},
            )
        ).mappings().one()
    phone = _protected_phone()
    report = ProtectedReport(
        event_key="a" * 64,
        vendor_task_id="b" * 64,
        custom_id="c" * 64,
        match_custom_id=str(message["custom_id"]),
        phone_enc=phone.phone_enc,
        phone_hmac=phone.phone_hmac,
        phone_mask=phone.phone_mask,
        key_version=phone.key_version,
        report_status=1,
        message_status="delivered",
        report_desc="DELIVRD",
        report_time=datetime(2026, 7, 16, 8, tzinfo=UTC),
        phone_hmacs=(str(message["phone_hmac"]),),
    )
    applied = await repository.apply_report(1, report)
    assert applied is not None and applied.changed is True
    assert await _statuses(engine, batch_id) == ["delivered"]
    async with engine.connect() as connection:
        late = (
            await connection.execute(
                text("SELECT late_evidence_at FROM sms_chunk WHERE batch_id=:id"),
                {"id": batch_id},
            )
        ).scalar_one()
        batches = int(
            (
                await connection.execute(
                    text("SELECT count(*) FROM sms_batch WHERE batch_no LIKE :p"),
                    {"p": f"{nonce}%"},
                )
            ).scalar_one()
        )
    assert late is not None
    assert batches == 1


@pytest.mark.asyncio
async def test_timeout_database_failure_rolls_back_and_reports_failure(
    timeout_env: tuple[Any, EngineBoundRepository, ReportTimeoutService, str],
) -> None:
    engine, repository, service, nonce = timeout_env
    batch_id = await _add_batch(engine, nonce=nonce, index=9, statuses=["sent"])

    async def boom(_connection: Any, _batch_id: int) -> None:
        raise RuntimeError("storage write failed")

    repository.on_claimed_batch = boom
    jobs = MemoryJobRepo()
    with pytest.raises(ReportTimeoutStorageError):
        await JobTracker(jobs).run_async(
            "expire_report_timeouts",
            service.expire_due_reports,
            **_bounds(batch_limit=1),
        )
    assert await _statuses(engine, batch_id) == ["sent"]
    assert jobs.rows[-1]["status"] == "failed"
    row = await _batch_row(engine, batch_id)
    assert row["reservation"] == "materialized"
    assert row["callbacks"] == 0


@pytest.mark.asyncio
async def test_timeout_candidate_query_is_bounded(
    timeout_env: tuple[Any, EngineBoundRepository, ReportTimeoutService, str],
) -> None:
    engine, _repository, service, nonce = timeout_env
    ids = [
        await _add_batch(engine, nonce=nonce, index=10 + index, statuses=["sent"])
        for index in range(5)
    ]
    result = await service.expire_due_reports(**_bounds(batch_limit=2))
    assert result.candidates == 2
    assert result.batches_changed == 2
    assert result.more_remaining is True
    changed = 0
    for batch_id in ids:
        if (await _statuses(engine, batch_id)) == ["unknown"]:
            changed += 1
    assert changed == 2


@pytest.mark.asyncio
async def test_timeout_sweep_commits_between_batches(
    timeout_env: tuple[Any, EngineBoundRepository, ReportTimeoutService, str],
) -> None:
    engine, repository, service, nonce = timeout_env
    ids = [
        await _add_batch(engine, nonce=nonce, index=20 + index, statuses=["sent"])
        for index in range(3)
    ]
    seen: list[int] = []

    async def fail_third(_connection: Any, batch_id: int) -> None:
        seen.append(batch_id)
        if len(seen) == 3:
            raise RuntimeError("later batch failed")

    repository.on_claimed_batch = fail_third
    result = await service.expire_due_reports(**_bounds(batch_limit=3))
    assert result.batches_changed == 2
    assert result.failed == 1
    assert await _statuses(engine, ids[0]) == ["unknown"]
    assert await _statuses(engine, ids[1]) == ["unknown"]
    assert await _statuses(engine, ids[2]) == ["sent"]


@pytest.mark.asyncio
async def test_locked_batch_is_skipped_and_later_revisited(
    timeout_env: tuple[Any, EngineBoundRepository, ReportTimeoutService, str],
) -> None:
    engine, _repository, service, nonce = timeout_env
    first = await _add_batch(engine, nonce=nonce, index=30, statuses=["sent"])
    second = await _add_batch(engine, nonce=nonce, index=31, statuses=["sent"])
    locker = await engine.connect()
    trans = await locker.begin()
    await locker.execute(
        text("SELECT id FROM sms_batch WHERE id=:id FOR UPDATE"),
        {"id": first},
    )
    try:
        result = await service.expire_due_reports(**_bounds(batch_limit=2))
        assert result.skipped_locked >= 1 or result.batches_changed == 1
        assert await _statuses(engine, first) == ["sent"]
        assert await _statuses(engine, second) == ["unknown"]
    finally:
        await trans.rollback()
        await locker.close()
    later = await service.expire_due_reports(**_bounds(batch_limit=2))
    assert later.messages_changed == 1
    assert await _statuses(engine, first) == ["unknown"]


@pytest.mark.asyncio
async def test_failure_in_later_batch_preserves_earlier_commits(
    timeout_env: tuple[Any, EngineBoundRepository, ReportTimeoutService, str],
) -> None:
    engine, repository, service, nonce = timeout_env
    ids = [
        await _add_batch(engine, nonce=nonce, index=40 + index, statuses=["sent"])
        for index in range(3)
    ]
    seen: list[int] = []

    async def fail_second(_connection: Any, batch_id: int) -> None:
        seen.append(batch_id)
        if len(seen) == 2:
            raise RuntimeError("second batch storage")

    repository.on_claimed_batch = fail_second
    result = await service.expire_due_reports(**_bounds(batch_limit=3))
    assert result.failed == 1
    assert await _statuses(engine, ids[0]) == ["unknown"]
    assert await _statuses(engine, ids[1]) == ["sent"]
    assert await _statuses(engine, ids[2]) == ["unknown"]


@pytest.mark.asyncio
async def test_large_batch_is_processed_in_message_slices(
    timeout_env: tuple[Any, EngineBoundRepository, ReportTimeoutService, str],
) -> None:
    engine, _repository, service, nonce = timeout_env
    batch_id = await _add_batch(engine, nonce=nonce, index=50, statuses=["sent"] * 8)
    first = await service.expire_due_reports(
        **_bounds(batch_limit=1, message_limit_per_batch=3)
    )
    assert first.messages_changed == 3
    assert first.more_remaining is True
    assert (await _statuses(engine, batch_id)).count("unknown") == 3
    assert (await _statuses(engine, batch_id)).count("sent") == 5
    row = await _batch_row(engine, batch_id)
    assert row["status"] == "sending"
    assert row["reservation"] == "materialized"
    assert row["callbacks"] == 0


@pytest.mark.asyncio
async def test_partial_batch_does_not_finish_or_release_inflight(
    timeout_env: tuple[Any, EngineBoundRepository, ReportTimeoutService, str],
) -> None:
    engine, _repository, service, nonce = timeout_env
    batch_id = await _add_batch(engine, nonce=nonce, index=51, statuses=["sent"] * 8)
    await service.expire_due_reports(**_bounds(batch_limit=1, message_limit_per_batch=3))
    row = await _batch_row(engine, batch_id)
    assert row["status"] != "completed"
    assert row["reservation"] == "materialized"
    assert row["callbacks"] == 0


@pytest.mark.asyncio
async def test_repeated_sweep_eventually_drains_due_work(
    timeout_env: tuple[Any, EngineBoundRepository, ReportTimeoutService, str],
) -> None:
    engine, _repository, service, nonce = timeout_env
    ids = [
        await _add_batch(engine, nonce=nonce, index=60 + index, statuses=["sent"])
        for index in range(5)
    ]
    large = await _add_batch(engine, nonce=nonce, index=70, statuses=["sent"] * 8)
    remaining = True
    rounds = 0
    while remaining and rounds < 20:
        result = await service.expire_due_reports(
            **_bounds(batch_limit=2, message_limit_per_batch=3)
        )
        remaining = result.more_remaining
        rounds += 1
    assert remaining is False
    for batch_id in ids:
        assert await _statuses(engine, batch_id) == ["unknown"]
    assert await _statuses(engine, large) == ["unknown"] * 8
    assert (await _batch_row(engine, large))["status"] == "completed"


@pytest.mark.asyncio
async def test_poison_batch_does_not_starve_other_candidates(
    timeout_env: tuple[Any, EngineBoundRepository, ReportTimeoutService, str],
) -> None:
    engine, repository, service, nonce = timeout_env
    ids = [
        await _add_batch(engine, nonce=nonce, index=80 + index, statuses=["sent"])
        for index in range(5)
    ]
    poison = ids[0]

    async def fail_poison(_connection: Any, batch_id: int) -> None:
        if batch_id == poison:
            raise RuntimeError("poison batch")

    repository.on_claimed_batch = fail_poison
    result = await service.expire_due_reports(**_bounds(batch_limit=2))
    assert result.failed == 1
    assert result.batches_changed == 1
    assert await _statuses(engine, poison) == ["sent"]
    changed = 0
    for batch_id in ids[1:]:
        if await _statuses(engine, batch_id) == ["unknown"]:
            changed += 1
    assert changed == 1


@pytest.mark.asyncio
async def test_parallel_sweep_and_report_apply_preserve_final_status(
    timeout_env: tuple[Any, EngineBoundRepository, ReportTimeoutService, str],
) -> None:
    engine, repository, service, nonce = timeout_env
    batch_id = await _add_batch(engine, nonce=nonce, index=90, statuses=["sent"])
    async with engine.connect() as connection:
        message = (
            await connection.execute(
                text(
                    """
                    SELECT m.id,m.created_at,m.phone_hmac,c.custom_id
                    FROM sms_message m JOIN sms_chunk c ON c.id=m.chunk_id
                    WHERE m.batch_id=:id
                    """
                ),
                {"id": batch_id},
            )
        ).mappings().one()
    phone = _protected_phone()
    report = ProtectedReport(
        event_key="d" * 64,
        vendor_task_id="e" * 64,
        custom_id="f" * 64,
        match_custom_id=str(message["custom_id"]),
        phone_enc=phone.phone_enc,
        phone_hmac=phone.phone_hmac,
        phone_mask=phone.phone_mask,
        key_version=phone.key_version,
        report_status=1,
        message_status="delivered",
        report_desc="DELIVRD",
        report_time=datetime(2026, 7, 16, 9, tzinfo=UTC),
        phone_hmacs=(str(message["phone_hmac"]),),
    )

    await asyncio.gather(
        service.expire_due_reports(**_bounds(batch_limit=1)),
        repository.apply_report(2, report),
    )
    status = (await _statuses(engine, batch_id))[0]
    assert status == "delivered"


@pytest.mark.asyncio
async def test_duplicate_or_ambiguous_commit_does_not_double_release(
    timeout_env: tuple[Any, EngineBoundRepository, ReportTimeoutService, str],
) -> None:
    engine, _repository, service, nonce = timeout_env
    batch_id = await _add_batch(engine, nonce=nonce, index=91, statuses=["sent"])
    await asyncio.gather(
        service.expire_due_reports(**_bounds(batch_limit=1)),
        service.expire_due_reports(**_bounds(batch_limit=1)),
    )
    row = await _batch_row(engine, batch_id)
    assert row["reservation"] == "released"
    assert row["callbacks"] == 1
    assert await _statuses(engine, batch_id) == ["unknown"]


@pytest.mark.asyncio
async def test_round_budget_and_sql_timeout_stop_work_safely(
    timeout_env: tuple[Any, EngineBoundRepository, ReportTimeoutService, str],
) -> None:
    engine, repository, service, nonce = timeout_env
    ids = [
        await _add_batch(engine, nonce=nonce, index=100 + index, statuses=["sent"])
        for index in range(4)
    ]
    seen: list[int] = []

    async def sleep_later(connection: Any, batch_id: int) -> None:
        seen.append(batch_id)
        if len(seen) == 2:
            await connection.execute(text("SELECT pg_sleep(1)"))

    repository.on_claimed_batch = sleep_later
    result = await service.expire_due_reports(
        **_bounds(
            batch_limit=4,
            max_round_seconds=0.2,
            statement_timeout_ms=100,
            lock_timeout_ms=100,
        )
    )
    assert result.more_remaining is True
    assert await _statuses(engine, ids[0]) == ["unknown"]
    assert result.batches_changed >= 1
    remaining_sent = 0
    for batch_id in ids:
        remaining_sent += (await _statuses(engine, batch_id)).count("sent")
    assert remaining_sent >= 1
