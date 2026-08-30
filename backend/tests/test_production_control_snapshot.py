from __future__ import annotations

import fcntl
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))

import production_control_snapshot as snapshot_module  # noqa: E402
from production_control_snapshot import (  # noqa: E402
    SNAPSHOT_MANIFEST_NAME,
    ProductionControlSnapshot,
    ProductionControlSnapshotError,
    SnapshotPaths,
)

launcher_loader = importlib.machinery.SourceFileLoader(
    "production_sms_compose_launcher",
    str(ROOT / "deploy" / "production-sms-compose-launcher"),
)
launcher_spec = importlib.util.spec_from_loader(launcher_loader.name, launcher_loader)
assert launcher_spec is not None
launcher_module = importlib.util.module_from_spec(launcher_spec)
launcher_loader.exec_module(launcher_module)

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
COMMIT_C = "c" * 40
TREE_A = "c" * 40
TREE_B = "d" * 40
TREE_C = "e" * 40


@dataclass(frozen=True)
class FakeTreeEntry:
    path: str
    payload: bytes
    mode: str = "100644"
    object_type: str = "blob"


class FakeRunner:
    def __init__(self) -> None:
        self.trees: dict[str, tuple[str, list[FakeTreeEntry]]] = {
            COMMIT_A: (
                TREE_A,
                [
                    FakeTreeEntry("deploy/scripts/helper.py", b"print('safe')\n"),
                    FakeTreeEntry(
                        "deploy/sms-compose",
                        b"#!/bin/sh\nexec /usr/bin/true\n",
                        mode="100755",
                    ),
                ],
            )
        }
        self.head = COMMIT_A
        self.status_output = b""
        self.ancestor_pairs = {(COMMIT_A, COMMIT_B)}
        self.calls: list[
            tuple[
                tuple[str, ...],
                tuple[int, ...],
                int | None,
                int | None,
                tuple[int, ...] | None,
            ]
        ] = []
        self._blob_payloads: dict[str, bytes] = {}

    def _object_id(self, entry: FakeTreeEntry) -> str:
        identity = entry.mode.encode("ascii") + b"\0" + entry.payload
        return hashlib.sha1(identity).hexdigest()  # noqa: S324 - fake Git identity

    def run(
        self,
        argv: list[str] | tuple[str, ...],
        *,
        pass_fds: list[int] | tuple[int, ...] = (),
        user: int | None = None,
        group: int | None = None,
        extra_groups: list[int] | tuple[int, ...] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        arguments = tuple(argv)
        self.calls.append(
            (
                arguments,
                tuple(pass_fds),
                user,
                group,
                None if extra_groups is None else tuple(extra_groups),
            )
        )
        if arguments[0] != snapshot_module.GIT:
            raise AssertionError(f"unexpected command: {arguments}")
        if "cat-file" in arguments:
            index = arguments.index("cat-file")
            operation, identity = arguments[index + 1 : index + 3]
            if operation == "-t":
                valid = identity in self.trees
                return subprocess.CompletedProcess(
                    arguments, 0 if valid else 1, b"commit\n" if valid else b"", b""
                )
            if operation == "blob":
                payload = self._blob_payloads.get(identity)
                return subprocess.CompletedProcess(
                    arguments,
                    0 if payload is not None else 1,
                    payload or b"",
                    b"",
                )
        if "rev-parse" in arguments:
            identity = arguments[-1]
            if identity == "HEAD":
                return subprocess.CompletedProcess(
                    arguments, 0, f"{self.head}\n".encode("ascii"), b""
                )
            commit = identity.removesuffix("^{tree}")
            tree = self.trees.get(commit)
            return subprocess.CompletedProcess(
                arguments,
                0 if tree is not None else 1,
                f"{tree[0]}\n".encode("ascii") if tree is not None else b"",
                b"",
            )
        if "ls-tree" in arguments:
            commit = arguments[-1]
            _, entries = self.trees[commit]
            records: list[bytes] = []
            self._blob_payloads = {}
            for entry in sorted(entries, key=lambda item: item.path.encode("ascii")):
                object_id = self._object_id(entry)
                self._blob_payloads[object_id] = entry.payload
                records.append(
                    (
                        f"{entry.mode} {entry.object_type} {object_id} "
                        f"{len(entry.payload)}\t{entry.path}"
                    ).encode("ascii")
                )
            return subprocess.CompletedProcess(
                arguments, 0, b"\0".join(records) + b"\0", b""
            )
        if "status" in arguments:
            return subprocess.CompletedProcess(arguments, 0, self.status_output, b"")
        if "merge-base" in arguments:
            ancestor, descendant = arguments[-2:]
            accepted = ancestor == descendant or (ancestor, descendant) in self.ancestor_pairs
            return subprocess.CompletedProcess(arguments, 0 if accepted else 1, b"", b"")
        raise AssertionError(f"unexpected Git command: {arguments}")


def _write_owned(path: Path, payload: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(mode)


@dataclass(frozen=True)
class SnapshotTestEnvironment:
    manager: ProductionControlSnapshot
    paths: SnapshotPaths
    runner: FakeRunner
    uid: int
    gid: int


def _environment(
    tmp_path: Path, runner: FakeRunner | None = None
) -> SnapshotTestEnvironment:
    active_runner = runner or FakeRunner()
    uid = os.geteuid()
    gid = os.getegid()
    control_root = tmp_path / "libexec" / "production-control"
    control_root.parent.mkdir(parents=True)
    paths = SnapshotPaths(
        repository=tmp_path / "checkout",
        control_root=control_root,
        lifecycle_lock=tmp_path / "run" / "secrets.lifecycle.lock",
        approval_root=tmp_path / "etc/sms-platform/production-control-approved",
    )
    paths.repository.mkdir()
    _write_owned(paths.lifecycle_lock, b"lock\n", 0o600)
    _write_owned(paths.approval_root / COMMIT_A, f"{COMMIT_A}\n".encode(), 0o444)
    return SnapshotTestEnvironment(
        manager=ProductionControlSnapshot(
            paths=paths,
            runner=active_runner,
            expected_uid=uid,
            expected_gid=gid,
            expected_operator_uid=uid,
            expected_operator_gid=gid,
        ),
        paths=paths,
        runner=active_runner,
        uid=uid,
        gid=gid,
    )


def _add_commit_b(environment: SnapshotTestEnvironment) -> None:
    environment.runner.trees[COMMIT_B] = (
        TREE_B,
        [FakeTreeEntry("deploy/sms-compose", b"#!/bin/sh\nexit 0\n", "100755")],
    )
    _write_owned(
        environment.paths.approval_root / COMMIT_B,
        f"{COMMIT_B}\n".encode(),
        0o444,
    )


def _add_divergent_commit_c(environment: SnapshotTestEnvironment) -> None:
    environment.runner.trees[COMMIT_C] = (
        TREE_C,
        [FakeTreeEntry("deploy/sms-compose", b"#!/bin/sh\nexit 3\n", "100755")],
    )
    _write_owned(
        environment.paths.approval_root / COMMIT_C,
        f"{COMMIT_C}\n".encode(),
        0o444,
    )


@pytest.mark.parametrize("operation", ["plan", "prepare", "activate"])
def test_mutating_and_plan_commands_require_approval_before_git(
    tmp_path: Path,
    operation: str,
) -> None:
    environment = _environment(tmp_path)
    (environment.paths.approval_root / COMMIT_A).unlink()
    environment.runner.calls.clear()

    with pytest.raises(ProductionControlSnapshotError, match="approval marker"):
        getattr(environment.manager, operation)(COMMIT_A)

    assert environment.runner.calls == []


@pytest.mark.parametrize(
    "drift",
    ["writable-directory", "wrong-mode", "wrong-content", "symlink", "hardlink"],
)
def test_approval_marker_contract_fails_closed_before_git(
    tmp_path: Path,
    drift: str,
) -> None:
    environment = _environment(tmp_path)
    marker = environment.paths.approval_root / COMMIT_A
    if drift == "writable-directory":
        environment.paths.approval_root.chmod(0o777)
    elif drift == "wrong-mode":
        marker.chmod(0o644)
    elif drift == "wrong-content":
        marker.chmod(0o644)
        marker.write_bytes(f"{COMMIT_B}\n".encode("ascii"))
        marker.chmod(0o444)
    elif drift == "symlink":
        target = tmp_path / "approval-target"
        _write_owned(target, f"{COMMIT_A}\n".encode("ascii"), 0o444)
        marker.unlink()
        marker.symlink_to(target)
    else:
        os.link(marker, environment.paths.approval_root / "alias")
    environment.runner.calls.clear()

    with pytest.raises(ProductionControlSnapshotError):
        environment.manager.plan(COMMIT_A)

    assert environment.runner.calls == []


def test_stable_launcher_accepts_only_the_exact_root_approval_marker(
    tmp_path: Path,
) -> None:
    approval_root = tmp_path / "approved"
    approval_root.mkdir(mode=0o755)
    marker = approval_root / COMMIT_A
    _write_owned(marker, f"{COMMIT_A}\n".encode("ascii"), 0o444)

    launcher_module._validate_approved_commit(
        COMMIT_A,
        approval_root=approval_root,
        expected_uid=os.geteuid(),
        expected_gid=os.getegid(),
    )

    os.link(marker, approval_root / "alias")
    with pytest.raises(launcher_module.LauncherError, match="unsafe"):
        launcher_module._validate_approved_commit(
            COMMIT_A,
            approval_root=approval_root,
            expected_uid=os.geteuid(),
            expected_gid=os.getegid(),
        )


def test_stable_launcher_checks_approval_before_any_snapshot_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def deny(_commit: str) -> None:
        raise launcher_module.LauncherError("approval denied before snapshot")

    monkeypatch.setattr(launcher_module, "_validate_approved_commit", deny)
    monkeypatch.setattr(launcher_module, "VERSIONS_ROOT", tmp_path / "missing")

    with pytest.raises(launcher_module.LauncherError, match="approval denied"):
        launcher_module._verify_version(COMMIT_A)


def test_stable_launcher_has_no_direct_candidate_recovery_execution_path() -> None:
    source = (ROOT / "deploy" / "production-sms-compose-launcher").read_text(
        encoding="utf-8"
    )

    assert "_recovery_activate_commit" not in source
    assert "SMS_PRODUCTION_CONTROL_RECOVERY_ACTIVATE" not in source


def test_stable_launcher_uses_manager_manifest_bound_and_accepts_over_one_mib() -> None:
    assert (
        launcher_module.MAX_MANIFEST_BYTES
        == snapshot_module.MAX_SNAPSHOT_MANIFEST_BYTES
        == 16 * 1024 * 1024
    )
    files = [
        {
            "mode": "100644",
            "path": f"files/{index:04d}-{'x' * 220}",
            "sha256": "0" * 64,
            "size": 0,
        }
        for index in range(5_000)
    ]
    raw = (
        json.dumps(
            {
                "commit": COMMIT_A,
                "files": files,
                "schema_version": 1,
                "tree": TREE_A,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("ascii")

    assert 1024 * 1024 < len(raw) < launcher_module.MAX_MANIFEST_BYTES
    assert len(launcher_module._parse_manifest(raw, COMMIT_A)) == len(files)


def test_prepare_builds_complete_immutable_snapshot_without_switching(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)

    result = environment.manager.prepare(COMMIT_A)

    assert result == {
        "bytes": 43,
        "commit": COMMIT_A,
        "current_target": None,
        "files": 2,
        "status": "prepared",
        "tree": TREE_A,
    }
    version = environment.paths.versions_root / COMMIT_A
    assert not os.path.lexists(environment.paths.current)
    assert stat.S_IMODE(version.stat().st_mode) == 0o555
    assert stat.S_IMODE((version / "deploy").stat().st_mode) == 0o555
    assert stat.S_IMODE((version / "deploy" / "sms-compose").stat().st_mode) == 0o555
    assert (
        version / "deploy" / "sms-compose"
    ).read_bytes() == b"#!/bin/sh\nexec /usr/bin/true\n"
    snapshot_manifest = version / SNAPSHOT_MANIFEST_NAME
    assert stat.S_IMODE(snapshot_manifest.stat().st_mode) == 0o444
    document = json.loads(snapshot_manifest.read_bytes())
    assert set(document) == {"schema_version", "commit", "tree", "files"}
    assert document["commit"] == COMMIT_A
    assert document["tree"] == TREE_A
    assert [item["path"] for item in document["files"]] == [
        "deploy/scripts/helper.py",
        "deploy/sms-compose",
    ]


def test_prepare_is_idempotent_without_rewriting_snapshot(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    environment.manager.prepare(COMMIT_A)
    wrapper = environment.paths.versions_root / COMMIT_A / "deploy" / "sms-compose"
    inode = wrapper.stat().st_ino

    environment.manager.prepare(COMMIT_A)

    assert wrapper.stat().st_ino == inode
    assert not os.path.lexists(environment.paths.current)


def test_activate_and_status_bind_snapshot_to_clean_active_head(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    environment.manager.prepare(COMMIT_A)
    git_call_count = len(environment.runner.calls)

    result = environment.manager.activate(COMMIT_A)

    assert result == environment.manager.status()
    assert result == {
        "bytes": 43,
        "commit": COMMIT_A,
        "current_target": f"versions/{COMMIT_A}",
        "files": 2,
        "status": "active",
        "tree": TREE_A,
    }
    assert os.readlink(environment.paths.current) == f"versions/{COMMIT_A}"
    git_calls = [call for call in environment.runner.calls if call[0][0] == snapshot_module.GIT]
    assert git_calls
    assert all(
        (user, group, extra_groups) == (environment.uid, environment.gid, ())
        for _, _, user, group, extra_groups in git_calls
    )
    assert all("core.fsmonitor=false" in arguments for arguments, *_ in git_calls)
    assert all("core.hooksPath=/dev/null" in arguments for arguments, *_ in git_calls)
    assert len(environment.runner.calls) > git_call_count


@pytest.mark.parametrize(
    ("head", "status_output", "message"),
    [
        (COMMIT_B, b"", "HEAD"),
        (COMMIT_A, b"?? unrelated-runtime-file\0", "not clean"),
    ],
)
def test_activate_requires_matching_clean_checkout(
    tmp_path: Path,
    head: str,
    status_output: bytes,
    message: str,
) -> None:
    environment = _environment(tmp_path)
    environment.manager.prepare(COMMIT_A)
    environment.runner.head = head
    environment.runner.status_output = status_output

    with pytest.raises(ProductionControlSnapshotError, match=message):
        environment.manager.activate(COMMIT_A)

    assert not os.path.lexists(environment.paths.current)


def test_status_fails_closed_when_active_checkout_later_moves(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    environment.manager.prepare(COMMIT_A)
    environment.manager.activate(COMMIT_A)
    environment.runner.head = COMMIT_B

    with pytest.raises(ProductionControlSnapshotError, match="HEAD"):
        environment.manager.status()


def test_status_fails_closed_before_git_when_approval_is_revoked(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    environment.manager.prepare(COMMIT_A)
    environment.manager.activate(COMMIT_A)
    (environment.paths.approval_root / COMMIT_A).unlink()
    environment.runner.calls.clear()

    with pytest.raises(ProductionControlSnapshotError, match="approval marker"):
        environment.manager.status()

    assert environment.runner.calls == []


def test_activate_fails_when_lifecycle_lock_is_held(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    environment.manager.prepare(COMMIT_A)
    descriptor = os.open(environment.paths.lifecycle_lock, os.O_RDWR)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(ProductionControlSnapshotError, match="lifecycle lock"):
            environment.manager.activate(COMMIT_A)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def test_activate_reuses_verified_inherited_lifecycle_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment(tmp_path)
    environment.manager.prepare(COMMIT_A)
    descriptor = os.open(environment.paths.lifecycle_lock, os.O_RDWR)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setenv("SMS_LIFECYCLE_LOCKED", "1")
    monkeypatch.setenv("SMS_LIFECYCLE_LOCK_FD", str(descriptor))
    try:
        assert environment.manager.activate(COMMIT_A)["status"] == "active"
        probe = os.open(environment.paths.lifecycle_lock, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(probe)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def test_activate_rejects_inherited_descriptor_for_another_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment(tmp_path)
    environment.manager.prepare(COMMIT_A)
    other_lock = tmp_path / "run" / "other.lock"
    _write_owned(other_lock, b"lock\n", 0o600)
    descriptor = os.open(other_lock, os.O_RDWR)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setenv("SMS_LIFECYCLE_LOCKED", "1")
    monkeypatch.setenv("SMS_LIFECYCLE_LOCK_FD", str(descriptor))
    try:
        with pytest.raises(ProductionControlSnapshotError, match="wrong inode"):
            environment.manager.activate(COMMIT_A)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def test_activate_failure_before_atomic_replace_keeps_old_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment(tmp_path)
    environment.manager.prepare(COMMIT_A)
    environment.manager.activate(COMMIT_A)
    _add_commit_b(environment)
    environment.manager.prepare(COMMIT_B)
    environment.runner.head = COMMIT_B
    original_switch = environment.manager._atomic_switch_current

    def fail_before_replace(_target: str) -> None:
        raise ProductionControlSnapshotError("injected pre-replace failure")

    monkeypatch.setattr(environment.manager, "_atomic_switch_current", fail_before_replace)
    with pytest.raises(ProductionControlSnapshotError, match="pre-replace failure"):
        environment.manager.activate(COMMIT_B)
    assert os.readlink(environment.paths.current) == f"versions/{COMMIT_A}"
    environment.runner.head = COMMIT_A
    assert environment.manager.status()["commit"] == COMMIT_A

    monkeypatch.setattr(environment.manager, "_atomic_switch_current", original_switch)
    environment.runner.head = COMMIT_B
    assert environment.manager.activate(COMMIT_B)["commit"] == COMMIT_B


def test_activate_failure_after_atomic_replace_keeps_new_pointer_and_reruns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment(tmp_path)
    environment.manager.prepare(COMMIT_A)
    environment.manager.activate(COMMIT_A)
    _add_commit_b(environment)
    environment.manager.prepare(COMMIT_B)
    environment.runner.head = COMMIT_B
    original_switch = environment.manager._atomic_switch_current

    def fail_after_replace(target: str) -> None:
        original_switch(target)
        raise ProductionControlSnapshotError("injected post-replace failure")

    monkeypatch.setattr(environment.manager, "_atomic_switch_current", fail_after_replace)
    with pytest.raises(ProductionControlSnapshotError, match="post-replace failure"):
        environment.manager.activate(COMMIT_B)
    assert os.readlink(environment.paths.current) == f"versions/{COMMIT_B}"
    assert environment.manager.status()["commit"] == COMMIT_B

    monkeypatch.setattr(environment.manager, "_atomic_switch_current", original_switch)
    assert environment.manager.activate(COMMIT_B)["commit"] == COMMIT_B
    assert os.readlink(environment.paths.current) == f"versions/{COMMIT_B}"
    assert (environment.paths.versions_root / COMMIT_A).is_dir()
    assert (environment.paths.versions_root / COMMIT_B).is_dir()


def test_activate_refuses_to_repair_an_unverified_current_pointer(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    environment.manager.prepare(COMMIT_A)
    environment.paths.current.symlink_to(f"versions/{COMMIT_B}")

    with pytest.raises(ProductionControlSnapshotError, match="approval marker"):
        environment.manager.activate(COMMIT_A)

    assert os.readlink(environment.paths.current) == f"versions/{COMMIT_B}"


def test_activate_same_commit_is_idempotent_without_replacing_current(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    environment.manager.prepare(COMMIT_A)
    environment.manager.activate(COMMIT_A)
    pointer_inode = environment.paths.current.lstat().st_ino

    result = environment.manager.activate(COMMIT_A)

    assert result["commit"] == COMMIT_A
    assert environment.paths.current.lstat().st_ino == pointer_inode


def test_activate_rejects_an_approved_backward_transition(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    environment.manager.prepare(COMMIT_A)
    environment.manager.activate(COMMIT_A)
    _add_commit_b(environment)
    environment.manager.prepare(COMMIT_B)
    environment.runner.head = COMMIT_B
    environment.manager.activate(COMMIT_B)
    environment.runner.head = COMMIT_A

    with pytest.raises(ProductionControlSnapshotError, match="forward transition"):
        environment.manager.activate(COMMIT_A)

    assert os.readlink(environment.paths.current) == f"versions/{COMMIT_B}"


def test_activate_rejects_an_approved_divergent_transition(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    environment.manager.prepare(COMMIT_A)
    environment.manager.activate(COMMIT_A)
    _add_divergent_commit_c(environment)
    environment.manager.prepare(COMMIT_C)
    environment.runner.head = COMMIT_C

    with pytest.raises(ProductionControlSnapshotError, match="forward transition"):
        environment.manager.activate(COMMIT_C)

    assert os.readlink(environment.paths.current) == f"versions/{COMMIT_A}"


def test_status_detects_snapshot_byte_drift(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    environment.manager.prepare(COMMIT_A)
    environment.manager.activate(COMMIT_A)
    wrapper = environment.paths.versions_root / COMMIT_A / "deploy" / "sms-compose"
    wrapper.chmod(0o755)
    wrapper.write_bytes(b"#!/bin/sh\nexec /bin/sh\n")
    wrapper.chmod(0o555)

    with pytest.raises(ProductionControlSnapshotError, match="digest drifted"):
        environment.manager.status()


def test_status_detects_pointer_drift(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    environment.manager.prepare(COMMIT_A)
    environment.manager.activate(COMMIT_A)
    _add_commit_b(environment)
    environment.manager.prepare(COMMIT_B)
    environment.paths.current.unlink()
    environment.paths.current.symlink_to(f"versions/{COMMIT_B}")

    with pytest.raises(ProductionControlSnapshotError, match="HEAD"):
        environment.manager.status()


@pytest.mark.parametrize(
    "entry",
    [
        FakeTreeEntry("deploy/link", b"target", mode="120000"),
        FakeTreeEntry("deploy/submodule", b"", mode="160000", object_type="commit"),
        FakeTreeEntry("../escape", b"payload"),
        FakeTreeEntry("deploy/../../escape", b"payload"),
        FakeTreeEntry("deploy/name with space", b"payload"),
    ],
)
def test_prepare_rejects_non_blob_modes_and_abnormal_paths(
    tmp_path: Path,
    entry: FakeTreeEntry,
) -> None:
    runner = FakeRunner()
    runner.trees[COMMIT_A] = (TREE_A, [entry])
    environment = _environment(tmp_path, runner)

    with pytest.raises(ProductionControlSnapshotError):
        environment.manager.prepare(COMMIT_A)
    assert not environment.paths.versions_root.exists()


def test_prepare_rejects_oversized_tree_before_reading_blobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = FakeRunner()
    runner.trees[COMMIT_A] = (
        TREE_A,
        [FakeTreeEntry("a", b"a"), FakeTreeEntry("b", b"b")],
    )
    environment = _environment(tmp_path, runner)
    monkeypatch.setattr(snapshot_module, "MAX_TRACKED_FILES", 1)

    with pytest.raises(ProductionControlSnapshotError, match="file count"):
        environment.manager.prepare(COMMIT_A)
    assert not any("blob" in call[0] for call in runner.calls)


def test_prepare_preserves_tracked_empty_regular_files(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.trees[COMMIT_A] = (TREE_A, [FakeTreeEntry("deploy/empty", b"")])
    environment = _environment(tmp_path, runner)

    result = environment.manager.prepare(COMMIT_A)

    assert result["bytes"] == 0
    empty = environment.paths.versions_root / COMMIT_A / "deploy" / "empty"
    assert empty.read_bytes() == b""
    assert stat.S_IMODE(empty.stat().st_mode) == 0o444


@pytest.mark.parametrize("commit", ["", "A" * 40, "a" * 39, "a" * 41, "../main"])
def test_expected_commit_is_strict_before_git(tmp_path: Path, commit: str) -> None:
    environment = _environment(tmp_path)

    with pytest.raises(ProductionControlSnapshotError, match="40 lowercase hex"):
        environment.manager.plan(commit)
    assert not environment.runner.calls


def test_old_versions_are_preserved_and_prepare_never_switches_current(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    environment.manager.prepare(COMMIT_A)
    environment.manager.activate(COMMIT_A)
    _add_commit_b(environment)

    environment.manager.prepare(COMMIT_B)

    assert (environment.paths.versions_root / COMMIT_A).is_dir()
    assert (environment.paths.versions_root / COMMIT_B).is_dir()
    assert os.readlink(environment.paths.current) == f"versions/{COMMIT_A}"


def test_plan_is_read_only_and_reports_existing_snapshot(tmp_path: Path) -> None:
    environment = _environment(tmp_path)

    first = environment.manager.plan(COMMIT_A)
    assert first["prepared"] is False
    assert not environment.paths.control_root.exists()

    environment.manager.prepare(COMMIT_A)
    second = environment.manager.plan(COMMIT_A)
    assert second["prepared"] is True
    assert not os.path.lexists(environment.paths.current)


def test_prepare_refuses_writable_control_parent(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    environment.paths.control_root.parent.chmod(0o777)

    with pytest.raises(ProductionControlSnapshotError, match="parent directory"):
        environment.manager.prepare(COMMIT_A)

    assert not environment.paths.control_root.exists()


def test_cli_requires_root_but_not_sudo_release_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(snapshot_module.os, "geteuid", lambda: 501)
    with pytest.raises(ProductionControlSnapshotError, match="execute as root"):
        snapshot_module._validate_cli_identity("prepare")

    monkeypatch.setattr(snapshot_module.os, "geteuid", lambda: 0)
    monkeypatch.delenv("SUDO_UID", raising=False)
    monkeypatch.delenv("SUDO_GID", raising=False)
    snapshot_module._validate_cli_identity("prepare")
