from __future__ import annotations

import hashlib
import importlib
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy/scripts"))


def manager_module() -> ModuleType:
    try:
        return importlib.import_module("test_secure_access_manager")
    except ModuleNotFoundError:
        pytest.fail("test secure access manager is not implemented")


class FakeRunner:
    def __init__(self) -> None:
        self.states = {
            "sms-platform.service": "active",
            "sms-platform-test-secure-access.service": "inactive",
        }
        self.calls: list[tuple[tuple[str, ...], bool]] = []
        self.on_start: Any = None
        self.fail_actions: set[str] = set()
        self.results = {
            "sms-platform-test-secure-access.service": "success",
        }
        self.started_monotonic_us = {
            "sms-platform-test-secure-access.service": 9_099_000_000,
        }

    def run(
        self,
        *argv: str,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, check))
        returncode = 0
        stdout = ""
        action = argv[1] if len(argv) > 1 else ""
        if action in self.fail_actions:
            returncode = 1
        elif argv[:2] == ("systemctl", "is-active"):
            state = self.states.get(argv[2], "inactive")
            stdout = f"{state}\n"
            returncode = 0 if state in {"active", "activating"} else 3
        elif argv[:2] == ("systemctl", "start"):
            self.states[argv[2]] = "active"
            if self.on_start is not None:
                self.on_start()
        elif argv[:2] == ("systemctl", "stop"):
            self.states[argv[2]] = "inactive"
        elif argv[:3] == ("systemctl", "show", "--property=Result"):
            stdout = f"{self.results.get(argv[4], 'success')}\n"
        elif argv[:3] == (
            "systemctl",
            "show",
            "--property=ExecMainStartTimestampMonotonic",
        ):
            stdout = f"{self.started_monotonic_us.get(argv[4], 0)}\n"
        result = subprocess.CompletedProcess(argv, returncode, stdout, "")
        if check and returncode != 0:
            raise subprocess.CalledProcessError(returncode, argv)
        return result


class FakeProbe:
    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.calls = 0

    def check(self) -> bool:
        self.calls += 1
        return self.available


class FakePublicProbe:
    def __init__(self, results: list[bool] | None = None) -> None:
        self.results = results or [True]
        self.calls: list[str] = []

    def check(self, url: str) -> bool:
        self.calls.append(url)
        if len(self.results) > 1:
            return self.results.pop(0)
        return self.results[0]


def fixture(tmp_path: Path):
    module = manager_module()
    root = tmp_path / "host-assets"
    root.mkdir()
    source_unit = root / "sms-platform-test-secure-access.service"
    source_unit.write_text("[Service]\nExecStart=fixed\n", encoding="utf-8")
    source_unit.chmod(0o644)
    for name in (
        "install_test_secure_access.py",
        "test_secure_access_contract.py",
        "test_secure_access_runtime.py",
        "test_secure_access_manager.py",
        "vendor_test_files.py",
        "check_test_update_migration.py",
        "run_with_lifecycle_lock.py",
        "public_cutover_bootstrap.py",
        "test_update_apply.py",
        "test_update_backup.py",
        "test_update_contract.py",
        "test_update_manager.py",
        "test_update_promote.py",
        "test_update_store.py",
        "test_update_verify.py",
        "check_public_readiness.py",
        "export_public_snapshot.py",
        "verify_public_snapshot_cutover.py",
    ):
        path = root / name
        path.write_text(f"# fixed {name}\n", encoding="utf-8")
        path.chmod(0o644)
    bootstrap_wrapper = root / "sms-compose-bootstrap"
    bootstrap_wrapper.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    bootstrap_wrapper.chmod(0o755)
    installed_unit = tmp_path / "sms-platform-test-secure-access.service"
    installed_unit.write_bytes(source_unit.read_bytes())
    installed_unit.chmod(0o644)
    binary = tmp_path / "cloudflared"
    binary.write_bytes(b"fixed-cloudflared-test-binary")
    binary.chmod(0o755)
    from test_secure_access_contract import HOST_ASSET_NAMES, serialize_host_manifest

    installed_assets = {
        name: (
            binary
            if name == "cloudflared"
            else root / name
        )
        for name in HOST_ASSET_NAMES
    }
    manifest = root / "manifest.json"
    manifest.write_text(
        serialize_host_manifest(
            {
                name: hashlib.sha256(path.read_bytes()).hexdigest()
                for name, path in installed_assets.items()
            },
            source_commit="a" * 40,
        ),
        encoding="utf-8",
    )
    manifest.chmod(0o644)
    status_path = tmp_path / "runtime/status.json"
    runner = FakeRunner()
    probe = FakeProbe()
    public_probe = FakePublicProbe()
    now = datetime(2026, 7, 19, 9, 0, tzinfo=UTC)
    secure_manager = module.SecureAccessManager(
        root=root,
        binary_path=binary,
        installed_unit=installed_unit,
        manifest_path=manifest,
        status_path=status_path,
        expected_uid=os.geteuid(),
        expected_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
        runner=runner,
        probe=probe,
        public_probe=public_probe,
        clock=lambda: now,
        monotonic_clock=lambda: 10_000.0,
        sleeper=lambda _seconds: None,
        ready_timeout_seconds=0.03,
    )
    return module, secure_manager, runner, probe, public_probe, status_path, now


