from __future__ import annotations

import json
import os
import socket
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy/scripts"))

NOW = datetime(2026, 7, 19, 12, tzinfo=UTC)
SENTINEL = "formal-key-private-13800138000-" + "a" * 64


def _write_state(
    path: Path,
    *,
    heartbeat_at: datetime,
    extra: dict[str, object] | None = None,
) -> None:
    document: dict[str, object] = {
        "schema_version": 1,
        "mode": "inactive",
        "heartbeat_at": heartbeat_at.isoformat(),
        "credential_configured": True,
        "active_recipient_count": 1,
        "pause_kind": None,
        "daily_limit": 100,
    }
    document.update(extra or {})
    temporary = path.with_suffix(".next")
    temporary.write_text(json.dumps(document) + "\n", encoding="utf-8")
    temporary.chmod(0o640)
    os.replace(temporary, path)


def _replace_socket(path: Path) -> socket.socket:
    if path.exists():
        path.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    os.chown(path, os.geteuid(), os.getegid())
    path.chmod(0o660)
    return server


def _socket_path() -> Path:
    socket_root = Path("/private/tmp")
    if not socket_root.is_dir():
        socket_root = Path("/tmp")
    return socket_root / f"vcr-{uuid.uuid4().hex[:12]}.sock"


class FakeSystemctl:
    def __init__(
        self,
        *,
        state_path: Path,
        socket_path: Path,
        heartbeat_at: datetime,
        fail_active: bool = False,
        state_extra: dict[str, object] | None = None,
    ) -> None:
        self.state_path = state_path
        self.socket_path = socket_path
        self.heartbeat_at = heartbeat_at
        self.fail_active = fail_active
        self.state_extra = state_extra
        self.calls: list[tuple[str, ...]] = []
        self.server: socket.socket | None = None

    def run(self, *argv: str) -> None:
        self.calls.append(argv)
        if argv == ("systemctl", "restart", "vendor-control-agent.service"):
            _write_state(
                self.state_path,
                heartbeat_at=self.heartbeat_at,
                extra=self.state_extra,
            )
            self.server = _replace_socket(self.socket_path)
            return
        if (
            argv == ("systemctl", "is-active", "--quiet", "vendor-control-agent.service")
            and self.fail_active
        ):
            raise RuntimeError("not active")

    def close(self) -> None:
        if self.server is not None:
            self.server.close()


def _reloader(
    tmp_path: Path,
    runner: FakeSystemctl,
    *,
    clock: datetime = NOW,
):
    from vendor_control_reload import VendorControlAgentReloader

    return VendorControlAgentReloader(
        state_path=runner.state_path,
        socket_path=runner.socket_path,
        expected_uid=os.geteuid(),
        expected_gid=os.getegid(),
        runner=runner,
        clock=lambda: clock,
        ready_timeout_s=0,
    )


def test_reload_restarts_then_requires_active_new_socket_and_fresh_safe_projection(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "control-state.json"
    socket_path = _socket_path()
    _write_state(state_path, heartbeat_at=NOW - timedelta(seconds=1))
    old_server = _replace_socket(socket_path)
    runner = FakeSystemctl(
        state_path=state_path,
        socket_path=socket_path,
        heartbeat_at=NOW,
    )

    try:
        result = _reloader(tmp_path, runner).run()
    finally:
        old_server.close()
        runner.close()
        socket_path.unlink(missing_ok=True)

    assert result == {"status": "ready"}
    assert runner.calls == [
        ("systemctl", "restart", "vendor-control-agent.service"),
        ("systemctl", "is-active", "--quiet", "vendor-control-agent.service"),
    ]
    assert SENTINEL not in json.dumps(result)


def test_reload_active_failure_stops_before_ready_probe(tmp_path: Path) -> None:
    state_path = tmp_path / "control-state.json"
    socket_path = _socket_path()
    _write_state(state_path, heartbeat_at=NOW - timedelta(seconds=1))
    old_server = _replace_socket(socket_path)
    runner = FakeSystemctl(
        state_path=state_path,
        socket_path=socket_path,
        heartbeat_at=NOW,
        fail_active=True,
    )

    try:
        with pytest.raises(RuntimeError, match="reload"):
            _reloader(tmp_path, runner).run()
    finally:
        old_server.close()
        runner.close()
        socket_path.unlink(missing_ok=True)

    assert runner.calls[-1] == (
        "systemctl",
        "is-active",
        "--quiet",
        "vendor-control-agent.service",
    )


@pytest.mark.parametrize(
    ("heartbeat_at", "extra"),
    [
        (NOW - timedelta(seconds=31), None),
        (NOW, {"unexpected": SENTINEL}),
        (NOW, {"schema_version": True}),
    ],
)
def test_reload_ready_failure_is_fail_closed_without_projection_disclosure(
    tmp_path: Path,
    heartbeat_at: datetime,
    extra: dict[str, object] | None,
) -> None:
    state_path = tmp_path / "control-state.json"
    socket_path = _socket_path()
    _write_state(state_path, heartbeat_at=NOW - timedelta(seconds=1))
    old_server = _replace_socket(socket_path)
    runner = FakeSystemctl(
        state_path=state_path,
        socket_path=socket_path,
        heartbeat_at=heartbeat_at,
        state_extra=extra,
    )

    try:
        with pytest.raises(RuntimeError) as error:
            _reloader(tmp_path, runner).run()
    finally:
        old_server.close()
        runner.close()
        socket_path.unlink(missing_ok=True)

    assert "ready" in str(error.value)
    assert SENTINEL not in str(error.value)
