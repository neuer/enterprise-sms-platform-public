from __future__ import annotations

from pathlib import Path

import pytest

from app.services.housekeeping import (
    CleanupCounts,
    ExpiredImport,
    HousekeepingService,
    ImportFileStore,
    LifecyclePolicy,
)


class FakeRepository:
    def __init__(self, imports: tuple[ExpiredImport, ...]) -> None:
        self.imports = imports
        self.cleaned_ids: tuple[int, ...] | None = None

    async def policy(self) -> LifecyclePolicy:
        return LifecyclePolicy(raw_days=90, unmatched_days=90, job_days=30)

    async def expired_imports(self) -> tuple[ExpiredImport, ...]:
        return self.imports

    async def cleanup(self, policy: LifecyclePolicy, import_ids: tuple[int, ...]) -> CleanupCounts:
        self.cleaned_ids = import_ids
        return CleanupCounts(2, 3, len(import_ids), 4, 5)


@pytest.mark.asyncio
async def test_housekeeping_removes_import_files_before_database_rows(tmp_path: Path) -> None:
    invalid = tmp_path / "11111111-1111-1111-1111-111111111111.csv"
    invalid.write_text("phone_mask,reason\n138****8000,invalid\n", encoding="utf-8")
    repository = FakeRepository((ExpiredImport(7, invalid.name), ExpiredImport(8, None)))
    service = HousekeepingService(repository, ImportFileStore(tmp_path))

    result = await service.run()

    assert not invalid.exists()
    assert repository.cleaned_ids == (7, 8)
    assert result.total == 16


@pytest.mark.asyncio
async def test_housekeeping_rejects_import_path_escape_before_database_delete(
    tmp_path: Path,
) -> None:
    repository = FakeRepository((ExpiredImport(7, "../outside.csv"),))
    service = HousekeepingService(repository, ImportFileStore(tmp_path))

    with pytest.raises(ValueError, match="import cleanup path"):
        await service.run()

    assert repository.cleaned_ids is None
