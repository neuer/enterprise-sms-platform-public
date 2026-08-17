"""导入源文件的分帧 AES-GCM 暂存；磁盘上永不出现号码明文。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import struct
from pathlib import Path
from tempfile import SpooledTemporaryFile
from typing import BinaryIO
from uuid import UUID

from cryptography.exceptions import InvalidTag

from app.services.crypto import (
    BOUND_ENVELOPE_MAGIC,
    NONCE_SIZE,
    TAG_SIZE,
    CryptoService,
    EncryptionContext,
)

LOGGER = logging.getLogger(__name__)
MAGIC_V1 = b"SMSI1"
MAGIC_V2 = b"SMSI2"
VERSION_SIZE = 2
LENGTH_SIZE = 4
IMPORT_ID_SIZE = 16
SIZE_SIZE = 8
FRAME_SIZE = 64 * 1024
IMPORT_CONTEXT_DOMAIN = "import-file"
IMPORT_CONTEXT_TABLE = "import_task"
IMPORT_CONTEXT_COLUMN = "import_phone_stage"


class ImportFileCodec:
    """上传时逐帧加密，worker 解析前只解密到严格有界内存文件。"""

    def __init__(
        self,
        crypto: CryptoService,
        root: Path,
        *,
        reject_legacy: bool = True,
    ) -> None:
        self.crypto = crypto
        self.root = root.resolve()
        self.reject_legacy = reject_legacy

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
                output.write(MAGIC_V2)
                output.write(version.to_bytes(VERSION_SIZE, "big"))
                output.write(import_id.bytes)
                output.write(size.to_bytes(SIZE_SIZE, "big"))
                frame_index = 0
                digest = hashlib.sha256()
                while True:
                    frame = source.read(FRAME_SIZE)
                    if not frame:
                        break
                    written += len(frame)
                    if written > max_bytes:
                        raise ValueError("导入文件超过大小限制")
                    digest.update(frame)
                    encrypted = self.crypto.encrypt_bound_bytes(
                        frame,
                        self._frame_context(import_id, frame_index, "data", size),
                    )
                    if encrypted.key_version != version:
                        raise RuntimeError("导入文件加密版本在写入期间变化")
                    output.write(struct.pack(">I", len(encrypted.payload)))
                    output.write(encrypted.payload)
                    frame_index += 1
                if written != size:
                    raise ValueError("上传文件大小在登记期间发生变化")
                manifest = json.dumps(
                    {
                        "import_id": str(import_id),
                        "frame_count": frame_index + 1,
                        "plaintext_size": size,
                        "sha256": digest.hexdigest(),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                terminal = self.crypto.encrypt_bound_bytes(
                    manifest,
                    self._frame_context(
                        import_id,
                        frame_index,
                        "terminal",
                        size,
                    ),
                )
                if terminal.key_version != version:
                    raise RuntimeError("导入文件加密版本在写入期间变化")
                output.write(struct.pack(">I", len(terminal.payload)))
                output.write(terminal.payload)
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

    @staticmethod
    def _frame_context(
        import_id: UUID,
        frame_index: int,
        frame_type: str,
        expected_size: int,
    ) -> EncryptionContext:
        return EncryptionContext(
            domain=IMPORT_CONTEXT_DOMAIN,
            table=IMPORT_CONTEXT_TABLE,
            column=IMPORT_CONTEXT_COLUMN,
            object_id=f"{import_id}:{frame_index}:{frame_type}:{expected_size}",
        )

    def _decrypt_v2_to_memory(
        self,
        source: BinaryIO,
        *,
        version: int,
        import_id: UUID,
        expected_import_id: UUID,
        expected_size: int,
        max_bytes: int,
        temporary: SpooledTemporaryFile[bytes],
    ) -> int:
        """读取 SMSI2：逐帧上下文认证、严格连续序号与认证最终 manifest。"""

        if import_id != expected_import_id:
            raise ValueError("导入源文件身份与任务不一致")
        total = 0
        digest = hashlib.sha256()
        frame_index = 0
        manifest_seen = False
        while True:
            encoded_length = source.read(LENGTH_SIZE)
            if not encoded_length:
                break
            if len(encoded_length) != LENGTH_SIZE:
                raise ValueError("导入源文件密文长度截断")
            length = struct.unpack(">I", encoded_length)[0]
            maximum = FRAME_SIZE + len(BOUND_ENVELOPE_MAGIC) + NONCE_SIZE + TAG_SIZE
            if length < len(BOUND_ENVELOPE_MAGIC) + NONCE_SIZE + TAG_SIZE or length > maximum:
                raise ValueError("导入源文件密文帧长度无效")
            payload = source.read(length)
            if len(payload) != length:
                raise ValueError("导入源文件密文帧截断")
            plaintext: bytes | None = None
            try:
                plaintext = self.crypto.decrypt_bound_bytes(
                    payload,
                    version,
                    self._frame_context(import_id, frame_index, "data", expected_size),
                    allow_legacy=False,
                )
                frame_type = "data"
            except (ValueError, InvalidTag):
                pass
            if plaintext is None:
                try:
                    plaintext = self.crypto.decrypt_bound_bytes(
                        payload,
                        version,
                        self._frame_context(
                            import_id,
                            frame_index,
                            "terminal",
                            expected_size,
                        ),
                        allow_legacy=False,
                    )
                    frame_type = "terminal"
                except (ValueError, InvalidTag):
                    raise ValueError("导入源文件密文帧认证失败") from None
            if frame_type == "data":
                if manifest_seen:
                    raise ValueError("导入源文件在终止帧后仍有数据帧")
                total += len(plaintext)
                if total > max_bytes:
                    raise ValueError("导入源文件解密后超过限制")
                digest.update(plaintext)
                temporary.write(plaintext)
            else:
                if manifest_seen:
                    raise ValueError("导入源文件存在多个终止帧")
                manifest_seen = True
                try:
                    manifest = json.loads(plaintext.decode("utf-8"))
                except (UnicodeError, ValueError):
                    raise ValueError("导入源文件终止清单无效") from None
                if (
                    not isinstance(manifest, dict)
                    or manifest.get("import_id") != str(expected_import_id)
                    or manifest.get("frame_count") != frame_index + 1
                    or manifest.get("plaintext_size") != expected_size
                    or manifest.get("sha256") != digest.hexdigest()
                ):
                    raise ValueError("导入源文件终止清单与内容不一致")
                if source.read(1):
                    raise ValueError("导入源文件在终止帧后仍有数据")
                break
            frame_index += 1
        if not manifest_seen:
            raise ValueError("导入源文件缺少终止帧")
        if total != expected_size:
            raise ValueError("导入源文件解密后大小不一致")
        return total

    def decrypt_to_memory(
        self,
        relative: str,
        *,
        expected_import_id: UUID,
        expected_size: int,
        max_bytes: int,
    ) -> SpooledTemporaryFile[bytes]:
        """逐帧认证并解密到不会落盘的有界内存文件。"""

        if expected_size < 0 or expected_size > max_bytes:
            raise ValueError("导入源文件大小无效")
        if relative != f"import-{expected_import_id}.smsx":
            raise ValueError("导入源文件名与任务身份不一致")
        # 返回给调用方消费并关闭，不能在本函数退出时使用上下文管理器。
        temporary = SpooledTemporaryFile(  # noqa: SIM115
            max_size=max_bytes + 1,
            mode="w+b",
        )
        total = 0
        try:
            with self._controlled(relative).open("rb") as source:
                magic = source.read(len(MAGIC_V1))
                if magic == MAGIC_V1:
                    if self.reject_legacy:
                        raise ValueError("legacy import format rejected")
                    version_raw = source.read(VERSION_SIZE)
                    if len(version_raw) != VERSION_SIZE:
                        raise ValueError("导入源文件密文头无效")
                    version = int.from_bytes(version_raw, "big")
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
                        plaintext = self.crypto.decrypt_bytes_legacy(payload, version)
                        total += len(plaintext)
                        if total > max_bytes:
                            raise ValueError("导入源文件解密后超过限制")
                        temporary.write(plaintext)
                    LOGGER.warning("SMSI1 legacy import file read import_id=unknown")
                elif magic == MAGIC_V2:
                    version_raw = source.read(VERSION_SIZE)
                    raw_import_id = source.read(IMPORT_ID_SIZE)
                    raw_size = source.read(SIZE_SIZE)
                    if (
                        len(version_raw) != VERSION_SIZE
                        or len(raw_import_id) != IMPORT_ID_SIZE
                        or len(raw_size) != SIZE_SIZE
                    ):
                        raise ValueError("导入源文件密文头无效")
                    try:
                        import_id = UUID(bytes=raw_import_id)
                    except ValueError:
                        raise ValueError("导入源文件密文头无效") from None
                    declared_size = int.from_bytes(raw_size, "big")
                    if declared_size != expected_size:
                        raise ValueError("导入源文件声明大小不一致")
                    total = self._decrypt_v2_to_memory(
                        source,
                        version=int.from_bytes(version_raw, "big"),
                        import_id=import_id,
                        expected_import_id=expected_import_id,
                        expected_size=expected_size,
                        max_bytes=max_bytes,
                        temporary=temporary,
                    )
                else:
                    raise ValueError("导入源文件密文头无效")
            if total != expected_size:
                raise ValueError("导入源文件解密后大小不一致")
            temporary.seek(0)
            return temporary
        except BaseException:
            temporary.close()
            raise

    def remove(self, relative: str) -> None:
        self._controlled(relative).unlink(missing_ok=True)
