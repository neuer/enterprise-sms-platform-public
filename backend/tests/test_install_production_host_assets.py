from __future__ import annotations

import importlib
import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy/scripts"))


def installer_module() -> ModuleType:
    return importlib.import_module("install_production_host_assets")


COMMIT = "a" * 40


@dataclass(frozen=True)
class TrackedAsset:
    mode: str
    payload: bytes


class FakeRunner:
    def __init__(
        self,
        module: ModuleType,
        source_root: Path,
        tracked: dict[str, TrackedAsset],
    ) -> None:
        self.module = module
        self.source_root = source_root
        self.tracked = tracked
        self.calls: list[tuple[str, ...]] = []
        self.dirty = False
        self.storage_passed = True
        self.active_service: str | None = None
        self.docker_load_states = {
            "docker.service": "masked",
            "docker.socket": "masked",
            "containerd.service": "masked",
        }
        self.platform_load_state = "not-found"
        self.platform_absent_returncode = 4
        self.platform_absent_active_state = "unknown"
        self.docker_filesystem = "ext4"
        self.git_sudo_uids: list[str | None] = []

    def run(  # type: ignore[no-untyped-def]
        self,
        argv: tuple[str, ...],
        *,
        git_sudo_uid: str | None = None,
    ):
        self.calls.append(argv)
        if argv[:3] == (self.module.GIT_BINARY, "-C", str(self.source_root)):
            self.git_sudo_uids.append(git_sudo_uid)
            assert argv[3:7] == (
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
            )
            arguments = argv[7:]
            if arguments == ("rev-parse", "--verify", "HEAD^{commit}"):
                return self.module.CommandResult(0, f"{COMMIT}\n".encode())
            if arguments == ("cat-file", "-t", COMMIT):
                return self.module.CommandResult(0, b"commit\n")
            if arguments == ("status", "--porcelain=v1", "--untracked-files=all"):
                return self.module.CommandResult(0, b"?? unexpected\n" if self.dirty else b"")
            if arguments[:3] == ("ls-tree", "-z", COMMIT):
                relative = arguments[-1]
                item = self.tracked[relative]
                return self.module.CommandResult(
                    0,
                    f"{item.mode} blob {'b' * 40}\t{relative}\0".encode(),
                )
            if arguments[:2] == ("cat-file", "blob"):
                relative = arguments[-1].split(":", 1)[1]
                return self.module.CommandResult(0, self.tracked[relative].payload)
        assert git_sudo_uid is None
        if argv[:3] == (self.module.PYTHON_BINARY, "-I", "-c"):
            assert argv[3].encode() == self.tracked["deploy/scripts/storage_preflight.py"].payload
            assert argv[4:] == ("--mode", "startup")
            return self.module.CommandResult(0 if self.storage_passed else 1)
        if argv[:4] == (
            self.module.SYSTEMCTL_BINARY,
            "show",
            "--property=LoadState",
            "--value",
        ):
            state = self.docker_load_states.get(argv[4], self.platform_load_state)
            return self.module.CommandResult(0, f"{state}\n".encode())
        if argv[:2] == (self.module.SYSTEMCTL_BINARY, "is-active"):
            service = argv[2]
            if service == self.active_service:
                return self.module.CommandResult(0, b"active\n")
            if service == "sms-platform.service" and self.platform_load_state == "not-found":
                return self.module.CommandResult(
                    self.platform_absent_returncode,
                    f"{self.platform_absent_active_state}\n".encode(),
                )
            return self.module.CommandResult(3, b"inactive\n")
        if argv[:5] == (
            self.module.FINDMNT_BINARY,
            "--noheadings",
            "--output",
            "FSTYPE",
            "--target",
        ):
            return self.module.CommandResult(0, f"{self.docker_filesystem}\n".encode())
        raise AssertionError(f"unexpected command: {argv!r}")


