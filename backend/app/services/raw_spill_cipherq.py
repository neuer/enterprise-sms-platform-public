"""非活动密文隔离区（cipherq）：先写清单再原子迁移，缺 Key / 暂态 I/O 永不驱逐。"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.services.crypto import (
    UnknownKeyVersionError,
)
from app.services.raw_spill_codec import (
    file_size_and_sha256,
    safe_src_name,
)
from app.services.raw_spill_format import (
    CIPHERQ_EVICTABLE_REASONS,
    CIPHERQ_FILE_NAME,
    CIPHERQ_FORBIDDEN_KEYS,
    CIPHERQ_MANIFEST_KEYS,
    CIPHERQ_MANIFEST_SUFFIX,
    CIPHERQ_RETAIN_REASONS,
    CIPHERQ_STATE_PENDING,
    CIPHERQ_STATE_SEALED,
    LOGGER,
    REASON_AUTH_FAILED,
    REASON_CORRUPT,
    REASON_KEY_UNAVAILABLE,
    REASON_TRANSIENT_IO,
    SHA256_PATTERN,
    SHANGHAI_TIMEZONE,
    SOURCE_PATTERN,
    RawSpillRecord,
    SpillMetadataAuthError,
    StreamChunkCrypto,
    parse_spill_filename,
)
from app.services.raw_spill_layout import (
    SpillCounters,
    SpillLayout,
    SpillLimits,
)
from app.services.raw_spill_reader import (
    SpillReader,
)


@dataclass(frozen=True, slots=True)
class _CipherqEntry:
    source: str
    token: str
    kind: str
    reason: str
    src_name: str
    size_bytes: int
    sha256: str
    state: str
    dest: Path
    manifest: Path
    isolated_at: str


class CipherQueue:
    """隔离区清单、容量淘汰与 Key 归还后的恢复认领。"""

    def __init__(
        self,
        layout: SpillLayout,
        limits: SpillLimits,
        counters: SpillCounters,
        reader: SpillReader,
    ) -> None:
        self._layout = layout
        self._limits = limits
        self._counters = counters
        self._reader = reader

    def _allocate_cipherq_dest_locked(self, source: str, token: str) -> tuple[str, Path, Path]:
        """同一 artifact 复用原路径；不同 artifact 分配新 token，禁止覆盖。"""

        dest, manifest_path = self._layout.cipherq_paths(source, token)
        if not dest.exists() and not self._layout.cipherq_wip(dest).exists():
            return token, dest, manifest_path
        existing = self._parse_cipherq_manifest(manifest_path)
        if existing is not None and existing.token == token and existing.source == source:
            return token, dest, manifest_path
        token = secrets.token_hex(16)
        dest, manifest_path = self._layout.cipherq_paths(source, token)
        return token, dest, manifest_path

    def _parse_cipherq_manifest(self, path: Path) -> _CipherqEntry | None:
        match = CIPHERQ_FILE_NAME.fullmatch(path.name)
        if match is None or not path.name.endswith(CIPHERQ_MANIFEST_SUFFIX):
            return None
        try:
            raw = json.loads(path.read_bytes().decode("utf-8"))
            if not isinstance(raw, dict) or not CIPHERQ_MANIFEST_KEYS.issubset(raw):
                return None
            if CIPHERQ_FORBIDDEN_KEYS.intersection(raw):
                return None
            source = str(raw["source"])
            token = str(raw["token"])
            src_name = safe_src_name(str(raw["src_name"]))
            if src_name is None:
                return None
            if SOURCE_PATTERN.fullmatch(source) is None:
                return None
            if not re.fullmatch(r"[0-9a-f]{32,64}", token):
                return None
            dest, expected = self._layout.cipherq_paths(source, token)
            if expected != path:
                return None
            state = str(raw["state"])
            if state not in {CIPHERQ_STATE_PENDING, CIPHERQ_STATE_SEALED}:
                return None
            reason = str(raw["reason"])
            if reason not in {
                REASON_KEY_UNAVAILABLE,
                REASON_TRANSIENT_IO,
                REASON_AUTH_FAILED,
                REASON_CORRUPT,
            }:
                return None
            digest = str(raw["sha256"])
            if SHA256_PATTERN.fullmatch(digest) is None:
                return None
            return _CipherqEntry(
                source=source,
                token=token,
                kind=str(raw["kind"]),
                reason=reason,
                src_name=src_name,
                size_bytes=int(raw["size_bytes"]),
                sha256=digest,
                state=state,
                dest=dest,
                manifest=path,
                isolated_at=str(raw["isolated_at"]),
            )
        except (OSError, KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            return None

    def _iter_cipherq_entries_locked(self) -> list[_CipherqEntry]:
        if not self._layout.directory.exists():
            return []
        items: list[_CipherqEntry] = []
        for path in sorted(self._layout.directory.glob(f"*{CIPHERQ_MANIFEST_SUFFIX}")):
            if path.name.endswith(".tmp"):
                continue
            entry = self._parse_cipherq_manifest(path)
            if entry is not None:
                items.append(entry)
        return items

    def iter_cipherq_payload_paths_locked(self) -> list[Path]:
        if not self._layout.directory.exists():
            return []
        items: list[Path] = []
        for path in [
            *sorted(self._layout.directory.glob("*.cq")),
            *sorted(self._layout.directory.glob("*.cq.wip")),
        ]:
            if not path.is_file() or path.name.endswith(".tmp"):
                continue
            items.append(path)
        return items

    def cipherq_usage_locked(self) -> tuple[int, int]:
        count = 0
        total = 0
        for path in self.iter_cipherq_payload_paths_locked():
            try:
                total += path.stat().st_size
            except OSError:
                continue
            count += 1
        return count, total

    def _drop_cipherq_entry_locked(self, entry: _CipherqEntry) -> None:
        entry.dest.unlink(missing_ok=True)
        self._layout.cipherq_wip(entry.dest).unlink(missing_ok=True)
        entry.manifest.unlink(missing_ok=True)
        entry.dest.with_name(entry.dest.name + ".tmp").unlink(missing_ok=True)
        entry.manifest.with_name(entry.manifest.name + ".tmp").unlink(missing_ok=True)

    def enforce_cipherq_quota_locked(self, extra_files: int = 0, extra_bytes: int = 0) -> int:
        """只淘汰已封印的 auth_failed/corrupt；缺 Key / 暂态 I/O 永不驱逐。"""

        evictable: list[tuple[float, int, _CipherqEntry]] = []
        retained_files = 0
        retained_bytes = 0
        for entry in self._iter_cipherq_entries_locked():
            if entry.state != CIPHERQ_STATE_SEALED or not entry.dest.exists():
                continue
            try:
                stat = entry.dest.stat()
            except OSError:
                continue
            if entry.reason in CIPHERQ_RETAIN_REASONS:
                retained_files += 1
                retained_bytes += stat.st_size
                continue
            if entry.reason in CIPHERQ_EVICTABLE_REASONS:
                evictable.append((stat.st_mtime, stat.st_size, entry))
        evictable.sort()
        total_files = retained_files + len(evictable)
        total_bytes = retained_bytes + sum(size for _mtime, size, _entry in evictable)
        dropped = 0
        while evictable and (
            total_files + extra_files > self._limits.max_cipherq_files
            or total_bytes + extra_bytes > self._limits.max_cipherq_bytes
        ):
            _mtime, size, entry = evictable.pop(0)
            self._drop_cipherq_entry_locked(entry)
            total_files -= 1
            total_bytes -= size
            dropped += 1
        if dropped:
            self._counters.cipherq_capacity_dropped += dropped
        return dropped

    def _ensure_cipherq_capacity_locked(self, additional_bytes: int) -> bool:
        self.enforce_cipherq_quota_locked(extra_files=1, extra_bytes=additional_bytes)
        count, total = self.cipherq_usage_locked()
        return (
            count + 1 <= self._limits.max_cipherq_files
            and total + additional_bytes <= self._limits.max_cipherq_bytes
        )

    def expire_cipherq_locked(self, now_ts: float) -> int:
        expired = 0
        for entry in self._iter_cipherq_entries_locked():
            if entry.state != CIPHERQ_STATE_SEALED or entry.reason not in CIPHERQ_EVICTABLE_REASONS:
                continue
            aged_path = entry.dest if entry.dest.exists() else entry.manifest
            if self._layout.file_age_seconds(aged_path, now_ts) < (
                self._limits.cipherq_retention_s
            ):
                continue
            self._drop_cipherq_entry_locked(entry)
            expired += 1
        return expired

    def _seal_cipherq_manifest_locked(self, entry: _CipherqEntry) -> bool:
        document = {
            "isolated_at": entry.isolated_at,
            "kind": entry.kind,
            "reason": entry.reason,
            "sha256": entry.sha256,
            "size_bytes": entry.size_bytes,
            "source": entry.source,
            "src_name": entry.src_name,
            "state": CIPHERQ_STATE_SEALED,
            "token": entry.token,
        }
        try:
            self._layout.write_json_atomic(entry.manifest, document)
            return True
        except OSError:
            return False

    def reclaim_cipherq_pending_locked(self, now_ts: float | None = None) -> int:
        """重入未完成隔离：有源则改名，有目标则封印；过期 .cq.wip 退回 .cq。"""

        reclaimed = 0
        now_value = time.time() if now_ts is None else now_ts
        for entry in self._iter_cipherq_entries_locked():
            source_path = self._layout.directory / entry.src_name
            wip = self._layout.cipherq_wip(entry.dest)
            if wip.exists() and not entry.dest.exists():
                age = self._layout.file_age_seconds(wip, now_value)
                if age >= self._limits.header_only_min_age_s:
                    try:
                        os.replace(wip, entry.dest)
                        self._layout.fsync_directory()
                        reclaimed += 1
                    except OSError:
                        continue
                continue
            if entry.state == CIPHERQ_STATE_SEALED:
                continue
            if entry.dest.exists():
                self._seal_cipherq_manifest_locked(entry)
                reclaimed += 1
                continue
            if source_path.exists():
                try:
                    os.replace(source_path, entry.dest)
                    self._layout.fsync_directory()
                    self._seal_cipherq_manifest_locked(entry)
                    reclaimed += 1
                except OSError:
                    continue
        return reclaimed

    def _iter_orphan_cipherq_paths_locked(self) -> list[Path]:
        known = {entry.dest.name for entry in self._iter_cipherq_entries_locked()}
        known.update(name + ".wip" for name in list(known))
        return [path for path in self.iter_cipherq_payload_paths_locked() if path.name not in known]

    def reclaim_cipherq_manifest_only_locked(self) -> int:
        """清单无密文：确定终态后删除清单；不得 silently 留下不可恢复孤儿。"""

        dropped = 0
        for entry in self._iter_cipherq_entries_locked():
            if entry.dest.exists() or self._layout.cipherq_wip(entry.dest).exists():
                continue
            source_path = self._layout.directory / entry.src_name
            if entry.state == CIPHERQ_STATE_PENDING and source_path.exists():
                continue
            LOGGER.warning(
                "raw spill cipherq manifest missing ciphertext",
                extra={"kind": entry.kind, "reason": entry.reason, "source": entry.source},
            )
            entry.manifest.unlink(missing_ok=True)
            dropped += 1
        return dropped

    def isolate_ciphertext_locked(
        self,
        path: Path,
        *,
        source: str,
        token: str,
        kind: str,
        reason: str,
    ) -> bool:
        """先写+fsync manifest，再原子改名密文，再封印。ENOSPC 时源文件不动。"""

        if SOURCE_PATTERN.fullmatch(source) is None:
            source = "report"
        if not token:
            token = secrets.token_hex(16)
        token, dest, manifest_path = self._allocate_cipherq_dest_locked(source, token)
        if dest.exists() or self._layout.cipherq_wip(dest).exists():
            existing = self._parse_cipherq_manifest(manifest_path)
            if existing is not None and existing.token == token:
                wip = self._layout.cipherq_wip(dest)
                if path == wip and not dest.exists():
                    try:
                        os.replace(path, dest)
                        self._layout.fsync_directory()
                    except OSError:
                        return True
                if existing.state != CIPHERQ_STATE_SEALED:
                    self._seal_cipherq_manifest_locked(existing)
                return True
            LOGGER.warning(
                "raw spill cipherq dest exists without overwrite",
                extra={"kind": kind, "reason": reason, "source": source},
            )
            return False
        if not path.exists():
            return False
        try:
            size, digest = file_size_and_sha256(path)
        except OSError:
            LOGGER.warning(
                "raw spill cipherq hash skipped transient io",
                extra={"kind": kind, "reason": REASON_TRANSIENT_IO, "source": source},
            )
            return False
        src_name = safe_src_name(path.name)
        if src_name is None:
            return False
        if not self._ensure_cipherq_capacity_locked(size):
            LOGGER.warning(
                "raw spill cipherq capacity fail-closed",
                extra={"kind": kind, "reason": reason, "source": source},
            )
            return False
        isolated_at = datetime.now(SHANGHAI_TIMEZONE).isoformat()
        document = {
            "isolated_at": isolated_at,
            "kind": kind,
            "reason": reason,
            "sha256": digest,
            "size_bytes": size,
            "source": source,
            "src_name": src_name,
            "state": CIPHERQ_STATE_PENDING,
            "token": token,
        }
        try:
            self._layout.write_json_atomic(manifest_path, document)
        except OSError as exc:
            LOGGER.warning(
                "raw spill cipherq manifest write failed",
                extra={
                    "error_type": type(exc).__name__,
                    "kind": kind,
                    "reason": reason,
                    "source": source,
                },
            )
            return False
        if dest.exists() or self._layout.cipherq_wip(dest).exists():
            LOGGER.warning(
                "raw spill cipherq dest exists without overwrite",
                extra={"kind": kind, "reason": reason, "source": source},
            )
            return False
        try:
            os.replace(path, dest)
            self._layout.fsync_directory()
        except OSError as exc:
            LOGGER.warning(
                "raw spill cipherq rename failed",
                extra={
                    "error_type": type(exc).__name__,
                    "kind": kind,
                    "reason": reason,
                    "source": source,
                },
            )
            return False
        document["state"] = CIPHERQ_STATE_SEALED
        try:
            self._layout.write_json_atomic(manifest_path, document)
        except OSError:
            return True
        return True

    def _claim_cipherq_locked(self, entry: _CipherqEntry) -> Path | None:
        """原子 .cq → .cq.wip 取得所有权；失败者看不到 dest。"""

        dest = entry.dest
        wip = self._layout.cipherq_wip(dest)
        if wip.exists():
            return None
        if not dest.exists():
            return None
        try:
            os.replace(dest, wip)
            self._layout.fsync_directory()
            return wip
        except OSError:
            return None

    def _restore_cipherq_locked(self, entry: _CipherqEntry) -> Path | None:
        """认领封印密文供原位读取；禁止改回 src_name，避免覆盖活动文件或丢掉 manifest。"""

        return self._claim_cipherq_locked(entry)

    def iter_cipherq_recoverable(
        self, crypto: StreamChunkCrypto, source: str
    ) -> Iterator[RawSpillRecord]:
        """Key 归还后从封印 .cq 恢复原 Raw；失败则重新隔离，不得丢字节。"""

        if not self._layout.directory.exists():
            return
        for entry in self._iter_cipherq_entries_locked():
            if entry.source != source or entry.state != CIPHERQ_STATE_SEALED:
                continue
            if not entry.dest.exists():
                continue
            with self._layout.quota_lock():
                restored = self._restore_cipherq_locked(entry)
            if restored is None:
                continue
            try:
                if entry.kind == "stream" or restored.name.endswith((".stream", ".tmp")):
                    record = self._reader.read_stream(restored, crypto, expected_source=source)
                else:
                    record = self._reader.read(restored, expected_source=source, crypto=crypto)
            except UnknownKeyVersionError:
                with self._layout.quota_lock():
                    self.isolate_ciphertext_locked(
                        restored,
                        source=entry.source,
                        token=entry.token,
                        kind=entry.kind,
                        reason=REASON_KEY_UNAVAILABLE,
                    )
                continue
            except SpillMetadataAuthError:
                with self._layout.quota_lock():
                    self.isolate_ciphertext_locked(
                        restored,
                        source=entry.source,
                        token=entry.token,
                        kind=entry.kind,
                        reason=REASON_AUTH_FAILED,
                    )
                continue
            except OSError:
                continue
            if record is None:
                with self._layout.quota_lock():
                    self.isolate_ciphertext_locked(
                        restored,
                        source=entry.source,
                        token=entry.token,
                        kind=entry.kind,
                        reason=(
                            entry.reason
                            if entry.reason in CIPHERQ_RETAIN_REASONS
                            else REASON_CORRUPT
                        ),
                    )
                continue
            yield record
        for claimed, entry in self._claim_orphan_cipherq_locked(source):
            try:
                record = self._reader.read(claimed, expected_source=source, crypto=crypto)
            except UnknownKeyVersionError:
                with self._layout.quota_lock():
                    self.isolate_ciphertext_locked(
                        claimed,
                        source=entry.source,
                        token=entry.token,
                        kind="spill",
                        reason=REASON_KEY_UNAVAILABLE,
                    )
                continue
            except SpillMetadataAuthError:
                with self._layout.quota_lock():
                    self.isolate_ciphertext_locked(
                        claimed,
                        source=entry.source,
                        token=entry.token,
                        kind="spill",
                        reason=REASON_AUTH_FAILED,
                    )
                continue
            except OSError:
                continue
            if record is None:
                continue
            yield record

    def _claim_orphan_cipherq_locked(self, source: str) -> list[tuple[Path, _CipherqEntry]]:
        claimed: list[tuple[Path, _CipherqEntry]] = []
        with self._layout.quota_lock():
            for path in self._iter_orphan_cipherq_paths_locked():
                parsed = parse_spill_filename(path.name)
                if parsed is None or parsed[0] != source:
                    continue
                if path.name.endswith(".cq.wip"):
                    continue
                token = parsed[1]
                entry = _CipherqEntry(
                    source=source,
                    token=token,
                    kind="spill",
                    reason=REASON_CORRUPT,
                    src_name=path.name,
                    size_bytes=0,
                    sha256="0" * 64,
                    state=CIPHERQ_STATE_SEALED,
                    dest=path,
                    manifest=path.with_name(path.name + ".man"),
                    isolated_at="",
                )
                restored = self._claim_cipherq_locked(entry)
                if restored is not None:
                    claimed.append((restored, entry))
        return claimed

    def remove_claimed_cipherq(self, path: Path) -> None:
        """落库成功后删除认领密文与对应 manifest。"""

        parsed = parse_spill_filename(path.name)
        path.unlink(missing_ok=True)
        if parsed is None:
            return
        source, token, rest = parsed
        if not rest.startswith(".cq"):
            return
        dest, manifest = self._layout.cipherq_paths(source, token)
        dest.unlink(missing_ok=True)
        self._layout.cipherq_wip(dest).unlink(missing_ok=True)
        manifest.unlink(missing_ok=True)
        dest.with_name(dest.name + ".tmp").unlink(missing_ok=True)
        manifest.with_name(manifest.name + ".tmp").unlink(missing_ok=True)
