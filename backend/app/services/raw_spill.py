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

from cryptography.exceptions import InvalidTag

from app.services.crypto import (
    BOUND_ENVELOPE_MAGIC,
    NONCE_SIZE,
    TAG_SIZE,
    EncryptedValue,
    EncryptionContext,
    UnknownKeyVersionError,
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
    r"^(?P<source>report|reply)-(?P<stream_id>[0-9a-f]{32})\.stream(?:\.tmp)?$"
)
ACTIVITY_FILE_NAME = re.compile(
    r"^(?:report|reply)-[0-9a-f]{32}\.stream(?:\.tmp)?$"
    r"|^(?:report|reply)-[0-9a-f]{32}\.spill(?:\.tmp)?$"
    r"|^(?:report|reply)-[0-9a-f]{64}\.spill(?:\.tmp)?$"
)
STREAM_UNIT_NAME = re.compile(
    r"^(?P<source>report|reply)-(?P<stream_id>[0-9a-f]{32})\."
    r"(?:stream(?:\.tmp)?(?:\.hdr)?|reserve|quarantine|headerq)(?:\.tmp)?$"
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
LOGGER = logging.getLogger(__name__)


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


@dataclass(frozen=True, slots=True)
class _StreamInspection:
    kind: str
    source: str
    stream_id: str
    age_seconds: float
    path: Path


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


def _require_http_status(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("invalid http_status")
    status = int(value)
    if not 100 <= status <= 599:
        raise ValueError("invalid http_status")
    return status


def _require_key_version(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("invalid key_version")
    version = int(value)
    if not 1 <= version <= 32767:
        raise ValueError("invalid key_version")
    return version


def _require_content_encoding(value: object) -> str:
    encoding = str(value or "")
    if not encoding or len(encoding) > 64 or "/" in encoding or "\\" in encoding:
        raise ValueError("invalid content_encoding")
    return encoding


def canonical_spill_header(
    *,
    source: str,
    payload_sha256: str,
    payload_enc_sha256: str,
    key_version: int,
    http_status: int,
    content_encoding: str,
    capture_state: str,
) -> bytes:
    """规范化 secondary spill 认证 header；任一字段变化都会改变正文 AAD。"""

    if SOURCE_PATTERN.fullmatch(source) is None:
        raise ValueError("invalid raw spill source")
    if SHA256_PATTERN.fullmatch(payload_sha256) is None:
        raise ValueError("invalid raw spill digest")
    if SHA256_PATTERN.fullmatch(payload_enc_sha256) is None:
        raise ValueError("invalid raw spill ciphertext digest")
    document = {
        "capture_state": normalize_capture_state(capture_state),
        "content_encoding": _require_content_encoding(content_encoding),
        "http_status": _require_http_status(http_status),
        "key_version": _require_key_version(key_version),
        "kind": STREAM_KIND_SPILL,
        "payload_enc_sha256": payload_enc_sha256,
        "payload_sha256": payload_sha256,
        "source": source,
    }
    return json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _spill_header_context(meta: dict[str, object]) -> EncryptionContext:
    source = str(meta["source"])
    payload_sha256 = str(meta["payload_sha256"])
    payload_enc_sha256 = str(meta["payload_enc_sha256"])
    http_status = _require_http_status(meta["http_status"])
    content_encoding = _require_content_encoding(meta["content_encoding"])
    capture_state = normalize_capture_state(str(meta["capture_state"]))
    key_version = _require_key_version(meta["key_version"])
    object_id = (
        f"{source}:{payload_sha256}:{payload_enc_sha256}:"
        f"{http_status}:{content_encoding}:{capture_state}:{key_version}"
    )
    return EncryptionContext(
        domain="vendor-raw",
        table="raw_spill",
        column=STREAM_KIND_SPILL,
        object_id=object_id,
    )


def _crypto_key_versions(crypto: StreamChunkCrypto) -> tuple[int, ...]:
    raw_versions = getattr(crypto, "key_versions", None)
    if not raw_versions:
        return ()
    versions: list[int] = []
    for value in raw_versions:
        version = int(value)
        if version not in versions:
            versions.append(version)
    return tuple(versions)


def _crypto_has_version(crypto: StreamChunkCrypto, version: int) -> bool:
    versions = _crypto_key_versions(crypto)
    return not versions or version in versions


def candidate_key_versions(crypto: StreamChunkCrypto, hint: int | None = None) -> tuple[int, ...]:
    """认证尝试顺序：提示版本与 active 优先，但必须覆盖整个 keyring。"""

    ordered: list[int] = []
    for value in (hint, getattr(crypto, "active_version", None)):
        if isinstance(value, int) and not isinstance(value, bool) and value not in ordered:
            ordered.append(value)
    for version in _crypto_key_versions(crypto):
        if version not in ordered:
            ordered.append(version)
    if not ordered:
        ordered = [1]
    return tuple(ordered)


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
        self.directory = directory
        self.max_total_bytes = max_total_bytes
        self.max_pending_files = max_pending_files
        self.header_only_min_age_s = header_only_min_age_s
        self.max_quarantine_files = max_quarantine_files
        self.max_quarantine_bytes = max_quarantine_bytes
        self.quarantine_retention_s = quarantine_retention_s
        self.max_cipherq_files = max_cipherq_files
        self.max_cipherq_bytes = max_cipherq_bytes
        self.cipherq_retention_s = cipherq_retention_s
        self.recover_budget = recover_budget or RecoverRoundBudget()
        self.reclaim_budget = reclaim_budget or ReclaimRoundBudget()
        self._probe = memory_probe
        self._header_only_cleaned = 0
        self._isolated_total = 0
        self._quarantine_expired = 0
        self._quarantine_capacity_dropped = 0
        self._cipherq_expired = 0
        self._cipherq_capacity_dropped = 0
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

    def _path(self, source: str, payload_sha256: str) -> Path:
        if SOURCE_PATTERN.fullmatch(source) is None:
            raise ValueError("invalid raw spill source")
        if SHA256_PATTERN.fullmatch(payload_sha256) is None:
            raise ValueError("invalid raw spill digest")
        return self.directory / f"{source}-{payload_sha256}.spill"

    def _spill_path_matches_identity(
        self, path: Path, source: str, payload_sha256: str
    ) -> bool:
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
            if path.is_file() and path.name not in {QUOTA_LOCK_NAME, RECLAIM_CURSOR_NAME}
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
        extra = (
            len(SPILL_MAGIC)
            + STREAM_RECORD_HEADER.size
            + len(frame)
            + len(payload_enc)
        )
        if extra > len(payload_enc) + SPILL_HEADER_BUDGET_BYTES:
            raise ValueError("authenticated spill header exceeds budget")
        with self._quota_lock():
            self._reclaim_nonstream_locked(time.time(), crypto)
            # 在途 .stream 与即将落盘的 .spill 是同一捕获的两份密文，不得互相占满文件配额。
            same_capture = self._reservation_for_source_locked(source) is not None
            if self.pending_spill_count() >= self.max_pending_files or (
                not same_capture and self._accounted_usage_locked() + extra > self.max_total_bytes
            ):
                raise SpillQuotaExceeded("raw spill quota exceeded")
        if SOURCE_PATTERN.fullmatch(source) is None:
            raise ValueError("invalid raw spill source")
        if SHA256_PATTERN.fullmatch(payload_sha256) is None:
            raise ValueError("invalid raw spill digest")
        self.directory.mkdir(parents=True, exist_ok=True)
        artifact_id = secrets.token_hex(16)
        target = self.directory / f"{source}-{artifact_id}.spill"
        tmp = self.directory / f"{source}-{artifact_id}.spill.tmp"
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
        self._fsync_directory()
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

    def iter_recoverable(
        self, crypto: StreamChunkCrypto, source: str
    ) -> Iterator[RawSpillRecord]:
        """恢复入口：只产出指定 source，一次一条；认证失败隔离，不写库。"""

        if SOURCE_PATTERN.fullmatch(source) is None:
            raise ValueError("invalid raw spill source")
        yield from self._iter_records(source, streams=False, crypto=crypto, isolate=True)
        yield from self._iter_records(source, streams=True, crypto=crypto, isolate=False)
        yield from self._iter_cipherq_recoverable(crypto, source)

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
        if not self.directory.exists():
            return
        for path in self._pending_paths(source, streams=streams):
            try:
                if streams:
                    if crypto is None:
                        continue
                    record = self._read_stream(path, crypto, expected_source=source)
                else:
                    record = self._read(path, expected_source=source, crypto=crypto)
            except SpillMetadataAuthError:
                LOGGER.warning(
                    "raw spill metadata authentication failed",
                    extra={"kind": "spill", "state": REASON_AUTH_FAILED},
                )
                if isolate and not streams:
                    self._isolate_spill_auth_failure(path, locked=False)
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
        """只删除历史 digest 文件名；权威清理以 record.path 为准。"""

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
        cipherq_count = 0
        cipherq_bytes = 0
        oldest_cipherq: float | None = None
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
            cipherq_count, cipherq_bytes = self._cipherq_usage_locked()
            for path in self._iter_cipherq_payload_paths_locked():
                try:
                    age = self._file_age_seconds(path, now_ts)
                except OSError:
                    continue
                oldest_cipherq = age if oldest_cipherq is None else max(oldest_cipherq, age)
        return ArtifactStats(
            active_count=self.pending_count(),
            quarantine_count=quarantine_count + cipherq_count,
            quarantine_bytes=quarantine_bytes + cipherq_bytes,
            oldest_quarantine_age_seconds=oldest if oldest_cipherq is None else (
                oldest_cipherq if oldest is None else max(oldest, oldest_cipherq)
            ),
            isolated_total=self._isolated_total,
            expired_total=self._quarantine_expired + self._cipherq_expired,
            capacity_dropped_total=(
                self._quarantine_capacity_dropped + self._cipherq_capacity_dropped
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
            if not path.is_file() or path.name in {QUOTA_LOCK_NAME, RECLAIM_CURSOR_NAME}:
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
            return _StreamInspection(
                STREAM_LIFE_TRANSIENT_IO, named_source, named_id, age, path
            )
        try:
            return self._classify_stream_header(
                handle,
                named_source=named_source,
                named_id=named_id,
                age=age,
                path=path,
                crypto=crypto,
            )
        except OSError:
            return _StreamInspection(
                STREAM_LIFE_TRANSIENT_IO, named_source, named_id, age, path
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
        if not _crypto_has_version(crypto, key_version):
            return _StreamInspection(
                STREAM_LIFE_KEY_UNAVAILABLE, source, stream_id, age, path
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
        except UnknownKeyVersionError:
            return _StreamInspection(
                STREAM_LIFE_KEY_UNAVAILABLE, source, stream_id, age, path
            )
        except InvalidTag:
            return _StreamInspection(STREAM_LIFE_AUTH_FAILED, source, stream_id, age, path)
        except OSError:
            return _StreamInspection(
                STREAM_LIFE_TRANSIENT_IO, source, stream_id, age, path
            )
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
        if not _crypto_has_version(crypto, key_version):
            return _StreamInspection(
                STREAM_LIFE_KEY_UNAVAILABLE, source, stream_id, age, path
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
        except UnknownKeyVersionError:
            return _StreamInspection(
                STREAM_LIFE_KEY_UNAVAILABLE, source, stream_id, age, path
            )
        except InvalidTag:
            return _StreamInspection(STREAM_LIFE_AUTH_FAILED, source, stream_id, age, path)
        except OSError:
            return _StreamInspection(
                STREAM_LIFE_TRANSIENT_IO, source, stream_id, age, path
            )
        except (ValueError, TypeError):
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
        key_unavailable = 0
        transient_io = 0
        auth_failed = 0
        cipherq_dropped_before = self._cipherq_capacity_dropped
        temps = self._reclaim_rewrite_tmps_locked(now_ts)
        temps += self._reclaim_marker_tmps_locked()
        temps += self._reclaim_cipherq_pending_locked(now_ts)
        spill_counts = self._reclaim_spills_locked(now_ts, crypto)
        isolated += spill_counts[0]
        key_unavailable += spill_counts[1]
        transient_io += spill_counts[2]
        auth_failed += spill_counts[3]
        for path in self._iter_stream_paths():
            inspection = self._inspect_stream_path(path, crypto=crypto, now_ts=now_ts)
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
            if inspection.age_seconds < self.header_only_min_age_s:
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
        orphans = self._reclaim_orphans_locked(None)
        orphans += self._reclaim_orphan_handoff_locked()
        expired = self._expire_quarantine_locked(now_ts)
        dropped = self._enforce_quarantine_quota_locked()
        cipherq_expired = self._expire_cipherq_locked(now_ts)
        self._enforce_cipherq_quota_locked()
        cipherq_manifest_only = self._reclaim_cipherq_manifest_only_locked()
        cipherq_dropped = self._cipherq_capacity_dropped - cipherq_dropped_before
        cleaned = header_only + partial_header + corrupt_header
        self._header_only_cleaned += cleaned
        self._isolated_total += isolated
        self._quarantine_expired += expired
        self._quarantine_capacity_dropped += dropped
        self._cipherq_expired += cipherq_expired
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
        self.last_reclaim = result
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

    def _reclaim_nonstream_locked(
        self, now_ts: float, crypto: StreamChunkCrypto | None = None
    ) -> None:
        """spill/tmp/孤儿分类；有 crypto 时认证失败的 .spill 立即离开活动配额。"""

        self._reclaim_marker_tmps_locked()
        self._reclaim_cipherq_pending_locked(now_ts)
        self._reclaim_spills_locked(now_ts, crypto)
        self._reclaim_orphans_locked(None)
        self._reclaim_orphan_handoff_locked()
        self._expire_quarantine_locked(now_ts)
        self._enforce_quarantine_quota_locked()
        self._expire_cipherq_locked(now_ts)
        self._enforce_cipherq_quota_locked()
        self._reclaim_cipherq_manifest_only_locked()

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

    def _reclaim_spills_locked(
        self, now_ts: float, crypto: StreamChunkCrypto | None = None
    ) -> tuple[int, int, int, int]:
        """损坏/认证失败的 .spill 进入 cipherq；缺 Key / 暂态 I/O 不得销毁。

        锁内只认证小 header，并受文件数/header 字节/时间预算约束。
        """

        if not self.directory.exists():
            return 0, 0, 0, 0
        isolated = 0
        key_unavailable = 0
        transient_io = 0
        auth_failed = 0
        paths = [
            path
            for path in [
                *sorted(self.directory.glob("*.spill")),
                *sorted(self.directory.glob("*.spill.tmp")),
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
            if self.reclaim_budget.exhausted(
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
                if age_seconds < self.header_only_min_age_s:
                    continue
                try:
                    os.replace(path, target)
                except FileNotFoundError:
                    # writer 在分类后先完成改名；目标已存在才算这次交错已收敛。
                    if not target.is_file():
                        raise
                    continue
                self._fsync_directory()
                path = target
            if kind == SPILL_LIFE_VALID:
                if crypto is None:
                    continue
                reason = self._inspect_spill_header_auth(path, crypto)
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
            aged = self._file_age_seconds(path, now_ts) >= self.header_only_min_age_s
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
        path = self.directory / RECLAIM_CURSOR_NAME
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
            self._write_json_atomic(self.directory / RECLAIM_CURSOR_NAME, {"after": after})
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
                expected = self._path(source, digest)
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
    ) -> bool:
        """把仍可能有效的密文原子迁入 cipherq；禁止先 unlink。"""

        reason = state
        if state in {SPILL_LIFE_CORRUPT, SPILL_LIFE_INCOMPLETE, "corrupt_tmp"}:
            reason = REASON_CORRUPT
        elif state == "auth_failed":
            reason = REASON_AUTH_FAILED
        return self._isolate_ciphertext_locked(
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
                self._remove_stream_locked(inspection.source, inspection.stream_id)
                return True
            inspection.path.unlink(missing_ok=True)
            return True
        token = inspection.stream_id
        if not token:
            token = hashlib.sha256(inspection.path.name.encode("utf-8")).hexdigest()[:32]
        moved = self._isolate_ciphertext_locked(
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
            self._release_stream_activity_locked(inspection.source, inspection.stream_id)
            if inspection.kind == STREAM_LIFE_CORRUPT_HEADER:
                self._write_header_quarantine(
                    inspection.source, inspection.stream_id, inspection.kind
                )
        return True

    def _isolate_spill_auth_failure(self, path: Path, *, locked: bool) -> None:
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
            with self._quota_lock():
                self._isolate_named_file_locked(
                    path, source=source, token=token, state="auth_failed", kind="spill"
                )
        self._isolated_total += 1
        previous = self.last_reclaim
        self.last_reclaim = SpillReclaimResult(
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

    def _parse_spill_header_frame(
        self, handle: BinaryIO
    ) -> tuple[bytes, bytes] | None:
        raw_len = handle.read(STREAM_RECORD_HEADER.size)
        if len(raw_len) < STREAM_RECORD_HEADER.size:
            return None
        (frame_len,) = STREAM_RECORD_HEADER.unpack(raw_len)
        if frame_len < STREAM_META_HEADER.size or frame_len > MAX_CLASSIFY_CONTROL_BYTES:
            return None
        frame = handle.read(frame_len)
        if len(frame) != frame_len:
            return None
        (meta_len,) = STREAM_META_HEADER.unpack_from(frame)
        if STREAM_META_HEADER.size + meta_len > len(frame):
            return None
        meta = frame[STREAM_META_HEADER.size : STREAM_META_HEADER.size + meta_len]
        ciphertext = frame[STREAM_META_HEADER.size + meta_len :]
        return meta, ciphertext

    def _decrypt_spill_header(
        self, meta: bytes, ciphertext: bytes, crypto: StreamChunkCrypto
    ) -> dict[str, object]:
        try:
            parsed = json.loads(meta.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise SpillMetadataAuthError("spill header is not an object")
            canonical = canonical_spill_header(
                source=str(parsed["source"]),
                payload_sha256=str(parsed["payload_sha256"]),
                payload_enc_sha256=str(parsed["payload_enc_sha256"]),
                key_version=_require_key_version(parsed["key_version"]),
                http_status=_require_http_status(parsed["http_status"]),
                content_encoding=_require_content_encoding(parsed["content_encoding"]),
                capture_state=str(parsed["capture_state"]),
            )
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise SpillMetadataAuthError("spill header is not canonical") from exc
        if canonical != meta:
            raise SpillMetadataAuthError("spill header is not canonical")
        context = _spill_header_context(parsed)
        hint = _require_key_version(parsed["key_version"])
        if not _crypto_has_version(crypto, hint):
            raise UnknownKeyVersionError(hint)
        last_error: Exception | None = None
        for version in candidate_key_versions(crypto, hint):
            try:
                plaintext = crypto.decrypt_bound_bytes(ciphertext, version, context)
            except UnknownKeyVersionError:
                raise
            except (ValueError, TypeError, InvalidTag) as exc:
                last_error = exc
                continue
            if plaintext != meta:
                raise SpillMetadataAuthError("spill header plaintext mismatch")
            authenticated = json.loads(plaintext.decode("utf-8"))
            if not isinstance(authenticated, dict):
                raise SpillMetadataAuthError("spill header is not an object")
            if _require_key_version(authenticated["key_version"]) != hint:
                raise SpillMetadataAuthError("spill header key_version mismatch")
            return authenticated
        raise SpillMetadataAuthError("spill header authentication failed") from last_error

    def _inspect_spill_header_auth(
        self, path: Path, crypto: StreamChunkCrypto
    ) -> str | None:
        """回收路径只认证 header 帧。成功返回 None，否则返回 typed reason。"""

        try:
            with path.open("rb") as handle:
                magic = handle.read(len(SPILL_MAGIC))
                if magic != SPILL_MAGIC:
                    return None
                parsed = self._parse_spill_header_frame(handle)
                if parsed is None:
                    return REASON_CORRUPT
                meta, ciphertext = parsed
                try:
                    visible = json.loads(meta.decode("utf-8"))
                    hint = _require_key_version(visible["key_version"])
                except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
                    return REASON_AUTH_FAILED
                if not _crypto_has_version(crypto, hint):
                    return REASON_KEY_UNAVAILABLE
                self._decrypt_spill_header(meta, ciphertext, crypto)
                return None
        except UnknownKeyVersionError:
            return REASON_KEY_UNAVAILABLE
        except SpillMetadataAuthError:
            return REASON_AUTH_FAILED
        except OSError:
            return REASON_TRANSIENT_IO
        except (TypeError, ValueError, UnicodeError):
            return REASON_AUTH_FAILED

    def _read(
        self,
        path: Path,
        *,
        expected_source: str | None = None,
        crypto: StreamChunkCrypto | None = None,
    ) -> RawSpillRecord | None:
        try:
            with path.open("rb") as handle:
                magic = handle.read(len(SPILL_MAGIC))
                if magic == SPILL_MAGIC:
                    return self._read_authenticated_spill(
                        handle, path, expected_source=expected_source, crypto=crypto
                    )
                handle.seek(0)
                return self._read_legacy_spill(handle, path, expected_source=expected_source)
        except SpillMetadataAuthError:
            raise
        except UnknownKeyVersionError:
            raise
        except (OSError, KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            return None

    def _read_authenticated_spill(
        self,
        handle: BinaryIO,
        path: Path,
        *,
        expected_source: str | None,
        crypto: StreamChunkCrypto | None,
    ) -> RawSpillRecord | None:
        parsed_frame = self._parse_spill_header_frame(handle)
        if parsed_frame is None:
            return None
        meta, ciphertext = parsed_frame
        try:
            visible = json.loads(meta.decode("utf-8"))
            source = str(visible["source"])
            payload_sha256 = str(visible["payload_sha256"])
            payload_enc_sha256 = str(visible["payload_enc_sha256"])
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            if crypto is not None:
                raise SpillMetadataAuthError("spill header is unreadable") from exc
            return None
        if expected_source is not None and source != expected_source:
            return None
        if not self._spill_path_matches_identity(path, source, payload_sha256):
            if crypto is not None:
                raise SpillMetadataAuthError("spill filename identity mismatch")
            return None
        authenticated: dict[str, object] | None = None
        if crypto is not None:
            authenticated = self._decrypt_spill_header(meta, ciphertext, crypto)
            source = str(authenticated["source"])
            payload_sha256 = str(authenticated["payload_sha256"])
            payload_enc_sha256 = str(authenticated["payload_enc_sha256"])
            if not self._spill_path_matches_identity(path, source, payload_sha256):
                raise SpillMetadataAuthError("spill filename identity mismatch")
            if expected_source is not None and source != expected_source:
                return None
        self._probe_payload_read(path.name)
        payload_enc = handle.read()
        if not payload_enc:
            return None
        if hashlib.sha256(payload_enc).hexdigest() != payload_enc_sha256:
            if crypto is not None:
                raise SpillMetadataAuthError("spill payload digest mismatch")
            return None
        fields = authenticated or visible
        parsed_name = parse_spill_filename(path.name)
        artifact_id = artifact_id_from_token(parsed_name[1]) if parsed_name else ""
        return RawSpillRecord(
            source=source,
            payload_sha256=payload_sha256,
            key_version=_require_key_version(fields["key_version"]),
            http_status=_require_http_status(fields["http_status"]),
            content_encoding=_require_content_encoding(fields["content_encoding"]),
            payload_enc=payload_enc,
            path=path,
            capture_state=normalize_capture_state(str(fields["capture_state"])),
            plaintext_bytes=len(payload_enc),
            metadata_authenticated=authenticated is not None,
            format_legacy=False,
            artifact_id=artifact_id,
        )

    def _read_legacy_spill(
        self,
        handle: BinaryIO,
        path: Path,
        *,
        expected_source: str | None,
    ) -> RawSpillRecord | None:
        header_bytes = handle.readline()
        header = json.loads(header_bytes.decode("utf-8"))
        source = str(header["source"])
        if expected_source is not None and source != expected_source:
            return None
        payload_sha256 = str(header["payload_sha256"])
        if not self._spill_path_matches_identity(path, source, payload_sha256):
            return None
        self._probe_payload_read(path.name)
        payload_enc = handle.read()
        if not payload_enc:
            return None
        try:
            key_version = _require_key_version(header.get("key_version", 1))
        except ValueError:
            key_version = 1
        try:
            http_status = _require_http_status(header.get("http_status", 200))
        except ValueError:
            http_status = 200
        try:
            content_encoding = _require_content_encoding(header.get("content_encoding", "identity"))
        except ValueError:
            content_encoding = "identity"
        return RawSpillRecord(
            source=source,
            payload_sha256=payload_sha256,
            key_version=key_version,
            http_status=http_status,
            content_encoding=content_encoding,
            payload_enc=payload_enc,
            path=path,
            capture_state=CAPTURE_UNKNOWN_LEGACY,
            plaintext_bytes=len(payload_enc),
            metadata_authenticated=False,
            format_legacy=True,
        )

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
                parsed = parse_spill_filename(path.name)
                if (
                    parsed is None
                    or parsed[0] != source
                    or not parsed[2].startswith(".cq")
                ):
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
        except UnknownKeyVersionError:
            raise
        except InvalidTag:
            return None, offset, False
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
        legacy_terminal = False
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
                        legacy_terminal = True
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
            except UnknownKeyVersionError:
                raise
            except (InvalidTag, ValueError, TypeError):
                incomplete = True
                break
            hasher.update(chunk)
            plaintext.extend(chunk)
            seq += 1
        assembled = bytes(plaintext)
        plaintext.clear()
        return _AssembledStream(
            assembled, hasher.hexdigest(), announce, terminal, incomplete, legacy_terminal
        )

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
        except UnknownKeyVersionError:
            raise
        except OSError:
            return None
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
        metadata_authenticated = True
        if assembled.terminal is not None and not assembled.incomplete:
            capture_state = normalize_capture_state(str(assembled.terminal["capture_state"]))
            http_status = _normalize_http_status(assembled.terminal["http_status"])
            content_encoding = str(assembled.terminal["content_encoding"])
            if assembled.legacy_terminal:
                capture_state = CAPTURE_UNKNOWN_LEGACY
                metadata_authenticated = False
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
            metadata_authenticated=metadata_authenticated,
            format_legacy=assembled.legacy_terminal,
        )

    def _cipherq_paths(self, source: str, token: str) -> tuple[Path, Path]:
        stem = f"{source}-{token}"
        return (
            self.directory / f"{stem}{CIPHERQ_SUFFIX}",
            self.directory / f"{stem}{CIPHERQ_MANIFEST_SUFFIX}",
        )

    def _cipherq_wip(self, dest: Path) -> Path:
        return dest.with_name(dest.name + ".wip")

    def _allocate_cipherq_dest_locked(
        self, source: str, token: str
    ) -> tuple[str, Path, Path]:
        """同一 artifact 复用原路径；不同 artifact 分配新 token，禁止覆盖。"""

        dest, manifest_path = self._cipherq_paths(source, token)
        if not dest.exists() and not self._cipherq_wip(dest).exists():
            return token, dest, manifest_path
        existing = self._parse_cipherq_manifest(manifest_path)
        if existing is not None and existing.token == token and existing.source == source:
            return token, dest, manifest_path
        token = secrets.token_hex(16)
        dest, manifest_path = self._cipherq_paths(source, token)
        return token, dest, manifest_path

    def _safe_src_name(self, name: str) -> str | None:
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            return None
        if name.startswith("."):
            return None
        return name

    def _file_size_and_sha256(self, path: Path) -> tuple[int, str]:
        hasher = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)
                size += len(chunk)
        return size, hasher.hexdigest()

    def _write_json_atomic(self, target: Path, document: dict[str, object]) -> None:
        payload = json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")
        tmp = target.with_name(target.name + ".tmp")
        with tmp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        self._fsync_directory()

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
            src_name = self._safe_src_name(str(raw["src_name"]))
            if src_name is None:
                return None
            if SOURCE_PATTERN.fullmatch(source) is None:
                return None
            if not re.fullmatch(r"[0-9a-f]{32,64}", token):
                return None
            dest, expected = self._cipherq_paths(source, token)
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
        if not self.directory.exists():
            return []
        items: list[_CipherqEntry] = []
        for path in sorted(self.directory.glob(f"*{CIPHERQ_MANIFEST_SUFFIX}")):
            if path.name.endswith(".tmp"):
                continue
            entry = self._parse_cipherq_manifest(path)
            if entry is not None:
                items.append(entry)
        return items

    def _iter_cipherq_payload_paths_locked(self) -> list[Path]:
        if not self.directory.exists():
            return []
        items: list[Path] = []
        for path in [
            *sorted(self.directory.glob("*.cq")),
            *sorted(self.directory.glob("*.cq.wip")),
        ]:
            if not path.is_file() or path.name.endswith(".tmp"):
                continue
            items.append(path)
        return items

    def _cipherq_usage_locked(self) -> tuple[int, int]:
        count = 0
        total = 0
        for path in self._iter_cipherq_payload_paths_locked():
            try:
                total += path.stat().st_size
            except OSError:
                continue
            count += 1
        return count, total

    def _drop_cipherq_entry_locked(self, entry: _CipherqEntry) -> None:
        entry.dest.unlink(missing_ok=True)
        self._cipherq_wip(entry.dest).unlink(missing_ok=True)
        entry.manifest.unlink(missing_ok=True)
        entry.dest.with_name(entry.dest.name + ".tmp").unlink(missing_ok=True)
        entry.manifest.with_name(entry.manifest.name + ".tmp").unlink(missing_ok=True)

    def _enforce_cipherq_quota_locked(
        self, extra_files: int = 0, extra_bytes: int = 0
    ) -> int:
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
            total_files + extra_files > self.max_cipherq_files
            or total_bytes + extra_bytes > self.max_cipherq_bytes
        ):
            _mtime, size, entry = evictable.pop(0)
            self._drop_cipherq_entry_locked(entry)
            total_files -= 1
            total_bytes -= size
            dropped += 1
        if dropped:
            self._cipherq_capacity_dropped += dropped
        return dropped

    def _ensure_cipherq_capacity_locked(self, additional_bytes: int) -> bool:
        self._enforce_cipherq_quota_locked(extra_files=1, extra_bytes=additional_bytes)
        count, total = self._cipherq_usage_locked()
        return (
            count + 1 <= self.max_cipherq_files
            and total + additional_bytes <= self.max_cipherq_bytes
        )

    def _expire_cipherq_locked(self, now_ts: float) -> int:
        expired = 0
        for entry in self._iter_cipherq_entries_locked():
            if entry.state != CIPHERQ_STATE_SEALED or entry.reason not in CIPHERQ_EVICTABLE_REASONS:
                continue
            aged_path = entry.dest if entry.dest.exists() else entry.manifest
            if self._file_age_seconds(aged_path, now_ts) < (
                self.cipherq_retention_s
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
            self._write_json_atomic(entry.manifest, document)
            return True
        except OSError:
            return False

    def _reclaim_cipherq_pending_locked(self, now_ts: float | None = None) -> int:
        """重入未完成隔离：有源则改名，有目标则封印；过期 .cq.wip 退回 .cq。"""

        reclaimed = 0
        now_value = time.time() if now_ts is None else now_ts
        for entry in self._iter_cipherq_entries_locked():
            source_path = self.directory / entry.src_name
            wip = self._cipherq_wip(entry.dest)
            if wip.exists() and not entry.dest.exists():
                if self._file_age_seconds(wip, now_value) >= self.header_only_min_age_s:
                    try:
                        os.replace(wip, entry.dest)
                        self._fsync_directory()
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
                    self._fsync_directory()
                    self._seal_cipherq_manifest_locked(entry)
                    reclaimed += 1
                except OSError:
                    continue
        return reclaimed

    def _iter_orphan_cipherq_paths_locked(self) -> list[Path]:
        known = {entry.dest.name for entry in self._iter_cipherq_entries_locked()}
        known.update(name + ".wip" for name in list(known))
        return [
            path
            for path in self._iter_cipherq_payload_paths_locked()
            if path.name not in known
        ]

    def _reclaim_cipherq_manifest_only_locked(self) -> int:
        """清单无密文：确定终态后删除清单；不得 silently 留下不可恢复孤儿。"""

        dropped = 0
        for entry in self._iter_cipherq_entries_locked():
            if entry.dest.exists() or self._cipherq_wip(entry.dest).exists():
                continue
            source_path = self.directory / entry.src_name
            if entry.state == CIPHERQ_STATE_PENDING and source_path.exists():
                continue
            LOGGER.warning(
                "raw spill cipherq manifest missing ciphertext",
                extra={"kind": entry.kind, "reason": entry.reason, "source": entry.source},
            )
            entry.manifest.unlink(missing_ok=True)
            dropped += 1
        return dropped

    def _isolate_ciphertext_locked(
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
        if dest.exists() or self._cipherq_wip(dest).exists():
            existing = self._parse_cipherq_manifest(manifest_path)
            if existing is not None and existing.token == token:
                wip = self._cipherq_wip(dest)
                if path == wip and not dest.exists():
                    try:
                        os.replace(path, dest)
                        self._fsync_directory()
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
            size, digest = self._file_size_and_sha256(path)
        except OSError:
            LOGGER.warning(
                "raw spill cipherq hash skipped transient io",
                extra={"kind": kind, "reason": REASON_TRANSIENT_IO, "source": source},
            )
            return False
        src_name = self._safe_src_name(path.name)
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
            self._write_json_atomic(manifest_path, document)
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
        if dest.exists() or self._cipherq_wip(dest).exists():
            LOGGER.warning(
                "raw spill cipherq dest exists without overwrite",
                extra={"kind": kind, "reason": reason, "source": source},
            )
            return False
        try:
            os.replace(path, dest)
            self._fsync_directory()
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
            self._write_json_atomic(manifest_path, document)
        except OSError:
            return True
        return True

    def _release_stream_activity_locked(self, source: str, stream_id: str) -> None:
        """隔离密文后释放预留与重写临时文件，不删除 .cq / .quarantine 交接标记。"""

        tmp = self._stream_tmp(source, stream_id)
        tmp.with_name(tmp.name + ".hdr").unlink(missing_ok=True)
        final = self._stream_path(source, stream_id)
        final.with_name(final.name + ".hdr").unlink(missing_ok=True)
        reserve = self._reservation_path(source, stream_id)
        reserve.unlink(missing_ok=True)
        reserve.with_name(reserve.name + ".tmp").unlink(missing_ok=True)

    def _claim_cipherq_locked(self, entry: _CipherqEntry) -> Path | None:
        """原子 .cq → .cq.wip 取得所有权；失败者看不到 dest。"""

        dest = entry.dest
        wip = self._cipherq_wip(dest)
        if wip.exists():
            return None
        if not dest.exists():
            return None
        try:
            os.replace(dest, wip)
            self._fsync_directory()
            return wip
        except OSError:
            return None

    def _restore_cipherq_locked(self, entry: _CipherqEntry) -> Path | None:
        """认领封印密文供原位读取；禁止改回 src_name，避免覆盖活动文件或丢掉 manifest。"""

        return self._claim_cipherq_locked(entry)

    def _iter_cipherq_recoverable(
        self, crypto: StreamChunkCrypto, source: str
    ) -> Iterator[RawSpillRecord]:
        """Key 归还后从封印 .cq 恢复原 Raw；失败则重新隔离，不得丢字节。"""

        if not self.directory.exists():
            return
        for entry in self._iter_cipherq_entries_locked():
            if entry.source != source or entry.state != CIPHERQ_STATE_SEALED:
                continue
            if not entry.dest.exists():
                continue
            with self._quota_lock():
                restored = self._restore_cipherq_locked(entry)
            if restored is None:
                continue
            try:
                if entry.kind == "stream" or restored.name.endswith((".stream", ".tmp")):
                    record = self._read_stream(restored, crypto, expected_source=source)
                else:
                    record = self._read(restored, expected_source=source, crypto=crypto)
            except UnknownKeyVersionError:
                with self._quota_lock():
                    self._isolate_ciphertext_locked(
                        restored,
                        source=entry.source,
                        token=entry.token,
                        kind=entry.kind,
                        reason=REASON_KEY_UNAVAILABLE,
                    )
                continue
            except SpillMetadataAuthError:
                with self._quota_lock():
                    self._isolate_ciphertext_locked(
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
                with self._quota_lock():
                    self._isolate_ciphertext_locked(
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
                record = self._read(claimed, expected_source=source, crypto=crypto)
            except UnknownKeyVersionError:
                with self._quota_lock():
                    self._isolate_ciphertext_locked(
                        claimed,
                        source=entry.source,
                        token=entry.token,
                        kind="spill",
                        reason=REASON_KEY_UNAVAILABLE,
                    )
                continue
            except SpillMetadataAuthError:
                with self._quota_lock():
                    self._isolate_ciphertext_locked(
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

    def _claim_orphan_cipherq_locked(
        self, source: str
    ) -> list[tuple[Path, _CipherqEntry]]:
        claimed: list[tuple[Path, _CipherqEntry]] = []
        with self._quota_lock():
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
        dest, manifest = self._cipherq_paths(source, token)
        dest.unlink(missing_ok=True)
        self._cipherq_wip(dest).unlink(missing_ok=True)
        manifest.unlink(missing_ok=True)
        dest.with_name(dest.name + ".tmp").unlink(missing_ok=True)
        manifest.with_name(manifest.name + ".tmp").unlink(missing_ok=True)

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
