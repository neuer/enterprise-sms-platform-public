"""#440：reclaim 锁内只认证 header，并受文件数/时间预算，不得阻塞 Heartbeat。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import time
from pathlib import Path
from typing import Any

import pytest

from app.core.redis_lock import HeartbeatLock
from app.services.crypto import CryptoService
from app.services.raw_spill import (
    DEFAULT_RECLAIM_MAX_HEADER_BYTES,
    DEFAULT_RECLAIM_MAX_SECONDS,
    RawSpillStore,
    ReclaimRoundBudget,
)
from app.services.report_ingest import ReportIngestService


def crypto() -> CryptoService:
    key = base64.b64encode(b"r" * 32).decode()
    return CryptoService.from_secret_values(key, key)


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


def _write_spill(store: RawSpillStore, payload: bytes, *, source: str = "report") -> Path:
    return store.write(
        source=source,
        payload_sha256=hashlib.sha256(payload).hexdigest(),
        key_version=1,
        http_status=200,
        content_encoding="identity",
        payload_enc=payload,
        crypto=crypto(),
    )


def test_reclaim_io_independent_of_backlog_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RawSpillStore(tmp_path, header_only_min_age_s=0)
    payload = b"Z" * (64 * 1024)
    for index in range(8):
        _write_spill(store, payload + f"{index}".encode())
    read_bytes = {"n": 0}
    real_open = Path.open

    def counting_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        handle = real_open(self, *args, **kwargs)
        if self.parent == tmp_path and self.name.endswith(".spill"):
            inner = handle.read

            def tracked(size: int | None = -1) -> bytes:
                data = inner() if size is None or size < 0 else inner(size)
                read_bytes["n"] += len(data)
                return data

            handle.read = tracked  # type: ignore[method-assign]
        return handle

    monkeypatch.setattr(Path, "open", counting_open)
    store.reclaim_idle("report", crypto())
    assert read_bytes["n"] < 16 * 1024
    assert read_bytes["n"] < 8 * len(payload)


def test_eight_gib_standin_reclaim_is_budgeted(tmp_path: Path) -> None:
    store = RawSpillStore(
        tmp_path,
        header_only_min_age_s=0,
        reclaim_budget=ReclaimRoundBudget(max_files=3, max_header_bytes=4096, max_seconds=2.0),
    )
    payload = b"G" * 4096
    for index in range(8):
        _write_spill(store, payload + bytes([index]))
    inspected = {"n": 0}
    original = store._inspect_spill_header_auth

    def counted(path: Path, crypto_service: CryptoService) -> str | None:
        inspected["n"] += 1
        return original(path, crypto_service)

    store._inspect_spill_header_auth = counted  # type: ignore[method-assign]
    store.reclaim_idle("report", crypto())
    assert inspected["n"] <= 3
    assert store.pending_count() == 8


def test_reclaim_does_not_payload_read_foreign_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RawSpillStore(tmp_path, header_only_min_age_s=0)
    _write_spill(store, b"report-header", source="report")
    _write_spill(store, b"Y" * 32_768, source="reply")
    payload_reads: list[str] = []
    original = store._probe_payload_read

    def tracked(name: str) -> None:
        payload_reads.append(name)
        original(name)

    store._probe_payload_read = tracked  # type: ignore[method-assign]
    store.reclaim_idle("report", crypto())
    assert payload_reads == []


@pytest.mark.asyncio
async def test_slow_reclaim_allows_redis_heartbeat(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path)
    _write_spill(store, b"slow-disk")
    original = store.reclaim_idle

    def slow_reclaim(source: str, crypto_service: CryptoService) -> Any:
        time.sleep(0.25)
        return original(source, crypto_service)

    store.reclaim_idle = slow_reclaim  # type: ignore[method-assign]
    beats: list[float] = []

    async def heartbeat() -> None:
        for _ in range(6):
            await asyncio.sleep(0.04)
            beats.append(time.monotonic())

    recovered, _ = await asyncio.gather(
        ReportIngestService(
            None,  # type: ignore[arg-type]
            FakeRepository(),
            crypto(),
            alerts=FakeAlerts(),
            spill=store,
        ).recover_spills(),
        heartbeat(),
    )
    assert recovered == 1
    assert len(beats) >= 3


def test_poll_lock_ttl_has_margin_over_reclaim_budget() -> None:
    assert DEFAULT_RECLAIM_MAX_HEADER_BYTES <= 64 * 1024
    poll_src = Path(__file__).resolve().parents[1] / "app" / "tasks" / "poll_report.py"
    text = poll_src.read_text(encoding="utf-8")
    assert "ttl_s=90" in text
    assert "beat_s=30" in text
    lock = HeartbeatLock(object(), ttl_s=90, beat_s=30)
    assert lock._beat_s < lock._ttl_s
    assert lock._beat_s > DEFAULT_RECLAIM_MAX_SECONDS
    assert lock._ttl_s - lock._beat_s >= 60


def test_one_inspect_per_artifact_under_budget(tmp_path: Path) -> None:
    store = RawSpillStore(
        tmp_path,
        reclaim_budget=ReclaimRoundBudget(max_files=2, max_header_bytes=2048, max_seconds=2.0),
    )
    for index in range(4):
        _write_spill(store, f"item-{index}".encode())
    seen: list[str] = []
    original = store._inspect_spill_header_auth

    def counted(path: Path, crypto_service: CryptoService) -> str | None:
        seen.append(path.name)
        return original(path, crypto_service)

    store._inspect_spill_header_auth = counted  # type: ignore[method-assign]
    store.reclaim_idle("report", crypto())
    assert len(seen) == len(set(seen))
    assert 1 <= len(seen) <= 2
