from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy/scripts"))

import install_production_control_bootstrap as module  # noqa: E402

COMMIT = "a" * 40
BOOTSTRAP = b"reviewed bootstrap\n"
MANAGER = b"#!/usr/bin/python3 -I\nprint('manager')\n"
LAUNCHER = b"#!/usr/bin/python3 -I\nprint('launcher')\n"


class FakeRunner:
    def __init__(self, *, launcher: Path) -> None:
        self.launcher = launcher
        self.assets = {
            module.BOOTSTRAP_SOURCE: ("100644", BOOTSTRAP),
            module.MANAGER_SOURCE: ("100644", MANAGER),
            module.LAUNCHER_SOURCE: ("100755", LAUNCHER),
        }
        self.objects: dict[str, bytes] = {}
        self.calls: list[
            tuple[
                tuple[str, ...],
                dict[str, str],
                tuple[int, ...],
                int | None,
                int | None,
                tuple[int, ...] | None,
            ]
        ] = []
        self.fail_manager_command: str | None = None
        self.manager_launcher_states: list[bool] = []

    @staticmethod
    def _object_id(mode: str, payload: bytes) -> str:
        return hashlib.sha1(  # noqa: S324 - fake Git object identity
            mode.encode("ascii") + b"\0" + payload
        ).hexdigest()

    def run(
        self,
        argv: Sequence[str],
        *,
        environment: Mapping[str, str],
        pass_fds: Sequence[int] = (),
        user: int | None = None,
        group: int | None = None,
        extra_groups: Sequence[int] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        call = tuple(argv)
        self.calls.append(
            (
                call,
                dict(environment),
                tuple(pass_fds),
                user,
                group,
                None if extra_groups is None else tuple(extra_groups),
            )
        )
        if call[0] == module.GIT:
            assert (user, group, extra_groups) == (
                module.OPERATOR_UID,
                module.OPERATOR_GID,
                (),
            )
            assert environment == module._git_environment()
            arguments = call[call.index("-C") + 2 :]
            if arguments == ("cat-file", "-t", COMMIT):
                return subprocess.CompletedProcess(call, 0, b"commit\n", b"")
            if arguments[:2] == ("ls-tree", "-z"):
                source = arguments[4]
                mode, payload = self.assets[source]
                object_id = self._object_id(mode, payload)
                self.objects[object_id] = payload
                output = (
                    f"{mode} blob {object_id}\t{source}".encode("ascii") + b"\0"
                )
                return subprocess.CompletedProcess(call, 0, output, b"")
            if arguments[:2] == ("cat-file", "-s"):
                payload = self.objects[arguments[2]]
                return subprocess.CompletedProcess(
                    call, 0, f"{len(payload)}\n".encode("ascii"), b""
                )
            if arguments[:2] == ("cat-file", "blob"):
                return subprocess.CompletedProcess(
                    call, 0, self.objects[arguments[2]], b""
                )
            raise AssertionError(f"unexpected Git command: {call}")

        assert call[:3] == (
            module.PYTHON,
            "-I",
            str(call[2]),
        )
        command = call[3]
        self.manager_launcher_states.append(self.launcher.is_symlink())
        assert user is None and group is None and extra_groups is None
        assert len(pass_fds) == 1
        assert environment["SMS_LIFECYCLE_LOCKED"] == "1"
        assert environment["SMS_LIFECYCLE_LOCK_FD"] == str(pass_fds[0])
        if command == self.fail_manager_command:
            return subprocess.CompletedProcess(call, 2, b"", b"failed\n")
        if command == "prepare":
            result = {"commit": COMMIT, "status": "prepared"}
        else:
            result = {
                "commit": COMMIT,
                "current_target": f"versions/{COMMIT}",
                "status": "active",
            }
        return subprocess.CompletedProcess(
            call,
            0,
            (json.dumps(result, sort_keys=True) + "\n").encode("ascii"),
            b"",
        )


@dataclass
class Fixture:
    installer: module.ProductionControlBootstrapInstaller
    runner: FakeRunner
    bootstrap: Path
    approval_root: Path
    approval_marker: Path
    repository: Path
    repository_metadata: os.stat_result
    manager: Path
    launcher: Path
    lifecycle_lock: Path
    legacy_intents: tuple[Path, ...]


def _write(path: Path, payload: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(mode)


def make_fixture(
    tmp_path: Path,
    *,
    confirmed: bool = True,
    loaded_from_fixed_path: bool = True,
    observed_repository_uid: int = module.REPOSITORY_UID,
    observed_repository_gid: int = module.REPOSITORY_GID,
    observed_repository_mode: int = module.REPOSITORY_MODE,
) -> Fixture:
    uid = os.geteuid()
    gid = os.getegid()
    repository = tmp_path / "opt/sms-platform"
    repository.mkdir(parents=True)
    repository.chmod(0o770)
    repository_metadata = os.stat_result(
        (
            stat.S_IFDIR | observed_repository_mode,
            1,
            1,
            2,
            observed_repository_uid,
            observed_repository_gid,
            0,
            0,
            0,
            0,
        )
    )
    libexec = tmp_path / "usr/local/libexec/sms-platform"
    sbin = tmp_path / "usr/local/sbin"
    etc = tmp_path / "etc/sms-platform"
    for directory in (libexec, sbin, etc):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o755)

    bootstrap = libexec / "production-control-bootstrap"
    manager = libexec / "production-control-snapshot"
    launcher = sbin / "sms-compose"
    legacy_target = repository / "deploy/sms-compose"
    _write(bootstrap, BOOTSTRAP, 0o555)
    os.symlink(legacy_target, launcher)

    approval_root = etc / "production-control-approved"
    approval_root.mkdir()
    approval_root.chmod(0o755)
    approval_marker = approval_root / COMMIT
    _write(approval_marker, f"{COMMIT}\n".encode("ascii"), 0o444)

    lifecycle_lock = tmp_path / "run/sms-platform/secrets.lifecycle.lock"
    _write(lifecycle_lock, b"lock\n", 0o600)
    legacy_intents = (
        etc / "production-host-assets.intent.json",
        etc / "production-host-assets.upgrade.intent.json",
    )
    runner = FakeRunner(launcher=launcher)
    installer = module.ProductionControlBootstrapInstaller(
        repository=repository,
        approval_root=approval_root,
        bootstrap_executable=bootstrap,
        loaded_from=(bootstrap if loaded_from_fixed_path else repository / "bootstrap.py"),
        manager_destination=manager,
        launcher_destination=launcher,
        legacy_launcher_target=legacy_target,
        lifecycle_lock=lifecycle_lock,
        legacy_intents=legacy_intents,
        runner=runner,
        uid=uid,
        gid=gid,
        effective_uid=uid,
        operator_uid=module.OPERATOR_UID,
        operator_gid=module.OPERATOR_GID,
        repository_uid=module.REPOSITORY_UID,
        repository_gid=module.REPOSITORY_GID,
        repository_mode=module.REPOSITORY_MODE,
        repository_metadata=repository_metadata,
        confirmed=confirmed,
    )
    return Fixture(
        installer=installer,
        runner=runner,
        bootstrap=bootstrap,
        approval_root=approval_root,
        approval_marker=approval_marker,
        repository=repository,
        repository_metadata=repository_metadata,
        manager=manager,
        launcher=launcher,
        lifecycle_lock=lifecycle_lock,
        legacy_intents=legacy_intents,
    )


def _manager_commands(fixture: Fixture) -> list[str]:
    return [
        call[0][3]
        for call in fixture.runner.calls
        if call[0][0] == module.PYTHON
    ]


def test_plan_is_read_only_and_verifies_git_as_fixed_operator(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path)

    result = fixture.installer.plan(COMMIT)

    assert result == {
        "action": "plan",
        "commit": COMMIT,
        "launcher": "legacy",
        "manager": "absent",
        "status": "ready",
    }
    assert fixture.launcher.is_symlink()
    assert not fixture.manager.exists()
    assert _manager_commands(fixture) == []
    assert all(
        call[3:6] == (module.OPERATOR_UID, module.OPERATOR_GID, ())
        for call in fixture.runner.calls
    )


def test_approval_marker_is_required_before_git_and_is_never_created(
    tmp_path: Path,
) -> None:
    fixture = make_fixture(tmp_path)
    fixture.approval_marker.unlink()

    with pytest.raises(
        module.ProductionControlBootstrapError,
        match="approval marker is unsafe or unavailable",
    ):
        fixture.installer.apply(COMMIT)

    assert fixture.runner.calls == []
    assert not fixture.approval_marker.exists()
    assert not fixture.manager.exists()
    assert fixture.launcher.is_symlink()


def test_apply_rechecks_approval_under_lock_before_any_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = make_fixture(tmp_path)
    real_assets = fixture.installer._assets

    def assets_then_revoke(commit: str) -> tuple[module.Asset, module.Asset]:
        assets = real_assets(commit)
        fixture.approval_marker.unlink()
        return assets

    monkeypatch.setattr(fixture.installer, "_assets", assets_then_revoke)
    with pytest.raises(
        module.ProductionControlBootstrapError,
        match="approval marker is unsafe or unavailable",
    ):
        fixture.installer.apply(COMMIT)

    assert fixture.runner.calls
    assert not fixture.manager.exists()
    assert fixture.launcher.is_symlink()
    assert not fixture.approval_marker.exists()


def test_approval_directory_and_marker_metadata_are_exact(tmp_path: Path) -> None:
    unsafe_directory = make_fixture(tmp_path / "directory")
    unsafe_directory.approval_root.chmod(0o775)
    with pytest.raises(
        module.ProductionControlBootstrapError,
        match="required directory is unsafe",
    ):
        unsafe_directory.installer.plan(COMMIT)
    assert unsafe_directory.runner.calls == []

    unsafe_mode = make_fixture(tmp_path / "mode")
    unsafe_mode.approval_marker.chmod(0o644)
    with pytest.raises(
        module.ProductionControlBootstrapError,
        match="approval marker is unsafe or unavailable",
    ):
        unsafe_mode.installer.plan(COMMIT)
    assert unsafe_mode.runner.calls == []

    hardlinked = make_fixture(tmp_path / "hardlink")
    os.link(
        hardlinked.approval_marker,
        hardlinked.approval_root / "duplicate-marker",
    )
    with pytest.raises(
        module.ProductionControlBootstrapError,
        match="approval marker is unsafe or unavailable",
    ):
        hardlinked.installer.plan(COMMIT)
    assert hardlinked.runner.calls == []


def test_approval_marker_rejects_symlink_and_wrong_content_before_git(
    tmp_path: Path,
) -> None:
    symlinked = make_fixture(tmp_path / "symlink")
    symlinked.approval_marker.unlink()
    target = symlinked.approval_root / "marker-target"
    _write(target, f"{COMMIT}\n".encode("ascii"), 0o444)
    os.symlink(target, symlinked.approval_marker)
    with pytest.raises(
        module.ProductionControlBootstrapError,
        match="approval marker is unsafe or unavailable",
    ):
        symlinked.installer.plan(COMMIT)
    assert symlinked.runner.calls == []

    wrong_content = make_fixture(tmp_path / "content")
    wrong_content.approval_marker.chmod(0o644)
    wrong_content.approval_marker.write_text(f"{COMMIT} \\n", encoding="ascii")
    wrong_content.approval_marker.chmod(0o444)
    with pytest.raises(
        module.ProductionControlBootstrapError,
        match="approval marker content is invalid",
    ):
        wrong_content.installer.plan(COMMIT)
    assert wrong_content.runner.calls == []


def test_repository_contract_is_root_operator_setgid_2770_and_exact(
    tmp_path: Path,
) -> None:
    assert (
        module.REPOSITORY_UID,
        module.REPOSITORY_GID,
        module.REPOSITORY_MODE,
    ) == (0, module.OPERATOR_GID, 0o2770)
    fixture = make_fixture(tmp_path / "valid")
    assert stat.S_IMODE(fixture.repository_metadata.st_mode) == 0o2770
    assert fixture.repository_metadata.st_uid == module.REPOSITORY_UID
    assert fixture.repository_metadata.st_gid == module.OPERATOR_GID
    assert fixture.installer.repository_uid == module.REPOSITORY_UID
    assert fixture.installer.repository_gid == module.REPOSITORY_GID
    assert fixture.installer.operator_uid == module.OPERATOR_UID
    assert fixture.installer.operator_gid == module.OPERATOR_GID
    assert fixture.installer.plan(COMMIT)["status"] == "ready"

    for name, mode in (("missing-setgid", 0o770), ("world-writable", 0o2772)):
        unsafe = make_fixture(
            tmp_path / name,
            observed_repository_mode=mode,
        )
        with pytest.raises(
            module.ProductionControlBootstrapError,
            match="production repository metadata is unsafe",
        ):
            unsafe.installer.plan(COMMIT)
        assert unsafe.runner.calls == []

    wrong_owner = make_fixture(
        tmp_path / "wrong-owner",
        observed_repository_uid=module.OPERATOR_UID,
    )
    with pytest.raises(
        module.ProductionControlBootstrapError,
        match="production repository metadata is unsafe",
    ):
        wrong_owner.installer.plan(COMMIT)
    assert wrong_owner.runner.calls == []


def test_bootstrap_must_run_from_reviewed_root_owned_blob(tmp_path: Path) -> None:
    wrong_path = make_fixture(tmp_path / "wrong-path", loaded_from_fixed_path=False)
    with pytest.raises(
        module.ProductionControlBootstrapError,
        match="fixed root-owned path",
    ):
        wrong_path.installer.plan(COMMIT)
    assert wrong_path.runner.calls == []

    wrong_blob = make_fixture(tmp_path / "wrong-blob")
    wrong_blob.runner.assets[module.BOOTSTRAP_SOURCE] = ("100644", b"other\n")
    with pytest.raises(
        module.ProductionControlBootstrapError,
        match="does not match",
    ):
        wrong_blob.installer.plan(COMMIT)


def test_apply_requires_one_explicit_confirmation_before_git(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path, confirmed=False)

    with pytest.raises(
        module.ProductionControlBootstrapError,
        match="confirmation is required",
    ):
        fixture.installer.apply(COMMIT)

    assert fixture.runner.calls == []
    assert fixture.launcher.is_symlink()


def test_apply_installs_manager_activates_snapshot_then_replaces_launcher_last(
    tmp_path: Path,
) -> None:
    fixture = make_fixture(tmp_path)

    result = fixture.installer.apply(COMMIT)

    assert result == {"action": "apply", "commit": COMMIT, "status": "installed"}
    assert _manager_commands(fixture) == ["prepare", "activate", "status"]
    assert fixture.runner.manager_launcher_states == [True, True, True]
    assert {call[0][0] for call in fixture.runner.calls} == {
        module.GIT,
        module.PYTHON,
    }
    assert fixture.manager.read_bytes() == MANAGER
    assert fixture.launcher.read_bytes() == LAUNCHER
    assert not fixture.launcher.is_symlink()
    for path in (fixture.manager, fixture.launcher):
        metadata = path.lstat()
        assert metadata.st_uid == os.geteuid()
        assert metadata.st_gid == os.getegid()
        assert stat.S_IMODE(metadata.st_mode) == 0o555


def test_manager_failure_leaves_legacy_launcher_and_rerun_is_idempotent(
    tmp_path: Path,
) -> None:
    fixture = make_fixture(tmp_path)
    fixture.runner.fail_manager_command = "status"

    with pytest.raises(
        module.ProductionControlBootstrapError,
        match="manager status failed",
    ):
        fixture.installer.apply(COMMIT)

    assert fixture.manager.read_bytes() == MANAGER
    assert fixture.launcher.is_symlink()
    fixture.runner.fail_manager_command = None

    assert fixture.installer.apply(COMMIT)["status"] == "installed"
    assert fixture.installer.apply(COMMIT)["status"] == "installed"
    assert not fixture.launcher.is_symlink()


def test_manager_publication_failure_leaves_no_destination_and_retry_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = make_fixture(tmp_path)
    real_replace = os.replace
    failed = False

    def fail_manager_once(source: Path, destination: Path) -> None:
        nonlocal failed
        if Path(destination) == fixture.manager and not failed:
            failed = True
            raise OSError("synthetic manager publication failure")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_manager_once)
    with pytest.raises(
        module.ProductionControlBootstrapError,
        match="manager publication failed",
    ):
        fixture.installer.apply(COMMIT)

    assert not fixture.manager.exists()
    assert fixture.launcher.is_symlink()
    assert fixture.installer.apply(COMMIT)["status"] == "installed"
    assert fixture.manager.lstat().st_nlink == 1


