from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from app.services.crypto import CryptoService
from app.services.raw_replay import RawReplayConflict, RawReplayRecord, RawReplayService
from app.services.raw_spill import (
    CAPTURE_COMPLETE_TOO_LARGE,
    CAPTURE_TRUNCATED,
    RawSpillStore,
    SpillQuotaExceeded,
)
from app.services.report_ingest import ReportIngestService
from app.vendor.zhihui import RawPulledPayload, VendorResponseTooLarge


def leftover_names(directory: Path, *suffixes: str) -> list[str]:
    return [
        path.name
        for path in directory.iterdir()
        if any(path.name.endswith(suffix) for suffix in suffixes)
    ]


def crypto() -> CryptoService:
    key = base64.b64encode(b"r" * 32).decode()
    return CryptoService.from_secret_values(key, key)


class FakeRepository:
    def __init__(self, *, fail_persist: bool = False) -> None:
        self.fail_persist = fail_persist
        self.events: list[tuple[str, Any]] = []

    async def persist_raw(self, **values: Any) -> int:
        self.events.append(("persist_raw", values))
        if self.fail_persist:
            raise RuntimeError("db unavailable")
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
    def __init__(self, result: RawPulledPayload | VendorResponseTooLarge) -> None:
        self.result = result
        self.calls = 0

    async def get_report_raw(self, body_sink: Any | None = None) -> RawPulledPayload:
        self.calls += 1
        if isinstance(self.result, VendorResponseTooLarge):
            if body_sink is not None and self.result.raw_body:
                body_sink.feed(self.result.raw_body)
                finish = getattr(body_sink, "finish", None)
                if callable(finish):
                    finish(complete=self.result.complete, too_large=self.result.complete)
            raise self.result
        if body_sink is not None:
            body_sink.feed(self.result.raw_payload)
            finish = getattr(body_sink, "finish", None)
            if callable(finish):
                finish(complete=True)
        return self.result


class TruncatedReplayRepository:
    def __init__(self) -> None:
        self.claim_calls: list[int] = []

    async def claim_raw_for_replay(self, raw_id: int) -> object:
        self.claim_calls.append(raw_id)
        return type(
            "Claim",
            (),
            {
                "claimed": False,
                "record": RawReplayRecord(
                    raw_id,
                    "report",
                    b"ciphertext",
                    "a" * 64,
                    1,
                    False,
                    capture_state=CAPTURE_TRUNCATED,
                ),
            },
        )()

    async def mark_replay_error(self, raw_id: int, error: str) -> None:
        return None

    async def audit_raw_replay(self, raw_id: int, **_: Any) -> None:
        return None


def test_stream_survives_mid_receive_crash_as_truncated(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path, max_total_bytes=1024 * 1024, max_pending_files=8)
    stream = store.open_stream("report", crypto())
    assert stream.feed(b'{"code":0,"data":[') is True
    recovered = store.list_pending_streams(crypto())
    assert len(recovered) == 1
    assert recovered[0].capture_state == CAPTURE_TRUNCATED
    assert recovered[0].payload_sha256


def test_finished_complete_stream_round_trips(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path)
    stream = store.open_stream("reply", crypto())
    assert stream.feed(b'{"code":0,"data":[]}') is True
    stream.finish(complete=True, too_large=True, http_status=200)
    recovered = store.list_pending_streams(crypto())
    assert recovered[0].capture_state == CAPTURE_COMPLETE_TOO_LARGE
    assert recovered[0].http_status == 200


def test_spill_quota_rejects_additional_write(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path, max_total_bytes=1024 * 1024, max_pending_files=1)
    store.write(
        source="report",
        payload_sha256="a" * 64,
        key_version=1,
        http_status=200,
        content_encoding="identity",
        payload_enc=b"encrypted-raw",
    )
    with pytest.raises(SpillQuotaExceeded):
        store.write(
            source="reply",
            payload_sha256="b" * 64,
            key_version=1,
            http_status=200,
            content_encoding="identity",
            payload_enc=b"another",
        )
    assert store.can_accept() is False


def test_in_flight_stream_does_not_block_same_capture_spill(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path, max_total_bytes=1024 * 1024, max_pending_files=1)
    stream = store.open_stream("report", crypto())
    assert stream.feed(b'{"code":0,"data":[]}') is True
    path = store.write(
        source="report",
        payload_sha256="c" * 64,
        key_version=1,
        http_status=200,
        content_encoding="identity",
        payload_enc=b"encrypted-same-capture",
    )
    assert path.exists()
    stream.discard()
    assert leftover_names(tmp_path, ".stream", ".stream.tmp") == []
    assert store.pending_spill_count() == 1


