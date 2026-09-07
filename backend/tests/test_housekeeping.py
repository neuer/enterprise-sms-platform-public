from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.services.housekeeping import (
    CleanupCounts,
    CleanupCursor,
    CleanupPage,
    ExpiredImport,
    HousekeepingService,
    ImportFileStore,
    LifecyclePolicy,
)

CUTOFF = datetime(2026, 1, 1, tzinfo=UTC)


class FakeRepository:
    def __init__(self, imports: tuple[ExpiredImport, ...]) -> None:
        self.imports = imports
        self.cleaned_ids: list[int] = []
        self.pages: list[tuple[str, datetime, CleanupCursor | None, int]] = []

    async def policy(self) -> LifecyclePolicy:
        return LifecyclePolicy(90, 90, 30)

    async def expired_imports(
        self,
        *,
        cutoff: datetime,
        after_id: int,
        limit: int,
    ) -> tuple[ExpiredImport, ...]:
        assert cutoff == CUTOFF
        return tuple(
            item for item in self.imports if item.id > after_id and item.id not in self.cleaned_ids
        )[:limit]

    async def finish_import(self, import_id: int, *, cutoff: datetime) -> int:
        assert cutoff == CUTOFF
        self.cleaned_ids.append(import_id)
        return 1

    async def cleanup_page(
        self,
        table: str,
        policy: LifecyclePolicy,
        *,
        cutoff: datetime,
        cursor: CleanupCursor | None,
        limit: int,
    ) -> CleanupPage:
        self.pages.append((table, cutoff, cursor, limit))
        return CleanupPage(0, cursor)


@pytest.mark.asyncio
async def test_housekeeping_removes_import_files_before_database_rows(tmp_path: Path) -> None:
    invalid = tmp_path / "safe.csv"
    invalid.write_text("phone_mask,reason\n138****8000,invalid\n", encoding="utf-8")

    class Repository(FakeRepository):
        async def finish_import(self, import_id: int, *, cutoff: datetime) -> int:
            assert not invalid.exists()
            return await super().finish_import(import_id, cutoff=cutoff)

    repository = Repository((ExpiredImport(7, invalid.name), ExpiredImport(8, None)))
    result = await HousekeepingService(repository, ImportFileStore(tmp_path)).run(cutoff=CUTOFF)
    assert repository.cleaned_ids == [7, 8]
    assert result.imports == 2


@pytest.mark.asyncio
async def test_housekeeping_rejects_import_path_escape_before_database_delete(
    tmp_path: Path,
) -> None:
    repository = FakeRepository((ExpiredImport(7, "../outside.csv"),))
    with pytest.raises(ValueError, match="import cleanup path"):
        await HousekeepingService(repository, ImportFileStore(tmp_path)).run(cutoff=CUTOFF)
    assert repository.cleaned_ids == []


@pytest.mark.asyncio
async def test_housekeeping_file_budget_and_failure_preserve_committed_progress(
    tmp_path: Path,
) -> None:
    repository = FakeRepository(tuple(ExpiredImport(i, f"safe-{i}.csv") for i in range(1, 6)))
    service = HousekeepingService(repository, ImportFileStore(tmp_path), max_files=4)
    assert (await service.run(cutoff=CUTOFF)).imports == 2
    assert repository.cleaned_ids == [1, 2]
    assert (await service.run(cutoff=CUTOFF)).imports == 2
    assert (await service.run(cutoff=CUTOFF)).imports == 1
    assert repository.cleaned_ids == [1, 2, 3, 4, 5]

    class FailingFiles(ImportFileStore):
        def remove(self, filename: str | None) -> None:
            if filename == "fail.csv":
                raise OSError("synthetic unlink failure")
            super().remove(filename)

    repository = FakeRepository((ExpiredImport(1, "ok.csv"), ExpiredImport(2, "fail.csv")))
    with pytest.raises(OSError, match="synthetic"):
        await HousekeepingService(repository, FailingFiles(tmp_path)).run(cutoff=CUTOFF)
    assert repository.cleaned_ids == [1]
    assert (
        await HousekeepingService(repository, ImportFileStore(tmp_path)).run(cutoff=CUTOFF)
    ).imports == 1


@pytest.mark.asyncio
async def test_housekeeping_round_robin_uses_fixed_cutoff_keyset_and_round_budget(
    tmp_path: Path,
) -> None:
    class Repository(FakeRepository):
        async def cleanup_page(
            self,
            table: str,
            policy: LifecyclePolicy,
            *,
            cutoff: datetime,
            cursor: CleanupCursor | None,
            limit: int,
        ) -> CleanupPage:
            await super().cleanup_page(table, policy, cutoff=cutoff, cursor=cursor, limit=limit)
            last_id = int(cursor[0]) if cursor else 0  # type: ignore[call-overload]
            return CleanupPage(limit, (last_id + limit,), CleanupCounts(raw=limit))

    repository = Repository(())
    result = await HousekeepingService(
        repository,
        ImportFileStore(tmp_path),
        batch_size=7,
        max_pages=20,
    ).run(cutoff=CUTOFF)
    assert result.total == 19 * 7  # 一页独立文件阶段，其余为数据库页。
    assert len({table for table, *_ in repository.pages[:13]}) == 13
    assert all(cutoff == CUTOFF and limit == 7 for _, cutoff, _, limit in repository.pages)
    assert repository.pages[13][2] == (7,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("child", "parent"),
    [
        ("callbacks", "callback_events"),
        ("usage_frequency", "usage"),
        ("usage_frequency", "usage_alias"),
        ("usage_frequency", "usage_subject"),
        ("usage_quota", "usage"),
        ("usage_alias", "usage_subject"),
    ],
)
async def test_housekeeping_revisits_parent_after_multiple_child_pages(
    tmp_path: Path, child: str, parent: str
) -> None:
    class Repository(FakeRepository):
        children = 5
        parent_exists = True
        child_sizes: list[int]
        parent_attempts = 0

        def __init__(self) -> None:
            super().__init__(())
            self.child_sizes = []

        async def cleanup_page(
            self, table: str, policy: LifecyclePolicy, **kwargs: object
        ) -> CleanupPage:
            limit = int(kwargs["limit"])  # type: ignore[call-overload]
            if table == child and self.children:
                affected = min(limit, self.children)
                self.children -= affected
                self.child_sizes.append(affected)
                return CleanupPage(affected, (5 - self.children,))
            if table == parent:
                self.parent_attempts += 1
                if not self.children and self.parent_exists:
                    self.parent_exists = False
                    return CleanupPage(1, (1,))
            return CleanupPage(0, None)

    repository = Repository()
    result = await HousekeepingService(repository, ImportFileStore(tmp_path), batch_size=2).run(
        cutoff=CUTOFF
    )
    assert repository.child_sizes == [2, 2, 1]
    assert repository.parent_attempts >= 2
    assert not repository.parent_exists
    assert not result.has_more
