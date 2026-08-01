from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))

import public_baseline_activation as module  # noqa: E402

VERSION = "1.6.0"
MIGRATION = "0039_manual_job_outbox"
ACTIVATION_ID = "test-20260731T120000Z-0123456789ab"


def _git(
    repository: Path,
    *arguments: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env={
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        },
    )
    if check:
        assert result.returncode == 0, result.stderr
    return result


def _repository(path: Path, marker: str) -> tuple[str, str]:
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.name", "Public Baseline Test")
    _git(path, "config", "user.email", "public-baseline@example.test")
    (path / ".gitignore").write_text(
        "\n".join(
            (
                ".env",
                "deploy/secrets/",
                "backend/.venv/",
                "deploy/scripts/__pycache__/",
                "scripts/__pycache__/",
                "server-only/",
                "",
            )
        ),
        encoding="ascii",
    )
    (path / "VERSION").write_text(f"{VERSION}\n", encoding="ascii")
    for directory in ("backend", "deploy", "deploy/scripts", "scripts"):
        (path / directory).mkdir(parents=True, exist_ok=True)
    (path / "backend/source.txt").write_text(
        f"backend-{marker}\n",
        encoding="ascii",
    )
    (path / "deploy/source.txt").write_text(
        f"deploy-{marker}\n",
        encoding="ascii",
    )
    (path / "deploy/systemd").mkdir(parents=True, exist_ok=True)
    (path / "deploy/systemd/vendor-control-agent.service").write_text(
        f"[Unit]\nDescription={marker}\n",
        encoding="ascii",
    )
    (path / "scripts/source.txt").write_text(
        f"scripts-{marker}\n",
        encoding="ascii",
    )
    _git(path, "add", ".")
    _git(path, "commit", "-m", f"{marker} root")
    return (
        _git(path, "rev-parse", "HEAD").stdout.strip(),
        _git(path, "rev-parse", "HEAD^{tree}").stdout.strip(),
    )


def _write_private(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)
    path.chmod(0o600)


def _add_persistence(active_root: Path) -> None:
    active_root.chmod(0o2770)
    for directory in (active_root / "backend", active_root / "deploy"):
        directory.chmod(0o2770)
    _write_private(active_root / ".env", b"ENVIRONMENT=test\n")
    secrets = active_root / "deploy/secrets"
    secrets.mkdir(mode=0o700)
    secrets.chmod(0o700)
    _write_private(secrets / "runtime-token", b"do-not-read-this-value\n")
    virtualenv = active_root / "backend/.venv"
    virtualenv.mkdir(mode=0o2770)
    virtualenv.chmod(0o2770)
    (virtualenv / "pyvenv.cfg").write_text("home = /python\n", encoding="ascii")
    for relative in (
        Path("deploy/scripts/__pycache__"),
        Path("scripts/__pycache__"),
    ):
        cache = active_root / relative
        cache.mkdir()
        (cache / "discardable.pyc").write_bytes(b"cache")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Guard:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def require_held(self) -> None:
        self.calls += 1
        if self.fail:
            raise RuntimeError("lifecycle lock is not held")


class PortableExchange:
    """测试替身；生产实现必须使用 renameat2 原子交换。"""

    def __init__(self) -> None:
        self.calls: list[tuple[Path, Path]] = []

    def exchange(self, left: Path, right: Path) -> None:
        self.calls.append((left, right))
        temporary = left.parent / ".portable-exchange"
        assert not temporary.exists()
        os.rename(left, temporary)
        os.rename(right, left)
        os.rename(temporary, right)


