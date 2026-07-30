from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from export_public_snapshot import export_snapshot  # noqa: E402
from verify_public_snapshot_cutover import (  # noqa: E402
    PublicSnapshotCutoverError,
    verify_public_snapshot_cutover,
)


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def _write(repository: Path, relative: str, content: str) -> None:
    destination = repository / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")


def _commit(repository: Path, message: str) -> str:
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


def _clear_worktree(repository: Path) -> None:
    for candidate in repository.iterdir():
        if candidate.name == ".git":
            continue
        if candidate.is_dir() and not candidate.is_symlink():
            shutil.rmtree(candidate)
        else:
            candidate.unlink()


def _write_source_pack(
    repository: Path,
    *,
    source: str,
    destination: Path,
    extra_objects: tuple[str, ...] = (),
) -> None:
    tree_objects = _git(
        repository,
        "rev-list",
        "--objects",
        "--no-object-names",
        f"{source}^{{tree}}",
    ).splitlines()
    object_ids = [source, *tree_objects, *extra_objects]
    result = subprocess.run(
        ["git", "-C", str(repository), "pack-objects", "--stdout"],
        input=("\n".join(object_ids) + "\n").encode(),
        capture_output=True,
        check=True,
    )
    destination.write_bytes(result.stdout)


def _build_server_repository(
    tmp_path: Path,
    *,
    source_repository: Path,
    baseline: str,
    source: str,
    target: str,
) -> tuple[Path, str]:
    server = tmp_path / "server"
    server.mkdir()
    _git(server, "init")
    _git(
        server,
        "fetch",
        str(source_repository),
        "refs/heads/deployed:refs/heads/deployed",
    )
    _git(
        server,
        "fetch",
        str(source_repository),
        "refs/heads/public:refs/heads/public",
    )
    assert _git(server, "rev-parse", "refs/heads/deployed") == baseline
    assert _git(server, "rev-parse", "refs/heads/public") == target
    missing_source = subprocess.run(
        ["git", "-C", str(server), "cat-file", "-e", f"{source}^{{commit}}"],
        capture_output=True,
        check=False,
    )
    assert missing_source.returncode != 0
    fetched_ref = "refs/test-updates/test-20260728T074728Z-1d6f777b7206/source"
    _git(server, "update-ref", fetched_ref, target)
    return server, fetched_ref


