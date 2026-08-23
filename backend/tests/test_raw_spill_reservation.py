from __future__ import annotations

import asyncio
import base64
import errno
import json
import subprocess
import sys
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.services.crypto import CryptoService
from app.services.raw_spill import (
    CAPTURE_COMPLETE_TOO_LARGE,
    CAPTURE_FRAME_OVERHEAD_BYTES,
    CAPTURE_TRUNCATED,
    CONTROL_FRAME_BUDGET_BYTES,
    CONTROL_FRAME_COUNT,
    DATA_FRAME_OVERHEAD_BYTES,
    DIRECTORY_METADATA_BYTES,
    INTERNAL_FRAME_SIZE,
    RECOVERY_CAPTURE_BYTES,
    STREAM_HEADER_BUDGET_BYTES,
    RawSpillStore,
    SpillQuotaExceeded,
    capture_reservation_bytes,
    max_internal_frames,
)
from app.services.reply_ingest import (
    ReplyIngestService,
)
from app.services.reply_ingest import (
    _discard_unused_stream as discard_unused_reply,
)
from app.services.report_ingest import (
    ReportIngestService,
)
from app.services.report_ingest import (
    _discard_unused_stream as discard_unused_report,
)
from app.settings import (
    RAW_SPILL_CAPTURE_OVERHEAD_BYTES,
    RAW_SPILL_CONTROL_FRAME_BUDGET_BYTES,
    RAW_SPILL_CONTROL_FRAME_COUNT,
    RAW_SPILL_DATA_FRAME_OVERHEAD_BYTES,
    RAW_SPILL_DIRECTORY_METADATA_BYTES,
    RAW_SPILL_INTERNAL_FRAME_SIZE,
    RAW_SPILL_MAX_INTERNAL_FRAMES,
    RAW_SPILL_MIN_TOTAL_BYTES,
    RAW_SPILL_RECOVERY_CAPTURE_BYTES,
    RAW_SPILL_STREAM_HEADER_BUDGET_BYTES,
)
from app.vendor.zhihui import RawPulledPayload, ZhihuiClient


def leftover_names(directory: Path, *suffixes: str) -> list[str]:
    return [
        path.name
        for path in directory.iterdir()
        if any(path.name.endswith(suffix) for suffix in suffixes)
    ]


def crypto() -> CryptoService:
    key = base64.b64encode(b"r" * 32).decode()
    return CryptoService.from_secret_values(key, key)


def sparse_file(path: Path, size: int) -> None:
    with path.open("wb") as handle:
        handle.truncate(size)


class FakeRepository:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    async def persist_raw(self, **values: Any) -> int:
        self.events.append(("persist_raw", values))
        return 12

    async def update_metadata(self, raw_id: int, *, custom_ids: list[str], item_count: int) -> None:
        self.events.append(("metadata", (raw_id, custom_ids, item_count)))

    async def filter_known_custom_ids(self, custom_ids: list[str]) -> list[str]:
        return custom_ids

    async def apply_report(self, raw_id: int, report: Any) -> None:
        self.events.append(("apply", report))

    async def failure_rate_candidate(self, batch_id: int) -> None:
        return None

    async def persist_unmatched(self, raw_id: int, report: Any) -> None:
        self.events.append(("unmatched", report))

    async def store_reply(self, raw_id: int, reply: Any) -> None:
        self.events.append(("store", reply))

    async def mark_processed(self, raw_id: int) -> None:
        self.events.append(("processed", raw_id))

    async def mark_error(self, raw_id: int, error: str) -> None:
        self.events.append(("error", error))


class FakeAlerts:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, **values: Any) -> None:
        self.events.append(values)


class RecordingGateway:
    def __init__(self, result: RawPulledPayload) -> None:
        self.result = result
        self.calls = 0

    async def get_report_raw(self, body_sink: Any | None = None) -> RawPulledPayload:
        self.calls += 1
        return _feed_sink(body_sink, self.result)

    async def get_reply_raw(self, body_sink: Any | None = None) -> RawPulledPayload:
        self.calls += 1
        return _feed_sink(body_sink, self.result)


