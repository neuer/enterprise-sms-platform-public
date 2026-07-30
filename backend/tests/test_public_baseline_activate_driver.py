from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))

import public_baseline_activate as module  # noqa: E402
from public_baseline_activate import (  # noqa: E402
    ActivationError,
    build_activation_manifest,
    build_public_images,
    build_standard_request,
    load_built_images,
    load_preparation,
    prepare_public_bundle,
    write_activation_payloads,
)
from test_update_contract import parse_test_update_request  # noqa: E402

REPOSITORY = module.CANONICAL_REPOSITORY
BASE_COMMIT = "2" * 40
BASE_TREE = "3" * 40
MIGRATION = "0039_manual_job_outbox"


def _git(repository: Path, *arguments: str, input_text: str | None = None) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        input=input_text,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _isolated_repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "isolated-public-main"
    repository.mkdir(parents=True)
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Public Test")
    _git(repository, "config", "user.email", "public@example.test")
    (repository / "README.md").write_text("public baseline\n", encoding="utf-8")
    (repository / "backend").mkdir()
    (repository / "backend" / "migrations" / "versions").mkdir(parents=True)
    (repository / "backend" / "Dockerfile").write_text(
        "FROM scratch\n",
        encoding="utf-8",
    )
    (repository / "backend" / "pyproject.toml").write_text(
        '[project]\nname = "public-test"\nversion = "1.6.0"\n',
        encoding="utf-8",
    )
    (repository / "backend" / "migrations" / "versions" / "0039_test.py").write_text(
        'revision = "0039_manual_job_outbox"\ndown_revision = None\n',
        encoding="utf-8",
    )
    (repository / "frontend").mkdir()
    (repository / "frontend" / "Dockerfile").write_text(
        "FROM scratch\n",
        encoding="utf-8",
    )
    (repository / "frontend" / "package.json").write_text(
        '{"name":"public-test","version":"1.6.0"}\n',
        encoding="utf-8",
    )
    (repository / "frontend" / "package-lock.json").write_text(
        '{"name":"public-test","version":"1.6.0",'
        '"packages":{"":{"version":"1.6.0"}}}\n',
        encoding="utf-8",
    )
    (repository / "VERSION").write_text("1.6.0\n", encoding="ascii")
    (repository / "openapi.yaml").write_text(
        "info:\n  version: 1.6.0\n",
        encoding="utf-8",
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "public baseline")
    commit = _git(repository, "rev-parse", "HEAD")
    _git(
        repository,
        "remote",
        "add",
        "origin",
        f"https://github.com/{REPOSITORY}.git",
    )
    _git(repository, "update-ref", "refs/remotes/origin/main", commit)
    _git(
        repository,
        "symbolic-ref",
        "refs/remotes/origin/HEAD",
        "refs/remotes/origin/main",
    )
    return repository, commit


def _verifiers(
    calls: list[tuple[str, str, str]],
) -> tuple[Callable[[str, str], None], Callable[[str, str], None]]:
    def public(repository: str, commit: str) -> None:
        calls.append(("public", repository, commit))

    def ci(repository: str, commit: str) -> None:
        calls.append(("ci", repository, commit))

    return public, ci


def _prepare(
    tmp_path: Path,
) -> tuple[module.ActivationPreparation, list[tuple[str, str, str]]]:
    repository, commit = _isolated_repository(tmp_path)
    calls: list[tuple[str, str, str]] = []
    public, ci = _verifiers(calls)
    preparation = prepare_public_bundle(
        repository,
        tmp_path / "activation-artifacts",
        repository=REPOSITORY,
        expected_commit=commit,
        update_id=f"test-20260731T000000Z-{commit[:12]}",
        public_repository_verifier=public,
        ci_verifier=ci,
    )
    return preparation, calls


def _tar_bytes(name: str, payload: bytes) -> tuple[tarfile.TarInfo, io.BytesIO]:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = 0o644
    return info, io.BytesIO(payload)


