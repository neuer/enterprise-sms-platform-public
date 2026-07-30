#!/usr/bin/env python3
"""通过交互式 TTY 安装 canonical vendor Docker secret source。"""

from __future__ import annotations

import getpass
import os
import stat
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

CANONICAL_SOURCE_DIR = Path(__file__).resolve().parents[1] / "secrets"
EXPECTED_ROOT_UID = 0


class VendorCredentialInstallError(RuntimeError):
    """凭据未通过固定 TTY 与 canonical 文件边界安装。"""


class TTYStream(Protocol):
    def isatty(self) -> bool: ...


def require_interactive_tty(stdin: TTYStream, stdout: TTYStream) -> None:
    if not stdin.isatty() or not stdout.isatty():
        raise VendorCredentialInstallError("interactive TTY is required")


def _validate_source_directory(path: Path, *, expected_uid: int) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise VendorCredentialInstallError("canonical source directory is unsafe") from exc
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise VendorCredentialInstallError("canonical source directory is unsafe")
    if metadata.st_uid != expected_uid or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise VendorCredentialInstallError("canonical source directory is unsafe")


def _validate_value(value: str) -> bytes:
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise VendorCredentialInstallError("vendor credentials were not installed")
    try:
        return value.encode("utf-8")
    except UnicodeError as exc:
        raise VendorCredentialInstallError("vendor credentials were not installed") from exc


def _stage_secret(directory: Path, name: str, value: bytes) -> Path:
    target = directory / name
    if target.exists() or target.is_symlink():
        metadata = target.lstat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise VendorCredentialInstallError("canonical secret target is unsafe")
    temporary = directory / f".{name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        return temporary
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def install_vendor_credentials(
    source_directory: Path,
    *,
    reader: Callable[[str], str] | None = None,
    stdin: TTYStream = sys.stdin,
    stdout: TTYStream = sys.stdout,
    expected_uid: int = 0,
) -> bool:
    """TTY 读取两个值并分别以 0600 原子替换 canonical source。"""

    require_interactive_tty(stdin, stdout)
    _validate_source_directory(source_directory, expected_uid=expected_uid)
    if reader is None:
        reader = getpass.getpass
    secret_name = _validate_value(reader("SecretName: "))
    secret_key = _validate_value(reader("SecretKey: "))
    staged: list[tuple[Path, Path]] = []
    try:
        for name, value in (
            ("vendor_secret_name", secret_name),
            ("vendor_secret_key", secret_key),
        ):
            target = source_directory / name
            staged.append((_stage_secret(source_directory, name, value), target))
        for temporary, target in staged:
            os.replace(temporary, target)
            target.chmod(0o600)
        directory_fd = os.open(
            source_directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return True
    finally:
        for temporary, _target in staged:
            temporary.unlink(missing_ok=True)


def main() -> int:
    try:
        install_vendor_credentials(
            CANONICAL_SOURCE_DIR,
            expected_uid=EXPECTED_ROOT_UID,
        )
    except VendorCredentialInstallError:
        print("未安装")
        return 1
    print("已安装")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
