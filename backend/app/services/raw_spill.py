"""厂商拉走即消费响应的本地加密 spill，供落库前崩溃恢复。

实现按职责拆在 raw_spill_* 模块：格式常量（format）、无状态编解码（codec）、
目录布局与共享状态（layout）、容量租约（quota）、认证读取（reader）、
密文隔离区（cipherq）、目录回收（reclaim）与捕获流（stream）。本模块保留
既有公开名并由 RawSpillStore 组合各组件；调用方继续从这里导入。
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import secrets
import time
from collections.abc import Iterator
from pathlib import Path

from app.services.crypto import UnknownKeyVersionError
from app.services.raw_spill_cipherq import CipherQueue
from app.services.raw_spill_codec import (
    _spill_header_context,
    canonical_spill_header,
)
from app.services.raw_spill_format import (
    CAPTURE_COMPLETE,
    CAPTURE_COMPLETE_TOO_LARGE,
    CAPTURE_FRAME_OVERHEAD_BYTES,
    CAPTURE_PROTOCOL_INVALID,
    CAPTURE_TRUNCATED,
    CAPTURE_UNKNOWN_LEGACY,
    CIPHERQ_MANIFEST_SUFFIX,
    CIPHERQ_STATE_PENDING,
    CIPHERQ_STATE_SEALED,
    CONTROL_FRAME_BUDGET_BYTES,
    CONTROL_FRAME_COUNT,
    DATA_FRAME_OVERHEAD_BYTES,
    DEFAULT_CIPHERQ_RETENTION_S,
    DEFAULT_MAX_CIPHERQ_BYTES,
    DEFAULT_MAX_CIPHERQ_FILES,
    DEFAULT_MAX_PENDING_FILES,
    DEFAULT_MAX_QUARANTINE_BYTES,
    DEFAULT_MAX_QUARANTINE_FILES,
    DEFAULT_MAX_TOTAL_BYTES,
    DEFAULT_QUARANTINE_RETENTION_S,
    DEFAULT_RECLAIM_MAX_FILES,
    DEFAULT_RECLAIM_MAX_HEADER_BYTES,
    DEFAULT_RECLAIM_MAX_SECONDS,
    DEFAULT_RECOVER_MAX_FILES,
    DEFAULT_RECOVER_MAX_SECONDS,
    DIRECTORY_METADATA_BYTES,
    HEADER_ONLY_RECLAIM_AFTER_S,
    HEADER_QUARANTINE_SUFFIX,
    INTERNAL_FRAME_SIZE,
    LOGGER,
    NON_REPLAYABLE_CAPTURE_STATES,
    REASON_AUTH_FAILED,
    REASON_KEY_UNAVAILABLE,
    REASON_TRANSIENT_IO,
    RECOVERY_CAPTURE_BYTES,
    SHA256_PATTERN,
    SOURCE_PATTERN,
    SPILL_HEADER_BUDGET_BYTES,
    SPILL_MAGIC,
    STREAM_HEADER_BUDGET_BYTES,
    STREAM_LIFE_CORRUPT_HEADER,
    STREAM_LIFE_INCOMPLETE_FRAMES,
    STREAM_LIFE_LEGAL_HEADER_ONLY,
    STREAM_LIFE_PARTIAL_HEADER,
    STREAM_LIFE_UNAUTHENTICATED_PARTIAL,
    STREAM_MAGIC,
    STREAM_META_HEADER,
    STREAM_RECORD_HEADER,
    VALID_CAPTURE_STATES,
    ArtifactStats,
    HeaderOnlyStats,
    RawSpillDegraded,
    RawSpillRecord,
    RawSpillSettings,
    ReclaimRoundBudget,
    RecoverMemoryProbe,
    RecoverRoundBudget,
    SpillMetadataAuthError,
    SpillQuotaExceeded,
    SpillReclaimResult,
    SpillReservation,
    StreamChunkCrypto,
    capture_reservation_bytes,
    discard_header_only_stream,
    durable_persist_capture_state,
    is_activity_filename,
    is_non_replayable_capture,
    iter_records_for_recover,
    manage_raw_spill_stream,
    max_internal_frames,
    normalize_capture_state,
    spill_file_identity_matches,
)
from app.services.raw_spill_inspect import StreamInspector
from app.services.raw_spill_layout import SpillCounters, SpillLayout, SpillLimits
from app.services.raw_spill_quota import SpillQuota
from app.services.raw_spill_reader import SpillReader
from app.services.raw_spill_reclaim import SpillReclaimer
from app.services.raw_spill_stream import (
    RawSpillStream,
)

# 调用方实际使用的公开名；其余格式常量请直接从 raw_spill_format 导入。
__all__ = [
    "CAPTURE_COMPLETE",
    "CAPTURE_COMPLETE_TOO_LARGE",
    "CAPTURE_FRAME_OVERHEAD_BYTES",
    "CAPTURE_PROTOCOL_INVALID",
    "capture_reservation_bytes",
    "CAPTURE_TRUNCATED",
    "CAPTURE_UNKNOWN_LEGACY",
    "CIPHERQ_MANIFEST_SUFFIX",
    "CIPHERQ_STATE_PENDING",
    "CIPHERQ_STATE_SEALED",
    "CONTROL_FRAME_BUDGET_BYTES",
    "CONTROL_FRAME_COUNT",
    "DATA_FRAME_OVERHEAD_BYTES",
    "DEFAULT_RECLAIM_MAX_HEADER_BYTES",
    "DEFAULT_RECLAIM_MAX_SECONDS",
    "DIRECTORY_METADATA_BYTES",
    "discard_header_only_stream",
    "durable_persist_capture_state",
    "HEADER_QUARANTINE_SUFFIX",
    "INTERNAL_FRAME_SIZE",
    "is_activity_filename",
    "is_non_replayable_capture",
    "iter_records_for_recover",
    "manage_raw_spill_stream",
    "max_internal_frames",
    "NON_REPLAYABLE_CAPTURE_STATES",
    "normalize_capture_state",
    "RawSpillDegraded",
    "RawSpillStore",
    "RawSpillStream",
    "ReclaimRoundBudget",
    "RecoverMemoryProbe",
    "RecoverRoundBudget",
    "RECOVERY_CAPTURE_BYTES",
    "spill_file_identity_matches",
    "SPILL_MAGIC",
    "SpillQuotaExceeded",
    "STREAM_HEADER_BUDGET_BYTES",
    "STREAM_MAGIC",
    "STREAM_META_HEADER",
    "STREAM_RECORD_HEADER",
    "VALID_CAPTURE_STATES",
]


class RawSpillStore:
    """只保存 AES-GCM 密文；明文手机号不得进入 spill 文件。"""

    def __init__(
        self,
        directory: Path,
        *,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
        max_pending_files: int = DEFAULT_MAX_PENDING_FILES,
        header_only_min_age_s: float = HEADER_ONLY_RECLAIM_AFTER_S,
        recover_budget: RecoverRoundBudget | None = None,
        reclaim_budget: ReclaimRoundBudget | None = None,
        memory_probe: RecoverMemoryProbe | None = None,
        max_quarantine_files: int = DEFAULT_MAX_QUARANTINE_FILES,
        max_quarantine_bytes: int = DEFAULT_MAX_QUARANTINE_BYTES,
        quarantine_retention_s: float = DEFAULT_QUARANTINE_RETENTION_S,
        max_cipherq_files: int = DEFAULT_MAX_CIPHERQ_FILES,
        max_cipherq_bytes: int = DEFAULT_MAX_CIPHERQ_BYTES,
        cipherq_retention_s: float = DEFAULT_CIPHERQ_RETENTION_S,
    ) -> None:
        if max_total_bytes < 1 or max_pending_files < 1:
            raise ValueError("raw spill quotas must be positive")
        if header_only_min_age_s < 0:
            raise ValueError("header_only_min_age_s must not be negative")
        if max_quarantine_files < 1 or max_quarantine_bytes < 1:
            raise ValueError("raw spill quarantine quotas must be positive")
        if quarantine_retention_s < 0:
            raise ValueError("quarantine_retention_s must not be negative")
        if max_cipherq_files < 1 or max_cipherq_bytes < 1:
            raise ValueError("raw spill cipherq quotas must be positive")
        if cipherq_retention_s < 0:
            raise ValueError("cipherq_retention_s must not be negative")
        self._layout = SpillLayout(directory)
        self._limits = SpillLimits(
            max_total_bytes=max_total_bytes,
            max_pending_files=max_pending_files,
            header_only_min_age_s=header_only_min_age_s,
            max_quarantine_files=max_quarantine_files,
            max_quarantine_bytes=max_quarantine_bytes,
            quarantine_retention_s=quarantine_retention_s,
            max_cipherq_files=max_cipherq_files,
            max_cipherq_bytes=max_cipherq_bytes,
            cipherq_retention_s=cipherq_retention_s,
            recover_budget=recover_budget or RecoverRoundBudget(),
            reclaim_budget=reclaim_budget or ReclaimRoundBudget(),
        )
        self._counters = SpillCounters()
        self._quota = SpillQuota(self._layout, self._limits)
        self._reader = SpillReader(self._layout, memory_probe)
        self._cipherq = CipherQueue(self._layout, self._limits, self._counters, self._reader)
        self._inspector = StreamInspector(self._layout)
        self._reclaimer = SpillReclaimer(
            self._layout,
            self._limits,
            self._counters,
            self._quota,
            self._reader,
            self._cipherq,
            self._inspector,
        )

    @property
    def directory(self) -> Path:
        return self._layout.directory

    # 限额是公开可写属性：读写都落到组件共享的 SpillLimits，修改立即生效。

    @property
    def max_total_bytes(self) -> int:
        return self._limits.max_total_bytes

    @max_total_bytes.setter
    def max_total_bytes(self, value: int) -> None:
        self._limits.max_total_bytes = value

    @property
    def max_pending_files(self) -> int:
        return self._limits.max_pending_files

    @max_pending_files.setter
    def max_pending_files(self, value: int) -> None:
        self._limits.max_pending_files = value

    @property
    def header_only_min_age_s(self) -> float:
        return self._limits.header_only_min_age_s

    @header_only_min_age_s.setter
    def header_only_min_age_s(self, value: float) -> None:
        self._limits.header_only_min_age_s = value

    @property
    def max_quarantine_files(self) -> int:
        return self._limits.max_quarantine_files

    @max_quarantine_files.setter
    def max_quarantine_files(self, value: int) -> None:
        self._limits.max_quarantine_files = value

    @property
    def max_quarantine_bytes(self) -> int:
        return self._limits.max_quarantine_bytes

    @max_quarantine_bytes.setter
    def max_quarantine_bytes(self, value: int) -> None:
        self._limits.max_quarantine_bytes = value

    @property
    def quarantine_retention_s(self) -> float:
        return self._limits.quarantine_retention_s

    @quarantine_retention_s.setter
    def quarantine_retention_s(self, value: float) -> None:
        self._limits.quarantine_retention_s = value

    @property
    def max_cipherq_files(self) -> int:
        return self._limits.max_cipherq_files

    @max_cipherq_files.setter
    def max_cipherq_files(self, value: int) -> None:
        self._limits.max_cipherq_files = value

    @property
    def max_cipherq_bytes(self) -> int:
        return self._limits.max_cipherq_bytes

    @max_cipherq_bytes.setter
    def max_cipherq_bytes(self, value: int) -> None:
        self._limits.max_cipherq_bytes = value

    @property
    def cipherq_retention_s(self) -> float:
        return self._limits.cipherq_retention_s

    @cipherq_retention_s.setter
    def cipherq_retention_s(self, value: float) -> None:
        self._limits.cipherq_retention_s = value

    @property
    def recover_budget(self) -> RecoverRoundBudget:
        return self._limits.recover_budget

    @recover_budget.setter
    def recover_budget(self, value: RecoverRoundBudget) -> None:
        self._limits.recover_budget = value

    @property
    def reclaim_budget(self) -> ReclaimRoundBudget:
        return self._limits.reclaim_budget

    @reclaim_budget.setter
    def reclaim_budget(self, value: ReclaimRoundBudget) -> None:
        self._limits.reclaim_budget = value

    @property
    def last_reclaim(self) -> SpillReclaimResult:
        return self._counters.last_reclaim

    @last_reclaim.setter
    def last_reclaim(self, value: SpillReclaimResult) -> None:
        self._counters.last_reclaim = value

    @classmethod
    def from_settings(cls, settings: RawSpillSettings) -> RawSpillStore:
        return cls(
            settings.raw_spill_dir,
            max_total_bytes=int(settings.raw_spill_max_total_bytes),
            max_pending_files=int(settings.raw_spill_max_pending_files),
            recover_budget=RecoverRoundBudget(
                max_files=int(
                    getattr(settings, "raw_spill_recover_max_files", DEFAULT_RECOVER_MAX_FILES)
                ),
                max_plaintext_bytes=int(
                    getattr(
                        settings,
                        "raw_spill_recover_max_plaintext_bytes",
                        RECOVERY_CAPTURE_BYTES,
                    )
                ),
                max_seconds=float(
                    getattr(
                        settings,
                        "raw_spill_recover_max_seconds",
                        DEFAULT_RECOVER_MAX_SECONDS,
                    )
                ),
            ),
            reclaim_budget=ReclaimRoundBudget(
                max_files=int(
                    getattr(settings, "raw_spill_reclaim_max_files", DEFAULT_RECLAIM_MAX_FILES)
                ),
                max_header_bytes=int(
                    getattr(
                        settings,
                        "raw_spill_reclaim_max_header_bytes",
                        DEFAULT_RECLAIM_MAX_HEADER_BYTES,
                    )
                ),
                max_seconds=float(
                    getattr(
                        settings,
                        "raw_spill_reclaim_max_seconds",
                        DEFAULT_RECLAIM_MAX_SECONDS,
                    )
                ),
            ),
            max_quarantine_files=int(
                getattr(settings, "raw_spill_max_quarantine_files", DEFAULT_MAX_QUARANTINE_FILES)
            ),
            max_quarantine_bytes=int(
                getattr(settings, "raw_spill_max_quarantine_bytes", DEFAULT_MAX_QUARANTINE_BYTES)
            ),
            quarantine_retention_s=float(
                getattr(
                    settings,
                    "raw_spill_quarantine_retention_s",
                    DEFAULT_QUARANTINE_RETENTION_S,
                )
            ),
            max_cipherq_files=int(
                getattr(settings, "raw_spill_max_cipherq_files", DEFAULT_MAX_CIPHERQ_FILES)
            ),
            max_cipherq_bytes=int(
                getattr(settings, "raw_spill_max_cipherq_bytes", DEFAULT_MAX_CIPHERQ_BYTES)
            ),
            cipherq_retention_s=float(
                getattr(settings, "raw_spill_cipherq_retention_s", DEFAULT_CIPHERQ_RETENTION_S)
            ),
        )

    def accounted_usage(self) -> int:
        """未覆盖文件字节 + 各租约 reserved_bytes；禁止把缺失预留当零。"""

        with self._layout.quota_lock():
            return self._quota.accounted_usage_locked()

    def can_accept(self, additional_bytes: int = 0, *, additional_files: int = 1) -> bool:
        if additional_bytes < 0:
            raise ValueError("additional_bytes must not be negative")
        if additional_files < 0:
            raise ValueError("additional_files must not be negative")
        with self._layout.quota_lock():
            return self._quota.can_accept_locked(
                additional_bytes, additional_files=additional_files
            )

    def list_reservations(self) -> list[SpillReservation]:
        """测试与巡检用；账本不含 PII。"""

        with self._layout.quota_lock():
            return list(self._quota.iter_reservations_locked())

    def write(
        self,
        *,
        source: str,
        payload_sha256: str,
        key_version: int,
        http_status: int,
        content_encoding: str,
        payload_enc: bytes,
        crypto: StreamChunkCrypto,
        capture_state: str = CAPTURE_COMPLETE,
    ) -> Path:
        """先写认证 header 与密文并 fsync，再原子改名，保证 kill -9 后仍可恢复。"""

        if not payload_enc:
            raise ValueError("raw spill payload is empty")
        capture_state = normalize_capture_state(capture_state)
        payload_enc_sha256 = hashlib.sha256(payload_enc).hexdigest()
        meta = canonical_spill_header(
            source=source,
            payload_sha256=payload_sha256,
            payload_enc_sha256=payload_enc_sha256,
            key_version=key_version,
            http_status=http_status,
            content_encoding=content_encoding,
            capture_state=capture_state,
        )
        parsed = json.loads(meta.decode("utf-8"))
        encrypted = crypto.encrypt_bound_bytes(meta, _spill_header_context(parsed))
        frame = STREAM_META_HEADER.pack(len(meta)) + meta + encrypted.payload
        extra = len(SPILL_MAGIC) + STREAM_RECORD_HEADER.size + len(frame) + len(payload_enc)
        if extra > len(payload_enc) + SPILL_HEADER_BUDGET_BYTES:
            raise ValueError("authenticated spill header exceeds budget")
        with self._layout.quota_lock():
            self._reclaimer.reclaim_nonstream_locked(time.time(), crypto)
            # 在途 .stream 与即将落盘的 .spill 是同一捕获的两份密文，不得互相占满文件配额。
            same_capture = self._quota.reservation_for_source_locked(source) is not None
            if self._quota.pending_spill_count() >= self._limits.max_pending_files or (
                not same_capture
                and self._quota.accounted_usage_locked() + extra > self._limits.max_total_bytes
            ):
                raise SpillQuotaExceeded("raw spill quota exceeded")
        if SOURCE_PATTERN.fullmatch(source) is None:
            raise ValueError("invalid raw spill source")
        if SHA256_PATTERN.fullmatch(payload_sha256) is None:
            raise ValueError("invalid raw spill digest")
        self._layout.directory.mkdir(parents=True, exist_ok=True)
        artifact_id = secrets.token_hex(16)
        target = self._layout.directory / f"{source}-{artifact_id}.spill"
        tmp = self._layout.directory / f"{source}-{artifact_id}.spill.tmp"
        with tmp.open("wb") as handle:
            handle.write(SPILL_MAGIC)
            handle.write(STREAM_RECORD_HEADER.pack(len(frame)))
            handle.write(frame)
            handle.write(payload_enc)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(tmp, target)
        except FileNotFoundError:
            # 回收器可能已把同一个 tmp 原子提升为目标；随机 artifact_id 保证目标唯一。
            if not target.is_file():
                raise
        self._layout.fsync_directory()
        return target

    def list_pending(
        self, source: str | None = None, crypto: StreamChunkCrypto | None = None
    ) -> list[RawSpillRecord]:
        return list(self.iter_pending(source, crypto))

    def list_pending_streams(
        self, crypto: StreamChunkCrypto, source: str | None = None
    ) -> list[RawSpillRecord]:
        """把接收中崩溃留下的加密流装配为可落库的截断/完整 spill 记录。"""

        return list(self.iter_pending_streams(crypto, source))

    def iter_pending(
        self, source: str | None = None, crypto: StreamChunkCrypto | None = None
    ) -> Iterator[RawSpillRecord]:
        """按文件惰性产出 .spill；可先按文件名 source 过滤，不读其它来源 payload。"""

        yield from self._iter_records(source, streams=False, crypto=crypto, isolate=False)

    def iter_pending_streams(
        self, crypto: StreamChunkCrypto, source: str | None = None
    ) -> Iterator[RawSpillRecord]:
        """按文件惰性装配 .stream；可先按文件名 source 过滤。"""

        yield from self._iter_records(source, streams=True, crypto=crypto, isolate=False)

    def iter_recoverable(self, crypto: StreamChunkCrypto, source: str) -> Iterator[RawSpillRecord]:
        """恢复入口：只产出指定 source，一次一条；认证失败隔离，不写库。"""

        if SOURCE_PATTERN.fullmatch(source) is None:
            raise ValueError("invalid raw spill source")
        yield from self._iter_records(source, streams=False, crypto=crypto, isolate=True)
        yield from self._iter_records(source, streams=True, crypto=crypto, isolate=False)
        yield from self._cipherq.iter_cipherq_recoverable(crypto, source)

    def _iter_records(
        self,
        source: str | None,
        *,
        streams: bool,
        crypto: StreamChunkCrypto | None,
        isolate: bool,
    ) -> Iterator[RawSpillRecord]:
        if source is not None and SOURCE_PATTERN.fullmatch(source) is None:
            raise ValueError("invalid raw spill source")
        if not self._layout.directory.exists():
            return
        for path in self._pending_paths(source, streams=streams):
            try:
                if streams:
                    if crypto is None:
                        continue
                    record = self._reader.read_stream(path, crypto, expected_source=source)
                else:
                    record = self._reader.read(path, expected_source=source, crypto=crypto)
            except SpillMetadataAuthError:
                LOGGER.warning(
                    "raw spill metadata authentication failed",
                    extra={"kind": "spill", "state": REASON_AUTH_FAILED},
                )
                if isolate and not streams:
                    self._reclaimer.isolate_spill_auth_failure(path, locked=False)
                continue
            except UnknownKeyVersionError:
                LOGGER.warning(
                    "raw spill key unavailable",
                    extra={
                        "kind": "stream" if streams else "spill",
                        "state": REASON_KEY_UNAVAILABLE,
                    },
                )
                continue
            except OSError as exc:
                LOGGER.warning(
                    "raw spill recover skipped transient io",
                    extra={
                        "error_type": type(exc).__name__,
                        "kind": "stream" if streams else "spill",
                        "state": REASON_TRANSIENT_IO,
                    },
                )
                continue
            except Exception as exc:
                LOGGER.warning(
                    "raw spill recover skipped unreadable file",
                    extra={
                        "error_type": type(exc).__name__,
                        "kind": "stream" if streams else "spill",
                    },
                )
                continue
            if record is None:
                continue
            size = len(record.payload_enc)
            self._reader.probe_acquire(size)
            try:
                yield record
            finally:
                self._reader.probe_release(size)

    def _pending_paths(self, source: str | None, *, streams: bool) -> list[Path]:
        prefix = f"{source}-*" if source else "*"
        if streams:
            return [
                *sorted(self._layout.directory.glob(f"{prefix}.stream")),
                *sorted(self._layout.directory.glob(f"{prefix}.stream.tmp")),
            ]
        return sorted(self._layout.directory.glob(f"{prefix}.spill"))

    def remove(self, source: str, payload_sha256: str) -> None:
        """只删除历史 digest 文件名；权威清理以 record.path 为准。"""

        path = self._layout.path(source, payload_sha256)
        path.unlink(missing_ok=True)

    def remove_stream(self, source: str, stream_id: str) -> None:
        with self._layout.quota_lock():
            self._layout.remove_stream_locked(source, stream_id)

    def open_stream(
        self,
        source: str,
        crypto: StreamChunkCrypto,
        *,
        capture_bytes: int = RECOVERY_CAPTURE_BYTES,
    ) -> RawSpillStream:
        """原子预留一次恢复捕获容量后再建 stream；失败不得留下租约或调用厂商。"""

        reserved_bytes = capture_reservation_bytes(capture_bytes)
        with self._layout.quota_lock():
            self._reclaimer.reclaim_idle_locked(source, crypto, time.time())
            if not self._quota.can_accept_locked(reserved_bytes, additional_files=1):
                raise SpillQuotaExceeded("raw spill quota exceeded")
            lease_id = secrets.token_hex(16)
            try:
                reservation = self._quota.write_reservation_locked(source, lease_id, reserved_bytes)
            except OSError as exc:
                if exc.errno == errno.ENOSPC:
                    raise SpillQuotaExceeded("raw spill disk full") from exc
                raise
            try:
                stream = RawSpillStream(
                    self,
                    source,
                    crypto,
                    reservation,
                    capture_bytes=capture_bytes,
                )
            except OSError as exc:
                self._layout.remove_stream_locked(source, lease_id)
                if exc.errno == errno.ENOSPC:
                    raise SpillQuotaExceeded("raw spill disk full") from exc
                raise
            except Exception:
                self._layout.remove_stream_locked(source, lease_id)
                raise
            return stream

    def header_only_stats(self) -> HeaderOnlyStats:
        """当前目录 header-only 数量、最老年龄与累计清理次数；不含 PII。"""

        header_only = 0
        partial_header = 0
        corrupt_header = 0
        unauthenticated = 0
        oldest: float | None = None
        now_ts = time.time()
        if self._layout.directory.exists():
            for path in self._inspector.iter_stream_paths():
                inspection = self._inspector.inspect_stream_path(path, crypto=None, now_ts=now_ts)
                if inspection is None:
                    continue
                if inspection.kind == STREAM_LIFE_LEGAL_HEADER_ONLY:
                    header_only += 1
                    oldest = (
                        inspection.age_seconds
                        if oldest is None
                        else max(oldest, inspection.age_seconds)
                    )
                elif inspection.kind == STREAM_LIFE_PARTIAL_HEADER:
                    partial_header += 1
                elif inspection.kind == STREAM_LIFE_CORRUPT_HEADER:
                    corrupt_header += 1
                elif inspection.kind in {
                    STREAM_LIFE_UNAUTHENTICATED_PARTIAL,
                    STREAM_LIFE_INCOMPLETE_FRAMES,
                }:
                    unauthenticated += 1
        return HeaderOnlyStats(
            header_only_count=header_only,
            oldest_age_seconds=oldest,
            cleaned_total=self._counters.header_only_cleaned,
            partial_header_count=partial_header,
            corrupt_header_count=corrupt_header,
            unauthenticated_partial_count=unauthenticated,
        )

    def artifact_stats(self) -> ArtifactStats:
        """活动配额与非活动隔离容量；供测试与巡检，不进 Prometheus 事实表。"""

        quarantine_count = 0
        quarantine_bytes = 0
        oldest: float | None = None
        cipherq_count = 0
        cipherq_bytes = 0
        oldest_cipherq: float | None = None
        now_ts = time.time()
        if self._layout.directory.exists():
            for path in self._reclaimer.iter_evidence_paths():
                quarantine_count += 1
                try:
                    quarantine_bytes += path.stat().st_size
                    age = self._layout.file_age_seconds(path, now_ts)
                except OSError:
                    continue
                oldest = age if oldest is None else max(oldest, age)
            cipherq_count, cipherq_bytes = self._cipherq.cipherq_usage_locked()
            for path in self._cipherq.iter_cipherq_payload_paths_locked():
                try:
                    age = self._layout.file_age_seconds(path, now_ts)
                except OSError:
                    continue
                oldest_cipherq = age if oldest_cipherq is None else max(oldest_cipherq, age)
        return ArtifactStats(
            active_count=self._quota.pending_count(),
            quarantine_count=quarantine_count + cipherq_count,
            quarantine_bytes=quarantine_bytes + cipherq_bytes,
            oldest_quarantine_age_seconds=oldest
            if oldest_cipherq is None
            else (oldest_cipherq if oldest is None else max(oldest, oldest_cipherq)),
            isolated_total=self._counters.isolated_total,
            expired_total=self._counters.quarantine_expired + self._counters.cipherq_expired,
            capacity_dropped_total=(
                self._counters.quarantine_capacity_dropped + self._counters.cipherq_capacity_dropped
            ),
            cipherq_count=cipherq_count,
            cipherq_bytes=cipherq_bytes,
            oldest_cipherq_age_seconds=oldest_cipherq,
        )

    def reclaim_idle(self, source: str, crypto: StreamChunkCrypto) -> SpillReclaimResult:
        """统一分类并回收：超龄空流、不可认证帧、损坏 spill、临时文件与孤儿标记。

        已连续认证的 data 不得当空文件删除；不可读对象进入非活动隔离，不占活动配额。
        清理覆盖共享目录全部来源，避免 Report 残留永久阻断 Reply。
        """

        if SOURCE_PATTERN.fullmatch(source) is None:
            raise ValueError("invalid raw spill source")
        with self._layout.quota_lock():
            return self._reclaimer.reclaim_idle_locked(source, crypto, time.time())

    def usage_bytes(self) -> int:
        """目录实际文件字节；配额判断必须走 accounted_usage。"""

        return self._quota.usage_bytes()

    def pending_count(self) -> int:
        """活动文件数：可完成或可恢复为库事实的对象，不含非活动隔离。"""

        return self._quota.pending_count()

    def pending_spill_count(self) -> int:
        return self._quota.pending_spill_count()

    def remove_claimed_cipherq(self, path: Path) -> None:
        """落库成功后删除认领密文与对应 manifest。"""

        self._cipherq.remove_claimed_cipherq(path)