def _manifest_document(
    *,
    base_commit: str,
    base_tree: str,
    target_commit: str,
    target_tree: str,
    bundle_sha256: str,
    api_sha256: str,
    web_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "activation_id": ACTIVATION_ID,
        "origin_url": module.PUBLIC_ORIGIN_URL,
        "base": {"commit": base_commit, "tree": base_tree},
        "target": {"commit": target_commit, "tree": target_tree},
        "bundle": {
            "file": "public-baseline.bundle",
            "sha256": bundle_sha256,
            "ref": "refs/heads/main",
        },
        "images": {
            "api": {
                "file": "api.tar",
                "sha256": api_sha256,
                "ref": f"sms-platform-test-api:{target_commit}",
                "id": f"sha256:{'1' * 64}",
                "version": VERSION,
                "revision": target_commit,
                "schema_revision": MIGRATION,
            },
            "web": {
                "file": "web.tar",
                "sha256": web_sha256,
                "ref": f"sms-platform-test-web:{target_commit}",
                "id": f"sha256:{'2' * 64}",
                "version": VERSION,
                "revision": target_commit,
                "schema_revision": MIGRATION,
            },
        },
        "migration": {
            "from": MIGRATION,
            "target": MIGRATION,
            "compatibility": "none",
        },
    }


def _fixture(
    tmp_path: Path,
    *,
    extra_bundle_ref: bool = False,
    target_deploy_symlink: bool = False,
) -> tuple[
    module.ActivationRequest,
    module.PublicBaselineActivator,
    Guard,
    PortableExchange,
    Path,
]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    tmp_path.chmod(0o700)
    active_root = tmp_path / "sms-platform"
    base_commit, base_tree = _repository(active_root, "private-base")
    _add_persistence(active_root)

    public_root = tmp_path / "public-source"
    target_commit, target_tree = _repository(public_root, "public-target")
    if target_deploy_symlink:
        _git(public_root, "rm", "-r", "deploy")
        (public_root / "deploy/scripts").rmdir()
        (public_root / "deploy").rmdir()
        (public_root / "deploy").symlink_to(tmp_path / "outside-deploy")
        _git(public_root, "add", "deploy")
        _git(public_root, "commit", "-m", "malicious deploy symlink")
        target_commit = _git(public_root, "rev-parse", "HEAD").stdout.strip()
        target_tree = _git(
            public_root, "rev-parse", "HEAD^{tree}"
        ).stdout.strip()
    if extra_bundle_ref:
        _git(public_root, "branch", "extra", target_commit)

    artifacts = tmp_path / "incoming"
    artifacts.mkdir(mode=0o700)
    bundle = artifacts / "public-baseline.bundle"
    bundle_refs = ["refs/heads/main"]
    if extra_bundle_ref:
        bundle_refs.append("refs/heads/extra")
    _git(
        public_root,
        "bundle",
        "create",
        str(bundle),
        *bundle_refs,
    )
    bundle.chmod(0o600)
    api = artifacts / "api.tar"
    web = artifacts / "web.tar"
    _write_private(api, b"api image archive")
    _write_private(web, b"web image archive")
    document = _manifest_document(
        base_commit=base_commit,
        base_tree=base_tree,
        target_commit=target_commit,
        target_tree=target_tree,
        bundle_sha256=_sha256(bundle),
        api_sha256=_sha256(api),
        web_sha256=_sha256(web),
    )
    request = module.ActivationRequest.from_json_bytes(
        json.dumps(document, separators=(",", ":")).encode("utf-8")
    )
    guard = Guard()
    exchange = PortableExchange()
    activator = module.PublicBaselineActivator(
        activation_id=ACTIVATION_ID,
        artifacts_root=artifacts,
        lifecycle_guard=guard,
        active_root=active_root,
        workspace_root=tmp_path,
        directory_exchange=exchange,
        expected_uid=os.geteuid(),
        expected_operator_uid=os.geteuid(),
        expected_operator_gid=os.getegid(),
        expected_system_gid=os.getegid(),
    )
    return request, activator, guard, exchange, public_root


