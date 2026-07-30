from __future__ import annotations

from pathlib import Path

import pytest

from app.services.export_repository import ExpiredExport
from app.tasks.export import cleanup_exports_once, dispatch_exports_once
from app.tasks.scheduler import build_beat_schedule


class FakeRepository:
    def __init__(self) -> None:
        self.cleared: list[tuple[int, str]] = []

    async def pending_ids(self) -> list[int]:
        return [3, 5]

    async def retention_days(self) -> int:
        return 7

    async def expired(self, retention_days: int) -> list[ExpiredExport]:
        assert retention_days == 7
        return [ExpiredExport(3, "/safe/export-3.smsx")]

    async def clear_file(self, task_id: int, file_path: str) -> None:
        self.cleared.append((task_id, file_path))


class FakeSender:
    def __init__(self) -> None:
        self.ids: list[int] = []

    async def send(self, task_id: int) -> None:
        self.ids.append(task_id)


class FakeCodec:
    def __init__(self) -> None:
        self.removed: list[str] = []

    def remove(self, raw_path: str | Path) -> None:
        self.removed.append(str(raw_path))


@pytest.mark.asyncio
async def test_dispatcher_sends_database_ids_to_bulk_queue_adapter() -> None:
    repository = FakeRepository()
    sender = FakeSender()
    assert await dispatch_exports_once(repository, sender) == 2
    assert sender.ids == [3, 5]


@pytest.mark.asyncio
async def test_cleanup_removes_ciphertext_before_clearing_database_reference() -> None:
    repository = FakeRepository()
    codec = FakeCodec()
    assert await cleanup_exports_once(repository, codec) == 1
    assert codec.removed == ["/safe/export-3.smsx"]
    assert repository.cleared == [(3, "/safe/export-3.smsx")]


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
