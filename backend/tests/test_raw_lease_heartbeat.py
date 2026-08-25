from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy.exc import DBAPIError

from app.services.crypto import CryptoService
from app.services.raw_lease import (
    RawLeaseHeartbeat,
    RawLeaseHeartbeatFailed,
    RawLeaseLost,
    RawProcessingLease,
    bind_raw_lease_heartbeat,
)
from app.services.reply_ingest import ReplyIngestService
from app.services.report_ingest import ReportApplyResult, ReportIngestService


def _crypto() -> CryptoService:
    import base64

    key = base64.b64encode(b"r" * 32).decode()
    return CryptoService.from_secret_values(key, key)


def _report_item() -> dict[str, object]:
    return {
        "taskId": "task-1",
        "customId": "custom1",
        "phone": "13800138000",
        "reportStatus": 1,
        "reportDescription": "DELIVRD",
        "reportTime": "2026-07-11T08:00:00Z",
    }


def _reply_item() -> dict[str, object]:
    return {
        "taskId": "task-1",
        "customId ": "custom1",
        "phone": "13800138000",
        "extCode": "01",
        "contents": "TD",
        "replyTime": "2026-07-12T08:00:00+08:00",
    }


def _lease(*, expired: bool = False) -> RawProcessingLease:
    expiry = datetime.now(UTC) + timedelta(seconds=-1 if expired else 60)
    return RawProcessingLease(12, UUID(int=12), 1, expires_at=expiry)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        DBAPIError("renew", None, Exception("dbapi")),
        TimeoutError("pool timeout"),
        ConnectionError("connection reset"),
    ],
)
async def test_raise_if_lost_fails_after_renewal_errors(error: Exception) -> None:
    async def renew(_lease: RawProcessingLease) -> None:
        raise error

    async with RawLeaseHeartbeat(renew, _lease(expired=True), interval_s=0.01) as beat:
        await asyncio.sleep(0.08)
        with pytest.raises(RawLeaseHeartbeatFailed):
            beat.raise_if_lost()
        assert beat._lost is not None


@pytest.mark.asyncio
async def test_cancelled_error_is_not_classified_as_lease_or_db_failure() -> None:
    async def renew(_lease: RawProcessingLease) -> None:
        raise asyncio.CancelledError()

    async with RawLeaseHeartbeat(renew, _lease(), interval_s=0.01) as beat:
        await asyncio.sleep(0.05)
        beat.raise_if_lost()
        assert beat._lost is None
        assert beat._task is not None
        assert beat._task.cancelled() or beat._task.done()


@pytest.mark.asyncio
async def test_bounded_retry_stops_at_original_lease_expiry() -> None:
    calls = 0

    async def renew(_lease: RawProcessingLease) -> None:
        nonlocal calls
        calls += 1
        raise TimeoutError("slow db")

    async with RawLeaseHeartbeat(renew, _lease(expired=True), interval_s=0.01) as beat:
        await asyncio.sleep(0.05)
        with pytest.raises(RawLeaseHeartbeatFailed, match="expired|failed"):
            beat.raise_if_lost()
    assert calls == 1


@pytest.mark.asyncio
async def test_successful_mark_processed_ignores_late_heartbeat_error() -> None:
    started = asyncio.Event()

    async def renew(_lease: RawProcessingLease) -> None:
        started.set()
        raise ConnectionError("late disconnect")

    async with RawLeaseHeartbeat(renew, _lease(), interval_s=0.01):
        await started.wait()
        await asyncio.sleep(0.02)
    # 退出时正文成功，迟到异常不得再抛出。


