#!/usr/bin/env python3
"""在非特权 systemd unit 内运行固定 Cloudflare Quick Tunnel。"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, TextIO, cast

from test_secure_access_contract import (
    CLOUDFLARED_PATH,
    MAX_LIFETIME_SECONDS,
    ORIGIN,
    STATUS_PATH,
    parse_quick_tunnel_url,
    serialize_ready_state,
)

_URL_CANDIDATE_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?[.]trycloudflare[.]com"
    r"(?![A-Za-z0-9.-])"
)
_CLOUDFLARED_ENV = {
    "HOME": "/nonexistent",
    "XDG_CONFIG_HOME": "/nonexistent",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
}


class SecureAccessRuntimeError(RuntimeError):
    """Quick Tunnel 运行过程未满足固定安全合同。"""


class TunnelProcess(Protocol):
    stderr: TextIO | Iterable[str] | None

    def wait(self) -> int: ...

    def terminate(self) -> None: ...


ProcessFactory = Callable[..., TunnelProcess]
Clock = Callable[[], datetime]


def cloudflared_argv() -> tuple[str, ...]:
    """返回唯一允许的 cloudflared argv。"""

    return (
        str(CLOUDFLARED_PATH),
        "tunnel",
        "--no-autoupdate",
        "--protocol",
        "http2",
        "--url",
        ORIGIN,
    )


def _candidate_urls(line: str) -> set[str]:
    candidates: set[str] = set()
    for match in _URL_CANDIDATE_RE.finditer(line):
        try:
            candidates.add(parse_quick_tunnel_url(match.group(0)))
        except ValueError:
            continue
    return candidates


def extract_quick_tunnel_url(lines: Iterable[str]) -> str:
    """从 cloudflared 输出中取得唯一的严格 Quick Tunnel URL。"""

    urls: set[str] = set()
    for line in lines:
        urls.update(_candidate_urls(line))
    if len(urls) != 1:
        raise SecureAccessRuntimeError("secure access tunnel URL is unavailable")
    return next(iter(urls))


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_ready_state(
    status_path: Path,
    *,
    url: str,
    started_at: datetime,
) -> None:
    """以 0640 原子发布无敏感 ready 元数据。"""

    expires_at = started_at + timedelta(seconds=MAX_LIFETIME_SECONDS)
    payload = serialize_ready_state(
        url=url,
        started_at=started_at,
        expires_at=expires_at,
    ).encode("utf-8")
    status_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    temporary = status_path.parent / f".{status_path.name}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o640)
    try:
        os.fchmod(descriptor, 0o640)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, status_path)
        _fsync_directory(status_path.parent)
    finally:
        os.close(descriptor)
        with suppress(FileNotFoundError):
            temporary.unlink()


def _remove_state(status_path: Path) -> None:
    try:
        status_path.unlink()
    except FileNotFoundError:
        return


class QuickTunnelRuntime:
    """只运行固定 argv，并只公开严格的无敏感 URL 状态。"""

    def __init__(
        self,
        *,
        status_path: Path = STATUS_PATH,
        process_factory: ProcessFactory | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.status_path = status_path
        self.process_factory = process_factory or cast(ProcessFactory, subprocess.Popen)
        self.clock = clock or (lambda: datetime.now(UTC))

    def run(self) -> int:
        process: TunnelProcess | None = None
        ready = False
        completed = False
        urls: set[str] = set()
        _remove_state(self.status_path)
        try:
            process = self.process_factory(
                cloudflared_argv(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                close_fds=True,
                env=_CLOUDFLARED_ENV,
            )
            if process.stderr is None:
                raise SecureAccessRuntimeError(
                    "secure access tunnel URL is unavailable"
                )
            for line in process.stderr:
                urls.update(_candidate_urls(line))
                if len(urls) > 1:
                    raise SecureAccessRuntimeError(
                        "secure access tunnel URL is unavailable"
                    )
                if len(urls) == 1 and not ready:
                    write_ready_state(
                        self.status_path,
                        url=next(iter(urls)),
                        started_at=self.clock(),
                    )
                    ready = True
            if not ready:
                raise SecureAccessRuntimeError(
                    "secure access tunnel URL is unavailable"
                )
            returncode = process.wait()
            if returncode != 0:
                raise SecureAccessRuntimeError("secure access tunnel exited")
            completed = True
            return returncode
        except (OSError, subprocess.SubprocessError) as exc:
            raise SecureAccessRuntimeError("secure access tunnel failed") from exc
        finally:
            if process is not None and not completed:
                with suppress(OSError):
                    process.terminate()
            _remove_state(self.status_path)


def main() -> int:
    if len(sys.argv) != 1:
        print("secure access runtime blocked", file=sys.stderr)
        return 2
    try:
        return QuickTunnelRuntime().run()
    except Exception:
        print("secure access runtime failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
