"""spill 目录回收：按最小 header 分类，可证为空才删除，其余迁入 cipherq。"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path

from app.services.raw_spill_cipherq import (
    CipherQueue,
)
from app.services.raw_spill_format import (
    HEADER_QUARANTINE_SUFFIX,
    LOGGER,
    MARKER_TMP_NAME,
    MAX_CLASSIFY_CONTROL_BYTES,
    REASON_AUTH_FAILED,
    REASON_CORRUPT,
    REASON_KEY_UNAVAILABLE,
    REASON_PROVABLY_EMPTY,
    REASON_TRANSIENT_IO,
    RECLAIM_CURSOR_NAME,
    REWRITE_HDR_NAME,
    SHANGHAI_TIMEZONE,
    SOURCE_PATTERN,
    SPILL_FILE_NAME,
    SPILL_HEADER_BUDGET_BYTES,
    SPILL_LIFE_CORRUPT,
    SPILL_LIFE_INCOMPLETE,
    SPILL_LIFE_TRANSIENT,
    SPILL_LIFE_VALID,
    SPILL_MAGIC,
    STREAM_ID_PATTERN,
    STREAM_LIFE_AUTH_FAILED,
    STREAM_LIFE_CORRUPT_HEADER,
    STREAM_LIFE_HAS_AUTHENTICATED_DATA,
    STREAM_LIFE_HAS_CONTROL,
    STREAM_LIFE_INCOMPLETE_FRAMES,
    STREAM_LIFE_KEY_UNAVAILABLE,
    STREAM_LIFE_LEGAL_HEADER_ONLY,
    STREAM_LIFE_PARTIAL_HEADER,
    STREAM_LIFE_TRANSIENT_IO,
    STREAM_LIFE_UNAUTHENTICATED_PARTIAL,
    STREAM_META_HEADER,
    STREAM_RECORD_HEADER,
    STREAM_UNIT_NAME,
    SpillReclaimResult,
    StreamChunkCrypto,
    parse_spill_filename,
)
from app.services.raw_spill_inspect import StreamInspector, _StreamInspection
from app.services.raw_spill_layout import (
    SpillCounters,
    SpillLayout,
    SpillLimits,
)
from app.services.raw_spill_quota import (
    SpillQuota,
)
from app.services.raw_spill_reader import (
    SpillReader,
)


class SpillReclaimer:
    """统一分类并回收超龄空流、不可认证帧、损坏 spill、临时文件与孤儿标记。"""

    def __init__(
        self,
        layout: SpillLayout,
        limits: SpillLimits,
        counters: SpillCounters,
        quota: SpillQuota,
        reader: SpillReader,
        cipherq: CipherQueue,
        inspector: StreamInspector,
    ) -> None:
        self._layout = layout
        self._limits = limits
        self._counters = counters
        self._quota = quota
        self._reader = reader
        self._cipherq = cipherq
        self._inspector = inspector

    def reclaim_idle_locked(
        self,
        source: str,
        crypto: StreamChunkCrypto,
        now_ts: float,
    ) -> SpillReclaimResult:
        header_only = 0
        partial_header = 0
        corrupt_header = 0
        unauthenticated_partial = 0
        isolated = 0
        key_unavailable = 0
        transient_io = 0
        auth_failed = 0
        cipherq_dropped_before = self._counters.cipherq_capacity_dropped
        temps = self._reclaim_rewrite_tmps_locked(now_ts)
        temps += self._reclaim_marker_tmps_locked()
        temps += self._cipherq.reclaim_cipherq_pending_locked(now_ts)
        spill_counts = self._reclaim_spills_locked(now_ts, crypto)
        isolated += spill_counts[0]
        key_unavailable += spill_counts[1]
        transient_io += spill_counts[2]
        auth_failed += spill_counts[3]
        for path in self._inspector.iter_stream_paths():
            inspection = self._inspector.inspect_stream_path(path, crypto=crypto, now_ts=now_ts)
            if inspection is None:
                continue
            if inspection.kind in {
                STREAM_LIFE_HAS_AUTHENTICATED_DATA,
                STREAM_LIFE_HAS_CONTROL,
            }:
                continue
            if inspection.kind == STREAM_LIFE_TRANSIENT_IO:
                transient_io += 1
                continue
            if inspection.age_seconds < self._limits.header_only_min_age_s:
                continue
            if inspection.kind == STREAM_LIFE_KEY_UNAVAILABLE:
                key_unavailable += 1
                if self._reclaim_classified_locked(inspection, reason=REASON_KEY_UNAVAILABLE):
                    isolated += 1
                continue
            if inspection.kind == STREAM_LIFE_AUTH_FAILED:
                auth_failed += 1
                unauthenticated_partial += 1
                if self._reclaim_classified_locked(inspection, reason=REASON_AUTH_FAILED):
                    isolated += 1
                continue
            if inspection.kind in {
                STREAM_LIFE_UNAUTHENTICATED_PARTIAL,
                STREAM_LIFE_INCOMPLETE_FRAMES,
                STREAM_LIFE_CORRUPT_HEADER,
            }:
                if self._reclaim_classified_locked(inspection, reason=REASON_CORRUPT):
                    isolated += 1
                    if inspection.kind == STREAM_LIFE_CORRUPT_HEADER:
                        corrupt_header += 1
                    else:
                        unauthenticated_partial += 1
                continue
            if inspection.kind in {
                STREAM_LIFE_LEGAL_HEADER_ONLY,
                STREAM_LIFE_PARTIAL_HEADER,
            }:
                self._reclaim_classified_locked(inspection, reason=REASON_PROVABLY_EMPTY)
                if inspection.kind == STREAM_LIFE_LEGAL_HEADER_ONLY:
                    header_only += 1
                else:
                    partial_header += 1
        orphans = self._quota.reclaim_orphans_locked(None)
        orphans += self._reclaim_orphan_handoff_locked()
        expired = self._expire_quarantine_locked(now_ts)
        dropped = self._enforce_quarantine_quota_locked()
        cipherq_expired = self._cipherq.expire_cipherq_locked(now_ts)
        self._cipherq.enforce_cipherq_quota_locked()
        cipherq_manifest_only = self._cipherq.reclaim_cipherq_manifest_only_locked()
        cipherq_dropped = self._counters.cipherq_capacity_dropped - cipherq_dropped_before
        cleaned = header_only + partial_header + corrupt_header
        self._counters.header_only_cleaned += cleaned
        self._counters.isolated_total += isolated
        self._counters.quarantine_expired += expired
        self._counters.quarantine_capacity_dropped += dropped
        self._counters.cipherq_expired += cipherq_expired
        result = SpillReclaimResult(
            header_only=header_only,
            partial_header=partial_header,
            corrupt_header=corrupt_header,
            unauthenticated_partial=unauthenticated_partial,
            incomplete_frames=unauthenticated_partial,
            orphans=orphans,
            isolated=isolated,
            temps_reclaimed=temps,
            quarantine_expired=expired + cipherq_expired,
            quarantine_capacity_dropped=dropped + cipherq_dropped,
            key_unavailable=key_unavailable,
            transient_io=transient_io,
            auth_failed=auth_failed,
            cipherq_expired=cipherq_expired,
            cipherq_capacity_dropped=cipherq_dropped,
            cipherq_manifest_only=cipherq_manifest_only,
        )
        self._counters.last_reclaim = result
        if cleaned or isolated or temps or orphans or key_unavailable or transient_io:
            LOGGER.warning(
                "raw spill artifacts reclaimed",
                extra={
                    "source": source,
                    "header_only": header_only,
                    "partial_header": partial_header,
                    "corrupt_header": corrupt_header,
                    "unauthenticated_partial": unauthenticated_partial,
                    "isolated": isolated,
                    "temps_reclaimed": temps,
                    "orphans": orphans,
                    "quarantine_expired": expired,
                    "quarantine_capacity_dropped": dropped,
                    "key_unavailable": key_unavailable,
                    "transient_io": transient_io,
                    "auth_failed": auth_failed,
                    "cipherq_expired": cipherq_expired,
                    "cipherq_capacity_dropped": cipherq_dropped,
                },
            )
        return result

    def reclaim_nonstream_locked(
        self, now_ts: float, crypto: StreamChunkCrypto | None = None
    ) -> None:
        """spill/tmp/孤儿分类；有 crypto 时认证失败的 .spill 立即离开活动配额。"""

        self._reclaim_marker_tmps_locked()
        self._cipherq.reclaim_cipherq_pending_locked(now_ts)
        self._reclaim_spills_locked(now_ts, crypto)
        self._quota.reclaim_orphans_locked(None)
        self._reclaim_orphan_handoff_locked()
        self._expire_quarantine_locked(now_ts)
        self._enforce_quarantine_quota_locked()
        self._cipherq.expire_cipherq_locked(now_ts)
        self._cipherq.enforce_cipherq_quota_locked()
        self._cipherq.reclaim_cipherq_manifest_only_locked()

    def iter_evidence_paths(self) -> list[Path]:
        if not self._layout.directory.exists():
            return []
        return [
            path
            for path in sorted(self._layout.directory.glob(f"*{HEADER_QUARANTINE_SUFFIX}"))
            if path.is_file() and not path.name.endswith(".tmp")
        ]

    def _reclaim_rewrite_tmps_locked(self, now_ts: float) -> int:
        """完成或隔离 key_version 重写留下的 *.hdr。"""

        if not self._layout.directory.exists():
            return 0
        reclaimed = 0
        for path in list(self._layout.directory.iterdir()):
            match = REWRITE_HDR_NAME.fullmatch(path.name)
            if match is None or not path.is_file():
                continue
            target = path.with_name(path.name[: -len(".hdr")])
            if self._hdr_is_promotable(path, target):
                os.replace(path, target)
                self._layout.fsync_directory()
                reclaimed += 1
                continue
            if self._layout.file_age_seconds(path, now_ts) < self._limits.header_only_min_age_s:
                continue
            self._isolate_named_file_locked(
                path,
                source=match.group("source"),
                token=match.group("stream_id"),
                state="corrupt_tmp",
                kind="hdr",
            )
            reclaimed += 1
        return reclaimed

    def _hdr_is_promotable(self, hdr: Path, target: Path) -> bool:
        try:
            with hdr.open("rb") as handle:
                parsed = self._reader.parse_stream_header_handle(handle, target)
            return parsed is not None
        except OSError:
            return False

    def _reclaim_marker_tmps_locked(self) -> int:
        """分类 *.reserve.tmp / *.quarantine.tmp / *.headerq.tmp。"""

        if not self._layout.directory.exists():
            return 0
        reclaimed = 0
        for path in list(self._layout.directory.iterdir()):
            if not path.is_file() or MARKER_TMP_NAME.fullmatch(path.name) is None:
                continue
            if self._promote_marker_tmp(path):
                reclaimed += 1
                continue
            path.unlink(missing_ok=True)
            reclaimed += 1
        return reclaimed

    def _promote_marker_tmp(self, tmp: Path) -> bool:
        if not tmp.name.endswith(".tmp"):
            return False
        try:
            raw = json.loads(tmp.read_bytes().decode("utf-8"))
            if not isinstance(raw, dict):
                return False
            forbidden = {"phone", "payload", "ciphertext", "secret", "key", "body"}
            if forbidden.intersection(raw):
                return False
            if "source" not in raw:
                return False
            target = tmp.with_name(tmp.name[: -len(".tmp")])
            os.replace(tmp, target)
            self._layout.fsync_directory()
            return True
        except (OSError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            return False

    def _reclaim_spills_locked(
        self, now_ts: float, crypto: StreamChunkCrypto | None = None
    ) -> tuple[int, int, int, int]:
        """损坏/认证失败的 .spill 进入 cipherq；缺 Key / 暂态 I/O 不得销毁。

        锁内只认证小 header，并受文件数/header 字节/时间预算约束。
        """

        if not self._layout.directory.exists():
            return 0, 0, 0, 0
        isolated = 0
        key_unavailable = 0
        transient_io = 0
        auth_failed = 0
        paths = [
            path
            for path in [
                *sorted(self._layout.directory.glob("*.spill")),
                *sorted(self._layout.directory.glob("*.spill.tmp")),
            ]
            if path.is_file()
        ]
        after = self._read_reclaim_cursor_locked()
        if after:
            paths = [path for path in paths if path.name > after] + [
                path for path in paths if path.name <= after
            ]
        started_at = time.monotonic()
        inspected = 0
        used_header_bytes = 0
        last_name = after
        for path in paths:
            if self._limits.reclaim_budget.exhausted(
                inspected=inspected,
                used_header_bytes=used_header_bytes,
                started_at=started_at,
            ):
                break
            kind, source, token = self._classify_spill_path(path)
            inspected += 1
            last_name = path.name
            used_header_bytes += SPILL_HEADER_BUDGET_BYTES
            if kind == SPILL_LIFE_TRANSIENT:
                transient_io += 1
                continue
            if kind == SPILL_LIFE_VALID and path.name.endswith(".spill.tmp"):
                target = path.with_name(path.name[: -len(".tmp")])
                # 新鲜 tmp 仍属于在途 write()；避免在 writer 最终改名前抢先提升。
                try:
                    age_seconds = max(0.0, now_ts - path.stat().st_mtime)
                except FileNotFoundError:
                    if not target.is_file():
                        raise
                    continue
                if age_seconds < self._limits.header_only_min_age_s:
                    continue
                try:
                    os.replace(path, target)
                except FileNotFoundError:
                    # writer 在分类后先完成改名；目标已存在才算这次交错已收敛。
                    if not target.is_file():
                        raise
                    continue
                self._layout.fsync_directory()
                path = target
            if kind == SPILL_LIFE_VALID:
                if crypto is None:
                    continue
                reason = self._reader.inspect_spill_header_auth(path, crypto)
                if reason is None:
                    continue
                if reason == REASON_TRANSIENT_IO:
                    transient_io += 1
                    continue
                if reason == REASON_KEY_UNAVAILABLE:
                    key_unavailable += 1
                elif reason == REASON_AUTH_FAILED:
                    auth_failed += 1
                if self._isolate_named_file_locked(
                    path,
                    source=source or "report",
                    token=token,
                    state=reason,
                    kind="spill",
                ):
                    isolated += 1
                continue
            aged = self._layout.file_age_seconds(path, now_ts) >= self._limits.header_only_min_age_s
            if kind == SPILL_LIFE_INCOMPLETE and path.name.endswith(".spill.tmp") and not aged:
                continue
            if self._isolate_named_file_locked(
                path,
                source=source or "report",
                token=token,
                state=REASON_CORRUPT,
                kind="spill",
            ):
                isolated += 1
        if last_name:
            self._write_reclaim_cursor_locked(last_name)
        return isolated, key_unavailable, transient_io, auth_failed

    def _read_reclaim_cursor_locked(self) -> str:
        path = self._layout.directory / RECLAIM_CURSOR_NAME
        try:
            raw = json.loads(path.read_bytes().decode("utf-8"))
            if not isinstance(raw, dict):
                return ""
            after = str(raw.get("after") or "")
            if not after or "/" in after or "\\" in after or after.startswith("."):
                return ""
            return after
        except (OSError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            return ""

    def _write_reclaim_cursor_locked(self, after: str) -> None:
        if not after or "/" in after or "\\" in after:
            return
        try:
            self._layout.write_json_atomic(
                self._layout.directory / RECLAIM_CURSOR_NAME, {"after": after}
            )
        except OSError:
            return

    def _classify_spill_path(self, path: Path) -> tuple[str, str, str]:
        match = SPILL_FILE_NAME.fullmatch(path.name)
        named_source = match.group("source") if match else ""
        named_digest = match.group("token") if match else ""
        try:
            with path.open("rb") as handle:
                magic = handle.read(len(SPILL_MAGIC))
                if magic == SPILL_MAGIC:
                    raw_len = handle.read(STREAM_RECORD_HEADER.size)
                    if len(raw_len) < STREAM_RECORD_HEADER.size:
                        return SPILL_LIFE_INCOMPLETE, named_source, named_digest
                    (frame_len,) = STREAM_RECORD_HEADER.unpack(raw_len)
                    if (
                        frame_len < STREAM_META_HEADER.size
                        or frame_len > MAX_CLASSIFY_CONTROL_BYTES
                    ):
                        return SPILL_LIFE_CORRUPT, named_source, named_digest
                    frame = handle.read(frame_len)
                    if len(frame) != frame_len:
                        return SPILL_LIFE_INCOMPLETE, named_source, named_digest
                    if match is None:
                        return SPILL_LIFE_CORRUPT, named_source or "report", named_digest
                    if not handle.read(1):
                        return SPILL_LIFE_INCOMPLETE, named_source, named_digest
                    return SPILL_LIFE_VALID, named_source, named_digest
                handle.seek(0)
                header_bytes = handle.readline()
                if not header_bytes.endswith(b"\n"):
                    return SPILL_LIFE_INCOMPLETE, named_source, named_digest
                header = json.loads(header_bytes.decode("utf-8"))
                source = str(header["source"])
                digest = str(header["payload_sha256"])
                expected = self._layout.path(source, digest)
                if path not in {expected, expected.with_suffix(".spill.tmp")}:
                    return SPILL_LIFE_CORRUPT, named_source or source, named_digest or digest
                if not handle.read(1):
                    return SPILL_LIFE_INCOMPLETE, source, digest
                return SPILL_LIFE_VALID, source, digest
        except OSError:
            token = named_digest or hashlib.sha256(path.name.encode("utf-8")).hexdigest()[:32]
            source = named_source or "report"
            return SPILL_LIFE_TRANSIENT, source, token
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            token = named_digest or hashlib.sha256(path.name.encode("utf-8")).hexdigest()[:32]
            source = named_source or "report"
            return SPILL_LIFE_CORRUPT, source, token

    def _reclaim_orphan_handoff_locked(self) -> int:
        """stream 删除后遗留的 .quarantine / 无主 .reserve 已在 orphans；这里只清交接标记。"""

        if not self._layout.directory.exists():
            return 0
        reclaimed = 0
        seen: set[tuple[str, str]] = set()
        for path in list(self._layout.directory.iterdir()):
            match = STREAM_UNIT_NAME.fullmatch(path.name)
            if match is None or not path.is_file():
                continue
            key = (match.group("source"), match.group("stream_id"))
            if key in seen:
                continue
            seen.add(key)
            source, stream_id = key
            stream_exists = (
                self._layout.stream_tmp(source, stream_id).exists()
                or self._layout.stream_path(source, stream_id).exists()
                or self._layout.stream_path(source, stream_id)
                .with_suffix(".stream.stream")
                .exists()
            )
            if stream_exists:
                continue
            quarantine = self._layout.quarantine_path(source, stream_id)
            if quarantine.exists():
                quarantine.unlink(missing_ok=True)
                quarantine.with_name(quarantine.name + ".tmp").unlink(missing_ok=True)
                reclaimed += 1
        return reclaimed

    def _expire_quarantine_locked(self, now_ts: float) -> int:
        expired = 0
        for path in self.iter_evidence_paths():
            if self._layout.file_age_seconds(path, now_ts) < self._limits.quarantine_retention_s:
                continue
            path.unlink(missing_ok=True)
            expired += 1
        return expired

    def _enforce_quarantine_quota_locked(self) -> int:
        paths = self.iter_evidence_paths()
        sizes: list[tuple[float, int, Path]] = []
        total = 0
        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                continue
            sizes.append((stat.st_mtime, stat.st_size, path))
            total += stat.st_size
        dropped = 0
        sizes.sort()
        while sizes and (
            len(sizes) > self._limits.max_quarantine_files
            or total > self._limits.max_quarantine_bytes
        ):
            _mtime, size, path = sizes.pop(0)
            path.unlink(missing_ok=True)
            total -= size
            dropped += 1
        return dropped

    def _isolate_named_file_locked(
        self,
        path: Path,
        *,
        source: str,
        token: str,
        state: str,
        kind: str,
    ) -> bool:
        """把仍可能有效的密文原子迁入 cipherq；禁止先 unlink。"""

        reason = state
        if state in {SPILL_LIFE_CORRUPT, SPILL_LIFE_INCOMPLETE, "corrupt_tmp"}:
            reason = REASON_CORRUPT
        elif state == "auth_failed":
            reason = REASON_AUTH_FAILED
        return self._cipherq.isolate_ciphertext_locked(
            path,
            source=source,
            token=token,
            kind=kind,
            reason=reason,
        )

    def _write_nonactive_evidence(
        self,
        source: str,
        token: str,
        state: str,
        *,
        kind: str,
        size_bytes: int,
    ) -> Path:
        """非活动隔离证据：无手机号、正文、密文、Key 或完整路径。"""

        self._enforce_quarantine_quota_locked()
        self._layout.directory.mkdir(parents=True, exist_ok=True)
        target = self._layout.directory / f"{source}-{token}{HEADER_QUARANTINE_SUFFIX}"
        payload = json.dumps(
            {
                "isolated_at": datetime.now(SHANGHAI_TIMEZONE).isoformat(),
                "kind": kind,
                "size_bytes": int(size_bytes),
                "source": source,
                "state": state,
                "stream_id": token,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        tmp = target.with_name(target.name + ".tmp")
        with tmp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        self._layout.fsync_directory()
        return target

    def _reclaim_classified_locked(
        self,
        inspection: _StreamInspection,
        *,
        reason: str,
    ) -> bool:
        """按原因处置 stream：可证为空才删除，其余保留原字节。"""

        if reason == REASON_PROVABLY_EMPTY:
            if SOURCE_PATTERN.fullmatch(inspection.source) and STREAM_ID_PATTERN.fullmatch(
                inspection.stream_id
            ):
                self._layout.remove_stream_locked(inspection.source, inspection.stream_id)
                return True
            inspection.path.unlink(missing_ok=True)
            return True
        token = inspection.stream_id
        if not token:
            token = hashlib.sha256(inspection.path.name.encode("utf-8")).hexdigest()[:32]
        moved = self._cipherq.isolate_ciphertext_locked(
            inspection.path,
            source=inspection.source or "report",
            token=token,
            kind="stream",
            reason=reason,
        )
        if not moved:
            return False
        if SOURCE_PATTERN.fullmatch(inspection.source) and STREAM_ID_PATTERN.fullmatch(
            inspection.stream_id
        ):
            self._layout.release_stream_activity_locked(inspection.source, inspection.stream_id)
            if inspection.kind == STREAM_LIFE_CORRUPT_HEADER:
                self._layout.write_header_quarantine(
                    inspection.source, inspection.stream_id, inspection.kind
                )
        return True

    def isolate_spill_auth_failure(self, path: Path, *, locked: bool) -> None:
        match = SPILL_FILE_NAME.fullmatch(path.name)
        parsed = parse_spill_filename(path.name)
        source = match.group("source") if match else (parsed[0] if parsed else "report")
        if match is not None:
            token = match.group("token")
        elif parsed is not None:
            token = parsed[1]
        else:
            token = hashlib.sha256(path.name.encode("utf-8")).hexdigest()[:32]
        if locked:
            self._isolate_named_file_locked(
                path, source=source, token=token, state="auth_failed", kind="spill"
            )
        else:
            with self._layout.quota_lock():
                self._isolate_named_file_locked(
                    path, source=source, token=token, state="auth_failed", kind="spill"
                )
        self._counters.isolated_total += 1
        previous = self._counters.last_reclaim
        self._counters.last_reclaim = SpillReclaimResult(
            header_only=previous.header_only,
            partial_header=previous.partial_header,
            corrupt_header=previous.corrupt_header,
            incomplete_frames=previous.incomplete_frames,
            unauthenticated_partial=previous.unauthenticated_partial,
            orphans=previous.orphans,
            isolated=previous.isolated + 1,
            temps_reclaimed=previous.temps_reclaimed,
            quarantine_expired=previous.quarantine_expired,
            quarantine_capacity_dropped=previous.quarantine_capacity_dropped,
            key_unavailable=previous.key_unavailable,
            transient_io=previous.transient_io,
            auth_failed=previous.auth_failed + 1,
            cipherq_expired=previous.cipherq_expired,
            cipherq_capacity_dropped=previous.cipherq_capacity_dropped,
            cipherq_manifest_only=previous.cipherq_manifest_only,
        )