class HoldGateway:
    def __init__(
        self,
        result: RawPulledPayload,
        started: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        self.result = result
        self.started = started
        self.release = release
        self.calls = 0

    async def get_report_raw(self, body_sink: Any | None = None) -> RawPulledPayload:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return _feed_sink(body_sink, self.result)

    async def get_reply_raw(self, body_sink: Any | None = None) -> RawPulledPayload:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return _feed_sink(body_sink, self.result)


class BoomGateway:
    def __init__(self) -> None:
        self.calls = 0

    async def get_report_raw(self, body_sink: Any | None = None) -> RawPulledPayload:
        self.calls += 1
        raise RuntimeError("vendor transport failed")


class AnnounceThenBoomGateway:
    def __init__(self) -> None:
        self.calls = 0

    async def get_report_raw(self, body_sink: Any | None = None) -> RawPulledPayload:
        self.calls += 1
        if body_sink is not None:
            body_sink.announce(http_status=502, content_encoding="identity")
        raise RuntimeError("body unread after announce")

    async def get_reply_raw(self, body_sink: Any | None = None) -> RawPulledPayload:
        return await self.get_report_raw(body_sink)


class _RawStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk


def _feed_sink(body_sink: Any | None, result: RawPulledPayload) -> RawPulledPayload:
    if body_sink is None:
        return result
    announce = getattr(body_sink, "announce", None)
    if callable(announce):
        announce(
            http_status=result.status_code,
            content_encoding=result.content_encoding,
            protocol_invalid=result.protocol_invalid,
        )
    if result.raw_payload:
        body_sink.feed(result.raw_payload)
    finish = getattr(body_sink, "finish", None)
    if callable(finish):
        finish(
            complete=True,
            http_status=result.status_code,
            content_encoding=result.content_encoding,
            protocol_invalid=result.protocol_invalid,
        )
    return result


def empty_payload() -> RawPulledPayload:
    return RawPulledPayload(json.dumps({"code": 0, "msg": None, "data": []}).encode(), 200)


def test_reservation_constants_match_settings_floor() -> None:
    frames = max_internal_frames(RECOVERY_CAPTURE_BYTES)
    assert INTERNAL_FRAME_SIZE == RAW_SPILL_INTERNAL_FRAME_SIZE == 64 * 1024
    assert frames == RAW_SPILL_MAX_INTERNAL_FRAMES == 1024
    assert DATA_FRAME_OVERHEAD_BYTES == RAW_SPILL_DATA_FRAME_OVERHEAD_BYTES == 36
    assert STREAM_HEADER_BUDGET_BYTES == RAW_SPILL_STREAM_HEADER_BUDGET_BYTES
    assert CONTROL_FRAME_COUNT == RAW_SPILL_CONTROL_FRAME_COUNT
    assert CONTROL_FRAME_BUDGET_BYTES == RAW_SPILL_CONTROL_FRAME_BUDGET_BYTES
    assert DIRECTORY_METADATA_BYTES == RAW_SPILL_DIRECTORY_METADATA_BYTES
    expected = (
        frames * INTERNAL_FRAME_SIZE
        + frames * DATA_FRAME_OVERHEAD_BYTES
        + STREAM_HEADER_BUDGET_BYTES
        + CONTROL_FRAME_COUNT * CONTROL_FRAME_BUDGET_BYTES
        + DIRECTORY_METADATA_BYTES
    )
    assert capture_reservation_bytes(RECOVERY_CAPTURE_BYTES) == expected
    assert RECOVERY_CAPTURE_BYTES == RAW_SPILL_RECOVERY_CAPTURE_BYTES
    assert CAPTURE_FRAME_OVERHEAD_BYTES == RAW_SPILL_CAPTURE_OVERHEAD_BYTES
    assert capture_reservation_bytes(RECOVERY_CAPTURE_BYTES) == RAW_SPILL_MIN_TOTAL_BYTES
    assert CAPTURE_FRAME_OVERHEAD_BYTES < 1024 * 1024


def test_near_quota_63mib_does_not_open_64mib_stream(tmp_path: Path) -> None:
    reserved = capture_reservation_bytes(RECOVERY_CAPTURE_BYTES)
    store = RawSpillStore(tmp_path, max_total_bytes=reserved + 20 * 1024 * 1024)
    sparse_file(tmp_path / "padding.bin", store.max_total_bytes - 63 * 1024 * 1024)
    with pytest.raises(SpillQuotaExceeded):
        store.open_stream("report", crypto())
    assert leftover_names(tmp_path, ".reserve", ".stream", ".stream.tmp") == []


@pytest.mark.asyncio
async def test_near_quota_skips_vendor_and_alerts(tmp_path: Path) -> None:
    reserved = capture_reservation_bytes(RECOVERY_CAPTURE_BYTES)
    store = RawSpillStore(tmp_path, max_total_bytes=reserved + 20 * 1024 * 1024)
    sparse_file(tmp_path / "padding.bin", store.max_total_bytes - 63 * 1024 * 1024)
    gateway = RecordingGateway(empty_payload())
    alerts = FakeAlerts()
    count = await ReportIngestService(
        gateway, FakeRepository(), crypto(), alerts=alerts, spill=store
    ).poll_once()
    assert count == 0
    assert gateway.calls == 0
    assert any(item["alert_type"] == "vendor_raw_spill_quota_exceeded" for item in alerts.events)


def test_directory_pressure_after_reserve_does_not_truncate_20mib(tmp_path: Path) -> None:
    reserved = capture_reservation_bytes(RECOVERY_CAPTURE_BYTES)
    store = RawSpillStore(tmp_path, max_total_bytes=reserved + 5 * 1024 * 1024)
    stream = store.open_stream("report", crypto())
    sparse_file(tmp_path / "pressure.bin", 55 * 1024 * 1024)
    payload = b"x" * (20 * 1024 * 1024)
    assert stream.feed(payload) is True
    stream.finish(complete=True, too_large=True, http_status=200)
    recovered = store.list_pending_streams(crypto())
    assert recovered[0].capture_state == CAPTURE_COMPLETE_TOO_LARGE
    assert recovered[0].payload_sha256


@pytest.mark.asyncio
async def test_complete_20mib_vendor_response_stays_complete_under_pressure(
    tmp_path: Path,
) -> None:
    reserved = capture_reservation_bytes(RECOVERY_CAPTURE_BYTES)
    store = RawSpillStore(tmp_path, max_total_bytes=reserved + 5 * 1024 * 1024)
    sink = store.open_stream("report", crypto())
    sparse_file(tmp_path / "pressure.bin", 55 * 1024 * 1024)
    payload = b"y" * (20 * 1024 * 1024)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_RawStream([payload]), request=request)

    client = ZhihuiClient(
        base_url="http://vendor.test",
        secret_name="name",
        secret_key="key",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://vendor.test",
        ),
        max_response_body_bytes=4 * 1024 * 1024,
        max_response_capture_bytes=RECOVERY_CAPTURE_BYTES,
        total_timeout_s=5,
    )
    with pytest.raises(Exception) as captured:
        await client.get_report_raw(body_sink=sink)
    await client.aclose()
    error = captured.value
    assert getattr(error, "complete", False) is True
    assert len(getattr(error, "raw_body", b"")) == len(payload)
    recovered = store.list_pending_streams(crypto())
    assert recovered[0].capture_state == CAPTURE_COMPLETE_TOO_LARGE