def test_manifest_is_exact_and_binds_git_images_and_migration(
    tmp_path: Path,
) -> None:
    request, _activator, _guard, _exchange, _public = _fixture(tmp_path)

    assert request.activation_id == ACTIVATION_ID
    assert request.origin_url == module.PUBLIC_ORIGIN_URL
    assert request.bundle.ref == module.PUBLIC_BUNDLE_REF
    assert request.bundle.archive_file == "public-baseline.bundle"
    assert request.images["api"].archive_file == "api.tar"
    assert request.images["api"].archive_sha256 == request.images["api"].sha256
    assert request.images["api"].revision == request.target.commit
    assert request.images["web"].schema_revision == MIGRATION
    assert request.migration.migration_from == MIGRATION

    document = request.to_mapping()
    document["unexpected"] = True
    with pytest.raises(module.PublicBaselineActivationError, match="fields"):
        module.ActivationRequest.from_json_bytes(
            json.dumps(document).encode("utf-8")
        )

    duplicate = (
        b'{"schema_version":1,"schema_version":1,'
        + json.dumps(
            {
                key: value
                for key, value in request.to_mapping().items()
                if key != "schema_version"
            },
            separators=(",", ":"),
        ).encode("utf-8")[1:]
    )
    with pytest.raises(module.PublicBaselineActivationError, match="duplicate"):
        module.ActivationRequest.from_json_bytes(duplicate)

    document = request.to_mapping()
    images = document["images"]
    assert isinstance(images, dict)
    api = images["api"]
    assert isinstance(api, dict)
    api["revision"] = request.base.commit
    with pytest.raises(module.PublicBaselineActivationError, match="revision"):
        module.ActivationRequest.from_json_bytes(
            json.dumps(document).encode("utf-8")
        )


def test_prepare_builds_standalone_public_detached_root(tmp_path: Path) -> None:
    request, activator, guard, _exchange, _public = _fixture(tmp_path)

    prepared = activator.prepare(request)

    assert guard.calls == 1
    assert prepared.activation_id == ACTIVATION_ID
    assert prepared.commit == request.target.commit
    assert prepared.tree == request.target.tree
    assert prepared.staged_root == activator.staged_root
    assert (
        _git(prepared.staged_root, "symbolic-ref", "-q", "HEAD", check=False).returncode
        == 1
    )
    assert (
        _git(
            prepared.staged_root,
            "for-each-ref",
            "--format=%(refname)",
        ).stdout.strip()
        == "refs/remotes/origin/main"
    )
    assert (
        _git(
            prepared.staged_root,
            "remote",
            "get-url",
            "--all",
            "origin",
        ).stdout.strip()
        == module.PUBLIC_ORIGIN_URL
    )
    assert not (prepared.staged_root / ".git/objects/info/alternates").exists()
    assert _git(prepared.staged_root, "reflog", "show", "--all").stdout == ""
    fsck = _git(
        prepared.staged_root,
        "fsck",
        "--strict",
        "--no-reflogs",
        "--unreachable",
        "--no-progress",
    )
    assert fsck.stdout == ""
    assert fsck.stderr == ""
    assert not (prepared.staged_root / ".env").exists()
    assert not (prepared.staged_root / "deploy/secrets").exists()
    assert not (prepared.staged_root / "backend/.venv").exists()

    again = activator.prepare(request)
    assert again == prepared
    assert guard.calls == 2


def test_prepare_preserves_git_modes_with_restrictive_umask(tmp_path: Path) -> None:
    request, activator, _guard, _exchange, _public = _fixture(tmp_path)

    previous_umask = os.umask(0o027)
    try:
        prepared = activator.prepare(request)
    finally:
        os.umask(previous_umask)

    unit = prepared.staged_root / "deploy/systemd/vendor-control-agent.service"
    assert stat.S_IMODE(unit.stat().st_mode) == 0o644

    git_root = prepared.staged_root / ".git"
    assert git_root.stat().st_uid == os.geteuid()
    assert git_root.stat().st_gid == os.getegid()
    assert stat.S_IMODE(git_root.stat().st_mode) & 0o050 == 0o050
    head = git_root / "HEAD"
    assert stat.S_IMODE(head.stat().st_mode) & 0o040 == 0o040