@dataclass
class Fixture:
    module: ModuleType
    installer: object
    runner: FakeRunner
    source_root: Path
    etc_root: Path
    systemd_root: Path
    local_sbin_root: Path
    docker_root: Path
    state_path: Path


def make_fixture(tmp_path: Path, *, effective_uid: int = 0) -> Fixture:
    module = installer_module()
    source_root = tmp_path / "source"
    etc_root = tmp_path / "host/etc/sms-platform"
    systemd_root = tmp_path / "host/etc/systemd/system"
    local_sbin_root = tmp_path / "host/usr/local/sbin"
    docker_root = tmp_path / "host/var/lib/docker"
    systemd_root.mkdir(parents=True, mode=0o755)
    local_sbin_root.mkdir(parents=True, mode=0o755)
    docker_root.mkdir(parents=True, mode=0o711)
    docker_root.chmod(0o711)
    specs = module.build_asset_specs(
        source_root=source_root,
        etc_root=etc_root,
        systemd_root=systemd_root,
        local_sbin_root=local_sbin_root,
    )
    tracked: dict[str, TrackedAsset] = {}
    for spec in specs:
        source = source_root / spec.source_relative
        source.parent.mkdir(parents=True, exist_ok=True)
        payload = f"fixed {spec.name}\n".encode()
        source.write_bytes(payload)
        source.chmod(int(spec.git_mode[-3:], 8))
        tracked[spec.source_relative.as_posix()] = TrackedAsset(
            mode=spec.git_mode,
            payload=payload,
        )
    runner = FakeRunner(module, source_root, tracked)
    state_path = etc_root / "production-host-assets.json"
    installer = module.ProductionHostAssetInstaller(
        source_root=source_root,
        etc_root=etc_root,
        systemd_root=systemd_root,
        local_sbin_root=local_sbin_root,
        docker_root=docker_root,
        state_path=state_path,
        runner=runner,
        expected_uid=os.geteuid(),
        expected_gid=os.getegid(),
        effective_uid=effective_uid,
        invocation_environment={"SUDO_UID": str(os.geteuid())},
    )
    return Fixture(
        module=module,
        installer=installer,
        runner=runner,
        source_root=source_root,
        etc_root=etc_root,
        systemd_root=systemd_root,
        local_sbin_root=local_sbin_root,
        docker_root=docker_root,
        state_path=state_path,
    )


def apply(fixture: Fixture) -> dict[str, object]:
    return fixture.installer.apply(  # type: ignore[no-any-return,union-attr]
        COMMIT,
        confirm_dedicated_production_host=True,
        confirm_vcenter_storage_reviewed=True,
    )


def test_manifest_is_exactly_seventeen_regular_assets_and_one_wrapper_symlink(
    tmp_path: Path,
) -> None:
    fixture = make_fixture(tmp_path)
    specs = fixture.installer.assets  # type: ignore[union-attr]

    assert len(specs) == 18
    assert sum(spec.kind == "regular" for spec in specs) == 17
    symlinks = [spec for spec in specs if spec.kind == "symlink"]
    assert len(symlinks) == 1
    assert symlinks[0].destination == fixture.local_sbin_root / "sms-compose"
    assert symlinks[0].symlink_target == fixture.source_root / "deploy/sms-compose"
    assert {spec.destination.name for spec in specs if spec.kind == "regular"} >= {
        "compose.env",
        "sms-storage-preflight",
        "sms-platform.service",
        "vendor-control-agent.service",
        "lifecycle.json",
        "lifecycle.env",
    }


