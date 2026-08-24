"""#439：缺 Key / 暂态 I/O 不得销毁密文；认证失败进入可恢复 cipherq。"""

from __future__ import annotations

import asyncio
import base64
import errno
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from app.services.crypto import CryptoService
from app.services.raw_spill import (
    CIPHERQ_MANIFEST_SUFFIX,
    CIPHERQ_STATE_PENDING,
    CIPHERQ_STATE_SEALED,
    HEADER_QUARANTINE_SUFFIX,
    RawSpillStore,
    is_activity_filename,
)
from app.services.reply_ingest import ReplyIngestService
from app.services.report_ingest import ReportIngestService

FORBIDDEN = ("phone", "payload", "ciphertext", "secret", "key", "body")
PHONE = "13800138000"
SMS_BODY = "您的验证码是123456"


def b64_key(byte: bytes) -> str:
    return base64.b64encode(byte * 32).decode("ascii")


def crypto_v1() -> CryptoService:
    key = b64_key(b"r")
    return CryptoService.from_secret_values(key, key)


def crypto_v2_only() -> CryptoService:
    key = b64_key(b"s")
    ring = json.dumps({"active_version": 2, "keys": {"2": key}})
    return CryptoService.from_secret_values(ring, ring)


def leftover_names(directory: Path, *suffixes: str) -> list[str]:
    return [
        path.name
        for path in directory.iterdir()
        if any(path.name.endswith(suffix) for suffix in suffixes)
    ]


class FakeRepository:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def persist_raw(self, **values: Any) -> int:
        self.events.append(values)
        return len(self.events)

    async def mark_error(self, raw_id: int, error: str) -> None:
        return None


class FakeAlerts:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, **values: Any) -> None:
        self.events.append(values)


def write_spill(
    store: RawSpillStore,
    *,
    source: str = "report",
    payload: bytes = b'{"code":0,"data":[]}',
    digest: str | None = None,
    service: CryptoService | None = None,
) -> Path:
    payload_sha256 = digest or hashlib.sha256(payload).hexdigest()
    return store.write(
        source=source,
        payload_sha256=payload_sha256,
        key_version=1,
        http_status=200,
        content_encoding="identity",
        payload_enc=payload,
        crypto=service or crypto_v1(),
    )


def write_stream(
    store: RawSpillStore,
    *,
    source: str = "report",
    payload: bytes = b'{"code":0,"data":[{"ok":true}]}',
    service: CryptoService | None = None,
) -> Path:
    stream = store.open_stream(source, service or crypto_v1(), capture_bytes=4096)
    stream.announce(http_status=200, content_encoding="identity")
    assert stream.feed(payload) is True
    stream.finish(complete=True, http_status=200, content_encoding="identity")
    return stream.path


def assert_clean_blob(blob: str, work: Path | None = None) -> None:
    assert PHONE not in blob
    assert SMS_BODY not in blob
    if work is not None:
        assert str(work) not in blob
    try:
        parsed = json.loads(blob)
    except json.JSONDecodeError:
        lowered = blob.lower()
        for word in ("phone", "payload", "ciphertext", "secret", "body"):
            assert word not in lowered
        return
    if isinstance(parsed, dict):
        assert set(_walk_keys(parsed)).isdisjoint(FORBIDDEN)


def _walk_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        keys.update(value)
        for item in value.values():
            keys.update(_walk_keys(item))
    elif isinstance(value, list):
        for item in value:
            keys.update(_walk_keys(item))
    return keys


def assert_manifests_clean(directory: Path) -> None:
    for path in directory.glob(f"*{CIPHERQ_MANIFEST_SUFFIX}"):
        if path.name.endswith(".tmp"):
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        text = path.read_text(encoding="utf-8")
        assert_clean_blob(text)
        assert "/" not in str(raw.get("src_name", ""))
        assert "\\" not in str(raw.get("src_name", ""))
        assert set(raw).isdisjoint(set(FORBIDDEN))


