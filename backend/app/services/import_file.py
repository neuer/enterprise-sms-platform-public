"""导入源文件的分帧 AES-GCM 暂存；磁盘上永不出现号码明文。"""

from __future__ import annotations

import os
import struct
from pathlib import Path
from tempfile import SpooledTemporaryFile
from typing import BinaryIO
from uuid import UUID

from app.services.crypto import NONCE_SIZE, TAG_SIZE, CryptoService

MAGIC = b"SMSI1"
VERSION_SIZE = 2
LENGTH_SIZE = 4
FRAME_SIZE = 64 * 1024


class ImportFileCodec:
    """上传时逐帧加密，worker 解析前只解密到严格有界内存文件。"""

    def __init__(self, crypto: CryptoService, root: Path) -> None:
        self.crypto = crypto
        self.root = root.resolve()

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    def _path(self, import_id: UUID, *, partial: bool = False) -> Path:
        suffix = ".part" if partial else ".smsx"
        return self.root / f"import-{import_id}{suffix}"

    def stage(
        self,
        import_id: UUID,
        source: BinaryIO,
        *,
        size: int,
        max_bytes: int,
    ) -> str:
        """同步分帧加密；调用方必须放入有界执行器。"""

        if size < 0 or size > max_bytes:
            raise ValueError("导入文件超过大小限制")
        self._ensure_root()
        part = self._path(import_id, partial=True)
        final = self._path(import_id)
        part.unlink(missing_ok=True)
        final.unlink(missing_ok=True)
        version = self.crypto.active_version
        written = 0
        source.seek(0)
        try:
            with part.open("xb") as output:
                os.chmod(part, 0o600)
                output.write(MAGIC)
                output.write(version.to_bytes(VERSION_SIZE, "big"))
                while True:
                    frame = source.read(FRAME_SIZE)
                    if not frame:
                        break
                    written += len(frame)
                    if written > max_bytes:
                        raise ValueError("导入文件超过大小限制")
                    encrypted = self.crypto.encrypt_bytes(frame)
                    if encrypted.key_version != version:
                        raise RuntimeError("导入文件加密版本在写入期间变化")
                    output.write(struct.pack(">I", len(encrypted.payload)))
                    output.write(encrypted.payload)
                if written != size:
                    raise ValueError("上传文件大小在登记期间发生变化")
                output.flush()
                os.fsync(output.fileno())
            os.replace(part, final)
            os.chmod(final, 0o600)
            directory_fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return final.name
        except BaseException:
            part.unlink(missing_ok=True)
            raise

    def _controlled(self, relative: str) -> Path:
        if Path(relative).name != relative or not relative.endswith(".smsx"):
            raise ValueError("导入源文件路径不受控")
        path = (self.root / relative).resolve()
        if path.parent != self.root:
            raise ValueError("导入源文件路径不受控")
        return path

    def decrypt_to_memory(
        self,
        relative: str,
        *,
        expected_size: int,
        max_bytes: int,
    ) -> SpooledTemporaryFile[bytes]:
        """逐帧认证并解密到不会落盘的有界内存文件。"""

        if expected_size < 0 or expected_size > max_bytes:
            raise ValueError("导入源文件大小无效")
        # 返回给调用方消费并关闭，不能在本函数退出时使用上下文管理器。
        temporary = SpooledTemporaryFile(  # noqa: SIM115
            max_size=max_bytes + 1,
            mode="w+b",
        )
        total = 0
        try:
            with self._controlled(relative).open("rb") as source:
                header = source.read(len(MAGIC) + VERSION_SIZE)
                if len(header) != len(MAGIC) + VERSION_SIZE or not header.startswith(
                    MAGIC
                ):
                    raise ValueError("导入源文件密文头无效")
                version = int.from_bytes(header[len(MAGIC) :], "big")
                while True:
                    encoded_length = source.read(LENGTH_SIZE)
                    if not encoded_length:
                        break
                    if len(encoded_length) != LENGTH_SIZE:
                        raise ValueError("导入源文件密文长度截断")
                    length = struct.unpack(">I", encoded_length)[0]
                    maximum = FRAME_SIZE + NONCE_SIZE + TAG_SIZE
                    if length < NONCE_SIZE + TAG_SIZE or length > maximum:
                        raise ValueError("导入源文件密文帧长度无效")
                    payload = source.read(length)
                    if len(payload) != length:
                        raise ValueError("导入源文件密文帧截断")
                    plaintext = self.crypto.decrypt_bytes(payload, version)
                    total += len(plaintext)
                    if total > max_bytes:
                        raise ValueError("导入源文件解密后超过限制")
                    temporary.write(plaintext)
            if total != expected_size:
                raise ValueError("导入源文件解密后大小不一致")
            temporary.seek(0)
            return temporary
        except BaseException:
            temporary.close()
            raise

    def remove(self, relative: str) -> None:
        self._controlled(relative).unlink(missing_ok=True)
