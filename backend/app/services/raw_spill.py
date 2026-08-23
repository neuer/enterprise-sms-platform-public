"""厂商拉走即消费响应的本地加密 spill，供落库前崩溃恢复。"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import logging
import os
import re
import secrets
import struct
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Protocol
from zoneinfo import ZoneInfo

from app.services.crypto import (
    BOUND_ENVELOPE_MAGIC,
    NONCE_SIZE,
    TAG_SIZE,
    EncryptedValue,
    EncryptionContext,
)

SOURCE_PATTERN = re.compile(r"^(report|reply)$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
STREAM_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
STREAM_MAGIC = b"SMSXRS1\n"
STREAM_RECORD_HEADER = struct.Struct(">I")
STREAM_META_HEADER = struct.Struct(">H")
STREAM_CONTROL_SENTINEL = 0xFFFFFFFF
STREAM_KIND_ANNOUNCE = "announce"
STREAM_KIND_TERMINAL = "terminal"
CAPTURE_COMPLETE = "complete"
CAPTURE_COMPLETE_TOO_LARGE = "complete_too_large"
CAPTURE_TRUNCATED = "truncated"
CAPTURE_PROTOCOL_INVALID = "protocol_invalid"
CAPTURE_UNKNOWN_LEGACY = "unknown_legacy"
VALID_CAPTURE_STATES = frozenset(
    {
        CAPTURE_COMPLETE,
        CAPTURE_COMPLETE_TOO_LARGE,
        CAPTURE_TRUNCATED,
        CAPTURE_PROTOCOL_INVALID,
        CAPTURE_UNKNOWN_LEGACY,
    }
)
NON_REPLAYABLE_CAPTURE_STATES = frozenset(
    {
        CAPTURE_TRUNCATED,
        CAPTURE_PROTOCOL_INVALID,
        CAPTURE_UNKNOWN_LEGACY,
    }
)
DEFAULT_MAX_TOTAL_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_PENDING_FILES = 32
DEFAULT_RECOVER_MAX_FILES = 8
DEFAULT_RECOVER_MAX_SECONDS = 8.0
SYNC_EVERY_BYTES = 1024 * 1024
RECOVERY_CAPTURE_BYTES = 64 * 1024 * 1024
# 平台内部帧合同：网络 chunk 不得映射为持久化 AES-GCM 帧。
# reservation 只由帧大小、最大帧数、控制帧和目录元数据证明，与 httpx 分片无关。
INTERNAL_FRAME_SIZE = 64 * 1024
DATA_FRAME_OVERHEAD_BYTES = (
    STREAM_RECORD_HEADER.size + len(BOUND_ENVELOPE_MAGIC) + NONCE_SIZE + TAG_SIZE
)
STREAM_HEADER_BUDGET_BYTES = 256
CONTROL_FRAME_COUNT = 2
CONTROL_FRAME_BUDGET_BYTES = 256
DIRECTORY_METADATA_BYTES = 768
# 3× 厂商绝对超时，避免回收仍在 DNS/connect/TLS 中的在途 header-only。
HEADER_ONLY_RECLAIM_AFTER_S = 30.0
# 非活动隔离与活动拉取配额分离：只保存无 PII 证据，不占 32 文件/64MiB 活动容量。
DEFAULT_MAX_QUARANTINE_FILES = 64
DEFAULT_MAX_QUARANTINE_BYTES = 256 * 1024
DEFAULT_QUARANTINE_RETENTION_S = 86400.0
MAX_CLASSIFY_DATA_CIPHER_BYTES = INTERNAL_FRAME_SIZE + DATA_FRAME_OVERHEAD_BYTES + 64
MAX_CLASSIFY_CONTROL_BYTES = CONTROL_FRAME_BUDGET_BYTES * 4
QUOTA_LOCK_NAME = ".quota.lock"
RESERVE_SUFFIX = ".reserve"
HEADER_QUARANTINE_SUFFIX = ".headerq"
HANDOFF_QUARANTINE_SUFFIX = ".quarantine"
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
RESERVATION_KEYS = frozenset({"created_at", "lease_id", "reserved_bytes", "source"})
STREAM_FILE_NAME = re.compile(
    r"^(?P<source>report|reply)-(?P<stream_id>[0-9a-f]{32})\.stream(?:\.tmp)?$"
)
ACTIVITY_FILE_NAME = re.compile(
    r"^(?:report|reply)-[0-9a-f]{32}\.stream(?:\.tmp)?$"
    r"|^(?:report|reply)-[0-9a-f]{64}\.spill(?:\.tmp)?$"
)
STREAM_UNIT_NAME = re.compile(
    r"^(?P<source>report|reply)-(?P<stream_id>[0-9a-f]{32})\."
    r"(?:stream(?:\.tmp)?(?:\.hdr)?|reserve|quarantine|headerq)(?:\.tmp)?$"
)
SPILL_FILE_NAME = re.compile(
    r"^(?P<source>report|reply)-(?P<digest>[0-9a-f]{64})\.spill(?:\.tmp)?$"
)
REWRITE_HDR_NAME = re.compile(
    r"^(?P<source>report|reply)-(?P<stream_id>[0-9a-f]{32})\.stream(?:\.tmp)?\.hdr$"
)
MARKER_TMP_NAME = re.compile(
    r"^(?P<source>report|reply)-(?P<token>[0-9a-f]{32,64})\."
    r"(?:headerq|quarantine|reserve)\.tmp$"
)
STREAM_LIFE_LEGAL_HEADER_ONLY = "legal_header_only"
STREAM_LIFE_PARTIAL_HEADER = "partial_header"
STREAM_LIFE_CORRUPT_HEADER = "corrupt_header"
STREAM_LIFE_INCOMPLETE_FRAMES = "incomplete_frames"
STREAM_LIFE_UNAUTHENTICATED_PARTIAL = "unauthenticated_partial"
STREAM_LIFE_HAS_CONTROL = "has_control"
STREAM_LIFE_HAS_AUTHENTICATED_DATA = "has_authenticated_data"
SPILL_LIFE_VALID = "valid_spill"
SPILL_LIFE_INCOMPLETE = "incomplete_spill"
SPILL_LIFE_CORRUPT = "corrupt_spill"
LOGGER = logging.getLogger(__name__)


def is_activity_filename(name: str) -> bool:
    """只有仍可能完成或恢复为库事实的对象占用活动文件配额。"""

    return ACTIVITY_FILE_NAME.fullmatch(name) is not None


def is_nonactive_quota_filename(name: str) -> bool:
    """非活动隔离/交接标记不占活动字节配额。"""

    return name.endswith(
        (
            HEADER_QUARANTINE_SUFFIX,
            HEADER_QUARANTINE_SUFFIX + ".tmp",
            HANDOFF_QUARANTINE_SUFFIX,
            HANDOFF_QUARANTINE_SUFFIX + ".tmp",
        )
    )


def max_internal_frames(capture_bytes: int) -> int:
    """明文捕获上限对应的内部帧数硬上限；最后一帧允许不足 INTERNAL_FRAME_SIZE。"""

    if capture_bytes < 1:
        raise ValueError("capture reservation must be positive")
    return (capture_bytes + INTERNAL_FRAME_SIZE - 1) // INTERNAL_FRAME_SIZE


def capture_reservation_bytes(capture_bytes: int) -> int:
    """按内部帧合同预留：帧容量 × 最大帧数 + 每帧信封 + 控制帧 + 目录元数据。

    不得用网络传输分片估算。容量无法由此式证明时，open_stream 必须在厂商
    HTTP 之前失败，禁止已经开始消费后再因帧开销截断。
    """

    frames = max_internal_frames(capture_bytes)
    return (
        frames * INTERNAL_FRAME_SIZE
        + frames * DATA_FRAME_OVERHEAD_BYTES
        + STREAM_HEADER_BUDGET_BYTES
        + CONTROL_FRAME_COUNT * CONTROL_FRAME_BUDGET_BYTES
        + DIRECTORY_METADATA_BYTES
    )


# 64MiB 恢复捕获的文档化开销；由上面帧合同算出，不是独立配额。
CAPTURE_FRAME_OVERHEAD_BYTES = (
    capture_reservation_bytes(RECOVERY_CAPTURE_BYTES) - RECOVERY_CAPTURE_BYTES
)


class SpillQuotaExceeded(RuntimeError):
    """spill 目录已达文件数或总字节上限，必须停止继续拉取。"""


@dataclass(frozen=True, slots=True)
class SpillReclaimResult:
    """一次 header-only/孤儿/非活动隔离的分类计数；不含路径或密文。"""

    header_only: int = 0
    partial_header: int = 0
    corrupt_header: int = 0
    incomplete_frames: int = 0
    unauthenticated_partial: int = 0
    orphans: int = 0
    isolated: int = 0
    temps_reclaimed: int = 0
    quarantine_expired: int = 0
    quarantine_capacity_dropped: int = 0

    @property
    def header_cleaned(self) -> int:
        return self.header_only + self.partial_header + self.corrupt_header + self.incomplete_frames

    @property
    def total(self) -> int:
        return self.header_cleaned + self.orphans + self.isolated + self.temps_reclaimed

    def __bool__(self) -> bool:
        return self.header_cleaned > 0

    def __ge__(self, other: object) -> bool:
        if isinstance(other, int):
            return self.total >= other
        return NotImplemented


@dataclass(frozen=True, slots=True)
class HeaderOnlyStats:
    """目录内 header-only 观察值；供测试与巡检，不进 Prometheus 事实表。"""

    header_only_count: int
    oldest_age_seconds: float | None
    cleaned_total: int
    partial_header_count: int = 0
    corrupt_header_count: int = 0
    unauthenticated_partial_count: int = 0


@dataclass(frozen=True, slots=True)
class ArtifactStats:
    """活动对象与非活动隔离的目录观察值；不含路径、密文或 PII。"""

    active_count: int
    quarantine_count: int
    quarantine_bytes: int
    oldest_quarantine_age_seconds: float | None
    isolated_total: int
    expired_total: int
    capacity_dropped_total: int


@dataclass(frozen=True, slots=True)
class _StreamInspection:
    kind: str
    source: str
    stream_id: str
    age_seconds: float
    path: Path


def flush_announced_pending_frames(stream: Any | None) -> None:
    """announce 之后、finish 之前的异常边界必须把不足一帧的缓冲落盘。"""

    if stream is None or getattr(stream, "has_announced", False) is not True:
        return
    if getattr(stream, "_finished", False):
        return
    flush = getattr(stream, "flush", None)
    if not callable(flush):
        return
    try:
        flush()
    except Exception as exc:
        LOGGER.warning(
            "raw spill pending frame flush failed",
            extra={"error_type": type(exc).__name__},
        )


def discard_header_only_stream(stream: Any | None) -> None:
    """announce 前失败只删除纯 header-only；已认证 data/announce 留给恢复。"""

    if stream is None:
        return
    if getattr(stream, "has_announced", False):
        return
    header_only = getattr(stream, "is_header_only", None)
    if header_only is None:
        if getattr(stream, "has_captured_bytes", True):
            return
    elif header_only is not True:
        return
    discard = getattr(stream, "discard", None)
    if not callable(discard):
        return
    try:
        discard()
    except Exception as exc:
        LOGGER.warning(
            "header-only raw spill stream discard failed",
            extra={"error_type": type(exc).__name__},
        )


@contextmanager
def manage_raw_spill_stream(stream: Any | None) -> Iterator[Any]:
    """退出时先 flush 已 announce 的短帧，再回收 announce 前的 header-only。"""

    try:
        yield stream
    finally:
        flush_announced_pending_frames(stream)
        discard_header_only_stream(stream)


@dataclass(frozen=True, slots=True)
class SpillReservation:
    """容量账本条目；只含来源、租约与字节，不得写入手机号、正文或密钥。"""

    source: str
    lease_id: str
    reserved_bytes: int
    path: Path


@dataclass(frozen=True, slots=True)
class RecoverRoundBudget:
    """单轮恢复上限；一次 poll 不得把整个 backlog 读进 RSS。"""

    max_files: int = DEFAULT_RECOVER_MAX_FILES
    max_plaintext_bytes: int = RECOVERY_CAPTURE_BYTES
    max_seconds: float = DEFAULT_RECOVER_MAX_SECONDS

    def exhausted(self, *, recovered: int, used_bytes: int, started_at: float) -> bool:
        if recovered >= self.max_files:
            return True
        if recovered > 0 and used_bytes >= self.max_plaintext_bytes:
            return True
        return recovered > 0 and (time.monotonic() - started_at) >= self.max_seconds


@dataclass
class RecoverMemoryProbe:
    """测试用：记录同时物化的字节峰值与已读 payload 文件名，不含 PII。"""

    live_bytes: int = 0
    peak_bytes: int = 0
    payload_reads: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def note_payload_read(self, name: str) -> None:
        with self._lock:
            self.payload_reads.append(name)

    def acquire(self, nbytes: int) -> None:
        if nbytes <= 0:
            return
        with self._lock:
            self.live_bytes += nbytes
            if self.live_bytes > self.peak_bytes:
                self.peak_bytes = self.live_bytes

    def release(self, nbytes: int) -> None:
        if nbytes <= 0:
            return
        with self._lock:
            self.live_bytes = max(0, self.live_bytes - nbytes)


class StreamChunkCrypto(Protocol):
    def encrypt_bound_bytes(
        self, plaintext: bytes, context: EncryptionContext
    ) -> EncryptedValue: ...

    def decrypt_bound_bytes(
        self,
        payload: bytes,
        key_version: int,
        context: EncryptionContext,
        *,
        allow_legacy: bool = False,
    ) -> bytes: ...


class RawSpillSettings(Protocol):
    raw_spill_dir: Path
    raw_spill_max_total_bytes: int
    raw_spill_max_pending_files: int
    raw_spill_recover_max_files: int
    raw_spill_recover_max_plaintext_bytes: int
    raw_spill_recover_max_seconds: float


def normalize_capture_state(value: str | None) -> str:
    state = value if value else CAPTURE_COMPLETE
    if state not in VALID_CAPTURE_STATES:
        raise ValueError("invalid capture_state")
    return state


def is_non_replayable_capture(state: str | None) -> bool:
    """截断、协议异常或未分类历史 raw 不得进入普通自动/人工重放。"""

    return normalize_capture_state(state) in NON_REPLAYABLE_CAPTURE_STATES


def _normalize_http_status(value: object) -> int:
    status = int(value) if isinstance(value, (int, str)) else 200
    return status if 100 <= status <= 599 else 200


def _control_object_id(
    *,
    source: str,
    stream_id: str,
    seq: int,
    kind: str,
    http_status: int,
    content_encoding: str,
    capture_state: str,
) -> str:
    return f"{source}:{stream_id}:{seq:08d}:{kind}:{http_status}:{content_encoding}:{capture_state}"


def _control_context(
    *,
    source: str,
    stream_id: str,
    seq: int,
    kind: str,
    http_status: int,
    content_encoding: str,
    capture_state: str,
) -> EncryptionContext:
    return EncryptionContext(
        domain="vendor-raw",
        table="raw_spill",
        column=kind,
        object_id=_control_object_id(
            source=source,
            stream_id=stream_id,
            seq=seq,
            kind=kind,
            http_status=http_status,
            content_encoding=content_encoding,
            capture_state=capture_state,
        ),
    )


@dataclass(frozen=True, slots=True)
class RawSpillRecord:
    source: str
    payload_sha256: str
    key_version: int
    http_status: int
    content_encoding: str
    payload_enc: bytes
    path: Path
    capture_state: str = CAPTURE_COMPLETE
    quarantined: bool = False
    stream_id: str = ""
    plaintext_bytes: int = 0

    @property
    def recover_weight_bytes(self) -> int:
        """单轮预算按明文字节计；spill 密文用信封长度近似。"""

        return self.plaintext_bytes or len(self.payload_enc)


def iter_records_for_recover(
    spill: Any,
    crypto: StreamChunkCrypto,
    source: str,
) -> Iterator[RawSpillRecord]:
    """优先走惰性恢复接口；旧 mock 仍可 list 后按 source 过滤。"""

    iterate = getattr(spill, "iter_recoverable", None)
    if callable(iterate):
        yield from iterate(crypto, source)
        return
    list_pending = getattr(spill, "list_pending", None)
    if callable(list_pending):
        for record in list_pending():
            if getattr(record, "source", None) == source:
                yield record
    list_streams = getattr(spill, "list_pending_streams", None)
    if callable(list_streams):
        for record in list_streams(crypto):
            if getattr(record, "source", None) == source:
                yield record


@dataclass(frozen=True, slots=True)
class _StreamFileHeader:
    source: str
    stream_id: str
    key_version: int
    path: Path


@dataclass(frozen=True, slots=True)
class _AssembledStream:
    plaintext: bytes
    digest: str
    announce: dict[str, object] | None
    terminal: dict[str, object] | None
    incomplete: bool

    @property
    def empty(self) -> bool:
        return not self.plaintext and self.announce is None and self.terminal is None


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
        memory_probe: RecoverMemoryProbe | None = None,
        max_quarantine_files: int = DEFAULT_MAX_QUARANTINE_FILES,
        max_quarantine_bytes: int = DEFAULT_MAX_QUARANTINE_BYTES,
        quarantine_retention_s: float = DEFAULT_QUARANTINE_RETENTION_S,
    ) -> None:
        if max_total_bytes < 1 or max_pending_files < 1:
            raise ValueError("raw spill quotas must be positive")
        if header_only_min_age_s < 0:
            raise ValueError("header_only_min_age_s must not be negative")
        if max_quarantine_files < 1 or max_quarantine_bytes < 1:
            raise ValueError("raw spill quarantine quotas must be positive")
        if quarantine_retention_s < 0:
            raise ValueError("quarantine_retention_s must not be negative")
        self.directory = directory
        self.max_total_bytes = max_total_bytes
        self.max_pending_files = max_pending_files
        self.header_only_min_age_s = header_only_min_age_s
        self.max_quarantine_files = max_quarantine_files
        self.max_quarantine_bytes = max_quarantine_bytes
        self.quarantine_retention_s = quarantine_retention_s
        self.recover_budget = recover_budget or RecoverRoundBudget()
        self._probe = memory_probe
        self._header_only_cleaned = 0
        self._isolated_total = 0
        self._quarantine_expired = 0
        self._quarantine_capacity_dropped = 0
        self.last_reclaim = SpillReclaimResult()

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
        )

    def _path(self, source: str, payload_sha256: str) -> Path:
        if SOURCE_PATTERN.fullmatch(source) is None:
            raise ValueError("invalid raw spill source")
        if SHA256_PATTERN.fullmatch(payload_sha256) is None:
            raise ValueError("invalid raw spill digest")
        return self.directory / f"{source}-{payload_sha256}.spill"

    def _stream_tmp(self, source: str, stream_id: str) -> Path:
        if SOURCE_PATTERN.fullmatch(source) is None:
            raise ValueError("invalid raw spill source")
        if STREAM_ID_PATTERN.fullmatch(stream_id) is None:
            raise ValueError("invalid raw spill stream id")
        return self.directory / f"{source}-{stream_id}.stream.tmp"

    def _stream_path(self, source: str, stream_id: str) -> Path:
        return self._stream_tmp(source, stream_id).with_suffix(".stream")

    def _quarantine_path(self, source: str, stream_id: str) -> Path:
        return self.directory / f"{source}-{stream_id}.quarantine"

    def _header_quarantine_path(self, source: str, stream_id: str) -> Path:
        return self.directory / f"{source}-{stream_id}{HEADER_QUARANTINE_SUFFIX}"

    def usage_bytes(self) -> int:
        """目录实际文件字节；配额判断必须走 accounted_usage。"""

        if not self.directory.exists():
            return 0
        return sum(
            path.stat().st_size
            for path in self.directory.iterdir()
            if path.is_file() and path.name != QUOTA_LOCK_NAME
        )

    def accounted_usage(self) -> int:
        """未覆盖文件字节 + 各租约 reserved_bytes；禁止把缺失预留当零。"""

        with self._quota_lock():
            return self._accounted_usage_locked()

    def pending_count(self) -> int:
        """活动文件数：可完成或可恢复为库事实的对象，不含非活动隔离。"""

        if not self.directory.exists():
            return 0
        return sum(
            1
            for path in self.directory.iterdir()
            if path.is_file() and is_activity_filename(path.name)
        )

    def pending_spill_count(self) -> int:
        if not self.directory.exists():
            return 0
        return sum(
            1 for path in self.directory.iterdir() if path.is_file() and path.suffix == ".spill"
        )

    def can_accept(self, additional_bytes: int = 0, *, additional_files: int = 1) -> bool:
        if additional_bytes < 0:
            raise ValueError("additional_bytes must not be negative")
        if additional_files < 0:
            raise ValueError("additional_files must not be negative")
        with self._quota_lock():
            return self._can_accept_locked(additional_bytes, additional_files=additional_files)

    def list_reservations(self) -> list[SpillReservation]:
        """测试与巡检用；账本不含 PII。"""

        with self._quota_lock():
            return list(self._iter_reservations_locked())

    def write(
        self,
        *,
        source: str,
        payload_sha256: str,
        key_version: int,
        http_status: int,
        content_encoding: str,
        payload_enc: bytes,
        capture_state: str = CAPTURE_COMPLETE,
    ) -> Path:
        """先写临时文件并 fsync，再原子改名，保证 kill -9 后仍可恢复完整密文。"""

        if not payload_enc:
            raise ValueError("raw spill payload is empty")
        capture_state = normalize_capture_state(capture_state)
        extra = len(payload_enc) + 256
        with self._quota_lock():
            self._reclaim_nonstream_locked(time.time())
            # 在途 .stream 与即将落盘的 .spill 是同一捕获的两份密文，不得互相占满文件配额。
            same_capture = self._reservation_for_source_locked(source) is not None
            if self.pending_spill_count() >= self.max_pending_files or (
                not same_capture and self._accounted_usage_locked() + extra > self.max_total_bytes
            ):
                raise SpillQuotaExceeded("raw spill quota exceeded")
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self._path(source, payload_sha256)
        header = json.dumps(
            {
                "capture_state": capture_state,
                "content_encoding": content_encoding,
                "http_status": http_status,
                "key_version": key_version,
                "payload_sha256": payload_sha256,
                "source": source,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        tmp = target.with_suffix(".spill.tmp")
        with tmp.open("wb") as handle:
            handle.write(header)
            handle.write(b"\n")
            handle.write(payload_enc)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        self._fsync_directory()
        return target

    def list_pending(self, source: str | None = None) -> list[RawSpillRecord]:
        return list(self.iter_pending(source))

    def list_pending_streams(
        self, crypto: StreamChunkCrypto, source: str | None = None
    ) -> list[RawSpillRecord]:
        """把接收中崩溃留下的加密流装配为可落库的截断/完整 spill 记录。"""

        return list(self.iter_pending_streams(crypto, source))

    def iter_pending(self, source: str | None = None) -> Iterator[RawSpillRecord]:
        """按文件惰性产出 .spill；可先按文件名 source 过滤，不读其它来源 payload。"""

        yield from self._iter_records(source, streams=False, crypto=None)

    def iter_pending_streams(
        self, crypto: StreamChunkCrypto, source: str | None = None
    ) -> Iterator[RawSpillRecord]:
        """按文件惰性装配 .stream；可先按文件名 source 过滤。"""

        yield from self._iter_records(source, streams=True, crypto=crypto)

    def iter_recoverable(
        self, crypto: StreamChunkCrypto, source: str
    ) -> Iterator[RawSpillRecord]:
        """恢复入口：只产出指定 source，一次一条，损坏文件跳过。"""

        if SOURCE_PATTERN.fullmatch(source) is None:
            raise ValueError("invalid raw spill source")
        yield from self.iter_pending(source)
        yield from self.iter_pending_streams(crypto, source)

    def _iter_records(
        self,
        source: str | None,
        *,
        streams: bool,
        crypto: StreamChunkCrypto | None,
    ) -> Iterator[RawSpillRecord]:
        if source is not None and SOURCE_PATTERN.fullmatch(source) is None:
            raise ValueError("invalid raw spill source")
        if not self.directory.exists():
            return
        for path in self._pending_paths(source, streams=streams):
            try:
                if streams:
                    if crypto is None:
                        continue
                    record = self._read_stream(path, crypto, expected_source=source)
                else:
                    record = self._read(path, expected_source=source)
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
            self._probe_acquire(size)
            try:
                yield record
            finally:
                self._probe_release(size)

    def _pending_paths(self, source: str | None, *, streams: bool) -> list[Path]:
        prefix = f"{source}-*" if source else "*"
        if streams:
            return [
                *sorted(self.directory.glob(f"{prefix}.stream")),
                *sorted(self.directory.glob(f"{prefix}.stream.tmp")),
            ]
        return sorted(self.directory.glob(f"{prefix}.spill"))

    def _probe_acquire(self, nbytes: int) -> None:
        if self._probe is not None:
            self._probe.acquire(nbytes)

    def _probe_release(self, nbytes: int) -> None:
        if self._probe is not None:
            self._probe.release(nbytes)

    def _probe_payload_read(self, name: str) -> None:
        if self._probe is not None:
            self._probe.note_payload_read(name)

    def remove(self, source: str, payload_sha256: str) -> None:
        path = self._path(source, payload_sha256)
        path.unlink(missing_ok=True)

    def remove_stream(self, source: str, stream_id: str) -> None:
        with self._quota_lock():
            self._remove_stream_locked(source, stream_id)

    def open_stream(
        self,
        source: str,
        crypto: StreamChunkCrypto,
        *,
        capture_bytes: int = RECOVERY_CAPTURE_BYTES,
    ) -> RawSpillStream:
        """原子预留一次恢复捕获容量后再建 stream；失败不得留下租约或调用厂商。"""

        reserved_bytes = capture_reservation_bytes(capture_bytes)
        with self._quota_lock():
            self._reclaim_idle_locked(source, crypto, time.time())
            if not self._can_accept_locked(reserved_bytes, additional_files=1):
                raise SpillQuotaExceeded("raw spill quota exceeded")
            lease_id = secrets.token_hex(16)
            try:
                reservation = self._write_reservation_locked(source, lease_id, reserved_bytes)
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
                self._remove_stream_locked(source, lease_id)
                if exc.errno == errno.ENOSPC:
                    raise SpillQuotaExceeded("raw spill disk full") from exc
                raise
            except Exception:
                self._remove_stream_locked(source, lease_id)
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
        if self.directory.exists():
            for path in self._iter_stream_paths():
                inspection = self._inspect_stream_path(path, crypto=None, now_ts=now_ts)
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
            cleaned_total=self._header_only_cleaned,
            partial_header_count=partial_header,
            corrupt_header_count=corrupt_header,
            unauthenticated_partial_count=unauthenticated,
        )

    def artifact_stats(self) -> ArtifactStats:
        """活动配额与非活动隔离容量；供测试与巡检，不进 Prometheus 事实表。"""

        quarantine_count = 0
        quarantine_bytes = 0
        oldest: float | None = None
        now_ts = time.time()
        if self.directory.exists():
            for path in self._iter_evidence_paths():
                quarantine_count += 1
                try:
                    quarantine_bytes += path.stat().st_size
                    age = self._file_age_seconds(path, now_ts)
                except OSError:
                    continue
                oldest = age if oldest is None else max(oldest, age)
        return ArtifactStats(
            active_count=self.pending_count(),
            quarantine_count=quarantine_count,
            quarantine_bytes=quarantine_bytes,
            oldest_quarantine_age_seconds=oldest,
            isolated_total=self._isolated_total,
            expired_total=self._quarantine_expired,
            capacity_dropped_total=self._quarantine_capacity_dropped,
        )

    def reclaim_idle(self, source: str, crypto: StreamChunkCrypto) -> SpillReclaimResult:
        """统一分类并回收：超龄空流、不可认证帧、损坏 spill、临时文件与孤儿标记。

        已连续认证的 data 不得当空文件删除；不可读对象进入非活动隔离，不占活动配额。
        清理覆盖共享目录全部来源，避免 Report 残留永久阻断 Reply。
        """

        if SOURCE_PATTERN.fullmatch(source) is None:
            raise ValueError("invalid raw spill source")
        with self._quota_lock():
            return self._reclaim_idle_locked(source, crypto, time.time())

    @contextmanager
    def _quota_lock(self) -> Iterator[None]:
        """Report/Reply 共用目录锁，禁止并发超卖。"""

        self.directory.mkdir(parents=True, exist_ok=True)
        handle = (self.directory / QUOTA_LOCK_NAME).open("a+b")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def _reservation_path(self, source: str, lease_id: str) -> Path:
        if SOURCE_PATTERN.fullmatch(source) is None:
            raise ValueError("invalid raw spill source")
        if STREAM_ID_PATTERN.fullmatch(lease_id) is None:
            raise ValueError("invalid raw spill stream id")
        return self.directory / f"{source}-{lease_id}{RESERVE_SUFFIX}"

    def _write_reservation_locked(
        self,
        source: str,
        lease_id: str,
        reserved_bytes: int,
    ) -> SpillReservation:
        path = self._reservation_path(source, lease_id)
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
        self._fsync_directory()
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
            if self._reservation_path(source, lease_id) != path:
                return None
            return SpillReservation(
                source=source,
                lease_id=lease_id,
                reserved_bytes=reserved_bytes,
                path=path,
            )
        except (OSError, KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            return None

    def _iter_reservations_locked(self) -> list[SpillReservation]:
        if not self.directory.exists():
            return []
        records: list[SpillReservation] = []
        for path in sorted(self.directory.glob(f"*{RESERVE_SUFFIX}")):
            if path.name.endswith(".reserve.tmp"):
                continue
            record = self._parse_reservation(path)
            if record is not None:
                records.append(record)
        return records

    def _reservation_for_source_locked(self, source: str) -> SpillReservation | None:
        for record in self._iter_reservations_locked():
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
                    f"{record.source}-{record.lease_id}.stream.tmp",
                    f"{record.source}-{record.lease_id}.stream.tmp.hdr",
                    f"{record.source}-{record.lease_id}.stream.hdr",
                    f"{record.source}-{record.lease_id}.quarantine",
                    f"{record.source}-{record.lease_id}{HEADER_QUARANTINE_SUFFIX}",
                }
            )
        return names

    def _accounted_usage_locked(self) -> int:
        if not self.directory.exists():
            return 0
        reservations = self._iter_reservations_locked()
        covered = self._covered_names_locked(reservations)
        uncovered = 0
        for path in self.directory.iterdir():
            if not path.is_file() or path.name == QUOTA_LOCK_NAME:
                continue
            if path.name in covered or path.name.endswith(RESERVE_SUFFIX):
                continue
            if is_nonactive_quota_filename(path.name):
                continue
            uncovered += path.stat().st_size
        return uncovered + sum(record.reserved_bytes for record in reservations)

    def _can_accept_locked(
        self,
        additional_bytes: int = 0,
        *,
        additional_files: int = 1,
    ) -> bool:
        return (
            self.pending_count() + additional_files <= self.max_pending_files
            and self._accounted_usage_locked() + additional_bytes <= self.max_total_bytes
        )

    def _remove_stream_locked(self, source: str, stream_id: str) -> None:
        tmp = self._stream_tmp(source, stream_id)
        tmp.unlink(missing_ok=True)
        tmp.with_name(tmp.name + ".hdr").unlink(missing_ok=True)
        final = self._stream_path(source, stream_id)
        final.unlink(missing_ok=True)
        final.with_name(final.name + ".hdr").unlink(missing_ok=True)
        quarantine = self._quarantine_path(source, stream_id)
        quarantine.unlink(missing_ok=True)
        quarantine.with_name(quarantine.name + ".tmp").unlink(missing_ok=True)
        headerq = self._header_quarantine_path(source, stream_id)
        headerq.unlink(missing_ok=True)
        headerq.with_name(headerq.name + ".tmp").unlink(missing_ok=True)
        reserve = self._reservation_path(source, stream_id)
        reserve.unlink(missing_ok=True)
        reserve.with_name(reserve.name + ".tmp").unlink(missing_ok=True)

    def _reclaim_orphans_locked(self, source: str | None = None) -> int:
        if not self.directory.exists():
            return 0
        reclaimed = 0
        for path in list(self.directory.glob(f"*{RESERVE_SUFFIX}.tmp")):
            path.unlink(missing_ok=True)
            reclaimed += 1
        for path in list(self.directory.glob(f"*{RESERVE_SUFFIX}")):
            record = self._parse_reservation(path)
            if record is None:
                path.unlink(missing_ok=True)
                reclaimed += 1
                continue
            if source is not None and record.source != source:
                continue
            if self._stream_tmp(record.source, record.lease_id).exists():
                continue
            if self._stream_path(record.source, record.lease_id).exists():
                continue
            path.unlink(missing_ok=True)
            reclaimed += 1
        return reclaimed

    def _iter_stream_paths(self) -> list[Path]:
        if not self.directory.exists():
            return []
        return [
            *sorted(self.directory.glob("*.stream")),
            *sorted(self.directory.glob("*.stream.tmp")),
        ]

    def _file_age_seconds(self, path: Path, now_ts: float) -> float:
        try:
            return max(0.0, now_ts - path.stat().st_mtime)
        except OSError:
            return 0.0

    def _inspect_stream_path(
        self,
        path: Path,
        crypto: StreamChunkCrypto | None,
        *,
        now_ts: float,
    ) -> _StreamInspection | None:
        """按最小 header/首帧分类；不解密、不装载全文，以免回收路径 OOM。"""

        match = STREAM_FILE_NAME.fullmatch(path.name)
        named_source = match.group("source") if match else ""
        named_id = match.group("stream_id") if match else ""
        age = self._file_age_seconds(path, now_ts)
        try:
            handle = path.open("rb")
        except OSError:
            return None
        try:
            return self._classify_stream_header(
                handle,
                named_source=named_source,
                named_id=named_id,
                age=age,
                path=path,
                crypto=crypto,
            )
        finally:
            handle.close()

    def _classify_stream_header(
        self,
        handle: BinaryIO,
        *,
        named_source: str,
        named_id: str,
        age: float,
        path: Path,
        crypto: StreamChunkCrypto | None,
    ) -> _StreamInspection | None:
        magic = handle.read(len(STREAM_MAGIC))
        if not magic or (len(magic) < len(STREAM_MAGIC) and STREAM_MAGIC.startswith(magic)):
            return _StreamInspection(STREAM_LIFE_PARTIAL_HEADER, named_source, named_id, age, path)
        if magic != STREAM_MAGIC:
            return _StreamInspection(STREAM_LIFE_CORRUPT_HEADER, named_source, named_id, age, path)
        header_bytes = handle.readline()
        if not header_bytes.endswith(b"\n"):
            return _StreamInspection(STREAM_LIFE_PARTIAL_HEADER, named_source, named_id, age, path)
        try:
            parsed = json.loads(header_bytes.decode("utf-8"))
            source = str(parsed["source"])
            stream_id = str(parsed["stream_id"])
            key_version = int(parsed["key_version"])
            if (
                self._stream_tmp(source, stream_id) != path
                and self._stream_path(source, stream_id) != path
            ):
                return _StreamInspection(
                    STREAM_LIFE_CORRUPT_HEADER, named_source, named_id, age, path
                )
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            return _StreamInspection(STREAM_LIFE_CORRUPT_HEADER, named_source, named_id, age, path)
        first = handle.read(STREAM_RECORD_HEADER.size)
        if not first:
            return _StreamInspection(
                STREAM_LIFE_LEGAL_HEADER_ONLY, source, stream_id, age, path
            )
        if crypto is None:
            return None
        if len(first) < STREAM_RECORD_HEADER.size:
            return _StreamInspection(
                STREAM_LIFE_UNAUTHENTICATED_PARTIAL, source, stream_id, age, path
            )
        (length,) = STREAM_RECORD_HEADER.unpack(first)
        if length in {STREAM_CONTROL_SENTINEL, 0}:
            return self._classify_first_control(
                handle,
                source=source,
                stream_id=stream_id,
                key_version=key_version,
                age=age,
                path=path,
                crypto=crypto,
            )
        return self._classify_first_data(
            handle,
            source=source,
            stream_id=stream_id,
            key_version=key_version,
            age=age,
            path=path,
            crypto=crypto,
            length=length,
        )

    def _classify_first_control(
        self,
        handle: BinaryIO,
        *,
        source: str,
        stream_id: str,
        key_version: int,
        age: float,
        path: Path,
        crypto: StreamChunkCrypto,
    ) -> _StreamInspection:
        """首个 announce/terminal 控制帧：未读完或认证失败都不是真实控制事实。"""

        raw_len = handle.read(STREAM_RECORD_HEADER.size)
        if len(raw_len) < STREAM_RECORD_HEADER.size:
            return _StreamInspection(
                STREAM_LIFE_UNAUTHENTICATED_PARTIAL, source, stream_id, age, path
            )
        (frame_len,) = STREAM_RECORD_HEADER.unpack(raw_len)
        if frame_len < STREAM_META_HEADER.size or frame_len > MAX_CLASSIFY_CONTROL_BYTES:
            return _StreamInspection(
                STREAM_LIFE_UNAUTHENTICATED_PARTIAL, source, stream_id, age, path
            )
        frame = handle.read(frame_len)
        if len(frame) != frame_len:
            return _StreamInspection(
                STREAM_LIFE_UNAUTHENTICATED_PARTIAL, source, stream_id, age, path
            )
        try:
            _fields, _offset, ok = self._read_control_frame(
                STREAM_RECORD_HEADER.pack(frame_len) + frame,
                0,
                crypto,
                source=source,
                stream_id=stream_id,
                key_version=key_version,
                seq=0,
            )
        except Exception:
            ok = False
        if ok:
            return _StreamInspection(STREAM_LIFE_HAS_CONTROL, source, stream_id, age, path)
        return _StreamInspection(
            STREAM_LIFE_UNAUTHENTICATED_PARTIAL, source, stream_id, age, path
        )

    def _classify_first_data(
        self,
        handle: BinaryIO,
        *,
        source: str,
        stream_id: str,
        key_version: int,
        age: float,
        path: Path,
        crypto: StreamChunkCrypto,
        length: int,
    ) -> _StreamInspection:
        """首个 data frame：只有整帧 AES-GCM 认证成功才算已认证 data。"""

        if length < 1 or length > MAX_CLASSIFY_DATA_CIPHER_BYTES:
            return _StreamInspection(
                STREAM_LIFE_UNAUTHENTICATED_PARTIAL, source, stream_id, age, path
            )
        ciphertext = handle.read(length)
        if len(ciphertext) != length:
            return _StreamInspection(
                STREAM_LIFE_UNAUTHENTICATED_PARTIAL, source, stream_id, age, path
            )
        try:
            crypto.decrypt_bound_bytes(
                ciphertext,
                key_version,
                EncryptionContext(
                    domain="vendor-raw",
                    table="raw_spill",
                    column="chunk",
                    object_id=f"{source}:{stream_id}:{0:08d}",
                ),
            )
        except Exception:
            return _StreamInspection(
                STREAM_LIFE_UNAUTHENTICATED_PARTIAL, source, stream_id, age, path
            )
        return _StreamInspection(
            STREAM_LIFE_HAS_AUTHENTICATED_DATA, source, stream_id, age, path
        )

    def _reclaim_idle_locked(
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
        temps = self._reclaim_rewrite_tmps_locked(now_ts)
        temps += self._reclaim_marker_tmps_locked()
        isolated += self._reclaim_spills_locked(now_ts)
        for path in self._iter_stream_paths():
            inspection = self._inspect_stream_path(path, crypto=crypto, now_ts=now_ts)
            if inspection is None:
                continue
            if inspection.kind in {
                STREAM_LIFE_HAS_AUTHENTICATED_DATA,
                STREAM_LIFE_HAS_CONTROL,
            }:
                continue
            if inspection.age_seconds < self.header_only_min_age_s:
                continue
            if inspection.kind in {
                STREAM_LIFE_UNAUTHENTICATED_PARTIAL,
                STREAM_LIFE_INCOMPLETE_FRAMES,
                STREAM_LIFE_CORRUPT_HEADER,
            }:
                self._reclaim_classified_locked(inspection, quarantine=True)
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
                self._reclaim_classified_locked(
                    inspection, quarantine=inspection.kind == STREAM_LIFE_CORRUPT_HEADER
                )
                if inspection.kind == STREAM_LIFE_LEGAL_HEADER_ONLY:
                    header_only += 1
                else:
                    partial_header += 1
        orphans = self._reclaim_orphans_locked(None)
        orphans += self._reclaim_orphan_handoff_locked()
        expired = self._expire_quarantine_locked(now_ts)
        dropped = self._enforce_quarantine_quota_locked()
        cleaned = header_only + partial_header + corrupt_header
        self._header_only_cleaned += cleaned
        self._isolated_total += isolated
        self._quarantine_expired += expired
        self._quarantine_capacity_dropped += dropped
        result = SpillReclaimResult(
            header_only=header_only,
            partial_header=partial_header,
            corrupt_header=corrupt_header,
            unauthenticated_partial=unauthenticated_partial,
            incomplete_frames=unauthenticated_partial,
            orphans=orphans,
            isolated=isolated,
            temps_reclaimed=temps,
            quarantine_expired=expired,
            quarantine_capacity_dropped=dropped,
        )
        self.last_reclaim = result
        if cleaned or isolated or temps or orphans:
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
                },
            )
        return result

    def _reclaim_nonstream_locked(self, now_ts: float) -> None:
        """不依赖 crypto 的 spill/tmp/孤儿分类；write() 开门前必须调用。"""

        self._reclaim_marker_tmps_locked()
        self._reclaim_spills_locked(now_ts)
        self._reclaim_orphans_locked(None)
        self._reclaim_orphan_handoff_locked()
        self._expire_quarantine_locked(now_ts)
        self._enforce_quarantine_quota_locked()

    def _iter_evidence_paths(self) -> list[Path]:
        if not self.directory.exists():
            return []
        return [
            path
            for path in sorted(self.directory.glob(f"*{HEADER_QUARANTINE_SUFFIX}"))
            if path.is_file() and not path.name.endswith(".tmp")
        ]

    def _reclaim_rewrite_tmps_locked(self, now_ts: float) -> int:
        """完成或隔离 key_version 重写留下的 *.hdr。"""

        if not self.directory.exists():
            return 0
        reclaimed = 0
        for path in list(self.directory.iterdir()):
            match = REWRITE_HDR_NAME.fullmatch(path.name)
            if match is None or not path.is_file():
                continue
            target = path.with_name(path.name[: -len(".hdr")])
            if self._hdr_is_promotable(path, target):
                os.replace(path, target)
                self._fsync_directory()
                reclaimed += 1
                continue
            if self._file_age_seconds(path, now_ts) < self.header_only_min_age_s:
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
                parsed = self._parse_stream_header_handle(handle, target)
            return parsed is not None
        except OSError:
            return False

    def _reclaim_marker_tmps_locked(self) -> int:
        """分类 *.reserve.tmp / *.quarantine.tmp / *.headerq.tmp。"""

        if not self.directory.exists():
            return 0
        reclaimed = 0
        for path in list(self.directory.iterdir()):
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
            self._fsync_directory()
            return True
        except (OSError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            return False

    def _reclaim_spills_locked(self, now_ts: float) -> int:
        """损坏/不完整 .spill 与 .spill.tmp 立即或超龄后离开活动配额。"""

        if not self.directory.exists():
            return 0
        isolated = 0
        for path in [
            *sorted(self.directory.glob("*.spill")),
            *sorted(self.directory.glob("*.spill.tmp")),
        ]:
            if not path.is_file():
                continue
            kind, source, token = self._classify_spill_path(path)
            if kind == SPILL_LIFE_VALID and path.name.endswith(".spill.tmp"):
                target = self._path(source, token)
                os.replace(path, target)
                self._fsync_directory()
                continue
            if kind == SPILL_LIFE_VALID:
                continue
            aged = self._file_age_seconds(path, now_ts) >= self.header_only_min_age_s
            if kind == SPILL_LIFE_INCOMPLETE and path.name.endswith(".spill.tmp") and not aged:
                continue
            self._isolate_named_file_locked(
                path,
                source=source or "report",
                token=token,
                state=SPILL_LIFE_CORRUPT if kind == SPILL_LIFE_CORRUPT else SPILL_LIFE_INCOMPLETE,
                kind="spill",
            )
            isolated += 1
        return isolated

    def _classify_spill_path(self, path: Path) -> tuple[str, str, str]:
        match = SPILL_FILE_NAME.fullmatch(path.name)
        named_source = match.group("source") if match else ""
        named_digest = match.group("digest") if match else ""
        try:
            with path.open("rb") as handle:
                header_bytes = handle.readline()
                if not header_bytes.endswith(b"\n"):
                    return SPILL_LIFE_INCOMPLETE, named_source, named_digest
                header = json.loads(header_bytes.decode("utf-8"))
                source = str(header["source"])
                digest = str(header["payload_sha256"])
                expected = self._path(source, digest)
                if path not in {expected, expected.with_suffix(".spill.tmp")}:
                    return SPILL_LIFE_CORRUPT, named_source or source, named_digest or digest
                if not handle.read(1):
                    return SPILL_LIFE_INCOMPLETE, source, digest
                return SPILL_LIFE_VALID, source, digest
        except (OSError, KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            token = named_digest or hashlib.sha256(path.name.encode("utf-8")).hexdigest()[:32]
            source = named_source or "report"
            return SPILL_LIFE_CORRUPT, source, token

    def _reclaim_orphan_handoff_locked(self) -> int:
        """stream 删除后遗留的 .quarantine / 无主 .reserve 已在 orphans；这里只清交接标记。"""

        if not self.directory.exists():
            return 0
        reclaimed = 0
        seen: set[tuple[str, str]] = set()
        for path in list(self.directory.iterdir()):
            match = STREAM_UNIT_NAME.fullmatch(path.name)
            if match is None or not path.is_file():
                continue
            key = (match.group("source"), match.group("stream_id"))
            if key in seen:
                continue
            seen.add(key)
            source, stream_id = key
            stream_exists = self._stream_tmp(source, stream_id).exists() or self._stream_path(
                source, stream_id
            ).exists()
            if stream_exists:
                continue
            quarantine = self._quarantine_path(source, stream_id)
            if quarantine.exists():
                quarantine.unlink(missing_ok=True)
                quarantine.with_name(quarantine.name + ".tmp").unlink(missing_ok=True)
                reclaimed += 1
        return reclaimed

    def _expire_quarantine_locked(self, now_ts: float) -> int:
        expired = 0
        for path in self._iter_evidence_paths():
            if self._file_age_seconds(path, now_ts) < self.quarantine_retention_s:
                continue
            path.unlink(missing_ok=True)
            expired += 1
        return expired

    def _enforce_quarantine_quota_locked(self) -> int:
        paths = self._iter_evidence_paths()
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
            len(sizes) > self.max_quarantine_files or total > self.max_quarantine_bytes
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
    ) -> None:
        size = 0
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        path.unlink(missing_ok=True)
        if SOURCE_PATTERN.fullmatch(source) is None:
            source = "report"
        if not token:
            token = hashlib.sha256(kind.encode("utf-8")).hexdigest()[:32]
        self._write_nonactive_evidence(source, token, state, kind=kind, size_bytes=size)

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
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / f"{source}-{token}{HEADER_QUARANTINE_SUFFIX}"
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
        self._fsync_directory()
        return target

    def _write_header_quarantine(self, source: str, stream_id: str, kind: str) -> Path:
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
        self._fsync_directory()
        return target

    def _reclaim_classified_locked(
        self, inspection: _StreamInspection, *, quarantine: bool
    ) -> None:
        if SOURCE_PATTERN.fullmatch(inspection.source) and STREAM_ID_PATTERN.fullmatch(
            inspection.stream_id
        ):
            size = 0
            try:
                size = inspection.path.stat().st_size
            except OSError:
                size = 0
            self._remove_stream_locked(inspection.source, inspection.stream_id)
            if quarantine:
                if inspection.kind == STREAM_LIFE_CORRUPT_HEADER:
                    self._write_header_quarantine(
                        inspection.source, inspection.stream_id, inspection.kind
                    )
                else:
                    self._write_nonactive_evidence(
                        inspection.source,
                        inspection.stream_id,
                        inspection.kind,
                        kind="stream",
                        size_bytes=size,
                    )
            return
        inspection.path.unlink(missing_ok=True)

    def _read(self, path: Path, *, expected_source: str | None = None) -> RawSpillRecord | None:
        try:
            with path.open("rb") as handle:
                header_bytes = handle.readline()
                header = json.loads(header_bytes.decode("utf-8"))
                source = str(header["source"])
                if expected_source is not None and source != expected_source:
                    return None
                payload_sha256 = str(header["payload_sha256"])
                if self._path(source, payload_sha256) != path:
                    return None
                self._probe_payload_read(path.name)
                payload_enc = handle.read()
                if not payload_enc:
                    return None
                return RawSpillRecord(
                    source=source,
                    payload_sha256=payload_sha256,
                    key_version=int(header["key_version"]),
                    http_status=int(header["http_status"]),
                    content_encoding=str(header["content_encoding"]),
                    payload_enc=payload_enc,
                    path=path,
                    capture_state=normalize_capture_state(header.get("capture_state")),
                    plaintext_bytes=len(payload_enc),
                )
        except (OSError, KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            return None

    def _parse_stream_header_handle(
        self, handle: BinaryIO, path: Path
    ) -> _StreamFileHeader | None:
        try:
            magic = handle.read(len(STREAM_MAGIC))
            if magic != STREAM_MAGIC:
                return None
            header_bytes = handle.readline()
            if not header_bytes.endswith(b"\n"):
                return None
            header = json.loads(header_bytes.decode("utf-8"))
            source = str(header["source"])
            stream_id = str(header["stream_id"])
            key_version = int(header["key_version"])
            expected_tmp = self._stream_tmp(source, stream_id)
            expected_final = self._stream_path(source, stream_id)
            if path not in {expected_tmp, expected_final}:
                return None
            return _StreamFileHeader(source, stream_id, key_version, path)
        except (OSError, KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            return None

    def _read_control_frame(
        self,
        body: bytes,
        offset: int,
        crypto: StreamChunkCrypto,
        *,
        source: str,
        stream_id: str,
        key_version: int,
        seq: int,
    ) -> tuple[dict[str, object] | None, int, bool]:
        """解析长度化 AES-GCM 控制帧；失败只报告 incomplete，不得抛穿。"""

        if offset + STREAM_RECORD_HEADER.size > len(body):
            return None, offset, False
        (frame_len,) = STREAM_RECORD_HEADER.unpack_from(body, offset)
        offset += STREAM_RECORD_HEADER.size
        if frame_len < STREAM_META_HEADER.size or offset + frame_len > len(body):
            return None, offset, False
        frame = body[offset : offset + frame_len]
        offset += frame_len
        (meta_len,) = STREAM_META_HEADER.unpack_from(frame)
        if STREAM_META_HEADER.size + meta_len > len(frame):
            return None, offset, False
        meta = frame[STREAM_META_HEADER.size : STREAM_META_HEADER.size + meta_len]
        ciphertext = frame[STREAM_META_HEADER.size + meta_len :]
        try:
            header = json.loads(meta.decode("utf-8"))
            kind = str(header["kind"])
            http_status = _normalize_http_status(header.get("http_status"))
            content_encoding = str(header.get("content_encoding") or "identity")
            capture_state = normalize_capture_state(str(header.get("capture_state")))
            plaintext = crypto.decrypt_bound_bytes(
                ciphertext,
                key_version,
                _control_context(
                    source=source,
                    stream_id=stream_id,
                    seq=seq,
                    kind=kind,
                    http_status=http_status,
                    content_encoding=content_encoding,
                    capture_state=capture_state,
                ),
            )
            if plaintext != meta:
                return None, offset, False
            return (
                {
                    "kind": kind,
                    "http_status": http_status,
                    "content_encoding": content_encoding,
                    "capture_state": capture_state,
                },
                offset,
                True,
            )
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            return None, offset, False

    def _parse_legacy_footer(self, rest: bytes) -> dict[str, object] | None:
        """兼容 #415 明文 footer；不完整 JSON 一律视为 terminal 失败。"""

        if not rest or b"\n" not in rest:
            return None
        try:
            line = rest.split(b"\n", maxsplit=1)[0]
            footer = json.loads(line.decode("utf-8"))
            return {
                "kind": STREAM_KIND_TERMINAL,
                "http_status": _normalize_http_status(footer.get("http_status", 200)),
                "content_encoding": str(footer.get("content_encoding") or "identity"),
                "capture_state": normalize_capture_state(str(footer.get("capture_state"))),
            }
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            return None

    def _read_control_handle(
        self,
        handle: BinaryIO,
        crypto: StreamChunkCrypto,
        header: _StreamFileHeader,
        *,
        seq: int,
    ) -> tuple[dict[str, object] | None, bool]:
        raw_len = handle.read(STREAM_RECORD_HEADER.size)
        if len(raw_len) < STREAM_RECORD_HEADER.size:
            return None, False
        (frame_len,) = STREAM_RECORD_HEADER.unpack(raw_len)
        if frame_len < STREAM_META_HEADER.size:
            return None, False
        frame = handle.read(frame_len)
        if len(frame) != frame_len:
            return None, False
        fields, _offset, ok = self._read_control_frame(
            STREAM_RECORD_HEADER.pack(frame_len) + frame,
            0,
            crypto,
            source=header.source,
            stream_id=header.stream_id,
            key_version=header.key_version,
            seq=seq,
        )
        return fields, ok

    def _assemble_stream_handle(
        self,
        handle: BinaryIO,
        header: _StreamFileHeader,
        crypto: StreamChunkCrypto,
    ) -> _AssembledStream:
        """流式解密：同一时刻只持有当前帧 + 累计明文，不保留 raw 全文与 chunks 列表。"""

        plaintext = bytearray()
        hasher = hashlib.sha256()
        announce: dict[str, object] | None = None
        terminal: dict[str, object] | None = None
        incomplete = False
        seq = 0
        while True:
            raw_len = handle.read(STREAM_RECORD_HEADER.size)
            if not raw_len:
                break
            if len(raw_len) < STREAM_RECORD_HEADER.size:
                incomplete = True
                break
            (length,) = STREAM_RECORD_HEADER.unpack(raw_len)
            if length == STREAM_CONTROL_SENTINEL:
                fields, ok = self._read_control_handle(
                    handle, crypto, header, seq=0
                )
                if not ok or fields is None or fields.get("kind") != STREAM_KIND_ANNOUNCE:
                    incomplete = True
                    break
                announce = fields
                continue
            if length == 0:
                marked = handle.tell()
                fields, ok = self._read_control_handle(handle, crypto, header, seq=seq)
                if ok and fields is not None and fields.get("kind") == STREAM_KIND_TERMINAL:
                    terminal = fields
                else:
                    handle.seek(marked)
                    line = handle.readline()
                    footer = line if line.endswith(b"\n") else line + b"\n"
                    legacy = self._parse_legacy_footer(footer)
                    if legacy is None:
                        incomplete = True
                    else:
                        terminal = legacy
                break
            ciphertext = handle.read(length)
            if len(ciphertext) != length:
                incomplete = True
                break
            try:
                chunk = crypto.decrypt_bound_bytes(
                    ciphertext,
                    header.key_version,
                    EncryptionContext(
                        domain="vendor-raw",
                        table="raw_spill",
                        column="chunk",
                        object_id=f"{header.source}:{header.stream_id}:{seq:08d}",
                    ),
                )
            except (ValueError, TypeError):
                incomplete = True
                break
            hasher.update(chunk)
            plaintext.extend(chunk)
            seq += 1
        assembled = bytes(plaintext)
        plaintext.clear()
        return _AssembledStream(assembled, hasher.hexdigest(), announce, terminal, incomplete)

    def _write_quarantine(self, source: str, stream_id: str) -> Path:
        """为不完整 terminal 写下无 PII 的 quarantine 标记，便于巡检与配额记账。"""

        self.directory.mkdir(parents=True, exist_ok=True)
        target = self._quarantine_path(source, stream_id)
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
        self._fsync_directory()
        return target

    def _read_stream(
        self,
        path: Path,
        crypto: StreamChunkCrypto,
        *,
        expected_source: str | None = None,
    ) -> RawSpillRecord | None:
        try:
            handle = path.open("rb")
        except OSError:
            return None
        try:
            header = self._parse_stream_header_handle(handle, path)
            if header is None:
                return None
            if expected_source is not None and header.source != expected_source:
                return None
            self._probe_payload_read(path.name)
            assembled = self._assemble_stream_handle(handle, header, crypto)
        except Exception as exc:
            LOGGER.warning(
                "raw spill stream recover skipped",
                extra={"error_type": type(exc).__name__},
            )
            return None
        finally:
            handle.close()
        if assembled.empty:
            return None
        try:
            payload_sha256 = assembled.digest
            encrypted = crypto.encrypt_bound_bytes(
                assembled.plaintext,
                EncryptionContext(
                    domain="vendor-raw",
                    table="raw_vendor_log",
                    column="payload_enc",
                    object_id=f"{header.source}:{payload_sha256}",
                ),
            )
        except (ValueError, TypeError):
            return None
        quarantined = False
        if assembled.terminal is not None and not assembled.incomplete:
            capture_state = normalize_capture_state(str(assembled.terminal["capture_state"]))
            http_status = _normalize_http_status(assembled.terminal["http_status"])
            content_encoding = str(assembled.terminal["content_encoding"])
        else:
            announce = assembled.announce or {}
            announced_state = (
                str(announce.get("capture_state")) if announce.get("capture_state") else ""
            )
            if announced_state == CAPTURE_PROTOCOL_INVALID:
                capture_state = CAPTURE_PROTOCOL_INVALID
            else:
                capture_state = CAPTURE_TRUNCATED
            http_status = _normalize_http_status(announce.get("http_status")) if announce else 200
            content_encoding = (
                str(announce.get("content_encoding") or "identity") if announce else "identity"
            )
            self._write_quarantine(header.source, header.stream_id)
            quarantined = True
        return RawSpillRecord(
            source=header.source,
            payload_sha256=payload_sha256,
            key_version=encrypted.key_version,
            http_status=http_status,
            content_encoding=content_encoding,
            payload_enc=encrypted.payload,
            path=path,
            capture_state=capture_state,
            quarantined=quarantined,
            stream_id=header.stream_id,
            plaintext_bytes=len(assembled.plaintext),
        )

    def _fsync_directory(self) -> None:
        directory_fd = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


