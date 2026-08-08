"""无明文中间文件的分帧 AES-GCM CSV 编解码。"""

from __future__ import annotations

import csv
import io
import os
import re
import struct
from collections.abc import AsyncIterable, Iterator, Sequence
from pathlib import Path
from typing import BinaryIO
from uuid import UUID

from app.services.crypto import (
    BOUND_ENVELOPE_MAGIC,
    NONCE_SIZE,
    TAG_SIZE,
    CryptoService,
    EncryptionContext,
)

LEGACY_MAGIC = b"SMSX1"
MAGIC = b"SMSX2"
VERSION_SIZE = 2
LENGTH_SIZE = 4
FRAME_KIND_SIZE = 1
DEFAULT_FRAME_SIZE = 64 * 1024
DANGEROUS_CSV_RE = re.compile(r"^[\s\t\r\n]*(?:[=+\-@])")
EXPORT_FILENAME = re.compile(
    r"^export-(?P<task_id>[1-9][0-9]*)-"
    r"(?P<lease_id>[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12})[.]smsx$"
)


def _csv_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return "'" + text if DANGEROUS_CSV_RE.match(text) else text


def _csv_line(values: Sequence[object]) -> bytes:
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\r\n").writerow([_csv_cell(value) for value in values])
    return output.getvalue().encode("utf-8")


