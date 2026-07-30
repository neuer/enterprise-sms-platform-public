from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))

NOW = datetime(2026, 7, 17, 9, tzinfo=UTC)
SENTINEL = "formal-key-private-13800138000-" + "a" * 64


def _write_state(path: Path, *, heartbeat_at: datetime = NOW) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "controlled",
                "heartbeat_at": heartbeat_at.isoformat(),
                "credential_configured": True,
                "active_recipient_count": 1,
                "pause_kind": None,
                "daily_limit": 100,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o640)


def test_reader_accepts_exact_fresh_root_group_read_only_projection(tmp_path: Path) -> None:
    from app.services.vendor_control_state import VendorControlStateGuard

    path = tmp_path / "control-state.json"
    _write_state(path)
    guard = VendorControlStateGuard(
        path,
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        clock=lambda: NOW + timedelta(seconds=30),
    )

    state = guard.require_fresh()

    assert state.mode == "controlled"
    assert state.active_recipient_count == 1
    assert state.daily_limit == 100


def test_fresh_reader_accepts_inactive_state_but_rejects_stale_projection(
    tmp_path: Path,
) -> None:
    from app.services.vendor_control_state import (
        VendorControlStateGuard,
        VendorControlStateUnavailable,
    )

    path = tmp_path / "control-state.json"
    _write_state(path, heartbeat_at=NOW)
    document = json.loads(path.read_text(encoding="utf-8"))
    document.update(
        {
            "mode": "inactive",
            "credential_configured": True,
            "active_recipient_count": 0,
        }
    )
    path.write_text(json.dumps(document) + "\n", encoding="utf-8")
    path.chmod(0o640)
    guard = VendorControlStateGuard(
        path,
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        clock=lambda: NOW + timedelta(seconds=30),
    )

    assert guard.read_fresh().mode == "inactive"

    stale = VendorControlStateGuard(
        path,
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        clock=lambda: NOW + timedelta(seconds=31),
    )
    with pytest.raises(VendorControlStateUnavailable) as captured:
        stale.read_fresh()
    assert captured.value.requires_critical_pause is True


@pytest.mark.parametrize("age_seconds", [31, 300])
def test_reader_rejects_stale_heartbeat_after_thirty_seconds(
    tmp_path: Path,
    age_seconds: int,
) -> None:
    from app.services.vendor_control_state import (
        VendorControlStateGuard,
        VendorControlStateUnavailable,
    )

    path = tmp_path / "control-state.json"
    _write_state(path, heartbeat_at=NOW - timedelta(seconds=age_seconds))

    with pytest.raises(VendorControlStateUnavailable) as captured:
        VendorControlStateGuard(
            path,
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
            clock=lambda: NOW,
        ).require_fresh()

    assert captured.value.requires_critical_pause is True
    assert SENTINEL not in str(captured.value)


def test_reader_rejects_missing_unknown_fields_and_unsafe_mode(tmp_path: Path) -> None:
    from app.services.vendor_control_state import (
        VendorControlStateGuard,
        VendorControlStateUnavailable,
    )

    missing = tmp_path / "missing.json"
    guard = VendorControlStateGuard(
        missing,
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        clock=lambda: NOW,
    )
    with pytest.raises(VendorControlStateUnavailable) as missing_error:
        guard.require_fresh()
    assert missing_error.value.requires_critical_pause is True

    invalid = tmp_path / "invalid.json"
    _write_state(invalid)
    document = json.loads(invalid.read_text(encoding="utf-8"))
    document["secret_key"] = SENTINEL
    invalid.write_text(json.dumps(document), encoding="utf-8")
    invalid.chmod(0o640)
    with pytest.raises(VendorControlStateUnavailable):
        VendorControlStateGuard(
            invalid,
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
            clock=lambda: NOW,
        ).require_fresh()


def test_agent_writer_uses_0640_exact_safe_fields(tmp_path: Path) -> None:
    import vendor_control_agent as agent_module

    path = tmp_path / "control-state.json"
    agent_module.write_control_state(
        path,
        mode="controlled",
        heartbeat_at=NOW,
        credential_configured=True,
        active_recipient_count=1,
        pause_kind=None,
        backend_gid=os.getgid(),
        expected_uid=os.getuid(),
    )

    document = json.loads(path.read_text(encoding="utf-8"))
    assert set(document) == {
        "schema_version",
        "mode",
        "heartbeat_at",
        "credential_configured",
        "active_recipient_count",
        "pause_kind",
        "daily_limit",
    }
    assert document["daily_limit"] == 100
    assert SENTINEL not in path.read_text(encoding="utf-8")
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


@pytest.mark.asyncio
async def test_agent_heartbeat_loop_writes_then_waits_exactly_ten_seconds() -> None:
    import vendor_control_agent as agent_module

    events: list[object] = []

    class Agent:
        def write_heartbeat(self) -> None:
            events.append("write")

    async def stop_after_first_interval(seconds: float) -> None:
        events.append(seconds)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await agent_module.heartbeat_loop(Agent(), sleeper=stop_after_first_interval)

    assert events == ["write", 10]
