"""非审计业务数据的保留期清理编排。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, fields, replace
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Protocol

from app.core.bounded_executor import run_bounded

# 子表在父表前进入轮转；页内提交的进度就是续作事实，无需另建任务状态表。
CLEANUP_TABLES = (
    "raw",
    "unmatched",
    "import_phones",
    "imports",
    "idempotency",
    "jobs",
    "callbacks",
    "callback_events",
    "usage_frequency",
    "usage_quota",
    "usage",
    "usage_projection",
    "usage_alias",
    "usage_subject",
)
CleanupCursor = tuple[object, ...]


@dataclass(frozen=True, slots=True)
class LifecyclePolicy:
    raw_days: int
    unmatched_days: int
    job_days: int
    usage_days: int = 90


@dataclass(frozen=True, slots=True)
class ExpiredImport:
    id: int
    invalid_file: str | None
    source_file: str | None = None


@dataclass(frozen=True, slots=True)
class CleanupCounts:
    raw: int = 0
    unmatched: int = 0
    imports: int = 0
    idempotency: int = 0
    jobs: int = 0
    usage: int = 0
    has_more: bool = False

    @property
    def total(self) -> int:
        return sum(getattr(self, field.name) for field in fields(self) if field.name != "has_more")

    def plus(self, other: CleanupCounts) -> CleanupCounts:
        return CleanupCounts(
            **{
                field.name: (
                    self.has_more or other.has_more
                    if field.name == "has_more"
                    else getattr(self, field.name) + getattr(other, field.name)
                )
                for field in fields(self)
            }
        )


@dataclass(frozen=True, slots=True)
class CleanupPage:
    affected: int
    cursor: CleanupCursor | None
    counts: CleanupCounts = CleanupCounts()


class HousekeepingRepository(Protocol):
    async def policy(self) -> LifecyclePolicy: ...

    async def expired_imports(
        self,
        *,
        cutoff: datetime,
        after_id: int,
        limit: int,
    ) -> tuple[ExpiredImport, ...]: ...

    async def finish_import(self, import_id: int, *, cutoff: datetime) -> int: ...

    async def cleanup_page(
        self,
        table: str,
        policy: LifecyclePolicy,
        *,
        cutoff: datetime,
        cursor: CleanupCursor | None,
        limit: int,
    ) -> CleanupPage: ...


class ImportFileStore:
    """只删除受控 import 根目录内的掩码清单及密文源。"""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def remove(self, filename: str | None) -> None:
        if filename is None:
            return
        if Path(filename).name != filename or Path(filename).suffix not in {".csv", ".smsx"}:
            raise ValueError("import cleanup path is outside controlled storage")
        candidate = (self.root / filename).resolve()
        if candidate.parent != self.root:
            raise ValueError("import cleanup path is outside controlled storage")
        candidate.unlink(missing_ok=True)
        if candidate.suffix == ".smsx":
            partial = candidate.with_suffix(".part")
            if partial.parent != self.root:
                raise ValueError("import cleanup path is outside controlled storage")
            partial.unlink(missing_ok=True)


class HousekeepingService:
    """固定 cutoff、表间轮转和主键游标；有界短事务失败后从剩余事实续作。"""

    def __init__(
        self,
        repository: HousekeepingRepository,
        files: ImportFileStore,
        *,
        batch_size: int = 500,
        max_pages: int = 64,
        max_seconds: float = 30,
        max_files: int = 64,
    ) -> None:
        if not 1 <= batch_size <= 1000 or not 1 <= max_pages <= 256:
            raise ValueError("invalid housekeeping page budget")
        if not 0 < max_seconds <= 120 or not 2 <= max_files <= 256:
            raise ValueError("invalid housekeeping run budget")
        self.repository = repository
        self.files = files
        self.batch_size = batch_size
        self.max_pages = max_pages
        self.max_seconds = max_seconds
        self.max_files = max_files

    async def run(self, *, cutoff: datetime | None = None) -> CleanupCounts:
        cutoff = cutoff or datetime.now(UTC)
        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError("housekeeping cutoff must include timezone")
        deadline = monotonic() + self.max_seconds
        policy = await self.repository.policy()
        pending = deque(CLEANUP_TABLES)
        cursors: dict[str, CleanupCursor | None] = dict.fromkeys(CLEANUP_TABLES)
        counts = CleanupCounts()
        files_left = self.max_files
        deferred = False
        for _ in range(self.max_pages):
            if not pending or monotonic() >= deadline:
                break
            table = pending.popleft()
            if table == "imports":
                if files_left < 2:
                    deferred = True
                    continue
                cursor = cursors[table]
                imports = await self.repository.expired_imports(
                    cutoff=cutoff,
                    after_id=int(cursor[0]) if cursor else 0,  # type: ignore[call-overload]
                    limit=min(self.batch_size, files_left // 2),
                )
                for item in imports:
                    if monotonic() >= deadline:
                        return replace(counts, has_more=True)
                    # 子号码已经排空；文件删除失败仍保留父记录，缺文件重试可幂等。
                    await run_bounded(self.files.remove, item.invalid_file, timeout_s=5)
                    await run_bounded(self.files.remove, item.source_file, timeout_s=5)
                    files_left -= 2
                    changed = await self.repository.finish_import(item.id, cutoff=cutoff)
                    counts = counts.plus(CleanupCounts(imports=changed))
                    cursors[table] = (item.id,)
                if imports:
                    pending.append(table)
                continue
            page = await self.repository.cleanup_page(
                table,
                policy,
                cutoff=cutoff,
                cursor=cursors[table],
                limit=self.batch_size,
            )
            counts = counts.plus(page.counts)
            cursors[table] = page.cursor
            if page.affected:
                pending.append(table)
                parents = {
                    "import_phones": ("imports",),
                    "callbacks": ("callback_events",),
                    "usage_frequency": ("usage", "usage_alias", "usage_subject"),
                    "usage_quota": ("usage",),
                    "usage_alias": ("usage_subject",),
                }.get(table, ())
                for parent in parents:
                    cursors[parent] = None
                    if parent not in pending:
                        pending.append(parent)
        return replace(counts, has_more=bool(pending) or deferred)