class ExportFileCodec:
    """写入即密文；读取逐帧认证后才向调用方交付明文字节。"""

    def __init__(
        self,
        crypto: CryptoService,
        root: Path,
        *,
        frame_size: int = DEFAULT_FRAME_SIZE,
        reject_legacy: bool = True,
    ) -> None:
        if frame_size < 16:
            raise ValueError("export frame_size must be at least 16")
        self.crypto = crypto
        self.root = root.resolve()
        self.frame_size = frame_size
        self.reject_legacy = reject_legacy
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    def _paths(self, task_id: int, lease_id: UUID) -> tuple[Path, Path]:
        if task_id < 1:
            raise ValueError("export task id must be positive")
        token = str(lease_id)
        return (
            self.root / f"export-{task_id}-{token}.part",
            self.root / f"export-{task_id}-{token}.smsx",
        )

    @staticmethod
    def _frame_context(
        task_id: int,
        lease_id: UUID,
        frame_index: int,
        kind: str,
    ) -> EncryptionContext:
        return EncryptionContext(
            domain="export-frame",
            table="export_task",
            column="file_ciphertext",
            object_id=f"{task_id}:{lease_id}:{frame_index}:{kind}",
        )

    def _write_frame(
        self,
        output: BinaryIO,
        plaintext: bytes,
        version: int,
        *,
        task_id: int,
        lease_id: UUID,
        frame_index: int,
        kind: str,
    ) -> None:
        encrypted = self.crypto.encrypt_bound_bytes(
            plaintext,
            self._frame_context(task_id, lease_id, frame_index, kind),
        )
        if encrypted.key_version != version:
            raise RuntimeError("export key version changed during write")
        output.write(kind[0].encode("ascii"))
        output.write(struct.pack(">I", len(encrypted.payload)))
        output.write(encrypted.payload)

    async def write_csv(
        self,
        task_id: int,
        lease_id: UUID,
        header: Sequence[object],
        rows: AsyncIterable[Sequence[object]],
    ) -> Path:
        """逐行生成 CSV，仅在有界内存帧内短暂持有明文。"""

        part, final = self._paths(task_id, lease_id)
        part.unlink(missing_ok=True)
        buffer = bytearray(b"\xef\xbb\xbf" + _csv_line(header))
        version = self.crypto.active_version
        frame_index = 0
        try:
            with part.open("xb") as output:
                os.chmod(part, 0o600)
                output.write(MAGIC)
                output.write(version.to_bytes(VERSION_SIZE, "big"))
                async for row in rows:
                    buffer.extend(_csv_line(row))
                    while len(buffer) >= self.frame_size:
                        frame = bytes(buffer[: self.frame_size])
                        del buffer[: self.frame_size]
                        self._write_frame(
                            output,
                            frame,
                            version,
                            task_id=task_id,
                            lease_id=lease_id,
                            frame_index=frame_index,
                            kind="data",
                        )
                        frame_index += 1
                if buffer:
                    self._write_frame(
                        output,
                        bytes(buffer),
                        version,
                        task_id=task_id,
                        lease_id=lease_id,
                        frame_index=frame_index,
                        kind="data",
                    )
                    frame_index += 1
                self._write_frame(
                    output,
                    b"",
                    version,
                    task_id=task_id,
                    lease_id=lease_id,
                    frame_index=frame_index,
                    kind="terminal",
                )
                output.flush()
                os.fsync(output.fileno())
            os.replace(part, final)
            os.chmod(final, 0o600)
            return final
        except BaseException:
            part.unlink(missing_ok=True)
            raise

    def _controlled_path(self, raw_path: str | Path) -> Path:
        path = Path(raw_path).resolve()
        try:
            path.relative_to(self.root)
        except ValueError:
            raise ValueError("export path is outside controlled storage") from None
        if path.suffix != ".smsx":
            raise ValueError("export path is outside controlled storage")
        return path

    def iter_decrypted(self, raw_path: str | Path) -> Iterator[bytes]:
        """校验帧上下文、顺序和终止帧后才 yield；迁移期兼容 SMSX1。"""

        path = self._controlled_path(raw_path)
        match = EXPORT_FILENAME.fullmatch(path.name)
        if match is None:
            raise ValueError("export path is outside controlled storage")
        task_id = int(match.group("task_id"))
        lease_id = UUID(match.group("lease_id"))
        with path.open("rb") as source:
            header = source.read(len(MAGIC) + VERSION_SIZE)
            if len(header) != len(MAGIC) + VERSION_SIZE or not header.startswith(
                (MAGIC, LEGACY_MAGIC)
            ):
                raise ValueError("truncated or invalid export header")
            version = int.from_bytes(header[len(MAGIC) :], "big")
            if header.startswith(LEGACY_MAGIC):
                if self.reject_legacy:
                    raise ValueError("legacy export format rejected")
                yield from self._iter_legacy(source, version)
                return
            frame_index = 0
            while True:
                raw_kind = source.read(FRAME_KIND_SIZE)
                if not raw_kind:
                    raise ValueError("export terminal frame is missing")
                if raw_kind == b"d":
                    kind = "data"
                elif raw_kind == b"t":
                    kind = "terminal"
                else:
                    raise ValueError("invalid export frame kind")
                encoded_length = source.read(LENGTH_SIZE)
                if len(encoded_length) != LENGTH_SIZE:
                    raise ValueError("truncated export frame length")
                length = struct.unpack(">I", encoded_length)[0]
                maximum = self.frame_size + len(BOUND_ENVELOPE_MAGIC) + NONCE_SIZE + TAG_SIZE
                if length < len(BOUND_ENVELOPE_MAGIC) + NONCE_SIZE + TAG_SIZE or length > maximum:
                    raise ValueError("invalid export frame length")
                payload = source.read(length)
                if len(payload) != length:
                    raise ValueError("truncated export frame")
                plaintext = self.crypto.decrypt_bound_bytes(
                    payload,
                    version,
                    self._frame_context(
                        task_id,
                        lease_id,
                        frame_index,
                        kind,
                    ),
                    allow_legacy=False,
                )
                if kind == "terminal":
                    if plaintext or source.read(1):
                        raise ValueError("invalid export terminal frame")
                    return
                yield plaintext
                frame_index += 1

    def _iter_legacy(self, source: BinaryIO, version: int) -> Iterator[bytes]:
        """只读历史 SMSX1；所有新文件均写入带终止认证的 SMSX2。"""

        while True:
            encoded_length = source.read(LENGTH_SIZE)
            if not encoded_length:
                return
            if len(encoded_length) != LENGTH_SIZE:
                raise ValueError("truncated export frame length")
            length = struct.unpack(">I", encoded_length)[0]
            maximum = self.frame_size + NONCE_SIZE + TAG_SIZE
            if length < NONCE_SIZE + TAG_SIZE or length > maximum:
                raise ValueError("invalid export frame length")
            payload = source.read(length)
            if len(payload) != length:
                raise ValueError("truncated export frame")
            yield self.crypto.decrypt_bytes(payload, version)

    def validate(self, raw_path: str | Path) -> Path:
        """在响应头发出前验证路径受控且文件存在。"""

        path = self._controlled_path(raw_path)
        if not path.is_file():
            raise FileNotFoundError("export ciphertext file is unavailable")
        return path

    def remove(self, raw_path: str | Path) -> None:
        self._controlled_path(raw_path).unlink(missing_ok=True)