def write_state(module: ModuleType, path: Path, now: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        module.serialize_ready_state(
            url="https://safe-name.trycloudflare.com",
            started_at=now,
            expires_at=now + timedelta(seconds=900),
        ),
        encoding="utf-8",
    )
    path.chmod(0o640)


def write_expired_state(module: ModuleType, path: Path, now: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        module.serialize_ready_state(
            url="https://safe-name.trycloudflare.com",
            started_at=now - timedelta(seconds=901),
            expires_at=now - timedelta(seconds=1),
        ),
        encoding="utf-8",
    )
    path.chmod(0o640)


def test_start_checks_fixed_platform_and_origin_then_returns_ready(
    tmp_path: Path,
) -> None:
    module, secure_manager, runner, probe, public_probe, status_path, now = fixture(
        tmp_path
    )
    runner.on_start = lambda: write_state(module, status_path, now)

    result = secure_manager.start()

    assert result.as_dict() == {
        "status": "ready",
        "url": "https://safe-name.trycloudflare.com",
        "expires_at": "2026-07-19T09:15:00+00:00",
    }
    assert probe.calls == 1
    assert public_probe.calls == ["https://safe-name.trycloudflare.com"]
    assert (
        ("systemctl", "is-active", "sms-platform.service"),
        False,
    ) in runner.calls
    assert (
        ("systemctl", "start", "sms-platform-test-secure-access.service"),
        True,
    ) in runner.calls
    assert all("shell" not in " ".join(argv) for argv, _check in runner.calls)


def test_repeated_start_is_idempotent_and_never_starts_a_second_tunnel(
    tmp_path: Path,
) -> None:
    module, secure_manager, runner, probe, public_probe, status_path, now = fixture(
        tmp_path
    )
    runner.states["sms-platform-test-secure-access.service"] = "active"
    write_state(module, status_path, now)

    first = secure_manager.start()
    second = secure_manager.start()

    assert first == second
    assert probe.calls == 0
    assert len(public_probe.calls) == 2
    assert not any(argv[:2] == ("systemctl", "start") for argv, _ in runner.calls)


