"""厂商拉走即消费响应的本地加密 spill，供落库前崩溃恢复。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.services.crypto import EncryptedValue, EncryptionContext

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
VALID_CAPTURE_STATES = frozenset(
    {
        CAPTURE_COMPLETE,
        CAPTURE_COMPLETE_TOO_LARGE,
        CAPTURE_TRUNCATED,
        CAPTURE_PROTOCOL_INVALID,
    }
)
NON_REPLAYABLE_CAPTURE_STATES = frozenset({CAPTURE_TRUNCATED, CAPTURE_PROTOCOL_INVALID})
DEFAULT_MAX_TOTAL_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_PENDING_FILES = 32
SYNC_EVERY_BYTES = 1024 * 1024
QUOTA_RESERVATION_BYTES = 65_536


class SpillQuotaExceeded(RuntimeError):
    """spill 目录已达文件数或总字节上限，必须停止继续拉取。"""


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


def normalize_capture_state(value: str | None) -> str:
    state = value if value else CAPTURE_COMPLETE
    if state not in VALID_CAPTURE_STATES:
        raise ValueError("invalid capture_state")
    return state


def is_non_replayable_capture(state: str | None) -> bool:
    """截断或协议异常 raw 不得进入普通自动/人工重放。"""

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


@dataclass(frozen=True, slots=True)
class _StreamFileHeader:
    source: str
    stream_id: str
    key_version: int
    body: bytes
    path: Path


@dataclass(frozen=True, slots=True)
class _AssembledStream:
    chunks: list[bytes]
    announce: dict[str, object] | None
    terminal: dict[str, object] | None
    incomplete: bool

    @property
    def empty(self) -> bool:
        return not self.chunks and self.announce is None and self.terminal is None


class RawSpillStore:
    """只保存 AES-GCM 密文；明文手机号不得进入 spill 文件。"""

    def __init__(
        self,
        directory: Path,
        *,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
        max_pending_files: int = DEFAULT_MAX_PENDING_FILES,
    ) -> None:
        if max_total_bytes < 1 or max_pending_files < 1:
            raise ValueError("raw spill quotas must be positive")
        self.directory = directory
        self.max_total_bytes = max_total_bytes
        self.max_pending_files = max_pending_files

    @classmethod
    def from_settings(cls, settings: RawSpillSettings) -> RawSpillStore:
        return cls(
            settings.raw_spill_dir,
            max_total_bytes=int(settings.raw_spill_max_total_bytes),
            max_pending_files=int(settings.raw_spill_max_pending_files),
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

    def usage_bytes(self) -> int:
        if not self.directory.exists():
            return 0
        return sum(path.stat().st_size for path in self.directory.iterdir() if path.is_file())

    def pending_count(self) -> int:
        if not self.directory.exists():
            return 0
        return sum(
            1
            for path in self.directory.iterdir()
            if path.is_file() and path.suffix in {".spill", ".stream", ".tmp", ".quarantine"}
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
        return (
            self.pending_count() + additional_files <= self.max_pending_files
            and self.usage_bytes() + additional_bytes <= self.max_total_bytes
        )

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
        # 在途 .stream 与即将落盘的 .spill 是同一捕获的两份密文，不得互相占满文件配额。
        if (
            self.pending_spill_count() >= self.max_pending_files
            or self.usage_bytes() + len(payload_enc) + 256 > self.max_total_bytes
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

    def list_pending(self) -> list[RawSpillRecord]:
        if not self.directory.exists():
            return []
        records: list[RawSpillRecord] = []
        for path in sorted(self.directory.glob("*.spill")):
            record = self._read(path)
            if record is not None:
                records.append(record)
        return records

    def list_pending_streams(self, crypto: StreamChunkCrypto) -> list[RawSpillRecord]:
        """把接收中崩溃留下的加密流装配为可落库的截断/完整 spill 记录。"""

        if not self.directory.exists():
            return []
        records: list[RawSpillRecord] = []
        for path in sorted(
            (*self.directory.glob("*.stream"), *self.directory.glob("*.stream.tmp"))
        ):
            record = self._read_stream(path, crypto)
            if record is not None:
                records.append(record)
        return records

    def remove(self, source: str, payload_sha256: str) -> None:
        path = self._path(source, payload_sha256)
        path.unlink(missing_ok=True)

    def remove_stream(self, source: str, stream_id: str) -> None:
        self._stream_tmp(source, stream_id).unlink(missing_ok=True)
        self._stream_path(source, stream_id).unlink(missing_ok=True)
        self._quarantine_path(source, stream_id).unlink(missing_ok=True)

    def open_stream(self, source: str, crypto: StreamChunkCrypto) -> RawSpillStream:
        if not self.can_accept(QUOTA_RESERVATION_BYTES):
            raise SpillQuotaExceeded("raw spill quota exceeded")
        return RawSpillStream(self, source, crypto)

    def _read(self, path: Path) -> RawSpillRecord | None:
        try:
            raw = path.read_bytes()
            header_bytes, payload_enc = raw.split(b"\n", maxsplit=1)
            header = json.loads(header_bytes.decode("utf-8"))
            source = str(header["source"])
            payload_sha256 = str(header["payload_sha256"])
            if self._path(source, payload_sha256) != path:
                return None
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
            )
        except (OSError, KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            return None

    def _parse_stream_file_header(self, path: Path) -> _StreamFileHeader | None:
        try:
            raw = path.read_bytes()
            if not raw.startswith(STREAM_MAGIC):
                return None
            rest = raw[len(STREAM_MAGIC) :]
            header_bytes, body = rest.split(b"\n", maxsplit=1)
            header = json.loads(header_bytes.decode("utf-8"))
            source = str(header["source"])
            stream_id = str(header["stream_id"])
            key_version = int(header["key_version"])
            expected_tmp = self._stream_tmp(source, stream_id)
            expected_final = self._stream_path(source, stream_id)
            if path not in {expected_tmp, expected_final}:
                return None
            return _StreamFileHeader(source, stream_id, key_version, body, path)
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

    def _assemble_stream_records(
        self,
        header: _StreamFileHeader,
        crypto: StreamChunkCrypto,
    ) -> _AssembledStream:
        chunks: list[bytes] = []
        announce: dict[str, object] | None = None
        terminal: dict[str, object] | None = None
        incomplete = False
        offset = 0
        seq = 0
        body = header.body
        while offset < len(body):
            if offset + STREAM_RECORD_HEADER.size > len(body):
                incomplete = True
                break
            (length,) = STREAM_RECORD_HEADER.unpack_from(body, offset)
            offset += STREAM_RECORD_HEADER.size
            if length == STREAM_CONTROL_SENTINEL:
                fields, offset, ok = self._read_control_frame(
                    body,
                    offset,
                    crypto,
                    source=header.source,
                    stream_id=header.stream_id,
                    key_version=header.key_version,
                    seq=0,
                )
                if not ok or fields is None or fields.get("kind") != STREAM_KIND_ANNOUNCE:
                    incomplete = True
                    break
                announce = fields
                continue
            if length == 0:
                fields, _ignored, ok = self._read_control_frame(
                    body,
                    offset,
                    crypto,
                    source=header.source,
                    stream_id=header.stream_id,
                    key_version=header.key_version,
                    seq=seq,
                )
                if ok and fields is not None and fields.get("kind") == STREAM_KIND_TERMINAL:
                    terminal = fields
                else:
                    legacy = self._parse_legacy_footer(body[offset:])
                    if legacy is None:
                        incomplete = True
                    else:
                        terminal = legacy
                break
            ciphertext = body[offset : offset + length]
            if len(ciphertext) != length:
                incomplete = True
                break
            offset += length
            try:
                chunks.append(
                    crypto.decrypt_bound_bytes(
                        ciphertext,
                        header.key_version,
                        EncryptionContext(
                            domain="vendor-raw",
                            table="raw_spill",
                            column="chunk",
                            object_id=f"{header.source}:{header.stream_id}:{seq:08d}",
                        ),
                    )
                )
            except (ValueError, TypeError):
                incomplete = True
                break
            seq += 1
        return _AssembledStream(chunks, announce, terminal, incomplete)

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

    def _read_stream(self, path: Path, crypto: StreamChunkCrypto) -> RawSpillRecord | None:
        header = self._parse_stream_file_header(path)
        if header is None:
            return None
        assembled = self._assemble_stream_records(header, crypto)
        if assembled.empty:
            return None
        plaintext = b"".join(assembled.chunks)
        try:
            payload_sha256 = hashlib.sha256(plaintext).hexdigest()
            encrypted = crypto.encrypt_bound_bytes(
                plaintext,
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
        )

    def _fsync_directory(self) -> None:
        directory_fd = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


class RawSpillStream:
    """按 chunk 写入认证加密流；接收中崩溃只留下密文，不得落明文。"""

    def __init__(
        self,
        store: RawSpillStore,
        source: str,
        crypto: StreamChunkCrypto,
    ) -> None:
        self.store = store
        self.source = source
        self.crypto = crypto
        self.stream_id = secrets.token_hex(16)
        self._seq = 0
        self._unsynced = 0
        self._finished = False
        self._key_version: int | None = None
        self._http_status = 200
        self._content_encoding = "identity"
        self._protocol_invalid = False
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

    def feed(self, chunk: bytes) -> bool:
        """追加一个认证加密 chunk。配额不足时返回 False，由调用方完整性终止。"""

        if self._finished or not chunk:
            return not self._finished
        overhead = STREAM_RECORD_HEADER.size + 48
        if not self.store.can_accept(len(chunk) + overhead, additional_files=0):
            return False
        encrypted = self.crypto.encrypt_bound_bytes(
            chunk,
            EncryptionContext(
                domain="vendor-raw",
                table="raw_spill",
                column="chunk",
                object_id=f"{self.source}:{self.stream_id}:{self._seq:08d}",
            ),
        )
        self._bind_key_version(encrypted.key_version)
        with self.path.open("ab") as handle:
            handle.write(STREAM_RECORD_HEADER.pack(len(encrypted.payload)))
            handle.write(encrypted.payload)
            handle.flush()
            self._unsynced += STREAM_RECORD_HEADER.size + len(encrypted.payload)
            if self._unsynced >= SYNC_EVERY_BYTES:
                os.fsync(handle.fileno())
                self._unsynced = 0
        self._seq += 1
        return True

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
        if http_status is not None:
            self._http_status = _normalize_http_status(http_status)
        if content_encoding is not None:
            self._content_encoding = content_encoding or "identity"
        if protocol_invalid:
            self._protocol_invalid = True
        if self._protocol_invalid:
            capture_state = CAPTURE_PROTOCOL_INVALID
        elif complete and too_large:
            capture_state = CAPTURE_COMPLETE_TOO_LARGE
        elif complete:
            capture_state = CAPTURE_COMPLETE
        else:
            capture_state = CAPTURE_TRUNCATED
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
        self._finished = True

    def discard(self) -> None:
        self.store.remove_stream(self.source, self.stream_id)
        self._finished = True

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