def test_plan_is_read_only_and_checks_commit_storage_and_inactive_services(
    tmp_path: Path,
) -> None:
    fixture = make_fixture(tmp_path)
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))

    assert fixture.installer.plan(COMMIT) == {  # type: ignore[union-attr]
        "action": "plan",
        "assets": 18,
        "source_commit": COMMIT,
        "status": "ready",
    }

    after = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    assert after == before
    assert (
        fixture.module.PYTHON_BINARY,
        "-I",
        "-c",
        fixture.runner.tracked["deploy/scripts/storage_preflight.py"].payload.decode(),
        "--mode",
        "startup",
    ) in fixture.runner.calls
    storage_command = next(
        call
        for call in fixture.runner.calls
        if call[:3] == (fixture.module.PYTHON_BINARY, "-I", "-c")
    )
    assert str(fixture.source_root / "deploy/scripts/storage_preflight.py") not in storage_command
    for unit in ("docker.service", "docker.socket", "containerd.service"):
        assert (
            fixture.module.SYSTEMCTL_BINARY,
            "show",
            "--property=LoadState",
            "--value",
            unit,
        ) in fixture.runner.calls
        assert (fixture.module.SYSTEMCTL_BINARY, "is-active", unit) in fixture.runner.calls
    assert (
        fixture.module.SYSTEMCTL_BINARY,
        "is-active",
        "sms-platform.service",
    ) in fixture.runner.calls
    assert (
        fixture.module.FINDMNT_BINARY,
        "--noheadings",
        "--output",
        "FSTYPE",
        "--target",
        str(fixture.docker_root),
    ) in fixture.runner.calls
    assert fixture.runner.git_sudo_uids
    assert set(fixture.runner.git_sudo_uids) == {str(fixture.source_root.lstat().st_uid)}


def test_plan_rejects_dirty_or_wrong_commit_checkout(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path)
    fixture.runner.dirty = True
    with pytest.raises(fixture.module.HostAssetInstallError, match="clean expected commit"):
        fixture.installer.plan(COMMIT)  # type: ignore[union-attr]

    fixture.runner.dirty = False
    with pytest.raises(fixture.module.HostAssetInstallError, match="expected commit"):
        fixture.installer.plan("A" * 40)  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "raw_sudo_uid",
    (None, "", "+501", " 501", "501 ", "0501", "uid501", str(2**32 - 1), "502"),
)
def test_sudo_uid_must_be_canonical_decimal_and_match_checkout_owner(
    raw_sudo_uid: str | None,
) -> None:
    module = installer_module()
    with pytest.raises(module.HostAssetInstallError, match="ownership is not trusted"):
        module._validated_git_sudo_uid(
            source_owner_uid=501,
            effective_uid=0,
            raw_sudo_uid=raw_sudo_uid,
        )


def test_sudo_uid_is_used_only_for_matching_non_root_checkout() -> None:
    module = installer_module()
    assert (
        module._validated_git_sudo_uid(
            source_owner_uid=501,
            effective_uid=0,
            raw_sudo_uid="501",
        )
        == "501"
    )
    assert (
        module._validated_git_sudo_uid(
            source_owner_uid=0,
            effective_uid=0,
            raw_sudo_uid="untrusted-value",
        )
        is None
    )
    assert (
        module._validated_git_sudo_uid(
            source_owner_uid=501,
            effective_uid=501,
            raw_sudo_uid="untrusted-value",
        )
        is None
    )


def test_plan_rejects_sudo_uid_mismatch_before_running_git(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path)
    fixture.installer.raw_sudo_uid = str(fixture.source_root.lstat().st_uid + 1)  # type: ignore[attr-defined]
    with pytest.raises(fixture.module.HostAssetInstallError, match="ownership is not trusted"):
        fixture.installer.plan(COMMIT)  # type: ignore[union-attr]
    assert fixture.runner.git_sudo_uids == []


def test_plan_rejects_source_bytes_or_source_symlink_not_in_commit(
    tmp_path: Path,
) -> None:
    fixture = make_fixture(tmp_path)
    source = fixture.source_root / "deploy/systemd/compose.env.example"
    source.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(fixture.module.HostAssetInstallError, match="bytes"):
        fixture.installer.plan(COMMIT)  # type: ignore[union-attr]

    source.unlink()
    source.symlink_to(fixture.source_root / "deploy/sms-compose")
    with pytest.raises(fixture.module.HostAssetInstallError, match="regular file"):
        fixture.installer.plan(COMMIT)  # type: ignore[union-attr]