@pytest.mark.asyncio
async def test_heartbeat_logs_and_events_have_no_phone_or_ciphertext(
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[tuple[int, str]] = []

    async def renew(_lease: RawProcessingLease) -> None:
        raise DBAPIError("renew", None, Exception("dbapi"))

    async def on_failure(lease: RawProcessingLease, _error: RawLeaseLost) -> None:
        events.append((lease.raw_id, str(lease.lease_id)))

    caplog.set_level(logging.WARNING)
    async with RawLeaseHeartbeat(
        renew, _lease(expired=True), interval_s=0.01, on_failure=on_failure
    ) as beat:
        await asyncio.sleep(0.08)
        with pytest.raises(RawLeaseHeartbeatFailed):
            beat.raise_if_lost()
    text = " ".join(record.getMessage() for record in caplog.records)
    extras = " ".join(str(getattr(record, "raw_id", "")) for record in caplog.records)
    assert "138" not in text
    assert "ciphertext" not in text.lower()
    assert "payload" not in extras
    assert events == [(12, str(UUID(int=12)))]


@asynccontextmanager
async def _fast_bind(repository: object, lease: RawProcessingLease | None, **_: object):
    async with bind_raw_lease_heartbeat(repository, lease, interval_s=0.01) as beat:
        yield beat


@pytest.mark.asyncio
async def test_report_item_writes_stop_after_dbapi_heartbeat_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.report_ingest as report_ingest_module

    monkeypatch.setattr(report_ingest_module, "bind_raw_lease_heartbeat", _fast_bind)

    class SlowRepo:
        def __init__(self) -> None:
            self.events: list[tuple[str, object]] = []
            self._leases: dict[int, object] = {}

        def remember_lease(self, lease: RawProcessingLease) -> None:
            self._leases[lease.raw_id] = lease

        async def renew_processing_lease(self, _lease: RawProcessingLease) -> None:
            raise DBAPIError("renew", None, Exception("dbapi"))

        async def filter_known_custom_ids(self, custom_ids: list[str]) -> list[str]:
            return [value for value in custom_ids if value == "custom1"]

        async def update_metadata(self, raw_id: int, **_: object) -> None:
            self.events.append(("metadata", raw_id))

        async def apply_report(self, raw_id: int, report_item: object) -> object:
            await asyncio.sleep(0.03)
            self.events.append(("apply", report_item))
            return ReportApplyResult(8, True)

        async def persist_unmatched(self, raw_id: int, report_item: object) -> None:
            self.events.append(("unmatched", report_item))

        async def mark_processed(self, raw_id: int, **_: object) -> None:
            self.events.append(("processed", raw_id))

        async def mark_error(self, raw_id: int, error: str, **_: object) -> None:
            self.events.append(("error", error))

    repository = SlowRepo()
    items = [_report_item() for _ in range(8)]
    with pytest.raises(RawLeaseLost):
        await ReportIngestService(None, repository, _crypto()).process_existing(
            12,
            items,
            lease=_lease(expired=True),
        )
    names = [event[0] for event in repository.events]
    assert names.count("apply") < 8
    assert "processed" not in names
    assert "error" not in names


@pytest.mark.asyncio
async def test_reply_item_writes_stop_after_timeout_heartbeat_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.reply_ingest as reply_ingest_module

    monkeypatch.setattr(reply_ingest_module, "bind_raw_lease_heartbeat", _fast_bind)

    class SlowRepo:
        def __init__(self) -> None:
            self.events: list[tuple[str, object]] = []
            self._leases: dict[int, object] = {}

        def remember_lease(self, lease: RawProcessingLease) -> None:
            self._leases[lease.raw_id] = lease

        async def renew_processing_lease(self, _lease: RawProcessingLease) -> None:
            raise TimeoutError("pool timeout")

        async def filter_known_custom_ids(self, custom_ids: list[str]) -> list[str]:
            return [value for value in custom_ids if value == "custom1"]

        async def update_metadata(self, raw_id: int, **_: object) -> None:
            self.events.append(("metadata", raw_id))

        async def store_reply(self, raw_id: int, item: object) -> None:
            await asyncio.sleep(0.03)
            self.events.append(("store", item))

        async def mark_processed(self, raw_id: int, **_: object) -> None:
            self.events.append(("processed", raw_id))

        async def mark_error(self, raw_id: int, error: str, **_: object) -> None:
            self.events.append(("error", error))

    repository = SlowRepo()
    items = [_reply_item() for _ in range(8)]
    with pytest.raises(RawLeaseLost):
        await ReplyIngestService(None, repository, _crypto()).process_existing(
            23,
            items,
            lease=RawProcessingLease(23, UUID(int=23), 1, datetime.now(UTC) - timedelta(seconds=1)),
        )
    names = [event[0] for event in repository.events]
    assert names.count("store") < 8
    assert "processed" not in names
    assert "error" not in names


@pytest.mark.asyncio
async def test_timeout_after_successful_renews_retries_within_latest_expiry() -> None:
    original = datetime.now(UTC) - timedelta(minutes=16)
    latest = datetime.now(UTC) + timedelta(seconds=5)
    calls = 0

    async def renew(_lease: RawProcessingLease) -> datetime:
        nonlocal calls
        calls += 1
        if calls <= 2:
            return latest
        raise TimeoutError("brief")

    async with RawLeaseHeartbeat(
        renew,
        RawProcessingLease(12, UUID(int=12), 1, expires_at=original),
        interval_s=0.02,
    ) as beat:
        await asyncio.sleep(0.4)
        assert beat._confirmed_expires_at == latest
        if beat._lost is not None:
            with pytest.raises(RawLeaseHeartbeatFailed, match="failed"):
                beat.raise_if_lost()
            assert "expired" not in str(beat._lost)
        else:
            beat.raise_if_lost()
    assert calls > 3


@pytest.mark.asyncio
async def test_confirmed_expiry_only_updates_on_datetime_returning() -> None:
    original = datetime.now(UTC) + timedelta(seconds=30)
    lease = RawProcessingLease(12, UUID(int=12), 1, expires_at=original)

    async def renew(_lease: RawProcessingLease) -> None:
        return None

    beat = RawLeaseHeartbeat(renew, lease, interval_s=0.01)
    await beat._renew_until_confirmed()
    assert beat._confirmed_expires_at == original


@pytest.mark.asyncio
async def test_wrong_owner_does_not_update_confirmed_expiry() -> None:
    original = datetime.now(UTC) + timedelta(seconds=30)
    lease = RawProcessingLease(12, UUID(int=12), 1, expires_at=original)

    async def renew(_lease: RawProcessingLease) -> datetime:
        raise RawLeaseLost("raw processing lease heartbeat lost")

    beat = RawLeaseHeartbeat(renew, lease, interval_s=0.01)
    with pytest.raises(RawLeaseLost):
        await beat._renew_until_confirmed()
    assert beat._confirmed_expires_at == original


@pytest.mark.asyncio
async def test_latest_confirmed_expiry_already_past_fails_immediately() -> None:
    calls = 0

    async def renew(_lease: RawProcessingLease) -> datetime:
        nonlocal calls
        calls += 1
        raise TimeoutError("late")

    async with RawLeaseHeartbeat(renew, _lease(expired=True), interval_s=0.01) as beat:
        await asyncio.sleep(0.05)
        with pytest.raises(RawLeaseHeartbeatFailed, match="expired|failed"):
            beat.raise_if_lost()
        assert beat._failure_class(beat._lost) == "local_expiry_stale"  # type: ignore[arg-type]
    assert calls == 1


@pytest.mark.asyncio
async def test_recover_after_brief_timeout_still_writes_one_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.report_ingest as report_ingest_module

    monkeypatch.setattr(report_ingest_module, "bind_raw_lease_heartbeat", _fast_bind)
    latest = datetime.now(UTC) + timedelta(seconds=5)
    calls = 0

    class RecoveringRepo:
        def __init__(self) -> None:
            self.events: list[tuple[str, object]] = []
            self._leases: dict[int, object] = {}

        def remember_lease(self, lease: RawProcessingLease) -> None:
            self._leases[lease.raw_id] = lease

        async def renew_processing_lease(self, _lease: RawProcessingLease) -> datetime:
            nonlocal calls
            calls += 1
            if calls == 3:
                raise TimeoutError("brief")
            return latest

        async def filter_known_custom_ids(self, custom_ids: list[str]) -> list[str]:
            return custom_ids

        async def update_metadata(self, raw_id: int, **_: object) -> None:
            self.events.append(("metadata", raw_id))

        async def apply_report(self, raw_id: int, report_item: object) -> object:
            await asyncio.sleep(0.08)
            self.events.append(("apply", report_item))
            return ReportApplyResult(1, True)

        async def persist_unmatched(self, raw_id: int, report_item: object) -> None:
            self.events.append(("unmatched", report_item))

        async def mark_processed(self, raw_id: int, **_: object) -> None:
            self.events.append(("processed", raw_id))

        async def mark_error(self, raw_id: int, error: str, **_: object) -> None:
            self.events.append(("error", error))

    repository = RecoveringRepo()
    await ReportIngestService(None, repository, _crypto()).process_existing(
        12,
        [_report_item()],
        lease=_lease(),
    )
    names = [event[0] for event in repository.events]
    assert names.count("processed") == 1
    assert "error" not in names


@pytest.mark.asyncio
async def test_stale_expiry_logs_have_no_phone_or_ciphertext(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def renew(_lease: RawProcessingLease) -> datetime:
        raise TimeoutError("late")

    caplog.set_level(logging.WARNING)
    async with RawLeaseHeartbeat(renew, _lease(expired=True), interval_s=0.01) as beat:
        await asyncio.sleep(0.05)
        with pytest.raises(RawLeaseHeartbeatFailed):
            beat.raise_if_lost()
    text = " ".join(record.getMessage() for record in caplog.records)
    extras = " ".join(
        f"{getattr(record, 'failure_class', '')}" for record in caplog.records
    )
    assert "138" not in text
    assert "ciphertext" not in text.lower()
    assert "local_expiry_stale" in extras