def test_report_and_reply_cannot_oversell_same_directory(tmp_path: Path) -> None:
    reserved = capture_reservation_bytes(RECOVERY_CAPTURE_BYTES)
    store = RawSpillStore(tmp_path, max_total_bytes=reserved + 1024)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def opener(source: str) -> None:
        barrier.wait()
        try:
            store.open_stream(source, crypto())
            outcomes.append("ok")
        except SpillQuotaExceeded:
            outcomes.append("denied")

    threads = [
        threading.Thread(target=opener, args=("report",)),
        threading.Thread(target=opener, args=("reply",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("ok") == 1
    assert outcomes.count("denied") == 1
    assert len(store.list_reservations()) == 1


@pytest.mark.asyncio
async def test_overlapping_report_reply_poll_does_not_call_second_vendor(
    tmp_path: Path,
) -> None:
    reserved = capture_reservation_bytes(RECOVERY_CAPTURE_BYTES)
    store = RawSpillStore(tmp_path, max_total_bytes=reserved + 4096)
    started = asyncio.Event()
    release = asyncio.Event()
    report_gateway = HoldGateway(empty_payload(), started, release)
    reply_gateway = RecordingGateway(empty_payload())
    report_alerts = FakeAlerts()
    reply_alerts = FakeAlerts()
    report_task = asyncio.create_task(
        ReportIngestService(
            report_gateway, FakeRepository(), crypto(), alerts=report_alerts, spill=store
        ).poll_once()
    )
    await started.wait()
    reply_count = await ReplyIngestService(
        reply_gateway, FakeRepository(), crypto(), alerts=reply_alerts, spill=store
    ).poll_once()
    assert reply_count == 0
    assert reply_gateway.calls == 0
    assert any(
        item["alert_type"] == "vendor_raw_spill_quota_exceeded" for item in reply_alerts.events
    )
    release.set()
    await report_task
    assert leftover_names(tmp_path, ".reserve", ".stream", ".stream.tmp") == []


@pytest.mark.asyncio
async def test_disk_full_on_stream_create_skips_vendor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RawSpillStore(tmp_path)
    real_open = Path.open

    def boom(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.name.endswith(".stream.tmp"):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", boom)
    gateway = RecordingGateway(empty_payload())
    alerts = FakeAlerts()
    count = await ReportIngestService(
        gateway, FakeRepository(), crypto(), alerts=alerts, spill=store
    ).poll_once()
    assert count == 0
    assert gateway.calls == 0
    assert leftover_names(tmp_path, ".reserve", ".stream", ".stream.tmp") == []
    assert any(item["alert_type"] == "vendor_raw_spill_quota_exceeded" for item in alerts.events)


def test_feed_enospc_returns_false_and_keeps_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RawSpillStore(tmp_path)
    stream = store.open_stream("reply", crypto(), capture_bytes=64 * 1024)
    real_open = Path.open

    def boom(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self == stream.path and args and str(args[0]).startswith("a"):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", boom)
    assert stream.feed(b'{"code":0,"data":[]}') is True
    assert stream.flush() is False
    assert store.list_reservations()
    monkeypatch.setattr(Path, "open", real_open)
    stream.discard()
    assert store.list_reservations() == []


@pytest.mark.asyncio
async def test_exception_before_body_releases_reservation(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path)
    gateway = BoomGateway()
    with pytest.raises(RuntimeError, match="vendor transport failed"):
        await ReportIngestService(
            gateway, FakeRepository(), crypto(), alerts=FakeAlerts(), spill=store
        ).poll_once()
    assert gateway.calls == 1
    assert leftover_names(tmp_path, ".reserve", ".stream", ".stream.tmp") == []


@pytest.mark.asyncio
async def test_announce_then_exception_before_feed_keeps_stream_and_lease(
    tmp_path: Path,
) -> None:
    store = RawSpillStore(tmp_path)
    gateway = AnnounceThenBoomGateway()
    repository = FakeRepository()
    service = ReportIngestService(
        gateway, repository, crypto(), alerts=FakeAlerts(), spill=store
    )
    with pytest.raises(RuntimeError, match="body unread after announce"):
        await service.poll_once()
    assert gateway.calls == 1
    assert leftover_names(tmp_path, ".stream", ".stream.tmp")
    reservations = store.list_reservations()
    assert len(reservations) == 1
    recovered = store.list_pending_streams(crypto())
    assert len(recovered) == 1
    assert recovered[0].capture_state == CAPTURE_TRUNCATED
    assert recovered[0].http_status == 502
    assert recovered[0].quarantined is True
    assert await service.recover_spills() == 1
    persisted = [value for event, value in repository.events if event == "persist_raw"]
    assert persisted[0]["capture_state"] == CAPTURE_TRUNCATED
    assert persisted[0]["http_status"] == 502
    assert leftover_names(tmp_path, ".reserve", ".stream", ".stream.tmp", ".quarantine") == []


def test_discard_unused_does_not_drop_announced_stream(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path)
    stream = store.open_stream("reply", crypto(), capture_bytes=4096)
    stream.announce(http_status=429, content_encoding="identity")
    assert stream.has_announced is True
    assert stream.has_captured_bytes is False
    discard_unused_report(stream)
    discard_unused_reply(stream)
    assert stream.path.exists()
    assert store.list_reservations()
    recovered = store.list_pending_streams(crypto())
    assert recovered[0].http_status == 429
    assert recovered[0].capture_state == CAPTURE_TRUNCATED


@pytest.mark.asyncio
async def test_successful_poll_releases_reservation(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path)
    await ReportIngestService(
        RecordingGateway(empty_payload()),
        FakeRepository(),
        crypto(),
        alerts=FakeAlerts(),
        spill=store,
    ).poll_once()
    assert leftover_names(tmp_path, ".reserve", ".stream", ".stream.tmp", ".spill") == []


def test_reservation_metadata_has_no_secrets(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path)
    stream = store.open_stream("report", crypto(), capture_bytes=4096)
    raw = json.loads(stream.reservation.path.read_text(encoding="utf-8"))
    assert set(raw) == {"created_at", "lease_id", "reserved_bytes", "source"}
    text = stream.reservation.path.read_text(encoding="utf-8")
    assert "phone" not in text.lower()
    assert "secret" not in text.lower()
    assert "payload" not in text.lower()
    assert "1" * 11 not in text
    stream.discard()


def test_orphan_reservation_is_reclaimed_on_idle(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path)
    lease_id = "ab" * 16
    path = tmp_path / f"report-{lease_id}.reserve"
    path.write_text(
        json.dumps(
            {
                "created_at": "2026-08-23T13:00:00+08:00",
                "lease_id": lease_id,
                "reserved_bytes": capture_reservation_bytes(4096),
                "source": "report",
            },
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    assert store.reclaim_idle("report", crypto()) >= 1
    assert leftover_names(tmp_path, ".reserve") == []


def test_invalid_reservation_with_extra_keys_is_reclaimed(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path)
    lease_id = "cd" * 16
    path = tmp_path / f"reply-{lease_id}.reserve"
    path.write_text(
        json.dumps(
            {
                "created_at": "2026-08-23T13:00:00+08:00",
                "lease_id": lease_id,
                "phone": "13800138000",
                "reserved_bytes": 4096,
                "source": "reply",
            }
        ),
        encoding="utf-8",
    )
    store.reclaim_idle("reply", crypto())
    assert leftover_names(tmp_path, ".reserve") == []


def test_kill_recovery_releases_reservation_after_persist(tmp_path: Path) -> None:
    backend = Path(__file__).resolve().parents[1]
    child = tmp_path / "child.py"
    child.write_text(
        "\n".join(
            [
                "import os, sys",
                f"sys.path.insert(0, {str(backend)!r})",
                "from pathlib import Path",
                "from app.services.crypto import CryptoService",
                "from app.services.raw_spill import RawSpillStore",
                "import base64",
                "key = base64.b64encode(b'r' * 32).decode()",
                "crypto = CryptoService.from_secret_values(key, key)",
                f"store = RawSpillStore(Path({str(tmp_path)!r}))",
                "stream = store.open_stream('report', crypto, capture_bytes=4096)",
                "stream.announce(http_status=200, content_encoding='identity')",
                'stream.feed(b\'{"code":0,"data":[]}\')',
                "assert stream.flush() is True",
                "os._exit(9)",
            ]
        ),
        encoding="utf-8",
    )
    completed = subprocess.run([sys.executable, str(child)], check=False)
    assert completed.returncode == 9
    store = RawSpillStore(tmp_path)
    assert store.list_reservations()
    recovered = asyncio.run(
        ReportIngestService(
            None,  # type: ignore[arg-type]
            FakeRepository(),
            crypto(),
            alerts=FakeAlerts(),
            spill=store,
        ).recover_spills()
    )
    assert recovered == 1
    assert leftover_names(tmp_path, ".reserve", ".stream", ".stream.tmp", ".quarantine") == []