def test_apply_requires_root_and_both_explicit_confirmations(tmp_path: Path) -> None:
    not_root = make_fixture(tmp_path / "not-root", effective_uid=1234)
    with pytest.raises(not_root.module.HostAssetInstallError, match="requires root"):
        apply(not_root)

    fixture = make_fixture(tmp_path / "confirm")
    with pytest.raises(fixture.module.HostAssetInstallError, match="both"):
        fixture.installer.apply(  # type: ignore[union-attr]
            COMMIT,
            confirm_dedicated_production_host=True,
            confirm_vcenter_storage_reviewed=False,
        )
    assert not fixture.etc_root.exists()


def test_apply_blocks_failed_storage_or_active_docker_without_writes(
    tmp_path: Path,
) -> None:
    storage = make_fixture(tmp_path / "storage")
    storage.runner.storage_passed = False
    with pytest.raises(storage.module.HostAssetInstallError, match="storage preflight"):
        apply(storage)
    assert not storage.etc_root.exists()

    active = make_fixture(tmp_path / "active")
    active.runner.active_service = "docker.service"
    with pytest.raises(active.module.HostAssetInstallError, match="inactive"):
        apply(active)
    assert not active.etc_root.exists()


def test_service_contract_accepts_masked_docker_and_absent_platform_only(
    tmp_path: Path,
) -> None:
    masked = make_fixture(tmp_path / "masked")
    masked.runner.platform_absent_returncode = 3
    masked.runner.platform_absent_active_state = "inactive"
    assert masked.installer.plan(COMMIT)["status"] == "ready"  # type: ignore[union-attr]

    loaded_platform = make_fixture(tmp_path / "loaded-platform")
    loaded_platform.runner.platform_load_state = "loaded"
    with pytest.raises(loaded_platform.module.HostAssetInstallError, match="unit must be absent"):
        loaded_platform.installer.plan(COMMIT)  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("unit", "fault"),
    tuple(
        (unit, fault)
        for unit in ("docker.service", "docker.socket", "containerd.service")
        for fault in ("not-masked", "active")
    ),
)
def test_each_docker_unit_must_be_masked_and_inactive_before_any_write(
    tmp_path: Path,
    unit: str,
    fault: str,
) -> None:
    fixture = make_fixture(tmp_path)
    if fault == "not-masked":
        fixture.runner.docker_load_states[unit] = "loaded"
    else:
        fixture.runner.active_service = unit
    with pytest.raises(fixture.module.HostAssetInstallError, match="masked and inactive"):
        apply(fixture)
    assert not fixture.etc_root.exists()


def test_empty_xfs_and_exact_ext4_lost_found_are_the_only_allowed_docker_roots(
    tmp_path: Path,
) -> None:
    xfs = make_fixture(tmp_path / "xfs")
    xfs.runner.docker_filesystem = "xfs"
    assert xfs.installer.plan(COMMIT)["status"] == "ready"  # type: ignore[union-attr]

    ext4 = make_fixture(tmp_path / "ext4")
    lost_found = ext4.docker_root / "lost+found"
    lost_found.mkdir(mode=0o700)
    lost_found.chmod(0o700)
    assert ext4.installer.plan(COMMIT)["status"] == "ready"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "fault",
    ("xfs-lost-found", "extra-file", "unsafe-lost-found", "recovered-entry"),
)
def test_nonempty_or_unsafe_docker_root_blocks_before_any_write(
    tmp_path: Path,
    fault: str,
) -> None:
    fixture = make_fixture(tmp_path)
    if fault == "xfs-lost-found":
        fixture.runner.docker_filesystem = "xfs"
        target = fixture.docker_root / "lost+found"
        target.mkdir(mode=0o700)
        target.chmod(0o700)
    elif fault == "extra-file":
        (fixture.docker_root / "docker-state").write_text("unexpected\n", encoding="utf-8")
    elif fault == "unsafe-lost-found":
        target = fixture.docker_root / "lost+found"
        target.mkdir(mode=0o755)
        target.chmod(0o755)
    else:
        target = fixture.docker_root / "lost+found"
        target.mkdir(mode=0o700)
        target.chmod(0o700)
        (target / "recovered-data").write_text("not empty\n", encoding="utf-8")
    with pytest.raises(fixture.module.HostAssetInstallError, match="Docker root"):
        apply(fixture)
    assert not fixture.etc_root.exists()


