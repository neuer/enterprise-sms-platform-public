"""#475：raw 处理租约 takeover 与迟到写入 fencing。"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.services.ops_repository import SqlOpsRepository
from app.services.raw_lease import RawLeaseLost, RawProcessingLease
from app.services.report_repository import SqlReportRepository

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


def _settings(url: object) -> Any:
    return cast(Any, SimpleNamespace(database_url=url, database_url_for=lambda _role: url))


@pytest.mark.asyncio
async def test_expired_owner_cannot_overwrite_takeover_terminal_state() -> None:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    settings = _settings(database_url)
    engine = create_async_engine(database_url)
    reports = SqlReportRepository(settings)
    ops = SqlOpsRepository(settings, redis=object())
    raw_id: int | None = None
    try:
        raw_id = await reports.persist_raw(
            payload_enc=b"ciphertext-only",
            payload_sha256="a" * 64,
            key_version=1,
            http_status=200,
            content_encoding="identity",
            custom_ids=[],
            item_count=0,
        )
        lease_a = reports._leases[raw_id]
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE raw_vendor_log
                    SET processing_lease_expires_at=now()-interval '1 second',
                        replay_eligibility='automatic',
                        parse_state='unattempted',
                        processed=false
                    WHERE id=:raw_id
                    """
                ),
                {"raw_id": raw_id},
            )
        claim_b = await ops.claim_raw_for_replay(raw_id, allow_manual=False)
        assert claim_b is not None and claim_b.claimed is True
        assert claim_b.lease is not None
        assert claim_b.lease.epoch == lease_a.epoch + 1
        assert claim_b.lease.lease_id != lease_a.lease_id

        reports.remember_lease(claim_b.lease)
        await reports.mark_processed(raw_id, lease=claim_b.lease)

        with pytest.raises(RawLeaseLost):
            await reports.mark_error(raw_id, "late owner error", lease=lease_a)

        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT processed,parse_state,replay_eligibility,
                          processing_lease_id,processing_lease_epoch
                        FROM raw_vendor_log WHERE id=:raw_id
                        """
                    ),
                    {"raw_id": raw_id},
                )
            ).mappings().one()
            events = (
                await connection.execute(
                    text(
                        """
                        SELECT event_type,task_kind FROM worker_lease_event
                        WHERE task_kind='raw' AND task_id=:raw_id
                        ORDER BY id
                        """
                    ),
                    {"raw_id": raw_id},
                )
            ).mappings().all()
        assert bool(row["processed"]) is True
        assert str(row["parse_state"]) == "processed"
        assert str(row["replay_eligibility"]) == "never"
        assert row["processing_lease_id"] is None
        assert int(row["processing_lease_epoch"]) == claim_b.lease.epoch
        assert [
            (str(item["event_type"]), str(item["task_kind"])) for item in events
        ] == [("fencing_miss", "raw")]
        assert "phone" not in str(events).casefold()
    finally:
        if raw_id is not None:
            async with engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM worker_lease_event WHERE task_kind='raw' AND task_id=:id"),
                    {"id": raw_id},
                )
                await connection.execute(
                    text("DELETE FROM raw_vendor_log WHERE id=:id"),
                    {"id": raw_id},
                )
        await engine.dispose()


@pytest.mark.asyncio
async def test_late_success_after_failed_takeover_only_allows_current_owner() -> None:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    settings = _settings(database_url)
    engine = create_async_engine(database_url)
    reports = SqlReportRepository(settings)
    ops = SqlOpsRepository(settings, redis=object())
    raw_id: int | None = None
    try:
        raw_id = await reports.persist_raw(
            payload_enc=b"ciphertext-only",
            payload_sha256="b" * 64,
            key_version=1,
            http_status=200,
            content_encoding="identity",
            custom_ids=[],
            item_count=0,
        )
        lease_a = reports._leases[raw_id]
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE raw_vendor_log
                    SET processing_lease_expires_at=now()-interval '1 second',
                        replay_eligibility='automatic',
                        processed=false
                    WHERE id=:raw_id
                    """
                ),
                {"raw_id": raw_id},
            )
        claim_b = await ops.claim_raw_for_replay(raw_id, allow_manual=False)
        assert claim_b is not None and claim_b.lease is not None
        await reports.mark_error(raw_id, "takeover transient", lease=claim_b.lease)

        with pytest.raises(RawLeaseLost):
            await reports.mark_processed(raw_id, lease=lease_a)

        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT processed,parse_state,replay_eligibility,error
                        FROM raw_vendor_log WHERE id=:raw_id
                        """
                    ),
                    {"raw_id": raw_id},
                )
            ).mappings().one()
        assert bool(row["processed"]) is False
        assert str(row["parse_state"]) != "processed"
        assert "takeover transient" in str(row["error"])
    finally:
        if raw_id is not None:
            async with engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM worker_lease_event WHERE task_kind='raw' AND task_id=:id"),
                    {"id": raw_id},
                )
                await connection.execute(
                    text("DELETE FROM raw_vendor_log WHERE id=:id"),
                    {"id": raw_id},
                )
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_claims_produce_one_winner_and_check_rejects_illegal_combo() -> None:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    settings = _settings(database_url)
    engine = create_async_engine(database_url)
    reports = SqlReportRepository(settings)
    ops = SqlOpsRepository(settings, redis=object())
    raw_id: int | None = None
    try:
        raw_id = await reports.persist_raw(
            payload_enc=b"ciphertext-only",
            payload_sha256="c" * 64,
            key_version=1,
            http_status=200,
            content_encoding="identity",
            custom_ids=[],
            item_count=0,
        )
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE raw_vendor_log
                    SET processing_lease_id=NULL,
                        processing_lease_expires_at=NULL,
                        replay_eligibility='automatic',
                        processed=false
                    WHERE id=:raw_id
                    """
                ),
                {"raw_id": raw_id},
            )
        first, second = await asyncio.gather(
            ops.claim_raw_for_replay(raw_id, allow_manual=False),
            ops.claim_raw_for_replay(raw_id, allow_manual=False),
        )
        winners = [item for item in (first, second) if item is not None and item.claimed]
        assert len(winners) == 1 and winners[0].lease is not None
        first = winners[0]

        async with engine.begin() as connection:
            with pytest.raises(IntegrityError):
                await connection.execute(
                    text(
                        """
                        UPDATE raw_vendor_log
                        SET processed=false,parse_state='processed'
                        WHERE id=:raw_id
                        """
                    ),
                    {"raw_id": raw_id},
                )
        stale = RawProcessingLease(raw_id, UUID(int=1), first.lease.epoch)
        with pytest.raises(RawLeaseLost):
            await reports.update_metadata(
                raw_id, custom_ids=[], item_count=0, lease=stale
            )
    finally:
        if raw_id is not None:
            async with engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM worker_lease_event WHERE task_kind='raw' AND task_id=:id"),
                    {"id": raw_id},
                )
                await connection.execute(
                    text("DELETE FROM raw_vendor_log WHERE id=:id"),
                    {"id": raw_id},
                )
        await engine.dispose()