def test_orphaned_manager_stage_does_not_block_atomic_retry(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path)
    orphan = fixture.manager.parent / f".{fixture.manager.name}-crash-residue"
    _write(orphan, MANAGER, 0o555)

    assert fixture.installer.apply(COMMIT)["status"] == "installed"

    assert orphan.exists()
    assert fixture.manager.lstat().st_nlink == 1
    assert orphan.lstat().st_ino != fixture.manager.lstat().st_ino
    assert not fixture.launcher.is_symlink()


def test_launcher_replace_failure_is_safe_and_resumable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = make_fixture(tmp_path)
    real_replace = os.replace

    def fail_replace(source: Path, destination: Path) -> None:
        if Path(destination) == fixture.launcher:
            raise OSError("synthetic replace failure")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(
        module.ProductionControlBootstrapError,
        match="launcher publication failed",
    ):
        fixture.installer.apply(COMMIT)

    assert fixture.launcher.is_symlink()
    assert _manager_commands(fixture) == ["prepare", "activate", "status"]
    monkeypatch.setattr(os, "replace", real_replace)
    assert fixture.installer.apply(COMMIT)["status"] == "installed"


def test_status_proves_installed_blobs_and_active_snapshot_without_prepare(
    tmp_path: Path,
) -> None:
    fixture = make_fixture(tmp_path)
    fixture.installer.apply(COMMIT)
    fixture.runner.calls.clear()

    result = fixture.installer.status(COMMIT)

    assert result == {
        "action": "status",
        "commit": COMMIT,
        "status": "installed",
    }
    assert _manager_commands(fixture) == ["status"]