@pytest.mark.parametrize(
    "unsafe",
    [
        "binary-mode",
        "binary-symlink",
        "hash",
        "unit-mode",
        "unit-drift",
        "runtime-drift",
        "manifest-mode",
    ],
)
def test_start_rejects_unsafe_or_drifted_host_assets_before_systemctl_start(
    tmp_path: Path,
    unsafe: str,
) -> None:
    module, secure_manager, runner, _, _, _, _ = fixture(tmp_path)
    if unsafe == "binary-mode":
        secure_manager.binary_path.chmod(0o775)
    elif unsafe == "binary-symlink":
        target = secure_manager.binary_path.with_suffix(".real")
        secure_manager.binary_path.rename(target)
        secure_manager.binary_path.symlink_to(target)
    elif unsafe == "hash":
        secure_manager.binary_path.write_bytes(b"drift")
        secure_manager.binary_path.chmod(0o755)
    elif unsafe == "unit-mode":
        secure_manager.installed_unit.chmod(0o664)
    elif unsafe == "unit-drift":
        secure_manager.installed_unit.write_text("drift\n", encoding="utf-8")
        secure_manager.installed_unit.chmod(0o644)
    elif unsafe == "runtime-drift":
        (secure_manager.root / "test_secure_access_runtime.py").write_text(
            "drift\n",
            encoding="utf-8",
        )
    else:
        secure_manager.manifest_path.chmod(0o664)

    with pytest.raises(module.SecureAccessManagerError, match="asset"):
        secure_manager.start()

    assert not any(argv[:2] == ("systemctl", "start") for argv, _ in runner.calls)


@pytest.mark.parametrize("platform_state,probe_available", [("inactive", True), ("active", False)])
def test_start_requires_active_platform_and_fixed_loopback_web(
    tmp_path: Path,
    platform_state: str,
    probe_available: bool,
) -> None:
    module, secure_manager, runner, probe, _, _, _ = fixture(tmp_path)
    runner.states["sms-platform.service"] = platform_state
    probe.available = probe_available

    with pytest.raises(module.SecureAccessManagerError, match="unavailable"):
        secure_manager.start()

    assert not any(argv[:2] == ("systemctl", "start") for argv, _ in runner.calls)


def test_start_timeout_stops_unit_resets_failure_and_cleans_state(
    tmp_path: Path,
) -> None:
    module, secure_manager, runner, _, _, status_path, _ = fixture(tmp_path)

    with pytest.raises(module.SecureAccessManagerError, match="ready"):
        secure_manager.start()

    assert (
        ("systemctl", "stop", "sms-platform-test-secure-access.service"),
        True,
    ) in runner.calls
    assert (
        ("systemctl", "reset-failed", "sms-platform-test-secure-access.service"),
        False,
    ) in runner.calls
    assert not status_path.exists()


@pytest.mark.parametrize(
    "service_state,with_ready,expected",
    [
        ("inactive", False, "inactive"),
        ("activating", False, "starting"),
        ("active", False, "starting"),
        ("active", True, "ready"),
        ("failed", False, "failed"),
    ],
)
def test_status_returns_only_the_fixed_public_shape(
    tmp_path: Path,
    service_state: str,
    with_ready: bool,
    expected: str,
) -> None:
    module, secure_manager, runner, _, _, status_path, now = fixture(tmp_path)
    runner.states["sms-platform-test-secure-access.service"] = service_state
    if with_ready:
        write_state(module, status_path, now)

    result = secure_manager.status().as_dict()

    assert result["status"] == expected
    assert set(result) == {"status", "url", "expires_at"}
    if expected != "ready":
        assert result["url"] is None
        assert result["expires_at"] is None


def test_stop_is_idempotent_and_removes_only_runtime_state(tmp_path: Path) -> None:
    module, secure_manager, runner, _, _, status_path, now = fixture(tmp_path)
    runner.states["sms-platform-test-secure-access.service"] = "active"
    write_state(module, status_path, now)

    first = secure_manager.stop()
    second = secure_manager.stop()

    assert first.as_dict() == {
        "status": "inactive",
        "url": None,
        "expires_at": None,
    }
    assert second == first
    assert not status_path.exists()
    assert sum(argv[:2] == ("systemctl", "stop") for argv, _ in runner.calls) == 2


