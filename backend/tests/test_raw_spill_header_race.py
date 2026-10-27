"""活动 .hdr 不得被另一来源回收，崩溃留下的合法头仍可有界恢复。"""

from __future__ import annotations

import base64
import fcntl
import multiprocessing
import os
import threading
import time
from pathlib import Path

import pytest

from app.services.crypto import CryptoService
from app.services.raw_spill import RawSpillStore
from app.services.raw_spill_format import QUOTA_LOCK_NAME


def crypto() -> CryptoService:
    key = base64.b64encode(b"r" * 32).decode()
    return CryptoService.from_secret_values(key, key)


def _close_lock_probe(directory: Path) -> bool:
    handle = (directory / QUOTA_LOCK_NAME).open("a+b")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        handle.close()


def age_path(path: Path, seconds: float) -> None:
    stamped = time.time() - seconds
    os.utime(path, times=(stamped, stamped))


def _peer_open(directory: str, source: str) -> None:
    store = RawSpillStore(Path(directory))
    stream = store.open_stream(source, crypto(), capture_bytes=4096)
    stream.discard()


@pytest.mark.parametrize(
    ("writer", "peer"),
    [("reply", "report"), ("report", "reply")],
)
def test_peer_process_leaves_fresh_header_and_stale_header_recovers(
    tmp_path: Path,
    writer: str,
    peer: str,
) -> None:
    store = RawSpillStore(tmp_path, header_only_min_age_s=30)
    stream = store.open_stream(writer, crypto(), capture_bytes=4096)
    stream.announce(http_status=200, content_encoding="identity")
    snapshot = stream.path.read_bytes()
    hdr = stream.path.with_name(stream.path.name + ".hdr")
    hdr.write_bytes(snapshot)

    context = multiprocessing.get_context("fork")
    process = context.Process(target=_peer_open, args=(str(tmp_path), peer))
    process.start()
    process.join(10)
    assert process.exitcode == 0
    assert hdr.is_file()
    assert stream.path.read_bytes() == snapshot

    age_path(hdr, 120)
    store.reclaim_idle(peer, crypto())
    assert not hdr.exists()
    assert stream.path.read_bytes() == snapshot
    recovered = store.list_pending_streams(crypto())
    assert any(item.http_status == 200 and item.source == writer for item in recovered)


def test_header_rewrite_holds_directory_lock_until_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RawSpillStore(tmp_path)
    stream = store.open_stream("reply", crypto(), capture_bytes=4096)
    seen = threading.Event()
    original = os.replace

    def wrapped(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        if str(src).endswith(".hdr"):
            box: dict[str, bool] = {}
            done = threading.Event()

            def probe() -> None:
                box["busy"] = _close_lock_probe(tmp_path)
                done.set()

            threading.Thread(target=probe).start()
            assert done.wait(2)
            assert box["busy"] is True
            seen.set()
        original(src, dst)

    monkeypatch.setattr(os, "replace", wrapped)
    stream.announce(http_status=200, content_encoding="identity")
    assert seen.is_set()
    assert stream.has_announced is True
    assert not stream.path.with_name(stream.path.name + ".hdr").exists()


def test_missing_header_replace_is_not_treated_as_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RawSpillStore(tmp_path)
    stream = store.open_stream("report", crypto(), capture_bytes=4096)
    original = os.replace

    def wrapped(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        if str(src).endswith(".hdr"):
            Path(src).unlink(missing_ok=True)
            raise FileNotFoundError(2, "missing header", str(src))
        original(src, dst)

    monkeypatch.setattr(os, "replace", wrapped)
    with pytest.raises(FileNotFoundError):
        stream.announce(http_status=200, content_encoding="identity")
    assert stream.has_announced is False
