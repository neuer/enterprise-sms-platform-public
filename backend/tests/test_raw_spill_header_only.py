from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.services.crypto import CryptoService
from app.services.raw_spill import (
    HEADER_QUARANTINE_SUFFIX,
    STREAM_MAGIC,
    RawSpillStore,
    discard_header_only_stream,
    is_activity_filename,
    manage_raw_spill_stream,
)
from app.services.reply_ingest import ReplyIngestService
from app.services.report_ingest import ReportIngestService
from app.vendor.zhihui import RawPulledPayload, VendorTransportError, ZhihuiClient


def leftover_names(directory: Path, *suffixes: str) -> list[str]:
    return [
        path.name
        for path in directory.iterdir()
        if any(path.name.endswith(suffix) for suffix in suffixes)
    ]


def counted_names(directory: Path) -> list[str]:
    return sorted(
        path.name
        for path in directory.iterdir()
        if path.is_file() and is_activity_filename(path.name)
    )


def assert_quota(store: RawSpillStore, expected_pending: int) -> None:
    names = counted_names(store.directory)
    assert len(names) == expected_pending
    assert store.pending_count() == expected_pending
    assert store.can_accept() is (expected_pending < store.max_pending_files)


def crypto() -> CryptoService:
    key = base64.b64encode(b"r" * 32).decode()
    return CryptoService.from_secret_values(key, key)


def age_path(path: Path, seconds: float = 120) -> None:
    stamped = time.time() - seconds
    os.utime(path, times=(stamped, stamped))


def age_streams(directory: Path, seconds: float = 120) -> None:
    for path in (*directory.glob("*.stream"), *directory.glob("*.stream.tmp")):
        age_path(path, seconds)


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


class BoomGateway:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls = 0
        self.error = error or RuntimeError("vendor transport failed")

    async def get_report_raw(self, body_sink: Any | None = None) -> RawPulledPayload:
        self.calls += 1
        raise self.error

    async def get_reply_raw(self, body_sink: Any | None = None) -> RawPulledPayload:
        self.calls += 1
        raise self.error


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


def seed_header_only(store: RawSpillStore, count: int, source: str = "report") -> None:
    for _ in range(count):
        store.open_stream(source, crypto(), capture_bytes=4096)


@pytest.mark.asyncio
async def test_32_transport_failures_do_not_block_33rd_poll(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path, max_pending_files=32)
    boom = BoomGateway()
    service = ReportIngestService(
        boom, FakeRepository(), crypto(), alerts=FakeAlerts(), spill=store
    )
    for _ in range(32):
        with pytest.raises(RuntimeError, match="vendor transport failed"):
            await service.poll_once()
        assert_quota(store, 0)
        assert leftover_names(tmp_path, ".reserve", ".stream", ".stream.tmp") == []
    recovered = RecordingGateway(empty_payload())
    service.gateway = recovered
    await service.poll_once()
    assert boom.calls == 32
    assert recovered.calls == 1
    assert_quota(store, 0)


@pytest.mark.asyncio
async def test_32_report_header_only_do_not_block_reply(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path, max_pending_files=32)
    seed_header_only(store, 32, "report")
    age_streams(tmp_path)
    stats = store.header_only_stats()
    assert stats.header_only_count == 32
    assert stats.oldest_age_seconds is not None
    assert stats.oldest_age_seconds >= 100
    assert_quota(store, 32)
    gateway = RecordingGateway(empty_payload())
    alerts = FakeAlerts()
    await ReplyIngestService(
        gateway, FakeRepository(), crypto(), alerts=alerts, spill=store
    ).poll_once()
    assert gateway.calls == 1
    assert_quota(store, 0)
    assert leftover_names(tmp_path, ".reserve", ".stream", ".stream.tmp") == []
    assert any(item["alert_type"] == "vendor_raw_header_only" for item in alerts.events)
    assert store.header_only_stats().cleaned_total >= 32


def test_context_manager_discards_header_only_on_pre_announce_error(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path)
    try:
        with store.open_stream("report", crypto(), capture_bytes=4096) as stream:
            assert stream.is_header_only is True
            assert stream.path.exists()
            raise RuntimeError("dns failed")
    except RuntimeError:
        pass
    assert_quota(store, 0)
    assert leftover_names(tmp_path, ".reserve", ".stream", ".stream.tmp") == []
    assert store.header_only_stats().cleaned_total >= 1


def test_context_manager_keeps_announced_stream(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path)
    stream = store.open_stream("reply", crypto(), capture_bytes=4096)
    try:
        with stream:
            stream.announce(http_status=502, content_encoding="identity")
            raise RuntimeError("after announce")
    except RuntimeError:
        pass
    assert stream.is_header_only is False
    assert stream.path.exists()
    assert store.pending_count() >= 1
    recovered = store.list_pending_streams(crypto())
    assert recovered
    assert recovered[0].http_status == 502


def test_young_header_only_is_not_reclaimed(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path)
    stream = store.open_stream("report", crypto(), capture_bytes=4096)
    result = store.reclaim_idle("report", crypto())
    assert result.header_only == 0
    assert stream.path.exists()
    assert_quota(store, 1)
    assert store.list_reservations()


