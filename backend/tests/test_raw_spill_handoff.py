from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.services.crypto import CryptoService
from app.services.raw_spill import (
    CAPTURE_COMPLETE,
    CAPTURE_PROTOCOL_INVALID,
    CAPTURE_TRUNCATED,
    STREAM_RECORD_HEADER,
    RawSpillStore,
    SpillQuotaExceeded,
)
from app.services.report_ingest import ReportIngestService
from app.vendor.zhihui import RawPulledPayload, ZhihuiClient


class _RawStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk


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
    def __init__(self, result: RawPulledPayload) -> None:
        self.result = result

    async def get_report_raw(self, body_sink: Any | None = None) -> RawPulledPayload:
        if body_sink is not None:
            announce = getattr(body_sink, "announce", None)
            if callable(announce):
                announce(
                    http_status=self.result.status_code,
                    content_encoding=self.result.content_encoding,
                    protocol_invalid=self.result.protocol_invalid,
                )
            if self.result.raw_payload:
                body_sink.feed(self.result.raw_payload)
            finish = getattr(body_sink, "finish", None)
            if callable(finish):
                finish(
                    complete=True,
                    http_status=self.result.status_code,
                    content_encoding=self.result.content_encoding,
                    protocol_invalid=self.result.protocol_invalid,
                )
        return self.result


class FailingSecondarySpill(RawSpillStore):
    def __init__(self, directory: Path, *, error: Exception) -> None:
        super().__init__(directory)
        self.error = error

    def write(self, **values: Any) -> Path:
        raise self.error


def _payload_sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def test_partial_terminal_after_zero_marker_recovers_authenticated_chunks(
    tmp_path: Path,
) -> None:
    store = RawSpillStore(tmp_path)
    stream = store.open_stream("report", crypto())
    stream.announce(http_status=500, content_encoding="identity")
    raw = b'{"code":0,"data":[{"ok":true}]}'
    assert stream.feed(raw) is True
    assert stream.flush() is True
    before = stream.path.stat().st_size
    with stream.path.open("ab") as handle:
        handle.write(STREAM_RECORD_HEADER.pack(0))
        handle.write(b'{"capture_state":"comp')
        handle.flush()
        os.fsync(handle.fileno())
    assert stream.path.stat().st_size > before
    recovered = store.list_pending_streams(crypto())
    assert len(recovered) == 1
    assert recovered[0].capture_state == CAPTURE_TRUNCATED
    assert recovered[0].http_status == 500
    assert recovered[0].quarantined is True
    assert recovered[0].payload_sha256 == _payload_sha(raw)
    assert leftover_names(tmp_path, ".quarantine")


def test_terminal_byte_boundary_kill_yields_complete_or_truncated(tmp_path: Path) -> None:
    store = RawSpillStore(tmp_path)
    stream = store.open_stream("report", crypto())
    stream.announce(http_status=429, content_encoding="unsupported")
    raw = b'{"code":0,"data":[]}'
    assert stream.feed(raw) is True
    assert stream.flush() is True
    before = stream.path.stat().st_size
    stream.finish(complete=True, http_status=429, content_encoding="unsupported")
    complete = stream.path.read_bytes()
    stream_id = stream.stream_id
    stream.path.unlink()
    tmp = store._stream_tmp("report", stream_id)
    expected = _payload_sha(raw)
    for size in range(before, len(complete) + 1):
        tmp.write_bytes(complete[:size])
        recovered = store.list_pending_streams(crypto())
        assert len(recovered) == 1, size
        assert recovered[0].payload_sha256 == expected
        assert recovered[0].http_status == 429
        assert recovered[0].content_encoding == "unsupported"
        if size == len(complete):
            assert recovered[0].capture_state == CAPTURE_COMPLETE
            assert recovered[0].quarantined is False
        else:
            assert recovered[0].capture_state == CAPTURE_TRUNCATED
            assert recovered[0].quarantined is True


@pytest.mark.parametrize(
    "status,encoding",
    [(500, "identity"), (429, "identity"), (204, "identity"), (200, "unsupported")],
)
def test_http_metadata_survives_authenticated_recovery(
    tmp_path: Path,
    status: int,
    encoding: str,
) -> None:
    store = RawSpillStore(tmp_path)
    stream = store.open_stream("reply", crypto())
    stream.announce(http_status=status, content_encoding=encoding)
    if status != 204:
        assert stream.feed(b'{"code":0,"data":[]}') is True
    stream.finish(complete=True, http_status=status, content_encoding=encoding)
    recovered = store.list_pending_streams(crypto())
    assert len(recovered) == 1
    assert recovered[0].capture_state == CAPTURE_COMPLETE
    assert recovered[0].http_status == status
    assert recovered[0].content_encoding == encoding


