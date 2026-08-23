"""#422 T5-03：secondary .spill 元数据必须与正文同等认证。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from app.services.crypto import CryptoService
from app.services.raw_capture_legacy import replay_forbidden_message
from app.services.raw_spill import (
    CAPTURE_COMPLETE,
    CAPTURE_PROTOCOL_INVALID,
    CAPTURE_TRUNCATED,
    CAPTURE_UNKNOWN_LEGACY,
    HEADER_QUARANTINE_SUFFIX,
    SPILL_MAGIC,
    STREAM_META_HEADER,
    STREAM_RECORD_HEADER,
    RawSpillStore,
    durable_persist_capture_state,
    is_non_replayable_capture,
    spill_file_identity_matches,
)
from app.services.report_ingest import ReportIngestService


def crypto() -> CryptoService:
    key = base64.b64encode(b"r" * 32).decode()
    return CryptoService.from_secret_values(key, key)


def leftover_names(directory: Path, *suffixes: str) -> list[str]:
    return [
        name
        for name in os.listdir(directory)
        if any(name.endswith(suffix) for suffix in suffixes)
    ]


class FakeRepository:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.errors: list[tuple[int, str]] = []

    async def persist_raw(self, **values: Any) -> int:
        self.events.append(values)
        return len(self.events)

    async def mark_error(self, raw_id: int, error: str) -> None:
        self.errors.append((raw_id, error))


class FakeAlerts:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, **values: Any) -> None:
        self.events.append(values)


def _write_spill(
    store: RawSpillStore,
    *,
    source: str = "report",
    payload: bytes = b"encrypted-raw",
    digest: str | None = None,
    capture_state: str = CAPTURE_COMPLETE,
    http_status: int = 200,
    content_encoding: str = "identity",
    key_version: int = 1,
    service: CryptoService | None = None,
) -> Path:
    payload_sha256 = digest or hashlib.sha256(payload).hexdigest()
    return store.write(
        source=source,
        payload_sha256=payload_sha256,
        key_version=key_version,
        http_status=http_status,
        content_encoding=content_encoding,
        payload_enc=payload,
        crypto=service or crypto(),
        capture_state=capture_state,
    )


def _split_spill(path: Path) -> tuple[bytes, bytes, bytes]:
    raw = path.read_bytes()
    assert raw.startswith(SPILL_MAGIC)
    rest = raw[len(SPILL_MAGIC) :]
    (frame_len,) = STREAM_RECORD_HEADER.unpack_from(rest)
    offset = STREAM_RECORD_HEADER.size
    frame = rest[offset : offset + frame_len]
    payload = rest[offset + frame_len :]
    (meta_len,) = STREAM_META_HEADER.unpack_from(frame)
    meta = frame[STREAM_META_HEADER.size : STREAM_META_HEADER.size + meta_len]
    ciphertext = frame[STREAM_META_HEADER.size + meta_len :]
    return meta, ciphertext, payload


def _rewrite_spill(path: Path, meta: bytes, ciphertext: bytes, payload: bytes) -> None:
    frame = STREAM_META_HEADER.pack(len(meta)) + meta + ciphertext
    path.write_bytes(SPILL_MAGIC + STREAM_RECORD_HEADER.pack(len(frame)) + frame + payload)


def _flip_header_field(path: Path, field: str, value: object) -> None:
    meta, ciphertext, payload = _split_spill(path)
    document = json.loads(meta.decode("utf-8"))
    document[field] = value
    _rewrite_spill(
        path,
        json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8"),
        ciphertext,
        payload,
    )


def _write_legacy_spill(
    directory: Path,
    *,
    source: str = "report",
    digest: str | None = None,
    payload: bytes = b"legacy-ciphertext",
    capture_state: str = CAPTURE_COMPLETE,
    http_status: int = 200,
    content_encoding: str = "identity",
    key_version: int = 1,
) -> Path:
    payload_sha256 = digest or hashlib.sha256(payload).hexdigest()
    header = json.dumps(
        {
            "capture_state": capture_state,
            "content_encoding": content_encoding,
            "http_status": http_status,
            "key_version": key_version,
            "payload_sha256": payload_sha256,
            "source": source,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    path = directory / f"{source}-{payload_sha256}.spill"
    path.write_bytes(header + b"\n" + payload)
    return path


def _recover(
    store: RawSpillStore, alerts: FakeAlerts | None = None
) -> tuple[int, FakeRepository, FakeAlerts]:
    repository = FakeRepository()
    sink = alerts or FakeAlerts()
    recovered = asyncio.run(
        ReportIngestService(
            None,  # type: ignore[arg-type]
            repository,
            crypto(),
            alerts=sink,
            spill=store,
        ).recover_spills()
    )
    return recovered, repository, sink


def test_authenticated_spill_round_trips_capture_state(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path)
    path = _write_spill(store, capture_state=CAPTURE_TRUNCATED, http_status=500)
    pending = store.list_pending("report", crypto())
    assert len(pending) == 1
    assert pending[0].capture_state == CAPTURE_TRUNCATED
    assert pending[0].http_status == 500
    assert pending[0].metadata_authenticated is True
    assert path.read_bytes().startswith(SPILL_MAGIC)
    recovered, repository, _alerts = _recover(store)
    assert recovered == 1
    assert repository.events[0]["capture_state"] == CAPTURE_TRUNCATED
    assert is_non_replayable_capture(repository.events[0]["capture_state"])


def test_metadata_bit_flip_fails_auth_and_does_not_persist(tmp_path: Path) -> None:
    cases = (
        ("capture_state", CAPTURE_COMPLETE),
        ("http_status", 200),
        ("content_encoding", "gzip"),
        ("source", "reply"),
        ("payload_sha256", "b" * 64),
        ("key_version", 2),
        ("payload_enc_sha256", "c" * 64),
    )
    for field, value in cases:
        work = tmp_path / field
        work.mkdir()
        store = RawSpillStore(work)
        path = _write_spill(
            store,
            payload=b"alpha",
            digest="a" * 64,
            capture_state=CAPTURE_TRUNCATED,
            http_status=500,
            content_encoding="identity",
        )
        _flip_header_field(path, field, value)
        recovered, repository, alerts = _recover(store)
        assert recovered == 0, field
        assert repository.events == [], field
        assert leftover_names(work, ".spill") == [], field
        assert leftover_names(work, HEADER_QUARANTINE_SUFFIX), field
        assert store.pending_count() == 0, field
        assert any(
            item["alert_type"] == "vendor_raw_nonactive_quarantine" for item in alerts.events
        ), field
        for item in alerts.events:
            blob = json.dumps(item, ensure_ascii=False)
            assert "alpha" not in blob
            assert "138" not in blob
            assert str(work) not in blob


def test_header_and_payload_swap_fails_auth(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path)
    first = _write_spill(store, payload=b"one", digest="1" * 64, capture_state=CAPTURE_COMPLETE)
    second = _write_spill(
        store, payload=b"two", digest="2" * 64, capture_state=CAPTURE_TRUNCATED, http_status=500
    )
    first_meta, first_cipher, first_payload = _split_spill(first)
    second_meta, second_cipher, second_payload = _split_spill(second)
    _rewrite_spill(first, second_meta, second_cipher, first_payload)
    _rewrite_spill(second, first_meta, first_cipher, second_payload)
    recovered, repository, _alerts = _recover(store)
    assert recovered == 0
    assert repository.events == []
    assert leftover_names(tmp_path, ".spill") == []
    assert leftover_names(tmp_path, HEADER_QUARANTINE_SUFFIX)


def test_cross_source_header_swap_fails_auth(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path)
    report = _write_spill(
        store, source="report", payload=b"report-body", digest="d" * 64
    )
    reply = _write_spill(
        store, source="reply", payload=b"reply-body", digest="e" * 64
    )
    report_meta, report_cipher, report_payload = _split_spill(report)
    reply_meta, reply_cipher, reply_payload = _split_spill(reply)
    _rewrite_spill(report, reply_meta, reply_cipher, report_payload)
    _rewrite_spill(reply, report_meta, report_cipher, reply_payload)
    recovered, repository, _alerts = _recover(store)
    assert recovered == 0
    assert repository.events == []


def test_truncated_cannot_gain_replay_via_header_edit(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path)
    path = _write_spill(
        store,
        payload=b"truncated-body",
        digest="f" * 64,
        capture_state=CAPTURE_PROTOCOL_INVALID,
        http_status=500,
    )
    _flip_header_field(path, "capture_state", CAPTURE_COMPLETE)
    _flip_header_field(path, "http_status", 200)
    recovered, repository, _alerts = _recover(store)
    assert recovered == 0
    assert repository.events == []
    assert leftover_names(tmp_path, ".spill") == []


def test_legacy_unauthenticated_spill_enters_human_inventory(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path)
    _write_legacy_spill(
        tmp_path,
        digest="9" * 64,
        capture_state=CAPTURE_COMPLETE,
        http_status=200,
    )
    pending = store.list_pending("report")
    assert pending[0].format_legacy is True
    assert pending[0].capture_state == CAPTURE_UNKNOWN_LEGACY
    assert durable_persist_capture_state(pending[0]) == CAPTURE_UNKNOWN_LEGACY
    recovered, repository, alerts = _recover(store)
    assert recovered == 1
    assert repository.events[0]["capture_state"] == CAPTURE_UNKNOWN_LEGACY
    assert is_non_replayable_capture(repository.events[0]["capture_state"])
    assert replay_forbidden_message(CAPTURE_UNKNOWN_LEGACY)
    assert any(item["alert_type"] == "vendor_raw_unknown_legacy" for item in alerts.events)
    assert leftover_names(tmp_path, ".spill") == []


def test_filename_header_and_db_identity_are_consistent(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path)
    digest = "a" * 64
    path = _write_spill(store, payload=b"id-body", digest=digest, source="report")
    record = store.list_pending("report", crypto())[0]
    assert record.path == path
    assert spill_file_identity_matches(record)
    assert record.source == "report"
    assert record.payload_sha256 == digest
    recovered, repository, _alerts = _recover(store)
    assert recovered == 1
    assert repository.events[0]["payload_sha256"] == digest
    object_id = f"{repository.events[0].get('source', 'report')}:{digest}"
    assert object_id == "report:" + digest


def test_auth_failure_does_not_consume_activity_quota(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path, max_pending_files=32)
    paths = [
        _write_spill(
            store,
            payload=f"body-{index}".encode(),
            digest=f"{index:064x}",
            capture_state=CAPTURE_TRUNCATED,
        )
        for index in range(8)
    ]
    for path in paths:
        _flip_header_field(path, "capture_state", CAPTURE_COMPLETE)
    assert store.pending_count() == 8
    recovered, repository, alerts = _recover(store)
    assert recovered == 0
    assert repository.events == []
    assert store.pending_count() == 0
    assert store.can_accept() is True
    assert leftover_names(tmp_path, HEADER_QUARANTINE_SUFFIX)
    assert any(item["alert_type"] == "vendor_raw_nonactive_quarantine" for item in alerts.events)
    for item in alerts.events:
        detail = item.get("detail") or {}
        assert set(detail) <= {
            "source",
            "isolated",
            "temps_reclaimed",
            "unauthenticated_partial",
            "orphans",
            "quarantine_expired",
            "quarantine_capacity_dropped",
            "dropped",
        }


def test_stream_spill_and_db_share_authenticated_capture_state(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path)
    service = crypto()
    stream = store.open_stream("report", service, capture_bytes=4096)
    stream.announce(http_status=500, content_encoding="identity", protocol_invalid=True)
    assert stream.feed(b'{"code":0,"data":[]}') is True
    stream.finish(
        complete=False,
        http_status=500,
        content_encoding="identity",
        protocol_invalid=True,
    )
    assembled = store.list_pending_streams(service, "report")
    assert assembled[0].capture_state == CAPTURE_PROTOCOL_INVALID
    store.write(
        source=assembled[0].source,
        payload_sha256=assembled[0].payload_sha256,
        key_version=assembled[0].key_version,
        http_status=assembled[0].http_status,
        content_encoding=assembled[0].content_encoding,
        payload_enc=assembled[0].payload_enc,
        crypto=service,
        capture_state=assembled[0].capture_state,
    )
    store.remove_stream("report", assembled[0].stream_id)
    recovered, repository, _alerts = _recover(store)
    assert recovered == 1
    assert repository.events[0]["capture_state"] == CAPTURE_PROTOCOL_INVALID
    assert repository.events[0]["http_status"] == 500
    assert is_non_replayable_capture(repository.events[0]["capture_state"])


def test_keyring_try_all_versions_does_not_trust_header_hint(tmp_path: Path) -> None:
    first = base64.b64encode(b"r" * 32).decode()
    second = base64.b64encode(b"s" * 32).decode()
    ring = json.dumps({"active_version": 2, "keys": {"1": first, "2": second}})
    rotated = CryptoService.from_secret_values(ring, ring)
    store = RawSpillStore(tmp_path)
    _write_spill(store, payload=b"rotated", digest="7" * 64, service=rotated, key_version=2)
    recovered = asyncio.run(
        ReportIngestService(
            None,  # type: ignore[arg-type]
            FakeRepository(),
            rotated,
            alerts=FakeAlerts(),
            spill=store,
        ).recover_spills()
    )
    assert recovered == 1
    _write_spill(store, payload=b"hint", digest="8" * 64, service=rotated, key_version=2)
    target = tmp_path / f"report-{'8' * 64}.spill"
    _flip_header_field(target, "key_version", 1)
    repository = FakeRepository()
    recovered_fail = asyncio.run(
        ReportIngestService(
            None,  # type: ignore[arg-type]
            repository,
            rotated,
            alerts=FakeAlerts(),
            spill=store,
        ).recover_spills()
    )
    assert recovered_fail == 0
    assert repository.events == []