def test_aged_header_only_reclaims_reservation(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path)
    stream = store.open_stream("report", crypto(), capture_bytes=4096)
    age_path(stream.path)
    result = store.reclaim_idle("reply", crypto())
    assert result.header_only == 1
    assert_quota(store, 0)
    assert leftover_names(tmp_path, ".reserve") == []
    assert store.header_only_stats().header_only_count == 0


def test_authenticated_data_is_never_deleted_as_header_only(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path)
    stream = store.open_stream("report", crypto(), capture_bytes=4096)
    stream.announce(http_status=200, content_encoding="identity")
    assert stream.feed(b'{"code":0,"data":[{"ok":true}]}') is True
    assert stream.flush() is True
    age_path(stream.path)
    result = store.reclaim_idle("report", crypto())
    assert result.header_only == 0
    assert result.incomplete_frames == 0
    assert stream.path.exists()
    recovered = store.list_pending_streams(crypto())
    assert recovered
    assert recovered[0].payload_sha256
    assert stream.path.exists()


def test_partial_corrupt_and_legal_header_have_distinct_handling(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path, header_only_min_age_s=0)
    legal = store.open_stream("report", crypto(), capture_bytes=4096)
    partial_id = "ab" * 16
    partial = tmp_path / f"reply-{partial_id}.stream.tmp"
    partial.write_bytes(STREAM_MAGIC[:4])
    corrupt_id = "cd" * 16
    corrupt = tmp_path / f"report-{corrupt_id}.stream.tmp"
    corrupt.write_bytes(STREAM_MAGIC + b"{not-json}\n")
    assert_quota(store, 3)
    result = store.reclaim_idle("report", crypto())
    assert result.header_only == 1
    assert result.partial_header == 1
    assert result.corrupt_header == 1
    assert not legal.path.exists()
    assert not partial.exists()
    assert not corrupt.exists()
    marker = tmp_path / f"report-{corrupt_id}{HEADER_QUARANTINE_SUFFIX}"
    assert marker.exists()
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert set(payload) == {"source", "state", "stream_id"}
    assert payload["state"] == "corrupt_header"
    assert "phone" not in marker.read_text(encoding="utf-8").lower()
    assert_quota(store, 0)
    stats = store.header_only_stats()
    assert stats.cleaned_total >= 3
    assert stats.header_only_count == 0
    assert stats.partial_header_count == 0
    assert stats.corrupt_header_count == 0


def test_incomplete_frames_are_not_deleted_as_header_only(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path, header_only_min_age_s=0)
    stream = store.open_stream("reply", crypto(), capture_bytes=4096)
    with stream.path.open("ab") as handle:
        handle.write(b"\x00\x00\x00\x08partial")
        handle.flush()
        os.fsync(handle.fileno())
    age_path(stream.path)
    result = store.reclaim_idle("reply", crypto())
    assert result.header_only == 0
    assert result.unauthenticated_partial >= 1
    assert result.isolated >= 1
    assert not stream.path.exists()
    assert store.list_pending_streams(crypto()) == []
    assert leftover_names(tmp_path, ".cq")
    assert_quota(store, 0)


def test_process_exit_between_open_stream_and_send_reclaims_quota(
    tmp_path: Path,
) -> None:
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
                "store.open_stream('report', crypto, capture_bytes=4096)",
                "os._exit(9)",
            ]
        ),
        encoding="utf-8",
    )
    completed = subprocess.run([sys.executable, str(child)], check=False)
    assert completed.returncode == 9
    store = RawSpillStore(tmp_path)
    assert leftover_names(tmp_path, ".stream.tmp")
    assert leftover_names(tmp_path, ".reserve")
    assert_quota(store, 1)
    age_streams(tmp_path)
    alerts = FakeAlerts()
    recovered = asyncio.run(
        ReportIngestService(
            None,  # type: ignore[arg-type]
            FakeRepository(),
            crypto(),
            alerts=alerts,
            spill=store,
        ).recover_spills()
    )
    assert recovered == 0
    assert_quota(store, 0)
    assert leftover_names(tmp_path, ".reserve", ".stream", ".stream.tmp") == []
    assert any(item["alert_type"] == "vendor_raw_header_only" for item in alerts.events)


@pytest.mark.asyncio
async def test_zhihui_connect_error_before_announce_discards_header_only(
    tmp_path: Path,
) -> None:
    store = RawSpillStore(tmp_path)
    stream = store.open_stream("report", crypto(), capture_bytes=4096)

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Name or service not known", request=request)

    client = ZhihuiClient(
        base_url="http://vendor.test",
        secret_name="name",
        secret_key="key",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://vendor.test",
        ),
        total_timeout_s=2,
    )
    with pytest.raises(VendorTransportError), manage_raw_spill_stream(stream):
        await client.get_report_raw(body_sink=stream)
    await client.aclose()
    assert stream.is_header_only is False
    assert_quota(store, 0)
    assert leftover_names(tmp_path, ".reserve", ".stream", ".stream.tmp") == []


def test_discard_helper_ignores_streams_with_data(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path)
    stream = store.open_stream("report", crypto(), capture_bytes=4096)
    stream.feed(b'{"code":0}')
    discard_header_only_stream(stream)
    assert stream.path.exists()
    stream.discard()
    assert_quota(store, 0)
