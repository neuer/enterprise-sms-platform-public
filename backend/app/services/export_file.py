"""无明文中间文件的分帧 AES-GCM CSV 编解码。"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import struct
from collections.abc import AsyncIterable, Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
from app.services.file_durability import fsync_directory

LEGACY_MAGIC = b"SMSX1"
MAGIC = b"SMSX2"
FORMAT_VERSION = 2
VERSION_SIZE = 2
LENGTH_SIZE = 4
FRAME_KIND_SIZE = 1
DEFAULT_FRAME_SIZE = 64 * 1024
MAX_TERMINAL_PLAINTEXT = 1024
MANIFEST_STATE_COMPLETE = "complete"
SHANGHAI = timezone(timedelta(hours=8))
DANGEROUS_CSV_RE = re.compile(r"^[\s\t\r\n]*(?:[=+\-@])")
EXPORT_FILENAME = re.compile(
    r"^export-(?P<task_id>[1-9][0-9]*)-"
    r"(?P<lease_id>[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12})[.]smsx$"
)
EXPORT_ARTIFACT_NAME = re.compile(
    r"^export-(?P<task_id>[1-9][0-9]*)-"
    r"(?P<lease_id>[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12})(?P<suffix>[.](?:part|smsx))$"
)
MANIFEST_KEYS = frozenset(
    {
        "created_at",
        "ciphertext_sha256",
        "ciphertext_size",
        "format_version",
        "lease_id",
        "row_count",
        "state",
        "task_id",
    }
)
QUARANTINE_DIR_NAME = "quarantine"


class ExportWriteInterrupted(BaseException):
    """模拟进程在写入边界被杀死；不走 Exception 清理。"""

    def __init__(self, stage: str) -> None:
        super().__init__(stage)
        self.stage = stage


@dataclass(frozen=True, slots=True)
class ExportReadyFile:
    path: Path
    task_id: int
    lease_id: UUID
    row_count: int | None
    format_version: int
    ciphertext_sha256: str | None
    ciphertext_size: int | None
    state: str
    created_at: datetime | None


@dataclass(frozen=True, slots=True)
class ExportArtifact:
    path: Path
    task_id: int
    lease_id: UUID
    suffix: str


class _CiphertextDigest:
    def __init__(self) -> None:
        self._digest = hashlib.sha256()
        self.size = 0

    def update(self, data: bytes) -> None:
        self._digest.update(data)
        self.size += len(data)

    def hexdigest(self) -> str:
        return self._digest.hexdigest()


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
        on_write_stage: Callable[[str], None] | None = None,
    ) -> None:
        if frame_size < 16:
            raise ValueError("export frame_size must be at least 16")
        self.crypto = crypto
        self.root = root.resolve()
        self.frame_size = frame_size
        self.reject_legacy = reject_legacy
        self.on_write_stage = on_write_stage
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

    def _stage(self, name: str) -> None:
        if self.on_write_stage is not None:
            self.on_write_stage(name)

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
    ) -> bytes:
        encrypted = self.crypto.encrypt_bound_bytes(
            plaintext,
            self._frame_context(task_id, lease_id, frame_index, kind),
        )
        if encrypted.key_version != version:
            raise RuntimeError("export key version changed during write")
        if kind == "terminal" and len(plaintext) > MAX_TERMINAL_PLAINTEXT:
            raise ValueError("export staging manifest exceeds terminal limit")
        raw = (
            kind[0].encode("ascii")
            + struct.pack(">I", len(encrypted.payload))
            + encrypted.payload
        )
        output.write(raw)
        return raw

    def _staging_manifest(
        self,
        *,
        task_id: int,
        lease_id: UUID,
        digest: _CiphertextDigest,
        row_count: int,
    ) -> bytes:
        """绑定 task/lease/格式版本与密文摘要的终止清单；不含手机号或正文。"""

        payload = {
            "created_at": datetime.now(SHANGHAI).isoformat(),
            "ciphertext_sha256": digest.hexdigest(),
            "ciphertext_size": digest.size,
            "format_version": FORMAT_VERSION,
            "lease_id": str(lease_id),
            "row_count": row_count,
            "state": MANIFEST_STATE_COMPLETE,
            "task_id": task_id,
        }
        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def _accept_manifest(
        self,
        plaintext: bytes,
        *,
        task_id: int,
        lease_id: UUID,
        digest: _CiphertextDigest | None,
    ) -> ExportReadyFile:
        try:
            raw = json.loads(plaintext.decode("utf-8"))
        except (UnicodeError, ValueError):
            raise ValueError("export staging manifest is invalid") from None
        if not isinstance(raw, dict) or set(raw) != MANIFEST_KEYS:
            raise ValueError("export staging manifest is invalid")
        try:
            created_at = datetime.fromisoformat(str(raw["created_at"]))
        except ValueError:
            raise ValueError("export staging manifest is invalid") from None
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("export staging manifest is invalid")
        row_count = raw["row_count"]
        if (
            raw["task_id"] != task_id
            or raw["lease_id"] != str(lease_id)
            or raw["format_version"] != FORMAT_VERSION
            or raw["state"] != MANIFEST_STATE_COMPLETE
            or not isinstance(row_count, int)
            or isinstance(row_count, bool)
            or row_count < 0
            or not isinstance(raw["ciphertext_sha256"], str)
            or not isinstance(raw["ciphertext_size"], int)
            or isinstance(raw["ciphertext_size"], bool)
            or raw["ciphertext_size"] < 0
        ):
            raise ValueError("export staging manifest does not match ciphertext")
        if digest is not None and (
            raw["ciphertext_sha256"] != digest.hexdigest()
            or raw["ciphertext_size"] != digest.size
        ):
            raise ValueError("export staging manifest does not match ciphertext")
        return ExportReadyFile(
            path=self.root,
            task_id=task_id,
            lease_id=lease_id,
            row_count=row_count,
            format_version=FORMAT_VERSION,
            ciphertext_sha256=str(raw["ciphertext_sha256"]),
            ciphertext_size=int(raw["ciphertext_size"]),
            state=MANIFEST_STATE_COMPLETE,
            created_at=created_at,
        )

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
        row_count = 0
        digest = _CiphertextDigest()
        try:
            with part.open("xb") as output:
                os.chmod(part, 0o600)
                prefix = MAGIC + version.to_bytes(VERSION_SIZE, "big")
                output.write(prefix)
                digest.update(prefix)
                async for row in rows:
                    row_count += 1
                    buffer.extend(_csv_line(row))
                    while len(buffer) >= self.frame_size:
                        frame = bytes(buffer[: self.frame_size])
                        del buffer[: self.frame_size]
                        digest.update(
                            self._write_frame(
                                output,
                                frame,
                                version,
                                task_id=task_id,
                                lease_id=lease_id,
                                frame_index=frame_index,
                                kind="data",
                            )
                        )
                        frame_index += 1
                if buffer:
                    digest.update(
                        self._write_frame(
                            output,
                            bytes(buffer),
                            version,
                            task_id=task_id,
                            lease_id=lease_id,
                            frame_index=frame_index,
                            kind="data",
                        )
                    )
                    frame_index += 1
                self._write_frame(
                    output,
                    self._staging_manifest(
                        task_id=task_id,
                        lease_id=lease_id,
                        digest=digest,
                        row_count=row_count,
                    ),
                    version,
                    task_id=task_id,
                    lease_id=lease_id,
                    frame_index=frame_index,
                    kind="terminal",
                )
                output.flush()
                os.fsync(output.fileno())
                self._stage("after_file_fsync")
            os.replace(part, final)
            self._stage("after_replace")
            os.chmod(final, 0o600)
            self._stage("after_chmod")
            fsync_directory(self.root)
            self._stage("after_directory_fsync")
            self.verify_ready(
                final,
                expected_task_id=task_id,
                expected_lease_id=lease_id,
                require_manifest=True,
            )
            return final
        except Exception:
            part.unlink(missing_ok=True)
            raise

    def contained_path(self, raw_path: str | Path) -> Path:
        """解析并要求路径位于导出根目录之内。"""

        path = Path(raw_path).resolve()
        try:
            path.relative_to(self.root)
        except ValueError:
            raise ValueError("export path is outside controlled storage") from None
        if path == self.root:
            raise ValueError("export path is outside controlled storage")
        return path

    def _contained(self, raw_path: str | Path) -> Path:
        return self.contained_path(raw_path)

    def _controlled_path(self, raw_path: str | Path) -> Path:
        path = self._contained(raw_path)
        if path.parent != self.root or path.suffix != ".smsx":
            raise ValueError("export path is outside controlled storage")
        return path

    def _controlled_artifact(self, raw_path: str | Path) -> Path:
        path = self._contained(raw_path)
        allowed_parents = {self.root, self.root / QUARANTINE_DIR_NAME}
        if path.parent not in allowed_parents or path.suffix not in {".smsx", ".part"}:
            raise ValueError("export path is outside controlled storage")
        return path

    def iter_decrypted(self, raw_path: str | Path) -> Iterator[bytes]:
        """先完整校验终止帧，再从同一已打开文件描述符流式解密。"""

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
            frames_offset = source.tell()
            digest = _CiphertextDigest()
            digest.update(header)
            # 第一遍必须读到并认证 terminal；任何截断都发生在首个明文字节交付前。
            for _ in self._iter_v2(
                source,
                version,
                task_id,
                lease_id,
                digest=digest,
                require_manifest=False,
            ):
                pass
            source.seek(frames_offset)
            yield from self._iter_v2(
                source,
                version,
                task_id,
                lease_id,
                verify_terminal=False,
            )

    def _iter_v2(
        self,
        source: BinaryIO,
        version: int,
        task_id: int,
        lease_id: UUID,
        *,
        digest: _CiphertextDigest | None = None,
        require_manifest: bool = False,
        verify_terminal: bool = True,
        accepted: list[ExportReadyFile] | None = None,
    ) -> Iterator[bytes]:
        """从当前偏移严格读取一个完整 SMSX2 帧序列。"""

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
            plaintext_limit = (
                MAX_TERMINAL_PLAINTEXT if kind == "terminal" else self.frame_size
            )
            maximum = plaintext_limit + len(BOUND_ENVELOPE_MAGIC) + NONCE_SIZE + TAG_SIZE
            if length < len(BOUND_ENVELOPE_MAGIC) + NONCE_SIZE + TAG_SIZE or length > maximum:
                raise ValueError("invalid export frame length")
            payload = source.read(length)
            if len(payload) != length:
                raise ValueError("truncated export frame")
            raw_frame = raw_kind + encoded_length + payload
            if kind == "data" and digest is not None:
                digest.update(raw_frame)
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
                if source.read(1):
                    raise ValueError("invalid export terminal frame")
                if verify_terminal:
                    if plaintext:
                        parsed = self._accept_manifest(
                            plaintext,
                            task_id=task_id,
                            lease_id=lease_id,
                            digest=digest,
                        )
                        if accepted is not None:
                            accepted.append(parsed)
                    elif require_manifest:
                        raise ValueError("export staging manifest is missing")
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
            yield self.crypto.decrypt_bytes_legacy(payload, version)

    def inspect_final(
        self,
        raw_path: str | Path,
        *,
        expected_task_id: int | None = None,
        expected_lease_id: UUID | None = None,
        require_manifest: bool = False,
    ) -> ExportReadyFile:
        """认证终止帧与可选清单；不向调用方交付 CSV 明文。"""

        path = self._controlled_path(raw_path)
        match = EXPORT_FILENAME.fullmatch(path.name)
        if match is None:
            raise ValueError("export path is outside controlled storage")
        task_id = int(match.group("task_id"))
        lease_id = UUID(match.group("lease_id"))
        if expected_task_id is not None and task_id != expected_task_id:
            raise ValueError("export ciphertext does not belong to authorized export task")
        if expected_lease_id is not None and lease_id != expected_lease_id:
            raise ValueError("export ciphertext does not belong to current export lease")
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError("export ciphertext file is unavailable")
        accepted: list[ExportReadyFile] = []
        with path.open("rb") as source:
            header = source.read(len(MAGIC) + VERSION_SIZE)
            if len(header) != len(MAGIC) + VERSION_SIZE or not header.startswith(MAGIC):
                raise ValueError("truncated or invalid export header")
            version = int.from_bytes(header[len(MAGIC) :], "big")
            digest = _CiphertextDigest()
            digest.update(header)
            for _ in self._iter_v2(
                source,
                version,
                task_id,
                lease_id,
                digest=digest,
                require_manifest=require_manifest,
                accepted=accepted,
            ):
                pass
        if accepted:
            parsed = accepted[0]
            return ExportReadyFile(
                path=path,
                task_id=parsed.task_id,
                lease_id=parsed.lease_id,
                row_count=parsed.row_count,
                format_version=parsed.format_version,
                ciphertext_sha256=parsed.ciphertext_sha256,
                ciphertext_size=parsed.ciphertext_size,
                state=parsed.state,
                created_at=parsed.created_at,
            )
        return ExportReadyFile(
            path=path,
            task_id=task_id,
            lease_id=lease_id,
            row_count=None,
            format_version=FORMAT_VERSION,
            ciphertext_sha256=None,
            ciphertext_size=None,
            state="legacy",
            created_at=None,
        )

    def verify_ready(
        self,
        raw_path: str | Path,
        *,
        expected_task_id: int,
        expected_lease_id: UUID | None = None,
        require_manifest: bool = True,
    ) -> ExportReadyFile:
        """mark_done 前校验文件名、权限、终止帧、摘要与最终路径身份。"""

        path = self._controlled_path(raw_path)
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError("export ciphertext file is unavailable")
        if path.stat().st_mode & 0o777 != 0o600:
            raise ValueError("export ciphertext permissions are invalid")
        return self.inspect_final(
            path,
            expected_task_id=expected_task_id,
            expected_lease_id=expected_lease_id,
            require_manifest=require_manifest,
        )

    def find_reusable_final(self, task_id: int) -> ExportReadyFile | None:
        """查找可复用的已认证最终文件，避免崩溃窗口留下永久孤儿。"""

        candidates: list[ExportReadyFile] = []
        for artifact in self.list_root_artifacts():
            if artifact.task_id != task_id or artifact.suffix != ".smsx":
                continue
            try:
                candidates.append(
                    self.verify_ready(
                        artifact.path,
                        expected_task_id=task_id,
                        require_manifest=True,
                    )
                )
            except (FileNotFoundError, OSError, ValueError):
                continue
        if not candidates:
            return None
        return max(candidates, key=lambda item: item.path.stat().st_mtime)

    def list_root_artifacts(self) -> list[ExportArtifact]:
        if not self.root.is_dir():
            return []
        items: list[ExportArtifact] = []
        for path in self.root.iterdir():
            if path.is_symlink() or not path.is_file():
                continue
            match = EXPORT_ARTIFACT_NAME.fullmatch(path.name)
            if match is None:
                continue
            items.append(
                ExportArtifact(
                    path=path.resolve(),
                    task_id=int(match.group("task_id")),
                    lease_id=UUID(match.group("lease_id")),
                    suffix=match.group("suffix"),
                )
            )
        return items

    def validate(self, raw_path: str | Path, *, expected_task_id: int) -> Path:
        """在响应头发出前绑定授权任务、受控路径与密文文件身份。"""

        path = self._controlled_path(raw_path)
        match = EXPORT_FILENAME.fullmatch(path.name)
        if match is None or int(match.group("task_id")) != expected_task_id:
            raise ValueError("export ciphertext does not belong to authorized export task")
        if not path.is_file():
            raise FileNotFoundError("export ciphertext file is unavailable")
        return path

    def remove(self, raw_path: str | Path) -> None:
        self._controlled_path(raw_path).unlink(missing_ok=True)

    def remove_artifact(self, raw_path: str | Path) -> None:
        self._controlled_artifact(raw_path).unlink(missing_ok=True)

    def quarantine(self, raw_path: str | Path) -> Path:
        """把不可认证或身份错误的密文移出活动根目录。"""

        path = self._controlled_artifact(raw_path)
        if not path.is_file():
            return path
        quarantine = self.root / QUARANTINE_DIR_NAME
        quarantine.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(quarantine, 0o700)
        destination = quarantine / path.name
        destination.unlink(missing_ok=True)
        os.replace(path, destination)
        os.chmod(destination, 0o600)
        fsync_directory(quarantine)
        fsync_directory(self.root)
        return destination

    def list_quarantine_artifacts(self) -> list[ExportArtifact]:
        quarantine = self.root / QUARANTINE_DIR_NAME
        if not quarantine.is_dir():
            return []
        items: list[ExportArtifact] = []
        for path in quarantine.iterdir():
            if path.is_symlink() or not path.is_file():
                continue
            match = EXPORT_ARTIFACT_NAME.fullmatch(path.name)
            if match is None:
                continue
            items.append(
                ExportArtifact(
                    path=path.resolve(),
                    task_id=int(match.group("task_id")),
                    lease_id=UUID(match.group("lease_id")),
                    suffix=match.group("suffix"),
                )
            )
        return items
