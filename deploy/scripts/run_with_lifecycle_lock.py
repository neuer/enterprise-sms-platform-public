#!/usr/bin/env python3
"""以可移植的非阻塞文件锁串行执行受控 Compose 生命周期动作。"""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import os
import signal
import stat
import subprocess
import sys
from pathlib import Path


class LifecycleLockError(RuntimeError):
    """生命周期锁路径或锁操作违反安全策略。"""


def _normalize_runtime_root(raw: str) -> Path:
    if not os.path.isabs(raw):
        raise LifecycleLockError("runtime root must be absolute")
    if ".." in Path(raw).parts:
        raise LifecycleLockError("runtime root must not contain parent traversal")
    normalized = os.path.normpath(raw)
    if normalized == os.path.sep or normalized.startswith(os.path.sep * 2):
        raise LifecycleLockError("runtime root is unsafe")
    return Path(normalized)


def _validate_private_directory(path: Path) -> None:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise LifecycleLockError("lifecycle lock parent must be a real directory")
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise LifecycleLockError("lifecycle lock parent ownership or mode is unsafe")


def _ensure_private_directory(path: Path) -> None:
    missing: list[Path] = []
    cursor = path
    while True:
        try:
            metadata = cursor.lstat()
        except FileNotFoundError:
            missing.append(cursor)
            parent = cursor.parent
            if parent == cursor:
                raise LifecycleLockError(
                    "lifecycle lock parent cannot be created"
                ) from None
            cursor = parent
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise LifecycleLockError("lifecycle lock ancestor is unsafe")
        break

    for directory in reversed(missing):
        with contextlib.suppress(FileExistsError):
            directory.mkdir(mode=0o700)
        _validate_private_directory(directory)
    _validate_private_directory(path)


def _open_lock(path: Path) -> int:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise LifecycleLockError("lifecycle lock file cannot be opened safely") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise LifecycleLockError("lifecycle lock file is unsafe")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(descriptor)
        raise LifecycleLockError("lifecycle lock is already held") from exc
    except (OSError, LifecycleLockError):
        os.close(descriptor)
        raise
    return descriptor


def _verify_held_lock(runtime_root: Path, descriptor: int) -> None:
    lock_path = Path(f"{runtime_root}.lifecycle.lock")
    try:
        inherited = os.fstat(descriptor)
        expected = lock_path.lstat()
    except OSError as exc:
        raise LifecycleLockError("inherited lifecycle lock fd is unavailable") from exc
    for metadata in (inherited, expected):
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise LifecycleLockError("inherited lifecycle lock fd is unsafe")
    if (inherited.st_dev, inherited.st_ino) != (expected.st_dev, expected.st_ino):
        raise LifecycleLockError("inherited lifecycle lock fd targets the wrong file")

    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        probe = os.open(lock_path, flags)
    except OSError as exc:
        raise LifecycleLockError("lifecycle lock probe cannot be opened safely") from exc
    try:
        probe_metadata = os.fstat(probe)
        if (probe_metadata.st_dev, probe_metadata.st_ino) != (
            inherited.st_dev,
            inherited.st_ino,
        ):
            raise LifecycleLockError("lifecycle lock changed during verification")
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                pass
            else:
                raise LifecycleLockError("lifecycle lock verification failed") from exc
        else:
            fcntl.flock(probe, fcntl.LOCK_UN)
            raise LifecycleLockError("inherited lifecycle lock fd is not locked")
    finally:
        os.close(probe)

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            raise LifecycleLockError(
                "inherited lifecycle lock fd does not hold the lock"
            ) from exc
        raise LifecycleLockError("lifecycle lock verification failed") from exc


def _parse_run_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--wrapper", required=True)
    parser.add_argument(
        "--operation",
        choices=(
            "up",
            "down",
            "rotate",
            "migrate",
            "partition-maintenance",
            "release",
            "vendor-test",
            "test-update",
        ),
        required=True,
    )
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    return parser.parse_args()


def _parse_verify_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--lock-fd", required=True, type=int)
    return parser.parse_args(sys.argv[2:])


def _wait_for_locked_child(
    command: list[str], environment: dict[str, str], descriptor: int
) -> int:
    child: subprocess.Popen[bytes] | None = None
    pending_signals: list[int] = []

    def forward_signal(signum: int, _frame: object) -> None:
        if child is None:
            pending_signals.append(signum)
            return
        with contextlib.suppress(ProcessLookupError):
            child.send_signal(signum)

    forwarded = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
    previous_handlers = {
        signum: signal.signal(signum, forward_signal) for signum in forwarded
    }
    try:
        child = subprocess.Popen(
            command,
            env=environment,
            pass_fds=(descriptor,),
        )
        for signum in pending_signals:
            forward_signal(signum, None)
        returncode = child.wait()
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    return 128 - returncode if returncode < 0 else returncode


def main() -> int:
    if sys.argv[1:2] == ["verify-held"]:
        arguments = _parse_verify_args()
        runtime_root = _normalize_runtime_root(arguments.runtime_root)
        _verify_held_lock(runtime_root, arguments.lock_fd)
        return 0

    arguments = _parse_run_args()
    runtime_root = _normalize_runtime_root(arguments.runtime_root)
    wrapper = Path(arguments.wrapper)
    if not wrapper.is_absolute() or not wrapper.is_file():
        raise LifecycleLockError("wrapper path is unsafe")

    lock_path = Path(f"{runtime_root}.lifecycle.lock")
    _ensure_private_directory(lock_path.parent)
    descriptor = _open_lock(lock_path)
    child_arguments = list(arguments.arguments)
    if child_arguments[:1] == ["--"]:
        child_arguments.pop(0)
    environment = os.environ.copy()
    environment["SMS_RUNTIME_ROOT"] = str(runtime_root)
    environment["SMS_RUNTIME_SECRETS_DIR"] = str(runtime_root / "current")
    environment["SMS_LIFECYCLE_LOCKED"] = "1"
    environment["SMS_LIFECYCLE_LOCK_FD"] = str(descriptor)
    try:
        return _wait_for_locked_child(
            [str(wrapper), "__locked", arguments.operation, *child_arguments],
            environment,
            descriptor,
        )
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LifecycleLockError as exc:
        print(f"sms-compose: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
