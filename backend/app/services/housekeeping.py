"""非审计业务数据的保留期清理编排。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.core.bounded_executor import run_bounded


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
    raw: int
    unmatched: int
    imports: int
    idempotency: int
    jobs: int
    usage: int = 0

    @property
    def total(self) -> int:
        return self.raw + self.unmatched + self.imports + self.idempotency + self.jobs + self.usage


class HousekeepingRepository(Protocol):
    async def policy(self) -> LifecyclePolicy: ...

    async def expired_imports(self) -> tuple[ExpiredImport, ...]: ...

    async def cleanup(
        self,
        policy: LifecyclePolicy,
        import_ids: tuple[int, ...],
    ) -> CleanupCounts: ...


class ImportFileStore:
    """只删除受控 import 根目录内的掩码剔除清单。"""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def remove(self, filename: str | None) -> None:
        if filename is None:
            return
        if Path(filename).name != filename or Path(filename).suffix not in {
            ".csv",
            ".smsx",
        }:
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
    """文件先删、数据库后删；任一步失败均保留可重试事实。"""

    def __init__(
        self,
        repository: HousekeepingRepository,
        files: ImportFileStore,
    ) -> None:
        self.repository = repository
        self.files = files

    async def run(self) -> CleanupCounts:
        policy = await self.repository.policy()
        expired = await self.repository.expired_imports()
        for item in expired:
            await run_bounded(self.files.remove, item.invalid_file, timeout_s=5)
            await run_bounded(self.files.remove, item.source_file, timeout_s=5)
        return await self.repository.cleanup(policy, tuple(item.id for item in expired))