def test_activate_finalize_and_rollback_preserve_only_allowlisted_state(
    tmp_path: Path,
) -> None:
    request, activator, guard, exchange, _public = _fixture(tmp_path)
    old_secret = (
        activator.active_root / "deploy/secrets/runtime-token"
    ).read_bytes()
    activator.prepare(request)

    outcome = activator.activate(request)

    assert outcome.state == "applied"
    assert outcome.active_root == activator.active_root
    assert outcome.recovery_root == activator.recovery_root
    assert outcome.commit == request.target.commit
    assert outcome.tree == request.target.tree
    assert len(exchange.calls) == 1
    assert (
        _git(activator.active_root, "rev-parse", "HEAD").stdout.strip()
        == request.target.commit
    )
    assert (activator.active_root / ".env").read_bytes() == b"ENVIRONMENT=test\n"
    assert (
        activator.active_root / "deploy/secrets/runtime-token"
    ).read_bytes() == old_secret
    assert (activator.active_root / "backend/.venv/pyvenv.cfg").is_file()
    assert stat.S_IMODE(activator.active_root.stat().st_mode) == 0o2770
    assert (
        stat.S_IMODE((activator.active_root / "backend").stat().st_mode)
        == 0o2770
    )
    assert (
        stat.S_IMODE((activator.active_root / "deploy").stat().st_mode)
        == 0o2770
    )
    git_root = activator.active_root / ".git"
    assert git_root.stat().st_uid == os.geteuid()
    assert git_root.stat().st_gid == os.getegid()
    assert stat.S_IMODE(git_root.stat().st_mode) & 0o050 == 0o050
    assert stat.S_IMODE((git_root / "HEAD").stat().st_mode) & 0o040 == 0o040
    assert (
        stat.S_IMODE(
            (activator.active_root / "backend/.venv").stat().st_mode
        )
        == 0o2770
    )
    for cache in module.DISCARDABLE_CACHE_PATHS:
        assert not (activator.active_root / cache).exists()
        assert (activator.recovery_root / cache).is_dir()
    journal = activator.journal_path.read_text(encoding="ascii")
    assert "do-not-read-this-value" not in journal

    verified = activator.finalize()
    assert verified.state == "verified"
    assert verified.as_test_update_state() == {
        "state": "verified",
        "actual_commit": request.target.commit,
        "actual_migration_head": MIGRATION,
    }

    rolled_back = activator.rollback()

    assert rolled_back.state == "rolled_back"
    assert rolled_back.commit == request.base.commit
    assert rolled_back.tree == request.base.tree
    assert rolled_back.recovery_root == activator.staged_root
    assert len(exchange.calls) == 2
    assert (
        _git(activator.active_root, "rev-parse", "HEAD").stdout.strip()
        == request.base.commit
    )
    assert (
        activator.active_root / "deploy/secrets/runtime-token"
    ).read_bytes() == old_secret
    for cache in module.DISCARDABLE_CACHE_PATHS:
        assert (activator.active_root / cache).is_dir()
    assert (
        _git(activator.staged_root, "rev-parse", "HEAD").stdout.strip()
        == request.target.commit
    )
    assert guard.calls == 4


def test_partial_persistence_move_is_recoverable(tmp_path: Path) -> None:
    request, activator, _guard, _exchange, _public = _fixture(tmp_path)
    activator.prepare(request)
    journal = activator._read_journal(request)
    source = activator.active_root / ".env"
    target = activator.staged_root / ".env"
    os.rename(source, target)
    journal["state"] = module.ActivationState.PERSISTENCE_MOVED.value
    journal["moved_persistence"] = [".env"]
    activator._write_journal(journal)

    outcome = activator.rollback()

    assert outcome.commit == request.base.commit
    assert (activator.active_root / ".env").read_bytes() == b"ENVIRONMENT=test\n"
    assert not (activator.staged_root / ".env").exists()


def test_target_symlink_cannot_redirect_persistent_move(tmp_path: Path) -> None:
    request, activator, _guard, _exchange, _public = _fixture(
        tmp_path,
        target_deploy_symlink=True,
    )
    with pytest.raises(
        module.PublicBaselineActivationError,
        match="real directory",
    ):
        activator.prepare(request)

    assert (activator.active_root / ".env").is_file()
    assert (
        activator.active_root / "deploy/secrets/runtime-token"
    ).is_file()
    assert not (tmp_path / "outside-deploy").exists()


