#!/usr/bin/env python3
"""管理测试环境固定、短时的 HTTPS Quick Tunnel。"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from test_secure_access_contract import (
    CLOUDFLARED_PATH,
    CLOUDFLARED_SHA256,
    HOST_ASSET_NAMES,
    HOST_ASSET_ROOT,
    HOST_MANIFEST_PATH,
    MAX_LIFETIME_SECONDS,
    SERVICE_NAME,
    STATUS_PATH,
    TEST_HOST_MARKER_PATH,
    HostManifest,
    SecureAccessState,
    parse_host_manifest,
    parse_ready_state,
    parse_test_host_marker,
    serialize_ready_state,
)

PLATFORM_SERVICE = "sms-platform.service"
INSTALLED_UNIT = Path("/etc/systemd/system") / SERVICE_NAME
DEFAULT_ROOT = HOST_ASSET_ROOT
READY_TIMEOUT_SECONDS = 60.0
PUBLIC_PROBE_INITIAL_DELAY_SECONDS = 5.0
MAX_STATE_BYTES = 4096


class SecureAccessManagerError(RuntimeError):
    """主机不满足临时 HTTPS 入口的固定安全合同。"""


class CommandRunner(Protocol):
    def run(
        self,
        *argv: str,
        check: bool,
    ) -> subprocess.CompletedProcess[str]: ...


class OriginProbe(Protocol):
    def check(self) -> bool: ...


class PublicTunnelProbe(Protocol):
    def check(self, url: str) -> bool: ...


class SubprocessRunner:
    """以固定 argv 执行 systemctl，不继承交互输入。"""

    def run(
        self,
        *argv: str,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(argv),
            check=check,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )


class LoopbackWebProbe:
    """只探测固定 127.0.0.1:18080/login，不读取响应 body。"""

    def check(self) -> bool:
        connection = http.client.HTTPConnection("127.0.0.1", 18080, timeout=5)
        try:
            connection.request("HEAD", "/login")
            response = connection.getresponse()
            return 200 <= response.status < 400
        except (OSError, http.client.HTTPException):
            return False
        finally:
            connection.close()


class HttpsPublicTunnelProbe:
    """验证随机 Quick Tunnel 已完成 TLS 和固定 `/login` 端到端注册。"""

    def check(self, url: str) -> bool:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.port is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            return False
        connection = http.client.HTTPSConnection(parsed.hostname, 443, timeout=5)
        try:
            connection.request("GET", "/login")
            response = connection.getresponse()
            response.read(1)
            return 200 <= response.status < 400
        except (OSError, http.client.HTTPException):
            return False
        finally:
            connection.close()


@dataclass(frozen=True, slots=True)
class ManagerResult:
    """管理器唯一允许输出的公开状态。"""

    status: str
    url: str | None = None
    expires_at: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "status": self.status,
            "url": self.url,
            "expires_at": self.expires_at,
        }


Clock = Callable[[], datetime]
Sleeper = Callable[[float], None]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _require_regular_file(
    path: Path,
    *,
    expected_uid: int,
    expected_mode: int,
) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SecureAccessManagerError("secure access host asset is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != expected_uid
        or stat.S_IMODE(metadata.st_mode) != expected_mode
    ):
        raise SecureAccessManagerError("secure access host asset is unsafe")
    return metadata


class SecureAccessManager:
    """以固定 systemd unit 管理单条 Quick Tunnel。"""

    def __init__(
        self,
        *,
        root: Path = DEFAULT_ROOT,
        binary_path: Path = CLOUDFLARED_PATH,
        installed_unit: Path = INSTALLED_UNIT,
        manifest_path: Path = HOST_MANIFEST_PATH,
        status_path: Path = STATUS_PATH,
        expected_uid: int = 0,
        expected_sha256: str = CLOUDFLARED_SHA256,
        runner: CommandRunner | None = None,
        probe: OriginProbe | None = None,
        public_probe: PublicTunnelProbe | None = None,
        clock: Clock | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
        sleeper: Sleeper = time.sleep,
        ready_timeout_seconds: float = READY_TIMEOUT_SECONDS,
        public_probe_initial_delay_seconds: float = (
            PUBLIC_PROBE_INITIAL_DELAY_SECONDS
        ),
    ) -> None:
        self.root = root
        self.binary_path = binary_path
        self.installed_unit = installed_unit
        self.manifest_path = manifest_path
        self.status_path = status_path
        self.expected_uid = expected_uid
        self.expected_sha256 = expected_sha256
        self.runner = runner or SubprocessRunner()
        self.probe = probe or LoopbackWebProbe()
        self.public_probe = public_probe or HttpsPublicTunnelProbe()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.monotonic_clock = monotonic_clock
        self.sleeper = sleeper
        self.ready_timeout_seconds = ready_timeout_seconds
        self.public_probe_initial_delay_seconds = (
            public_probe_initial_delay_seconds
        )

    @property
    def source_unit(self) -> Path:
        return self.root / SERVICE_NAME

    def _read_manifest(self) -> HostManifest:
        _require_regular_file(
            self.manifest_path,
            expected_uid=self.expected_uid,
            expected_mode=0o644,
        )
        descriptor = -1
        try:
            descriptor = os.open(
                self.manifest_path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            metadata = os.fstat(descriptor)
            if metadata.st_size > MAX_STATE_BYTES:
                raise SecureAccessManagerError("secure access host manifest is unsafe")
            raw = os.read(descriptor, MAX_STATE_BYTES + 1)
            if len(raw) > MAX_STATE_BYTES:
                raise SecureAccessManagerError("secure access host manifest is unsafe")
            return parse_host_manifest(raw.decode("utf-8"))
        except SecureAccessManagerError:
            raise
        except (OSError, UnicodeError, ValueError) as exc:
            raise SecureAccessManagerError(
                "secure access host manifest is invalid"
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _validate_assets(self) -> str:
        manifest = self._read_manifest()
        files = manifest.files
        _require_regular_file(
            self.binary_path,
            expected_uid=self.expected_uid,
            expected_mode=0o755,
        )
        try:
            if (
                files["cloudflared"] != self.expected_sha256
                or _sha256(self.binary_path) != files["cloudflared"]
            ):
                raise SecureAccessManagerError("secure access host asset has drifted")
        except OSError as exc:
            raise SecureAccessManagerError(
                "secure access host asset is unavailable"
            ) from exc
        _require_regular_file(
            self.installed_unit,
            expected_uid=self.expected_uid,
            expected_mode=0o644,
        )
        try:
            _require_regular_file(
                self.source_unit,
                expected_uid=self.expected_uid,
                expected_mode=0o644,
            )
            if _sha256(self.installed_unit) != files[SERVICE_NAME]:
                raise SecureAccessManagerError("secure access host asset has drifted")
            if _sha256(self.source_unit) != files[SERVICE_NAME]:
                raise SecureAccessManagerError("secure access host asset has drifted")
            for name in HOST_ASSET_NAMES:
                if name in {"cloudflared", SERVICE_NAME}:
                    continue
                path = self.root / name
                _require_regular_file(
                    path,
                    expected_uid=self.expected_uid,
                    expected_mode=(
                        0o755 if name == "sms-compose-bootstrap" else 0o644
                    ),
                )
                if _sha256(path) != files[name]:
                    raise SecureAccessManagerError(
                        "secure access host asset has drifted"
                    )
        except OSError as exc:
            raise SecureAccessManagerError(
                "secure access host asset is unavailable"
            ) from exc
        return manifest.source_commit

    def _service_state(self, service: str) -> str:
        try:
            result = self.runner.run(
                "systemctl",
                "is-active",
                service,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SecureAccessManagerError(
                "secure access service state is unavailable"
            ) from exc
        return result.stdout.strip()

    def _service_result(self, service: str) -> str:
        try:
            result = self.runner.run(
                "systemctl",
                "show",
                "--property=Result",
                "--value",
                service,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SecureAccessManagerError(
                "secure access service result is unavailable"
            ) from exc
        return result.stdout.strip()

    def _service_started_monotonic_us(self, service: str) -> int:
        try:
            result = self.runner.run(
                "systemctl",
                "show",
                "--property=ExecMainStartTimestampMonotonic",
                "--value",
                service,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SecureAccessManagerError(
                "secure access service timing is unavailable"
            ) from exc
        value = result.stdout.strip()
        if result.returncode != 0 or not value.isdecimal() or int(value) <= 0:
            raise SecureAccessManagerError(
                "secure access service timing is unavailable"
            )
        return int(value)

    def _read_state(self, *, require_unexpired: bool = True) -> SecureAccessState | None:
        try:
            metadata = self.status_path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SecureAccessManagerError(
                "secure access runtime state is unavailable"
            ) from exc
        descriptor = -1
        try:
            descriptor = os.open(
                self.status_path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o640
                or not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
                or opened.st_size > MAX_STATE_BYTES
            ):
                raise SecureAccessManagerError(
                    "secure access runtime state is unsafe"
                )
            raw = os.read(descriptor, MAX_STATE_BYTES + 1)
            if len(raw) > MAX_STATE_BYTES:
                raise SecureAccessManagerError(
                    "secure access runtime state is unsafe"
                )
            state = parse_ready_state(raw.decode("utf-8"))
        except SecureAccessManagerError:
            raise
        except (OSError, UnicodeError, ValueError) as exc:
            raise SecureAccessManagerError(
                "secure access runtime state is invalid"
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if (
            require_unexpired
            and state.expires_at.astimezone(UTC) <= self.clock().astimezone(UTC)
        ):
            raise SecureAccessManagerError("secure access runtime state has expired")
        return state

    def _remove_state(self) -> None:
        try:
            self.status_path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise SecureAccessManagerError(
                "secure access runtime state cannot be cleared"
            ) from exc

    def _ready_result(self, state: SecureAccessState) -> ManagerResult:
        return ManagerResult(
            status="ready",
            url=state.url,
            expires_at=state.expires_at.astimezone(UTC).isoformat(),
        )

    def status(self) -> ManagerResult:
        service_state = self._service_state(SERVICE_NAME)
        if service_state == "active":
            try:
                state = self._read_state(require_unexpired=False)
            except SecureAccessManagerError:
                return ManagerResult(status="failed")
            if state is None:
                return ManagerResult(status="starting")
            if state.expires_at.astimezone(UTC) <= self.clock().astimezone(UTC):
                try:
                    self._stop_and_clean()
                except SecureAccessManagerError:
                    return ManagerResult(status="failed")
                return ManagerResult(status="inactive")
            return self._ready_result(state)
        if service_state == "activating":
            return ManagerResult(status="starting")
        if service_state == "failed":
            try:
                state = self._read_state(require_unexpired=False)
            except SecureAccessManagerError:
                return ManagerResult(status="failed")
            if (
                state is not None
                and state.expires_at.astimezone(UTC) <= self.clock().astimezone(UTC)
            ):
                try:
                    self._stop_and_clean()
                except SecureAccessManagerError:
                    return ManagerResult(status="failed")
                return ManagerResult(status="inactive")
            if state is None and self._service_result(SERVICE_NAME) == "timeout":
                try:
                    started_us = self._service_started_monotonic_us(SERVICE_NAME)
                except SecureAccessManagerError:
                    return ManagerResult(status="failed")
                age_us = int(self.monotonic_clock() * 1_000_000) - started_us
                if age_us < MAX_LIFETIME_SECONDS * 1_000_000:
                    return ManagerResult(status="failed")
                try:
                    self._stop_and_clean()
                except SecureAccessManagerError:
                    return ManagerResult(status="failed")
                return ManagerResult(status="inactive")
            return ManagerResult(status="failed")
        if service_state in {"inactive", "unknown", ""}:
            return ManagerResult(status="inactive")
        return ManagerResult(status="failed")

    def _stop_and_clean(self) -> None:
        try:
            self.runner.run(
                "systemctl",
                "stop",
                SERVICE_NAME,
                check=True,
            )
            self.runner.run(
                "systemctl",
                "reset-failed",
                SERVICE_NAME,
                check=False,
            )
            if self._service_state(SERVICE_NAME) not in {"inactive", "unknown", ""}:
                raise SecureAccessManagerError(
                    "secure access service stop was not confirmed"
                )
        except SecureAccessManagerError:
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            raise SecureAccessManagerError(
                "secure access service stop failed"
            ) from exc
        self._remove_state()

    def _fail_closed(self) -> None:
        self._stop_and_clean()

    def start(self) -> ManagerResult:
        self._validate_assets()
        current = self.status()
        started_service = False
        if (
            current.status == "ready"
            and current.url is not None
            and self.public_probe.check(current.url)
        ):
            return current
        if current.status == "failed":
            self._fail_closed()
            raise SecureAccessManagerError("secure access service is unavailable")
        if current.status == "inactive":
            if self._service_state(PLATFORM_SERVICE) != "active" or not self.probe.check():
                raise SecureAccessManagerError("secure access origin is unavailable")
            self._remove_state()
            try:
                self.runner.run(
                    "systemctl",
                    "start",
                    SERVICE_NAME,
                    check=True,
                )
                started_service = True
            except (OSError, subprocess.SubprocessError) as exc:
                self._fail_closed()
                raise SecureAccessManagerError(
                    "secure access service start failed"
                ) from exc

        deadline = time.monotonic() + self.ready_timeout_seconds
        public_probe_delay_pending = started_service
        while time.monotonic() < deadline:
            current = self.status()
            if current.status == "ready":
                if public_probe_delay_pending:
                    self.sleeper(self.public_probe_initial_delay_seconds)
                    public_probe_delay_pending = False
                if current.url is not None and self.public_probe.check(current.url):
                    return current
                self.sleeper(0.1)
                continue
            if current.status in {"failed", "inactive"}:
                self._fail_closed()
                raise SecureAccessManagerError(
                    "secure access service did not become ready"
                )
            self.sleeper(0.1)
        self._fail_closed()
        raise SecureAccessManagerError("secure access service did not become ready")

    def stop(self) -> ManagerResult:
        self._stop_and_clean()
        return ManagerResult(status="inactive")


def require_test_host_marker(
    path: Path = TEST_HOST_MARKER_PATH,
    *,
    expected_uid: int = 0,
) -> None:
    """只允许 root 控制的独立开发测试主机 marker 使用入口。"""

    descriptor = -1
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or stat.S_IMODE(metadata.st_mode) not in {0o400, 0o600}
            or metadata.st_size > MAX_STATE_BYTES
        ):
            raise OSError("unsafe marker")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (
            (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_size > MAX_STATE_BYTES
        ):
            raise OSError("marker changed")
        raw = os.read(descriptor, MAX_STATE_BYTES + 1)
        if len(raw) > MAX_STATE_BYTES:
            raise OSError("marker too large")
        parse_test_host_marker(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise SecureAccessManagerError(
            "secure access test host marker is invalid"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def parse_manager_action(
    argv: Sequence[str],
    *,
    euid: int,
    mode: str | None,
    internal: bool = False,
) -> str:
    """只接受 root 在 development 下执行三个固定动作。"""

    if (
        euid != 0
        or mode != "development"
        or len(argv) != 1
        or argv[0] not in (
            {"start", "status", "stop", "verify-assets"}
            if internal
            else {"start", "status", "stop"}
        )
    ):
        raise SecureAccessManagerError("secure access invocation is blocked")
    return argv[0]


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    environment = os.environ if environ is None else environ
    try:
        action = parse_manager_action(
            arguments,
            euid=os.geteuid(),
            mode=environment.get("SMS_SECRETS_MODE"),
            internal=environment.get("SMS_SECURE_ACCESS_INTERNAL") == "1",
        )
        if action != "stop":
            require_test_host_marker(expected_uid=0)
        manager = SecureAccessManager()
        if action == "verify-assets":
            source_commit = manager._validate_assets()
            print(
                json.dumps(
                    {
                        "schema_version": 1,
                        "source_commit": source_commit,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            return 0
        result = getattr(manager, action)()
        print(json.dumps(result.as_dict(), separators=(",", ":"), sort_keys=True))
        return 0
    except Exception:
        print("secure access manager blocked", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "SecureAccessManager",
    "SecureAccessManagerError",
    "parse_manager_action",
    "require_test_host_marker",
    "serialize_ready_state",
]
