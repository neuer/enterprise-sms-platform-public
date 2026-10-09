"""厂商拉走即消费 spill 的文件格式常量、文件名规则与无状态数据结构。"""

from __future__ import annotations

import logging
import re
import struct
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
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
SPILL_MAGIC = b"SMSXSP2\n"
STREAM_RECORD_HEADER = struct.Struct(">I")
STREAM_META_HEADER = struct.Struct(">H")
STREAM_CONTROL_SENTINEL = 0xFFFFFFFF
STREAM_KIND_ANNOUNCE = "announce"
STREAM_KIND_TERMINAL = "terminal"
STREAM_KIND_SPILL = "spill"
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
DEFAULT_RECLAIM_MAX_FILES = 16
DEFAULT_RECLAIM_MAX_SECONDS = 2.0
DEFAULT_RECLAIM_MAX_HEADER_BYTES = 64 * 1024
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
# secondary spill 认证 header 开销；计入单文件 extra，不得扩大目录配额或缩小 64MiB。
SPILL_HEADER_BUDGET_BYTES = 768
# 3× 厂商绝对超时，避免回收仍在 DNS/connect/TLS 中的在途 header-only。
HEADER_ONLY_RECLAIM_AFTER_S = 30.0
# 非活动隔离与活动拉取配额分离：.headerq 只保存无 PII 小标记；.cq 保存原密文字节。
DEFAULT_MAX_QUARANTINE_FILES = 64
DEFAULT_MAX_QUARANTINE_BYTES = 256 * 1024
DEFAULT_QUARANTINE_RETENTION_S = 86400.0
DEFAULT_MAX_CIPHERQ_FILES = 64
DEFAULT_CIPHERQ_RETENTION_S = 86400.0
REASON_KEY_UNAVAILABLE = "key_unavailable"
REASON_TRANSIENT_IO = "transient_io"
REASON_AUTH_FAILED = "auth_failed"
REASON_CORRUPT = "corrupt"
REASON_PROVABLY_EMPTY = "provably_empty"
CIPHERQ_STATE_PENDING = "pending"
CIPHERQ_STATE_SEALED = "sealed"
CIPHERQ_SUFFIX = ".cq"
CIPHERQ_WIP_SUFFIX = ".cq.wip"
CIPHERQ_MANIFEST_SUFFIX = ".cq.man"
RECLAIM_CURSOR_NAME = ".reclaim.cursor"
CIPHERQ_EVICTABLE_REASONS = frozenset({REASON_AUTH_FAILED, REASON_CORRUPT})
CIPHERQ_RETAIN_REASONS = frozenset({REASON_KEY_UNAVAILABLE, REASON_TRANSIENT_IO})
CIPHERQ_MANIFEST_KEYS = frozenset(
    {
        "isolated_at",
        "kind",
        "reason",
        "sha256",
        "size_bytes",
        "source",
        "src_name",
        "state",
        "token",
    }
)
CIPHERQ_FORBIDDEN_KEYS = frozenset({"phone", "payload", "ciphertext", "secret", "key", "body"})
MAX_CLASSIFY_DATA_CIPHER_BYTES = INTERNAL_FRAME_SIZE + DATA_FRAME_OVERHEAD_BYTES + 64
MAX_CLASSIFY_CONTROL_BYTES = CONTROL_FRAME_BUDGET_BYTES * 4
QUOTA_LOCK_NAME = ".quota.lock"
RESERVE_SUFFIX = ".reserve"
HEADER_QUARANTINE_SUFFIX = ".headerq"
HANDOFF_QUARANTINE_SUFFIX = ".quarantine"
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
RESERVATION_KEYS = frozenset({"created_at", "lease_id", "reserved_bytes", "source"})
STREAM_FILE_NAME = re.compile(
    r"^(?P<source>report|reply)-(?P<stream_id>[0-9a-f]{32})\.stream(?:\.stream|\.tmp)?$"
)
ACTIVITY_FILE_NAME = re.compile(
    r"^(?:report|reply)-[0-9a-f]{32}\.stream(?:\.stream|\.tmp)?$"
    r"|^(?:report|reply)-[0-9a-f]{32}\.spill(?:\.tmp)?$"
    r"|^(?:report|reply)-[0-9a-f]{64}\.spill(?:\.tmp)?$"
)
STREAM_UNIT_NAME = re.compile(
    r"^(?P<source>report|reply)-(?P<stream_id>[0-9a-f]{32})\."
    r"(?:stream(?:\.stream|\.tmp)?(?:\.hdr)?|reserve|quarantine|headerq)(?:\.tmp)?$"
)
SPILL_FILE_NAME = re.compile(
    r"^(?P<source>report|reply)-(?P<token>[0-9a-f]{32}|[0-9a-f]{64})\.spill(?:\.tmp)?$"
)
REWRITE_HDR_NAME = re.compile(
    r"^(?P<source>report|reply)-(?P<stream_id>[0-9a-f]{32})\.stream(?:\.tmp)?\.hdr$"
)
MARKER_TMP_NAME = re.compile(
    r"^(?P<source>report|reply)-(?P<token>[0-9a-f]{32,64})\."
    r"(?:headerq|quarantine|reserve|cq\.man)\.tmp$"
)
CIPHERQ_FILE_NAME = re.compile(
    r"^(?P<source>report|reply)-(?P<token>[0-9a-f]{32,64})\.cq(?:\.wip|\.man)?(?:\.tmp)?$"
)
SPILL_OR_CIPHERQ_NAME = re.compile(
    r"^(?P<source>report|reply)-(?P<token>[0-9a-f]{32}|[0-9a-f]{64})"
    r"(?P<rest>\.spill(?:\.tmp)?|\.cq(?:\.wip)?)$"
)
STREAM_LIFE_LEGAL_HEADER_ONLY = "legal_header_only"
STREAM_LIFE_PARTIAL_HEADER = "partial_header"
STREAM_LIFE_CORRUPT_HEADER = "corrupt_header"
STREAM_LIFE_INCOMPLETE_FRAMES = "incomplete_frames"
STREAM_LIFE_UNAUTHENTICATED_PARTIAL = "unauthenticated_partial"
STREAM_LIFE_HAS_CONTROL = "has_control"
STREAM_LIFE_HAS_AUTHENTICATED_DATA = "has_authenticated_data"
STREAM_LIFE_KEY_UNAVAILABLE = REASON_KEY_UNAVAILABLE
STREAM_LIFE_TRANSIENT_IO = REASON_TRANSIENT_IO
STREAM_LIFE_AUTH_FAILED = REASON_AUTH_FAILED
SPILL_LIFE_VALID = "valid_spill"
SPILL_LIFE_INCOMPLETE = "incomplete_spill"
SPILL_LIFE_CORRUPT = "corrupt_spill"
SPILL_LIFE_TRANSIENT = REASON_TRANSIENT_IO
# 拆分后的各模块共用原日志器名，日志路由与告警过滤保持不变。
LOGGER = logging.getLogger("app.services.raw_spill")


