"""厂商拉走即消费响应的本地加密 spill，供落库前崩溃恢复。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

SOURCE_PATTERN = re.compile(r"^(report|reply)$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class RawSpillRecord:
    source: str
    payload_sha256: str
    key_version: int
    http_status: int
    content_encoding: str
    payload_enc: bytes
    path: Path


class RawSpillStore:
    """只保存 AES-GCM 密文；明文手机号不得进入 spill 文件。"""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, source: str, payload_sha256: str) -> Path:
        if SOURCE_PATTERN.fullmatch(source) is None:
            raise ValueError("invalid raw spill source")
        if SHA256_PATTERN.fullmatch(payload_sha256) is None:
            raise ValueError("invalid raw spill digest")
        return self.directory / f"{source}-{payload_sha256}.spill"

    def write(
        self,
        *,
        source: str,
        payload_sha256: str,
        key_version: int,
        http_status: int,
        content_encoding: str,
        payload_enc: bytes,
    ) -> Path:
        """先写临时文件并 fsync，再原子改名，保证 kill -9 后仍可恢复完整密文。"""

        if not payload_enc:
            raise ValueError("raw spill payload is empty")
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self._path(source, payload_sha256)
        header = json.dumps(
            {
                "source": source,
                "payload_sha256": payload_sha256,
                "key_version": key_version,
                "http_status": http_status,
                "content_encoding": content_encoding,
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
        directory_fd = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
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

    def remove(self, source: str, payload_sha256: str) -> None:
        path = self._path(source, payload_sha256)
        path.unlink(missing_ok=True)

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
            )
        except (OSError, KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            return None
