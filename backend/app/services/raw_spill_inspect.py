"""spill 目录中 .stream 文件的轻量分类：只读 header 与首帧，回收路径不得 OOM。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from cryptography.exceptions import InvalidTag

from app.services.crypto import EncryptionContext, UnknownKeyVersionError
from app.services.raw_spill_codec import (
    _crypto_has_version,
    read_control_frame,
)
from app.services.raw_spill_format import (
    MAX_CLASSIFY_CONTROL_BYTES,
    MAX_CLASSIFY_DATA_CIPHER_BYTES,
    STREAM_CONTROL_SENTINEL,
    STREAM_FILE_NAME,
    STREAM_LIFE_AUTH_FAILED,
    STREAM_LIFE_CORRUPT_HEADER,
    STREAM_LIFE_HAS_AUTHENTICATED_DATA,
    STREAM_LIFE_HAS_CONTROL,
    STREAM_LIFE_KEY_UNAVAILABLE,
    STREAM_LIFE_LEGAL_HEADER_ONLY,
    STREAM_LIFE_PARTIAL_HEADER,
    STREAM_LIFE_TRANSIENT_IO,
    STREAM_LIFE_UNAUTHENTICATED_PARTIAL,
    STREAM_MAGIC,
    STREAM_META_HEADER,
    STREAM_RECORD_HEADER,
    StreamChunkCrypto,
)
from app.services.raw_spill_layout import (
    SpillLayout,
)


@dataclass(frozen=True, slots=True)
class _StreamInspection:
    kind: str
    source: str
    stream_id: str
    age_seconds: float
    path: Path


class StreamInspector:
    """按最小 header / 首帧给 .stream 分类；不解密全文、不装载 payload，供回收与巡检。"""

    def __init__(self, layout: SpillLayout) -> None:
        self._layout = layout

    def iter_stream_paths(self) -> list[Path]:
        if not self._layout.directory.exists():
            return []
        return [
            *sorted(self._layout.directory.glob("*.stream")),
            *sorted(self._layout.directory.glob("*.stream.tmp")),
        ]

    def inspect_stream_path(
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
        age = self._layout.file_age_seconds(path, now_ts)
        try:
            handle = path.open("rb")
        except OSError:
            return _StreamInspection(STREAM_LIFE_TRANSIENT_IO, named_source, named_id, age, path)
        try:
            return self.classify_stream_header(
                handle,
                named_source=named_source,
                named_id=named_id,
                age=age,
                path=path,
                crypto=crypto,
            )
        except OSError:
            return _StreamInspection(STREAM_LIFE_TRANSIENT_IO, named_source, named_id, age, path)
        finally:
            handle.close()

    def classify_stream_header(
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
                self._layout.stream_tmp(source, stream_id) != path
                and self._layout.stream_path(source, stream_id) != path
            ):
                return _StreamInspection(
                    STREAM_LIFE_CORRUPT_HEADER, named_source, named_id, age, path
                )
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            return _StreamInspection(STREAM_LIFE_CORRUPT_HEADER, named_source, named_id, age, path)
        first = handle.read(STREAM_RECORD_HEADER.size)
        if not first:
            return _StreamInspection(STREAM_LIFE_LEGAL_HEADER_ONLY, source, stream_id, age, path)
        if crypto is None:
            return None
        if len(first) < STREAM_RECORD_HEADER.size:
            return _StreamInspection(
                STREAM_LIFE_UNAUTHENTICATED_PARTIAL, source, stream_id, age, path
            )
        (length,) = STREAM_RECORD_HEADER.unpack(first)
        if length in {STREAM_CONTROL_SENTINEL, 0}:
            return self.classify_first_control(
                handle,
                source=source,
                stream_id=stream_id,
                key_version=key_version,
                age=age,
                path=path,
                crypto=crypto,
            )
        return self.classify_first_data(
            handle,
            source=source,
            stream_id=stream_id,
            key_version=key_version,
            age=age,
            path=path,
            crypto=crypto,
            length=length,
        )

    def classify_first_control(
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
            return _StreamInspection(STREAM_LIFE_KEY_UNAVAILABLE, source, stream_id, age, path)
        try:
            _fields, _offset, ok = read_control_frame(
                STREAM_RECORD_HEADER.pack(frame_len) + frame,
                0,
                crypto,
                source=source,
                stream_id=stream_id,
                key_version=key_version,
                seq=0,
            )
        except UnknownKeyVersionError:
            return _StreamInspection(STREAM_LIFE_KEY_UNAVAILABLE, source, stream_id, age, path)
        except InvalidTag:
            return _StreamInspection(STREAM_LIFE_AUTH_FAILED, source, stream_id, age, path)
        except OSError:
            return _StreamInspection(STREAM_LIFE_TRANSIENT_IO, source, stream_id, age, path)
        if ok:
            return _StreamInspection(STREAM_LIFE_HAS_CONTROL, source, stream_id, age, path)
        return _StreamInspection(STREAM_LIFE_UNAUTHENTICATED_PARTIAL, source, stream_id, age, path)

    def classify_first_data(
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
            return _StreamInspection(STREAM_LIFE_KEY_UNAVAILABLE, source, stream_id, age, path)
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
            return _StreamInspection(STREAM_LIFE_KEY_UNAVAILABLE, source, stream_id, age, path)
        except InvalidTag:
            return _StreamInspection(STREAM_LIFE_AUTH_FAILED, source, stream_id, age, path)
        except OSError:
            return _StreamInspection(STREAM_LIFE_TRANSIENT_IO, source, stream_id, age, path)
        except (ValueError, TypeError):
            return _StreamInspection(
                STREAM_LIFE_UNAUTHENTICATED_PARTIAL, source, stream_id, age, path
            )
        return _StreamInspection(STREAM_LIFE_HAS_AUTHENTICATED_DATA, source, stream_id, age, path)