def _build_cutover_repository(
    tmp_path: Path,
) -> tuple[Path, str, str, str, str, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Cutover Test")
    _git(repository, "config", "user.email", "cutover@example.invalid")

    policy = {
        "schema_version": 1,
        "excluded_paths": ["docs/private/**"],
        "required_public_files": [
            "README.md",
            "LICENSE",
            "SECURITY.md",
            "CONTRIBUTING.md",
            "PUBLICATION.md",
            "public-repository.json",
        ],
        "documentation_phone_allowlist": [],
    }
    for filename in (
        "README.md",
        "LICENSE",
        "SECURITY.md",
        "CONTRIBUTING.md",
        "PUBLICATION.md",
    ):
        _write(repository, filename, f"{filename} synthetic public content\n")
    _write(
        repository,
        "public-repository.json",
        json.dumps(policy, indent=2, sort_keys=True) + "\n",
    )
    _write(repository, "backend/app/runtime.py", "VERSION = 1\n")
    _write(
        repository,
        "deploy/scripts/prepare_runtime_secrets.py",
        "VERSION = 1\n",
    )
    private_merge_base = _commit(repository, "private baseline")

    _write(repository, "backend/app/runtime.py", "VERSION = 2\n")
    _write(
        repository,
        "deploy/scripts/prepare_runtime_secrets.py",
        "VERSION = 2\n",
    )
    _write(repository, "docs/private/evidence.txt", "private evidence\n")
    source = _commit(repository, "private publication source")

    exported = tmp_path / "exported"
    assert (
        export_snapshot(
            repository,
            ref=source,
            output=exported,
            policy_path=repository / "public-repository.json",
        )
        == source
    )
    _git(repository, "switch", "-c", "deployed", private_merge_base)
    _write(repository, "backend/app/deployed_only.py", "DEPLOYED_FIX = True\n")
    deployed_baseline = _commit(repository, "deployed private branch")

    _git(repository, "switch", "--orphan", "public")
    _clear_worktree(repository)
    shutil.copytree(exported, repository, dirs_exist_ok=True)
    publication = _commit(repository, "initial public snapshot")

    _write(repository, "backend/app/runtime.py", "VERSION = 3\n")
    _write(repository, "docs/public-change.md", "public follow-up\n")
    _write(repository, "PROGRESS.md", "public cutover progress\n")
    target = _commit(repository, "public follow-up")
    _git(repository, "update-ref", "refs/remotes/origin/main", target)
    return (
        repository,
        deployed_baseline,
        source,
        private_merge_base,
        publication,
        target,
    )


def test_verifies_reproducible_public_snapshot_cutover(tmp_path: Path) -> None:
    (
        repository,
        baseline,
        source,
        private_merge_base,
        publication,
        target,
    ) = _build_cutover_repository(tmp_path)

    result = verify_public_snapshot_cutover(
        repository,
        baseline=baseline,
        target=target,
        ref="origin/main",
    )

    assert result == {
        "components": ["api"],
        "cutover": True,
        "logical_changed": 5,
        "migration_changed": False,
        "private_merge_base": private_merge_base,
        "publication_commit": publication,
        "risk": "high-risk",
        "runtime_changed": True,
        "source_commit": source,
    }


def test_rejects_tampered_snapshot_provenance(tmp_path: Path) -> None:
    repository, baseline, source, _merge_base, _publication, _target = (
        _build_cutover_repository(tmp_path)
    )
    manifest_path = repository / "PUBLIC-SNAPSHOT.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["policy_sha256"] = hashlib.sha256(b"tampered").hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tampered_target = _commit(repository, "tamper provenance")
    _git(
        repository,
        "update-ref",
        "refs/remotes/origin/main",
        tampered_target,
    )

    with pytest.raises(
        PublicSnapshotCutoverError,
        match="policy digest",
    ):
        verify_public_snapshot_cutover(
            repository,
            baseline=baseline,
            target=tampered_target,
            ref="origin/main",
        )

    assert source != tampered_target


def test_rejects_cutover_from_non_main_ref(tmp_path: Path) -> None:
    repository, baseline, _source, _merge_base, _publication, target = (
        _build_cutover_repository(tmp_path)
    )

    with pytest.raises(PublicSnapshotCutoverError, match="origin/main"):
        verify_public_snapshot_cutover(
            repository,
            baseline=baseline,
            target=target,
            ref="origin/feature",
        )


def test_verifies_remote_fetched_ref_without_trusting_stale_tracking_ref(
    tmp_path: Path,
) -> None:
    repository, baseline, _source, _merge_base, publication, target = (
        _build_cutover_repository(tmp_path)
    )
    fetched_ref = "refs/test-updates/test-20260728T063501Z-a06082dbc95c/source"
    _git(repository, "update-ref", fetched_ref, target)
    _git(repository, "update-ref", "refs/remotes/origin/main", publication)

    result = verify_public_snapshot_cutover(
        repository,
        baseline=baseline,
        target=target,
        ref="origin/main",
        resolved_ref=fetched_ref,
    )

    assert result["cutover"] is True
    assert result["publication_commit"] == publication


def test_rejects_unsafe_remote_fetched_ref(tmp_path: Path) -> None:
    repository, baseline, _source, _merge_base, _publication, target = (
        _build_cutover_repository(tmp_path)
    )

    with pytest.raises(PublicSnapshotCutoverError, match="resolved ref"):
        verify_public_snapshot_cutover(
            repository,
            baseline=baseline,
            target=target,
            ref="origin/main",
            resolved_ref="--upload-pack=attacker",
        )


def test_verifies_cutover_with_minimal_source_tree_pack(
    tmp_path: Path,
) -> None:
    (
        repository,
        baseline,
        source,
        private_merge_base,
        publication,
        target,
    ) = _build_cutover_repository(tmp_path)
    source_pack = tmp_path / "cutover-source.pack"
    _write_source_pack(
        repository,
        source=source,
        destination=source_pack,
    )
    server, fetched_ref = _build_server_repository(
        tmp_path,
        source_repository=repository,
        baseline=baseline,
        source=source,
        target=target,
    )

    result = verify_public_snapshot_cutover(
        server,
        baseline=baseline,
        target=target,
        ref="origin/main",
        resolved_ref=fetched_ref,
        source_pack=source_pack,
        expected_source_commit=source,
        expected_private_merge_base=private_merge_base,
    )

    assert result["source_commit"] == source
    assert result["private_merge_base"] == private_merge_base
    assert result["publication_commit"] == publication
    assert result["components"] == ["api"]


def test_rejects_source_pack_with_unexpected_objects(tmp_path: Path) -> None:
    repository, baseline, source, private_merge_base, _publication, target = (
        _build_cutover_repository(tmp_path)
    )
    source_pack = tmp_path / "cutover-source.pack"
    _write_source_pack(
        repository,
        source=source,
        destination=source_pack,
        extra_objects=(target,),
    )
    server, fetched_ref = _build_server_repository(
        tmp_path,
        source_repository=repository,
        baseline=baseline,
        source=source,
        target=target,
    )

    with pytest.raises(PublicSnapshotCutoverError, match="unexpected objects"):
        verify_public_snapshot_cutover(
            server,
            baseline=baseline,
            target=target,
            ref="origin/main",
            resolved_ref=fetched_ref,
            source_pack=source_pack,
            expected_source_commit=source,
            expected_private_merge_base=private_merge_base,
        )


def test_rejects_source_pack_not_bound_to_snapshot_manifest(
    tmp_path: Path,
) -> None:
    repository, baseline, source, private_merge_base, _publication, target = (
        _build_cutover_repository(tmp_path)
    )
    source_pack = tmp_path / "cutover-source.pack"
    _write_source_pack(
        repository,
        source=source,
        destination=source_pack,
    )
    server, fetched_ref = _build_server_repository(
        tmp_path,
        source_repository=repository,
        baseline=baseline,
        source=source,
        target=target,
    )

    with pytest.raises(PublicSnapshotCutoverError, match="binding"):
        verify_public_snapshot_cutover(
            server,
            baseline=baseline,
            target=target,
            ref="origin/main",
            resolved_ref=fetched_ref,
            source_pack=source_pack,
            expected_source_commit="f" * 40,
            expected_private_merge_base=private_merge_base,
        )