def recover_report(
    store: RawSpillStore, service: CryptoService
) -> tuple[int, FakeRepository, FakeAlerts]:
    repository = FakeRepository()
    alerts = FakeAlerts()
    recovered = asyncio.run(
        ReportIngestService(
            None,  # type: ignore[arg-type]
            repository,
            service,
            alerts=alerts,
            spill=store,
        ).recover_spills()
    )
    return recovered, repository, alerts


def recover_reply(
    store: RawSpillStore, service: CryptoService
) -> tuple[int, FakeRepository, FakeAlerts]:
    repository = FakeRepository()
    alerts = FakeAlerts()
    recovered = asyncio.run(
        ReplyIngestService(
            None,
            repository,
            service,
            alerts=alerts,
            spill=store,
        ).recover_spills()
    )
    return recovered, repository, alerts


def test_v1_spill_survives_v2_only_reclaim_and_recovers(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path, header_only_min_age_s=0)
    payload = b'{"code":0,"data":[{"ok":true}]}'
    digest = hashlib.sha256(payload).hexdigest()
    write_spill(store, payload=payload, digest=digest)
    result = store.reclaim_idle("report", crypto_v2_only())
    assert result.key_unavailable >= 1
    assert leftover_names(tmp_path, ".spill") == []
    assert leftover_names(tmp_path, ".cq")
    assert_manifests_clean(tmp_path)
    recovered, repository, alerts = recover_report(store, crypto_v1())
    assert recovered == 1
    assert repository.events[0]["payload_sha256"] == digest
    assert leftover_names(tmp_path, ".spill") == []
    for item in alerts.events:
        assert_clean_blob(json.dumps(item, ensure_ascii=False), tmp_path)


def test_v1_stream_survives_v2_only_reclaim_and_recovers(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path, header_only_min_age_s=0)
    payload = b'{"code":0,"data":[{"ok":true}]}'
    write_stream(store, payload=payload)
    result = store.reclaim_idle("report", crypto_v2_only())
    assert result.key_unavailable >= 1
    assert leftover_names(tmp_path, ".stream", ".tmp") == []
    assert leftover_names(tmp_path, ".cq")
    recovered, repository, _alerts = recover_report(store, crypto_v1())
    assert recovered == 1
    assert repository.events[0]["payload_sha256"] == hashlib.sha256(payload).hexdigest()