class FakeDockerRunner:
    def __init__(self, *, mutate_context: bool = False) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.images: dict[str, tuple[str, bytes, dict[str, str]]] = {}
        self.context_snapshots: list[dict[str, bytes]] = []
        self.mutate_context = mutate_context

    @staticmethod
    def _argument(arguments: Sequence[str], name: str) -> str:
        index = arguments.index(name)
        return arguments[index + 1]

    @staticmethod
    def _build_arguments(arguments: Sequence[str]) -> dict[str, str]:
        values: dict[str, str] = {}
        for index, value in enumerate(arguments):
            if value == "--build-arg":
                key, item = arguments[index + 1].split("=", maxsplit=1)
                values[key] = item
        return values

    def _build(self, arguments: Sequence[str]) -> str:
        assert tuple(arguments[:3]) == ("docker", "buildx", "build")
        assert "--load" in arguments
        assert self._argument(arguments, "--platform") == "linux/amd64"
        context = Path(arguments[-1])
        dockerfile = Path(self._argument(arguments, "--file"))
        assert context in dockerfile.parents
        assert not (context / ".git").exists()
        snapshot = {
            path.relative_to(context).as_posix(): path.read_bytes()
            for path in context.rglob("*")
            if path.is_file()
        }
        self.context_snapshots.append(snapshot)
        values = self._build_arguments(arguments)
        ref = self._argument(arguments, "--tag")
        labels = {
            module._DOCKER_LABELS["version"]: values["APP_VERSION"],
            module._DOCKER_LABELS["revision"]: values["GIT_SHA"],
            module._DOCKER_LABELS["schema_revision"]: values["SCHEMA_REVISION"],
        }
        config = json.dumps(
            {
                "architecture": "amd64",
                "config": {"Labels": labels},
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        image_id = f"sha256:{hashlib.sha256(config).hexdigest()}"
        self.images[ref] = (image_id, config, labels)
        if self.mutate_context:
            (context / "README.md").write_text("mutated during build\n")
        return ""

    def _inspect(self, arguments: Sequence[str]) -> str:
        ref = arguments[-1]
        image_id, _config, labels = self.images[ref]
        return "|".join(
            (
                image_id,
                "amd64",
                labels[module._DOCKER_LABELS["version"]],
                labels[module._DOCKER_LABELS["revision"]],
                labels[module._DOCKER_LABELS["schema_revision"]],
            )
        )

    def _save(self, arguments: Sequence[str]) -> str:
        destination = Path(self._argument(arguments, "--output"))
        ref = arguments[-1]
        image_id, config, _labels = self.images[ref]
        config_name = f"{image_id.removeprefix('sha256:')}.json"
        manifest = json.dumps(
            [{"Config": config_name, "RepoTags": [ref], "Layers": []}],
            separators=(",", ":"),
        ).encode()
        with tarfile.open(destination, mode="w") as archive:
            info, stream = _tar_bytes("manifest.json", manifest)
            archive.addfile(info, stream)
            info, stream = _tar_bytes(config_name, config)
            archive.addfile(info, stream)
        return ""

    def run(
        self,
        arguments: Sequence[str],
        *,
        operation: str,
        timeout: int,
    ) -> str:
        del operation, timeout
        self.calls.append(tuple(arguments))
        if tuple(arguments[:3]) == ("docker", "buildx", "build"):
            return self._build(arguments)
        if tuple(arguments[:3]) == ("docker", "image", "inspect"):
            return self._inspect(arguments)
        if tuple(arguments[:3]) == ("docker", "image", "save"):
            return self._save(arguments)
        raise AssertionError(f"unexpected Docker command: {arguments!r}")


def test_prepare_generates_complete_single_main_bundle_without_manual_plan(
    tmp_path: Path,
) -> None:
    preparation, calls = _prepare(tmp_path)

    assert calls == [
        ("public", REPOSITORY, preparation.target.commit),
        ("ci", REPOSITORY, preparation.target.commit),
    ]
    assert {
        path.name for path in preparation.artifact_dir.iterdir()
    } == {
        module.BUNDLE_FILE,
        module.INVENTORY_FILE,
    }
    assert stat.S_IMODE(preparation.artifact_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(preparation.bundle.path.stat().st_mode) == 0o600
    assert (
        _git(
            preparation.workspace,
            "bundle",
            "list-heads",
            str(preparation.bundle.path),
        )
        == f"{preparation.target.commit} refs/heads/main"
    )

    inventory = json.loads(
        (preparation.artifact_dir / module.INVENTORY_FILE).read_text(
            encoding="utf-8"
        )
    )
    assert set(inventory) == module._INVENTORY_FIELDS
    assert inventory["ref"] == "refs/heads/main"
    assert inventory["commit"] == preparation.target.commit
    assert inventory["tree"] == preparation.target.tree
    assert inventory["object_count"] == preparation.inventory.object_count
    assert inventory["objects_sha256"] == preparation.inventory.objects_sha256
    assert inventory["bundle_sha256"] == hashlib.sha256(
        preparation.bundle.path.read_bytes()
    ).hexdigest()


def test_build_uses_bundle_blob_context_and_freezes_verified_images(
    tmp_path: Path,
) -> None:
    preparation, _calls = _prepare(tmp_path)
    runner = FakeDockerRunner()

    images = build_public_images(preparation, docker_runner=runner)

    assert set(images) == {"api", "web"}
    assert len(runner.context_snapshots) == 2
    for snapshot in runner.context_snapshots:
        assert snapshot["backend/Dockerfile"] == b"FROM scratch\n"
        assert snapshot["frontend/Dockerfile"] == b"FROM scratch\n"
        assert ".git/config" not in snapshot
    assert [call[:3] for call in runner.calls].count(
        ("docker", "buildx", "build")
    ) == 2
    assert [call[:3] for call in runner.calls].count(
        ("docker", "image", "inspect")
    ) == 4
    assert [call[:3] for call in runner.calls].count(
        ("docker", "image", "save")
    ) == 2
    for component in ("api", "web"):
        archive = preparation.artifact_dir / f"{component}.tar"
        assert stat.S_IMODE(archive.stat().st_mode) == 0o600
        assert archive.stat().st_nlink == 1
        assert images[component].sha256 == hashlib.sha256(
            archive.read_bytes()
        ).hexdigest()
    frozen = json.loads(
        (preparation.artifact_dir / module.BUILT_IMAGES_FILE).read_text(
            encoding="utf-8"
        )
    )
    assert set(frozen) == module._BUILT_IMAGES_FIELDS
    assert frozen["commit"] == preparation.target.commit
    assert frozen["tree"] == preparation.target.tree
    assert frozen["context"]["file_count"] == len(
        runner.context_snapshots[0]
    )
    assert load_built_images(preparation) == images
    assert not any(
        path.name.startswith(".canonical-image-build-")
        for path in preparation.artifact_dir.iterdir()
    )


def test_finalize_binds_manifest_and_standard_request_once(
    tmp_path: Path,
) -> None:
    preparation, _calls = _prepare(tmp_path)
    images = build_public_images(
        preparation,
        docker_runner=FakeDockerRunner(),
    )

    manifest = build_activation_manifest(
        preparation,
        base_commit=BASE_COMMIT,
        base_tree=BASE_TREE,
        migration_head=MIGRATION,
        images=images,
    )
    request = build_standard_request(
        preparation,
        base_commit=BASE_COMMIT,
        migration_head=MIGRATION,
        environment_mode="live",
        images=images,
    )
    parsed = parse_test_update_request(json.dumps(request))
    assert parsed.update_id == preparation.update_id
    assert parsed.base_commit == BASE_COMMIT
    assert parsed.commit == preparation.target.commit
    assert parsed.components == frozenset({"api", "web"})
    assert parsed.migration_from == parsed.migration_target == MIGRATION
    assert manifest["activation_id"] == request["update_id"]
    manifest_target = manifest["target"]
    manifest_base = manifest["base"]
    manifest_images = manifest["images"]
    request_images = request["images"]
    assert isinstance(manifest_target, dict)
    assert isinstance(manifest_base, dict)
    assert isinstance(manifest_images, dict)
    assert isinstance(request_images, dict)
    assert manifest_target["commit"] == request["commit"]
    assert manifest_base["commit"] == request["base_commit"]
    for component in ("api", "web"):
        manifest_image = manifest_images[component]
        request_image = request_images[component]
        assert isinstance(manifest_image, dict)
        assert isinstance(request_image, dict)
        assert manifest_image["id"] == request_image["id"]
        assert manifest_image["ref"] == request_image["ref"]
        assert manifest_image["file"] == request_image["archive_file"]
        assert manifest_image["sha256"] == request_image["archive_sha256"]

    manifest_path, request_path = write_activation_payloads(
        preparation,
        base_commit=BASE_COMMIT,
        base_tree=BASE_TREE,
        migration_head=MIGRATION,
        environment_mode="live",
    )
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(request_path.stat().st_mode) == 0o600
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == manifest
    assert json.loads(request_path.read_text(encoding="utf-8")) == request
    with pytest.raises(ActivationError, match="already exist"):
        write_activation_payloads(
            preparation,
            base_commit=BASE_COMMIT,
            base_tree=BASE_TREE,
            migration_head=MIGRATION,
            environment_mode="live",
        )


def test_prepare_build_finalize_cli_has_no_manual_image_id_surface(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository, commit = _isolated_repository(tmp_path)
    artifacts = tmp_path / "cli-artifacts"
    calls: list[tuple[str, str, str]] = []
    public, ci = _verifiers(calls)
    update_id = f"test-20260731T010101Z-{commit[:12]}"
    common = [
        "--workspace",
        str(repository),
        "--artifacts",
        str(artifacts),
        "--repository",
        REPOSITORY,
        "--commit",
        commit,
    ]

    assert module.main(
        ["prepare", *common, "--update-id", update_id],
        public_repository_verifier=public,
        ci_verifier=ci,
    ) == 0
    prepared = json.loads(capsys.readouterr().out)
    assert prepared["state"] == "prepared"
    runner = FakeDockerRunner()
    assert module.main(
        ["build", *common],
        public_repository_verifier=public,
        ci_verifier=ci,
        docker_runner=runner,
    ) == 0
    built = json.loads(capsys.readouterr().out)
    assert built["state"] == "images-ready"
    assert module.main(
        [
            "finalize",
            *common,
            "--base-commit",
            BASE_COMMIT,
            "--base-tree",
            BASE_TREE,
            "--migration-head",
            MIGRATION,
            "--environment-mode",
            "live",
        ],
        public_repository_verifier=public,
        ci_verifier=ci,
    ) == 0
    finalized = json.loads(capsys.readouterr().out)
    assert finalized["state"] == "manifest-ready"
    assert calls == [
        ("public", REPOSITORY, commit),
        ("ci", REPOSITORY, commit),
    ] * 3
    with pytest.raises(SystemExit):
        module._parser().parse_args(
            [
                "finalize",
                *common,
                "--base-commit",
                BASE_COMMIT,
                "--base-tree",
                BASE_TREE,
                "--migration-head",
                MIGRATION,
                "--environment-mode",
                "live",
                "--api-image-id",
                "sha256:" + "a" * 64,
            ]
        )


def test_prepare_rejects_dirty_ignored_and_non_main_sources(tmp_path: Path) -> None:
    repository, commit = _isolated_repository(tmp_path)
    public, ci = _verifiers([])

    (repository / "dirty.txt").write_text("dirty", encoding="utf-8")
    with pytest.raises(ActivationError, match="clean"):
        prepare_public_bundle(
            repository,
            tmp_path / "dirty-output",
            repository=REPOSITORY,
            expected_commit=commit,
            public_repository_verifier=public,
            ci_verifier=ci,
        )
    (repository / "dirty.txt").unlink()

    (repository / ".gitignore").write_text("local-only\n", encoding="utf-8")
    _git(repository, "add", ".gitignore")
    _git(repository, "commit", "-m", "ignore local content")
    commit = _git(repository, "rev-parse", "HEAD")
    _git(repository, "update-ref", "refs/remotes/origin/main", commit)
    (repository / "local-only").write_text("must not be read\n", encoding="utf-8")
    with pytest.raises(ActivationError, match="ignored"):
        prepare_public_bundle(
            repository,
            tmp_path / "ignored-output",
            repository=REPOSITORY,
            expected_commit=commit,
            public_repository_verifier=public,
            ci_verifier=ci,
        )
    (repository / "local-only").unlink()

    _git(repository, "switch", "-c", "other")
    with pytest.raises(ActivationError, match="main"):
        prepare_public_bundle(
            repository,
            tmp_path / "branch-output",
            repository=REPOSITORY,
            expected_commit=commit,
            public_repository_verifier=public,
            ci_verifier=ci,
        )


def test_prepare_rejects_extra_refs_alternates_and_unreachable_objects(
    tmp_path: Path,
) -> None:
    public, ci = _verifiers([])

    extra_repo, extra_commit = _isolated_repository(tmp_path / "extra")
    _git(extra_repo, "update-ref", "refs/heads/extra", extra_commit)
    with pytest.raises(ActivationError, match="only main refs"):
        prepare_public_bundle(
            extra_repo,
            tmp_path / "extra-output",
            repository=REPOSITORY,
            expected_commit=extra_commit,
            public_repository_verifier=public,
            ci_verifier=ci,
        )

    alternate_repo, alternate_commit = _isolated_repository(tmp_path / "alternate")
    alternate_file = Path(
        _git(
            alternate_repo,
            "rev-parse",
            "--git-path",
            "objects/info/alternates",
        )
    )
    if not alternate_file.is_absolute():
        alternate_file = alternate_repo / alternate_file
    alternate_file.parent.mkdir(parents=True, exist_ok=True)
    alternate_file.write_text("/untrusted/object-store\n", encoding="utf-8")
    with pytest.raises(ActivationError, match="alternates"):
        prepare_public_bundle(
            alternate_repo,
            tmp_path / "alternate-output",
            repository=REPOSITORY,
            expected_commit=alternate_commit,
            public_repository_verifier=public,
            ci_verifier=ci,
        )

    unreachable_repo, unreachable_commit = _isolated_repository(
        tmp_path / "unreachable"
    )
    _git(unreachable_repo, "hash-object", "-w", "--stdin", input_text="orphan")
    with pytest.raises(ActivationError, match="reachable"):
        prepare_public_bundle(
            unreachable_repo,
            tmp_path / "unreachable-output",
            repository=REPOSITORY,
            expected_commit=unreachable_commit,
            public_repository_verifier=public,
            ci_verifier=ci,
        )


def test_prepare_rejects_external_gitdir_filters_and_worktree_hardlinks(
    tmp_path: Path,
) -> None:
    public, ci = _verifiers([])

    external_repo, external_commit = _isolated_repository(tmp_path / "gitdir")
    external_git = tmp_path / "external-object-store.git"
    shutil.move(str(external_repo / ".git"), external_git)
    (external_repo / ".git").write_text(
        f"gitdir: {external_git}\n",
        encoding="utf-8",
    )
    with pytest.raises(ActivationError, match="in-workspace .git"):
        prepare_public_bundle(
            external_repo,
            tmp_path / "gitdir-output",
            repository=REPOSITORY,
            expected_commit=external_commit,
            public_repository_verifier=public,
            ci_verifier=ci,
        )

    filter_repo, filter_commit = _isolated_repository(tmp_path / "filter")
    _git(filter_repo, "config", "filter.private.clean", "cat")
    _git(filter_repo, "config", "filter.private.smudge", "cat")
    with pytest.raises(ActivationError, match="filters"):
        prepare_public_bundle(
            filter_repo,
            tmp_path / "filter-output",
            repository=REPOSITORY,
            expected_commit=filter_commit,
            public_repository_verifier=public,
            ci_verifier=ci,
        )

    hardlink_repo, hardlink_commit = _isolated_repository(tmp_path / "hardlink")
    outside = tmp_path / "hardlink-source"
    outside.write_bytes((hardlink_repo / "README.md").read_bytes())
    (hardlink_repo / "README.md").unlink()
    os.link(outside, hardlink_repo / "README.md")
    assert _git(hardlink_repo, "status", "--porcelain") == ""
    with pytest.raises(ActivationError, match="hardlink|identity"):
        prepare_public_bundle(
            hardlink_repo,
            tmp_path / "hardlink-output",
            repository=REPOSITORY,
            expected_commit=hardlink_commit,
            public_repository_verifier=public,
            ci_verifier=ci,
        )


def test_prepare_rejects_line_ending_drift_and_lfs_pointer(tmp_path: Path) -> None:
    public, ci = _verifiers([])
    newline_repo, newline_commit = _isolated_repository(tmp_path / "newline")
    _git(newline_repo, "config", "core.autocrlf", "true")
    dockerfile = newline_repo / "backend" / "Dockerfile"
    dockerfile.unlink()
    _git(newline_repo, "checkout", "--", "backend/Dockerfile")
    assert b"\r\n" in dockerfile.read_bytes()
    assert _git(newline_repo, "status", "--porcelain") == ""
    with pytest.raises(ActivationError, match="line-ending"):
        prepare_public_bundle(
            newline_repo,
            tmp_path / "newline-output",
            repository=REPOSITORY,
            expected_commit=newline_commit,
            public_repository_verifier=public,
            ci_verifier=ci,
        )

    lfs_repo, _commit = _isolated_repository(tmp_path / "lfs")
    (lfs_repo / "large.bin").write_text(
        "version " + "https://" + "git-lfs.github.com/spec/v1\n"
        "oid sha256:" + "a" * 64 + "\n"
        "size 100\n",
        encoding="ascii",
    )
    _git(lfs_repo, "add", "large.bin")
    _git(lfs_repo, "commit", "-m", "add lfs pointer")
    lfs_commit = _git(lfs_repo, "rev-parse", "HEAD")
    _git(lfs_repo, "update-ref", "refs/remotes/origin/main", lfs_commit)
    with pytest.raises(ActivationError, match="LFS"):
        prepare_public_bundle(
            lfs_repo,
            tmp_path / "lfs-output",
            repository=REPOSITORY,
            expected_commit=lfs_commit,
            public_repository_verifier=public,
            ci_verifier=ci,
        )


def test_build_detects_context_mutation_before_freezing_images(
    tmp_path: Path,
) -> None:
    preparation, _calls = _prepare(tmp_path)

    with pytest.raises(ActivationError, match="content differs"):
        build_public_images(
            preparation,
            docker_runner=FakeDockerRunner(mutate_context=True),
        )

    assert not (preparation.artifact_dir / module.API_ARCHIVE).exists()
    assert not (preparation.artifact_dir / module.WEB_ARCHIVE).exists()
    assert not (preparation.artifact_dir / module.BUILT_IMAGES_FILE).exists()


def test_finalize_rejects_artifact_symlink_hardlink_and_archive_tampering(
    tmp_path: Path,
) -> None:
    preparation, _calls = _prepare(tmp_path)
    public, ci = _verifiers([])
    alias = tmp_path / "artifact-alias"
    alias.symlink_to(preparation.artifact_dir, target_is_directory=True)
    with pytest.raises(ActivationError, match="real directory"):
        load_preparation(
            preparation.workspace,
            alias,
            repository=REPOSITORY,
            expected_commit=preparation.target.commit,
            public_repository_verifier=public,
            ci_verifier=ci,
        )

    build_public_images(preparation, docker_runner=FakeDockerRunner())
    api = preparation.artifact_dir / module.API_ARCHIVE
    backing = tmp_path / "api-archive-backing.tar"
    api.rename(backing)
    os.link(backing, api)
    with pytest.raises(ActivationError, match="identity is unsafe"):
        load_built_images(preparation)


def test_prepare_requires_isolation_and_exact_ci_before_writing_bundle(
    tmp_path: Path,
) -> None:
    repository, commit = _isolated_repository(tmp_path)

    with pytest.raises(ActivationError, match="outside"):
        prepare_public_bundle(
            ROOT,
            tmp_path / "root-output",
            repository=REPOSITORY,
            expected_commit=commit,
            public_repository_verifier=lambda _repo, _commit: None,
            ci_verifier=lambda _repo, _commit: None,
        )
    with pytest.raises(ActivationError, match="isolated"):
        prepare_public_bundle(
            repository,
            repository / "artifacts",
            repository=REPOSITORY,
            expected_commit=commit,
            public_repository_verifier=lambda _repo, _commit: None,
            ci_verifier=lambda _repo, _commit: None,
        )

    output = tmp_path / "ci-failed-output"

    def failed_ci(_repository: str, _commit: str) -> None:
        raise ActivationError("exact CI failed")

    with pytest.raises(ActivationError, match="CI failed"):
        prepare_public_bundle(
            repository,
            output,
            repository=REPOSITORY,
            expected_commit=commit,
            public_repository_verifier=lambda _repo, _commit: None,
            ci_verifier=failed_ci,
        )
    assert not output.exists()


def test_cli_normalizes_missing_artifact_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository, commit = _isolated_repository(tmp_path)
    public, ci = _verifiers([])
    result = module.main(
        [
            "finalize",
            "--workspace",
            str(repository),
            "--artifacts",
            str(tmp_path / "missing"),
            "--repository",
            REPOSITORY,
            "--commit",
            commit,
            "--base-commit",
            BASE_COMMIT,
            "--base-tree",
            BASE_TREE,
            "--migration-head",
            MIGRATION,
            "--environment-mode",
            "live",
        ],
        public_repository_verifier=public,
        ci_verifier=ci,
    )
    captured = capsys.readouterr()
    assert result == 1
    assert "public-baseline-activation:" in captured.err
    assert "Traceback" not in captured.err


def test_source_contains_no_manual_build_plan_remote_mutation_or_secret_input() -> None:
    source = (SCRIPTS / "public_baseline_activate.py").read_text(encoding="utf-8")
    lowered = source.lower()

    assert "refs/heads/main" in source
    assert "verify_ci_commit.py" in source
    assert "build_public_images" in source
    assert "_materialize_verified_context" in source
    assert "_verify_context(context, inventory)" in source
    assert '"buildx"' in source
    assert '"inspect"' in source
    assert '"save"' in source
    assert "build_command_plan" not in source
    assert "PLAN_FILE" not in source
    assert "--api-image-id" not in source
    assert "--web-image-id" not in source
    for forbidden in (
        "ssh ",
        "rsync ",
        "scp ",
        "--password",
        "--secret",
        "private_merge_base",
        "source_pack",
        "git push",
    ):
        assert forbidden not in lowered
