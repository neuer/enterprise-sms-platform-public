from __future__ import annotations

import base64
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from app.services.crypto import CryptoService
from app.services.export_file import QUARANTINE_DIR_NAME, ExportFileCodec
from app.services.export_reconcile import reconcile_export_storage
from app.services.export_repository import ExpiredExport, ExportLeaseRef

LEASE_A = UUID("20000000-0000-4000-8000-000000000009")
LEASE_B = UUID("30000000-0000-4000-8000-000000000009")


def crypto() -> CryptoService:
    key = base64.b64encode(b"r" * 32).decode()
    return CryptoService.from_secret_values(key, key)


async def rows() -> AsyncIterator[tuple[object, ...]]:
    yield ("138****8000",)


class FakeRepository:
    def __init__(self) -> None:
        self.cleared: list[tuple[int, str]] = []
        self.unreadables: list[tuple[int, str]] = []
        self._expired: list[ExpiredExport] = []
        self._ready: list[ExpiredExport] = []
        self._leases: list[ExportLeaseRef] = []

    async def retention_days(self) -> int:
        return 7

    async def expired(self, retention_days: int) -> list[ExpiredExport]:
        return list(self._expired)

    async def ready_files(self, retention_days: int) -> list[ExpiredExport]:
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
        self.unreadables.append((task_id, file_path))
        self._ready = [
            item
            for item in self._ready
            if not (item.id == task_id and item.file_path == file_path)
        ]


def _age(path: Path, moment: datetime) -> None:
    stamp = moment.timestamp()
    os.utime(path, (stamp, stamp))


@pytest.mark.asyncio
async def test_ready_missing_file_becomes_failed_not_downloadable(tmp_path: Path) -> None:
    codec = ExportFileCodec(crypto(), tmp_path)
    missing = tmp_path / f"export-9-{LEASE_A}.smsx"
    repository = FakeRepository()
    repository._ready = [ExpiredExport(9, str(missing))]

    stats = await reconcile_export_storage(repository, codec)

    assert stats.missing_failed == 1
    assert repository.unreadables == [(9, str(missing))]
    assert repository._ready == []


@pytest.mark.asyncio
async def test_ready_wrong_identity_is_quarantined_and_failed(tmp_path: Path) -> None:
    codec = ExportFileCodec(crypto(), tmp_path)
    path = await codec.write_csv(10, LEASE_A, ("phone",), rows())
    repository = FakeRepository()
    repository._ready = [ExpiredExport(9, str(path))]

    stats = await reconcile_export_storage(repository, codec)

    assert stats.unreadable_failed == 1
    assert stats.quarantined == 1
    assert repository.unreadables == [(9, str(path))]
    assert not path.exists()
    quarantined = tmp_path / QUARANTINE_DIR_NAME / path.name
    assert quarantined.is_file()


@pytest.mark.asyncio
async def test_active_lease_files_are_kept(tmp_path: Path) -> None:
    codec = ExportFileCodec(crypto(), tmp_path)
    final = await codec.write_csv(9, LEASE_A, ("phone",), rows())
    part = tmp_path / f"export-9-{LEASE_A}.part"
    part.write_bytes(b"in-progress")
    _age(final, datetime(2026, 1, 1, tzinfo=UTC))
    _age(part, datetime(2026, 1, 1, tzinfo=UTC))
    repository = FakeRepository()
    repository._leases = [ExportLeaseRef(9, LEASE_A)]

    stats = await reconcile_export_storage(
        repository,
        codec,
        now=datetime(2026, 1, 20, tzinfo=UTC),
    )

    assert stats.orphans_removed == 0
    assert final.exists()
    assert part.exists()


@pytest.mark.asyncio
async def test_old_lease_ciphertext_is_removed_after_retention(tmp_path: Path) -> None:
    codec = ExportFileCodec(crypto(), tmp_path)
    old = await codec.write_csv(9, LEASE_A, ("phone",), rows())
    current = await codec.write_csv(9, LEASE_B, ("phone",), rows())
    _age(old, datetime(2026, 1, 1, tzinfo=UTC))
    repository = FakeRepository()
    repository._ready = [ExpiredExport(9, str(current))]
    repository._leases = [ExportLeaseRef(9, LEASE_B)]

    stats = await reconcile_export_storage(
        repository,
        codec,
        now=datetime(2026, 1, 10, tzinfo=UTC),
    )

    assert not old.exists()
    assert current.exists()
    assert stats.orphans_removed == 1
    assert repository.unreadables == []


@pytest.mark.asyncio
async def test_unreadable_interrupt_between_quarantine_and_mark_converges(
    tmp_path: Path,
) -> None:
    codec = ExportFileCodec(crypto(), tmp_path)
    path = await codec.write_csv(10, LEASE_A, ("phone",), rows())
    repository = FakeRepository()
    repository._ready = [ExpiredExport(9, str(path))]
    codec.quarantine(path)
    assert not path.exists()

    stats = await reconcile_export_storage(repository, codec)
    assert stats.missing_failed == 1
    assert repository.unreadables == [(9, str(path))]

    second = await reconcile_export_storage(repository, codec)
    assert second.total == 0
