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
CAPTURE_COMPLETE = "complete"
CAPTURE_COMPLETE_TOO_LARGE = "complete_too_large"
CAPTURE_TRUNCATED = "truncated"
CAPTURE_UNKNOWN_LEGACY = "unknown_legacy"
VALID_CAPTURE_STATES = frozenset(
    {
        CAPTURE_COMPLETE,
        CAPTURE_COMPLETE_TOO_LARGE,
        CAPTURE_TRUNCATED,
        CAPTURE_UNKNOWN_LEGACY,
    }
)
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
            if path.is_file() and path.suffix in {".spill", ".stream", ".tmp"}
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

    def _read_stream(self, path: Path, crypto: StreamChunkCrypto) -> RawSpillRecord | None:
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
            chunks: list[bytes] = []
            offset = 0
            seq = 0
            footer: dict[str, object] | None = None
            while offset < len(body):
                if offset + STREAM_RECORD_HEADER.size > len(body):
                    break
                (length,) = STREAM_RECORD_HEADER.unpack_from(body, offset)
                offset += STREAM_RECORD_HEADER.size
                if length == 0:
                    footer_bytes = body[offset:]
                    footer_line = footer_bytes.split(b"\n", maxsplit=1)[0]
                    footer = json.loads(footer_line.decode("utf-8"))
                    break
                ciphertext = body[offset : offset + length]
                if len(ciphertext) != length:
                    break
                offset += length
                chunks.append(
                    crypto.decrypt_bound_bytes(
                        ciphertext,
                        key_version,
                        EncryptionContext(
                            domain="vendor-raw",
                            table="raw_spill",
                            column="chunk",
                            object_id=f"{source}:{stream_id}:{seq:08d}",
                        ),
                    )
                )
                seq += 1
            if not chunks:
                return None
            plaintext = b"".join(chunks)
            payload_sha256 = hashlib.sha256(plaintext).hexdigest()
            encrypted = crypto.encrypt_bound_bytes(
                plaintext,
                EncryptionContext(
                    domain="vendor-raw",
                    table="raw_vendor_log",
                    column="payload_enc",
                    object_id=f"{source}:{payload_sha256}",
                ),
            )
            if footer is None:
                capture_state = CAPTURE_TRUNCATED
                http_status = 200
                content_encoding = "identity"
            else:
                capture_state = normalize_capture_state(str(footer.get("capture_state")))
                status_value = footer.get("http_status", 200)
                http_status = int(status_value) if isinstance(status_value, (int, str)) else 200
                content_encoding = str(footer.get("content_encoding") or "identity")
            return RawSpillRecord(
                source=source,
                payload_sha256=payload_sha256,
                key_version=encrypted.key_version,
                http_status=http_status if 100 <= http_status <= 599 else 200,
                content_encoding=content_encoding,
                payload_enc=encrypted.payload,
                path=path,
                capture_state=capture_state,
            )
        except (OSError, KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            return None

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
        if self._key_version is None:
            self._rewrite_header_key_version(encrypted.key_version)
            self._key_version = encrypted.key_version
        elif encrypted.key_version != self._key_version:
            raise ValueError("raw spill stream key version changed")
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

    def finish(
        self,
        *,
        complete: bool,
        http_status: int = 200,
        content_encoding: str = "identity",
        too_large: bool = False,
    ) -> None:
        """写入完整性页脚并原子晋升为 .stream；截断不得伪造成完整捕获。"""

        if self._finished:
            return
        if complete and too_large:
            capture_state = CAPTURE_COMPLETE_TOO_LARGE
        elif complete:
            capture_state = CAPTURE_COMPLETE
        else:
            capture_state = CAPTURE_TRUNCATED
        footer = json.dumps(
            {
                "capture_state": capture_state,
                "content_encoding": content_encoding,
                "http_status": http_status if 100 <= http_status <= 599 else 200,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        with self.path.open("ab") as handle:
            handle.write(STREAM_RECORD_HEADER.pack(0))
            handle.write(footer)
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        final = self.store._stream_path(self.source, self.stream_id)
        os.replace(self.path, final)
        self.store._fsync_directory()
        self.path = final
        self._finished = True

    def discard(self) -> None:
        self.store.remove_stream(self.source, self.stream_id)
        self._finished = True

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
