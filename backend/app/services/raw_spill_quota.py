"""spill 活动配额与容量租约账本；所有 *_locked 方法须在配额锁内调用。"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from app.services.raw_spill_format import (
    HEADER_QUARANTINE_SUFFIX,
    QUOTA_LOCK_NAME,
    RECLAIM_CURSOR_NAME,
    RESERVATION_KEYS,
    RESERVE_SUFFIX,
    SHANGHAI_TIMEZONE,
    SOURCE_PATTERN,
    STREAM_ID_PATTERN,
    SpillReservation,
    is_activity_filename,
    is_nonactive_quota_filename,
)
from app.services.raw_spill_layout import (
    SpillLayout,
    SpillLimits,
)


class SpillQuota:
    """活动文件数与字节配额；在途流以容量租约预留计入。"""

    def __init__(self, layout: SpillLayout, limits: SpillLimits) -> None:
        self._layout = layout
        self._limits = limits

    def usage_bytes(self) -> int:
        """目录实际文件字节；配额判断必须走 accounted_usage。"""

        if not self._layout.directory.exists():
            return 0
        return sum(
            path.stat().st_size
            for path in self._layout.directory.iterdir()
            if path.is_file() and path.name not in {QUOTA_LOCK_NAME, RECLAIM_CURSOR_NAME}
        )

    def pending_count(self) -> int:
        """活动文件数：可完成或可恢复为库事实的对象，不含非活动隔离。"""

        if not self._layout.directory.exists():
            return 0
        return sum(
            1
            for path in self._layout.directory.iterdir()
            if path.is_file() and is_activity_filename(path.name)
        )

    def pending_spill_count(self) -> int:
        if not self._layout.directory.exists():
            return 0
        return sum(
            1
            for path in self._layout.directory.iterdir()
            if path.is_file() and path.suffix == ".spill"
        )

    def write_reservation_locked(
        self,
        source: str,
        lease_id: str,
        reserved_bytes: int,
    ) -> SpillReservation:
        path = self._layout.reservation_path(source, lease_id)
        payload = json.dumps(
            {
                "created_at": datetime.now(SHANGHAI_TIMEZONE).isoformat(),
                "lease_id": lease_id,
                "reserved_bytes": reserved_bytes,
                "source": source,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        self._layout.fsync_directory()
        return SpillReservation(
            source=source,
            lease_id=lease_id,
            reserved_bytes=reserved_bytes,
            path=path,
        )

    def _parse_reservation(self, path: Path) -> SpillReservation | None:
        try:
            raw = json.loads(path.read_bytes().decode("utf-8"))
            if not isinstance(raw, dict) or set(raw) != RESERVATION_KEYS:
                return None
            source = str(raw["source"])
            lease_id = str(raw["lease_id"])
            reserved_bytes = int(raw["reserved_bytes"])
            created_at = str(raw["created_at"])
            if reserved_bytes < 1:
                return None
            if SOURCE_PATTERN.fullmatch(source) is None:
                return None
            if STREAM_ID_PATTERN.fullmatch(lease_id) is None:
                return None
            if "+" not in created_at and not created_at.endswith("Z"):
                return None
            if self._layout.reservation_path(source, lease_id) != path:
                return None
            return SpillReservation(
                source=source,
                lease_id=lease_id,
                reserved_bytes=reserved_bytes,
                path=path,
            )
        except (OSError, KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            return None

    def iter_reservations_locked(self) -> list[SpillReservation]:
        if not self._layout.directory.exists():
            return []
        records: list[SpillReservation] = []
        for path in sorted(self._layout.directory.glob(f"*{RESERVE_SUFFIX}")):
            if path.name.endswith(".reserve.tmp"):
                continue
            record = self._parse_reservation(path)
            if record is not None:
                records.append(record)
        return records

    def reservation_for_source_locked(self, source: str) -> SpillReservation | None:
        for record in self.iter_reservations_locked():
            if record.source == source:
                return record
        return None

    def _covered_names_locked(self, reservations: list[SpillReservation]) -> set[str]:
        names: set[str] = set()
        for record in reservations:
            names.update(
                {
                    record.path.name,
                    f"{record.source}-{record.lease_id}.stream",
                    f"{record.source}-{record.lease_id}.stream.stream",
                    f"{record.source}-{record.lease_id}.stream.stream.hdr",
                    f"{record.source}-{record.lease_id}.stream.tmp",
                    f"{record.source}-{record.lease_id}.stream.tmp.hdr",
                    f"{record.source}-{record.lease_id}.stream.hdr",
                    f"{record.source}-{record.lease_id}.quarantine",
                    f"{record.source}-{record.lease_id}{HEADER_QUARANTINE_SUFFIX}",
                }
            )
        return names

    def accounted_usage_locked(self) -> int:
        if not self._layout.directory.exists():
            return 0
        reservations = self.iter_reservations_locked()
        covered = self._covered_names_locked(reservations)
        uncovered = 0
        for path in self._layout.directory.iterdir():
            if not path.is_file() or path.name in {QUOTA_LOCK_NAME, RECLAIM_CURSOR_NAME}:
                continue
            if path.name in covered or path.name.endswith(RESERVE_SUFFIX):
                continue
            if is_nonactive_quota_filename(path.name):
                continue
            uncovered += path.stat().st_size
        return uncovered + sum(record.reserved_bytes for record in reservations)

    def can_accept_locked(
        self,
        additional_bytes: int = 0,
        *,
        additional_files: int = 1,
    ) -> bool:
        return (
            self.pending_count() + additional_files <= self._limits.max_pending_files
            and self.accounted_usage_locked() + additional_bytes <= self._limits.max_total_bytes
        )

    def reclaim_orphans_locked(self, source: str | None = None) -> int:
        if not self._layout.directory.exists():
            return 0
        reclaimed = 0
        for path in list(self._layout.directory.glob(f"*{RESERVE_SUFFIX}.tmp")):
            path.unlink(missing_ok=True)
            reclaimed += 1
        for path in list(self._layout.directory.glob(f"*{RESERVE_SUFFIX}")):
            record = self._parse_reservation(path)
            if record is None:
                path.unlink(missing_ok=True)
                reclaimed += 1
                continue
            if source is not None and record.source != source:
                continue
            if self._layout.stream_tmp(record.source, record.lease_id).exists():
                continue
            if self._layout.stream_path(record.source, record.lease_id).exists():
                continue
            path.unlink(missing_ok=True)
            reclaimed += 1
        return reclaimed