def test_finish_fsync_failure_does_not_skip_authenticated_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RawSpillStore(tmp_path)
    stream = store.open_stream("report", crypto())
    stream.announce(http_status=500, content_encoding="identity")
    raw = b'{"code":0,"data":[1]}'
    assert stream.feed(raw) is True
    assert stream.flush() is True
    real_fsync = os.fsync

    def boom(fd: int) -> None:
        raise OSError("injected fsync")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError, match="injected fsync"):
        stream.finish(complete=True, http_status=500, content_encoding="identity")
    monkeypatch.setattr(os, "fsync", real_fsync)
    recovered = store.list_pending_streams(crypto())
    assert len(recovered) == 1
    assert recovered[0].capture_state in {CAPTURE_COMPLETE, CAPTURE_TRUNCATED}
    assert recovered[0].http_status == 500
    assert recovered[0].payload_sha256 == _payload_sha(raw)


def test_finish_rename_failure_does_not_skip_authenticated_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RawSpillStore(tmp_path)
    stream = store.open_stream("report", crypto())
    stream.announce(http_status=204, content_encoding="identity")
    raw = b'{"code":0,"data":[]}'
    assert stream.feed(raw) is True
    real_replace = os.replace

    def boom(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        raise OSError("injected rename")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="injected rename"):
        stream.finish(complete=True, http_status=204, content_encoding="identity")
    monkeypatch.setattr(os, "replace", real_replace)
    recovered = store.list_pending_streams(crypto())
    assert len(recovered) == 1
    assert recovered[0].capture_state in {CAPTURE_COMPLETE, CAPTURE_TRUNCATED}
    assert recovered[0].http_status == 204
    assert recovered[0].payload_sha256 == _payload_sha(raw)


def test_injected_failure_after_zero_marker_keeps_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RawSpillStore(tmp_path)
    stream = store.open_stream("report", crypto())
    stream.announce(http_status=500, content_encoding="identity")
    raw = b'{"code":0,"data":[]}'
    assert stream.feed(raw) is True

    def partial(self: Any, **_: Any) -> None:
        with self.path.open("ab") as handle:
            handle.write(STREAM_RECORD_HEADER.pack(0))
            handle.write(b"\n")
            handle.flush()
        raise OSError("injected after marker")

    monkeypatch.setattr(type(stream), "_write_control_frame", partial)
    with pytest.raises(OSError, match="injected after marker"):
        stream.finish(complete=True, http_status=500)
    recovered = store.list_pending_streams(crypto())
    assert len(recovered) == 1
    assert recovered[0].capture_state == CAPTURE_TRUNCATED
    assert recovered[0].payload_sha256 == _payload_sha(raw)
    assert recovered[0].quarantined is True


@pytest.mark.asyncio
async def test_recover_spills_persists_quarantined_truncated_and_alerts(
    tmp_path: Path,
) -> None:
    store = RawSpillStore(tmp_path)
    stream = store.open_stream("report", crypto())
    stream.announce(http_status=500, content_encoding="identity")
    raw = b'{"code":0,"data":[]}'
    assert stream.feed(raw) is True
    assert stream.flush() is True
    with stream.path.open("ab") as handle:
        handle.write(STREAM_RECORD_HEADER.pack(0))
        handle.write(b'{"capture_state":"comp')
        handle.flush()
        os.fsync(handle.fileno())
    repository = FakeRepository()
    alerts = FakeAlerts()
    service = ReportIngestService(
        None,
        repository,
        crypto(),
        alerts=alerts,
        spill=store,
    )
    assert await service.recover_spills() == 1
    persisted = [value for event, value in repository.events if event == "persist_raw"]
    assert persisted[0]["capture_state"] == CAPTURE_TRUNCATED
    assert persisted[0]["http_status"] == 500
    assert any("truncated" in error for event, error in repository.events if event == "error")
    assert any(item["alert_type"] == "vendor_raw_stream_quarantined" for item in alerts.events)
    assert leftover_names(tmp_path, ".stream", ".stream.tmp", ".quarantine") == []


@pytest.mark.asyncio
async def test_secondary_spill_quota_and_db_failure_keeps_stream(tmp_path: Path) -> None:
    raw = json.dumps({"code": 0, "msg": None, "data": []}).encode()
    repository = FakeRepository(fail_persist=True)
    alerts = FakeAlerts()
    service = ReportIngestService(
        RecordingGateway(RawPulledPayload(raw, 200)),
        repository,
        crypto(),
        alerts=alerts,
        spill=FailingSecondarySpill(tmp_path, error=SpillQuotaExceeded("full")),
    )
    with pytest.raises(RuntimeError, match="db unavailable"):
        await service.poll_once()
    assert leftover_names(tmp_path, ".spill") == []
    assert leftover_names(tmp_path, ".stream", ".stream.tmp")
    repository.fail_persist = False
    assert await service.recover_spills() == 1
    persisted = [value for event, value in repository.events if event == "persist_raw"]
    assert persisted[-1]["http_status"] == 200
    assert leftover_names(tmp_path, ".stream", ".stream.tmp", ".quarantine") == []


