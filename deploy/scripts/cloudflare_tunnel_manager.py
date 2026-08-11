#!/usr/bin/env python3
"""管理开发测试主机的持久 Cloudflare Named Tunnel。"""

from __future__ import annotations

import getpass
import hashlib
import http.client
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlunsplit

from test_secure_access_contract import (
    CLOUDFLARED_PATH,
    CLOUDFLARED_SHA256,
    HOST_ASSET_ROOT,
    HOST_MANIFEST_PATH,
    ORIGIN,
    PERSISTENT_SERVICE_NAME,
    TEST_HOST_MARKER_PATH,
    parse_host_manifest,
)
from test_secure_access_manager import require_test_host_marker
from verify_web_transport import TransportEvidence, run_probe

CONFIG_PATH = Path("/etc/sms-platform/cloudflare-tunnel.json")
TOKEN_PATH = Path("/etc/sms-platform/cloudflare-tunnel-token")
INSTALLED_UNIT = Path("/etc/systemd/system") / PERSISTENT_SERVICE_NAME
PLATFORM_SERVICE = "sms-platform.service"
MAX_FILE_BYTES = 8192
START_TIMEOUT_SECONDS = 30.0
_HOSTNAME_RE = re.compile(
    r"(?=.{4,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?"
)
_TOKEN_RE = re.compile(r"[A-Za-z0-9._~+/-]{40,4096}={0,2}")


class CloudflareTunnelManagerError(RuntimeError):
    """持久 Tunnel 不满足固定安全合同。"""


class CommandRunner(Protocol):
    def run(
        self,
        *argv: str,
        check: bool,
    ) -> subprocess.CompletedProcess[str]: ...


class OriginProbe(Protocol):
    def check(self) -> bool: ...


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
    """只探测固定回环 Web origin，不读取响应 body。"""

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


@dataclass(frozen=True, slots=True)
class TunnelConfig:
    """持久 Tunnel 的非敏感公开主机名配置。"""

    hostname: str

    @property
    def http_base(self) -> str:
        return urlunsplit(("http", self.hostname, "", "", ""))

    @property
    def https_base(self) -> str:
        return urlunsplit(("https", self.hostname, "", "", ""))


@dataclass(frozen=True, slots=True)
class ManagerResult:
    """管理器唯一允许输出的无敏感状态。"""

    status: str
    hostname: str | None = None
    token_configured: bool = False
    tls_version: str | None = None
    certificate_days_remaining: int | None = None

    def as_dict(self) -> dict[str, str | int | bool | None]:
        return {
            "status": self.status,
            "hostname": self.hostname,
            "token_configured": self.token_configured,
            "tls_version": self.tls_version,
            "certificate_days_remaining": self.certificate_days_remaining,
        }


TokenReader = Callable[[], str]
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
    max_bytes: int | None = None,
) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise CloudflareTunnelManagerError("cloudflare tunnel asset is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != expected_uid
        or stat.S_IMODE(metadata.st_mode) != expected_mode
        or (max_bytes is not None and metadata.st_size > max_bytes)
    ):
        raise CloudflareTunnelManagerError("cloudflare tunnel asset is unsafe")
    return metadata