def test_prepare_rejects_extra_bundle_ref_and_unallowlisted_ignored_path(
    tmp_path: Path,
) -> None:
    request, activator, _guard, _exchange, _public = _fixture(
        tmp_path / "extra-ref",
        extra_bundle_ref=True,
    )
    with pytest.raises(module.PublicBaselineActivationError, match="one main ref"):
        activator.prepare(request)

    other = tmp_path / "ignored"
    request, activator, _guard, _exchange, _public = _fixture(other)
    ignored = activator.active_root / "server-only"
    ignored.mkdir()
    (ignored / "state").write_text("not allowlisted\n", encoding="ascii")
    with pytest.raises(module.PublicBaselineActivationError, match="allowlisted"):
        activator.prepare(request)


def test_prepare_fails_closed_before_mutation_without_lock_or_exact_base(
    tmp_path: Path,
) -> None:
    request, activator, _guard, exchange, _public = _fixture(tmp_path / "lock")
    failing_guard = Guard(fail=True)
    locked = module.PublicBaselineActivator(
        activation_id=ACTIVATION_ID,
        artifacts_root=activator.artifacts_root,
        lifecycle_guard=failing_guard,
        active_root=activator.active_root,
        workspace_root=activator.workspace_root,
        directory_exchange=exchange,
        expected_uid=os.geteuid(),
        expected_operator_uid=os.geteuid(),
        expected_operator_gid=os.getegid(),
        expected_system_gid=os.getegid(),
    )
    with pytest.raises(RuntimeError, match="lock"):
        locked.prepare(request)
    assert not locked.journal_path.exists()
    assert not locked.staged_root.exists()

    request, mismatched, _guard, _exchange, _public = _fixture(
        tmp_path / "tree"
    )
    document = request.to_mapping()
    base = document["base"]
    assert isinstance(base, dict)
    base["tree"] = "f" * 40
    wrong = module.ActivationRequest.from_json_bytes(
        json.dumps(document).encode("utf-8")
    )
    with pytest.raises(module.PublicBaselineActivationError, match="identity"):
        mismatched.prepare(wrong)
    assert not mismatched.journal_path.exists()


def test_artifacts_and_persistence_require_private_metadata(
    tmp_path: Path,
) -> None:
    request, activator, _guard, _exchange, _public = _fixture(
        tmp_path / "artifact"
    )
    (activator.artifacts_root / "api.tar").chmod(0o644)
    with pytest.raises(module.PublicBaselineActivationError, match="metadata"):
        activator.prepare(request)

    request, activator, _guard, _exchange, _public = _fixture(
        tmp_path / "secret"
    )
    (
        activator.active_root / "deploy/secrets/runtime-token"
    ).chmod(0o644)
    with pytest.raises(module.PublicBaselineActivationError, match="secret"):
        activator.prepare(request)


def _verified_activation(
    tmp_path: Path,
) -> tuple[
    module.ActivationRequest,
    module.PublicBaselineActivator,
    Guard,
]:
    request, activator, guard, _exchange, _public = _fixture(tmp_path)
    activator.prepare(request)
    activator.activate(request)
    activator.finalize()
    return request, activator, guard


def test_cleanup_requires_verified_and_keeps_active_runtime_and_artifacts(
    tmp_path: Path,
) -> None:
    request, activator, _guard, _exchange, _public = _fixture(
        tmp_path / "not-verified"
    )
    activator.prepare(request)
    activator.activate(request)
    with pytest.raises(
        module.PublicBaselineActivationError,
        match="verified",
    ):
        activator.cleanup()
    assert activator.recovery_root.is_dir()

    request, activator, guard = _verified_activation(tmp_path / "verified")
    secret = (
        activator.active_root / "deploy/secrets/runtime-token"
    ).read_bytes()
    artifact_names = {
        entry.name for entry in activator.artifacts_root.iterdir()
    }

    outcome = activator.cleanup()

    assert outcome.state == "cleaned"
    assert outcome.commit == request.target.commit
    assert outcome.recovery_root == activator.recovery_root
    assert not outcome.recovery_root.exists()
    assert not activator.cleanup_root.exists()
    assert activator.active_root.is_dir()
    assert (activator.active_root / ".env").is_file()
    assert (activator.active_root / "backend/.venv").is_dir()
    assert (
        activator.active_root / "deploy/secrets/runtime-token"
    ).read_bytes() == secret
    assert {
        entry.name for entry in activator.artifacts_root.iterdir()
    } == artifact_names
    journal = json.loads(activator.journal_path.read_text(encoding="ascii"))
    assert journal["state"] == "cleaned"
    assert isinstance(journal["cleanup_dev"], int)
    assert isinstance(journal["cleanup_ino"], int)
    assert activator.journal_path.stat().st_size < 4096

    assert activator.cleanup() == outcome
    assert guard.calls == 5


