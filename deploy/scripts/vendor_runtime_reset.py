#!/usr/bin/env python3
"""在宿主 lifecycle lock 内撤销正式厂商 runtime secret 副本。"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NamedTuple, Protocol

from prepare_runtime_secrets import VENDOR_REVOCATION_TOMBSTONE
from test_update_backup import require_inherited_lifecycle_lock
from vendor_credential_store import VendorCredentialStore
from vendor_test_files import (
    VendorTestFileError,
    read_live_vendor_origin,
    read_vendor_test_marker,
    remove_vendor_test_marker,
    require_restored_pure_mock_dotenv,
    restore_pure_mock_dotenv,
)

VENDOR_READER_SERVICES = ("worker-realtime", "worker-bulk")
BACKEND_SERVICES = (
    "api",
    "worker-realtime",
    "worker-bulk",
    "worker-callback",
    "outbox-dispatcher",
    "beat",
)
OLD_SETTING_SERVICES = (
    "api",
    "beat",
    "worker-realtime",
    "worker-bulk",
    "worker-callback",
    "outbox-dispatcher",
)
VENDOR_CREDENTIAL_ROOT = Path("/var/lib/sms-platform/vendor-test/credentials")
VENDOR_TEST_MARKER = Path("/etc/sms-platform/test-environment")
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
        try:
            self.operations.require_lifecycle_lock()
            if self._safe_mock_is_running():
                return self._finalize_safe_mock()

            self.operations.require_restorable_configuration()
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
            raise VendorRuntimeResetError("runtime reset failed") from None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Revoke vendor runtime credentials")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    return parser


def _safe_absolute(path: Path) -> bool:
    return path.is_absolute() and path != Path(path.anchor) and ".." not in path.parts


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if (
        os.geteuid() != 0
        or os.environ.get("SMS_SECRETS_MODE") != "development"
        or not _safe_absolute(args.root)
        or not _safe_absolute(args.runtime_root)
    ):
        print("vendor runtime reset failed", file=sys.stderr)
        return 1
    try:
        result = VendorRuntimeResetManager(
            HostRuntimeResetOperations(
                root=args.root,
                runtime_root=args.runtime_root,
            )
        ).reset()
    except VendorRuntimeResetError:
        print("vendor runtime reset failed", file=sys.stderr)
        return 1
    print(json.dumps({"status": result.status}, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