@pytest.mark.asyncio
async def test_secondary_spill_oserror_and_db_failure_keeps_stream(tmp_path: Path) -> None:
    raw = json.dumps({"code": 0, "msg": None, "data": []}).encode()
    repository = FakeRepository(fail_persist=True)
    service = ReportIngestService(
        RecordingGateway(RawPulledPayload(raw, 500, "identity")),
        repository,
        crypto(),
        alerts=FakeAlerts(),
        spill=FailingSecondarySpill(tmp_path, error=OSError("disk full")),
    )
    with pytest.raises(RuntimeError, match="db unavailable"):
        await service.poll_once()
    assert leftover_names(tmp_path, ".stream", ".stream.tmp")
    recovered = service.spill.list_pending_streams(crypto()) if service.spill else []
    assert recovered[0].http_status == 500
    assert recovered[0].payload_sha256 == _payload_sha(raw)


@pytest.mark.asyncio
async def test_process_exit_between_discard_and_commit_keeps_secondary_spill(
    tmp_path: Path,
) -> None:
    raw = json.dumps({"code": 0, "msg": None, "data": []}).encode()
    repository = FakeRepository(fail_persist=True)
    service = ReportIngestService(
        RecordingGateway(RawPulledPayload(raw, 200)),
        repository,
        crypto(),
        alerts=FakeAlerts(),
        spill=RawSpillStore(tmp_path),
    )
    with pytest.raises(RuntimeError, match="db unavailable"):
        await service.poll_once()
    assert leftover_names(tmp_path, ".spill")
    assert leftover_names(tmp_path, ".stream", ".stream.tmp") == []
    repository.fail_persist = False
    assert await service.recover_spills() == 1


@pytest.mark.asyncio
async def test_protocol_invalid_pull_is_persisted_and_not_replayed(tmp_path: Path) -> None:
    raw = b'{"code":0,"data":[]}'
    repository = FakeRepository()
    alerts = FakeAlerts()
    count = await ReportIngestService(
        RecordingGateway(RawPulledPayload(raw, 204, "identity", protocol_invalid=True)),
        repository,
        crypto(),
        alerts=alerts,
        spill=RawSpillStore(tmp_path),
    ).poll_once()
    assert count == 0
    persisted = [value for event, value in repository.events if event == "persist_raw"]
    assert persisted[0]["capture_state"] == CAPTURE_PROTOCOL_INVALID
    assert persisted[0]["http_status"] == 204
    assert any(event[0] == "error" for event in repository.events)
    assert any(item["alert_type"] == "vendor_raw_protocol_invalid" for item in alerts.events)
    assert leftover_names(tmp_path, ".stream", ".stream.tmp") == []


@pytest.mark.asyncio
async def test_zhihui_status_and_encoding_survive_stream_recovery(tmp_path: Path) -> None:
    raw = b'{"code":1,"msg":"busy","data":[]}'
    stream_body = _RawStream([raw])

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            headers={"content-encoding": "br"},
            stream=stream_body,
            request=request,
        )

    store = RawSpillStore(tmp_path)
    sink = store.open_stream("report", crypto())
    client = ZhihuiClient(
        base_url="http://vendor.test",
        secret_name="name",
        secret_key="key",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://vendor.test",
        ),
        max_response_body_bytes=64,
        max_response_capture_bytes=128,
        total_timeout_s=1,
    )
    pulled = await client.get_report_raw(body_sink=sink)
    await client.aclose()
    assert pulled.status_code == 500
    assert pulled.content_encoding == "unsupported"
    recovered = store.list_pending_streams(crypto())
    assert recovered[0].http_status == 500
    assert recovered[0].content_encoding == "unsupported"
    assert recovered[0].capture_state == CAPTURE_COMPLETE
    assert recovered[0].payload_sha256 == _payload_sha(raw)


def test_real_restart_recovers_partial_terminal(tmp_path: Path) -> None:
    backend = Path(__file__).resolve().parents[1]
    child = tmp_path / "child.py"
    child.write_text(
        "\n".join(
            [
                "import os, sys",
                f"sys.path.insert(0, {str(backend)!r})",
                "from pathlib import Path",
                "from app.services.crypto import CryptoService",
                "from app.services.raw_spill import RawSpillStore, STREAM_RECORD_HEADER",
                "import base64",
                "key = base64.b64encode(b'r' * 32).decode()",
                "crypto = CryptoService.from_secret_values(key, key)",
                f"store = RawSpillStore(Path({str(tmp_path)!r}))",
                "stream = store.open_stream('report', crypto)",
                "stream.announce(http_status=500, content_encoding='identity')",
                'stream.feed(b\'{"code":0,"data":[1]}\')',
                "assert stream.flush() is True",
                "with stream.path.open('ab') as handle:",
                "    handle.write(STREAM_RECORD_HEADER.pack(0))",
                '    handle.write(b\'{"capture_state":"complete"\')',
                "    handle.flush()",
                "    os.fsync(handle.fileno())",
            ]
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, str(child)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    recovered = RawSpillStore(tmp_path).list_pending_streams(crypto())
    assert len(recovered) == 1
    assert recovered[0].capture_state == CAPTURE_TRUNCATED
    assert recovered[0].http_status == 500
    assert recovered[0].quarantined is True
    assert recovered[0].payload_sha256 == _payload_sha(b'{"code":0,"data":[1]}')