def test_cleanup_rejects_wrong_recovery_identity_and_persistent_residue(
    tmp_path: Path,
) -> None:
    _request, activator, _guard = _verified_activation(
        tmp_path / "wrong-target"
    )
    (activator.recovery_root / "wrong-target.txt").write_text(
        "wrong target\n",
        encoding="ascii",
    )
    _git(activator.recovery_root, "add", "wrong-target.txt")
    _git(activator.recovery_root, "commit", "-m", "wrong cleanup target")

    with pytest.raises(module.PublicBaselineActivationError, match="identity"):
        activator.cleanup()

    assert activator.active_root.is_dir()
    assert activator.recovery_root.is_dir()
    state = json.loads(activator.journal_path.read_text(encoding="ascii"))
    assert state["state"] == "verified"

    _request, activator, _guard = _verified_activation(
        tmp_path / "persistent-residue"
    )
    _write_private(activator.recovery_root / ".env", b"stale runtime\n")

    with pytest.raises(
        module.PublicBaselineActivationError,
        match="remains in recovery",
    ):
        activator.cleanup()

    assert (activator.active_root / ".env").is_file()
    assert (activator.recovery_root / ".env").is_file()


def test_cleanup_resumes_after_interrupted_fd_safe_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, activator, _guard = _verified_activation(tmp_path)
    real_unlink = module.os.unlink
    interrupted = False

    def unlink_once_then_interrupt(
        path: str,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal interrupted
        real_unlink(path, dir_fd=dir_fd)
        if not interrupted:
            interrupted = True
            raise OSError("injected cleanup interruption")

    monkeypatch.setattr(module.os, "unlink", unlink_once_then_interrupt)
    with pytest.raises(
        module.PublicBaselineActivationError,
        match="interrupted",
    ):
        activator.cleanup()
    monkeypatch.setattr(module.os, "unlink", real_unlink)

    state = json.loads(activator.journal_path.read_text(encoding="ascii"))
    assert state["state"] == "finalizing"
    assert not activator.recovery_root.exists()
    assert activator.cleanup_root.is_dir()
    assert (activator.active_root / ".env").is_file()

    outcome = activator.cleanup()

    assert outcome.state == "cleaned"
    assert outcome.commit == request.target.commit
    assert not activator.cleanup_root.exists()
    assert (activator.active_root / "deploy/secrets").is_dir()


def test_cleanup_rejects_root_and_nested_symlink_attacks(
    tmp_path: Path,
) -> None:
    _request, activator, _guard = _verified_activation(
        tmp_path / "root-symlink"
    )
    displaced = tmp_path / "root-symlink/displaced-recovery"
    os.rename(activator.recovery_root, displaced)
    activator.recovery_root.symlink_to(activator.active_root)

    with pytest.raises(module.PublicBaselineActivationError):
        activator.cleanup()

    assert activator.active_root.is_dir()
    assert (activator.active_root / ".env").is_file()
    assert displaced.is_dir()

    _request, activator, _guard = _verified_activation(
        tmp_path / "nested-symlink"
    )
    escape = (
        activator.recovery_root
        / "deploy/scripts/__pycache__/escape"
    )
    escape.symlink_to(activator.active_root / ".env")

    with pytest.raises(
        module.PublicBaselineActivationError,
        match="metadata is unsafe",
    ):
        activator.cleanup()

    assert escape.is_symlink()
    assert (activator.active_root / ".env").read_bytes() == b"ENVIRONMENT=test\n"
    state = json.loads(activator.journal_path.read_text(encoding="ascii"))
    assert state["state"] == "verified"


def test_server_operator_identity_and_modes_are_exact(tmp_path: Path) -> None:
    request, activator, guard, exchange, _public = _fixture(
        tmp_path / "wrong-uid"
    )
    wrong_uid = module.PublicBaselineActivator(
        activation_id=ACTIVATION_ID,
        artifacts_root=activator.artifacts_root,
        lifecycle_guard=guard,
        active_root=activator.active_root,
        workspace_root=activator.workspace_root,
        directory_exchange=exchange,
        expected_uid=os.geteuid(),
        expected_operator_uid=os.geteuid() + 1,
        expected_operator_gid=os.getegid(),
        expected_system_gid=os.getegid(),
    )
    with pytest.raises(
        module.PublicBaselineActivationError,
        match="ownership or mode",
    ):
        wrong_uid.prepare(request)

    request, activator, guard, exchange, _public = _fixture(
        tmp_path / "wrong-gid"
    )
    wrong_gid = module.PublicBaselineActivator(
        activation_id=ACTIVATION_ID,
        artifacts_root=activator.artifacts_root,
        lifecycle_guard=guard,
        active_root=activator.active_root,
        workspace_root=activator.workspace_root,
        directory_exchange=exchange,
        expected_uid=os.geteuid(),
        expected_operator_uid=os.geteuid(),
        expected_operator_gid=os.getegid() + 1,
        expected_system_gid=os.getegid(),
    )
    with pytest.raises(
        module.PublicBaselineActivationError,
        match="ownership or mode",
    ):
        wrong_gid.prepare(request)


def test_persistent_secret_files_use_fixed_system_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _request, activator, guard, exchange, _public = _fixture(tmp_path)
    system_gid = os.getegid() + 1
    system_owned = module.PublicBaselineActivator(
        activation_id=ACTIVATION_ID,
        artifacts_root=activator.artifacts_root,
        lifecycle_guard=guard,
        active_root=activator.active_root,
        workspace_root=activator.workspace_root,
        directory_exchange=exchange,
        expected_uid=os.geteuid(),
        expected_operator_uid=os.geteuid(),
        expected_operator_gid=os.getegid(),
        expected_system_gid=system_gid,
    )

    def secret_entry(gid: int) -> SimpleNamespace:
        metadata = SimpleNamespace(
            st_mode=stat.S_IFREG | 0o600,
            st_uid=os.geteuid(),
            st_gid=gid,
            st_nlink=1,
            st_size=1,
        )
        return SimpleNamespace(
            is_symlink=lambda: False,
            stat=lambda *, follow_symlinks: metadata,
        )

    monkeypatch.setattr(module.os, "scandir", lambda _path: [secret_entry(system_gid)])
    system_owned._validate_secrets(
        system_owned.active_root / "deploy/secrets"
    )

    monkeypatch.setattr(
        module.os,
        "scandir",
        lambda _path: [secret_entry(os.getegid())],
    )
    with pytest.raises(
        module.PublicBaselineActivationError,
        match="secret metadata",
    ):
        system_owned._validate_secrets(
            system_owned.active_root / "deploy/secrets"
        )


def test_active_root_rejects_group_or_other_writable_runtime(
    tmp_path: Path,
) -> None:
    request, activator, _guard, _exchange, _public = _fixture(
        tmp_path / "other-write"
    )
    (activator.active_root / "backend/.venv").chmod(0o2772)
    with pytest.raises(
        module.PublicBaselineActivationError,
        match="virtual environment metadata",
    ):
        activator.prepare(request)

    request, activator, _guard, _exchange, _public = _fixture(
        tmp_path / "root-other-write"
    )
    activator.active_root.chmod(0o2772)
    with pytest.raises(
        module.PublicBaselineActivationError,
        match="active root ownership or mode",
    ):
        activator.prepare(request)
