from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy/scripts"))


class FakeSystemctl:
    def __init__(
        self,
        *,
        socket_path: Path,
        state_path: Path,
        uid: int,
        gid: int,
    ) -> None:
        self.socket_path = socket_path
        self.state_path = state_path
        self.uid = uid
        self.gid = gid
        self.calls: list[tuple[str, ...]] = []

    def run(self, *argv: str) -> None:
        self.calls.append(argv)
        if argv == ("systemctl", "enable", "--now", "vendor-control-agent.service"):
            control_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                control_socket.bind(str(self.socket_path))
            finally:
                control_socket.close()
            os.chown(self.socket_path, self.uid, self.gid)
            self.socket_path.chmod(0o660)
            self.state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "mode": "setup_required",
                        "heartbeat_at": datetime.now(UTC).isoformat(),
                        "credential_configured": False,
                        "active_recipient_count": 0,
                        "pause_kind": None,
                        "daily_limit": 100,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            os.chown(self.state_path, self.uid, self.gid)
            self.state_path.chmod(0o640)


def fixture(tmp_path: Path, request: pytest.FixtureRequest):
    from vendor_test_bootstrap import VendorTestConsoleBootstrap

    uid, gid = os.geteuid(), os.getegid()
    root = tmp_path / "platform"
    (root / "deploy/systemd").mkdir(parents=True)
    source_unit = root / "deploy/systemd/vendor-control-agent.service"
    source_unit.write_text("[Unit]\nDescription=fixed agent\n", encoding="utf-8")
    installed_unit = tmp_path / "vendor-control-agent.service"
    installed_unit.write_text(source_unit.read_text(encoding="utf-8"), encoding="utf-8")
    installed_unit.chmod(0o644)
    dotenv = root / ".env"
    dotenv.write_text(
        "ENVIRONMENT=development\nDEBUG=1\nAUTH_MOCK=1\nVENDOR_MOCK=1\n"
        "VENDOR_BASE_URL=http://mock-vendor:9028\nCOMPOSE_PROFILES=dev\n",
        encoding="utf-8",
    )
    dotenv.chmod(0o600)
    state_dir = tmp_path / "state"
    socket_dir = Path("/tmp") / f"sms-vtb-{uuid.uuid4().hex[:12]}"
    request.addfinalizer(lambda: shutil.rmtree(socket_dir, ignore_errors=True))
    compose_env = tmp_path / "compose.env"
    compose_env.write_text(
        f"SMS_PLATFORM_ROOT={root}\n"
        "SMS_SECRETS_MODE=development\n"
        "SMS_RUNTIME_ROOT=/run/sms-platform/secrets\n"
        f"SMS_VENDOR_CREDENTIAL_ROOT={state_dir / 'credentials'}\n"
        f"SMS_VENDOR_TEST_STATE_DIR={state_dir}\n"
        f"SMS_VENDOR_CONTROL_SOCKET_DIR={socket_dir}\n",
        encoding="utf-8",
    )
    compose_env.chmod(0o600)
    marker = tmp_path / "test-environment"
    backup_dir = tmp_path / "test-backups"
    runner = FakeSystemctl(
        socket_path=socket_dir / "vendor-control.sock",
        state_path=state_dir / "control-state.json",
        uid=uid,
        gid=gid,
    )
    bootstrap = VendorTestConsoleBootstrap(
        root=root,
        compose_env=compose_env,
        installed_unit=installed_unit,
        state_dir=state_dir,
        socket_dir=socket_dir,
        marker_file=marker,
        backup_dir=backup_dir,
        expected_uid=uid,
        backend_gid=gid,
        runner=runner,
    )
    return bootstrap, runner, dotenv, installed_unit, state_dir, socket_dir, backup_dir


def test_mock_bootstrap_prepares_fixed_paths_starts_agent_and_verifies_heartbeat(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    bootstrap, runner, _, _, state_dir, socket_dir, backup_dir = fixture(
        tmp_path,
        request,
    )

    result = bootstrap.run()

    assert result == {
        "credential_configured": False,
        "mode": "setup_required",
        "status": "prepared",
    }
    assert runner.calls == [
        ("systemctl", "daemon-reload"),
        ("systemctl", "enable", "--now", "vendor-control-agent.service"),
        ("systemctl", "is-active", "--quiet", "vendor-control-agent.service"),
    ]
    assert (state_dir.stat().st_uid, state_dir.stat().st_gid) == (
        os.geteuid(),
        os.getegid(),
    )
    assert (state_dir.stat().st_mode & 0o777) == 0o710
    assert (socket_dir.stat().st_mode & 0o777) == 0o750
    assert (backup_dir.stat().st_mode & 0o777) == 0o700
    assert (socket_dir / "vendor-control.sock").is_socket()
    assert "secret" not in json.dumps(result).casefold()
    assert "phone" not in json.dumps(result).casefold()


@pytest.mark.parametrize("unsafe", ["live", "unit-drift", "marker"])
def test_bootstrap_rejects_non_mock_or_drifted_host_before_systemctl(
    tmp_path: Path,
    unsafe: str,
    request: pytest.FixtureRequest,
) -> None:
    bootstrap, runner, dotenv, installed_unit, *_ = fixture(tmp_path, request)
    if unsafe == "live":
        dotenv.write_text(
            "DEBUG=1\nAUTH_MOCK=1\nVENDOR_MOCK=0\n"
            "VENDOR_BASE_URL=https://vendor.example.invalid\nCOMPOSE_PROFILES=\n",
            encoding="utf-8",
        )
    elif unsafe == "unit-drift":
        installed_unit.write_text("[Unit]\nDescription=drifted\n", encoding="utf-8")
    else:
        bootstrap.marker_file.write_text("{}\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="bootstrap"):
        bootstrap.run()

    assert runner.calls == []


def test_bootstrap_rejects_checkpoint_directory_mode_drift(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    bootstrap, runner, *_, backup_dir = fixture(tmp_path, request)
    backup_dir.mkdir(mode=0o755)

    with pytest.raises(RuntimeError, match="directory is unsafe"):
        bootstrap.run()

    assert backup_dir.stat().st_mode & 0o777 == 0o755
    assert runner.calls == []
