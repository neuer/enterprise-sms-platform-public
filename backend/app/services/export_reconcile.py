"""导出密文目录与 export_task 的双向崩溃对账。"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from app.core.bounded_executor import run_bounded
from app.services.export_file import ExportFileCodec
from app.services.export_repository import ExpiredExport, ExportLeaseRef

LOGGER = logging.getLogger(__name__)


class ExportReconcileRepository(Protocol):
    async def retention_days(self) -> int: ...

    async def expired(self, retention_days: int) -> list[ExpiredExport]: ...

    async def ready_files(self, retention_days: int) -> list[ExpiredExport]: ...

    async def active_leases(self) -> list[ExportLeaseRef]: ...

    async def clear_file(self, task_id: int, file_path: str) -> None: ...

    async def mark_unreadable(self, task_id: int, file_path: str) -> None: ...


@dataclass(frozen=True, slots=True)
class ExportReconcileStats:
    expired_cleared: int = 0
    missing_failed: int = 0
    unreadable_failed: int = 0
    orphans_removed: int = 0
    quarantined: int = 0

    @property
    def total(self) -> int:
        return (
            self.expired_cleared
            + self.missing_failed
            + self.unreadable_failed
            + self.orphans_removed
            + self.quarantined
        )


def _aged(path: Path, *, now: datetime, retention: timedelta) -> bool:
    mtime = datetime.fromtimestamp(os.stat(path).st_mtime, tz=UTC)
    return now - mtime >= retention


def _missing(path: Path) -> bool:
    return os.path.islink(path) or not os.path.isfile(path)


def _log(action: str, task_id: int | None) -> None:
    if task_id is None:
        LOGGER.warning("export storage reconcile action=%s", action)
        return
    LOGGER.warning("export storage reconcile action=%s task_id=%s", action, task_id)


async def _forget_referenced(
    repository: ExportReconcileRepository,
    codec: ExportFileCodec,
    item: ExpiredExport,
) -> None:
    """先删文件再清 DB；任一边界中断后下一轮可重入收敛。"""

    await run_bounded(codec.remove_artifact, item.file_path, timeout_s=5)
    await repository.clear_file(item.id, item.file_path)


async def reconcile_export_storage(
    repository: ExportReconcileRepository,
    codec: ExportFileCodec,
    *,
    now: datetime | None = None,
) -> ExportReconcileStats:
    """目录 ↔ 数据库对账：保活租约、校验 ready、回收孤儿、缺失转 failed。"""

    moment = now or datetime.now(UTC)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("export reconcile now must include timezone")
    retention_days = await repository.retention_days()
    if retention_days < 1:
        raise ValueError("export retention_days must be positive")
    retention = timedelta(days=retention_days)
    expired = await repository.expired(retention_days)
    ready = await repository.ready_files(retention_days)
    active = {(item.id, item.lease_id) for item in await repository.active_leases()}
    ready_paths: set[Path] = set()
    for item in ready:
        try:
            ready_paths.add(codec.contained_path(item.file_path))
        except ValueError:
            continue

    expired_cleared = 0
    for item in expired:
        await _forget_referenced(repository, codec, item)
        _log("expired_cleared", item.id)
        expired_cleared += 1

    missing_failed = 0
    unreadable_failed = 0
    quarantined = 0
    for item in ready:
        try:
            path = codec.contained_path(item.file_path)
        except ValueError:
            await repository.mark_unreadable(item.id, item.file_path)
            _log("unreadable_failed", item.id)
            unreadable_failed += 1
            continue
        if _missing(path):
            await repository.mark_unreadable(item.id, item.file_path)
            _log("missing_failed", item.id)
            missing_failed += 1
            continue
        try:
            await run_bounded(
                codec.verify_ready,
                path,
                expected_task_id=item.id,
                require_manifest=False,
                timeout_s=30,
            )
        except (FileNotFoundError, OSError, ValueError):
            await run_bounded(codec.quarantine, path, timeout_s=5)
            await repository.mark_unreadable(item.id, item.file_path)
            _log("unreadable_failed", item.id)
            unreadable_failed += 1
            quarantined += 1

    orphans_removed = 0
    for artifact in codec.list_root_artifacts():
        if (artifact.task_id, artifact.lease_id) in active:
            continue
        if artifact.path in ready_paths:
            continue
        if artifact.suffix == ".smsx":
            try:
                await run_bounded(
                    codec.verify_ready,
                    artifact.path,
                    expected_task_id=artifact.task_id,
                    expected_lease_id=artifact.lease_id,
                    require_manifest=False,
                    timeout_s=30,
                )
            except (FileNotFoundError, OSError, ValueError):
                await run_bounded(codec.quarantine, artifact.path, timeout_s=5)
                _log("quarantined", artifact.task_id)
                quarantined += 1
                continue
            if not _aged(artifact.path, now=moment, retention=retention):
                continue
            await run_bounded(codec.remove_artifact, artifact.path, timeout_s=5)
            _log("orphan_removed", artifact.task_id)
            orphans_removed += 1
            continue
        if not _aged(artifact.path, now=moment, retention=retention):
            continue
        await run_bounded(codec.remove_artifact, artifact.path, timeout_s=5)
        _log("orphan_removed", artifact.task_id)
        orphans_removed += 1

    for artifact in codec.list_quarantine_artifacts():
        if not _aged(artifact.path, now=moment, retention=retention):
            continue
        await run_bounded(codec.remove_artifact, artifact.path, timeout_s=5)
        _log("quarantine_expired", artifact.task_id)
        orphans_removed += 1

    return ExportReconcileStats(
        expired_cleared=expired_cleared,
        missing_failed=missing_failed,
        unreadable_failed=unreadable_failed,
        orphans_removed=orphans_removed,
        quarantined=quarantined,
    )
