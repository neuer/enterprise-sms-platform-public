#!/usr/bin/env python3
"""通过固定 argv 将封闭四镜像发布包交给远端唯一生命周期入口。"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import shlex
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))

from release_manifest import (  # type: ignore[import-not-found,unused-ignore]  # noqa: E402
    ReleaseManifest,
    ReleaseManifestError,
    load_manifest,
)


class RemoteReleaseError(RuntimeError):
    """远端发布编排未满足失败关闭契约。"""


class CommandRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess[str]: ...


class SubprocessRunner:
    """仅以参数数组运行本地工具。"""

    def run(
        self,
        argv: Sequence[str],
        *,
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(argv),
            check=False,
            capture_output=capture_output,
            text=True,
        )


@dataclass(frozen=True)
class RemoteTarget:
    host: str
    port: int
    user: str
    platform_root: str
    secrets_mode: Literal["development", "production"]
    runtime_root: str


_HOST_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
_USER_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
_REMOTE_REF_RE = re.compile(r"origin/[A-Za-z0-9](?:[A-Za-z0-9._/-]{0,126}[A-Za-z0-9])?")
_HEX_40_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])")
_HEX_64_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")
_SHA_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
_VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_WORKFLOW_REPOSITORY_RE = re.compile(
    r"(?:local|[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"
)
_IMAGE_NAMES = ("api", "web", "postgres", "redis")
_RELEASE_REPORT_FIELDS = {
    "schema_version",
    "gate_type",
    "candidate_commit",
    "source",
    "generated_at",
    "trivy_image",
    "images",
    "promotion_source",
    "passed",
}
_RELEASE_SOURCE_FIELDS = {
    "app_version",
    "git_sha",
    "schema_revision",
    "openapi_sha256",
    "workflow_repository",
    "workflow_run_id",
    "workflow_run_attempt",
    "sbom_sha256",
}
_RELEASE_IMAGE_FIELDS = {
    "ref",
    "image_id",
    "repo_digests",
    "scan_report_sha256",
    "scan_passed",
}
_FINAL_RELEASE_ROOT = PurePosixPath("/var/lib/sms-platform/releases")
_PROMOTION_SOURCE_FIELDS = {"report_sha256", "candidate_commit", "source", "images"}
_PROMOTION_IMAGE_FIELDS = {"ref", "image_id", "scan_report_sha256"}
_RSYNC_UPLOAD_ATTEMPTS = 3
_VENDOR_AGENT_RUNTIME_PATHS = (
    "deploy/scripts/vendor_control_agent.py",
    "deploy/scripts/vendor_control_journal.py",
    "deploy/scripts/vendor_control_protocol.py",
    "deploy/scripts/vendor_credential_store.py",
    "deploy/scripts/vendor_control_reload.py",
    "deploy/scripts/vendor_seal_sessions.py",
)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RemoteReleaseError("release bundle JSON contains duplicate fields")
        result[key] = value
    return result


def _read_json(path: Path, context: str) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream, object_pairs_hook=_reject_duplicate_keys)
    except RemoteReleaseError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RemoteReleaseError(f"{context} is not readable strict JSON") from exc
    if type(value) is not dict:
        raise RemoteReleaseError(f"{context} must be a JSON object")
    return cast(dict[str, Any], value)


def _validate_remote_path(value: str, context: str) -> PurePosixPath:
    if type(value) is not str or not value.startswith("/") or "//" in value:
        raise RemoteReleaseError(f"invalid {context}")
    path = PurePosixPath(value)
    if str(path) != value or path == PurePosixPath("/"):
        raise RemoteReleaseError(f"invalid {context}")
    if any(
        part in {".", ".."} or re.fullmatch(r"[A-Za-z0-9._-]+", part) is None
        for part in path.parts[1:]
    ):
        raise RemoteReleaseError(f"invalid {context}")
    return path


def _validate_target(target: RemoteTarget, manifest: ReleaseManifest) -> None:
    if _HOST_RE.fullmatch(target.host) is None or ".." in target.host:
        raise RemoteReleaseError("invalid remote host")
    if _USER_RE.fullmatch(target.user) is None:
        raise RemoteReleaseError("invalid remote user")
    if type(target.port) is not int or not 1 <= target.port <= 65535:
        raise RemoteReleaseError("invalid remote port")
    platform_root = _validate_remote_path(target.platform_root, "platform root")
    runtime_root = _validate_remote_path(target.runtime_root, "runtime root")
    if runtime_root == platform_root or platform_root in runtime_root.parents:
        raise RemoteReleaseError("runtime root must be outside the Git worktree")
    if runtime_root == _FINAL_RELEASE_ROOT or _FINAL_RELEASE_ROOT in runtime_root.parents:
        raise RemoteReleaseError("runtime root must be outside the final release store")
    if target.secrets_mode != manifest.mode:
        raise RemoteReleaseError("remote mode does not match manifest mode")


def _validate_public_url(value: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != "/readyz"
    ):
        raise RemoteReleaseError("public probe URL must target /readyz")


def _expected_bundle_names(manifest: ReleaseManifest) -> set[str]:
    names = {"manifest.json", cast(str, manifest.evidence["release_gate"])}
    for image in manifest.images.values():
        if image.archive_file is not None:
            names.add(image.archive_file)
    data_images = manifest.evidence["data_images"]
    if data_images is not None:
        names.add(cast(str, data_images))
    backup = manifest.evidence["backup_restore_change"]
    if backup is not None:
        if not isinstance(backup, Mapping):
            raise RemoteReleaseError("invalid backup evidence mapping")
        names.update(backup.values())
    return names


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise RemoteReleaseError("release bundle file is not readable") from exc
    return digest.hexdigest()


def _validate_release_evidence(path: Path, manifest: ReleaseManifest) -> None:
    report = _read_json(path, "release gate evidence")
    if set(report) != _RELEASE_REPORT_FIELDS:
        raise RemoteReleaseError("release gate evidence has invalid fields")
    if (
        report["schema_version"] != 1
        or report["gate_type"] != "release"
        or report["passed"] is not True
        or report["candidate_commit"] != manifest.commit
    ):
        raise RemoteReleaseError("release gate evidence is not bound to the manifest")
    release_source = _validate_release_source(
        report["source"],
        manifest,
        "release source metadata",
    )
    images = report["images"]
    if type(images) is not dict or set(images) != set(_IMAGE_NAMES):
        raise RemoteReleaseError("release gate evidence must contain four images")
    for name in _IMAGE_NAMES:
        image = images[name]
        if type(image) is not dict or set(image) != _RELEASE_IMAGE_FIELDS:
            raise RemoteReleaseError("release gate image evidence has invalid fields")
        if (
            image["ref"] != manifest.images[name].ref
            or image["image_id"] != manifest.images[name].image_id
            or type(image["scan_report_sha256"]) is not str
            or _HEX_64_RE.fullmatch(image["scan_report_sha256"]) is None
            or image["scan_passed"] is not True
        ):
            raise RemoteReleaseError("release gate image evidence is not bound to the manifest")
    promotion = report["promotion_source"]
    if manifest.mode == "development":
        if promotion is not None:
            raise RemoteReleaseError("development release evidence cannot be promoted")
        return
    if type(promotion) is not dict or set(promotion) != _PROMOTION_SOURCE_FIELDS:
        raise RemoteReleaseError("production release evidence lacks candidate-build provenance")
    if (
        promotion["candidate_commit"] != manifest.commit
        or type(promotion["report_sha256"]) is not str
        or _HEX_64_RE.fullmatch(promotion["report_sha256"]) is None
    ):
        raise RemoteReleaseError("production promotion source is not bound")
    promotion_source = _validate_release_source(
        promotion["source"],
        manifest,
        "promotion source metadata",
    )
    if promotion_source != release_source:
        raise RemoteReleaseError("production promotion source metadata is not bound")
    source_images = promotion["images"]
    if type(source_images) is not dict or set(source_images) != set(_IMAGE_NAMES):
        raise RemoteReleaseError("production promotion source images are invalid")
    for name in _IMAGE_NAMES:
        source = source_images[name]
        if (
            type(source) is not dict
            or set(source) != _PROMOTION_IMAGE_FIELDS
            or source["ref"] != f"sms-platform-release-{name}:{manifest.commit}"
            or source["image_id"] != manifest.images[name].image_id
            or type(source["scan_report_sha256"]) is not str
            or _HEX_64_RE.fullmatch(source["scan_report_sha256"]) is None
        ):
            raise RemoteReleaseError("production promotion source image is not bound")


def _validate_release_source(
    value: object,
    manifest: ReleaseManifest,
    context: str,
) -> dict[str, Any]:
    if type(value) is not dict or set(value) != _RELEASE_SOURCE_FIELDS:
        raise RemoteReleaseError(f"{context} has invalid fields")
    source = cast(dict[str, Any], value)
    sboms = source["sbom_sha256"]
    workflow_repository = source["workflow_repository"]
    if (
        type(source["app_version"]) is not str
        or _VERSION_RE.fullmatch(source["app_version"]) is None
        or source["git_sha"] != manifest.commit
        or source["schema_revision"] != manifest.migration_target
        or type(source["openapi_sha256"]) is not str
        or _HEX_64_RE.fullmatch(source["openapi_sha256"]) is None
        or type(workflow_repository) is not str
        or _WORKFLOW_REPOSITORY_RE.fullmatch(workflow_repository) is None
        or (manifest.mode == "production" and workflow_repository == "local")
        or type(source["workflow_run_id"]) is not int
        or type(source["workflow_run_attempt"]) is not int
        or source["workflow_run_id"] < (1 if workflow_repository != "local" else 0)
        or source["workflow_run_attempt"]
        < (1 if workflow_repository != "local" else 0)
        or type(sboms) is not dict
        or set(sboms) != set(_IMAGE_NAMES)
        or any(
            type(digest) is not str or _HEX_64_RE.fullmatch(digest) is None
            for digest in sboms.values()
        )
    ):
        raise RemoteReleaseError(f"{context} is invalid")
    return source


def _validated_bundle(
    manifest_path: Path,
) -> tuple[ReleaseManifest, list[Path], dict[Path, str]]:
    if not manifest_path.is_absolute() or manifest_path.name != "manifest.json":
        raise RemoteReleaseError("manifest must be an absolute manifest.json path")
    try:
        parent_info = manifest_path.parent.lstat()
    except OSError as exc:
        raise RemoteReleaseError("release bundle directory is unavailable") from exc
    if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
        raise RemoteReleaseError("release bundle directory is unsafe")
    try:
        manifest = load_manifest(manifest_path)
    except ReleaseManifestError as exc:
        raise RemoteReleaseError("release manifest is invalid") from exc
    expected = _expected_bundle_names(manifest)
    try:
        entries = list(os.scandir(manifest_path.parent))
    except OSError as exc:
        raise RemoteReleaseError("release bundle inventory is unavailable") from exc
    if {entry.name for entry in entries} != expected:
        raise RemoteReleaseError("release bundle is not a closed file set")
    for entry in entries:
        if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
            raise RemoteReleaseError("release bundle is not a closed regular-file set")
    files = [manifest_path.parent / name for name in sorted(expected)]
    local_hashes = {path: _sha256_file(path) for path in files}
    for image in manifest.images.values():
        if image.archive_file is None:
            continue
        archive_path = manifest_path.parent / image.archive_file
        if image.archive_sha256 is None or not hmac.compare_digest(
            local_hashes[archive_path], image.archive_sha256
        ):
            raise RemoteReleaseError("development archive does not match the manifest")
    release_gate_path = (
        manifest_path.parent / cast(str, manifest.evidence["release_gate"])
    )
    expected_release_gate_hash = cast(
        str,
        manifest.evidence["release_gate_sha256"],
    )
    if not hmac.compare_digest(
        local_hashes[release_gate_path],
        expected_release_gate_hash,
    ):
        raise RemoteReleaseError("release gate evidence hash does not match manifest")
    _validate_release_evidence(release_gate_path, manifest)
    return manifest, files, local_hashes


def _validated_arguments(
    arguments: argparse.Namespace,
) -> tuple[ReleaseManifest, RemoteTarget, list[Path], dict[Path, str]]:
    manifest, files, local_hashes = _validated_bundle(Path(arguments.manifest))
    target = cast(RemoteTarget, arguments.target)
    _validate_target(target, manifest)
    remote_ref = arguments.remote_ref
    if (
        type(remote_ref) is not str
        or _REMOTE_REF_RE.fullmatch(remote_ref) is None
        or ".." in remote_ref
    ):
        raise RemoteReleaseError("invalid remote ref")
    public_urls = tuple(arguments.public_urls)
    for url in public_urls:
        _validate_public_url(url)
    return manifest, target, files, local_hashes


def _ssh_base(target: RemoteTarget) -> list[str]:
    return [
        "ssh",
        "-p",
        str(target.port),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        f"{target.user}@{target.host}",
    ]


def _ssh_transport(target: RemoteTarget) -> str:
    return f"ssh -p {target.port} -o BatchMode=yes -o StrictHostKeyChecking=yes"


def _remote_fetch_refspec(remote_ref: str) -> str:
    branch = remote_ref.removeprefix("origin/")
    return f"refs/heads/{branch}:refs/remotes/{remote_ref}"


def _remote_bundle(target: RemoteTarget, release_id: str) -> str:
    return f"{target.runtime_root}/{release_id}"


def _rsync_upload_argv(
    target: RemoteTarget,
    local_path: Path,
    remote_partial: str,
) -> list[str]:
    endpoint = f"{target.user}@{target.host}"
    return [
        "rsync",
        "--partial",
        "--inplace",
        "--chmod=Fu=rw,Fgo=",
        "-e",
        _ssh_transport(target),
        str(local_path),
        f"{endpoint}:{remote_partial}",
    ]


def _upload_plan(
    target: RemoteTarget,
    release_id: str,
    files: Sequence[Path],
) -> list[list[str]]:
    ssh = _ssh_base(target)
    bundle = _remote_bundle(target, release_id)
    commands = [
        ssh + ["install", "-d", "-m", "0700", "--", target.runtime_root],
        ssh + ["install", "-d", "-m", "0700", "--", bundle],
    ]
    for path in files:
        final = f"{bundle}/{path.name}"
        partial = f"{final}.part"
        commands.extend(
            [
                ssh + ["test", "-e", final],
                _rsync_upload_argv(target, path, partial),
                ssh + ["sha256sum", "--", partial],
                ssh + ["mv", "--", partial, final],
            ]
        )
    return commands


def build_release_plan(arguments: argparse.Namespace) -> list[list[str]]:
    """构建不使用 shell 插值的确定性命令计划。"""

    manifest, target, files, _ = _validated_arguments(arguments)
    ssh = _ssh_base(target)
    bundle = _remote_bundle(target, manifest.release_id)
    rollback_ref = f"release-rollback/{manifest.release_id}"
    rollback_full_ref = f"refs/heads/{rollback_ref}"
    plan: list[list[str]] = [
        ["git", "status", "--porcelain", "--untracked-files=normal"],
        ["git", "rev-parse", "HEAD"],
        ["git", "rev-parse", arguments.remote_ref],
        ["git", "merge-base", "--is-ancestor", manifest.commit, arguments.remote_ref],
    ]
    plan.extend(
        [
            "docker",
            "image",
            "inspect",
            "--format",
            "{{.Id}} {{.Os}}/{{.Architecture}}",
            manifest.images[name].ref,
        ]
        for name in _IMAGE_NAMES
    )
    plan.extend(_upload_plan(target, manifest.release_id, files))
    plan.extend(
        [
            ssh
            + [
                "git",
                "-C",
                target.platform_root,
                "status",
                "--porcelain",
                "--untracked-files=normal",
            ],
            ssh
            + [
                "git",
                "-C",
                target.platform_root,
                "fetch",
                "--prune",
                "origin",
                _remote_fetch_refspec(arguments.remote_ref),
            ],
            ssh + ["git", "-C", target.platform_root, "rev-parse", arguments.remote_ref],
            ssh
            + [
                "git",
                "-C",
                target.platform_root,
                "merge-base",
                "--is-ancestor",
                "HEAD",
                manifest.commit,
            ],
            ssh
            + [
                "git",
                "-C",
                target.platform_root,
                "show-ref",
                "--verify",
                "--quiet",
                rollback_full_ref,
            ],
            ssh
            + [
                "git",
                "-C",
                target.platform_root,
                "rev-parse",
                rollback_full_ref,
            ],
            ssh
            + [
                "git",
                "-C",
                target.platform_root,
                "branch",
                rollback_ref,
                "HEAD",
            ],
            *(
                [
                    ssh
                    + [
                        "git",
                        "-C",
                        target.platform_root,
                        "diff",
                        "--name-only",
                        rollback_full_ref,
                        manifest.commit,
                        "--",
                        *_VENDOR_AGENT_RUNTIME_PATHS,
                    ]
                ]
                if target.secrets_mode == "development"
                else []
            ),
            ssh + ["git", "-C", target.platform_root, "merge", "--ff-only", manifest.commit],
            ssh
            + [
                *_sms_compose_remote_argv(
                    target,
                    "prepare",
                    "--manifest",
                    f"{bundle}/manifest.json",
                ),
            ],
        ]
    )
    plan.extend(ssh + ["rm", "--", f"{bundle}/{path.name}"] for path in files)
    plan.extend(
        [
            ssh + ["rmdir", "--", bundle],
            ssh
            + [
                *_sms_compose_remote_argv(
                    target,
                    "activate",
                    "--release-id",
                    manifest.release_id,
                ),
            ],
            ssh
            + [
                *_sms_compose_remote_argv(
                    target,
                    "status",
                    "--release-id",
                    manifest.release_id,
                ),
            ],
            *(
                [
                    ssh
                    + [
                        "sudo",
                        "--",
                        "/usr/bin/systemctl",
                        "restart",
                        "vendor-control-agent.service",
                    ],
                    ssh
                    + [
                        "systemctl",
                        "is-active",
                        "--quiet",
                        "vendor-control-agent.service",
                    ],
                ]
                if target.secrets_mode == "development"
                else []
            ),
            ssh + ["systemctl", "is-active", "--quiet", "sms-platform.service"],
            ssh
            + [
                "git",
                "-C",
                target.platform_root,
                "status",
                "--porcelain",
                "--untracked-files=normal",
            ],
        ]
    )
    plan.extend(
        ["curl", "--fail", "--silent", "--show-error", "--max-time", "10", url]
        for url in arguments.public_urls
    )
    return plan


def _run(
    runner: CommandRunner, argv: Sequence[str], context: str
) -> subprocess.CompletedProcess[str]:
    result = runner.run(argv, capture_output=True)
    if result.returncode != 0:
        raise RemoteReleaseError(f"{context} failed")
    return result


def _validate_public_health_response(result: subprocess.CompletedProcess[str]) -> None:
    try:
        value = json.loads(result.stdout, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, RemoteReleaseError) as exc:
        raise RemoteReleaseError("public health response is not strict JSON") from exc
    if type(value) is not dict or value != {"status": "ok"}:
        raise RemoteReleaseError("public health response is not the API health contract")


def _single_line(result: subprocess.CompletedProcess[str], context: str) -> str:
    value = result.stdout
    if value.endswith("\n"):
        value = value[:-1]
    if not value or "\n" in value or "\r" in value:
        raise RemoteReleaseError(f"{context} returned invalid output")
    return value


def _commit_line(result: subprocess.CompletedProcess[str], context: str) -> str:
    value = _single_line(result, context)
    if _HEX_40_RE.fullmatch(value) is None:
        raise RemoteReleaseError(f"{context} returned an invalid commit")
    return value


def _remote_hash(
    runner: CommandRunner,
    ssh: Sequence[str],
    remote_path: str,
) -> str:
    result = _run(runner, [*ssh, "sha256sum", "--", remote_path], "remote file verification")
    line = _single_line(result, "remote file verification")
    match = re.fullmatch(r"([0-9a-f]{64}) [ *](.+)", line)
    if match is None or match.group(2) != remote_path:
        raise RemoteReleaseError("remote file verification returned invalid output")
    return match.group(1)


def _validate_local_state(
    runner: CommandRunner,
    manifest: ReleaseManifest,
    remote_ref: str,
) -> None:
    status_result = _run(
        runner,
        ["git", "status", "--porcelain", "--untracked-files=normal"],
        "local Git cleanliness check",
    )
    if status_result.stdout:
        raise RemoteReleaseError("local Git worktree is not clean")
    head = _single_line(
        _run(runner, ["git", "rev-parse", "HEAD"], "local commit check"), "local commit check"
    )
    remote = _single_line(
        _run(runner, ["git", "rev-parse", remote_ref], "local remote-ref check"),
        "local remote-ref check",
    )
    if not hmac.compare_digest(head, manifest.commit) or not hmac.compare_digest(
        remote, manifest.commit
    ):
        raise RemoteReleaseError("local commit is not the approved remote-ref commit")
    _run(
        runner,
        ["git", "merge-base", "--is-ancestor", manifest.commit, remote_ref],
        "local remote ancestry check",
    )
    for name in _IMAGE_NAMES:
        spec = manifest.images[name]
        result = _run(
            runner,
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{.Id}} {{.Os}}/{{.Architecture}}",
                spec.ref,
            ],
            "local image inspection",
        )
        fields = _single_line(result, "local image inspection").split()
        if (
            len(fields) != 2
            or fields[1] != "linux/amd64"
            or not hmac.compare_digest(fields[0], spec.image_id)
        ):
            raise RemoteReleaseError("local image inspection does not match the manifest")


def _upload_bundle(
    runner: CommandRunner,
    target: RemoteTarget,
    manifest: ReleaseManifest,
    files: Sequence[Path],
    local_hashes: Mapping[Path, str],
) -> None:
    ssh = _ssh_base(target)
    bundle = _remote_bundle(target, manifest.release_id)
    _run(
        runner,
        [*ssh, "install", "-d", "-m", "0700", "--", target.runtime_root],
        "remote staging root creation",
    )
    _run(
        runner,
        [*ssh, "install", "-d", "-m", "0700", "--", bundle],
        "remote staging directory creation",
    )
    for path in files:
        final = f"{bundle}/{path.name}"
        partial = f"{final}.part"
        exists = runner.run([*ssh, "test", "-e", final], capture_output=True)
        if exists.returncode == 0:
            if not hmac.compare_digest(_remote_hash(runner, ssh, final), local_hashes[path]):
                raise RemoteReleaseError("existing remote file differs from the release bundle")
            continue
        if exists.returncode != 1:
            raise RemoteReleaseError("remote final-file check failed")
        upload_argv = _rsync_upload_argv(target, path, partial)
        for attempt in range(_RSYNC_UPLOAD_ATTEMPTS):
            uploaded = runner.run(upload_argv, capture_output=True)
            if uploaded.returncode == 0:
                break
            if attempt == _RSYNC_UPLOAD_ATTEMPTS - 1:
                raise RemoteReleaseError("release bundle upload failed")
        if not hmac.compare_digest(_remote_hash(runner, ssh, partial), local_hashes[path]):
            raise RemoteReleaseError("uploaded remote file failed verification")
        _run(runner, [*ssh, "mv", "--", partial, final], "remote atomic rename")


def _prepare_remote_git(
    runner: CommandRunner,
    target: RemoteTarget,
    manifest: ReleaseManifest,
    remote_ref: str,
) -> bool:
    ssh = _ssh_base(target)
    status = _run(
        runner,
        [
            *ssh,
            "git",
            "-C",
            target.platform_root,
            "status",
            "--porcelain",
            "--untracked-files=normal",
        ],
        "remote Git cleanliness check",
    )
    if status.stdout:
        raise RemoteReleaseError("remote Git worktree is not clean")
    _run(
        runner,
        [
            *ssh,
            "git",
            "-C",
            target.platform_root,
            "fetch",
            "--prune",
            "origin",
            _remote_fetch_refspec(remote_ref),
        ],
        "remote Git fetch",
    )
    remote_head = _commit_line(
        _run(
            runner,
            [*ssh, "git", "-C", target.platform_root, "rev-parse", "HEAD"],
            "remote HEAD check",
        ),
        "remote HEAD check",
    )
    remote_commit = _commit_line(
        _run(
            runner,
            [*ssh, "git", "-C", target.platform_root, "rev-parse", remote_ref],
            "remote remote-ref check",
        ),
        "remote remote-ref check",
    )
    if not hmac.compare_digest(remote_commit, manifest.commit):
        raise RemoteReleaseError("remote remote-ref does not match the manifest commit")
    _run(
        runner,
        [
            *ssh,
            "git",
            "-C",
            target.platform_root,
            "merge-base",
            "--is-ancestor",
            "HEAD",
            manifest.commit,
        ],
        "remote fast-forward check",
    )
    rollback_ref = f"release-rollback/{manifest.release_id}"
    rollback_full_ref = f"refs/heads/{rollback_ref}"
    rollback_exists = runner.run(
        [
            *ssh,
            "git",
            "-C",
            target.platform_root,
            "show-ref",
            "--verify",
            "--quiet",
            rollback_full_ref,
        ],
        capture_output=True,
    )
    if rollback_exists.returncode == 0:
        rollback_commit = _commit_line(
            _run(
                runner,
                [*ssh, "git", "-C", target.platform_root, "rev-parse", rollback_full_ref],
                "remote rollback-ref check",
            ),
            "remote rollback-ref check",
        )
        _run(
            runner,
            [
                *ssh,
                "git",
                "-C",
                target.platform_root,
                "merge-base",
                "--is-ancestor",
                rollback_commit,
                manifest.commit,
            ],
            "remote rollback-ref ancestry check",
        )
        if remote_head != manifest.commit and not hmac.compare_digest(
            rollback_commit,
            remote_head,
        ):
            raise RemoteReleaseError("remote rollback-ref does not match the original HEAD")
    elif rollback_exists.returncode == 1:
        rollback_commit = remote_head
        _run(
            runner,
            [
                *ssh,
                "git",
                "-C",
                target.platform_root,
                "branch",
                rollback_ref,
                remote_head,
            ],
            "remote rollback-ref creation",
        )
    else:
        raise RemoteReleaseError("remote rollback-ref check failed")
    vendor_agent_changed = False
    if target.secrets_mode == "development":
        changed = runner.run(
            [
                *ssh,
                "git",
                "-C",
                target.platform_root,
                "diff",
                "--quiet",
                rollback_commit,
                manifest.commit,
                "--",
                *_VENDOR_AGENT_RUNTIME_PATHS,
            ],
            capture_output=True,
        )
        if changed.returncode not in {0, 1}:
            raise RemoteReleaseError("remote vendor control agent change check failed")
        vendor_agent_changed = changed.returncode == 1
    _run(
        runner,
        [*ssh, "git", "-C", target.platform_root, "merge", "--ff-only", manifest.commit],
        "remote Git fast-forward",
    )
    return vendor_agent_changed


def _sms_compose_remote_argv(target: RemoteTarget, *arguments: str) -> list[str]:
    return [
        "sudo",
        "--",
        "/usr/bin/env",
        f"SMS_SECRETS_MODE={target.secrets_mode}",
        "/usr/local/sbin/sms-compose",
        "release",
        *arguments,
    ]


def _sms_compose_command(target: RemoteTarget, *arguments: str) -> list[str]:
    return [*_ssh_base(target), *_sms_compose_remote_argv(target, *arguments)]


def _reload_vendor_control_agent(runner: CommandRunner, target: RemoteTarget) -> None:
    ssh = _ssh_base(target)
    _run(
        runner,
        [
            *ssh,
            "sudo",
            "--",
            "/usr/bin/systemctl",
            "restart",
            "vendor-control-agent.service",
        ],
        "remote vendor control agent restart",
    )
    _run(
        runner,
        [*ssh, "systemctl", "is-active", "--quiet", "vendor-control-agent.service"],
        "remote vendor control agent active check",
    )


def deploy_release(arguments: argparse.Namespace, runner: CommandRunner) -> None:
    """执行已验证发布包的上传、prepare、activate 与外部探针。"""

    manifest, target, files, local_hashes = _validated_arguments(arguments)
    _validate_local_state(runner, manifest, arguments.remote_ref)
    _upload_bundle(runner, target, manifest, files, local_hashes)
    vendor_agent_changed = _prepare_remote_git(runner, target, manifest, arguments.remote_ref)
    bundle = _remote_bundle(target, manifest.release_id)
    _run(
        runner,
        _sms_compose_command(
            target,
            "prepare",
            "--manifest",
            f"{bundle}/manifest.json",
        ),
        "remote release prepare",
    )
    ssh = _ssh_base(target)
    for path in files:
        _run(runner, [*ssh, "rm", "--", f"{bundle}/{path.name}"], "remote staging cleanup")
    _run(runner, [*ssh, "rmdir", "--", bundle], "remote staging cleanup")
    _run(
        runner,
        _sms_compose_command(target, "activate", "--release-id", manifest.release_id),
        "remote release activation",
    )
    status_result = _run(
        runner,
        _sms_compose_command(target, "status", "--release-id", manifest.release_id),
        "remote release status",
    )
    try:
        status_value = json.loads(status_result.stdout, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, RemoteReleaseError) as exc:
        raise RemoteReleaseError("remote release status is not strict JSON") from exc
    if type(status_value) is not dict or status_value.get("state") != "succeeded":
        raise RemoteReleaseError("remote release did not reach succeeded")
    if vendor_agent_changed:
        _reload_vendor_control_agent(runner, target)
    ssh = _ssh_base(target)
    _run(
        runner,
        [*ssh, "systemctl", "is-active", "--quiet", "sms-platform.service"],
        "remote systemd service check",
    )
    for url in arguments.public_urls:
        probe_result = _run(
            runner,
            ["curl", "--fail", "--silent", "--show-error", "--max-time", "10", url],
            "public release probe",
        )
        _validate_public_health_response(probe_result)
    final_status = _run(
        runner,
        [
            *ssh,
            "git",
            "-C",
            target.platform_root,
            "status",
            "--porcelain",
            "--untracked-files=normal",
        ],
        "final remote Git cleanliness check",
    )
    if final_status.stdout:
        raise RemoteReleaseError("final remote Git worktree is not clean")


def _redact_token(value: str) -> str:
    value = _SHA_ID_RE.sub("sha256:<redacted>", value)
    value = _HEX_64_RE.sub("<redacted>", value)
    return _HEX_40_RE.sub("<redacted>", value)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="安全交接四镜像远端发布")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--user", required=True)
    parser.add_argument("--platform-root", required=True)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--mode", choices=("development", "production"), required=True)
    parser.add_argument("--remote-ref", default="origin/main")
    parser.add_argument("--public-url", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    parsed = parser.parse_args(argv)
    parsed.target = RemoteTarget(
        host=parsed.host,
        port=parsed.port,
        user=parsed.user,
        platform_root=parsed.platform_root,
        secrets_mode=parsed.mode,
        runtime_root=parsed.runtime_root,
    )
    parsed.public_urls = tuple(parsed.public_url)
    try:
        if parsed.dry_run:
            for command in build_release_plan(parsed):
                print(shlex.join([_redact_token(token) for token in command]))
        else:
            deploy_release(parsed, SubprocessRunner())
    except RemoteReleaseError as exc:
        print(f"release deployment failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
