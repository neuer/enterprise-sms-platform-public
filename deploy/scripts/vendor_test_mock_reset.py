#!/usr/bin/env python3
"""在宿主 lifecycle lock 内把开发测试环境完整切回纯 Mock。"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import uuid
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import NamedTuple, Protocol
from urllib.parse import urlsplit

from prepare_runtime_secrets import VENDOR_REVOCATION_TOMBSTONE
from test_update_backup import require_inherited_lifecycle_lock
from vendor_credential_store import VendorCredentialStore
from vendor_test_files import (
    PURE_MOCK_DOTENV_VALUES,
    VendorTestFileError,
    VendorTestMarker,
)

VENDOR_READER_SERVICES = ("worker-realtime", "worker-report", "worker-bulk")
BACKEND_SERVICES = (
    "api",
    "worker-realtime",
    "worker-report",
    "worker-bulk",
    "worker-callback",
    "outbox-dispatcher",
    "beat",
)
OLD_SETTING_SERVICES = (
    "api",
    "beat",
    "worker-realtime",
    "worker-report",
    "worker-bulk",
    "worker-callback",
    "outbox-dispatcher",
)
VENDOR_CREDENTIAL_ROOT = Path("/var/lib/sms-platform/vendor-test/credentials")
VENDOR_TEST_MARKER = Path("/etc/sms-platform/test-environment")
PLATFORM_ROOT = Path("/opt/sms-platform")
RUNTIME_ROOT = Path("/run/sms-platform/secrets")
_LIVE_FIXED_DOTENV_VALUES = {
    "ENVIRONMENT": "development",
    "DEBUG": "1",
    "AUTH_MOCK": "1",
    "VENDOR_MOCK": "0",
    "COMPOSE_PROFILES": "",
    "SMS_VENDOR_TEST_STATE_DIR": "/var/lib/sms-platform/vendor-test",
    "SMS_VENDOR_CONTROL_SOCKET_DIR": "/run/sms-platform/vendor-control",
}
_COMPOSE_DOTENV_OVERRIDE_KEYS = frozenset(
    {
        "ENVIRONMENT",
        "DEBUG",
        "AUTH_MOCK",
        "VENDOR_MOCK",
        "VENDOR_BASE_URL",
        "VENDOR_LIVE_TEST_ORIGIN",
        "COMPOSE_PROFILES",
        "SMS_VENDOR_LIVE_TEST_ORIGIN",
    }
)
_LIVE_ONLY_DOTENV_KEYS = frozenset(
    {
        "VENDOR_LIVE_TEST_ORIGIN",
        "SMS_VENDOR_TEST_STATE_DIR",
        "SMS_VENDOR_CONTROL_SOCKET_DIR",
    }
)
_DOTENV_LINE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([A-Za-z0-9_./:@+*,-]*)")
_VENDOR_HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?")
_VENDOR_CREDENTIAL_KEYS = frozenset(
    {"VENDOR_SECRET_NAME", "VENDOR_SECRET_KEY", "SECRETNAME", "SECRETKEY"}
)
_MARKER_FIELDS = frozenset(
    {
        "schema_version",
        "mode",
        "vendor_origin",
        "daily_segment_limit",
        "timezone",
        "backup_config",
    }
)
_CONTAINER_PROBE = (
    "from pathlib import Path;"
    "p=(Path('/run/secrets/vendor_secret_name'),"
    "Path('/run/secrets/vendor_secret_key'));"
    "raise SystemExit(0 if all(x.read_bytes()=="
    f"{VENDOR_REVOCATION_TOMBSTONE!r} for x in p) else 1)"
)
_MOCK_SETTINGS_PROBE = (
    "import os;"
    "forbidden=('VENDOR_LIVE_TEST_ORIGIN','SMS_VENDOR_TEST_STATE_DIR',"
    "'SMS_VENDOR_CONTROL_SOCKET_DIR');"
    "ok=(os.environ.get('ENVIRONMENT')=='development' and "
    "os.environ.get('DEBUG')=='1' and os.environ.get('AUTH_MOCK')=='1' and "
    "os.environ.get('VENDOR_MOCK')=='1' and "
    "os.environ.get('VENDOR_BASE_URL')=='http://mock-vendor:9028' and "
    "os.environ.get('COMPOSE_PROFILES')=='dev' and "
    "all(name not in os.environ for name in forbidden));"
    "raise SystemExit(0 if ok else 1)"
)


class VendorRuntimeResetError(RuntimeError):
    """runtime 撤销失败；消息不携带子进程输出或 secret 元数据。"""


class RuntimeResetResult(NamedTuple):
    status: str


def _validate_private_file(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise VendorTestFileError("controlled file is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) not in {0o400, 0o600}
    ):
        raise VendorTestFileError("controlled file metadata is invalid")


def _parse_dotenv(path: Path) -> tuple[list[str], dict[str, str]]:
    _validate_private_file(path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise VendorTestFileError("dotenv is unavailable") from exc
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _DOTENV_LINE.fullmatch(line)
        if match is None:
            raise VendorTestFileError("dotenv contains non-strict syntax")
        key, value = match.groups()
        if key.upper() in _VENDOR_CREDENTIAL_KEYS:
            raise VendorTestFileError("dotenv must not contain vendor credentials")
        if key in values:
            raise VendorTestFileError("dotenv contains duplicate keys")
        values[key] = value
    return lines, values


def _safe_vendor_origin(value: object) -> str:
    if type(value) is not str:
        raise VendorTestFileError("vendor origin is invalid")
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise VendorTestFileError("vendor origin is invalid") from exc
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.hostname is None
        or _VENDOR_HOST.fullmatch(parsed.hostname) is None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or value != parsed.geturl()
    ):
        raise VendorTestFileError("vendor origin is invalid")
    return value


def _fsync_parent(path: Path) -> None:
    parent = path.parent
    try:
        metadata = parent.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
        ):
            raise VendorTestFileError("controlled file parent is unsafe")
        descriptor = os.open(
            parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except VendorTestFileError:
        raise
    except OSError as exc:
        raise VendorTestFileError("controlled file parent is unavailable") from exc


def _atomic_replace(path: Path, payload: bytes) -> None:
    _fsync_parent(path)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
        _validate_private_file(path)
        _fsync_parent(path)
    except VendorTestFileError:
        raise
    except OSError as exc:
        raise VendorTestFileError("controlled file update failed") from exc
    finally:
        temporary.unlink(missing_ok=True)


def read_live_vendor_origin(path: Path) -> str:
    """确认 dotenv 仍是与 root 环境绑定的完整真实厂商配置。"""

    _lines, values = _parse_dotenv(path)
    selected = {key: values.get(key) for key in _LIVE_FIXED_DOTENV_VALUES}
    if selected != _LIVE_FIXED_DOTENV_VALUES:
        raise VendorTestFileError("dotenv is not in controlled live mode")
    expected_origin = _safe_vendor_origin(values.get("VENDOR_LIVE_TEST_ORIGIN"))
    if values.get("VENDOR_BASE_URL") != expected_origin:
        raise VendorTestFileError("live vendor origin is invalid")
    return expected_origin


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise VendorTestFileError("marker contains duplicate fields")
        value[key] = item
    return value


def read_vendor_test_marker(
    path: Path,
    *,
    expected_vendor_origin: str | None = None,
) -> VendorTestMarker:
    """按固定字段读取 marker，厂商 origin 由当前 dotenv 绑定。"""

    _validate_private_file(path)
    try:
        decoded = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except VendorTestFileError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VendorTestFileError("marker contains invalid JSON") from exc
    if type(decoded) is not dict:
        raise VendorTestFileError("marker must be an object")
    payload = decoded
    if set(payload) != set(_MARKER_FIELDS):
        raise VendorTestFileError("marker fields are invalid")
    actual = _safe_vendor_origin(payload.get("vendor_origin"))
    expected = actual if expected_vendor_origin is None else _safe_vendor_origin(
        expected_vendor_origin
    )
    if payload != {
        "schema_version": 1,
        "mode": "development-vendor-live",
        "vendor_origin": expected,
        "daily_segment_limit": 100,
        "timezone": "Asia/Shanghai",
        "backup_config": "/etc/sms-platform/test-update-backup.json",
    }:
        raise VendorTestFileError("marker contract values are invalid")
    return VendorTestMarker(
        schema_version=1,
        mode="development-vendor-live",
        vendor_origin=actual,
        daily_segment_limit=100,
        timezone="Asia/Shanghai",
        backup_config=Path("/etc/sms-platform/test-update-backup.json"),
    )


def require_restored_pure_mock_dotenv(path: Path) -> None:
    """确认固定运行键已回到纯 Mock，且 live-only 键已经移除。"""

    _lines, values = _parse_dotenv(path)
    selected = {key: values.get(key) for key in PURE_MOCK_DOTENV_VALUES}
    if selected != dict(PURE_MOCK_DOTENV_VALUES) or any(
        key in values for key in _LIVE_ONLY_DOTENV_KEYS
    ):
        raise VendorTestFileError("dotenv must be restored to pure Mock mode")


def restore_pure_mock_dotenv(path: Path) -> bool:
    """只允许从精确 live 或已恢复状态原子收敛到纯 Mock。"""

    lines, values = _parse_dotenv(path)
    pure = {key: values.get(key) for key in PURE_MOCK_DOTENV_VALUES} == dict(
        PURE_MOCK_DOTENV_VALUES
    ) and not any(key in values for key in _LIVE_ONLY_DOTENV_KEYS)
    if pure:
        require_restored_pure_mock_dotenv(path)
        _fsync_parent(path)
        return False
    read_live_vendor_origin(path)
    restored: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            restored.append(line)
            continue
        key = line.split("=", 1)[0]
        if key in _LIVE_ONLY_DOTENV_KEYS:
            continue
        if key in PURE_MOCK_DOTENV_VALUES:
            restored.append(f"{key}={PURE_MOCK_DOTENV_VALUES[key]}")
        else:
            restored.append(line)
    _atomic_replace(path, ("\n".join(restored) + "\n").encode())
    require_restored_pure_mock_dotenv(path)
    return True


def remove_vendor_test_marker(
    path: Path,
    *,
    expected_vendor_origin: str | None = None,
) -> bool:
    """校验后删除 live marker；缺失重放仍落盘父目录。"""

    try:
        path.lstat()
    except FileNotFoundError:
        _fsync_parent(path)
        return False
    except OSError as exc:
        raise VendorTestFileError("marker is unavailable") from exc
    read_vendor_test_marker(path, expected_vendor_origin=expected_vendor_origin)
    try:
        path.unlink()
        _fsync_parent(path)
    except VendorTestFileError:
        raise
    except OSError as exc:
        raise VendorTestFileError("marker removal failed") from exc
    return True


class RuntimeResetOperations(Protocol):
    def require_lifecycle_lock(self) -> None: ...

    def dotenv_is_pure_mock(self) -> bool: ...

    def require_restorable_configuration(self) -> None: ...

    def stop_old_setting_services(self) -> None: ...

    def restore_pure_mock_dotenv(self) -> None: ...

    def runtime_is_revoked(self) -> bool: ...

    def readers_are_revoked(self) -> bool: ...

    def only_current_generation(self) -> bool: ...

    def revoke_runtime(self) -> None: ...

    def validate_compose(self) -> None: ...

    def start_mock_vendor(self) -> None: ...

    def mock_vendor_is_ready(self) -> bool: ...

    def start_backend_services(self) -> None: ...

    def backend_services_are_mock(self) -> bool: ...

    def web_reaches_api(self) -> bool: ...

    def restore_recovery_surface(self) -> None: ...

    def credential_store_is_empty(self) -> bool: ...

    def reset_credential_store(self) -> None: ...

    def cleanup_stale(self) -> None: ...

    def live_marker_is_absent(self) -> bool: ...

    def remove_live_marker(self) -> None: ...


class FixedCommandRunner:
    """执行代码定义的固定 argv，并完全丢弃输出。"""

    def succeeds(
        self,
        command: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
    ) -> bool:
        try:
            result = subprocess.run(
                list(command),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=dict(env) if env is not None else None,
                check=False,
            )
        except OSError:
            return False
        return result.returncode == 0

    def output(
        self,
        command: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
    ) -> bytes | None:
        try:
            result = subprocess.run(
                list(command),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=dict(env) if env is not None else None,
                check=False,
            )
        except OSError:
            return None
        return result.stdout if result.returncode == 0 else None


class HostRuntimeResetOperations:
    """把撤销协议绑定到固定 preprocessor 与 Compose 服务集合。"""

    def __init__(
        self,
        *,
        root: Path,
        runtime_root: Path,
        runner: FixedCommandRunner | None = None,
    ) -> None:
        self.root = root
        self.runtime_root = runtime_root
        self.runner = runner or FixedCommandRunner()
        self.preprocessor = root / "deploy/scripts/prepare_runtime_secrets.py"
        self.source_dir = root / "deploy/secrets"
        self.dotenv_file = root / ".env"
        self.credential_store = VendorCredentialStore(VENDOR_CREDENTIAL_ROOT)
        self.marker_file = VENDOR_TEST_MARKER
        self.live_vendor_origin: str | None = None
        self.configuration_preflight_complete = False
        self.compose = (
            "docker",
            "compose",
            "--env-file",
            str(root / ".env"),
            "-f",
            str(root / "deploy/docker-compose.yml"),
        )
        self.compose_env = dict(os.environ)
        for key in _COMPOSE_DOTENV_OVERRIDE_KEYS:
            self.compose_env.pop(key, None)

    def _require_success(
        self,
        command: Sequence[str],
        *,
        compose: bool = False,
    ) -> None:
        if not self.runner.succeeds(
            command,
            env=self.compose_env if compose else None,
        ):
            raise VendorRuntimeResetError("runtime reset failed")

    def _preprocessor(self, command: str, *arguments: str) -> bool:
        return self.runner.succeeds(
            [
                sys.executable,
                str(self.preprocessor),
                command,
                "--runtime-root",
                str(self.runtime_root),
                *arguments,
            ]
        )

    def require_lifecycle_lock(self) -> None:
        require_inherited_lifecycle_lock(self.runtime_root)

    def dotenv_is_pure_mock(self) -> bool:
        try:
            require_restored_pure_mock_dotenv(self.dotenv_file)
        except VendorTestFileError:
            return False
        return True

    def require_restorable_configuration(self) -> None:
        live_vendor_origin: str | None = None
        try:
            require_restored_pure_mock_dotenv(self.dotenv_file)
        except VendorTestFileError:
            live_vendor_origin = read_live_vendor_origin(self.dotenv_file)
            read_vendor_test_marker(
                self.marker_file,
                expected_vendor_origin=live_vendor_origin,
            )
        if (
            self.configuration_preflight_complete
            and live_vendor_origin != self.live_vendor_origin
        ):
            raise VendorRuntimeResetError("runtime reset failed")
        self.live_vendor_origin = live_vendor_origin
        self.configuration_preflight_complete = True

    def stop_old_setting_services(self) -> None:
        stopped = self.runner.succeeds(
            [*self.compose, "stop", *OLD_SETTING_SERVICES],
            env=self.compose_env,
        )
        verified = True
        for service in OLD_SETTING_SERVICES:
            running = self.runner.output(
                [
                    *self.compose,
                    "ps",
                    "--status",
                    "running",
                    "--services",
                    service,
                ],
                env=self.compose_env,
            )
            if running is None or running.strip():
                verified = False
        if not stopped or not verified:
            raise VendorRuntimeResetError("runtime reset failed")

    def restore_pure_mock_dotenv(self) -> None:
        self.require_restorable_configuration()
        restore_pure_mock_dotenv(self.dotenv_file)

    def runtime_is_revoked(self) -> bool:
        return self._preprocessor("verify-vendor-revoked")

    def readers_are_revoked(self) -> bool:
        return all(
            self.runner.succeeds(
                [
                    *self.compose,
                    "exec",
                    "-T",
                    service,
                    "python",
                    "-c",
                    _CONTAINER_PROBE,
                ],
                env=self.compose_env,
            )
            for service in VENDOR_READER_SERVICES
        )

    def only_current_generation(self) -> bool:
        return self._preprocessor("verify-only-current")

    def revoke_runtime(self) -> None:
        self._require_success(
            [
                sys.executable,
                str(self.preprocessor),
                "revoke-vendor",
                "--source-dir",
                str(self.source_dir),
                "--runtime-root",
                str(self.runtime_root),
                "--mode",
                "development",
            ]
        )

    def validate_compose(self) -> None:
        self._require_success(
            [*self.compose, "config", "--quiet"],
            compose=True,
        )

    def start_mock_vendor(self) -> None:
        self._require_success(
            [
                *self.compose,
                "up",
                "-d",
                "--no-deps",
                "--force-recreate",
                "--wait",
                "--wait-timeout",
                "120",
                "mock-vendor",
            ],
            compose=True,
        )

    def mock_vendor_is_ready(self) -> bool:
        return self.runner.succeeds(
            [
                *self.compose,
                "exec",
                "-T",
                "mock-vendor",
                "python",
                "-m",
                "app.healthcheck",
                "live",
                "9028",
            ],
            env=self.compose_env,
        )

    def start_backend_services(self) -> None:
        self._require_success(
            [
                *self.compose,
                "up",
                "-d",
                "--no-deps",
                "--force-recreate",
                "--wait",
                "--wait-timeout",
                "120",
                *BACKEND_SERVICES,
                "web",
            ],
            compose=True,
        )

    def backend_services_are_mock(self) -> bool:
        return all(
            self.runner.succeeds(
                [
                    *self.compose,
                    "exec",
                    "-T",
                    service,
                    "python",
                    "-c",
                    _MOCK_SETTINGS_PROBE,
                ],
                env=self.compose_env,
            )
            for service in BACKEND_SERVICES
        )

    def web_reaches_api(self) -> bool:
        return self.runner.succeeds(
            [
                *self.compose,
                "exec",
                "-T",
                "web",
                "wget",
                "-q",
                "--spider",
                "http://127.0.0.1:8080/livez",
            ],
            env=self.compose_env,
        )

    def restore_recovery_surface(self) -> None:
        """失败后只恢复不持有厂商凭据的 API 与 Web，供同 operation 对账。"""

        for service in ("api", "web"):
            self._require_success(
                [
                    *self.compose,
                    "up",
                    "-d",
                    "--no-deps",
                    "--force-recreate",
                    "--wait",
                    "--wait-timeout",
                    "120",
                    service,
                ],
                compose=True,
            )
        if not self.web_reaches_api():
            raise VendorRuntimeResetError("runtime reset failed")

    def credential_store_is_empty(self) -> bool:
        return not self.credential_store.reset_required()

    def reset_credential_store(self) -> None:
        status = self.credential_store.reset()
        if status.configured or status.state != "setup_required":
            raise VendorRuntimeResetError("runtime reset failed")

    def cleanup_stale(self) -> None:
        self._require_success(
            [
                sys.executable,
                str(self.preprocessor),
                "cleanup",
                "--runtime-root",
                str(self.runtime_root),
                "--stale",
            ]
        )

    def live_marker_is_absent(self) -> bool:
        try:
            self.marker_file.lstat()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return False

    def remove_live_marker(self) -> None:
        if self.live_vendor_origin is None:
            self.live_vendor_origin = read_vendor_test_marker(
                self.marker_file,
                expected_vendor_origin=None,
            ).vendor_origin
        remove_vendor_test_marker(
            self.marker_file,
            expected_vendor_origin=self.live_vendor_origin,
        )


class VendorRuntimeResetManager:
    """幂等回到纯 Mock；任何部分失败都禁止恢复旧厂商消费者。"""

    def __init__(self, operations: RuntimeResetOperations) -> None:
        self.operations = operations

    def _safe_mock_is_running(self) -> bool:
        if not self.operations.dotenv_is_pure_mock():
            return False
        if not self.operations.runtime_is_revoked():
            return False
        if not self.operations.mock_vendor_is_ready():
            return False
        if not self.operations.backend_services_are_mock():
            return False
        if not self.operations.web_reaches_api():
            return False
        return self.operations.readers_are_revoked()

    def _finalize_safe_mock(self) -> RuntimeResetResult:
        if not self.operations.credential_store_is_empty():
            self.operations.reset_credential_store()
        if not self.operations.credential_store_is_empty():
            raise VendorRuntimeResetError("runtime reset failed")
        if not self.operations.only_current_generation():
            self.operations.cleanup_stale()
        if not (
            self._safe_mock_is_running()
            and self.operations.only_current_generation()
            and self.operations.credential_store_is_empty()
        ):
            raise VendorRuntimeResetError("runtime reset failed")
        if not self.operations.live_marker_is_absent():
            self.operations.remove_live_marker()
        if not self.operations.live_marker_is_absent():
            raise VendorRuntimeResetError("runtime reset failed")
        return RuntimeResetResult("runtime_revoked")

    def reset(self) -> RuntimeResetResult:
        recovery_surface_needed = False
        try:
            self.operations.require_lifecycle_lock()
            if self._safe_mock_is_running():
                return self._finalize_safe_mock()

            self.operations.require_restorable_configuration()
            recovery_surface_needed = True
            self.operations.stop_old_setting_services()
            self.operations.restore_pure_mock_dotenv()
            if not self.operations.dotenv_is_pure_mock():
                raise VendorRuntimeResetError("runtime reset failed")
            if not self.operations.runtime_is_revoked():
                self.operations.revoke_runtime()
            if not self.operations.runtime_is_revoked():
                raise VendorRuntimeResetError("runtime reset failed")
            self.operations.validate_compose()
            self.operations.start_mock_vendor()
            if not self.operations.mock_vendor_is_ready():
                raise VendorRuntimeResetError("runtime reset failed")
            self.operations.start_backend_services()
            if not self._safe_mock_is_running():
                raise VendorRuntimeResetError("runtime reset failed")
            return self._finalize_safe_mock()
        except Exception:
            if recovery_surface_needed:
                with suppress(Exception):
                    self.operations.restore_recovery_surface()
            raise VendorRuntimeResetError("runtime reset failed") from None


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if (
        arguments != ["__locked", "vendor-test", "reset-to-mock"]
        or os.geteuid() != 0
        or os.environ.get("SMS_SECRETS_MODE") != "development"
        or Path(os.environ.get("SMS_PLATFORM_ROOT", str(PLATFORM_ROOT)))
        != PLATFORM_ROOT
        or Path(os.environ.get("SMS_RUNTIME_ROOT", str(RUNTIME_ROOT))) != RUNTIME_ROOT
    ):
        print("vendor runtime reset failed", file=sys.stderr)
        return 1
    try:
        result = VendorRuntimeResetManager(
            HostRuntimeResetOperations(
                root=PLATFORM_ROOT,
                runtime_root=RUNTIME_ROOT,
            )
        ).reset()
    except VendorRuntimeResetError:
        print("vendor runtime reset failed", file=sys.stderr)
        return 1
    print(json.dumps({"status": result.status}, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