@pytest.mark.asyncio
async def test_truncated_report_is_not_normal_replayable(tmp_path: Path) -> None:
    repository = FakeRepository()
    alerts = FakeAlerts()
    gateway = RecordingGateway(
        VendorResponseTooLarge("too large", raw_body=b'{"code":0,"data":[', complete=False)
    )
    with pytest.raises(VendorResponseTooLarge):
        await ReportIngestService(
            gateway,
            repository,
            crypto(),
            alerts=alerts,
            spill=RawSpillStore(tmp_path),
        ).poll_once()

    persisted = [value for event, value in repository.events if event == "persist_raw"]
    assert persisted[0]["capture_state"] == CAPTURE_TRUNCATED
    assert any("truncated" in error for event, error in repository.events if event == "error")
    assert any(item["alert_type"] == "vendor_raw_truncated" for item in alerts.events)
    assert leftover_names(tmp_path, ".stream") == []


@pytest.mark.asyncio
async def test_complete_oversized_report_is_manual_only(tmp_path: Path) -> None:
    repository = FakeRepository()
    alerts = FakeAlerts()
    raw = json.dumps({"code": 0, "msg": None, "data": []}).encode()
    with pytest.raises(VendorResponseTooLarge):
        await ReportIngestService(
            RecordingGateway(VendorResponseTooLarge("too large", raw_body=raw, complete=True)),
            repository,
            crypto(),
            alerts=alerts,
            spill=RawSpillStore(tmp_path),
        ).poll_once()

    persisted = [value for event, value in repository.events if event == "persist_raw"]
    assert persisted[0]["capture_state"] == CAPTURE_COMPLETE_TOO_LARGE
    assert any(item["alert_type"] == "vendor_raw_oversized_complete" for item in alerts.events)


@pytest.mark.asyncio
async def test_spill_success_db_failure_keeps_spill_for_restart(tmp_path: Path) -> None:
    repository = FakeRepository(fail_persist=True)
    alerts = FakeAlerts()
    raw = json.dumps({"code": 0, "msg": None, "data": []}).encode()
    service = ReportIngestService(
        RecordingGateway(RawPulledPayload(raw, 200)),
        repository,
        crypto(),
        alerts=alerts,
        spill=RawSpillStore(tmp_path),
    )
    with pytest.raises(RuntimeError, match="db unavailable"):
        await service.poll_once()

    assert any(item["alert_type"] == "vendor_raw_persist_failed" for item in alerts.events)
    assert leftover_names(tmp_path, ".spill")
    assert leftover_names(tmp_path, ".stream") == []
    repository.fail_persist = False
    assert await service.recover_spills() == 1
    assert leftover_names(tmp_path, ".spill") == []


@pytest.mark.asyncio
async def test_successful_persist_cleans_spill_and_stream(tmp_path: Path) -> None:
    raw = json.dumps({"code": 0, "msg": None, "data": []}).encode()
    service = ReportIngestService(
        RecordingGateway(RawPulledPayload(raw, 200)),
        FakeRepository(),
        crypto(),
        spill=RawSpillStore(tmp_path),
    )
    assert await service.poll_once() == 0
    assert leftover_names(tmp_path, ".spill") == []
    assert leftover_names(tmp_path, ".stream") == []
    assert leftover_names(tmp_path, ".tmp") == []


@pytest.mark.asyncio
async def test_quota_backpressure_skips_vendor_pull(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path, max_total_bytes=1024 * 1024, max_pending_files=1)
    (tmp_path / "blocker.spill").write_bytes(b"not-json")
    gateway = RecordingGateway(RawPulledPayload(b'{"code":0,"data":[]}', 200))
    alerts = FakeAlerts()
    count = await ReportIngestService(
        gateway, FakeRepository(), crypto(), alerts=alerts, spill=store
    ).poll_once()
    assert count == 0
    assert gateway.calls == 0
    assert any(item["alert_type"] == "vendor_raw_spill_quota_exceeded" for item in alerts.events)


@pytest.mark.asyncio
async def test_truncated_raw_replay_is_rejected() -> None:
    service = RawReplayService(
        TruncatedReplayRepository(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )
    with pytest.raises(RawReplayConflict, match="截断"):
        await service.replay(9, actor="admin01", ip="10.0.0.8")