def _read_bounded_file(
    path: Path,
    *,
    expected_uid: int,
    expected_mode: int,
) -> bytes:
    before = _require_regular_file(
        path,
        expected_uid=expected_uid,
        expected_mode=expected_mode,
        max_bytes=MAX_FILE_BYTES,
    )
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_size > MAX_FILE_BYTES
        ):
            raise CloudflareTunnelManagerError("cloudflare tunnel asset is unsafe")
        payload = os.read(descriptor, MAX_FILE_BYTES + 1)
        if len(payload) > MAX_FILE_BYTES:
            raise CloudflareTunnelManagerError("cloudflare tunnel asset is unsafe")
        return payload
    except CloudflareTunnelManagerError:
        raise
    except OSError as exc:
        raise CloudflareTunnelManagerError("cloudflare tunnel asset is unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _validate_parent(path: Path, *, expected_uid: int) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        path.mkdir(mode=0o700, parents=False)
        metadata = path.lstat()
    except OSError as exc:
        raise CloudflareTunnelManagerError("cloudflare tunnel directory is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != expected_uid
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise CloudflareTunnelManagerError("cloudflare tunnel directory is unsafe")


def _atomic_write(
    path: Path,
    payload: bytes,
    *,
    mode: int,
    expected_uid: int,
) -> None:
    if len(payload) > MAX_FILE_BYTES:
        raise CloudflareTunnelManagerError("cloudflare tunnel payload is invalid")
    _validate_parent(path.parent, expected_uid=expected_uid)
    descriptor = -1
    temporary_name = ""
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            dir=path.parent,
        )
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        temporary_name = ""
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise CloudflareTunnelManagerError("cloudflare tunnel configuration failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name:
            with suppress(OSError):
                os.unlink(temporary_name)


def parse_hostname(value: object) -> str:
    """只接受规范化的小写 FQDN。"""

    if type(value) is not str or _HOSTNAME_RE.fullmatch(value) is None:
        raise CloudflareTunnelManagerError("cloudflare tunnel hostname is invalid")
    return value


def parse_token(value: object) -> str:
    """验证 Tunnel token 的形状，但绝不输出 token。"""

    if type(value) is not str or _TOKEN_RE.fullmatch(value) is None:
        raise CloudflareTunnelManagerError("cloudflare tunnel token is invalid")
    return value


def _default_token_reader() -> str:
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise CloudflareTunnelManagerError("cloudflare tunnel token requires a TTY")
    return getpass.getpass("Cloudflare Tunnel token: ")


class CloudflareTunnelManager:
    """以 root-only token-file 和固定 systemd unit 管理 Named Tunnel。"""

    def __init__(
        self,
        *,
        root: Path = HOST_ASSET_ROOT,
        binary_path: Path = CLOUDFLARED_PATH,
        manifest_path: Path = HOST_MANIFEST_PATH,
        config_path: Path = CONFIG_PATH,
        token_path: Path = TOKEN_PATH,
        installed_unit: Path = INSTALLED_UNIT,
        expected_uid: int = 0,
        expected_sha256: str = CLOUDFLARED_SHA256,
        runner: CommandRunner | None = None,
        origin_probe: OriginProbe | None = None,
        token_reader: TokenReader = _default_token_reader,
        transport_probe: Callable[..., TransportEvidence] = run_probe,
        monotonic_clock: Callable[[], float] = time.monotonic,
        sleeper: Sleeper = time.sleep,
        start_timeout_seconds: float = START_TIMEOUT_SECONDS,
    ) -> None:
        self.root = root
        self.binary_path = binary_path
        self.manifest_path = manifest_path
        self.config_path = config_path
        self.token_path = token_path
        self.installed_unit = installed_unit
        self.expected_uid = expected_uid
        self.expected_sha256 = expected_sha256
        self.runner = runner or SubprocessRunner()
        self.origin_probe = origin_probe or LoopbackWebProbe()
        self.token_reader = token_reader
        self.transport_probe = transport_probe
        self.monotonic_clock = monotonic_clock
        self.sleeper = sleeper
        self.start_timeout_seconds = start_timeout_seconds

    @property
    def source_unit(self) -> Path:
        return self.root / PERSISTENT_SERVICE_NAME

    def _read_manifest_files(self) -> Mapping[str, str]:
        raw = _read_bounded_file(
            self.manifest_path,
            expected_uid=self.expected_uid,
            expected_mode=0o644,
        )
        try:
            return parse_host_manifest(raw.decode("utf-8")).files
        except (UnicodeError, ValueError) as exc:
            raise CloudflareTunnelManagerError(
                "cloudflare tunnel host manifest is invalid"
            ) from exc

    def _validate_host_assets(self) -> None:
        files = self._read_manifest_files()
        required = {
            "cloudflared": (self.binary_path, 0o755, self.expected_sha256),
            "cloudflare_tunnel_manager.py": (
                self.root / "cloudflare_tunnel_manager.py",
                0o644,
                files.get("cloudflare_tunnel_manager.py"),
            ),
            "verify_web_transport.py": (
                self.root / "verify_web_transport.py",
                0o644,
                files.get("verify_web_transport.py"),
            ),
            PERSISTENT_SERVICE_NAME: (
                self.source_unit,
                0o644,
                files.get(PERSISTENT_SERVICE_NAME),
            ),
        }
        for name, (path, mode, expected_digest) in required.items():
            if expected_digest is None or files.get(name) != expected_digest:
                raise CloudflareTunnelManagerError("cloudflare tunnel host manifest is invalid")
            _require_regular_file(
                path,
                expected_uid=self.expected_uid,
                expected_mode=mode,
            )
            try:
                observed = _sha256(path)
            except OSError as exc:
                raise CloudflareTunnelManagerError(
                    "cloudflare tunnel host asset is unavailable"
                ) from exc
            if observed != expected_digest:
                raise CloudflareTunnelManagerError("cloudflare tunnel host asset has drifted")

    def _service_state(self, service: str) -> str:
        try:
            result = self.runner.run("systemctl", "is-active", service, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise CloudflareTunnelManagerError(
                "cloudflare tunnel service state is unavailable"
            ) from exc
        return result.stdout.strip()

    def _load_config(self) -> TunnelConfig:
        raw = _read_bounded_file(
            self.config_path,
            expected_uid=self.expected_uid,
            expected_mode=0o600,
        )
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise CloudflareTunnelManagerError(
                "cloudflare tunnel configuration is invalid"
            ) from exc
        if (
            type(document) is not dict
            or set(document) != {"schema_version", "hostname", "origin"}
            or document.get("schema_version") != 1
            or document.get("origin") != ORIGIN
        ):
            raise CloudflareTunnelManagerError("cloudflare tunnel configuration is invalid")
        return TunnelConfig(hostname=parse_hostname(document.get("hostname")))

    def _token_configured(self) -> bool:
        try:
            raw = _read_bounded_file(
                self.token_path,
                expected_uid=self.expected_uid,
                expected_mode=0o600,
            )
            parse_token(raw.decode("ascii").rstrip("\n"))
            return True
        except (CloudflareTunnelManagerError, UnicodeError):
            return False

    def _require_installed_unit(self) -> None:
        files = self._read_manifest_files()
        expected = files.get(PERSISTENT_SERVICE_NAME)
        _require_regular_file(
            self.installed_unit,
            expected_uid=self.expected_uid,
            expected_mode=0o644,
        )
        try:
            observed = _sha256(self.installed_unit)
        except OSError as exc:
            raise CloudflareTunnelManagerError(
                "cloudflare tunnel service unit is unavailable"
            ) from exc
        if expected is None or observed != expected:
            raise CloudflareTunnelManagerError("cloudflare tunnel service unit has drifted")

    def install(self) -> ManagerResult:
        self._validate_host_assets()
        if self._service_state(PERSISTENT_SERVICE_NAME) == "active":
            raise CloudflareTunnelManagerError(
                "cloudflare tunnel service must be stopped before install"
            )
        try:
            self.runner.run(
                "systemd-analyze",
                "verify",
                str(self.source_unit),
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CloudflareTunnelManagerError("cloudflare tunnel service unit is invalid") from exc
        _atomic_write(
            self.installed_unit,
            self.source_unit.read_bytes(),
            mode=0o644,
            expected_uid=self.expected_uid,
        )
        try:
            self.runner.run("systemctl", "daemon-reload", check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            raise CloudflareTunnelManagerError("cloudflare tunnel systemd reload failed") from exc
        self._require_installed_unit()
        return self.status()

    def configure(self, hostname: str) -> ManagerResult:
        self._validate_host_assets()
        if self._service_state(PERSISTENT_SERVICE_NAME) == "active":
            raise CloudflareTunnelManagerError(
                "cloudflare tunnel service must be stopped before configure"
            )
        safe_hostname = parse_hostname(hostname)
        payload = (
            json.dumps(
                {
                    "schema_version": 1,
                    "hostname": safe_hostname,
                    "origin": ORIGIN,
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        _atomic_write(
            self.config_path,
            payload,
            mode=0o600,
            expected_uid=self.expected_uid,
        )
        return self.status()

    def install_token(self) -> ManagerResult:
        self._validate_host_assets()
        if self._service_state(PERSISTENT_SERVICE_NAME) == "active":
            raise CloudflareTunnelManagerError(
                "cloudflare tunnel service must be stopped before token rotation"
            )
        token = parse_token(self.token_reader())
        try:
            _atomic_write(
                self.token_path,
                token.encode("ascii") + b"\n",
                mode=0o600,
                expected_uid=self.expected_uid,
            )
        finally:
            token = ""
        return self.status()

    def status(self) -> ManagerResult:
        service_state = self._service_state(PERSISTENT_SERVICE_NAME)
        try:
            hostname = self._load_config().hostname
        except CloudflareTunnelManagerError:
            hostname = None
        normalized = (
            "active"
            if service_state == "active"
            else "starting"
            if service_state == "activating"
            else "inactive"
            if service_state in {"inactive", "unknown", ""}
            else "failed"
        )
        return ManagerResult(
            status=normalized,
            hostname=hostname,
            token_configured=self._token_configured(),
        )

    def _stop_fail_closed(self) -> None:
        try:
            self.runner.run(
                "systemctl",
                "disable",
                "--now",
                PERSISTENT_SERVICE_NAME,
                check=False,
            )
            self.runner.run(
                "systemctl",
                "reset-failed",
                PERSISTENT_SERVICE_NAME,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CloudflareTunnelManagerError("cloudflare tunnel service stop failed") from exc

    def start(self) -> ManagerResult:
        self._validate_host_assets()
        self._require_installed_unit()
        config = self._load_config()
        if not self._token_configured():
            raise CloudflareTunnelManagerError("cloudflare tunnel token is unavailable")
        if self._service_state(PLATFORM_SERVICE) != "active" or not self.origin_probe.check():
            raise CloudflareTunnelManagerError("cloudflare tunnel origin is unavailable")
        try:
            self.runner.run(
                "systemctl",
                "enable",
                "--now",
                PERSISTENT_SERVICE_NAME,
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self._stop_fail_closed()
            raise CloudflareTunnelManagerError("cloudflare tunnel service start failed") from exc
        deadline = self.monotonic_clock() + self.start_timeout_seconds
        while self.monotonic_clock() < deadline:
            if self._service_state(PERSISTENT_SERVICE_NAME) == "active":
                return ManagerResult(
                    status="active",
                    hostname=config.hostname,
                    token_configured=True,
                )
            self.sleeper(0.1)
        self._stop_fail_closed()
        raise CloudflareTunnelManagerError("cloudflare tunnel service did not become active")

    def verify(self) -> ManagerResult:
        config = self._load_config()
        if self._service_state(PERSISTENT_SERVICE_NAME) != "active":
            raise CloudflareTunnelManagerError("cloudflare tunnel service is inactive")
        try:
            evidence = self.transport_probe(
                http_base=config.http_base,
                https_base=config.https_base,
                min_certificate_days=14,
                timeout_s=10,
            )
        except Exception as exc:
            raise CloudflareTunnelManagerError(
                "cloudflare tunnel transport verification failed"
            ) from exc
        return ManagerResult(
            status="verified",
            hostname=config.hostname,
            token_configured=self._token_configured(),
            tls_version=evidence.tls_version,
            certificate_days_remaining=evidence.certificate_days_remaining,
        )

    def stop(self) -> ManagerResult:
        self._stop_fail_closed()
        if self._service_state(PERSISTENT_SERVICE_NAME) not in {
            "inactive",
            "unknown",
            "",
        }:
            raise CloudflareTunnelManagerError("cloudflare tunnel service stop was not confirmed")
        return self.status()


def parse_manager_action(
    argv: Sequence[str], *, euid: int, mode: str | None
) -> tuple[str, str | None]:
    """只接受 root 在 development 下执行固定动作。"""

    if euid != 0 or mode != "development" or not argv:
        raise CloudflareTunnelManagerError("cloudflare tunnel invocation is blocked")
    if len(argv) == 3 and argv[:2] == ["configure", "--hostname"]:
        return "configure", parse_hostname(argv[2])
    if len(argv) == 1 and argv[0] in {
        "install",
        "install-token",
        "start",
        "status",
        "verify",
        "stop",
    }:
        return argv[0], None
    raise CloudflareTunnelManagerError("cloudflare tunnel invocation is blocked")


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    environment = os.environ if environ is None else environ
    try:
        action, hostname = parse_manager_action(
            arguments,
            euid=os.geteuid(),
            mode=environment.get("SMS_SECRETS_MODE"),
        )
        if action != "stop":
            require_test_host_marker(TEST_HOST_MARKER_PATH, expected_uid=0)
        manager = CloudflareTunnelManager()
        if action == "configure":
            assert hostname is not None
            result = manager.configure(hostname)
        elif action == "install-token":
            result = manager.install_token()
        else:
            result = getattr(manager, action.replace("-", "_"))()
        print(json.dumps(result.as_dict(), separators=(",", ":"), sort_keys=True))
        return 0
    except Exception:
        print("cloudflare tunnel manager blocked", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CloudflareTunnelManager",
    "CloudflareTunnelManagerError",
    "ManagerResult",
    "TunnelConfig",
    "parse_hostname",
    "parse_manager_action",
    "parse_token",
]
