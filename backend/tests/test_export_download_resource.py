"""预验证资源同一描述符、路径边界与取消时显式关闭。"""
from __future__ import annotations

import asyncio
import os
import threading
from uuid import uuid4

import pytest
from cryptography.exceptions import InvalidTag
from starlette.requests import ClientDisconnect

from app.api.reports import _ExportDownloadResponse, _prepare_export_download
from app.services.export_file import ExportFileCodec
from tests.test_export_file import LEASE_ID, crypto, rows
from tests.test_export_service import principal
from tests.test_reports_api import PUBLIC_ID, FakePrepared, FakeService


@pytest.mark.asyncio
async def test_prepared_stream_never_reopens_validated_path(tmp_path, monkeypatch):
    codec = ExportFileCodec(crypto(), tmp_path, frame_size=32)
    path = await codec.write_csv(9, LEASE_ID, ("phone", "content", "status"), rows())
    prepared = codec.prepare_download(path, expected_task_id=9)
    original = prepared.source.fileno()
    assert prepared.row_count == 2
    prepared.assert_current()

    def reject_reopen(*args, **kwargs):
        raise AssertionError("must not reopen path after validation")

    monkeypatch.setattr(type(path), "open", reject_reopen)
    plaintext = b"".join(prepared.iter_decrypted())
    assert b"13800138000" in plaintext and original >= 0
    assert prepared.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["replace", "truncate", "symlink"])
async def test_final_resource_check_rejects_changed_file(tmp_path, mutation):
    codec = ExportFileCodec(crypto(), tmp_path, frame_size=32)
    path = await codec.write_csv(9, LEASE_ID, ("phone", "content", "status"), rows())
    prepared = codec.prepare_download(path, expected_task_id=9)
    other = await codec.write_csv(9, uuid4(), ("phone", "content", "status"), rows())
    try:
        if mutation == "replace":
            os.replace(other, path)
        elif mutation == "truncate":
            path.write_bytes(b"truncated")
        else:
            path.unlink()
            path.symlink_to(other)
        with pytest.raises((ValueError, OSError)):
            prepared.assert_current()
    finally:
        prepared.close()
    assert prepared.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["header", "truncated", "tag", "symlink", "fifo"])
async def test_invalid_preparation_closes_open_descriptors(tmp_path, monkeypatch, mutation):
    codec = ExportFileCodec(crypto(), tmp_path, frame_size=32)
    path = await codec.write_csv(9, LEASE_ID, ("phone", "content", "status"), rows())
    if mutation == "header":
        path.write_bytes(b"bad")
    elif mutation == "truncated":
        path.write_bytes(path.read_bytes()[:-2])
    elif mutation == "tag":
        payload = bytearray(path.read_bytes())
        payload[20] ^= 1
        path.write_bytes(payload)
    elif mutation == "symlink":
        other = await codec.write_csv(9, uuid4(), ("phone", "content", "status"), rows())
        path.unlink()
        path.symlink_to(other)
    else:
        path.unlink()
        os.mkfifo(path)
    sources = []
    original = os.fdopen

    def tracked(*args, **kwargs):
        source = original(*args, **kwargs)
        sources.append(source)
        return source

    monkeypatch.setattr(os, "fdopen", tracked)
    with pytest.raises((ValueError, OSError, InvalidTag)):
        codec.prepare_download(path, expected_task_id=9)
    assert all(source.closed for source in sources)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["headers", "body"])
async def test_response_send_failure_closes_resource_even_before_iteration(stage, tmp_path):
    codec = ExportFileCodec(crypto(), tmp_path, frame_size=32)
    path = await codec.write_csv(9, LEASE_ID, ("phone", "content", "status"), rows())
    resource = codec.prepare_download(path, expected_task_id=9)
    response = _ExportDownloadResponse(resource, PUBLIC_ID)

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        failure_type = "http.response.start" if stage == "headers" else "http.response.body"
        if message["type"] == failure_type:
            raise OSError("synthetic disconnected client")

    with pytest.raises(ClientDisconnect):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert resource.closed


@pytest.mark.asyncio
async def test_cancelled_preparation_closes_late_thread_result():
    entered, finish = threading.Event(), threading.Event()
    closed = asyncio.Event()

    class NotifyingPrepared(FakePrepared):
        def close(self):
            super().close()
            closed.set()

    prepared = NotifyingPrepared()

    class SlowCodec:
        def prepare_download(self, *args, **kwargs):
            entered.set()
            assert finish.wait(5)
            return prepared

    task_info = await FakeService().get(PUBLIC_ID, principal=principal(11, "viewer", "viewer"))
    task = asyncio.create_task(_prepare_export_download(SlowCodec(), task_info))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    finish.set()
    await asyncio.wait_for(closed.wait(), 5)
    assert prepared.closed