def test_status_fails_closed_on_asset_drift(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path)
    fixture.installer.apply(COMMIT)
    fixture.manager.chmod(0o755)

    with pytest.raises(
        module.ProductionControlBootstrapError,
        match="snapshot manager destination is not adoptable",
    ):
        fixture.installer.status(COMMIT)


def test_legacy_intent_or_unknown_launcher_blocks_before_manager_install(
    tmp_path: Path,
) -> None:
    intent = make_fixture(tmp_path / "intent")
    _write(intent.legacy_intents[1], b"{}\n", 0o600)
    with pytest.raises(
        module.ProductionControlBootstrapError,
        match="transaction is incomplete",
    ):
        intent.installer.apply(COMMIT)
    assert not intent.manager.exists()
    assert _manager_commands(intent) == []

    unknown = make_fixture(tmp_path / "unknown")
    unknown.launcher.unlink()
    os.symlink("/tmp/not-reviewed", unknown.launcher)
    with pytest.raises(
        module.ProductionControlBootstrapError,
        match="neither the exact legacy link",
    ):
        unknown.installer.apply(COMMIT)
    assert not unknown.manager.exists()


def test_completed_legacy_host_state_commit_is_not_reinterpreted(
    tmp_path: Path,
) -> None:
    fixture = make_fixture(tmp_path)
    legacy_state = fixture.legacy_intents[0].parent / "production-host-assets.json"
    _write(
        legacy_state,
        b'{"schema_version":1,"source_commit":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}\n',
        0o600,
    )

    assert fixture.installer.apply(COMMIT)["status"] == "installed"
    assert legacy_state.read_bytes().endswith(b"\n")


