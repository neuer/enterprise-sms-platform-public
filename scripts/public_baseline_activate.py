#!/usr/bin/env python3
"""为一次性公开基线激活生成纯公开、可复验的本地交付材料。

本模块故意不实现 SSH、上传、远端 Git 操作或 secrets 读取。它只在公开工作区之外
验证一个隔离的 GitHub ``main`` checkout，生成完整 public bundle，再从 bundle
中的原始 blob 构建、复核并冻结 API/Web 镜像；最后生成严格绑定的激活请求。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn, Protocol

from release_metadata import (
    ReleaseMetadataError,
    schema_head,
    source_version,
)

PUBLIC_WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
CI_VERIFIER = PUBLIC_WORKSPACE_ROOT / "scripts" / "verify_ci_commit.py"
CANONICAL_REPOSITORY = "neuer/enterprise-sms-platform-public"
CANONICAL_ORIGIN_URL = f"https://github.com/{CANONICAL_REPOSITORY}.git"
MAIN_REF = "refs/heads/main"
REMOTE_MAIN_REF = "refs/remotes/origin/main"
BUNDLE_FILE = "public-baseline.bundle"
INVENTORY_FILE = "public-object-inventory.json"
BUILT_IMAGES_FILE = "built-images.json"
MANIFEST_FILE = "baseline-manifest.json"
STANDARD_REQUEST_FILE = "request.json"
API_ARCHIVE = "api.tar"
WEB_ARCHIVE = "web.tar"
OBJECT_FORMAT = "sha1"
SHA_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
MIGRATION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
UPDATE_ID_RE = re.compile(r"test-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}")
_SAFE_TREE_PATH_RE = re.compile(r"[ -~]+")
_MAX_METADATA_BYTES = 512 * 1024
_MAX_BUNDLE_BYTES = 1024 * 1024 * 1024
_MAX_IMAGE_ARCHIVE_BYTES = 8 * 1024 * 1024 * 1024
_MAX_CONTEXT_FILE_BYTES = 128 * 1024 * 1024
_MAX_CONTEXT_BYTES = 1024 * 1024 * 1024
_DANGEROUS_GIT_ENV = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_NAMESPACE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_REPLACE_REF_BASE",
        "GIT_WORK_TREE",
    }
)
_INVENTORY_FIELDS = frozenset(
    {
        "schema_version",
        "ref",
        "commit",
        "tree",
        "object_format",
        "object_count",
        "objects_sha256",
        "bundle_sha256",
        "app_version",
        "schema_revision",
        "update_id",
    }
)
_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "activation_id",
        "origin_url",
        "base",
        "target",
        "bundle",
        "images",
        "migration",
    }
)
_STANDARD_REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "update_id",
        "base_commit",
        "commit",
        "source_ref",
        "environment_mode",
        "components",
        "images",
        "migration",
    }
)
_BUILT_IMAGES_FIELDS = frozenset(
    {
        "schema_version",
        "update_id",
        "commit",
        "tree",
        "context",
        "images",
    }
)
_BUILT_CONTEXT_FIELDS = frozenset({"file_count", "bytes", "sha256"})
_BUILT_IMAGE_FIELDS = frozenset(
    {
        "file",
        "sha256",
        "ref",
        "id",
        "version",
        "revision",
        "schema_revision",
    }
)
_DOCKER_LABELS = {
    "version": "org.opencontainers.image.version",
    "revision": "org.opencontainers.image.revision",
    "schema_revision": "com.sms-platform.schema-revision",
}


class ActivationError(RuntimeError):
    """公开基线激活材料不满足失败关闭合同。"""


@dataclass(frozen=True, slots=True)
class TargetIdentity:
    """已由公开 GitHub main 与精确 CI 绑定的目标身份。"""

    repository: str
    commit: str
    tree: str
    app_version: str
    schema_revision: str


@dataclass(frozen=True, slots=True)
class ObjectInventory:
    """不含路径或内容的 Git 对象清单摘要。"""

    object_count: int
    objects_sha256: str


@dataclass(frozen=True, slots=True)
class BundleArtifact:
    """只含完整 ``refs/heads/main`` 的 public bundle。"""

    path: Path
    sha256: str
    ref: str


@dataclass(frozen=True, slots=True)
class ActivationPreparation:
    """受控镜像构建与请求终结所需的纯公开准备结果。"""

    workspace: Path
    artifact_dir: Path
    target: TargetIdentity
    inventory: ObjectInventory
    bundle: BundleArtifact
    update_id: str


@dataclass(frozen=True, slots=True)
class ImageArtifact:
    """一个已构建、不可变且本地摘要已复核的镜像归档。"""

    file: str
    sha256: str
    ref: str
    image_id: str
    version: str
    revision: str
    schema_revision: str


@dataclass(frozen=True, slots=True)
class ContextEntry:
    """从公开 Git blob 直接物化的一个规范化构建上下文文件。"""

    path: str
    mode: int
    object_id: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ContextInventory:
    """规范化构建上下文的完整路径、模式和内容绑定。"""

    entries: tuple[ContextEntry, ...]
    total_bytes: int
    sha256: str


class DockerRunner(Protocol):
    """只允许固定 argv 的本地 Docker 执行边界。"""

    def run(
        self,
        arguments: Sequence[str],
        *,
        operation: str,
        timeout: int,
    ) -> str: ...


class FixedDockerRunner:
    """不经 shell 执行固定 Docker argv，并隐藏底层输出中的环境细节。"""

    def run(
        self,
        arguments: Sequence[str],
        *,
        operation: str,
        timeout: int,
    ) -> str:
        if not arguments or arguments[0] != "docker":
            _fail("Docker command boundary is invalid")
        try:
            completed = subprocess.run(
                list(arguments),
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ActivationError(f"Docker {operation} failed") from error
        if completed.returncode != 0:
            _fail(f"Docker {operation} failed")
        return completed.stdout.strip()


PublicRepositoryVerifier = Callable[[str, str], None]
CiVerifier = Callable[[str, str], None]


def _fail(message: str) -> NoReturn:
    raise ActivationError(message)


def _safe_git_environment() -> dict[str, str]:
    for name in _DANGEROUS_GIT_ENV:
        if os.environ.get(name):
            _fail("Git object environment overrides are forbidden")
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }
    )
    return environment


def _run_git(
    repository: Path,
    arguments: Sequence[str],
    *,
    operation: str,
    binary: bool = False,
    allowed_returncodes: frozenset[int] = frozenset({0}),
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    command = [
        "git",
        "-c",
        "core.fsmonitor=false",
        "-c",
        f"core.hooksPath={os.devnull}",
        *arguments,
    ]
    completed = subprocess.run(
        command,
        cwd=repository,
        env=_safe_git_environment(),
        check=False,
        capture_output=True,
        text=not binary,
    )
    if completed.returncode not in allowed_returncodes:
        _fail(f"Git {operation} failed")
    return completed


def _git_text(repository: Path, *arguments: str, operation: str) -> str:
    completed = _run_git(repository, arguments, operation=operation)
    assert isinstance(completed.stdout, str)
    return completed.stdout.strip()


def _git_bytes(repository: Path, *arguments: str, operation: str) -> bytes:
    completed = _run_git(repository, arguments, operation=operation, binary=True)
    assert isinstance(completed.stdout, bytes)
    return completed.stdout


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _absolute_without_symlink_resolution(path: Path) -> Path:
    try:
        return Path(os.path.abspath(os.fspath(path)))
    except (OSError, TypeError, ValueError) as error:
        raise ActivationError("activation path is invalid") from error


def _resolve_real_directory(path: Path, *, label: str) -> Path:
    """拒绝目录本身的 symlink，并稳定解析其真实身份。"""

    absolute = _absolute_without_symlink_resolution(path)
    try:
        before = absolute.lstat()
        if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode):
            _fail(f"{label} must be a real directory")
        resolved = absolute.resolve(strict=True)
        after = resolved.lstat()
    except ActivationError:
        raise
    except OSError as error:
        raise ActivationError(f"{label} is unavailable") from error
    if (
        not stat.S_ISDIR(after.st_mode)
        or stat.S_ISLNK(after.st_mode)
        or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
    ):
        _fail(f"{label} identity is unstable")
    return resolved


def _resolve_isolated_workspace(workspace: Path) -> Path:
    resolved = _resolve_real_directory(
        workspace,
        label="activation workspace",
    )
    public_root = PUBLIC_WORKSPACE_ROOT.resolve(strict=True)
    if _is_within(resolved, public_root) or _is_within(public_root, resolved):
        _fail("activation workspace must be outside the public workspace")
    return resolved


def _resolve_new_artifact_dir(workspace: Path, artifact_dir: Path) -> Path:
    absolute = _absolute_without_symlink_resolution(artifact_dir)
    try:
        absolute.lstat()
    except FileNotFoundError:
        pass
    except OSError as error:
        raise ActivationError("artifact directory identity is unavailable") from error
    else:
        _fail("artifact directory must not already exist")
    parent = _resolve_real_directory(
        absolute.parent,
        label="artifact parent",
    )
    resolved = parent / absolute.name
    public_root = PUBLIC_WORKSPACE_ROOT.resolve(strict=True)
    if (
        _is_within(resolved, workspace)
        or _is_within(workspace, resolved)
        or _is_within(resolved, public_root)
        or _is_within(public_root, resolved)
    ):
        _fail("artifacts must be isolated from both Git workspaces")
    return resolved


def _resolve_existing_artifact_dir(workspace: Path, artifact_dir: Path) -> Path:
    resolved = _resolve_real_directory(
        artifact_dir,
        label="artifact directory",
    )
    public_root = PUBLIC_WORKSPACE_ROOT.resolve(strict=True)
    if (
        _is_within(resolved, workspace)
        or _is_within(workspace, resolved)
        or _is_within(resolved, public_root)
        or _is_within(public_root, resolved)
    ):
        _fail("artifact directory is not isolated")
    try:
        metadata = resolved.lstat()
    except OSError as error:
        raise ActivationError("artifact directory is unavailable") from error
    if (
        metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_nlink < 2
    ):
        _fail("artifact directory ownership or mode is invalid")
    return resolved


def _canonical_repository(value: str) -> str:
    if REPOSITORY_RE.fullmatch(value) is None:
        _fail("GitHub repository identity is invalid")
    return value.lower()


def _repository_from_origin(value: str) -> str:
    prefix = "https://github.com/"
    if not value.startswith(prefix):
        _fail("origin must be an anonymous HTTPS GitHub URL")
    path = value.removeprefix(prefix)
    if any(marker in value for marker in ("@", "?", "#")):
        _fail("origin must not contain credentials or URL metadata")
    if path.endswith(".git"):
        path = path[:-4]
    if "/" not in path or path.startswith("/") or path.endswith("/"):
        _fail("origin repository path is invalid")
    return _canonical_repository(path)


def _parse_refs(raw: str, *, commit: str) -> None:
    refs: dict[str, tuple[str, str]] = {}
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) != 4 or parts[3] != "END":
            _fail("Git ref inventory is malformed")
        ref, object_id, symref, _marker = parts
        if ref in refs or SHA_RE.fullmatch(object_id) is None:
            _fail("Git ref inventory is malformed")
        refs[ref] = (object_id, symref)
    required = {
        MAIN_REF: (commit, ""),
        REMOTE_MAIN_REF: (commit, ""),
    }
    for ref, expected in required.items():
        if refs.get(ref) != expected:
            _fail("local and origin main refs must resolve to the exact target")
    allowed = set(required)
    remote_head = "refs/remotes/origin/HEAD"
    if remote_head in refs:
        if refs[remote_head] != (commit, REMOTE_MAIN_REF):
            _fail("origin HEAD must be a symbolic alias of origin/main")
        allowed.add(remote_head)
    if set(refs) != allowed:
        _fail("the isolated repository may contain only main refs")


def _parse_object_ids(raw: str, *, source: str) -> frozenset[str]:
    values: set[str] = set()
    for line in raw.splitlines():
        value = line.strip()
        if SHA_RE.fullmatch(value) is None:
            _fail(f"{source} object inventory is malformed")
        values.add(value)
    if not values:
        _fail(f"{source} object inventory is empty")
    return frozenset(values)


def _objects_digest(objects: frozenset[str]) -> str:
    payload = "".join(f"{value}\n" for value in sorted(objects)).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _stat_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _open_private_regular(
    path: Path,
    *,
    expected_mode: int,
    minimum_size: int = 1,
    maximum_size: int,
    label: str,
) -> tuple[int, os.stat_result]:
    descriptor = -1
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != expected_mode
            or not minimum_size <= before.st_size <= maximum_size
        ):
            _fail(f"{label} identity is unsafe")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if _stat_identity(opened) != _stat_identity(before):
            _fail(f"{label} identity changed while opening")
        return descriptor, before
    except ActivationError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        raise ActivationError(f"{label} is unavailable") from error


def _require_open_file_unchanged(
    path: Path,
    descriptor: int,
    before: os.stat_result,
    *,
    label: str,
) -> None:
    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
    except OSError as error:
        raise ActivationError(f"{label} identity cannot be rechecked") from error
    if (
        _stat_identity(opened) != _stat_identity(before)
        or _stat_identity(current) != _stat_identity(before)
    ):
        _fail(f"{label} changed while reading")


def _sha256_private_file(
    path: Path,
    *,
    expected_mode: int = 0o600,
    minimum_size: int = 1,
    maximum_size: int,
    label: str,
) -> str:
    descriptor, before = _open_private_regular(
        path,
        expected_mode=expected_mode,
        minimum_size=minimum_size,
        maximum_size=maximum_size,
        label=label,
    )
    digest = hashlib.sha256()
    size = 0
    try:
        while block := os.read(descriptor, 1024 * 1024):
            size += len(block)
            if size > maximum_size:
                _fail(f"{label} is too large")
            digest.update(block)
        if size != before.st_size:
            _fail(f"{label} changed while reading")
        _require_open_file_unchanged(
            path,
            descriptor,
            before,
            label=label,
        )
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _read_private_file(
    path: Path,
    *,
    expected_mode: int = 0o600,
    minimum_size: int = 1,
    maximum_size: int,
    label: str,
) -> bytes:
    descriptor, before = _open_private_regular(
        path,
        expected_mode=expected_mode,
        minimum_size=minimum_size,
        maximum_size=maximum_size,
        label=label,
    )
    payload = bytearray()
    try:
        while block := os.read(descriptor, 1024 * 1024):
            payload.extend(block)
            if len(payload) > maximum_size:
                _fail(f"{label} is too large")
        if len(payload) != before.st_size:
            _fail(f"{label} changed while reading")
        _require_open_file_unchanged(
            path,
            descriptor,
            before,
            label=label,
        )
    finally:
        os.close(descriptor)
    return bytes(payload)


def _copy_private_file(
    source: Path,
    destination: Path,
    *,
    expected_mode: int,
    minimum_size: int = 1,
    maximum_size: int,
    label: str,
) -> tuple[str, int]:
    source_descriptor, before = _open_private_regular(
        source,
        expected_mode=expected_mode,
        minimum_size=minimum_size,
        maximum_size=maximum_size,
        label=label,
    )
    destination_descriptor = -1
    digest = hashlib.sha256()
    size = 0
    try:
        destination_descriptor = os.open(
            destination,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            expected_mode,
        )
        os.fchmod(destination_descriptor, expected_mode)
        while block := os.read(source_descriptor, 1024 * 1024):
            size += len(block)
            if size > maximum_size:
                _fail(f"{label} is too large")
            digest.update(block)
            view = memoryview(block)
            while view:
                written = os.write(destination_descriptor, view)
                if written <= 0:
                    _fail(f"{label} copy failed")
                view = view[written:]
        if size != before.st_size:
            _fail(f"{label} changed while copying")
        os.fsync(destination_descriptor)
        _require_open_file_unchanged(
            source,
            source_descriptor,
            before,
            label=label,
        )
    except ActivationError:
        destination.unlink(missing_ok=True)
        raise
    except OSError as error:
        destination.unlink(missing_ok=True)
        raise ActivationError(f"{label} copy failed") from error
    finally:
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
        os.close(source_descriptor)
    return digest.hexdigest(), size


def _resolve_update_id(
    commit: str,
    *,
    update_id: str | None,
    now: datetime | None,
) -> str:
    if update_id is None:
        moment = now or datetime.now(UTC)
        if moment.tzinfo is None or moment.utcoffset() is None:
            _fail("activation clock must be timezone-aware")
        update_id = (
            f"test-{moment.astimezone(UTC):%Y%m%dT%H%M%SZ}-{commit[:12]}"
        )
    if (
        UPDATE_ID_RE.fullmatch(update_id) is None
        or not update_id.endswith(f"-{commit[:12]}")
    ):
        _fail("activation update ID is invalid")
    return update_id


def _validate_git_storage(workspace: Path, *, commit: str) -> None:
    """要求独立 clone 的全部 Git 元数据真实位于 ``workspace/.git``。"""

    expected = workspace / ".git"
    try:
        root_metadata = workspace.lstat()
        git_metadata = expected.lstat()
    except OSError as error:
        raise ActivationError("isolated Git metadata is unavailable") from error
    if (
        root_metadata.st_uid != os.getuid()
        or root_metadata.st_mode & 0o022
        or not stat.S_ISDIR(git_metadata.st_mode)
        or stat.S_ISLNK(git_metadata.st_mode)
        or git_metadata.st_uid != os.getuid()
        or git_metadata.st_mode & 0o022
    ):
        _fail("activation source must use a private in-workspace .git directory")
    expected_resolved = expected.resolve(strict=True)
    absolute_git_dir = Path(
        _git_text(
            workspace,
            "rev-parse",
            "--absolute-git-dir",
            operation="absolute Git directory check",
        )
    ).resolve(strict=True)
    common_raw = Path(
        _git_text(
            workspace,
            "rev-parse",
            "--git-common-dir",
            operation="Git common directory check",
        )
    )
    if not common_raw.is_absolute():
        common_raw = workspace / common_raw
    common_dir = common_raw.resolve(strict=True)
    objects_raw = Path(
        _git_text(
            workspace,
            "rev-parse",
            "--git-path",
            "objects",
            operation="Git object directory check",
        )
    )
    if not objects_raw.is_absolute():
        objects_raw = workspace / objects_raw
    objects_dir = objects_raw.resolve(strict=True)
    if (
        absolute_git_dir != expected_resolved
        or common_dir != expected_resolved
        or objects_dir != (expected_resolved / "objects").resolve(strict=True)
    ):
        _fail("external Git directories and linked worktrees are forbidden")

    worktrees = _git_text(
        workspace,
        "worktree",
        "list",
        "--porcelain",
        operation="Git worktree inventory",
    ).split("\n\n")
    if len(worktrees) != 1:
        _fail("activation repository must contain exactly one worktree")
    worktree_lines = worktrees[0].splitlines()
    if worktree_lines != [
        f"worktree {workspace}",
        f"HEAD {commit}",
        f"branch {MAIN_REF}",
    ]:
        _fail("activation repository worktree identity is invalid")

    for current, directories, files in os.walk(
        expected_resolved,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current)
        try:
            current_metadata = current_path.lstat()
        except OSError as error:
            raise ActivationError("Git metadata tree is unavailable") from error
        if (
            not stat.S_ISDIR(current_metadata.st_mode)
            or stat.S_ISLNK(current_metadata.st_mode)
            or current_metadata.st_uid != os.getuid()
            or current_metadata.st_mode & 0o022
        ):
            _fail("Git metadata directories must be private and real")
        for name in directories:
            candidate = current_path / name
            try:
                metadata = candidate.lstat()
            except OSError as error:
                raise ActivationError("Git metadata tree is unavailable") from error
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o022
            ):
                _fail("Git metadata directories must be private and real")
        for name in files:
            candidate = current_path / name
            try:
                metadata = candidate.lstat()
            except OSError as error:
                raise ActivationError("Git metadata tree is unavailable") from error
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1
                or metadata.st_mode & 0o022
            ):
                _fail("Git metadata files must not use symlinks or hardlinks")

    filter_config = _run_git(
        workspace,
        ("config", "--local", "--get-regexp", r"^filter\."),
        operation="Git filter configuration check",
        allowed_returncodes=frozenset({0, 1}),
    )
    assert isinstance(filter_config.stdout, str)
    if filter_config.stdout.strip():
        _fail("Git clean and smudge filters are forbidden")

    def local_config(name: str) -> str | None:
        result = _run_git(
            workspace,
            ("config", "--local", "--get", name),
            operation=f"Git {name} configuration check",
            allowed_returncodes=frozenset({0, 1}),
        )
        assert isinstance(result.stdout, str)
        return result.stdout.strip().lower() or None

    if local_config("core.autocrlf") not in (None, "false"):
        _fail("Git automatic line-ending conversion is forbidden")
    if local_config("core.filemode") != "true":
        _fail("Git file mode tracking must be enabled")
    if local_config("core.symlinks") not in (None, "true"):
        _fail("Git symlink emulation is forbidden")


def _safe_tree_path(raw: bytes) -> str:
    try:
        value = raw.decode("ascii")
    except UnicodeDecodeError as error:
        raise ActivationError("non-ASCII Git paths are forbidden") from error
    if (
        not value
        or _SAFE_TREE_PATH_RE.fullmatch(value) is None
        or value.startswith("/")
        or value.endswith("/")
        or "\\" in value
    ):
        _fail("Git tree path is unsafe")
    parts = value.split("/")
    if any(
        not part
        or part in (".", "..")
        or part.casefold() == ".git"
        for part in parts
    ):
        _fail("Git tree path is unsafe")
    return value


def _git_blob(repository: Path, object_id: str) -> bytes:
    payload = _git_bytes(
        repository,
        "cat-file",
        "blob",
        object_id,
        operation="Git blob read",
    )
    if len(payload) > _MAX_CONTEXT_FILE_BYTES:
        _fail("Git blob exceeds the activation context limit")
    actual = hashlib.sha1(
        f"blob {len(payload)}\0".encode("ascii") + payload
    ).hexdigest()
    if actual != object_id:
        _fail("Git blob identity is invalid")
    return payload


def _reject_filtered_blob(path: str, payload: bytes) -> None:
    if path.casefold().endswith("/.lfsconfig") or path.casefold() == ".lfsconfig":
        _fail("Git LFS configuration is forbidden")
    lfs_pointer_header = (
        b"version " + b"https://" + b"git-lfs.github.com/spec/v1\n"
    )
    if payload.startswith(lfs_pointer_header):
        _fail("Git LFS pointer content is forbidden")
    if Path(path).name != ".gitattributes":
        return
    try:
        rendered = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ActivationError("Git attributes must be UTF-8") from error
    forbidden = {
        "filter",
        "working-tree-encoding",
        "ident",
        "export-ignore",
        "export-subst",
    }
    for raw_line in rendered.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        for token in line.split()[1:]:
            attribute = token.lstrip("-!").split("=", maxsplit=1)[0].casefold()
            if attribute in forbidden:
                _fail("Git attributes that transform or omit content are forbidden")


def _context_inventory(repository: Path, ref: str) -> ContextInventory:
    raw = _git_bytes(
        repository,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        ref,
        operation="Git tree inventory",
    )
    entries: list[ContextEntry] = []
    seen_paths: set[str] = set()
    seen_casefolded: set[str] = set()
    total_bytes = 0
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            identity, raw_path = record.split(b"\t", maxsplit=1)
            raw_mode, raw_type, raw_object = identity.split(b" ", maxsplit=2)
            mode_text = raw_mode.decode("ascii")
            object_type = raw_type.decode("ascii")
            object_id = raw_object.decode("ascii")
        except (ValueError, UnicodeDecodeError) as error:
            raise ActivationError("Git tree inventory is malformed") from error
        if (
            mode_text not in ("100644", "100755")
            or object_type != "blob"
            or SHA_RE.fullmatch(object_id) is None
        ):
            _fail("Git symlinks, submodules and special modes are forbidden")
        path = _safe_tree_path(raw_path)
        if path in seen_paths or path.casefold() in seen_casefolded:
            _fail("Git tree paths collide on the activation filesystem")
        seen_paths.add(path)
        seen_casefolded.add(path.casefold())
        payload = _git_blob(repository, object_id)
        _reject_filtered_blob(path, payload)
        total_bytes += len(payload)
        if total_bytes > _MAX_CONTEXT_BYTES:
            _fail("activation build context exceeds its size limit")
        entries.append(
            ContextEntry(
                path=path,
                mode=0o755 if mode_text == "100755" else 0o644,
                object_id=object_id,
                size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            )
        )
    if not entries:
        _fail("Git tree inventory is empty")
    ordered = tuple(sorted(entries, key=lambda item: item.path))
    digest_payload = json.dumps(
        [
            [
                item.path,
                item.mode,
                item.object_id,
                item.size,
                item.sha256,
            ]
            for item in ordered
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return ContextInventory(
        entries=ordered,
        total_bytes=total_bytes,
        sha256=hashlib.sha256(digest_payload).hexdigest(),
    )


def _expected_context_directories(
    inventory: ContextInventory,
) -> frozenset[str]:
    directories: set[str] = set()
    for entry in inventory.entries:
        parent = Path(entry.path).parent
        while parent != Path("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    return frozenset(directories)


def _verify_context(
    root: Path,
    inventory: ContextInventory,
    *,
    git_workspace: bool = False,
) -> None:
    """逐路径复核规范上下文，不信任 Git status 或 stat cache。"""

    try:
        root_metadata = root.lstat()
    except OSError as error:
        raise ActivationError("activation context is unavailable") from error
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or stat.S_ISLNK(root_metadata.st_mode)
        or root_metadata.st_uid != os.getuid()
        or (
            stat.S_IMODE(root_metadata.st_mode) != 0o700
            if not git_workspace
            else bool(root_metadata.st_mode & 0o022)
        )
    ):
        _fail("activation context root is unsafe")
    expected_files = {entry.path: entry for entry in inventory.entries}
    expected_directories = _expected_context_directories(inventory)
    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    for current, directories, files in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current)
        if git_workspace and current_path == root and ".git" in directories:
            directories.remove(".git")
        for name in directories:
            candidate = current_path / name
            relative = candidate.relative_to(root).as_posix()
            try:
                metadata = candidate.lstat()
            except OSError as error:
                raise ActivationError("activation context is unavailable") from error
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or (
                    stat.S_IMODE(metadata.st_mode) != 0o700
                    if not git_workspace
                    else bool(metadata.st_mode & 0o022)
                )
            ):
                _fail("activation context directory identity is unsafe")
            actual_directories.add(relative)
        for name in files:
            candidate = current_path / name
            relative = candidate.relative_to(root).as_posix()
            entry = expected_files.get(relative)
            if entry is None:
                _fail("activation context contains an unexpected file")
            digest = _sha256_private_file(
                candidate,
                expected_mode=entry.mode,
                minimum_size=0,
                maximum_size=_MAX_CONTEXT_FILE_BYTES,
                label="activation context file",
            )
            try:
                size = candidate.lstat().st_size
            except OSError as error:
                raise ActivationError("activation context is unavailable") from error
            if size != entry.size or digest != entry.sha256:
                _fail("activation context content differs from its Git blob")
            actual_files.add(relative)
    if (
        actual_files != set(expected_files)
        or actual_directories != set(expected_directories)
    ):
        _fail("activation context path inventory is incomplete")


def _validate_worktree_snapshot(
    workspace: Path,
    inventory: ContextInventory,
) -> None:
    """验证 checkout 字节和模式与 Git blob 完全一致。"""

    _verify_context(workspace, inventory, git_workspace=True)


def _reject_repository_indirection(workspace: Path) -> None:
    for relative in ("objects/info/alternates", "info/grafts", "shallow"):
        git_path = _git_text(
            workspace,
            "rev-parse",
            "--git-path",
            relative,
            operation="Git indirection path check",
        )
        candidate = Path(git_path)
        if not candidate.is_absolute():
            candidate = workspace / candidate
        if candidate.exists() or candidate.is_symlink():
            _fail("Git alternates, grafts and shallow metadata are forbidden")
    if _git_text(
        workspace,
        "rev-parse",
        "--is-shallow-repository",
        operation="shallow check",
    ) != "false":
        _fail("shallow repositories are forbidden")
    partial = _run_git(
        workspace,
        (
            "config",
            "--local",
            "--get-regexp",
            r"^(extensions\.partialclone|remote\..*\.promisor|remote\..*\.partialclonefilter)$",
        ),
        operation="partial clone check",
        allowed_returncodes=frozenset({0, 1}),
    )
    assert isinstance(partial.stdout, str)
    if partial.stdout.strip():
        _fail("partial or promisor repositories are forbidden")

def _validate_clean_checkout(workspace: Path) -> None:
    if _git_bytes(
        workspace,
        "status",
        "--porcelain=v2",
        "-z",
        "--untracked-files=all",
        operation="clean status check",
    ):
        _fail("activation workspace must be clean")
    if _git_bytes(
        workspace,
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "-z",
        operation="ignored file check",
    ):
        _fail("ignored files are forbidden in the isolated workspace")
    tracked = _git_bytes(
        workspace,
        "ls-files",
        "-v",
        "-z",
        operation="tracked file flag check",
    )
    for item in tracked.split(b"\0"):
        if item and not item.startswith(b"H "):
            _fail("assume-unchanged and sparse tracked files are forbidden")


def _validate_object_store(workspace: Path) -> ObjectInventory:
    reachable = _parse_object_ids(
        _git_text(
            workspace,
            "rev-list",
            "--objects",
            "--no-object-names",
            MAIN_REF,
            operation="reachable object inventory",
        ),
        source="reachable",
    )
    all_objects = _parse_object_ids(
        _git_text(
            workspace,
            "cat-file",
            "--batch-all-objects",
            "--batch-check=%(objectname)",
            operation="complete object inventory",
        ),
        source="complete",
    )
    if all_objects != reachable:
        _fail("every local Git object must be reachable from main")
    fsck = _run_git(
        workspace,
        (
            "fsck",
            "--full",
            "--strict",
            "--unreachable",
            "--no-reflogs",
            "--no-progress",
        ),
        operation="object integrity check",
    )
    assert isinstance(fsck.stdout, str)
    assert isinstance(fsck.stderr, str)
    if fsck.stdout.strip() or fsck.stderr.strip():
        _fail("Git object integrity output must be empty")
    return ObjectInventory(
        object_count=len(reachable),
        objects_sha256=_objects_digest(reachable),
    )


def validate_public_workspace(
    workspace: Path,
    *,
    repository: str,
    expected_commit: str,
) -> tuple[TargetIdentity, ObjectInventory]:
    """验证隔离 checkout 只含干净、完整且公开的精确 main 对象。"""

    resolved = _resolve_isolated_workspace(workspace)
    repository = _canonical_repository(repository)
    if repository != CANONICAL_REPOSITORY:
        _fail("activation repository is not the canonical public repository")
    if SHA_RE.fullmatch(expected_commit) is None:
        _fail("expected target commit is invalid")
    if _git_text(
        resolved,
        "rev-parse",
        "--is-inside-work-tree",
        operation="worktree identity",
    ) != "true":
        _fail("activation source must be a Git worktree")
    if _git_text(
        resolved,
        "symbolic-ref",
        "-q",
        "HEAD",
        operation="HEAD branch check",
    ) != MAIN_REF:
        _fail("activation source must be checked out on main")
    remotes = _git_text(resolved, "remote", operation="remote inventory").splitlines()
    if remotes != ["origin"]:
        _fail("activation source must contain only origin")
    fetch_urls = _git_text(
        resolved,
        "remote",
        "get-url",
        "--all",
        "origin",
        operation="origin URL check",
    ).splitlines()
    push_urls = _git_text(
        resolved,
        "remote",
        "get-url",
        "--push",
        "--all",
        "origin",
        operation="origin push URL check",
    ).splitlines()
    if len(fetch_urls) != 1 or len(push_urls) != 1:
        _fail("origin must have exactly one fetch and push URL")
    if (
        _repository_from_origin(fetch_urls[0]) != repository
        or _repository_from_origin(push_urls[0]) != repository
    ):
        _fail("origin does not match the expected public repository")

    commit = _git_text(
        resolved,
        "rev-parse",
        "--verify",
        f"{MAIN_REF}^{{commit}}",
        operation="main commit resolution",
    )
    if commit != expected_commit:
        _fail("main does not match the expected target commit")
    remote_commit = _git_text(
        resolved,
        "rev-parse",
        "--verify",
        f"{REMOTE_MAIN_REF}^{{commit}}",
        operation="origin main commit resolution",
    )
    if remote_commit != commit:
        _fail("main and origin/main must be identical")
    _validate_git_storage(resolved, commit=commit)
    tree = _git_text(
        resolved,
        "rev-parse",
        f"{commit}^{{tree}}",
        operation="target tree resolution",
    )
    if SHA_RE.fullmatch(tree) is None:
        _fail("target tree is invalid")
    refs = _git_text(
        resolved,
        "for-each-ref",
        "--format=%(refname)%09%(objectname)%09%(symref)%09END",
        operation="ref inventory",
    )
    _parse_refs(refs, commit=commit)
    _reject_repository_indirection(resolved)
    _validate_clean_checkout(resolved)
    worktree_inventory = _context_inventory(resolved, MAIN_REF)
    _validate_worktree_snapshot(resolved, worktree_inventory)
    inventory = _validate_object_store(resolved)
    try:
        app_version = source_version(resolved)
        schema_revision = schema_head(resolved)
    except ReleaseMetadataError as error:
        raise ActivationError("public version or migration identity is invalid") from error
    return (
        TargetIdentity(
            repository=repository,
            commit=commit,
            tree=tree,
            app_version=app_version,
            schema_revision=schema_revision,
        ),
        inventory,
    )


def verify_public_github_repository(repository: str, expected_commit: str) -> None:
    """通过不带认证的 GitHub API 证明仓库公开且默认 main 精确匹配。"""

    repository = _canonical_repository(repository)
    if repository != CANONICAL_REPOSITORY:
        _fail("activation repository is not the canonical public repository")
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "enterprise-sms-public-baseline-activation",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    def request_json(url: str) -> object:
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                if response.status != 200:
                    _fail("anonymous GitHub repository verification failed")
                return json.load(response)
        except (
            OSError,
            TimeoutError,
            urllib.error.URLError,
            UnicodeError,
            json.JSONDecodeError,
        ) as error:
            raise ActivationError(
                "anonymous GitHub repository verification failed"
            ) from error

    metadata = request_json(f"https://api.github.com/repos/{repository}")
    if (
        type(metadata) is not dict
        or str(metadata.get("full_name", "")).lower() != repository
        or metadata.get("private") is not False
        or metadata.get("default_branch") != "main"
    ):
        _fail("GitHub repository is not the expected public main repository")
    ref = request_json(
        f"https://api.github.com/repos/{repository}/git/ref/heads/main"
    )
    if type(ref) is not dict or type(ref.get("object")) is not dict:
        _fail("GitHub main ref response is invalid")
    if ref["object"].get("sha") != expected_commit:
        _fail("GitHub main does not match the exact target commit")


def verify_exact_ci(repository: str, commit: str) -> None:
    """调用仓库唯一 CI verifier，要求精确 commit 的 GitHub Actions 成功。"""

    completed = subprocess.run(
        [
            sys.executable,
            str(CI_VERIFIER),
            "--repository",
            repository,
            "--commit",
            commit,
        ],
        cwd=PUBLIC_WORKSPACE_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0 or completed.stdout.strip() != "ci_gate=success":
        _fail("exact GitHub Actions ci-gate is not successful")


def _bundle_header(path: Path) -> tuple[str, tuple[tuple[str, str], ...]]:
    try:
        with path.open("rb") as handle:
            first = handle.readline(4097)
            if len(first) > 4096 or first not in (
                b"# v2 git bundle\n",
                b"# v3 git bundle\n",
            ):
                _fail("bundle header is invalid")
            prerequisites: list[str] = []
            heads: list[tuple[str, str]] = []
            for _ in range(256):
                line = handle.readline(4097)
                if len(line) > 4096:
                    _fail("bundle header is invalid")
                if line == b"\n":
                    break
                if not line:
                    _fail("bundle header is incomplete")
                if line.startswith(b"@"):
                    continue
                try:
                    text = line.decode("ascii").rstrip("\n")
                except UnicodeDecodeError as error:
                    raise ActivationError("bundle header is invalid") from error
                if text.startswith("-"):
                    prerequisites.append(text)
                    continue
                parts = text.split(" ", maxsplit=1)
                if (
                    len(parts) != 2
                    or SHA_RE.fullmatch(parts[0]) is None
                    or not parts[1]
                ):
                    _fail("bundle header is invalid")
                heads.append((parts[0], parts[1]))
            else:
                _fail("bundle header is too large")
    except OSError as error:
        raise ActivationError("bundle header cannot be read") from error
    if prerequisites:
        _fail("incremental bundles with prerequisites are forbidden")
    return first.decode("ascii").strip(), tuple(heads)


def _verify_bundle_clone(
    bundle: Path,
    *,
    artifact_parent: Path,
    target: TargetIdentity,
    inventory: ObjectInventory,
) -> None:
    with tempfile.TemporaryDirectory(
        prefix=".public-bundle-verify-",
        dir=artifact_parent,
    ) as temporary:
        bare = Path(temporary) / "repository.git"
        _run_git(
            artifact_parent,
            ("init", "--bare", str(bare)),
            operation="bundle verification repository initialization",
        )
        _run_git(
            bare,
            (
                "fetch",
                "--no-tags",
                str(bundle),
                f"{MAIN_REF}:{MAIN_REF}",
            ),
            operation="bundle verification fetch",
        )
        _run_git(
            bare,
            ("symbolic-ref", "HEAD", MAIN_REF),
            operation="bundle verification HEAD binding",
        )
        refs = _git_text(
            bare,
            "for-each-ref",
            "--format=%(refname)%09%(objectname)",
            operation="bundle verification ref inventory",
        )
        if refs != f"{MAIN_REF}\t{target.commit}":
            _fail("verified bundle must contain exactly main")
        reachable = _parse_object_ids(
            _git_text(
                bare,
                "rev-list",
                "--objects",
                "--no-object-names",
                MAIN_REF,
                operation="bundle reachable inventory",
            ),
            source="bundle reachable",
        )
        all_objects = _parse_object_ids(
            _git_text(
                bare,
                "cat-file",
                "--batch-all-objects",
                "--batch-check=%(objectname)",
                operation="bundle complete inventory",
            ),
            source="bundle complete",
        )
        if (
            reachable != all_objects
            or len(reachable) != inventory.object_count
            or _objects_digest(reachable) != inventory.objects_sha256
        ):
            _fail("bundle object inventory does not match public main")
        fsck = _run_git(
            bare,
            (
                "fsck",
                "--full",
                "--strict",
                "--unreachable",
                "--no-reflogs",
                "--no-progress",
            ),
            operation="bundle object integrity check",
        )
        assert isinstance(fsck.stdout, str)
        assert isinstance(fsck.stderr, str)
        if fsck.stdout.strip() or fsck.stderr.strip():
            _fail("bundle contains unreachable or invalid objects")


def _create_and_verify_bundle(
    workspace: Path,
    output: Path,
    *,
    target: TargetIdentity,
    inventory: ObjectInventory,
) -> BundleArtifact:
    _run_git(
        workspace,
        ("bundle", "create", str(output), MAIN_REF),
        operation="public bundle creation",
    )
    try:
        output.chmod(0o600)
    except OSError as error:
        raise ActivationError("bundle permissions cannot be secured") from error
    _run_git(
        workspace,
        ("bundle", "verify", str(output)),
        operation="public bundle verification",
    )
    _version, heads = _bundle_header(output)
    if heads != ((target.commit, MAIN_REF),):
        _fail("bundle must contain exactly refs/heads/main")
    listed = _git_text(
        workspace,
        "bundle",
        "list-heads",
        str(output),
        operation="bundle head listing",
    )
    if listed != f"{target.commit} {MAIN_REF}":
        _fail("bundle list-heads must contain only exact main")
    _verify_bundle_clone(
        output,
        artifact_parent=output.parent,
        target=target,
        inventory=inventory,
    )
    digest = _sha256_private_file(
        output,
        maximum_size=_MAX_BUNDLE_BYTES,
        label="public bundle",
    )
    return BundleArtifact(path=output, sha256=digest, ref=MAIN_REF)


def _write_json_new(path: Path, payload: Mapping[str, object]) -> None:
    rendered = (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            os.fchmod(handle.fileno(), 0o600)
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        raise ActivationError("activation artifact cannot be written safely") from error


def _fsync_directory(path: Path) -> None:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        os.fsync(descriptor)
    except OSError as error:
        raise ActivationError("activation artifact directory sync failed") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _inventory_document(
    target: TargetIdentity,
    inventory: ObjectInventory,
    bundle: BundleArtifact,
    update_id: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "ref": MAIN_REF,
        "commit": target.commit,
        "tree": target.tree,
        "object_format": OBJECT_FORMAT,
        "object_count": inventory.object_count,
        "objects_sha256": inventory.objects_sha256,
        "bundle_sha256": bundle.sha256,
        "app_version": target.app_version,
        "schema_revision": target.schema_revision,
        "update_id": update_id,
    }


def prepare_public_bundle(
    workspace: Path,
    artifact_dir: Path,
    *,
    repository: str,
    expected_commit: str,
    update_id: str | None = None,
    now: datetime | None = None,
    public_repository_verifier: PublicRepositoryVerifier = verify_public_github_repository,
    ci_verifier: CiVerifier = verify_exact_ci,
) -> ActivationPreparation:
    """验证公开 main 和 CI，并原子生成 bundle 与对象摘要。"""

    resolved_workspace = _resolve_isolated_workspace(workspace)
    resolved_artifacts = _resolve_new_artifact_dir(
        resolved_workspace,
        artifact_dir,
    )
    target, inventory = validate_public_workspace(
        resolved_workspace,
        repository=repository,
        expected_commit=expected_commit,
    )
    public_repository_verifier(target.repository, target.commit)
    ci_verifier(target.repository, target.commit)
    resolved_update_id = _resolve_update_id(
        target.commit,
        update_id=update_id,
        now=now,
    )

    temporary = Path(
        tempfile.mkdtemp(
            prefix=f".{resolved_artifacts.name}.",
            dir=resolved_artifacts.parent,
        )
    )
    temporary.chmod(0o700)
    try:
        bundle = _create_and_verify_bundle(
            resolved_workspace,
            temporary / BUNDLE_FILE,
            target=target,
            inventory=inventory,
        )
        _write_json_new(
            temporary / INVENTORY_FILE,
            _inventory_document(target, inventory, bundle, resolved_update_id),
        )
        os.replace(temporary, resolved_artifacts)
        _fsync_directory(resolved_artifacts.parent)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    final_bundle = BundleArtifact(
        path=resolved_artifacts / BUNDLE_FILE,
        sha256=bundle.sha256,
        ref=bundle.ref,
    )
    return ActivationPreparation(
        workspace=resolved_workspace,
        artifact_dir=resolved_artifacts,
        target=target,
        inventory=inventory,
        bundle=final_bundle,
        update_id=resolved_update_id,
    )


def _load_json_object(path: Path, *, label: str) -> dict[str, object]:
    try:
        raw = _read_private_file(
            path,
            maximum_size=_MAX_METADATA_BYTES,
            label=label,
        )
        value = json.loads(raw.decode("utf-8"))
    except ActivationError:
        raise
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ActivationError(f"{label} is invalid") from error
    if type(value) is not dict:
        _fail(f"{label} is invalid")
    return value


def _load_inventory(
    path: Path,
) -> tuple[TargetIdentity, ObjectInventory, str, str]:
    value = _load_json_object(path, label="object inventory summary")
    if set(value) != _INVENTORY_FIELDS:
        _fail("object inventory summary fields are invalid")
    object_count = value.get("object_count")
    if (
        type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or value.get("ref") != MAIN_REF
        or value.get("object_format") != OBJECT_FORMAT
        or type(object_count) is not int
        or object_count <= 0
    ):
        _fail("object inventory summary values are invalid")

    def required_string(field: str, pattern: re.Pattern[str]) -> str:
        item = value.get(field)
        if type(item) is not str or pattern.fullmatch(item) is None:
            _fail("object inventory summary values are invalid")
        return item

    commit = required_string("commit", SHA_RE)
    tree = required_string("tree", SHA_RE)
    objects_sha256 = required_string("objects_sha256", SHA256_RE)
    bundle_sha256 = required_string("bundle_sha256", SHA256_RE)
    app_version = required_string(
        "app_version",
        re.compile(r"[0-9]+\.[0-9]+\.[0-9]+"),
    )
    schema_revision = required_string("schema_revision", MIGRATION_RE)
    update_id = required_string("update_id", UPDATE_ID_RE)
    if not update_id.endswith(f"-{commit[:12]}"):
        _fail("object inventory summary values are invalid")
    target = TargetIdentity(
        repository="",
        commit=commit,
        tree=tree,
        app_version=app_version,
        schema_revision=schema_revision,
    )
    inventory = ObjectInventory(
        object_count=object_count,
        objects_sha256=objects_sha256,
    )
    return target, inventory, bundle_sha256, update_id


def load_preparation(
    workspace: Path,
    artifact_dir: Path,
    *,
    repository: str,
    expected_commit: str,
    public_repository_verifier: PublicRepositoryVerifier = verify_public_github_repository,
    ci_verifier: CiVerifier = verify_exact_ci,
) -> ActivationPreparation:
    """在镜像构建后重新验证 workspace、CI、bundle 与对象摘要，关闭 TOCTOU。"""

    resolved_workspace = _resolve_isolated_workspace(workspace)
    resolved_artifacts = _resolve_existing_artifact_dir(
        resolved_workspace,
        artifact_dir,
    )
    target, actual_inventory = validate_public_workspace(
        resolved_workspace,
        repository=repository,
        expected_commit=expected_commit,
    )
    public_repository_verifier(target.repository, target.commit)
    ci_verifier(target.repository, target.commit)
    (
        recorded_target,
        recorded_inventory,
        recorded_bundle_sha,
        update_id,
    ) = _load_inventory(resolved_artifacts / INVENTORY_FILE)
    if (
        recorded_target.commit != target.commit
        or recorded_target.tree != target.tree
        or recorded_target.app_version != target.app_version
        or recorded_target.schema_revision != target.schema_revision
        or recorded_inventory != actual_inventory
    ):
        _fail("recorded public object inventory no longer matches main")
    bundle_path = resolved_artifacts / BUNDLE_FILE
    bundle = _create_existing_bundle_artifact(
        resolved_workspace,
        bundle_path,
        target=target,
        inventory=actual_inventory,
    )
    if bundle.sha256 != recorded_bundle_sha:
        _fail("public bundle digest no longer matches its inventory summary")
    return ActivationPreparation(
        workspace=resolved_workspace,
        artifact_dir=resolved_artifacts,
        target=target,
        inventory=actual_inventory,
        bundle=bundle,
        update_id=update_id,
    )


def _create_existing_bundle_artifact(
    workspace: Path,
    path: Path,
    *,
    target: TargetIdentity,
    inventory: ObjectInventory,
) -> BundleArtifact:
    with tempfile.TemporaryDirectory(
        prefix=".public-bundle-snapshot-",
        dir=path.parent,
    ) as temporary:
        snapshot_root = Path(temporary)
        snapshot_root.chmod(0o700)
        snapshot = snapshot_root / BUNDLE_FILE
        digest, _size = _copy_private_file(
            path,
            snapshot,
            expected_mode=0o600,
            maximum_size=_MAX_BUNDLE_BYTES,
            label="public bundle",
        )
        _run_git(
            workspace,
            ("bundle", "verify", str(snapshot)),
            operation="existing public bundle verification",
        )
        _version, heads = _bundle_header(snapshot)
        if heads != ((target.commit, MAIN_REF),):
            _fail("existing bundle does not contain exact main")
        listed = _git_text(
            workspace,
            "bundle",
            "list-heads",
            str(snapshot),
            operation="existing bundle head listing",
        )
        if listed != f"{target.commit} {MAIN_REF}":
            _fail("existing bundle list-heads is invalid")
        _verify_bundle_clone(
            snapshot,
            artifact_parent=snapshot_root,
            target=target,
            inventory=inventory,
        )
    return BundleArtifact(path=path, sha256=digest, ref=MAIN_REF)


def _write_context_blob(
    root: Path,
    entry: ContextEntry,
    payload: bytes,
) -> None:
    if len(payload) != entry.size or hashlib.sha256(payload).hexdigest() != entry.sha256:
        _fail("Git blob changed while materializing the build context")
    destination = root.joinpath(*entry.path.split("/"))
    try:
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        for parent in (destination.parent, *destination.parent.parents):
            if parent == root.parent:
                break
            if _is_within(parent, root):
                parent.chmod(0o700)
        descriptor = os.open(
            destination,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            entry.mode,
        )
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    _fail("activation context write failed")
                view = view[written:]
            os.fchmod(descriptor, entry.mode)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except ActivationError:
        raise
    except OSError as error:
        raise ActivationError("activation context write failed") from error


def _materialize_verified_context(
    preparation: ActivationPreparation,
    temporary_root: Path,
) -> tuple[Path, ContextInventory]:
    """从稳定 bundle 副本直接读取 blob，不执行 checkout/filter。"""

    bundle_snapshot = temporary_root / BUNDLE_FILE
    digest, _size = _copy_private_file(
        preparation.bundle.path,
        bundle_snapshot,
        expected_mode=0o600,
        maximum_size=_MAX_BUNDLE_BYTES,
        label="public bundle",
    )
    if digest != preparation.bundle.sha256:
        _fail("public bundle changed before the image build")
    bare = temporary_root / "source.git"
    _run_git(
        temporary_root,
        ("init", "--bare", str(bare)),
        operation="canonical context repository initialization",
    )
    _run_git(
        bare,
        (
            "fetch",
            "--no-tags",
            str(bundle_snapshot),
            f"{MAIN_REF}:{MAIN_REF}",
        ),
        operation="canonical context bundle fetch",
    )
    _run_git(
        bare,
        ("symbolic-ref", "HEAD", MAIN_REF),
        operation="canonical context HEAD binding",
    )
    refs = _git_text(
        bare,
        "for-each-ref",
        "--format=%(refname)%09%(objectname)",
        operation="canonical context ref inventory",
    )
    if refs != f"{MAIN_REF}\t{preparation.target.commit}":
        _fail("canonical context repository contains unexpected refs")
    actual_tree = _git_text(
        bare,
        "rev-parse",
        f"{MAIN_REF}^{{tree}}",
        operation="canonical context tree identity",
    )
    if actual_tree != preparation.target.tree:
        _fail("canonical context tree differs from the public target")
    object_inventory = _validate_object_store(bare)
    if object_inventory != preparation.inventory:
        _fail("canonical context object inventory differs from public main")
    inventory = _context_inventory(bare, MAIN_REF)
    context = temporary_root / "context"
    try:
        context.mkdir(mode=0o700)
        context.chmod(0o700)
    except OSError as error:
        raise ActivationError("canonical build context cannot be created") from error
    for entry in inventory.entries:
        payload = _git_blob(bare, entry.object_id)
        _write_context_blob(context, entry, payload)
    _verify_context(context, inventory)
    return context, inventory


def _docker_build_arguments(
    *,
    component: str,
    context: Path,
    target: TargetIdentity,
) -> tuple[str, ...]:
    if component not in ("api", "web"):
        _fail("image component is invalid")
    source_directory = {"api": "backend", "web": "frontend"}[component]
    return (
        "docker",
        "buildx",
        "build",
        "--platform",
        "linux/amd64",
        "--load",
        "--file",
        str(context / source_directory / "Dockerfile"),
        "--build-arg",
        f"APP_VERSION={target.app_version}",
        "--build-arg",
        f"GIT_SHA={target.commit}",
        "--build-arg",
        f"SCHEMA_REVISION={target.schema_revision}",
        "--tag",
        f"sms-platform-test-{component}:{target.commit}",
        str(context),
    )


def _inspect_docker_image(
    runner: DockerRunner,
    *,
    component: str,
    target: TargetIdentity,
) -> tuple[str, str]:
    image_ref = f"sms-platform-test-{component}:{target.commit}"
    observed = runner.run(
        (
            "docker",
            "image",
            "inspect",
            "--format",
            (
                "{{.Id}}|{{.Architecture}}|"
                '{{index .Config.Labels "org.opencontainers.image.version"}}|'
                '{{index .Config.Labels "org.opencontainers.image.revision"}}|'
                '{{index .Config.Labels "com.sms-platform.schema-revision"}}'
            ),
            image_ref,
        ),
        operation=f"{component} image inspection",
        timeout=120,
    )
    parts = observed.split("|")
    if (
        len(parts) != 5
        or IMAGE_ID_RE.fullmatch(parts[0]) is None
        or parts[1] != "amd64"
        or parts[2] != target.app_version
        or parts[3] != target.commit
        or parts[4] != target.schema_revision
    ):
        _fail(f"{component} image identity or labels are invalid")
    return image_ref, parts[0]


def _tar_member_bytes(
    archive: tarfile.TarFile,
    *,
    name: str,
    maximum_size: int,
) -> bytes:
    matches = [member for member in archive.getmembers() if member.name == name]
    if (
        len(matches) != 1
        or not matches[0].isfile()
        or matches[0].issym()
        or matches[0].islnk()
        or not 1 <= matches[0].size <= maximum_size
    ):
        _fail("Docker image archive metadata is unsafe")
    handle = archive.extractfile(matches[0])
    if handle is None:
        _fail("Docker image archive metadata is unavailable")
    payload = handle.read(maximum_size + 1)
    if len(payload) != matches[0].size:
        _fail("Docker image archive metadata is incomplete")
    return payload


def _verify_docker_archive(
    path: Path,
    *,
    image_ref: str,
    image_id: str,
    target: TargetIdentity,
) -> str:
    """直接复核 docker-save config digest、标签和架构。"""

    descriptor, before = _open_private_regular(
        path,
        expected_mode=0o600,
        maximum_size=_MAX_IMAGE_ARCHIVE_BYTES,
        label="Docker image archive",
    )
    try:
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            try:
                with tarfile.open(fileobj=stream, mode="r:*") as archive:
                    raw_manifest = _tar_member_bytes(
                        archive,
                        name="manifest.json",
                        maximum_size=_MAX_METADATA_BYTES,
                    )
                    try:
                        manifest = json.loads(raw_manifest.decode("utf-8"))
                    except (UnicodeError, json.JSONDecodeError) as error:
                        raise ActivationError(
                            "Docker image archive manifest is invalid"
                        ) from error
                    if type(manifest) is not list or len(manifest) != 1:
                        _fail("Docker image archive must contain exactly one image")
                    record = manifest[0]
                    if type(record) is not dict:
                        _fail("Docker image archive manifest is invalid")
                    config_name = record.get("Config")
                    repo_tags = record.get("RepoTags")
                    if (
                        type(config_name) is not str
                        or not config_name
                        or config_name.startswith("/")
                        or "\\" in config_name
                        or ".." in Path(config_name).parts
                        or repo_tags != [image_ref]
                    ):
                        _fail("Docker image archive binding is invalid")
                    raw_config = _tar_member_bytes(
                        archive,
                        name=config_name,
                        maximum_size=_MAX_METADATA_BYTES,
                    )
            except (tarfile.TarError, OSError) as error:
                raise ActivationError("Docker image archive is invalid") from error
        if f"sha256:{hashlib.sha256(raw_config).hexdigest()}" != image_id:
            _fail("Docker image archive config does not match its image ID")
        try:
            config = json.loads(raw_config.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ActivationError("Docker image config is invalid") from error
        if type(config) is not dict or config.get("architecture") != "amd64":
            _fail("Docker image archive architecture is invalid")
        config_body = config.get("config")
        if type(config_body) is not dict or type(config_body.get("Labels")) is not dict:
            _fail("Docker image archive labels are invalid")
        labels = config_body["Labels"]
        expected_labels = {
            _DOCKER_LABELS["version"]: target.app_version,
            _DOCKER_LABELS["revision"]: target.commit,
            _DOCKER_LABELS["schema_revision"]: target.schema_revision,
        }
        if any(labels.get(name) != value for name, value in expected_labels.items()):
            _fail("Docker image archive labels are invalid")
        os.lseek(descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        size = 0
        while block := os.read(descriptor, 1024 * 1024):
            size += len(block)
            if size > _MAX_IMAGE_ARCHIVE_BYTES:
                _fail("Docker image archive is too large")
            digest.update(block)
        if size != before.st_size:
            _fail("Docker image archive changed while hashing")
        _require_open_file_unchanged(
            path,
            descriptor,
            before,
            label="Docker image archive",
        )
    except ActivationError:
        raise
    except OSError as error:
        raise ActivationError("Docker image archive cannot be verified") from error
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _save_and_verify_image(
    runner: DockerRunner,
    *,
    temporary_root: Path,
    component: str,
    target: TargetIdentity,
) -> ImageArtifact:
    image_ref, image_id = _inspect_docker_image(
        runner,
        component=component,
        target=target,
    )
    archive = temporary_root / f"{component}.tar"
    if archive.exists() or archive.is_symlink():
        _fail("Docker archive destination is not empty")
    runner.run(
        (
            "docker",
            "image",
            "save",
            "--output",
            str(archive),
            image_ref,
        ),
        operation=f"{component} image archive",
        timeout=900,
    )
    try:
        metadata = archive.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or not 1 <= metadata.st_size <= _MAX_IMAGE_ARCHIVE_BYTES
        ):
            _fail("Docker image archive output is unsafe")
        archive.chmod(0o600)
    except ActivationError:
        raise
    except OSError as error:
        raise ActivationError("Docker image archive cannot be secured") from error
    second_ref, second_id = _inspect_docker_image(
        runner,
        component=component,
        target=target,
    )
    if (second_ref, second_id) != (image_ref, image_id):
        _fail("Docker image identity changed while saving")
    digest = _verify_docker_archive(
        archive,
        image_ref=image_ref,
        image_id=image_id,
        target=target,
    )
    return ImageArtifact(
        file=f"{component}.tar",
        sha256=digest,
        ref=image_ref,
        image_id=image_id,
        version=target.app_version,
        revision=target.commit,
        schema_revision=target.schema_revision,
    )


def _image_document(image: ImageArtifact) -> dict[str, object]:
    return {
        "file": image.file,
        "sha256": image.sha256,
        "ref": image.ref,
        "id": image.image_id,
        "version": image.version,
        "revision": image.revision,
        "schema_revision": image.schema_revision,
    }


def _built_images_document(
    preparation: ActivationPreparation,
    inventory: ContextInventory,
    images: Mapping[str, ImageArtifact],
) -> dict[str, object]:
    if set(images) != {"api", "web"}:
        _fail("exactly API and Web images must be built")
    return {
        "schema_version": 1,
        "update_id": preparation.update_id,
        "commit": preparation.target.commit,
        "tree": preparation.target.tree,
        "context": {
            "file_count": len(inventory.entries),
            "bytes": inventory.total_bytes,
            "sha256": inventory.sha256,
        },
        "images": {
            component: _image_document(images[component])
            for component in ("api", "web")
        },
    }


def build_public_images(
    preparation: ActivationPreparation,
    *,
    docker_runner: DockerRunner | None = None,
) -> Mapping[str, ImageArtifact]:
    """从 verified bundle 的规范 blob 上下文构建并冻结两张镜像。"""

    runner = docker_runner or FixedDockerRunner()
    destinations = [
        preparation.artifact_dir / API_ARCHIVE,
        preparation.artifact_dir / WEB_ARCHIVE,
        preparation.artifact_dir / BUILT_IMAGES_FILE,
        preparation.artifact_dir / MANIFEST_FILE,
        preparation.artifact_dir / STANDARD_REQUEST_FILE,
    ]
    for destination in destinations:
        try:
            destination.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ActivationError("activation output identity is unavailable") from error
        _fail("activation image outputs must not already exist")

    published: list[Path] = []
    try:
        with tempfile.TemporaryDirectory(
            prefix=".canonical-image-build-",
            dir=preparation.artifact_dir,
        ) as temporary:
            temporary_root = Path(temporary)
            temporary_root.chmod(0o700)
            context, inventory = _materialize_verified_context(
                preparation,
                temporary_root,
            )
            images: dict[str, ImageArtifact] = {}
            for component in ("api", "web"):
                _verify_context(context, inventory)
                runner.run(
                    _docker_build_arguments(
                        component=component,
                        context=context,
                        target=preparation.target,
                    ),
                    operation=f"{component} image build",
                    timeout=3600,
                )
                _verify_context(context, inventory)
                images[component] = _save_and_verify_image(
                    runner,
                    temporary_root=temporary_root,
                    component=component,
                    target=preparation.target,
                )
                _verify_context(context, inventory)

            for component in ("api", "web"):
                image = images[component]
                source = temporary_root / image.file
                destination = preparation.artifact_dir / image.file
                digest, _size = _copy_private_file(
                    source,
                    destination,
                    expected_mode=0o600,
                    maximum_size=_MAX_IMAGE_ARCHIVE_BYTES,
                    label=f"{component} image archive",
                )
                if digest != image.sha256:
                    destination.unlink(missing_ok=True)
                    _fail("published image archive digest changed")
                if (
                    _sha256_private_file(
                        destination,
                        maximum_size=_MAX_IMAGE_ARCHIVE_BYTES,
                        label=f"{component} published image archive",
                    )
                    != image.sha256
                ):
                    destination.unlink(missing_ok=True)
                    _fail("published image archive cannot be reverified")
                published.append(destination)
            _write_json_new(
                preparation.artifact_dir / BUILT_IMAGES_FILE,
                _built_images_document(preparation, inventory, images),
            )
            published.append(preparation.artifact_dir / BUILT_IMAGES_FILE)
            _fsync_directory(preparation.artifact_dir)
            return images
    except BaseException:
        for path in reversed(published):
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
        raise


def _parse_image_artifact(
    value: object,
    *,
    component: str,
    target: TargetIdentity,
    artifact_dir: Path,
) -> ImageArtifact:
    if type(value) is not dict or set(value) != _BUILT_IMAGE_FIELDS:
        _fail("built image metadata fields are invalid")

    def required(field: str, pattern: re.Pattern[str]) -> str:
        item = value.get(field)
        if type(item) is not str or pattern.fullmatch(item) is None:
            _fail("built image metadata values are invalid")
        return item

    expected_file = f"{component}.tar"
    expected_ref = f"sms-platform-test-{component}:{target.commit}"
    file = required("file", re.compile(r"[a-z]+\.tar"))
    sha256 = required("sha256", SHA256_RE)
    ref = required(
        "ref",
        re.compile(r"sms-platform-test-(?:api|web):[0-9a-f]{40}"),
    )
    image_id = required("id", IMAGE_ID_RE)
    version = required("version", re.compile(r"[0-9]+\.[0-9]+\.[0-9]+"))
    revision = required("revision", SHA_RE)
    schema_revision = required("schema_revision", MIGRATION_RE)
    if (
        file != expected_file
        or ref != expected_ref
        or version != target.app_version
        or revision != target.commit
        or schema_revision != target.schema_revision
    ):
        _fail("built image metadata is not target-bound")
    actual_sha = _verify_docker_archive(
        artifact_dir / file,
        image_ref=ref,
        image_id=image_id,
        target=target,
    )
    if actual_sha != sha256:
        _fail("built image archive digest no longer matches metadata")
    return ImageArtifact(
        file=file,
        sha256=sha256,
        ref=ref,
        image_id=image_id,
        version=version,
        revision=revision,
        schema_revision=schema_revision,
    )


def load_built_images(
    preparation: ActivationPreparation,
) -> Mapping[str, ImageArtifact]:
    value = _load_json_object(
        preparation.artifact_dir / BUILT_IMAGES_FILE,
        label="built image metadata",
    )
    if set(value) != _BUILT_IMAGES_FIELDS or value.get("schema_version") != 1:
        _fail("built image metadata fields are invalid")
    if (
        value.get("update_id") != preparation.update_id
        or value.get("commit") != preparation.target.commit
        or value.get("tree") != preparation.target.tree
    ):
        _fail("built image metadata does not match the public target")
    context = value.get("context")
    if type(context) is not dict or set(context) != _BUILT_CONTEXT_FIELDS:
        _fail("built context metadata is invalid")
    if (
        type(context.get("file_count")) is not int
        or context["file_count"] <= 0
        or type(context.get("bytes")) is not int
        or context["bytes"] < 0
        or type(context.get("sha256")) is not str
        or SHA256_RE.fullmatch(context["sha256"]) is None
    ):
        _fail("built context metadata is invalid")
    expected_context = _context_inventory(
        preparation.workspace,
        MAIN_REF,
    )
    if context != {
        "file_count": len(expected_context.entries),
        "bytes": expected_context.total_bytes,
        "sha256": expected_context.sha256,
    }:
        _fail("built context metadata does not match the public Git tree")
    raw_images = value.get("images")
    if type(raw_images) is not dict or set(raw_images) != {"api", "web"}:
        _fail("built image metadata must contain exactly API and Web")
    return {
        component: _parse_image_artifact(
            raw_images[component],
            component=component,
            target=preparation.target,
            artifact_dir=preparation.artifact_dir,
        )
        for component in ("api", "web")
    }


def build_activation_manifest(
    preparation: ActivationPreparation,
    *,
    base_commit: str,
    base_tree: str,
    migration_head: str,
    images: Mapping[str, ImageArtifact] | None = None,
) -> dict[str, object]:
    """构造与标准更新 request 分离的严格公开基线 manifest。"""

    if (
        SHA_RE.fullmatch(base_commit) is None
        or SHA_RE.fullmatch(base_tree) is None
        or base_commit == preparation.target.commit
    ):
        _fail("opaque server base identity is invalid")
    if MIGRATION_RE.fullmatch(migration_head) is None:
        _fail("migration head is invalid")
    if migration_head != preparation.target.schema_revision:
        _fail("server and target migration heads must be identical")
    frozen = images if images is not None else load_built_images(preparation)
    if set(frozen) != {"api", "web"}:
        _fail("built image metadata must contain exactly API and Web")
    api = frozen["api"]
    web = frozen["web"]
    manifest: dict[str, object] = {
        "schema_version": 1,
        "activation_id": preparation.update_id,
        "origin_url": CANONICAL_ORIGIN_URL,
        "base": {"commit": base_commit, "tree": base_tree},
        "target": {
            "commit": preparation.target.commit,
            "tree": preparation.target.tree,
        },
        "bundle": {
            "file": BUNDLE_FILE,
            "sha256": preparation.bundle.sha256,
            "ref": MAIN_REF,
        },
        "images": {
            "api": _image_document(api),
            "web": _image_document(web),
        },
        "migration": {
            "from": migration_head,
            "target": migration_head,
            "compatibility": "none",
        },
    }
    if set(manifest) != _MANIFEST_FIELDS:
        raise AssertionError("activation manifest schema drift")
    return manifest


def build_standard_request(
    preparation: ActivationPreparation,
    *,
    base_commit: str,
    migration_head: str,
    environment_mode: str,
    images: Mapping[str, ImageArtifact] | None = None,
) -> dict[str, object]:
    """构造既有 test-update parser 可直接严格解析的标准 request。"""

    if SHA_RE.fullmatch(base_commit) is None or base_commit == preparation.target.commit:
        _fail("opaque server base identity is invalid")
    if migration_head != preparation.target.schema_revision:
        _fail("server and target migration heads must be identical")
    if environment_mode not in ("pre-live", "live"):
        _fail("environment mode is invalid")
    frozen = images if images is not None else load_built_images(preparation)
    if set(frozen) != {"api", "web"}:
        _fail("built image metadata must contain exactly API and Web")
    api = frozen["api"]
    web = frozen["web"]
    request: dict[str, object] = {
        "schema_version": 1,
        "update_id": preparation.update_id,
        "base_commit": base_commit,
        "commit": preparation.target.commit,
        "source_ref": "origin/main",
        "environment_mode": environment_mode,
        "components": ["api", "web"],
        "images": {
            "api": {
                "ref": api.ref,
                "id": api.image_id,
                "archive_file": api.file,
                "archive_sha256": api.sha256,
            },
            "web": {
                "ref": web.ref,
                "id": web.image_id,
                "archive_file": web.file,
                "archive_sha256": web.sha256,
            },
        },
        "migration": {
            "from": migration_head,
            "target": migration_head,
            "compatibility": "none",
        },
    }
    if set(request) != _STANDARD_REQUEST_FIELDS:
        raise AssertionError("standard request schema drift")
    return request


def _validate_document_binding(
    preparation: ActivationPreparation,
    manifest: Mapping[str, object],
    request: Mapping[str, object],
    *,
    images: Mapping[str, ImageArtifact],
) -> None:
    """在落盘前显式比较 manifest/request 的全部共享身份。"""

    manifest_base = manifest.get("base")
    if not isinstance(manifest_base, dict):
        _fail("activation manifest base identity is invalid")
    if (
        manifest.get("activation_id") != request.get("update_id")
        or manifest.get("activation_id") != preparation.update_id
        or manifest.get("target")
        != {
            "commit": request.get("commit"),
            "tree": preparation.target.tree,
        }
        or manifest_base.get("commit") != request.get("base_commit")
        or manifest.get("migration") != request.get("migration")
    ):
        _fail("activation manifest and standard request identities differ")
    manifest_images = manifest.get("images")
    request_images = request.get("images")
    if not isinstance(manifest_images, dict) or not isinstance(request_images, dict):
        _fail("activation image documents are invalid")
    for component in ("api", "web"):
        image = images[component]
        expected_manifest = _image_document(image)
        expected_request = {
            "ref": image.ref,
            "id": image.image_id,
            "archive_file": image.file,
            "archive_sha256": image.sha256,
        }
        if (
            manifest_images.get(component) != expected_manifest
            or request_images.get(component) != expected_request
        ):
            _fail("activation image documents are not exactly bound")


def _validate_standard_request_with_shared_parser(
    request: Mapping[str, object],
) -> None:
    deploy_scripts = PUBLIC_WORKSPACE_ROOT / "deploy" / "scripts"
    deploy_path = str(deploy_scripts)
    if deploy_path not in sys.path:
        sys.path.insert(0, deploy_path)
    try:
        from test_update_contract import (  # noqa: PLC0415
            TestUpdateContractError,
            parse_test_update_request,
        )
    except ImportError as error:
        raise ActivationError("shared update contract is unavailable") from error
    try:
        parse_test_update_request(
            json.dumps(request, separators=(",", ":"), sort_keys=True)
        )
    except (TestUpdateContractError, TypeError, ValueError) as error:
        raise ActivationError(
            "standard request does not satisfy the shared update contract"
        ) from error


def write_activation_manifest(
    preparation: ActivationPreparation,
    *,
    base_commit: str,
    base_tree: str,
    migration_head: str,
) -> Path:
    """以 ``0600`` 新文件写入 baseline manifest；永不覆盖既有文件。"""

    manifest = build_activation_manifest(
        preparation,
        base_commit=base_commit,
        base_tree=base_tree,
        migration_head=migration_head,
    )
    path = preparation.artifact_dir / MANIFEST_FILE
    _write_json_new(path, manifest)
    return path


def write_activation_payloads(
    preparation: ActivationPreparation,
    *,
    base_commit: str,
    base_tree: str,
    migration_head: str,
    environment_mode: str,
) -> tuple[Path, Path]:
    """先写 baseline manifest，最后写标准 request 作为本地发布标记。"""

    images = load_built_images(preparation)
    manifest = build_activation_manifest(
        preparation,
        base_commit=base_commit,
        base_tree=base_tree,
        migration_head=migration_head,
        images=images,
    )
    request = build_standard_request(
        preparation,
        base_commit=base_commit,
        migration_head=migration_head,
        environment_mode=environment_mode,
        images=images,
    )
    _validate_document_binding(
        preparation,
        manifest,
        request,
        images=images,
    )
    _validate_standard_request_with_shared_parser(request)
    manifest_path = preparation.artifact_dir / MANIFEST_FILE
    request_path = preparation.artifact_dir / STANDARD_REQUEST_FILE
    for output in (manifest_path, request_path):
        try:
            output.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ActivationError("activation payload identity is unavailable") from error
        _fail("activation payload files must not already exist")
    _write_json_new(manifest_path, manifest)
    try:
        _write_json_new(request_path, request)
        _fsync_directory(preparation.artifact_dir)
    except BaseException:
        manifest_path.unlink(missing_ok=True)
        request_path.unlink(missing_ok=True)
        raise
    return manifest_path, request_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--workspace", required=True, type=Path)
    prepare.add_argument("--artifacts", required=True, type=Path)
    prepare.add_argument("--repository", required=True)
    prepare.add_argument("--commit", required=True)
    prepare.add_argument("--update-id")

    build = subparsers.add_parser("build")
    build.add_argument("--workspace", required=True, type=Path)
    build.add_argument("--artifacts", required=True, type=Path)
    build.add_argument("--repository", required=True)
    build.add_argument("--commit", required=True)

    finalize = subparsers.add_parser("finalize")
    finalize.add_argument("--workspace", required=True, type=Path)
    finalize.add_argument("--artifacts", required=True, type=Path)
    finalize.add_argument("--repository", required=True)
    finalize.add_argument("--commit", required=True)
    finalize.add_argument("--base-commit", required=True)
    finalize.add_argument("--base-tree", required=True)
    finalize.add_argument("--migration-head", required=True)
    finalize.add_argument(
        "--environment-mode",
        required=True,
        choices=("pre-live", "live"),
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    public_repository_verifier: PublicRepositoryVerifier = (
        verify_public_github_repository
    ),
    ci_verifier: CiVerifier = verify_exact_ci,
    docker_runner: DockerRunner | None = None,
) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "prepare":
            result = prepare_public_bundle(
                arguments.workspace,
                arguments.artifacts,
                repository=arguments.repository,
                expected_commit=arguments.commit,
                update_id=arguments.update_id,
                public_repository_verifier=public_repository_verifier,
                ci_verifier=ci_verifier,
            )
            print(
                json.dumps(
                    {
                        "state": "prepared",
                        "commit": result.target.commit,
                        "tree": result.target.tree,
                        "bundle_sha256": result.bundle.sha256,
                        "object_count": result.inventory.object_count,
                        "objects_sha256": result.inventory.objects_sha256,
                        "update_id": result.update_id,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            return 0
        preparation = load_preparation(
            arguments.workspace,
            arguments.artifacts,
            repository=arguments.repository,
            expected_commit=arguments.commit,
            public_repository_verifier=public_repository_verifier,
            ci_verifier=ci_verifier,
        )
        if arguments.command == "build":
            images = build_public_images(
                preparation,
                docker_runner=docker_runner,
            )
            print(
                json.dumps(
                    {
                        "state": "images-ready",
                        "commit": preparation.target.commit,
                        "images": {
                            component: {
                                "id": images[component].image_id,
                                "sha256": images[component].sha256,
                            }
                            for component in ("api", "web")
                        },
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            return 0
        manifest_path, request_path = write_activation_payloads(
            preparation,
            base_commit=arguments.base_commit,
            base_tree=arguments.base_tree,
            migration_head=arguments.migration_head,
            environment_mode=arguments.environment_mode,
        )
        print(
            json.dumps(
                {
                    "state": "manifest-ready",
                    "commit": preparation.target.commit,
                    "manifest": manifest_path.name,
                    "request": request_path.name,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 0
    except ActivationError as error:
        print(f"public-baseline-activation: {error}", file=sys.stderr)
        return 1
    except (OSError, subprocess.SubprocessError, ValueError):
        print(
            "public-baseline-activation: local activation operation failed",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
