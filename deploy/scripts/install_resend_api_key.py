#!/usr/bin/env python3
"""从标准输入原子安装 Resend Docker secret 源文件，不回显凭据。"""

from __future__ import annotations

import argparse
import contextlib
import os
import stat
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import BinaryIO

MAX_KEY_BYTES = 512


class ResendKeyInstallError(ValueError):
    """输入或目标文件不符合最小权限安装要求。"""


def _read_key(stream: BinaryIO) -> bytes:
    value = stream.read(MAX_KEY_BYTES + 3).rstrip(b"\r\n")
    if not value:
        raise ResendKeyInstallError("Resend API key input is empty")
    if len(value) > MAX_KEY_BYTES:
        raise ResendKeyInstallError("Resend API key input is too large")
    try:
        decoded = value.decode("utf-8")
    except UnicodeError as exc:
        raise ResendKeyInstallError("Resend API key input is invalid") from exc
    if any(character.isspace() for character in decoded):
        raise ResendKeyInstallError("Resend API key input is invalid")
    return value + b"\n"


def install_key(path: str | Path, stream: BinaryIO) -> None:
    """写入同目录临时文件并原子替换，最终权限固定为 0600。"""

    destination = Path(path)
    if not destination.parent.is_dir():
        raise ResendKeyInstallError("Resend API key destination directory is unavailable")
    if destination.is_symlink():
        raise ResendKeyInstallError("Resend API key destination must not be a symlink")
    value = _read_key(stream)
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        remaining = memoryview(value)
        while remaining:
            remaining = remaining[os.write(descriptor, remaining) :]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, destination)
    except OSError as exc:
        raise ResendKeyInstallError("Resend API key installation failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install a Resend API key from stdin without echoing it"
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        install_key(args.output, sys.stdin.buffer)
    except ResendKeyInstallError as exc:
        print(f"Resend API key installation failed: {exc}", file=sys.stderr)
        return 2
    print("Resend API key installed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