def test_stop_accepts_reset_failed_not_loaded_after_confirmed_stop(
    tmp_path: Path,
) -> None:
    module, secure_manager, runner, _, _, status_path, now = fixture(tmp_path)
    runner.states["sms-platform-test-secure-access.service"] = "active"
    runner.fail_actions.add("reset-failed")
    write_state(module, status_path, now)

    result = secure_manager.stop()

    assert result.status == "inactive"
    assert not status_path.exists()
    assert (
        ("systemctl", "reset-failed", "sms-platform-test-secure-access.service"),
        False,
    ) in runner.calls


def test_stop_failure_preserves_state_and_never_claims_inactive(
    tmp_path: Path,
) -> None:
    module, secure_manager, runner, _, _, status_path, now = fixture(tmp_path)
    runner.states["sms-platform-test-secure-access.service"] = "active"
    runner.fail_actions.add("stop")
    write_state(module, status_path, now)

    with pytest.raises(module.SecureAccessManagerError, match="stop"):
        secure_manager.stop()

    assert status_path.exists()
    assert runner.states["sms-platform-test-secure-access.service"] == "active"


def test_expired_failed_unit_is_cleaned_and_restarted_in_one_call(
    tmp_path: Path,
) -> None:
    module, secure_manager, runner, probe, _, status_path, now = fixture(tmp_path)
    runner.states["sms-platform-test-secure-access.service"] = "failed"
    write_expired_state(module, status_path, now)
    runner.on_start = lambda: write_state(module, status_path, now)

    result = secure_manager.start()

    assert result.status == "ready"
    assert probe.calls == 1
    assert (
        ("systemctl", "reset-failed", "sms-platform-test-secure-access.service"),
        False,
    ) in runner.calls
    assert (
        ("systemctl", "start", "sms-platform-test-secure-access.service"),
        True,
    ) in runner.calls


def test_runtime_max_timeout_without_preserved_state_restarts_in_one_call(
    tmp_path: Path,
) -> None:
    module, secure_manager, runner, probe, _, status_path, now = fixture(tmp_path)
    runner.states["sms-platform-test-secure-access.service"] = "failed"
    runner.results["sms-platform-test-secure-access.service"] = "timeout"
    runner.on_start = lambda: write_state(module, status_path, now)

    result = secure_manager.start()

    assert result.status == "ready"
    assert probe.calls == 1
    assert (
        (
            "systemctl",
            "show",
            "--property=Result",
            "--value",
            "sms-platform-test-secure-access.service",
        ),
        False,
    ) in runner.calls
    assert (
        (
            "systemctl",
            "show",
            "--property=ExecMainStartTimestampMonotonic",
            "--value",
            "sms-platform-test-secure-access.service",
        ),
        False,
    ) in runner.calls
    assert (
        ("systemctl", "start", "sms-platform-test-secure-access.service"),
        True,
    ) in runner.calls


def test_recent_timeout_without_state_is_failed_not_assumed_expired(
    tmp_path: Path,
) -> None:
    module, secure_manager, runner, _, _, _, _ = fixture(tmp_path)
    runner.states["sms-platform-test-secure-access.service"] = "failed"
    runner.results["sms-platform-test-secure-access.service"] = "timeout"
    runner.started_monotonic_us["sms-platform-test-secure-access.service"] = (
        9_990_000_000
    )

    assert secure_manager.status().status == "failed"
    with pytest.raises(module.SecureAccessManagerError, match="unavailable"):
        secure_manager.start()
    assert (
        ("systemctl", "start", "sms-platform-test-secure-access.service"),
        True,
    ) not in runner.calls


def test_start_waits_for_public_https_reachability_before_returning_url(
    tmp_path: Path,
) -> None:
    module, secure_manager, runner, _, public_probe, status_path, now = fixture(
        tmp_path
    )
    public_probe.results = [False, True]
    runner.on_start = lambda: write_state(module, status_path, now)

    result = secure_manager.start()

    assert result.status == "ready"
    assert public_probe.calls == [
        "https://safe-name.trycloudflare.com",
        "https://safe-name.trycloudflare.com",
    ]