def test_docker_root_symlink_or_wrong_mode_blocks_before_any_write(tmp_path: Path) -> None:
    linked = make_fixture(tmp_path / "linked")
    linked.docker_root.rmdir()
    actual = linked.docker_root.parent / "actual-docker"
    actual.mkdir(mode=0o711)
    linked.docker_root.symlink_to(actual, target_is_directory=True)
    with pytest.raises(linked.module.HostAssetInstallError, match="symlink"):
        apply(linked)
    assert not linked.etc_root.exists()

    wrong_mode = make_fixture(tmp_path / "wrong-mode")
    wrong_mode.docker_root.chmod(0o755)
    with pytest.raises(wrong_mode.module.HostAssetInstallError, match="Docker root is unsafe"):
        apply(wrong_mode)
    assert not wrong_mode.etc_root.exists()


def test_apply_installs_exact_modes_symlink_and_commits_state_last(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = make_fixture(tmp_path)
    events: list[Path] = []
    real_regular = fixture.module._commit_regular_no_replace
    real_symlink = fixture.module._commit_symlink_no_replace

    def record_regular(temporary: Path, destination: Path) -> None:
        events.append(destination)
        real_regular(temporary, destination)

    def record_symlink(spec, *, expected_uid: int, expected_gid: int) -> None:  # type: ignore[no-untyped-def]
        events.append(spec.destination)
        real_symlink(spec, expected_uid=expected_uid, expected_gid=expected_gid)

    monkeypatch.setattr(fixture.module, "_commit_regular_no_replace", record_regular)
    monkeypatch.setattr(fixture.module, "_commit_symlink_no_replace", record_symlink)

    result = apply(fixture)

    assert result["status"] == "installed"
    assert events[-1] == fixture.state_path
    assert fixture.state_path.is_file()
    state = json.loads(fixture.state_path.read_text(encoding="ascii"))
    assert state["source_commit"] == COMMIT
    assert len(state["assets"]) == 18
    for spec in fixture.installer.assets:  # type: ignore[union-attr]
        metadata = spec.destination.lstat()
        assert metadata.st_uid == os.geteuid()
        assert metadata.st_gid == os.getegid()
        if spec.kind == "regular":
            assert stat.S_IMODE(metadata.st_mode) == spec.mode
            assert (
                spec.destination.read_bytes()
                == (fixture.source_root / spec.source_relative).read_bytes()
            )
        else:
            assert spec.destination.is_symlink()
            assert os.readlink(spec.destination) == str(spec.symlink_target)
    assert stat.S_IMODE(fixture.state_path.stat().st_mode) == 0o600
    assert fixture.installer.status()["status"] == "installed"  # type: ignore[union-attr]


def test_apply_never_invokes_mutating_host_or_git_commands(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path)
    apply(fixture)

    flattened = [" ".join(call) for call in fixture.runner.calls]
    assert not any(" fetch" in command for command in flattened)
    assert not any(" apt" in command or "mkfs" in command for command in flattened)
    assert not any(" mount" in command or "fstab" in command for command in flattened)
    assert not any("docker compose" in command for command in flattened)
    systemctl_calls = [
        call for call in fixture.runner.calls if call[0] == fixture.module.SYSTEMCTL_BINARY
    ]
    assert systemctl_calls
    assert all(call[1] in {"is-active", "show"} for call in systemctl_calls)


def test_apply_refuses_any_existing_destination_without_overwrite(
    tmp_path: Path,
) -> None:
    fixture = make_fixture(tmp_path)
    fixture.etc_root.mkdir(mode=0o700)
    target = fixture.etc_root / "compose.env"
    target.write_text("operator-owned\n", encoding="utf-8")
    target.chmod(0o600)

    with pytest.raises(fixture.module.HostAssetInstallError, match="already exists"):
        apply(fixture)

    assert target.read_text(encoding="utf-8") == "operator-owned\n"
    assert not fixture.state_path.exists()


def test_failed_publish_never_commits_state_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = make_fixture(tmp_path)
    real_commit = fixture.module._commit_regular_no_replace
    calls = 0

    def fail_second(temporary: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise fixture.module.HostAssetInstallError("simulated publish failure")
        real_commit(temporary, destination)

    monkeypatch.setattr(fixture.module, "_commit_regular_no_replace", fail_second)
    with pytest.raises(fixture.module.HostAssetInstallError, match="simulated"):
        apply(fixture)

    assert not fixture.state_path.exists()
    assert fixture.installer.status()["status"] == "incomplete"  # type: ignore[union-attr]


def test_wrapper_target_drift_before_state_prevents_success_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = make_fixture(tmp_path)
    real_symlink = fixture.module._commit_symlink_no_replace

    def publish_then_drift(spec, *, expected_uid: int, expected_gid: int) -> None:  # type: ignore[no-untyped-def]
        real_symlink(spec, expected_uid=expected_uid, expected_gid=expected_gid)
        spec.symlink_target.write_text("drift after symlink\n", encoding="utf-8")
        spec.symlink_target.chmod(0o755)

    monkeypatch.setattr(fixture.module, "_commit_symlink_no_replace", publish_then_drift)
    with pytest.raises(fixture.module.HostAssetInstallError, match="wrapper target has drifted"):
        apply(fixture)
    assert not fixture.state_path.exists()
    assert fixture.installer.status()["status"] == "incomplete"  # type: ignore[union-attr]


def test_post_state_drift_never_returns_installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = make_fixture(tmp_path)
    real_commit = fixture.module._commit_regular_no_replace

    def publish_then_drift(temporary: Path, destination: Path) -> None:
        real_commit(temporary, destination)
        if destination == fixture.state_path:
            target = fixture.source_root / "deploy/sms-compose"
            target.write_text("drift after state\n", encoding="utf-8")
            target.chmod(0o755)

    monkeypatch.setattr(fixture.module, "_commit_regular_no_replace", publish_then_drift)
    with pytest.raises(fixture.module.HostAssetInstallError, match="post-publish"):
        apply(fixture)
    assert fixture.state_path.exists()
    assert fixture.installer.status()["status"] == "drifted"  # type: ignore[union-attr]


def test_status_is_read_only_for_absent_incomplete_and_drifted_states(
    tmp_path: Path,
) -> None:
    absent = make_fixture(tmp_path / "absent")
    assert absent.installer.status() == {  # type: ignore[union-attr]
        "action": "status",
        "assets_present": 0,
        "status": "absent",
    }

    installed = make_fixture(tmp_path / "installed")
    apply(installed)
    target = installed.etc_root / "compose.env"
    target.write_text("drift\n", encoding="utf-8")
    target.chmod(0o600)
    assert installed.installer.status()["status"] == "drifted"  # type: ignore[union-attr]


@pytest.mark.parametrize("drift", ("content", "missing", "mode"))
def test_status_fails_closed_when_wrapper_target_drifts(
    tmp_path: Path,
    drift: str,
) -> None:
    fixture = make_fixture(tmp_path)
    apply(fixture)
    target = fixture.source_root / "deploy/sms-compose"
    if drift == "content":
        target.write_text("changed wrapper\n", encoding="utf-8")
        target.chmod(0o755)
    elif drift == "missing":
        target.unlink()
    else:
        target.chmod(0o644)

    assert fixture.installer.status()["status"] == "drifted"  # type: ignore[union-attr]


def test_cli_exposes_only_reviewed_arguments() -> None:
    module = installer_module()
    parser = module._parser()
    parsed = parser.parse_args(
        [
            "apply",
            "--expected-commit",
            COMMIT,
            "--confirm-dedicated-production-host",
            "--confirm-vcenter-storage-reviewed",
        ]
    )
    assert parsed.action == "apply"
    for forbidden in ("--root", "--force", "--skip-storage", "--source-root"):
        with pytest.raises(SystemExit):
            parser.parse_args(["status", forbidden])


@pytest.mark.parametrize(
    ("result", "expected"),
    (
        ({"action": "plan", "status": "ready"}, 0),
        ({"action": "apply", "status": "installed"}, 0),
        ({"action": "status", "status": "installed"}, 0),
        ({"action": "status", "status": "absent"}, 1),
        ({"action": "status", "status": "incomplete"}, 1),
        ({"action": "status", "status": "drifted"}, 1),
    ),
)
def test_cli_exit_code_is_zero_only_for_complete_success(
    result: dict[str, object], expected: int
) -> None:
    module = installer_module()
    assert module._result_exit_code(result) == expected


def test_status_absent_keeps_bounded_json_and_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = installer_module()

    class AbsentInstaller:
        def status(self) -> dict[str, object]:
            return {"action": "status", "assets_present": 0, "status": "absent"}

    monkeypatch.setattr(module, "ProductionHostAssetInstaller", AbsentInstaller)
    assert module.main(["status"]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "action": "status",
        "assets_present": 0,
        "status": "absent",
    }
    assert captured.err == ""


def test_blocked_cli_stderr_never_echoes_internal_reason(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = installer_module()

    class BlockedInstaller:
        def plan(self, _expected_commit: str) -> dict[str, object]:
            raise module.HostAssetInstallError("private path and command detail")

    monkeypatch.setattr(module, "ProductionHostAssetInstaller", BlockedInstaller)
    assert module.main(["plan", "--expected-commit", COMMIT]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"action": "plan", "status": "blocked"}
    assert "private" not in captured.err


def test_subprocess_runner_passes_sudo_uid_only_to_fixed_read_only_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = installer_module()
    environments: list[dict[str, str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        environments.append(dict(kwargs["env"]))  # type: ignore[arg-type]
        return subprocess.CompletedProcess(argv, 0, b"ok\n", b"")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    runner = module.SubprocessRunner()
    git_argv = (
        module.GIT_BINARY,
        "-C",
        "/opt/sms-platform",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    assert runner.run(git_argv, git_sudo_uid="501").returncode == 0
    assert environments[-1]["SUDO_UID"] == "501"

    assert runner.run((module.PYTHON_BINARY, "--version")).returncode == 0
    assert "SUDO_UID" not in environments[-1]
    calls_before = len(environments)
    assert runner.run((module.PYTHON_BINARY, "--version"), git_sudo_uid="501").returncode == 126
    assert len(environments) == calls_before


@pytest.mark.parametrize(
    ("stdout", "stderr"),
    (
        (b"x" * (2 * 1024 * 1024 + 1), b""),
        (b"", b"x" * (2 * 1024 * 1024 + 1)),
        (b"x" * (1024 * 1024 + 1), b"x" * (1024 * 1024 + 1)),
    ),
)
def test_subprocess_runner_rejects_oversized_captured_output(
    monkeypatch: pytest.MonkeyPatch,
    stdout: bytes,
    stderr: bytes,
) -> None:
    module = installer_module()

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 0, stdout, stderr)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    result = module.SubprocessRunner().run((module.PYTHON_BINARY, "--version"))
    assert result.returncode == module.COMMAND_OUTPUT_LIMIT_RETURN_CODE
    assert result.stdout == b""
    assert result.stderr == b""
