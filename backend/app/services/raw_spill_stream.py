"""合并为固定内部帧后写入认证加密流的捕获对象。"""

from __future__ import annotations

import errno
import json
import os
from typing import TYPE_CHECKING

from app.services.crypto import (
    EncryptionContext,
)
from app.services.raw_spill_codec import (
    _control_context,
    _normalize_http_status,
)
from app.services.raw_spill_format import (
    CAPTURE_COMPLETE,
    CAPTURE_COMPLETE_TOO_LARGE,
    CAPTURE_PROTOCOL_INVALID,
    CAPTURE_TRUNCATED,
    DATA_FRAME_OVERHEAD_BYTES,
    INTERNAL_FRAME_SIZE,
    RECOVERY_CAPTURE_BYTES,
    STREAM_CONTROL_SENTINEL,
    STREAM_KIND_ANNOUNCE,
    STREAM_KIND_TERMINAL,
    STREAM_MAGIC,
    STREAM_META_HEADER,
    STREAM_RECORD_HEADER,
    SYNC_EVERY_BYTES,
    SpillReservation,
    StreamChunkCrypto,
    discard_header_only_stream,
    flush_announced_pending_frames,
    max_internal_frames,
)

if TYPE_CHECKING:
    from app.services.raw_spill import RawSpillStore


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
        self.path = store._layout.stream_tmp(source, self.stream_id)
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
        store._layout.fsync_directory()
        self._refresh_ondisk_bytes()

    def __enter__(self) -> RawSpillStream:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        flush_announced_pending_frames(self)
        discard_header_only_stream(self)

    @property
    def is_header_only(self) -> bool:
        """尚未 announce、尚未接受正文、也没有待 flush 缓冲的纯文件头。"""

        return not self._finished and not self._announced and self._seq == 0 and not self._buffer

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
        final = self.store._layout.stream_path(self.source, self.stream_id)
        os.replace(self.path, final)
        self.store._layout.fsync_directory()
        self.path = final
        self._refresh_ondisk_bytes()
        self._finished = True

    def discard(self) -> None:
        header_only = self.is_header_only
        self.store.remove_stream(self.source, self.stream_id)
        if header_only:
            self.store._counters.header_only_cleaned += 1
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
        self.store._layout.fsync_directory()
        self._refresh_ondisk_bytes()