def test_fresh_start_allows_public_dns_propagation_before_first_probe(
    tmp_path: Path,
) -> None:
    module, secure_manager, runner, _, public_probe, status_path, now = fixture(
        tmp_path
    )
    sleeps: list[float] = []
    secure_manager.sleeper = sleeps.append
    runner.on_start = lambda: write_state(module, status_path, now)

    result = secure_manager.start()

    assert result.status == "ready"
    assert sleeps[0] == module.PUBLIC_PROBE_INITIAL_DELAY_SECONDS
    assert module.PUBLIC_PROBE_INITIAL_DELAY_SECONDS >= 5.0
    assert module.READY_TIMEOUT_SECONDS >= 60.0
    assert public_probe.calls == ["https://safe-name.trycloudflare.com"]


def test_start_fails_closed_when_public_https_never_becomes_reachable(
    tmp_path: Path,
) -> None:
    module, secure_manager, runner, _, public_probe, status_path, now = fixture(
        tmp_path
    )
    public_probe.results = [False]
    runner.on_start = lambda: write_state(module, status_path, now)

    with pytest.raises(module.SecureAccessManagerError, match="ready"):
        secure_manager.start()

    assert public_probe.calls
    assert runner.states["sms-platform-test-secure-access.service"] == "inactive"
    assert not status_path.exists()


def test_oversized_runtime_state_is_rejected_without_reading_unbounded_data(
    tmp_path: Path,
) -> None:
    _, secure_manager, runner, _, _, status_path, _ = fixture(tmp_path)
    runner.states["sms-platform-test-secure-access.service"] = "active"
    status_path.parent.mkdir(parents=True)
    status_path.write_bytes(b"x" * 4097)
    status_path.chmod(0o640)

    assert secure_manager.status().status == "failed"


def test_test_host_marker_is_required_and_strict(tmp_path: Path) -> None:
    module = manager_module()
    marker = tmp_path / "test-host"
    marker.write_text("{}\n", encoding="utf-8")
    marker.chmod(0o600)

    with pytest.raises(module.SecureAccessManagerError, match="test host"):
        module.require_test_host_marker(
            marker,
            expected_uid=os.geteuid(),
        )

    from test_secure_access_contract import serialize_test_host_marker

    marker.write_text(serialize_test_host_marker(), encoding="utf-8")
    marker.chmod(0o600)
    module.require_test_host_marker(marker, expected_uid=os.geteuid())


@pytest.mark.parametrize(
    "argv,euid,mode",
    [
        (["start"], 501, "development"),
        (["start"], 0, "production"),
        (["start", "--origin", "http://evil"], 0, "development"),
        (["shell"], 0, "development"),
        ([], 0, "development"),
    ],
)
def test_cli_rejects_non_root_production_and_arbitrary_passthrough(
    argv: list[str],
    euid: int,
    mode: str,
) -> None:
    module = manager_module()

    with pytest.raises(module.SecureAccessManagerError, match="invocation"):
        module.parse_manager_action(argv, euid=euid, mode=mode)


@pytest.mark.parametrize("action", ["start", "status", "stop"])
def test_cli_accepts_only_three_fixed_development_actions(action: str) -> None:
    module = manager_module()

    assert module.parse_manager_action([action], euid=0, mode="development") == action


def test_internal_asset_verification_action_is_not_publicly_exposed() -> None:
    module = manager_module()

    with pytest.raises(module.SecureAccessManagerError, match="invocation"):
        module.parse_manager_action(
            ["verify-assets"],
            euid=0,
            mode="development",
        )

    assert (
        module.parse_manager_action(
            ["verify-assets"],
            euid=0,
            mode="development",
            internal=True,
        )
        == "verify-assets"
    )