class RawSpillStream:
    """合并为固定内部帧后写入认证加密流；网络 chunk 不得成为持久化帧。"""

    def __init__(
        self,
        store: RawSpillStore,
        source: str,
        crypto: StreamChunkCrypto,
        reservation: SpillReservation,
        *,
        capture_bytes: int = RECOVERY_CAPTURE_BYTES,
    ) -> None:
        self.store = store
        self.source = source
        self.crypto = crypto
        self.reservation = reservation
        self.stream_id = reservation.lease_id
        self._capture_bytes = capture_bytes
        self._max_frames = max_internal_frames(capture_bytes)
        self._seq = 0
        self._unsynced = 0
        self._finished = False
        self._announced = False
        self._key_version: int | None = None
        self._http_status = 200
        self._content_encoding = "identity"
        self._protocol_invalid = False
        self._plaintext_bytes = 0
        self._ondisk_bytes = 0
        self._buffer = bytearray()
        self._capture_state: str | None = None
        self.path = store._stream_tmp(source, self.stream_id)
        store.directory.mkdir(parents=True, exist_ok=True)
        header = json.dumps(
            {"source": source, "stream_id": self.stream_id, "key_version": 0},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        with self.path.open("wb") as handle:
            handle.write(STREAM_MAGIC)
            handle.write(header)
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        store._fsync_directory()
        self._refresh_ondisk_bytes()

    def __enter__(self) -> RawSpillStream:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        flush_announced_pending_frames(self)
        discard_header_only_stream(self)

    @property
    def is_header_only(self) -> bool:
        """尚未 announce、尚未接受正文、也没有待 flush 缓冲的纯文件头。"""

        return (
            not self._finished
            and not self._announced
            and self._seq == 0
            and not self._buffer
        )

    @property
    def has_announced(self) -> bool:
        """announce 控制帧是否已持久化；此后不得当 unused header-only 丢弃。"""

        return self._announced

    @property
    def has_captured_bytes(self) -> bool:
        """是否已接受正文（含未 flush 缓冲）。announce 前的空租约才可立即释放。"""

        return self._seq > 0 or bool(self._buffer)

    @property
    def plaintext_bytes(self) -> int:
        """已接受的明文字节；含仍在内部帧缓冲、尚未加密落盘的尾部。"""

        return self._plaintext_bytes

    @property
    def on_disk_bytes(self) -> int:
        """当前加密流文件字节；与明文计数分离，二者各自有硬上限。"""

        return self._ondisk_bytes

    @property
    def frame_count(self) -> int:
        """已持久化的内部 data frame 数，不是网络 chunk 数。"""

        return self._seq

    @property
    def pending_plaintext_bytes(self) -> int:
        """尚未加密落盘的短帧缓冲。"""

        return len(self._buffer)

    @property
    def capture_state(self) -> str | None:
        """finish 后的完整性状态；未结束则为 None。"""

        return self._capture_state

    @property
    def max_frames(self) -> int:
        """本请求允许的内部 data frame 硬上限。"""

        return self._max_frames

    def feed(self, chunk: bytes) -> bool:
        """把网络/调用方 chunk 合并进固定内部帧。超出明文或落盘上限返回 False。"""

        if self._finished:
            return False
        if not chunk:
            return True
        view = memoryview(chunk)
        offset = 0
        while offset < len(chunk):
            remaining_plain = self._capture_bytes - self._plaintext_bytes
            if remaining_plain <= 0:
                return False
            remaining_frame = INTERNAL_FRAME_SIZE - len(self._buffer)
            take = min(len(chunk) - offset, remaining_plain, remaining_frame)
            if take <= 0:
                return False
            self._buffer.extend(view[offset : offset + take])
            self._plaintext_bytes += take
            offset += take
            if len(self._buffer) >= INTERNAL_FRAME_SIZE and not self._emit_full_frames():
                return False
        return True

    def flush(self) -> bool:
        """把不足一帧的尾部加密落盘。finish 与异常边界必须调用。"""

        if self._finished:
            return False
        if not self._buffer:
            return True
        return self._emit_frame(bytes(self._buffer))

    def announce(
        self,
        *,
        http_status: int,
        content_encoding: str = "identity",
        protocol_invalid: bool = False,
    ) -> None:
        """在读取正文前写入认证 HTTP 元数据，崩溃恢复不得改写状态/编码。"""

        if self._finished:
            return
        if self._buffer and not self.flush():
            raise OSError(errno.ENOSPC, "raw spill announce flush failed")
        self._http_status = _normalize_http_status(http_status)
        self._content_encoding = content_encoding or "identity"
        self._protocol_invalid = bool(protocol_invalid)
        capture_state = CAPTURE_PROTOCOL_INVALID if self._protocol_invalid else CAPTURE_COMPLETE
        self._write_control_frame(
            kind=STREAM_KIND_ANNOUNCE,
            sentinel=STREAM_CONTROL_SENTINEL,
            seq=0,
            http_status=self._http_status,
            content_encoding=self._content_encoding,
            capture_state=capture_state,
        )
        self._announced = True

    def finish(
        self,
        *,
        complete: bool,
        http_status: int | None = None,
        content_encoding: str | None = None,
        too_large: bool = False,
        protocol_invalid: bool = False,
    ) -> None:
        """写入认证完整性页脚并原子晋升为 .stream；截断不得伪造成完整捕获。"""

        if self._finished:
            return
        durable = self.flush()
        if http_status is not None:
            self._http_status = _normalize_http_status(http_status)
        if content_encoding is not None:
            self._content_encoding = content_encoding or "identity"
        if protocol_invalid:
            self._protocol_invalid = True
        if not durable:
            complete = False
            too_large = False
        if self._protocol_invalid:
            capture_state = CAPTURE_PROTOCOL_INVALID
        elif complete and too_large:
            capture_state = CAPTURE_COMPLETE_TOO_LARGE
        elif complete:
            capture_state = CAPTURE_COMPLETE
        else:
            capture_state = CAPTURE_TRUNCATED
        self._capture_state = capture_state
        self._write_control_frame(
            kind=STREAM_KIND_TERMINAL,
            sentinel=0,
            seq=self._seq,
            http_status=self._http_status,
            content_encoding=self._content_encoding,
            capture_state=capture_state,
        )
        final = self.store._stream_path(self.source, self.stream_id)
        os.replace(self.path, final)
        self.store._fsync_directory()
        self.path = final
        self._refresh_ondisk_bytes()
        self._finished = True

    def discard(self) -> None:
        header_only = self.is_header_only
        self.store.remove_stream(self.source, self.stream_id)
        if header_only:
            self.store._header_only_cleaned += 1
        self._buffer.clear()
        self._finished = True

    def _emit_full_frames(self) -> bool:
        """只写出已满的内部帧；尾部短帧留给 flush。"""

        while len(self._buffer) >= INTERNAL_FRAME_SIZE:
            payload = bytes(self._buffer[:INTERNAL_FRAME_SIZE])
            del self._buffer[:INTERNAL_FRAME_SIZE]
            if not self._emit_frame(payload, clear_buffer=False):
                self._buffer = bytearray(payload) + self._buffer
                return False
        return True

    def _emit_frame(self, payload: bytes, *, clear_buffer: bool = True) -> bool:
        """把一个内部帧加密落盘。明文已在 feed 计入，这里只检查帧数与落盘上限。"""

        if not payload:
            return True
        if self._seq >= self._max_frames:
            return False
        framed = len(payload) + DATA_FRAME_OVERHEAD_BYTES
        if self._ondisk_bytes + framed > self.reservation.reserved_bytes:
            return False
        encrypted = self.crypto.encrypt_bound_bytes(
            payload,
            EncryptionContext(
                domain="vendor-raw",
                table="raw_spill",
                column="chunk",
                object_id=f"{self.source}:{self.stream_id}:{self._seq:08d}",
            ),
        )
        self._bind_key_version(encrypted.key_version)
        try:
            with self.path.open("ab") as handle:
                handle.write(STREAM_RECORD_HEADER.pack(len(encrypted.payload)))
                handle.write(encrypted.payload)
                handle.flush()
                self._unsynced += STREAM_RECORD_HEADER.size + len(encrypted.payload)
                if self._unsynced >= SYNC_EVERY_BYTES:
                    os.fsync(handle.fileno())
                    self._unsynced = 0
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                return False
            raise
        self._seq += 1
        if clear_buffer:
            self._buffer.clear()
        self._refresh_ondisk_bytes()
        return True

    def _refresh_ondisk_bytes(self) -> None:
        try:
            self._ondisk_bytes = self.path.stat().st_size
        except OSError:
            return

    def _bind_key_version(self, key_version: int) -> None:
        if self._key_version is None:
            self._rewrite_header_key_version(key_version)
            self._key_version = key_version
        elif key_version != self._key_version:
            raise ValueError("raw spill stream key version changed")

    def _write_control_frame(
        self,
        *,
        kind: str,
        sentinel: int,
        seq: int,
        http_status: int,
        content_encoding: str,
        capture_state: str,
    ) -> None:
        meta = json.dumps(
            {
                "capture_state": capture_state,
                "content_encoding": content_encoding,
                "http_status": http_status,
                "kind": kind,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        encrypted = self.crypto.encrypt_bound_bytes(
            meta,
            _control_context(
                source=self.source,
                stream_id=self.stream_id,
                seq=seq,
                kind=kind,
                http_status=http_status,
                content_encoding=content_encoding,
                capture_state=capture_state,
            ),
        )
        self._bind_key_version(encrypted.key_version)
        payload = STREAM_META_HEADER.pack(len(meta)) + meta + encrypted.payload
        with self.path.open("ab") as handle:
            handle.write(STREAM_RECORD_HEADER.pack(sentinel))
            handle.write(STREAM_RECORD_HEADER.pack(len(payload)))
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        self._unsynced = 0
        self._refresh_ondisk_bytes()

    def _rewrite_header_key_version(self, key_version: int) -> None:
        raw = self.path.read_bytes()
        if not raw.startswith(STREAM_MAGIC):
            raise ValueError("invalid raw spill stream")
        rest = raw[len(STREAM_MAGIC) :]
        header_bytes, body = rest.split(b"\n", maxsplit=1)
        header = json.loads(header_bytes.decode("utf-8"))
        header["key_version"] = key_version
        rewritten = (
            STREAM_MAGIC
            + json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8")
            + b"\n"
            + body
        )
        tmp = self.path.with_name(self.path.name + ".hdr")
        with tmp.open("wb") as handle:
            handle.write(rewritten)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)
        self.store._fsync_directory()
        self._refresh_ondisk_bytes()
