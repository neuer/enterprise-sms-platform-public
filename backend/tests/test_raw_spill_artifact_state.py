"""#439 T5-02：Raw Artifact 有界终态与非活动隔离，不占活动拉取配额。"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
from typing import Any

import pytest

from app.services.crypto import CryptoService
from app.services.raw_spill import (
    HEADER_QUARANTINE_SUFFIX,
    STREAM_MAGIC,
    RawSpillStore,
    is_activity_filename,
)
from app.services.reply_ingest import ReplyIngestService
from app.services.report_ingest import ReportIngestService
from app.vendor.zhihui import RawPulledPayload

FORBIDDEN_EVIDENCE = ("phone", "payload", "ciphertext", "secret", "key", "body", "/")


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
    stamped = time_stamp(seconds)
    os.utime(path, times=(stamped, stamped))


def time_stamp(seconds: float) -> float:
    return __import__("time").time() - seconds


def age_dir(directory: Path, seconds: float = 120) -> None:
    for path in directory.iterdir():
        if path.is_file() and path.name != ".quota.lock":
            age_path(path, seconds)


class FakeRepository:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    async def persist_raw(self, **values: Any) -> int:
        self.events.append(("persist_raw", values))
        return len(self.events)

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


def clear_artifacts(directory: Path) -> None:
    for path in list(directory.iterdir()):
        if path.is_file() and path.name != ".quota.lock":
            path.unlink()


def header_length(data: bytes) -> int:
    assert data.startswith(STREAM_MAGIC)
    return data.index(b"\n", len(STREAM_MAGIC)) + 1


def sample_announce_bytes(directory: Path) -> bytes:
    store = RawSpillStore(directory, header_only_min_age_s=0)
    stream = store.open_stream("report", crypto(), capture_bytes=4096)
    stream.announce(http_status=200, content_encoding="identity")
    payload = stream.path.read_bytes()
    clear_artifacts(directory)
    return payload


def sample_data_bytes(directory: Path) -> bytes:
    store = RawSpillStore(directory, header_only_min_age_s=0)
    stream = store.open_stream("report", crypto(), capture_bytes=4096)
    assert stream.feed(b'{"code":0}') is True
    assert stream.flush() is True
    payload = stream.path.read_bytes()
    clear_artifacts(directory)
    return payload


def sample_terminal_bytes(directory: Path) -> bytes:
    store = RawSpillStore(directory, header_only_min_age_s=0)
    stream = store.open_stream("report", crypto(), capture_bytes=4096)
    stream.finish(complete=True, http_status=200, content_encoding="identity")
    payload = stream.path.read_bytes()
    clear_artifacts(directory)
    return payload


def sample_announce_then_data_bytes(directory: Path) -> bytes:
    store = RawSpillStore(directory, header_only_min_age_s=0)
    stream = store.open_stream("report", crypto(), capture_bytes=4096)
    stream.announce(http_status=200, content_encoding="identity")
    announced = stream.path.read_bytes()
    assert stream.feed(b'{"code":0,"data":[{"ok":true}]}') is True
    assert stream.flush() is True
    payload = stream.path.read_bytes()
    clear_artifacts(directory)
    return announced, payload


def write_named_stream(directory: Path, data: bytes, *, full: bytes | None = None) -> Path:
    header_src = full if full is not None else data
    header_line = header_src[len(STREAM_MAGIC) :].split(b"\n", 1)[0]
    header = json.loads(header_line.decode("utf-8"))
    source = str(header["source"])
    stream_id = str(header["stream_id"])
    path = directory / f"{source}-{stream_id}.stream.tmp"
    path.write_bytes(data)
    return path


def evidence_payloads(directory: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for path in (
        *directory.glob(f"*{HEADER_QUARANTINE_SUFFIX}"),
        *directory.glob("*.cq.man"),
    ):
        if path.name.endswith(".tmp"):
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        items.append(raw)
        text = path.read_text(encoding="utf-8").lower()
        for forbidden in FORBIDDEN_EVIDENCE:
            if forbidden == "/":
                assert "\\" not in text
                continue
            assert forbidden not in text
        assert set(raw).isdisjoint({"phone", "payload", "ciphertext", "secret", "key"})
        if "src_name" in raw:
            assert "/" not in str(raw["src_name"])
            assert "\\" not in str(raw["src_name"])
    return items


async def restart(store: RawSpillStore) -> tuple[int, FakeAlerts, FakeRepository]:
    alerts = FakeAlerts()
    repository = FakeRepository()
    recovered = await ReportIngestService(
        None,  # type: ignore[arg-type]
        repository,
        crypto(),
        alerts=alerts,
        spill=store,
    ).recover_spills()
    return recovered, alerts, repository


def assert_activity_cleared(store: RawSpillStore) -> None:
    assert_quota(store, 0)
    assert store.list_pending_streams(crypto()) == []
    assert store.list_pending() == []


@pytest.mark.parametrize("kind", ["announce", "control", "data"])
def test_kill_at_every_byte_of_first_frame_has_terminal_state(
    tmp_path: Path, kind: str
) -> None:
    samples = tmp_path / "samples"
    samples.mkdir()
    if kind == "announce":
        full = sample_announce_bytes(samples)
    elif kind == "control":
        full = sample_terminal_bytes(samples)
    else:
        full = sample_data_bytes(samples)
    head = header_length(full)
    frame = full[head:]
    work = tmp_path / kind
    work.mkdir()
    store = RawSpillStore(work, header_only_min_age_s=0, max_pending_files=32)
    persisted = 0
    isolated = 0
    for size in range(len(frame) + 1):
        clear_artifacts(work)
        write_named_stream(work, full[: head + size], full=full)
        recovered, alerts, repository = asyncio.run(restart(store))
        assert_activity_cleared(store)
        if size == 0:
            assert recovered == 0
            assert leftover_names(work, HEADER_QUARANTINE_SUFFIX) == []
            continue
        if recovered:
            persisted += 1
            assert repository.events
            continue
        isolated += 1
        assert leftover_names(work, ".cq") or leftover_names(work, HEADER_QUARANTINE_SUFFIX)
        evidence_payloads(work)
        assert any(
            item["alert_type"] == "vendor_raw_nonactive_quarantine" for item in alerts.events
        )
    assert isolated >= 1
    assert persisted >= 1


def test_kill_at_write_boundaries_of_announce_and_data(tmp_path: Path) -> None:
    samples = tmp_path / "samples"
    samples.mkdir()
    announced, complete = sample_announce_then_data_bytes(samples)
    head = header_length(announced)
    work = tmp_path / "writes"
    work.mkdir()
    store = RawSpillStore(work, header_only_min_age_s=0)
    write_sizes = sorted(
        {
            head,
            head + 4,
            head + 8,
            len(announced),
            len(announced) + 4,
            len(complete),
        }
    )
    for size in write_sizes:
        clear_artifacts(work)
        write_named_stream(work, complete[:size], full=complete)
        asyncio.run(restart(store))
        assert_activity_cleared(store)


def test_corrupt_spill_leaves_activity_quota(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path, max_pending_files=32)
    (tmp_path / f"report-{'a' * 64}.spill").write_bytes(b"not-a-spill")
    assert_quota(store, 1)
    recovered, alerts, _repository = asyncio.run(restart(store))
    assert recovered == 0
    assert leftover_names(tmp_path, ".spill") == []
    assert leftover_names(tmp_path, ".cq")
    evidence_payloads(tmp_path)
    assert_quota(store, 0)
    stats = store.artifact_stats()
    assert stats.active_count == 0
    assert stats.quarantine_count >= 1
    assert stats.isolated_total >= 1
    assert any(item["alert_type"] == "vendor_raw_nonactive_quarantine" for item in alerts.events)


def test_atomic_write_temps_are_promoted_or_isolated(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path, header_only_min_age_s=0)
    digest = "b" * 64
    payload = b"encrypted-raw"
    final = store.write(
        source="report",
        payload_sha256=digest,
        key_version=1,
        http_status=200,
        content_encoding="identity",
        payload_enc=payload,
        crypto=crypto(),
    )
    tmp = final.with_name(final.name + ".tmp")
    final.rename(tmp)
    assert leftover_names(tmp_path, ".spill") == []
    store.reclaim_idle("report", crypto())
    assert final.exists()
    assert store.list_pending("report")

    incomplete = tmp_path / f"report-{'c' * 64}.spill.tmp"
    incomplete.write_bytes(b'{"source":"report"')
    age_path(incomplete)
    store.reclaim_idle("report", crypto())
    assert not incomplete.exists()
    assert leftover_names(tmp_path, ".cq")

    headerq_tmp = tmp_path / f"report-{'d' * 32}.headerq.tmp"
    headerq_tmp.write_text(
        json.dumps(
            {"source": "report", "state": "corrupt_header", "stream_id": "d" * 32},
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    quarantine_tmp = tmp_path / f"reply-{'e' * 32}.quarantine.tmp"
    quarantine_tmp.write_text(
        json.dumps(
            {"source": "reply", "state": "quarantined", "stream_id": "e" * 32},
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    broken_tmp = tmp_path / f"report-{'f' * 32}.headerq.tmp"
    broken_tmp.write_bytes(b"{not-json")
    store.reclaim_idle("reply", crypto())
    assert (tmp_path / f"report-{'d' * 32}.headerq").exists()
    assert not headerq_tmp.exists()
    assert not quarantine_tmp.exists()
    assert not broken_tmp.exists()
    assert_quota(store, 1)


def test_stream_delete_reconciles_orphan_markers(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path, header_only_min_age_s=0)
    stream = store.open_stream("report", crypto(), capture_bytes=4096)
    source = stream.source
    stream_id = stream.stream_id
    reserve = stream.reservation.path
    stream.path.unlink()
    quarantine = tmp_path / f"{source}-{stream_id}.quarantine"
    quarantine.write_text(
        json.dumps(
            {"source": source, "state": "quarantined", "stream_id": stream_id},
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    headerq = tmp_path / f"{source}-{stream_id}{HEADER_QUARANTINE_SUFFIX}"
    headerq.write_text(
        json.dumps(
            {"source": source, "state": "corrupt_header", "stream_id": stream_id},
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    assert reserve.exists()
    store.reclaim_idle("reply", crypto())
    assert leftover_names(tmp_path, ".reserve") == []
    assert leftover_names(tmp_path, ".quarantine") == []
    assert headerq.exists()
    assert_quota(store, 0)
    assert store.accounted_usage() == 0


@pytest.mark.asyncio
async def test_32_unreadable_objects_do_not_stop_shared_volume_polls(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path, max_pending_files=32, header_only_min_age_s=0)
    for index in range(32):
        (tmp_path / f"report-{index:064x}.spill").write_bytes(b"corrupt")
    assert_quota(store, 32)
    gateway = RecordingGateway(empty_payload())
    alerts = FakeAlerts()
    await ReplyIngestService(
        gateway, FakeRepository(), crypto(), alerts=alerts, spill=store
    ).poll_once()
    assert gateway.calls == 1
    assert_quota(store, 0)
    assert leftover_names(tmp_path, ".spill") == []
    assert leftover_names(tmp_path, ".cq")
    assert store.artifact_stats().quarantine_count >= 1
    assert any(item["alert_type"] == "vendor_raw_nonactive_quarantine" for item in alerts.events)


@pytest.mark.asyncio
async def test_32_orphan_markers_do_not_stop_reply(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path, max_pending_files=32)
    for index in range(32):
        token = f"{index:032x}"
        (tmp_path / f"report-{token}.quarantine").write_text(
            json.dumps(
                {"source": "report", "state": "quarantined", "stream_id": token},
                separators=(",", ":"),
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        (tmp_path / f"report-{token}.reserve").write_text(
            json.dumps(
                {
                    "created_at": "2026-08-23T21:00:00+08:00",
                    "lease_id": token,
                    "reserved_bytes": 4096,
                    "source": "report",
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    assert_quota(store, 0)
    gateway = RecordingGateway(empty_payload())
    await ReplyIngestService(
        gateway, FakeRepository(), crypto(), alerts=FakeAlerts(), spill=store
    ).poll_once()
    assert gateway.calls == 1
    assert leftover_names(tmp_path, ".reserve", ".quarantine") == []
    assert_quota(store, 0)


def test_authenticated_prefix_is_never_deleted_as_empty(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path, header_only_min_age_s=0)
    stream = store.open_stream("report", crypto(), capture_bytes=4096)
    stream.announce(http_status=200, content_encoding="identity")
    assert stream.feed(b'{"code":0,"data":[{"ok":true}]}') is True
    assert stream.flush() is True
    with stream.path.open("ab") as handle:
        handle.write(b"\x00\x00\x00\x10partial-unauth")
        handle.flush()
        os.fsync(handle.fileno())
    age_path(stream.path)
    result = store.reclaim_idle("report", crypto())
    assert result.header_only == 0
    assert stream.path.exists()
    recovered = store.list_pending_streams(crypto())
    assert recovered
    assert recovered[0].payload_sha256
    assert recovered[0].capture_state in {"truncated", "protocol_invalid"}
    assert recovered[0].plaintext_bytes > 0


def test_nonactive_quarantine_has_capacity_retention_metrics(tmp_path: Path) -> None:
    store = RawSpillStore(
        tmp_path,
        header_only_min_age_s=0,
        max_cipherq_files=2,
        max_cipherq_bytes=4096,
        cipherq_retention_s=60,
    )
    for index in range(4):
        (tmp_path / f"report-{index:064x}.spill").write_bytes(b"corrupt")
    result = store.reclaim_idle("report", crypto())
    assert result.isolated >= 2
    stats = store.artifact_stats()
    assert stats.active_count == 0
    assert stats.cipherq_count <= 2
    assert stats.cipherq_bytes <= 4096
    assert stats.capacity_dropped_total >= 1
    assert_quota(store, 0)

    retained = leftover_names(tmp_path, ".cq")
    assert retained
    for name in retained:
        age_path(tmp_path / name, seconds=120)
    store.cipherq_retention_s = 30
    expired = store.reclaim_idle("reply", crypto())
    assert expired.cipherq_expired >= 1
    assert store.artifact_stats().cipherq_count == 0


@pytest.mark.asyncio
async def test_quarantine_bytes_do_not_consume_activity_account(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path, max_pending_files=32)
    for index in range(32):
        marker = tmp_path / f"report-{index:032x}{HEADER_QUARANTINE_SUFFIX}"
        marker.write_text(
            json.dumps(
                {
                    "isolated_at": "2026-08-23T21:00:00+08:00",
                    "kind": "spill",
                    "size_bytes": 12,
                    "source": "report",
                    "state": "corrupt_spill",
                    "stream_id": f"{index:032x}",
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    assert_quota(store, 0)
    assert store.accounted_usage() == 0
    gateway = RecordingGateway(empty_payload())
    await ReplyIngestService(
        gateway, FakeRepository(), crypto(), alerts=FakeAlerts(), spill=store
    ).poll_once()
    assert gateway.calls == 1


def test_young_unauthenticated_partial_is_not_isolated(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path)
    stream = store.open_stream("report", crypto(), capture_bytes=4096)
    with stream.path.open("ab") as handle:
        handle.write(b"\xff\xff\xff\xff\x00")
        handle.flush()
        os.fsync(handle.fileno())
    result = store.reclaim_idle("report", crypto())
    assert result.isolated == 0
    assert stream.path.exists()
    assert_quota(store, 1)


def test_rewrite_hdr_tmp_is_promoted(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path)
    stream = store.open_stream("report", crypto(), capture_bytes=4096)
    stream.announce(http_status=200, content_encoding="identity")
    hdr = stream.path.with_name(stream.path.name + ".hdr")
    hdr.write_bytes(stream.path.read_bytes())
    stream.path.unlink()
    store.reclaim_idle("report", crypto())
    assert stream.path.exists()
    assert not hdr.exists()
    assert store.list_reservations()
    recovered = store.list_pending_streams(crypto())
    assert recovered
    assert recovered[0].http_status == 200