def parse_spill_filename(name: str) -> tuple[str, str, str] | None:
    """解析 spill / cipherq 文件名，得到 source、token 与后缀。"""

    match = SPILL_OR_CIPHERQ_NAME.fullmatch(name)
    if match is None:
        return None
    return match.group("source"), match.group("token"), match.group("rest")


def artifact_id_from_token(token: str) -> str:
    """32 位 hex 是独立 artifact_id；64 位 hex 是历史 digest 文件名。"""

    return token if len(token) == 32 else ""


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
            CIPHERQ_SUFFIX,
            CIPHERQ_SUFFIX + ".tmp",
            CIPHERQ_WIP_SUFFIX,
            CIPHERQ_WIP_SUFFIX + ".tmp",
            CIPHERQ_MANIFEST_SUFFIX,
            CIPHERQ_MANIFEST_SUFFIX + ".tmp",
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
# cipherq 至少能容纳一次完整 64MiB 预留，不得用 256KiB headerq 配额存密文。
DEFAULT_MAX_CIPHERQ_BYTES = capture_reservation_bytes(RECOVERY_CAPTURE_BYTES)


class SpillQuotaExceeded(RuntimeError):
    """spill 目录已达文件数或总字节上限，必须停止继续拉取。"""


class RawSpillDegraded(RuntimeError):
    """本轮 raw 已安全落库，但 spill 崩溃兜底写入能力不可用。"""


class SpillMetadataAuthError(RuntimeError):
    """secondary spill 元数据认证失败；禁止写入 raw_vendor_log。"""


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
    key_unavailable: int = 0
    transient_io: int = 0
    auth_failed: int = 0
    cipherq_expired: int = 0
    cipherq_capacity_dropped: int = 0
    cipherq_manifest_only: int = 0

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
    cipherq_count: int = 0
    cipherq_bytes: int = 0
    oldest_cipherq_age_seconds: float | None = None


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


@dataclass(frozen=True, slots=True)
class ReclaimRoundBudget:
    """单轮 reclaim 上限；只约束 header 分类，不得扫描完整 payload。"""

    max_files: int = DEFAULT_RECLAIM_MAX_FILES
    max_header_bytes: int = DEFAULT_RECLAIM_MAX_HEADER_BYTES
    max_seconds: float = DEFAULT_RECLAIM_MAX_SECONDS

    def exhausted(self, *, inspected: int, used_header_bytes: int, started_at: float) -> bool:
        if inspected >= self.max_files:
            return True
        if used_header_bytes >= self.max_header_bytes:
            return True
        return inspected > 0 and (time.monotonic() - started_at) >= self.max_seconds


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


def durable_persist_capture_state(record: RawSpillRecord) -> str:
    """只有认证过的 capture_state 可以进入库；未认证格式一律 unknown_legacy。"""

    if record.format_legacy or not record.metadata_authenticated:
        return CAPTURE_UNKNOWN_LEGACY
    return normalize_capture_state(record.capture_state)


def spill_file_identity_matches(record: RawSpillRecord) -> bool:
    """文件名必须属于该 source，且是 digest 旧名、独立 artifact_id 或 cipherq 认领名。"""

    if record.stream_id:
        return True
    parsed = parse_spill_filename(record.path.name)
    if parsed is None:
        return False
    named_source, token, rest = parsed
    if named_source != record.source:
        return False
    if rest.startswith(".cq"):
        if record.artifact_id:
            return token == record.artifact_id
        return len(token) != 64 or token == record.payload_sha256
    if len(token) == 64:
        return token == record.payload_sha256
    if record.artifact_id:
        return token == record.artifact_id
    return True


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
    metadata_authenticated: bool = True
    format_legacy: bool = False
    artifact_id: str = ""

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
    legacy_terminal: bool = False

    @property
    def empty(self) -> bool:
        return not self.plaintext and self.announce is None and self.terminal is None
