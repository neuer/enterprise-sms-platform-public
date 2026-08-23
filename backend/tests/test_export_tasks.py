from __future__ import annotations

import base64
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from app.services.crypto import CryptoService
from app.services.export_file import ExportFileCodec
from app.services.export_repository import ExpiredExport, ExportLeaseRef
from app.tasks.export import cleanup_exports_once, dispatch_exports_once
from app.tasks.scheduler import build_beat_schedule

LEASE_ID = UUID("20000000-0000-4000-8000-000000000009")


def crypto() -> CryptoService:
    key = base64.b64encode(b"t" * 32).decode()
    return CryptoService.from_secret_values(key, key)


class FakeRepository:
    def __init__(self) -> None:
        self.cleared: list[tuple[int, str]] = []
        self._expired: list[ExpiredExport] = []
        self._ready: list[ExpiredExport] = []
        self._leases: list[ExportLeaseRef] = []

    async def pending_ids(self) -> list[int]:
        return [3, 5]

    async def retention_days(self) -> int:
        return 7

    async def expired(self, retention_days: int) -> list[ExpiredExport]:
        assert retention_days == 7
        return list(self._expired)

    async def ready_files(self, retention_days: int) -> list[ExpiredExport]:
        assert retention_days == 7
        return list(self._ready)

    async def active_leases(self) -> list[ExportLeaseRef]:
        return list(self._leases)

    async def clear_file(self, task_id: int, file_path: str) -> None:
        self.cleared.append((task_id, file_path))
        self._expired = [
            item
            for item in self._expired
            if not (item.id == task_id and item.file_path == file_path)
        ]

    async def mark_unreadable(self, task_id: int, file_path: str) -> None:
        self._ready = [
            item
            for item in self._ready
            if not (item.id == task_id and item.file_path == file_path)
        ]


class FakeSender:
    def __init__(self) -> None:
        self.ids: list[int] = []

    async def send(self, task_id: int) -> None:
        self.ids.append(task_id)


async def _rows() -> object:
    yield ("138****8000",)


def _age(path: Path, moment: datetime) -> None:
    stamp = moment.timestamp()
    os.utime(path, (stamp, stamp))


@pytest.mark.asyncio
async def test_dispatcher_sends_database_ids_to_bulk_queue_adapter() -> None:
    repository = FakeRepository()
    sender = FakeSender()
    assert await dispatch_exports_once(repository, sender) == 2
    assert sender.ids == [3, 5]


@pytest.mark.asyncio
async def test_cleanup_removes_ciphertext_before_clearing_database_reference(
    tmp_path: Path,
) -> None:
    codec = ExportFileCodec(crypto(), tmp_path)
    path = await codec.write_csv(3, LEASE_ID, ("phone",), _rows())
    events: list[str] = []
    original = codec.remove_artifact

    def tracked(raw_path: str | Path) -> None:
        events.append(f"remove:{Path(raw_path).name}")
        original(raw_path)

    codec.remove_artifact = tracked  # type: ignore[method-assign]
    repository = FakeRepository()
    repository._expired = [ExpiredExport(3, str(path))]
    original_clear = repository.clear_file

    async def clear_file(task_id: int, file_path: str) -> None:
        events.append(f"clear:{task_id}")
        await original_clear(task_id, file_path)

    repository.clear_file = clear_file  # type: ignore[method-assign]
    assert await cleanup_exports_once(repository, codec) == 1
    assert events == [f"remove:{path.name}", "clear:3"]
    assert not path.exists()
    assert repository.cleared == [(3, str(path))]


def test_export_beat_tasks_use_bulk_queue_and_fixed_intervals() -> None:
    schedule = build_beat_schedule({})
    assert schedule["dispatch-exports"] == {
        "task": "app.tasks.dispatch_exports",
        "schedule": 60,
        "options": {"queue": "bulk"},
    }
    assert schedule["cleanup-exports"] == {
        "task": "app.tasks.cleanup_exports",
        "schedule": 3600,
        "options": {"queue": "bulk"},
    }


@pytest.mark.asyncio
async def test_cleanup_converges_when_interrupted_between_delete_and_clear(
    tmp_path: Path,
) -> None:
    codec = ExportFileCodec(crypto(), tmp_path)
    path = await codec.write_csv(3, LEASE_ID, ("phone",), _rows())
    repository = FakeRepository()
    repository._expired = [ExpiredExport(3, str(path))]
    codec.remove_artifact(path)
    assert not path.exists()

    assert await cleanup_exports_once(repository, codec) == 1
    assert repository.cleared == [(3, str(path))]
    assert await cleanup_exports_once(repository, codec) == 0


@pytest.mark.asyncio
async def test_cleanup_expires_directory_orphans_after_retention(
    tmp_path: Path,
) -> None:
    codec = ExportFileCodec(crypto(), tmp_path)
    old_lease = UUID("30000000-0000-4000-8000-000000000009")
    orphan = await codec.write_csv(4, old_lease, ("phone",), _rows())
    part = tmp_path / f"export-4-{old_lease}.part"
    part.write_bytes(b"partial")
    aged = datetime(2026, 1, 1, tzinfo=UTC)
    _age(orphan, aged)
    _age(part, aged)

    repository = FakeRepository()
    now = datetime(2026, 1, 10, tzinfo=UTC)
    assert await cleanup_exports_once(repository, codec, now=now) == 2
    assert not orphan.exists()
    assert not part.exists()