def test_manager_commands_share_the_outer_lifecycle_lock(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path)

    fixture.installer.apply(COMMIT)

    manager_calls = [
        call for call in fixture.runner.calls if call[0][0] == module.PYTHON
    ]
    assert len(manager_calls) == 3
    inherited = {call[2] for call in manager_calls}
    assert len(inherited) == 1
    assert next(iter(inherited))[0] >= 0
    assert all(
        call[1]["SMS_LIFECYCLE_LOCK_FD"] == str(call[2][0])
        for call in manager_calls
    )


def test_parser_exposes_only_plan_apply_status_and_one_confirmation() -> None:
    parser = module._parser()
    planned = parser.parse_args(["plan", "--expected-commit", COMMIT])
    applied = parser.parse_args(
        [
            "apply",
            "--expected-commit",
            COMMIT,
            "--confirm-reviewed-control-bootstrap",
        ]
    )
    status = parser.parse_args(["status", "--expected-commit", COMMIT])

    assert planned.action == "plan"
    assert applied.confirm_reviewed_control_bootstrap is True
    assert status.action == "status"


def test_subprocess_runner_uses_no_shell_and_fixed_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        captured["argv"] = argv
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, b"ok\n", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    environment = module._git_environment()
    result = module.SubprocessRunner().run(
        (module.GIT, "cat-file", "-t", COMMIT),
        environment=environment,
        user=module.OPERATOR_UID,
        group=module.OPERATOR_GID,
        extra_groups=(),
    )

    assert result.returncode == 0
    assert captured["shell"] is False
    assert captured["env"] == environment
    assert captured["user"] == module.OPERATOR_UID
    assert captured["group"] == module.OPERATOR_GID
    assert captured["extra_groups"] == ()
