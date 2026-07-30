#!/usr/bin/env python3
"""从纯 Mock 单机环境一次性准备页面联调控制代理。"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

_COMPOSE_ENV_FIELDS = frozenset(
    {
        "SMS_PLATFORM_ROOT",
        "SMS_SECRETS_MODE",
        "SMS_RUNTIME_ROOT",
        "SMS_VENDOR_CREDENTIAL_ROOT",
        "SMS_VENDOR_TEST_STATE_DIR",
        "SMS_VENDOR_CONTROL_SOCKET_DIR",
    }
)
_MOCK_ENV_FIELDS = frozenset(
    {
        "ENVIRONMENT",
        "DEBUG",
        "AUTH_MOCK",
        "VENDOR_MOCK",
        "VENDOR_BASE_URL",
        "COMPOSE_PROFILES",
    }
)
_STATE_FIELDS = frozenset(
    {
        "schema_version",
        "mode",
        "heartbeat_at",
        "credential_configured",
        "active_recipient_count",
        "pause_kind",
        "daily_limit",
    }
)
_LINE_RE = re.compile(r"([A-Z][A-Z0-9_]*)=(.*)")


class SystemctlRunner(Protocol):
    def run(self, *argv: str) -> None: ...


class SubprocessSystemctlRunner:
    def run(self, *argv: str) -> None:
        subprocess.run(
            list(argv),
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _read_key_values(path: Path, *, strip_comments: bool) -> dict[str, str]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("vendor test bootstrap configuration is unavailable") from exc
    values: dict[str, str] = {}
    for source_line in raw.splitlines():
        line = source_line.strip()
        if not line or line.startswith("#"):
            continue
        if strip_comments:
            line = line.split("#", 1)[0].strip()
        match = _LINE_RE.fullmatch(line)
        if match is None or match[1] in values:
            raise RuntimeError("vendor test bootstrap configuration is invalid")
        values[match[1]] = match[2].strip()
    return values


def _require_private_file(path: Path, *, expected_uid: int, mode: int) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RuntimeError("vendor test bootstrap file is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != expected_uid
        or stat.S_IMODE(metadata.st_mode) != mode
    ):
        raise RuntimeError("vendor test bootstrap file is unsafe")


def _secure_directory(
    path: Path,
    *,
    expected_uid: int,
    expected_gid: int,
    mode: int,
) -> None:
    try:
        path.mkdir(mode=mode, parents=True, exist_ok=True)
        metadata = path.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise OSError("not a directory")
        os.chown(path, expected_uid, expected_gid, follow_symlinks=False)
        os.chmod(path, mode, follow_symlinks=False)
        metadata = path.lstat()
    except OSError as exc:
        raise RuntimeError("vendor test bootstrap directory is unsafe") from exc
    if (
        (metadata.st_uid, metadata.st_gid) != (expected_uid, expected_gid)
        or stat.S_IMODE(metadata.st_mode) != mode
    ):
        raise RuntimeError("vendor test bootstrap directory is unsafe")


def _secure_fixed_directory(
    path: Path,
    *,
    expected_uid: int,
    expected_gid: int,
    mode: int,
) -> None:
    """仅创建缺失目录；既有目录任何权限漂移都 fail closed。"""

    created = False
    try:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            path.mkdir(mode=mode, parents=True)
            created = True
            metadata = path.lstat()
        if created:
            os.chown(path, expected_uid, expected_gid, follow_symlinks=False)
            os.chmod(path, mode, follow_symlinks=False)
            metadata = path.lstat()
    except OSError as exc:
        raise RuntimeError("vendor test bootstrap directory is unsafe") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or (metadata.st_uid, metadata.st_gid) != (expected_uid, expected_gid)
        or stat.S_IMODE(metadata.st_mode) != mode
    ):
        raise RuntimeError("vendor test bootstrap directory is unsafe")


class VendorTestConsoleBootstrap:
    """只在 Mock 仍开启且 unit 未漂移时启动 root 控制代理。"""

    def __init__(
        self,
        *,
        root: Path,
        compose_env: Path,
        installed_unit: Path,
        state_dir: Path,
        socket_dir: Path,
        marker_file: Path,
        backup_dir: Path,
        expected_uid: int,
        backend_gid: int,
        runner: SystemctlRunner,
    ) -> None:
        self.root = root
        self.compose_env = compose_env
        self.installed_unit = installed_unit
        self.state_dir = state_dir
        self.socket_dir = socket_dir
        self.marker_file = marker_file
        self.backup_dir = backup_dir
        self.expected_uid = expected_uid
        self.backend_gid = backend_gid
        self.runner = runner

    def _validate_configuration(self) -> None:
        dotenv = self.root / ".env"
        source_unit = self.root / "deploy/systemd/vendor-control-agent.service"
        _require_private_file(dotenv, expected_uid=self.expected_uid, mode=0o600)
        _require_private_file(
            self.compose_env,
            expected_uid=self.expected_uid,
            mode=0o600,
        )
        _require_private_file(
            self.installed_unit,
            expected_uid=self.expected_uid,
            mode=0o644,
        )
        try:
            if self.installed_unit.read_bytes() != source_unit.read_bytes():
                raise RuntimeError("vendor test bootstrap unit has drifted")
        except OSError as exc:
            raise RuntimeError("vendor test bootstrap unit is unavailable") from exc

        mock_values = _read_key_values(dotenv, strip_comments=True)
        selected_mock = {key: mock_values.get(key) for key in _MOCK_ENV_FIELDS}
        if selected_mock != {
            "ENVIRONMENT": "development",
            "DEBUG": "1",
            "AUTH_MOCK": "1",
            "VENDOR_MOCK": "1",
            "VENDOR_BASE_URL": "http://mock-vendor:9028",
            "COMPOSE_PROFILES": "dev",
        }:
            raise RuntimeError("vendor test bootstrap requires pure Mock mode")

        compose_values = _read_key_values(self.compose_env, strip_comments=False)
        expected_compose = {
            "SMS_PLATFORM_ROOT": str(self.root),
            "SMS_SECRETS_MODE": "development",
            "SMS_RUNTIME_ROOT": "/run/sms-platform/secrets",
            "SMS_VENDOR_CREDENTIAL_ROOT": str(self.state_dir / "credentials"),
            "SMS_VENDOR_TEST_STATE_DIR": str(self.state_dir),
            "SMS_VENDOR_CONTROL_SOCKET_DIR": str(self.socket_dir),
        }
        if set(compose_values) != _COMPOSE_ENV_FIELDS or compose_values != expected_compose:
            raise RuntimeError("vendor test bootstrap host configuration is invalid")
        if self.marker_file.exists() or self.marker_file.is_symlink():
            raise RuntimeError("vendor test bootstrap requires inactive Mock state")
        active_credentials = self.state_dir / "credentials/active"
        if active_credentials.exists() or active_credentials.is_symlink():
            raise RuntimeError("vendor test bootstrap credentials already exist")

    def _verify_agent(self) -> dict[str, object]:
        socket_path = self.socket_dir / "vendor-control.sock"
        state_path = self.state_dir / "control-state.json"
        try:
            socket_info = socket_path.lstat()
            state_info = state_path.lstat()
            document = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("vendor test bootstrap agent state is unavailable") from exc
        if (
            not stat.S_ISSOCK(socket_info.st_mode)
            or stat.S_ISLNK(socket_info.st_mode)
            or (socket_info.st_uid, socket_info.st_gid)
            != (self.expected_uid, self.backend_gid)
            or stat.S_IMODE(socket_info.st_mode) != 0o660
            or not stat.S_ISREG(state_info.st_mode)
            or stat.S_ISLNK(state_info.st_mode)
            or (state_info.st_uid, state_info.st_gid)
            != (self.expected_uid, self.backend_gid)
            or stat.S_IMODE(state_info.st_mode) != 0o640
            or type(document) is not dict
            or set(document) != _STATE_FIELDS
        ):
            raise RuntimeError("vendor test bootstrap agent state is unsafe")
        try:
            heartbeat = datetime.fromisoformat(
                str(document["heartbeat_at"]).replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise RuntimeError("vendor test bootstrap heartbeat is invalid") from exc
        age = (datetime.now(UTC) - heartbeat.astimezone(UTC)).total_seconds()
        if (
            document["schema_version"] != 1
            or document["mode"] != "setup_required"
            or document["credential_configured"] is not False
            or type(document["active_recipient_count"]) is not int
            or document["active_recipient_count"] < 0
            or document["pause_kind"] is not None
            or document["daily_limit"] != 100
            or heartbeat.tzinfo is None
            or age < -5
            or age > 30
        ):
            raise RuntimeError("vendor test bootstrap agent state is invalid")
        return {
            "credential_configured": False,
            "mode": "setup_required",
            "status": "prepared",
        }

    def run(self) -> dict[str, object]:
        self._validate_configuration()
        _secure_directory(
            self.state_dir,
            expected_uid=self.expected_uid,
            expected_gid=self.backend_gid,
            mode=0o710,
        )
        _secure_directory(
            self.socket_dir,
            expected_uid=self.expected_uid,
            expected_gid=self.backend_gid,
            mode=0o750,
        )
        _secure_fixed_directory(
            self.backup_dir,
            expected_uid=self.expected_uid,
            expected_gid=0 if self.expected_uid == 0 else os.getegid(),
            mode=0o700,
        )
        try:
            self.runner.run("systemctl", "daemon-reload")
            self.runner.run(
                "systemctl",
                "enable",
                "--now",
                "vendor-control-agent.service",
            )
            self.runner.run(
                "systemctl",
                "is-active",
                "--quiet",
                "vendor-control-agent.service",
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("vendor test bootstrap agent start failed") from exc
        deadline = time.monotonic() + 10
        while True:
            try:
                return self._verify_agent()
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("vendor test bootstrap agent did not become ready") from None
                time.sleep(0.1)


def main() -> int:
    if os.geteuid() != 0 or os.environ.get("SMS_SECRETS_MODE") != "development":
        print("vendor-test bootstrap blocked", file=sys.stderr)
        return 1
    bootstrap = VendorTestConsoleBootstrap(
        root=Path("/opt/sms-platform"),
        compose_env=Path("/etc/sms-platform/compose.env"),
        installed_unit=Path("/etc/systemd/system/vendor-control-agent.service"),
        state_dir=Path("/var/lib/sms-platform/vendor-test"),
        socket_dir=Path("/run/sms-platform/vendor-control"),
        marker_file=Path("/etc/sms-platform/test-environment"),
        backup_dir=Path("/var/lib/sms-platform/test-backups"),
        expected_uid=0,
        backend_gid=10001,
        runner=SubprocessSystemctlRunner(),
    )
    try:
        print(json.dumps(bootstrap.run(), separators=(",", ":"), sort_keys=True))
        return 0
    except Exception:
        print("vendor-test bootstrap blocked", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
