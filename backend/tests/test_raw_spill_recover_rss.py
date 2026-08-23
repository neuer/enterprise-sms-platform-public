"""#440：恢复不得把共享 backlog 一次性物化进 realtime Worker RSS。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
from pathlib import Path
from typing import Any

import pytest

from app.services.crypto import CryptoService
from app.services.raw_spill import (
    HEADER_QUARANTINE_SUFFIX,
    RawSpillStore,
    RecoverMemoryProbe,
    RecoverRoundBudget,
)
from app.services.reply_ingest import ReplyIngestService
from app.services.report_ingest import ReportIngestService


def leftover_names(directory: Path, *suffixes: str) -> list[str]:
    return [
        name
        for name in os.listdir(directory)
        if any(name.endswith(suffix) for suffix in suffixes)
    ]


def crypto() -> CryptoService:
    key = base64.b64encode(b"r" * 32).decode()
    return CryptoService.from_secret_values(key, key)


class FakeRepository:
    def __init__(self, *, fail_digest: str | None = None) -> None:
        self.events: list[dict[str, Any]] = []
        self.fail_digest = fail_digest

    async def persist_raw(self, **values: Any) -> int:
        if self.fail_digest and values.get("payload_sha256") == self.fail_digest:
            raise RuntimeError("injected persist")
        self.events.append(values)
        return len(self.events)

    async def mark_error(self, raw_id: int, error: str) -> None:
        return None


class FakeAlerts:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, **values: Any) -> None:
        self.events.append(values)


def _write_spill(
    store: RawSpillStore, source: str, payload: bytes, digest: str | None = None
) -> str:
    payload_sha256 = digest or hashlib.sha256(payload).hexdigest()
    store.write(
        source=source,
        payload_sha256=payload_sha256,
        key_version=1,
        http_status=200,
        content_encoding="identity",
        payload_enc=payload,
        crypto=crypto(),
    )
    return payload_sha256


def _write_stream(store: RawSpillStore, source: str, payload: bytes) -> None:
    stream = store.open_stream(source, crypto(), capture_bytes=max(len(payload), 4096))
    stream.announce(http_status=200, content_encoding="identity")
    assert stream.feed(payload) is True
    stream.finish(complete=True, http_status=200, content_encoding="identity")


@pytest.mark.asyncio
async def test_recover_peak_tracks_one_file_not_backlog_size(tmp_path: Path) -> None:
    probe = RecoverMemoryProbe()
    store = RawSpillStore(tmp_path, memory_probe=probe)
    chunk = b"R" * 4096
    for index in range(6):
        _write_spill(store, "report", chunk, digest=f"{index:064x}")
    one_file = len(chunk)
    service = ReportIngestService(
        None,  # type: ignore[arg-type]
        FakeRepository(),
        crypto(),
        alerts=FakeAlerts(),
        spill=store,
    )
    assert await service.recover_spills() == 6
    assert probe.peak_bytes <= one_file
    assert probe.peak_bytes < 3 * one_file
    assert len(probe.payload_reads) == 6


@pytest.mark.asyncio
async def test_report_recover_does_not_read_reply_payload(tmp_path: Path) -> None:
    probe = RecoverMemoryProbe()
    store = RawSpillStore(tmp_path, memory_probe=probe)
    _write_spill(store, "report", b"report-body", digest="a" * 64)
    _write_spill(store, "reply", b"reply-secret-payload", digest="b" * 64)
    _write_stream(store, "reply", b'{"code":0,"data":[{"phone":"13800000000"}]}')
    recovered = await ReportIngestService(
        None,  # type: ignore[arg-type]
        FakeRepository(),
        crypto(),
        alerts=FakeAlerts(),
        spill=store,
    ).recover_spills()
    assert recovered == 1
    assert all(name.startswith("report-") for name in probe.payload_reads)
    assert not any(name.startswith("reply-") for name in probe.payload_reads)
    assert store.list_pending("reply")
    assert store.list_pending_streams(crypto(), "reply")


@pytest.mark.asyncio
async def test_reply_recover_does_not_read_report_payload(tmp_path: Path) -> None:
    probe = RecoverMemoryProbe()
    store = RawSpillStore(tmp_path, memory_probe=probe)
    _write_spill(store, "reply", b"reply-body", digest="c" * 64)
    _write_spill(store, "report", b"report-secret-payload", digest="d" * 64)
    recovered = await ReplyIngestService(
        None,
        FakeRepository(),
        crypto(),
        alerts=FakeAlerts(),
        spill=store,
    ).recover_spills()
    assert recovered == 1
    assert all(name.startswith("reply-") for name in probe.payload_reads)
    assert not any(name.startswith("report-") for name in probe.payload_reads)


@pytest.mark.asyncio
async def test_concurrent_report_reply_recover_peak_is_two_files(tmp_path: Path) -> None:
    probe = RecoverMemoryProbe()
    store = RawSpillStore(tmp_path, memory_probe=probe)
    payload = b"X" * 8192
    for index in range(4):
        _write_spill(store, "report", payload, digest=f"{index:064x}")
        _write_spill(store, "reply", payload, digest=f"{index + 16:064x}")
    report = ReportIngestService(
        None,  # type: ignore[arg-type]
        FakeRepository(),
        crypto(),
        alerts=FakeAlerts(),
        spill=store,
    )
    reply = ReplyIngestService(
        None,
        FakeRepository(),
        crypto(),
        alerts=FakeAlerts(),
        spill=store,
    )
    recovered = await asyncio.gather(report.recover_spills(), reply.recover_spills())
    assert recovered == [4, 4]
    assert probe.peak_bytes <= 2 * len(payload)
    assert probe.peak_bytes < 4 * len(payload)


@pytest.mark.asyncio
async def test_corrupt_file_does_not_block_remaining_recover(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path)
    good_a = _write_spill(store, "report", b"alpha", digest="1" * 64)
    (tmp_path / f"report-{'e' * 64}.spill").write_bytes(b"not-a-spill")
    _write_spill(store, "report", b"omega", digest="2" * 64)
    repository = FakeRepository()
    recovered = await ReportIngestService(
        None,  # type: ignore[arg-type]
        repository,
        crypto(),
        alerts=FakeAlerts(),
        spill=store,
    ).recover_spills()
    assert recovered == 2
    assert {item["payload_sha256"] for item in repository.events} == {good_a, "2" * 64}
    leftovers = leftover_names(tmp_path, ".spill")
    assert leftovers == []
    isolated = leftover_names(tmp_path, HEADER_QUARANTINE_SUFFIX)
    assert isolated
    assert store.pending_count() == 0
    assert store.can_accept() is True


@pytest.mark.asyncio
async def test_persist_failure_on_one_file_continues(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path)
    bad = _write_spill(store, "report", b"bad", digest="3" * 64)
    _write_spill(store, "report", b"ok", digest="4" * 64)
    repository = FakeRepository(fail_digest=bad)
    recovered = await ReportIngestService(
        None,  # type: ignore[arg-type]
        repository,
        crypto(),
        alerts=FakeAlerts(),
        spill=store,
    ).recover_spills()
    assert recovered == 1
    assert repository.events[0]["payload_sha256"] == "4" * 64
    assert store.list_pending("report")


@pytest.mark.asyncio
async def test_recover_round_file_budget_leaves_remainder(tmp_path: Path) -> None:
    store = RawSpillStore(
        tmp_path,
        recover_budget=RecoverRoundBudget(max_files=2, max_plaintext_bytes=1024 * 1024),
    )
    for index in range(5):
        _write_spill(store, "report", b"n", digest=f"{index:064x}")
    service = ReportIngestService(
        None,  # type: ignore[arg-type]
        FakeRepository(),
        crypto(),
        alerts=FakeAlerts(),
        spill=store,
    )
    assert await service.recover_spills() == 2
    assert len(store.list_pending("report")) == 3
    assert await service.recover_spills() == 2
    assert len(store.list_pending("report")) == 1


@pytest.mark.asyncio
async def test_stream_recover_is_source_filtered_and_round_trips(tmp_path: Path) -> None:
    probe = RecoverMemoryProbe()
    store = RawSpillStore(tmp_path, memory_probe=probe)
    raw = b'{"code":0,"data":[]}'
    _write_stream(store, "report", raw)
    _write_stream(store, "reply", b'{"code":0,"data":[{"skip":true}]}')
    repository = FakeRepository()
    recovered = await ReportIngestService(
        None,  # type: ignore[arg-type]
        repository,
        crypto(),
        alerts=FakeAlerts(),
        spill=store,
    ).recover_spills()
    assert recovered == 1
    assert repository.events[0]["payload_sha256"] == hashlib.sha256(raw).hexdigest()
    assert probe.payload_reads
    assert all(
        name.startswith("report-") and name.endswith((".stream", ".tmp"))
        for name in probe.payload_reads
    )
    assert store.list_pending_streams(crypto(), "reply")


def test_reclaim_and_recover_do_not_whole_file_read_foreign_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RawSpillStore(tmp_path)
    _write_stream(store, "reply", b"Y" * 32_768)
    whole_reads: list[str] = []
    real_read = Path.read_bytes

    def tracked(self: Path) -> bytes:
        if self.parent == tmp_path and self.suffix in {".stream", ".tmp"}:
            whole_reads.append(self.name)
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", tracked)
    store.reclaim_idle("report", crypto())
    assert whole_reads == []


def test_iter_pending_is_lazy(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path)
    _write_spill(store, "report", b"one", digest="5" * 64)
    _write_spill(store, "report", b"two", digest="6" * 64)
    iterator = store.iter_pending("report")
    first = next(iterator)
    assert first.payload_sha256 == "5" * 64
    second = next(iterator)
    assert second.payload_sha256 == "6" * 64
    assert list(store.list_pending("report"))
