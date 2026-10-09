"""spill 目录布局、共享配额锁、可变限额与累计计数。"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from app.services.raw_spill_format import (
    CIPHERQ_MANIFEST_SUFFIX,
    CIPHERQ_SUFFIX,
    HEADER_QUARANTINE_SUFFIX,
    QUOTA_LOCK_NAME,
    RESERVE_SUFFIX,
    SHA256_PATTERN,
    SOURCE_PATTERN,
    STREAM_ID_PATTERN,
    ReclaimRoundBudget,
    RecoverRoundBudget,
    SpillReclaimResult,
    parse_spill_filename,
)


@dataclass(slots=True)
class SpillLimits:
    """可在运行中调整的目录限额；各组件共享同一实例，修改立即生效。"""

    max_total_bytes: int
    max_pending_files: int
    header_only_min_age_s: float
    max_quarantine_files: int
    max_quarantine_bytes: int
    quarantine_retention_s: float
    max_cipherq_files: int
    max_cipherq_bytes: int
    cipherq_retention_s: float
    recover_budget: RecoverRoundBudget
    reclaim_budget: ReclaimRoundBudget


@dataclass(slots=True)
class SpillCounters:
    """进程内累计计数与最近一次回收结果；不含路径、密文或 PII。"""

    header_only_cleaned: int = 0
    isolated_total: int = 0
    quarantine_expired: int = 0
    quarantine_capacity_dropped: int = 0
    cipherq_expired: int = 0
    cipherq_capacity_dropped: int = 0
    last_reclaim: SpillReclaimResult = field(default_factory=SpillReclaimResult)


class SpillLayout:
    """spill 目录内各类文件的命名、配额锁、原子写与目录 fsync。"""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def path(self, source: str, payload_sha256: str) -> Path:
        if SOURCE_PATTERN.fullmatch(source) is None:
            raise ValueError("invalid raw spill source")
        if SHA256_PATTERN.fullmatch(payload_sha256) is None:
            raise ValueError("invalid raw spill digest")
        return self.directory / f"{source}-{payload_sha256}.spill"

    def spill_path_matches_identity(self, path: Path, source: str, payload_sha256: str) -> bool:
        """接受历史 digest 文件名、独立 artifact_id，以及 cipherq 认领名。"""

        parsed = parse_spill_filename(path.name)
        if parsed is None:
            return False
        named_source, token, _rest = parsed
        if named_source != source:
            return False
        if len(token) == 64:
            return token == payload_sha256
        return True

    def stream_tmp(self, source: str, stream_id: str) -> Path:
        if SOURCE_PATTERN.fullmatch(source) is None:
            raise ValueError("invalid raw spill source")
        if STREAM_ID_PATTERN.fullmatch(stream_id) is None:
            raise ValueError("invalid raw spill stream id")
        return self.directory / f"{source}-{stream_id}.stream.tmp"

    def stream_path(self, source: str, stream_id: str) -> Path:
        return self.stream_tmp(source, stream_id).with_suffix("")

    def quarantine_path(self, source: str, stream_id: str) -> Path:
        return self.directory / f"{source}-{stream_id}.quarantine"

    def _header_quarantine_path(self, source: str, stream_id: str) -> Path:
        return self.directory / f"{source}-{stream_id}{HEADER_QUARANTINE_SUFFIX}"

    @contextmanager
    def quota_lock(self) -> Iterator[None]:
        """Report/Reply 共用目录锁，禁止并发超卖。"""

        self.directory.mkdir(parents=True, exist_ok=True)
        handle = (self.directory / QUOTA_LOCK_NAME).open("a+b")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def reservation_path(self, source: str, lease_id: str) -> Path:
        if SOURCE_PATTERN.fullmatch(source) is None:
            raise ValueError("invalid raw spill source")
        if STREAM_ID_PATTERN.fullmatch(lease_id) is None:
            raise ValueError("invalid raw spill stream id")
        return self.directory / f"{source}-{lease_id}{RESERVE_SUFFIX}"

    def remove_stream_locked(self, source: str, stream_id: str) -> None:
        tmp = self.stream_tmp(source, stream_id)
        tmp.unlink(missing_ok=True)
        tmp.with_name(tmp.name + ".hdr").unlink(missing_ok=True)
        final = self.stream_path(source, stream_id)
        final.unlink(missing_ok=True)
        final.with_name(final.name + ".hdr").unlink(missing_ok=True)
        legacy = final.with_name(final.name + ".stream")
        legacy.unlink(missing_ok=True)
        legacy.with_name(legacy.name + ".hdr").unlink(missing_ok=True)
        quarantine = self.quarantine_path(source, stream_id)
        quarantine.unlink(missing_ok=True)
        quarantine.with_name(quarantine.name + ".tmp").unlink(missing_ok=True)
        headerq = self._header_quarantine_path(source, stream_id)
        headerq.unlink(missing_ok=True)
        headerq.with_name(headerq.name + ".tmp").unlink(missing_ok=True)
        reserve = self.reservation_path(source, stream_id)
        reserve.unlink(missing_ok=True)
        reserve.with_name(reserve.name + ".tmp").unlink(missing_ok=True)

    def file_age_seconds(self, path: Path, now_ts: float) -> float:
        try:
            return max(0.0, now_ts - path.stat().st_mtime)
        except OSError:
            return 0.0

    def write_header_quarantine(self, source: str, stream_id: str, kind: str) -> Path:
        """损坏 header 的无 PII 隔离标记；不计入 pending_count 文件配额。"""

        self.directory.mkdir(parents=True, exist_ok=True)
        target = self._header_quarantine_path(source, stream_id)
        payload = json.dumps(
            {"source": source, "state": kind, "stream_id": stream_id},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        tmp = target.with_name(target.name + ".tmp")
        with tmp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        self.fsync_directory()
        return target

    def write_quarantine(self, source: str, stream_id: str) -> Path:
        """为不完整 terminal 写下无 PII 的 quarantine 标记，便于巡检与配额记账。"""

        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.quarantine_path(source, stream_id)
        payload = json.dumps(
            {"source": source, "state": "quarantined", "stream_id": stream_id},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        tmp = target.with_name(target.name + ".tmp")
        with tmp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        self.fsync_directory()
        return target

    def cipherq_paths(self, source: str, token: str) -> tuple[Path, Path]:
        stem = f"{source}-{token}"
        return (
            self.directory / f"{stem}{CIPHERQ_SUFFIX}",
            self.directory / f"{stem}{CIPHERQ_MANIFEST_SUFFIX}",
        )

    def cipherq_wip(self, dest: Path) -> Path:
        return dest.with_name(dest.name + ".wip")

    def write_json_atomic(self, target: Path, document: dict[str, object]) -> None:
        payload = json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")
        tmp = target.with_name(target.name + ".tmp")
        with tmp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        self.fsync_directory()

    def release_stream_activity_locked(self, source: str, stream_id: str) -> None:
        """隔离密文后释放预留与重写临时文件，不删除 .cq / .quarantine 交接标记。"""

        tmp = self.stream_tmp(source, stream_id)
        tmp.with_name(tmp.name + ".hdr").unlink(missing_ok=True)
        final = self.stream_path(source, stream_id)
        final.with_name(final.name + ".hdr").unlink(missing_ok=True)
        legacy = final.with_name(final.name + ".stream")
        legacy.unlink(missing_ok=True)
        legacy.with_name(legacy.name + ".hdr").unlink(missing_ok=True)
        reserve = self.reservation_path(source, stream_id)
        reserve.unlink(missing_ok=True)
        reserve.with_name(reserve.name + ".tmp").unlink(missing_ok=True)

    def fsync_directory(self) -> None:
        directory_fd = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