def test_eio_on_header_read_keeps_ciphertext(tmp_path: Path, monkeypatch: Any) -> None:
    store = RawSpillStore(tmp_path, header_only_min_age_s=0)
    payload = b"keep-after-eio"
    digest = hashlib.sha256(payload).hexdigest()
    path = write_spill(store, payload=payload, digest=digest)
    original = path.read_bytes()
    real_open = Path.open

    def eio(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.suffix == ".spill":
            raise OSError(errno.EIO, "injected eio")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", eio)
    result = store.reclaim_idle("report", crypto_v1())
    monkeypatch.setattr(Path, "open", real_open)
    assert result.transient_io >= 1
    assert path.exists()
    assert path.read_bytes() == original
    recovered, repository, _alerts = recover_report(store, crypto_v1())
    assert recovered == 1
    assert repository.events[0]["payload_sha256"] == digest


def test_eacces_on_header_read_keeps_ciphertext(tmp_path: Path, monkeypatch: Any) -> None:
    store = RawSpillStore(tmp_path, header_only_min_age_s=0)
    payload = b"keep-after-eacces"
    digest = hashlib.sha256(payload).hexdigest()
    path = write_spill(store, payload=payload, digest=digest)
    original = path.read_bytes()
    real_open = Path.open

    def eacces(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.suffix == ".spill":
            raise OSError(errno.EACCES, "injected eacces")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", eacces)
    result = store.reclaim_idle("report", crypto_v1())
    monkeypatch.setattr(Path, "open", real_open)
    assert result.transient_io >= 1
    assert path.exists()
    assert path.read_bytes() == original
    recovered, repository, _alerts = recover_report(store, crypto_v1())
    assert recovered == 1
    assert repository.events[0]["payload_sha256"] == digest


def test_bit_flip_enters_cipherq_with_original_bytes(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path, header_only_min_age_s=0)
    path = write_spill(store, payload=b"alpha-body", digest="a" * 64)
    flipped = bytearray(path.read_bytes())
    flipped[-1] ^= 0x01
    path.write_bytes(flipped)
    expected = hashlib.sha256(flipped).hexdigest()
    result = store.reclaim_idle("report", crypto_v1())
    assert result.auth_failed >= 1
    assert leftover_names(tmp_path, ".spill") == []
    cq_names = leftover_names(tmp_path, ".cq")
    assert len(cq_names) == 1
    cq = tmp_path / cq_names[0]
    assert hashlib.sha256(cq.read_bytes()).hexdigest() == expected
    assert_manifests_clean(tmp_path)


def test_isolate_kill_after_manifest_before_rename_is_reentrant(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path, header_only_min_age_s=0)
    path = write_spill(store, payload=b"reentrant", digest="b" * 64)
    raw = path.read_bytes()
    token = "b" * 64
    manifest = {
        "isolated_at": "2026-08-24T13:00:00+08:00",
        "kind": "spill",
        "reason": "auth_failed",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
        "source": "report",
        "src_name": path.name,
        "state": CIPHERQ_STATE_PENDING,
        "token": token,
    }
    (tmp_path / f"report-{token}{CIPHERQ_MANIFEST_SUFFIX}").write_text(
        json.dumps(manifest, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    store.reclaim_idle("report", crypto_v1())
    assert not path.exists()
    dest = tmp_path / f"report-{token}.cq"
    assert dest.exists()
    assert dest.read_bytes() == raw
    sealed = json.loads((tmp_path / f"report-{token}{CIPHERQ_MANIFEST_SUFFIX}").read_text())
    assert sealed["state"] == CIPHERQ_STATE_SEALED


def test_enospc_on_manifest_write_leaves_source(tmp_path: Path, monkeypatch: Any) -> None:
    store = RawSpillStore(tmp_path, header_only_min_age_s=0)
    path = write_spill(store, payload=b"keep-on-enospc", digest="c" * 64)
    original = path.read_bytes()
    real_open = Path.open

    def enospc(self: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if self.name.endswith(".cq.man.tmp") and any(flag in mode for flag in "wax+"):
            raise OSError(errno.ENOSPC, "injected enospc")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", enospc)
    path_after_flip = path
    flipped = bytearray(original)
    flipped[-1] ^= 0x01
    path.write_bytes(flipped)
    store.reclaim_idle("report", crypto_v1())
    monkeypatch.setattr(Path, "open", real_open)
    assert path_after_flip.exists()
    assert leftover_names(tmp_path, ".cq") == []


def test_legal_header_only_still_deleted_after_age(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path, header_only_min_age_s=0)
    stream = store.open_stream("report", crypto_v1(), capture_bytes=4096)
    result = store.reclaim_idle("report", crypto_v1())
    assert result.header_only == 1
    assert not stream.path.exists()
    assert leftover_names(tmp_path, ".cq") == []
    assert leftover_names(tmp_path, HEADER_QUARANTINE_SUFFIX) == []


def test_authenticated_prefix_still_not_deleted_as_empty(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path, header_only_min_age_s=0)
    stream = store.open_stream("report", crypto_v1(), capture_bytes=4096)
    stream.announce(http_status=200, content_encoding="identity")
    assert stream.feed(b'{"code":0,"data":[{"ok":true}]}') is True
    assert stream.flush() is True
    with stream.path.open("ab") as handle:
        handle.write(b"\x00\x00\x00\x10partial-unauth")
        handle.flush()
        os.fsync(handle.fileno())
    result = store.reclaim_idle("report", crypto_v1())
    assert result.header_only == 0
    assert stream.path.exists()
    recovered = store.list_pending_streams(crypto_v1())
    assert recovered
    assert recovered[0].plaintext_bytes > 0


def test_cipherq_excluded_from_activity_quota(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path, max_pending_files=32)
    for index in range(40):
        token = f"{index:032x}"
        (tmp_path / f"report-{token}.cq").write_bytes(b"x" * 64)
        (tmp_path / f"report-{token}{CIPHERQ_MANIFEST_SUFFIX}").write_text(
            json.dumps(
                {
                    "isolated_at": "2026-08-24T13:00:00+08:00",
                    "kind": "spill",
                    "reason": "auth_failed",
                    "sha256": "a" * 64,
                    "size_bytes": 64,
                    "source": "report",
                    "src_name": f"report-{'a' * 64}.spill",
                    "state": CIPHERQ_STATE_SEALED,
                    "token": token,
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    assert store.pending_count() == 0
    assert not any(is_activity_filename(name) for name in leftover_names(tmp_path, ".cq"))
    stream = store.open_stream("reply", crypto_v1(), capture_bytes=4096)
    assert stream.path.exists()
    assert store.pending_count() == 1


def test_cipherq_capacity_does_not_evict_key_unavailable(tmp_path: Path) -> None:
    store = RawSpillStore(
        tmp_path,
        header_only_min_age_s=0,
        max_cipherq_files=2,
        max_cipherq_bytes=8 * 1024 * 1024,
    )
    first = write_spill(store, payload=b"v1-one", digest="1" * 64)
    second = write_spill(store, payload=b"v1-two", digest="2" * 64)
    store.reclaim_idle("report", crypto_v2_only())
    assert leftover_names(tmp_path, ".cq")
    kept = {name for name in leftover_names(tmp_path, ".cq")}
    assert len(kept) == 2
    flipped = write_spill(store, payload=b"bit-flip", digest="3" * 64)
    raw = bytearray(flipped.read_bytes())
    raw[-1] ^= 0x01
    flipped.write_bytes(raw)
    store.reclaim_idle("report", crypto_v1())
    assert kept <= set(leftover_names(tmp_path, ".cq"))
    assert first.name not in leftover_names(tmp_path, ".spill")
    assert second.name not in leftover_names(tmp_path, ".spill")
    assert flipped.exists()


def test_alerts_logs_manifests_have_no_sensitive_material(
    tmp_path: Path, caplog: Any
) -> None:
    store = RawSpillStore(tmp_path, header_only_min_age_s=0)
    write_spill(store, payload=b"quiet-body", digest="d" * 64)
    with caplog.at_level(logging.WARNING, logger="app.services.raw_spill"):
        store.reclaim_idle("report", crypto_v2_only())
        _recovered, _repository, alerts = recover_report(store, crypto_v1())
    assert_manifests_clean(tmp_path)
    for record in caplog.records:
        assert_clean_blob(record.getMessage(), tmp_path)
        if record.__dict__.get("src_name"):
            assert "/" not in str(record.__dict__["src_name"])
    for item in alerts.events:
        blob = json.dumps(item, ensure_ascii=False)
        assert_clean_blob(blob, tmp_path)
        detail = item.get("detail") or {}
        assert "/" not in json.dumps(detail, ensure_ascii=False)


def test_report_and_reply_share_volume_across_key_rotation(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path, header_only_min_age_s=0)
    report_payload = b'{"code":0,"data":[{"report":true}]}'
    reply_payload = b'{"code":0,"data":[{"reply":true}]}'
    report_digest = hashlib.sha256(report_payload).hexdigest()
    reply_digest = hashlib.sha256(reply_payload).hexdigest()
    write_spill(store, source="report", payload=report_payload, digest=report_digest)
    write_spill(store, source="reply", payload=reply_payload, digest=reply_digest)
    store.reclaim_idle("report", crypto_v2_only())
    assert leftover_names(tmp_path, ".spill") == []
    assert len(leftover_names(tmp_path, ".cq")) == 2
    report_n, report_repo, _alerts = recover_report(store, crypto_v1())
    reply_n, reply_repo, _alerts = recover_reply(store, crypto_v1())
    assert report_n == 1
    assert reply_n == 1
    assert report_repo.events[0]["payload_sha256"] == report_digest
    assert reply_repo.events[0]["payload_sha256"] == reply_digest
