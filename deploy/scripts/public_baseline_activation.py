#!/usr/bin/env python3
"""将无共同 Git 历史的公有基线一次性、可恢复地切换为测试服代码根。

本模块只负责离线证据校验、独立公有 Git 根准备、三项持久路径搬迁和目录
原子交换。数据库、Docker、volume、业务 ``/var/lib`` 状态和生命周期锁均由
外层管理器负责。
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import os
import re
import stat
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, cast

PUBLIC_ORIGIN_URL = (
    "https://github.com/neuer/enterprise-sms-platform-public.git"
)
PUBLIC_BUNDLE_REF = "refs/heads/main"
PUBLIC_REMOTE_REF = "refs/remotes/origin/main"
MIGRATION_HEAD = "0039_manual_job_outbox"
DEFAULT_ACTIVE_ROOT = Path("/opt/sms-platform")

PERSISTENT_PATHS = (
    Path(".env"),
    Path("deploy/secrets"),
    Path("backend/.venv"),
)
DISCARDABLE_CACHE_PATHS = (
    Path("deploy/scripts/__pycache__"),
    Path("scripts/__pycache__"),
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
_GIT_FIELDS = frozenset({"commit", "tree"})
_BUNDLE_FIELDS = frozenset({"file", "sha256", "ref"})
_IMAGE_FIELDS = frozenset(
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
_MIGRATION_FIELDS = frozenset({"from", "target", "compatibility"})
_JOURNAL_FIELDS = frozenset(
    {
        "schema_version",
        "activation_id",
        "request_sha256",
        "state",
        "base",
        "target",
        "version",
        "building_root",
        "staged_root",
        "recovery_root",
        "cleanup_root",
        "cleanup_dev",
        "cleanup_ino",
        "moved_persistence",
    }
)
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
_ACTIVATION_ID_RE = re.compile(
    r"test-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}"
)
_VERSION_RE = re.compile(r"[0-9]+[.][0-9]+[.][0-9]+")
_ARCHIVE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,126}[A-Za-z0-9]")
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_JOURNAL_BYTES = 64 * 1024
_MAX_BUNDLE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_IMAGE_BYTES = 16 * 1024 * 1024 * 1024
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_ACTIVE_ROOT_MODE = 0o2770
_OPERATOR_DIRECTORY_MODE = 0o2770
_CLEANUP_ROOT_MODE = 0o700
_CLEANUP_PROTECTED_PATHS = PERSISTENT_PATHS + (
    Path("data"),
    Path("volumes"),
    Path("deploy/data"),
    Path("deploy/volumes"),
    Path("backend/data"),
    Path("postgres-data"),
    Path("redis-data"),
    Path("pgdata"),
    Path("redisdata"),
    Path("redisauthdata"),
    Path("rediscontroldata"),
    Path("importdata"),
    Path("exportdata"),
)


class PublicBaselineActivationError(RuntimeError):
    """公有基线证据、状态或文件系统边界不满足安全合同。"""


class ActivationState(StrEnum):
    BUILDING = "building"
    PREPARED = "prepared"
    PERSISTENCE_MOVED = "persistence_moved"
    ROOTS_EXCHANGED = "roots_exchanged"
    APPLIED = "applied"
    VERIFIED = "verified"
    FINALIZING = "finalizing"
    CLEANED = "cleaned"
    ROLLED_BACK = "rolled_back"


class LifecycleGuard(Protocol):
    """由外层持有生命周期锁后提供的只读断言。"""

    def require_held(self) -> None: ...


class DirectoryExchange(Protocol):
    """在同一文件系统上原子交换两个已存在目录。"""

    def exchange(self, left: Path, right: Path) -> None: ...


@dataclass(frozen=True)
class GitIdentity:
    commit: str
    tree: str


@dataclass(frozen=True)
class BundleEvidence:
    file: str
    sha256: str
    ref: str

    @property
    def archive_file(self) -> str:
        return self.file

    @property
    def archive_sha256(self) -> str:
        return self.sha256


@dataclass(frozen=True)
class ImageEvidence:
    file: str
    sha256: str
    ref: str
    image_id: str
    version: str
    revision: str
    schema_revision: str

    @property
    def id(self) -> str:
        return self.image_id

    @property
    def archive_file(self) -> str:
        return self.file

    @property
    def archive_sha256(self) -> str:
        return self.sha256


@dataclass(frozen=True)
class MigrationEvidence:
    migration_from: str
    target: str
    compatibility: str

    @property
    def from_revision(self) -> str:
        return self.migration_from


@dataclass(frozen=True)
class ActivationRequest:
    schema_version: int
    activation_id: str
    origin_url: str
    base: GitIdentity
    target: GitIdentity
    bundle: BundleEvidence
    images: Mapping[str, ImageEvidence]
    migration: MigrationEvidence

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> ActivationRequest:
        """严格解析独立的 ``baseline-manifest.json``。"""

        if type(raw) is not bytes or not 0 < len(raw) <= _MAX_MANIFEST_BYTES:
            raise PublicBaselineActivationError(
                "public baseline manifest size is invalid"
            )
        try:
            text = raw.decode("utf-8")
            value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PublicBaselineActivationError(
                "public baseline manifest is invalid"
            ) from exc
        if type(value) is not dict:
            raise PublicBaselineActivationError(
                "public baseline manifest must be an object"
            )
        document = cast(dict[str, object], value)
        _require_exact_fields(document, _MANIFEST_FIELDS, "manifest")

        if document["schema_version"] != 1:
            raise PublicBaselineActivationError(
                "public baseline manifest schema is invalid"
            )
        activation_id = _require_matching_string(
            document["activation_id"],
            _ACTIVATION_ID_RE,
            "activation ID",
        )
        if document["origin_url"] != PUBLIC_ORIGIN_URL:
            raise PublicBaselineActivationError(
                "public baseline origin URL is invalid"
            )
        base = _parse_git_identity(document["base"], "base")
        target = _parse_git_identity(document["target"], "target")
        if base.commit == target.commit:
            raise PublicBaselineActivationError(
                "public baseline target must differ from base"
            )
        bundle = _parse_bundle(document["bundle"])
        images = _parse_images(document["images"], target.commit)
        migration = _parse_migration(document["migration"])
        return cls(
            schema_version=1,
            activation_id=activation_id,
            origin_url=PUBLIC_ORIGIN_URL,
            base=base,
            target=target,
            bundle=bundle,
            images=images,
            migration=migration,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "activation_id": self.activation_id,
            "origin_url": self.origin_url,
            "base": {
                "commit": self.base.commit,
                "tree": self.base.tree,
            },
            "target": {
                "commit": self.target.commit,
                "tree": self.target.tree,
            },
            "bundle": {
                "file": self.bundle.file,
                "sha256": self.bundle.sha256,
                "ref": self.bundle.ref,
            },
            "images": {
                component: {
                    "file": image.file,
                    "sha256": image.sha256,
                    "ref": image.ref,
                    "id": image.image_id,
                    "version": image.version,
                    "revision": image.revision,
                    "schema_revision": image.schema_revision,
                }
                for component, image in sorted(self.images.items())
            },
            "migration": {
                "from": self.migration.migration_from,
                "target": self.migration.target,
                "compatibility": self.migration.compatibility,
            },
        }

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            self.to_mapping(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class PreparedActivation:
    activation_id: str
    state: str
    staged_root: Path
    commit: str
    tree: str


@dataclass(frozen=True)
class ActivationOutcome:
    activation_id: str
    state: str
    active_root: Path
    commit: str
    tree: str
    recovery_root: Path

    def as_test_update_state(self) -> dict[str, object]:
        """返回可直接交给外层 TestUpdateStore 的非敏感观测值。"""

        mapped_state = "verified" if self.state == "verified" else self.state
        return {
            "state": mapped_state,
            "actual_commit": self.commit,
            "actual_migration_head": MIGRATION_HEAD,
        }


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        environment: Mapping[str, str],
    ) -> CommandResult: ...


class SubprocessRunner:
    def run(
        self,
        argv: Sequence[str],
        *,
        environment: Mapping[str, str],
    ) -> CommandResult:
        try:
            result = subprocess.run(
                list(argv),
                check=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                env=dict(environment),
            )
        except OSError as exc:
            raise PublicBaselineActivationError(
                "public baseline command is unavailable"
            ) from exc
        try:
            stdout = result.stdout.decode("utf-8", errors="strict")
            stderr = result.stderr.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise PublicBaselineActivationError(
                "public baseline command output is invalid"
            ) from exc
        return CommandResult(result.returncode, stdout, stderr)


class LinuxAtomicDirectoryExchange:
    """使用 Linux ``renameat2(RENAME_EXCHANGE)`` 完成不可分割根切换。"""

    _AT_FDCWD = -100
    _RENAME_EXCHANGE = 2

    def exchange(self, left: Path, right: Path) -> None:
        _require_real_directory(left, "active root")
        _require_real_directory(right, "staged root")
        if left.stat().st_dev != right.stat().st_dev:
            raise PublicBaselineActivationError(
                "public baseline roots must share a filesystem"
            )
        library = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(library, "renameat2", None)
        if renameat2 is None:
            raise PublicBaselineActivationError(
                "atomic directory exchange is unavailable"
            )
        renameat2.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int
        result = renameat2(
            self._AT_FDCWD,
            os.fsencode(left),
            self._AT_FDCWD,
            os.fsencode(right),
            self._RENAME_EXCHANGE,
        )
        if result != 0:
            error_number = ctypes.get_errno()
            raise PublicBaselineActivationError(
                "atomic directory exchange failed"
            ) from OSError(error_number, os.strerror(error_number))
        _fsync_directory(left.parent)


class PublicBaselineActivator:
    """公有基线切换的可恢复核心；调用方必须先持有生命周期锁。"""

    def __init__(
        self,
        *,
        activation_id: str,
        artifacts_root: Path,
        lifecycle_guard: LifecycleGuard,
        active_root: Path = DEFAULT_ACTIVE_ROOT,
        workspace_root: Path | None = None,
        directory_exchange: DirectoryExchange | None = None,
        expected_uid: int = 0,
        expected_operator_uid: int,
        expected_operator_gid: int,
        expected_system_gid: int,
        runner: CommandRunner | None = None,
    ) -> None:
        if (
            type(activation_id) is not str
            or _ACTIVATION_ID_RE.fullmatch(activation_id) is None
        ):
            raise PublicBaselineActivationError("activation ID is invalid")
        if type(expected_uid) is not int or expected_uid < 0:
            raise PublicBaselineActivationError("expected owner is invalid")
        if (
            type(expected_operator_uid) is not int
            or expected_operator_uid < 0
            or type(expected_operator_gid) is not int
            or expected_operator_gid < 0
        ):
            raise PublicBaselineActivationError(
                "expected operator identity is invalid"
            )
        if type(expected_system_gid) is not int or expected_system_gid < 0:
            raise PublicBaselineActivationError(
                "expected system identity is invalid"
            )
        if os.geteuid() != expected_uid:
            raise PublicBaselineActivationError(
                "public baseline process owner is invalid"
            )
        self.activation_id = activation_id
        self.artifacts_root = _require_absolute_path(
            artifacts_root, "artifact root"
        )
        self.active_root = _require_absolute_path(active_root, "active root")
        self.workspace_root = _require_absolute_path(
            workspace_root or self.active_root.parent,
            "workspace root",
        )
        if _is_within(self.workspace_root, Path("/var/lib")):
            raise PublicBaselineActivationError(
                "workspace must not use authoritative /var/lib state"
            )
        self.lifecycle_guard = lifecycle_guard
        self.directory_exchange = (
            directory_exchange or LinuxAtomicDirectoryExchange()
        )
        self.expected_uid = expected_uid
        self.expected_operator_uid = expected_operator_uid
        self.expected_operator_gid = expected_operator_gid
        self.expected_system_gid = expected_system_gid
        self.runner = runner or SubprocessRunner()
        suffix = self.activation_id
        stem = f".{self.active_root.name}-public-baseline-{suffix}"
        self.building_root = self.workspace_root / f"{stem}-building"
        self.staged_root = self.workspace_root / f"{stem}-staged"
        self.recovery_root = self.workspace_root / f"{stem}-recovery"
        self.cleanup_root = self.workspace_root / f"{stem}-cleanup-tombstone"
        self.journal_path = self.workspace_root / f"{stem}-state.json"

    def prepare(self, request: ActivationRequest) -> PreparedActivation:
        """只准备并验证目标根，不搬迁运行态路径，也不切换 active root。"""

        self.lifecycle_guard.require_held()
        self._require_request(request)
        self._validate_boundaries()
        self._validate_artifacts(request)
        self._validate_base_root(request)

        if _lexists(self.journal_path):
            journal = self._read_journal(request)
            state = ActivationState(cast(str, journal["state"]))
            if state == ActivationState.PREPARED:
                self._validate_target_root(self.staged_root, request)
                return self._prepared(request)
            if state == ActivationState.BUILDING:
                if _lexists(self.staged_root):
                    self._validate_target_root(self.staged_root, request)
                    journal["state"] = ActivationState.PREPARED.value
                    self._write_journal(journal)
                    return self._prepared(request)
                if _lexists(self.building_root):
                    self._validate_target_root(self.building_root, request)
                    os.rename(self.building_root, self.staged_root)
                    _fsync_directory(self.workspace_root)
                    journal["state"] = ActivationState.PREPARED.value
                    self._write_journal(journal)
                    return self._prepared(request)
            raise PublicBaselineActivationError(
                "public baseline activation is not preparable"
            )

        for path in (
            self.building_root,
            self.staged_root,
            self.recovery_root,
            self.cleanup_root,
        ):
            if _lexists(path):
                raise PublicBaselineActivationError(
                    "public baseline workspace is not empty"
                )
        journal = self._new_journal(request, ActivationState.BUILDING)
        self._write_journal(journal, must_not_exist=True)
        self._build_standalone_target(request)
        os.rename(self.building_root, self.staged_root)
        _fsync_directory(self.workspace_root)
        journal["state"] = ActivationState.PREPARED.value
        self._write_journal(journal)
        return self._prepared(request)

    def activate(self, request: ActivationRequest) -> ActivationOutcome:
        """迁移三项 allowlist 后原子切换根，并保留旧根供回滚。"""

        self.lifecycle_guard.require_held()
        self._require_request(request)
        self._validate_boundaries()
        self._validate_artifacts(request)
        journal = self._read_journal(request)
        state = ActivationState(cast(str, journal["state"]))
        if state not in {
            ActivationState.PREPARED,
            ActivationState.PERSISTENCE_MOVED,
            ActivationState.ROOTS_EXCHANGED,
            ActivationState.APPLIED,
            ActivationState.VERIFIED,
        }:
            raise PublicBaselineActivationError(
                "public baseline activation is not applicable"
            )

        arrangement = self._arrangement(request)
        if arrangement == "applied":
            journal["state"] = (
                ActivationState.VERIFIED.value
                if state == ActivationState.VERIFIED
                else ActivationState.APPLIED.value
            )
            self._write_journal(journal)
            return self._target_outcome(request, cast(str, journal["state"]))

        if arrangement == "before_exchange":
            self._move_persistence_to_target(request, journal)
            self._apply_active_root_profile(self.staged_root)
            self._validate_target_root(
                self.staged_root,
                request,
                allow_persistence=True,
                active_profile=True,
            )
            self.directory_exchange.exchange(
                self.active_root,
                self.staged_root,
            )
            journal["state"] = ActivationState.ROOTS_EXCHANGED.value
            self._write_journal(journal)
            arrangement = "exchanged"

        if arrangement != "exchanged":
            raise PublicBaselineActivationError(
                "public baseline root arrangement is invalid"
            )
        self._validate_target_root(
            self.active_root,
            request,
            allow_persistence=True,
            active_profile=True,
        )
        self._validate_base_identity(self.staged_root, request.base)
        if _lexists(self.recovery_root):
            raise PublicBaselineActivationError(
                "public baseline recovery root already exists"
            )
        os.rename(self.staged_root, self.recovery_root)
        _fsync_directory(self.workspace_root)
        journal["state"] = ActivationState.APPLIED.value
        self._write_journal(journal)
        return self._target_outcome(request, ActivationState.APPLIED.value)

    def rollback(self) -> ActivationOutcome:
        """从任意可识别阶段恢复原根；目标根保留作失败证据。"""

        self.lifecycle_guard.require_held()
        self._validate_boundaries()
        journal = self._read_journal()
        request = self._request_from_journal(journal)
        state = ActivationState(cast(str, journal["state"]))
        if state == ActivationState.ROLLED_BACK:
            self._validate_base_root(request)
            return self._base_outcome(request)
        if state == ActivationState.BUILDING:
            self._validate_base_root(request)
            journal["state"] = ActivationState.ROLLED_BACK.value
            self._write_journal(journal)
            return self._base_outcome(request)

        arrangement = self._arrangement(request)
        if arrangement == "applied":
            if _lexists(self.staged_root):
                raise PublicBaselineActivationError(
                    "rollback staging root already exists"
                )
            os.rename(self.recovery_root, self.staged_root)
            _fsync_directory(self.workspace_root)
            arrangement = "exchanged"

        if arrangement == "exchanged":
            self.directory_exchange.exchange(
                self.active_root,
                self.staged_root,
            )
            arrangement = "before_exchange"

        if arrangement == "before_exchange":
            self._restore_persistence_to_base(request)
            self._validate_base_root(request)
        elif arrangement == "building":
            self._validate_base_root(request)
        else:
            raise PublicBaselineActivationError(
                "public baseline rollback arrangement is invalid"
            )

        journal["state"] = ActivationState.ROLLED_BACK.value
        self._write_journal(journal)
        return self._base_outcome(request)

    def finalize(self) -> ActivationOutcome:
        """确认目标根已验收；旧根继续保留，绝不在此删除恢复证据。"""

        self.lifecycle_guard.require_held()
        self._validate_boundaries()
        journal = self._read_journal()
        request = self._request_from_journal(journal)
        state = ActivationState(cast(str, journal["state"]))
        if state not in {ActivationState.APPLIED, ActivationState.VERIFIED}:
            raise PublicBaselineActivationError(
                "public baseline activation is not finalizable"
            )
        if self._arrangement(request) != "applied":
            raise PublicBaselineActivationError(
                "public baseline final root arrangement is invalid"
            )
        self._validate_target_root(
            self.active_root,
            request,
            allow_persistence=True,
            active_profile=True,
        )
        self._validate_base_identity(self.recovery_root, request.base)
        journal["state"] = ActivationState.VERIFIED.value
        self._write_journal(journal)
        return self._target_outcome(request, ActivationState.VERIFIED.value)

    def cleanup(self) -> ActivationOutcome:
        """验收完成后删除旧根；固定 tombstone 使中断删除可安全重入。"""

        self.lifecycle_guard.require_held()
        self._validate_boundaries()
        self._validate_cleanup_path_separation()
        journal = self._read_journal()
        request = self._request_from_journal(journal)
        state = ActivationState(cast(str, journal["state"]))
        if state not in {
            ActivationState.VERIFIED,
            ActivationState.FINALIZING,
            ActivationState.CLEANED,
        }:
            raise PublicBaselineActivationError(
                "public baseline cleanup requires verified state"
            )
        self._validate_target_root(
            self.active_root,
            request,
            allow_persistence=True,
            active_profile=True,
        )
        self._validate_persistence(
            self.active_root,
            active_profile=True,
            require_virtualenv=True,
        )

        if state == ActivationState.CLEANED:
            if _lexists(self.recovery_root) or _lexists(self.cleanup_root):
                raise PublicBaselineActivationError(
                    "cleaned public baseline still has an old root"
                )
            return self._target_outcome(
                request,
                ActivationState.CLEANED.value,
            )

        if state == ActivationState.VERIFIED:
            self._start_cleanup(request, journal)
            state = ActivationState.FINALIZING

        if state != ActivationState.FINALIZING:
            raise PublicBaselineActivationError(
                "public baseline cleanup state is invalid"
            )
        if _lexists(self.recovery_root):
            raise PublicBaselineActivationError(
                "public baseline recovery root reappeared during cleanup"
            )
        if _lexists(self.cleanup_root):
            self._remove_cleanup_tombstone(journal)
        journal["state"] = ActivationState.CLEANED.value
        self._write_journal(journal)
        return self._target_outcome(
            request,
            ActivationState.CLEANED.value,
        )

    def _start_cleanup(
        self,
        request: ActivationRequest,
        journal: dict[str, object],
    ) -> None:
        recovery_exists = _lexists(self.recovery_root)
        tombstone_exists = _lexists(self.cleanup_root)
        if recovery_exists == tombstone_exists:
            raise PublicBaselineActivationError(
                "verified public baseline cleanup roots are invalid"
            )
        candidate = self.recovery_root if recovery_exists else self.cleanup_root
        self._validate_cleanup_candidate(candidate, request)
        if recovery_exists:
            os.rename(self.recovery_root, self.cleanup_root)
            _fsync_directory(self.workspace_root)
        metadata = self._lock_cleanup_tombstone()
        journal["cleanup_dev"] = metadata.st_dev
        journal["cleanup_ino"] = metadata.st_ino
        journal["state"] = ActivationState.FINALIZING.value
        self._write_journal(journal)

    def _validate_cleanup_path_separation(self) -> None:
        for candidate in (self.recovery_root, self.cleanup_root):
            if (
                candidate == self.workspace_root
                or _is_within(candidate, Path("/var/lib"))
                or _paths_overlap(candidate, self.active_root)
                or _paths_overlap(candidate, self.artifacts_root)
            ):
                raise PublicBaselineActivationError(
                    "public baseline cleanup path is unsafe"
                )
        if (
            self.cleanup_root == self.recovery_root
            or self.cleanup_root.parent != self.workspace_root
            or self.recovery_root.parent != self.workspace_root
        ):
            raise PublicBaselineActivationError(
                "public baseline cleanup path is not fixed"
            )

    def _validate_cleanup_candidate(
        self,
        root: Path,
        request: ActivationRequest,
    ) -> None:
        metadata = root.lstat()
        if root == self.cleanup_root and (
            not stat.S_ISLNK(metadata.st_mode)
            and stat.S_ISDIR(metadata.st_mode)
            and metadata.st_uid == self.expected_uid
            and metadata.st_gid == self.expected_operator_gid
            and stat.S_IMODE(metadata.st_mode) == _CLEANUP_ROOT_MODE
        ):
            for name in ("backend", "deploy"):
                self._validate_operator_directory(
                    root / name,
                    expected_device=metadata.st_dev,
                    context="cleanup checkout directory",
                )
        else:
            self._validate_active_root_profile(root)
        if metadata.st_dev not in {
            self.active_root.stat().st_dev,
            self.workspace_root.stat().st_dev,
        }:
            raise PublicBaselineActivationError(
                "public baseline recovery root filesystem is invalid"
            )
        if (
            metadata.st_dev != self.active_root.stat().st_dev
            or metadata.st_dev != self.workspace_root.stat().st_dev
        ):
            raise PublicBaselineActivationError(
                "public baseline recovery root crosses a filesystem"
            )
        self._validate_base_identity(root, request.base)
        self._require_single_worktree(root)
        if self._git(
            root,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        ):
            raise PublicBaselineActivationError(
                "public baseline recovery root is dirty"
            )
        self._validate_tracked_owners(root)
        self._validate_ignored_inventory(root)
        for relative in PERSISTENT_PATHS:
            if not _lexists(self.active_root / relative):
                raise PublicBaselineActivationError(
                    "persistent path is not present in active root"
                )
            if _lexists(root / relative):
                raise PublicBaselineActivationError(
                    "persistent path remains in recovery root"
                )
        descriptor = self._open_cleanup_directory(root)
        try:
            opened = os.fstat(descriptor)
            if (
                opened.st_dev != metadata.st_dev
                or opened.st_ino != metadata.st_ino
            ):
                raise PublicBaselineActivationError(
                    "public baseline recovery root changed during validation"
                )
            self._validate_cleanup_tree_fd(
                descriptor,
                root_device=metadata.st_dev,
                relative=Path(),
            )
        finally:
            os.close(descriptor)

    def _lock_cleanup_tombstone(self) -> os.stat_result:
        descriptor = self._open_cleanup_directory(self.cleanup_root)
        try:
            metadata = os.fstat(descriptor)
            if (
                metadata.st_uid != self.expected_uid
                or metadata.st_dev != self.active_root.stat().st_dev
                or metadata.st_dev != self.workspace_root.stat().st_dev
            ):
                raise PublicBaselineActivationError(
                    "public baseline cleanup tombstone identity is invalid"
                )
            os.fchmod(descriptor, _CLEANUP_ROOT_MODE)
            os.fsync(descriptor)
            locked = os.fstat(descriptor)
            if stat.S_IMODE(locked.st_mode) != _CLEANUP_ROOT_MODE:
                raise PublicBaselineActivationError(
                    "public baseline cleanup tombstone mode is invalid"
                )
            return locked
        except OSError as exc:
            raise PublicBaselineActivationError(
                "public baseline cleanup tombstone cannot be locked"
            ) from exc
        finally:
            os.close(descriptor)

    def _open_cleanup_directory(self, path: Path) -> int:
        try:
            return os.open(
                path,
                os.O_RDONLY | _DIRECTORY | _NOFOLLOW,
            )
        except OSError as exc:
            raise PublicBaselineActivationError(
                "public baseline cleanup directory is unsafe"
            ) from exc

    def _validate_cleanup_tree_fd(
        self,
        descriptor: int,
        *,
        root_device: int,
        relative: Path,
    ) -> None:
        try:
            names = os.listdir(descriptor)
        except OSError as exc:
            raise PublicBaselineActivationError(
                "public baseline cleanup inventory is unavailable"
            ) from exc
        allowed_owners = {self.expected_uid, self.expected_operator_uid}
        for name in names:
            if name in {"", ".", ".."} or "/" in name:
                raise PublicBaselineActivationError(
                    "public baseline cleanup entry name is invalid"
                )
            child_relative = relative / name
            if child_relative in _CLEANUP_PROTECTED_PATHS:
                raise PublicBaselineActivationError(
                    "protected runtime state remains in recovery root"
                )
            try:
                metadata = os.stat(
                    name,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise PublicBaselineActivationError(
                    "public baseline cleanup entry is unavailable"
                ) from exc
            if (
                metadata.st_dev != root_device
                or metadata.st_uid not in allowed_owners
                or stat.S_ISLNK(metadata.st_mode)
            ):
                raise PublicBaselineActivationError(
                    "public baseline cleanup entry metadata is unsafe"
                )
            if stat.S_ISDIR(metadata.st_mode):
                child = self._open_cleanup_child(
                    descriptor,
                    name,
                    metadata,
                )
                try:
                    self._validate_cleanup_tree_fd(
                        child,
                        root_device=root_device,
                        relative=child_relative,
                    )
                finally:
                    os.close(child)
            elif (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
            ):
                raise PublicBaselineActivationError(
                    "public baseline cleanup entry type is unsafe"
                )

    def _open_cleanup_child(
        self,
        parent_descriptor: int,
        name: str,
        expected: os.stat_result,
    ) -> int:
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | _DIRECTORY | _NOFOLLOW,
                dir_fd=parent_descriptor,
            )
        except OSError as exc:
            raise PublicBaselineActivationError(
                "public baseline cleanup child is unsafe"
            ) from exc
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != expected.st_dev
            or opened.st_ino != expected.st_ino
            or not stat.S_ISDIR(opened.st_mode)
        ):
            os.close(descriptor)
            raise PublicBaselineActivationError(
                "public baseline cleanup child changed"
            )
        return descriptor

    def _remove_cleanup_tombstone(
        self,
        journal: Mapping[str, object],
    ) -> None:
        expected_dev = cast(int, journal["cleanup_dev"])
        expected_ino = cast(int, journal["cleanup_ino"])
        try:
            workspace = os.open(
                self.workspace_root,
                os.O_RDONLY | _DIRECTORY | _NOFOLLOW,
            )
        except OSError as exc:
            raise PublicBaselineActivationError(
                "public baseline cleanup workspace is unavailable"
            ) from exc
        try:
            metadata = os.stat(
                self.cleanup_root.name,
                dir_fd=workspace,
                follow_symlinks=False,
            )
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != self.expected_uid
                or stat.S_IMODE(metadata.st_mode) != _CLEANUP_ROOT_MODE
                or metadata.st_dev != expected_dev
                or metadata.st_ino != expected_ino
                or metadata.st_dev != self.active_root.stat().st_dev
            ):
                raise PublicBaselineActivationError(
                    "public baseline cleanup tombstone changed"
                )
            root = self._open_cleanup_child(
                workspace,
                self.cleanup_root.name,
                metadata,
            )
            try:
                self._remove_cleanup_contents_fd(
                    root,
                    root_device=expected_dev,
                    relative=Path(),
                )
            finally:
                os.close(root)
            current = os.stat(
                self.cleanup_root.name,
                dir_fd=workspace,
                follow_symlinks=False,
            )
            if (
                current.st_dev != expected_dev
                or current.st_ino != expected_ino
                or not stat.S_ISDIR(current.st_mode)
            ):
                raise PublicBaselineActivationError(
                    "public baseline cleanup tombstone changed"
                )
            os.rmdir(self.cleanup_root.name, dir_fd=workspace)
            os.fsync(workspace)
        except PublicBaselineActivationError:
            raise
        except OSError as exc:
            raise PublicBaselineActivationError(
                "public baseline cleanup deletion was interrupted"
            ) from exc
        finally:
            os.close(workspace)

    def _remove_cleanup_contents_fd(
        self,
        descriptor: int,
        *,
        root_device: int,
        relative: Path,
    ) -> None:
        allowed_owners = {self.expected_uid, self.expected_operator_uid}
        try:
            names = os.listdir(descriptor)
        except OSError as exc:
            raise PublicBaselineActivationError(
                "public baseline cleanup inventory is unavailable"
            ) from exc
        for name in names:
            if name in {"", ".", ".."} or "/" in name:
                raise PublicBaselineActivationError(
                    "public baseline cleanup entry name is invalid"
                )
            child_relative = relative / name
            if child_relative in _CLEANUP_PROTECTED_PATHS:
                raise PublicBaselineActivationError(
                    "protected runtime state remains in cleanup tombstone"
                )
            try:
                metadata = os.stat(
                    name,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise PublicBaselineActivationError(
                    "public baseline cleanup entry is unavailable"
                ) from exc
            if (
                metadata.st_dev != root_device
                or metadata.st_uid not in allowed_owners
                or stat.S_ISLNK(metadata.st_mode)
            ):
                raise PublicBaselineActivationError(
                    "public baseline cleanup entry metadata is unsafe"
                )
            if stat.S_ISDIR(metadata.st_mode):
                child = self._open_cleanup_child(
                    descriptor,
                    name,
                    metadata,
                )
                try:
                    self._remove_cleanup_contents_fd(
                        child,
                        root_device=root_device,
                        relative=child_relative,
                    )
                finally:
                    os.close(child)
                current = os.stat(
                    name,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
                if (
                    current.st_dev != metadata.st_dev
                    or current.st_ino != metadata.st_ino
                    or not stat.S_ISDIR(current.st_mode)
                ):
                    raise PublicBaselineActivationError(
                        "public baseline cleanup directory changed"
                    )
                os.rmdir(name, dir_fd=descriptor)
            elif (
                stat.S_ISREG(metadata.st_mode)
                and metadata.st_nlink == 1
            ):
                os.unlink(name, dir_fd=descriptor)
            else:
                raise PublicBaselineActivationError(
                    "public baseline cleanup entry type is unsafe"
                )
        os.fsync(descriptor)

    def _prepared(self, request: ActivationRequest) -> PreparedActivation:
        return PreparedActivation(
            activation_id=request.activation_id,
            state=ActivationState.PREPARED.value,
            staged_root=self.staged_root,
            commit=request.target.commit,
            tree=request.target.tree,
        )

    def _target_outcome(
        self,
        request: ActivationRequest,
        state: str,
    ) -> ActivationOutcome:
        return ActivationOutcome(
            activation_id=request.activation_id,
            state=state,
            active_root=self.active_root,
            commit=request.target.commit,
            tree=request.target.tree,
            recovery_root=self.recovery_root,
        )

    def _base_outcome(self, request: ActivationRequest) -> ActivationOutcome:
        return ActivationOutcome(
            activation_id=request.activation_id,
            state=ActivationState.ROLLED_BACK.value,
            active_root=self.active_root,
            commit=request.base.commit,
            tree=request.base.tree,
            recovery_root=self.staged_root,
        )

    def _require_request(self, request: ActivationRequest) -> None:
        if not isinstance(request, ActivationRequest):
            raise PublicBaselineActivationError(
                "activation request type is invalid"
            )
        if request.activation_id != self.activation_id:
            raise PublicBaselineActivationError(
                "activation request ID does not match"
            )

    def _validate_boundaries(self) -> None:
        _require_real_directory(
            self.workspace_root,
            "public baseline workspace",
            expected_uid=self.expected_uid,
            forbid_group_other_write=True,
        )
        _require_real_directory(
            self.artifacts_root,
            "public baseline artifact root",
            expected_uid=self.expected_uid,
            exact_mode=0o700,
        )
        if (
            self.active_root.parent != self.workspace_root
            and self.active_root.stat().st_dev
            != self.workspace_root.stat().st_dev
        ):
            raise PublicBaselineActivationError(
                "active and workspace roots must share a filesystem"
            )
        for path in (
            self.building_root,
            self.staged_root,
            self.recovery_root,
            self.cleanup_root,
            self.journal_path,
        ):
            if path.parent != self.workspace_root:
                raise PublicBaselineActivationError(
                    "public baseline workspace path is invalid"
                )

    def _validate_artifacts(self, request: ActivationRequest) -> None:
        names = [request.bundle.file]
        names.extend(image.file for image in request.images.values())
        if len(names) != len(set(names)):
            raise PublicBaselineActivationError(
                "public baseline artifact names must be unique"
            )
        self._validate_artifact(
            request.bundle.file,
            request.bundle.sha256,
            max_bytes=_MAX_BUNDLE_BYTES,
        )
        for image in request.images.values():
            self._validate_artifact(
                image.file,
                image.sha256,
                max_bytes=_MAX_IMAGE_BYTES,
            )

    def _validate_artifact(
        self,
        name: str,
        expected_sha256: str,
        *,
        max_bytes: int,
    ) -> None:
        flags = os.O_RDONLY | _NOFOLLOW
        directory_descriptor = self._artifact_dir_fd()
        try:
            descriptor = os.open(name, flags, dir_fd=directory_descriptor)
        except OSError as exc:
            os.close(directory_descriptor)
            raise PublicBaselineActivationError(
                "public baseline artifact is unavailable"
            ) from exc
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != self.expected_uid
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1
                or not 0 < metadata.st_size <= max_bytes
            ):
                raise PublicBaselineActivationError(
                    "public baseline artifact metadata is unsafe"
                )
            digest = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            after = os.fstat(descriptor)
            if (
                (metadata.st_dev, metadata.st_ino, metadata.st_size)
                != (after.st_dev, after.st_ino, after.st_size)
                or digest.hexdigest() != expected_sha256
            ):
                raise PublicBaselineActivationError(
                    "public baseline artifact digest is invalid"
                )
        finally:
            os.close(descriptor)
            os.close(directory_descriptor)

    def _artifact_dir_fd(self) -> int:
        try:
            return os.open(
                self.artifacts_root,
                os.O_RDONLY | _DIRECTORY | _NOFOLLOW,
            )
        except OSError as exc:
            raise PublicBaselineActivationError(
                "public baseline artifact root is unavailable"
            ) from exc

    def _validate_base_root(
        self,
        request: ActivationRequest,
        *,
        require_persistence: bool = True,
    ) -> None:
        self._validate_active_root_profile(self.active_root)
        self._validate_base_identity(self.active_root, request.base)
        self._require_single_worktree(self.active_root)
        if self._git(
            self.active_root,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        ):
            raise PublicBaselineActivationError(
                "active root contains uncommitted changes"
            )
        self._validate_tracked_owners(self.active_root)
        self._validate_ignored_inventory(self.active_root)
        if require_persistence:
            self._validate_persistence(
                self.active_root,
                active_profile=True,
            )
        self._reject_alternates(self.active_root)

    def _validate_active_root_profile(self, root: Path) -> None:
        try:
            metadata = root.lstat()
        except OSError as exc:
            raise PublicBaselineActivationError(
                "active root metadata is unavailable"
            ) from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != self.expected_uid
            or metadata.st_gid != self.expected_operator_gid
            or stat.S_IMODE(metadata.st_mode) != _ACTIVE_ROOT_MODE
        ):
            raise PublicBaselineActivationError(
                "active root ownership or mode is invalid"
            )
        for name in ("backend", "deploy"):
            self._validate_operator_directory(
                root / name,
                expected_device=metadata.st_dev,
                context="active checkout directory",
            )

    def _has_active_root_profile(self, root: Path) -> bool:
        try:
            self._validate_active_root_profile(root)
        except PublicBaselineActivationError:
            return False
        return True

    def _validate_operator_directory(
        self,
        path: Path,
        *,
        expected_device: int,
        context: str,
    ) -> None:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise PublicBaselineActivationError(
                f"{context} is unavailable"
            ) from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != self.expected_operator_uid
            or metadata.st_gid != self.expected_operator_gid
            or stat.S_IMODE(metadata.st_mode) != _OPERATOR_DIRECTORY_MODE
            or metadata.st_dev != expected_device
        ):
            raise PublicBaselineActivationError(
                f"{context} ownership or mode is invalid"
            )

    def _validate_staging_root_profile(self, root: Path) -> None:
        _require_real_directory(
            root,
            "public target root",
            expected_uid=self.expected_uid,
            forbid_group_other_write=True,
        )
        device = root.stat().st_dev
        for name in ("backend", "deploy"):
            _require_real_directory(
                root / name,
                "public target checkout directory",
                expected_uid=self.expected_uid,
                forbid_group_other_write=True,
            )
            if (root / name).stat().st_dev != device:
                raise PublicBaselineActivationError(
                    "public target checkout crosses a filesystem"
                )

    def _validate_tracked_owners(self, root: Path) -> None:
        allowed_owners = {self.expected_uid, self.expected_operator_uid}
        raw = self._git(root, "ls-files", "-z")
        paths: set[Path] = set()
        for name in raw.split("\0"):
            if not name:
                continue
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts:
                raise PublicBaselineActivationError(
                    "tracked checkout path is invalid"
                )
            paths.add(relative)
            paths.update(
                parent for parent in relative.parents if parent != Path(".")
            )
        for relative in sorted(paths, key=lambda item: item.as_posix()):
            try:
                metadata = (root / relative).lstat()
            except OSError as exc:
                raise PublicBaselineActivationError(
                    "tracked checkout entry is unavailable"
                ) from exc
            if metadata.st_uid not in allowed_owners:
                raise PublicBaselineActivationError(
                    "tracked checkout owner is invalid"
                )

    def _apply_active_root_profile(self, root: Path) -> None:
        if not self._has_active_root_profile(root):
            self._validate_staging_root_profile(root)
            try:
                os.chown(
                    root,
                    self.expected_uid,
                    self.expected_operator_gid,
                    follow_symlinks=False,
                )
                os.chmod(
                    root,
                    _ACTIVE_ROOT_MODE,
                    follow_symlinks=False,
                )
                for name in ("backend", "deploy"):
                    directory = root / name
                    os.chown(
                        directory,
                        self.expected_operator_uid,
                        self.expected_operator_gid,
                        follow_symlinks=False,
                    )
                    os.chmod(
                        directory,
                        _OPERATOR_DIRECTORY_MODE,
                        follow_symlinks=False,
                    )
                    _fsync_directory(directory)
                _fsync_directory(root)
            except OSError as exc:
                raise PublicBaselineActivationError(
                    "public target ownership transition failed"
                ) from exc
        self._normalize_git_metadata_permissions(root)
        self._validate_active_root_profile(root)

    def _normalize_git_metadata_permissions(self, root: Path) -> None:
        """让日常 operator 只读 Git，同时保留 root 的写权限。"""

        git_root = root / ".git"
        try:
            metadata = git_root.lstat()
        except OSError as exc:
            raise PublicBaselineActivationError(
                "public target Git metadata is unavailable"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise PublicBaselineActivationError(
                "public target Git metadata must be a real directory"
            )

        pending = [git_root]
        while pending:
            directory = pending.pop()
            try:
                directory_metadata = directory.lstat()
                if (
                    stat.S_ISLNK(directory_metadata.st_mode)
                    or not stat.S_ISDIR(directory_metadata.st_mode)
                ):
                    raise PublicBaselineActivationError(
                        "public target Git metadata directory is unsafe"
                    )
                os.chown(
                    directory,
                    self.expected_uid,
                    self.expected_operator_gid,
                    follow_symlinks=False,
                )
                os.chmod(
                    directory,
                    (stat.S_IMODE(directory_metadata.st_mode) & 0o700) | 0o050,
                    follow_symlinks=False,
                )
                with os.scandir(directory) as entries:
                    children = list(entries)
            except PublicBaselineActivationError:
                raise
            except OSError as exc:
                raise PublicBaselineActivationError(
                    "public target Git metadata cannot be normalized"
                ) from exc

            for entry in children:
                path = Path(entry.path)
                try:
                    child = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(child.st_mode):
                        raise PublicBaselineActivationError(
                            "public target Git metadata contains a symlink"
                        )
                    if stat.S_ISDIR(child.st_mode):
                        pending.append(path)
                        continue
                    if not stat.S_ISREG(child.st_mode):
                        raise PublicBaselineActivationError(
                            "public target Git metadata contains an unsafe entry"
                        )
                    os.chown(
                        path,
                        self.expected_uid,
                        self.expected_operator_gid,
                        follow_symlinks=False,
                    )
                    os.chmod(
                        path,
                        (stat.S_IMODE(child.st_mode) & 0o700) | 0o040,
                        follow_symlinks=False,
                    )
                except PublicBaselineActivationError:
                    raise
                except OSError as exc:
                    raise PublicBaselineActivationError(
                        "public target Git metadata cannot be normalized"
                    ) from exc
            _fsync_directory(directory)

    def _validate_base_identity(
        self,
        root: Path,
        expected: GitIdentity,
    ) -> None:
        observed = self._git_identity(root)
        if observed != expected:
            raise PublicBaselineActivationError(
                "active root Git identity does not match manifest"
            )

    def _build_standalone_target(self, request: ActivationRequest) -> None:
        self._run_checked(("git", "init", "--quiet", str(self.building_root)))
        self._git(self.building_root, "config", "core.logAllRefUpdates", "false")
        self._git(
            self.building_root,
            "remote",
            "add",
            "--track",
            "main",
            "origin",
            request.origin_url,
        )
        bundle_path = self.artifacts_root / request.bundle.file
        self._git(self.building_root, "bundle", "verify", str(bundle_path))
        listed = self._git(
            self.building_root,
            "bundle",
            "list-heads",
            str(bundle_path),
        ).splitlines()
        if listed != [f"{request.target.commit} {PUBLIC_BUNDLE_REF}"]:
            raise PublicBaselineActivationError(
                "public Git bundle must contain exactly one main ref"
            )
        self._git(
            self.building_root,
            "fetch",
            "--no-tags",
            "--no-write-fetch-head",
            str(bundle_path),
            f"+{PUBLIC_BUNDLE_REF}:{PUBLIC_REMOTE_REF}",
        )
        # Git applies the process umask when it materializes tracked files.
        # The test host intentionally uses a restrictive 0027 umask, but the
        # public tree's recorded 100644/100755 modes are part of the source
        # contract (the vendor-control unit preflight depends on this).  Use
        # the standard non-writable 0022 mask for checkout only so the
        # resulting worktree preserves those modes without widening any
        # persistent runtime paths.
        previous_umask = os.umask(0o022)
        try:
            self._git(
                self.building_root,
                "checkout",
                "--quiet",
                "--detach",
                request.target.commit,
            )
        finally:
            os.umask(previous_umask)
        self._validate_artifact(
            request.bundle.file,
            request.bundle.sha256,
            max_bytes=_MAX_BUNDLE_BYTES,
        )
        self._validate_target_root(self.building_root, request)

    def _validate_target_root(
        self,
        root: Path,
        request: ActivationRequest,
        *,
        allow_persistence: bool = False,
        active_profile: bool = False,
    ) -> None:
        if active_profile:
            self._validate_active_root_profile(root)
        else:
            self._validate_staging_root_profile(root)
        if self._git_identity(root) != request.target:
            raise PublicBaselineActivationError(
                "public target Git identity does not match manifest"
            )
        if self._git(root, "symbolic-ref", "-q", "HEAD", allowed_codes=(0, 1)):
            raise PublicBaselineActivationError(
                "public target HEAD must be detached"
            )
        refs = self._git(
            root,
            "for-each-ref",
            "--format=%(refname)",
        ).splitlines()
        if refs != [PUBLIC_REMOTE_REF]:
            raise PublicBaselineActivationError(
                "public target refs are invalid"
            )
        if self._git(root, "remote").splitlines() != ["origin"]:
            raise PublicBaselineActivationError(
                "public target remote inventory is invalid"
            )
        if self._git(root, "remote", "get-url", "--all", "origin").splitlines() != [
            PUBLIC_ORIGIN_URL
        ]:
            raise PublicBaselineActivationError(
                "public target origin URL is invalid"
            )
        fetch_specs = self._git(
            root,
            "config",
            "--get-all",
            "remote.origin.fetch",
        ).splitlines()
        if fetch_specs != [f"+{PUBLIC_BUNDLE_REF}:{PUBLIC_REMOTE_REF}"]:
            raise PublicBaselineActivationError(
                "public target fetch scope is invalid"
            )
        if self._git(
            root,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        ):
            raise PublicBaselineActivationError(
                "public target root is not clean"
            )
        ignored = self._ignored_paths(root)
        if ignored and (
            not allow_persistence
            or any(
                not _path_is_within_allowlist(path, PERSISTENT_PATHS)
                for path in ignored
            )
        ):
            raise PublicBaselineActivationError(
                "public target ignored inventory is invalid"
            )
        self._reject_alternates(root)
        if self._git(root, "rev-parse", "--is-shallow-repository") != "false":
            raise PublicBaselineActivationError(
                "public target repository must not be shallow"
            )
        if self._git(root, "reflog", "show", "--all"):
            raise PublicBaselineActivationError(
                "public target repository contains reflogs"
            )
        logs = root / ".git/logs"
        if _lexists(logs):
            try:
                with os.scandir(logs) as entries:
                    if any(entries):
                        raise PublicBaselineActivationError(
                            "public target repository contains reflogs"
                        )
            except OSError as exc:
                raise PublicBaselineActivationError(
                    "public target reflog inventory is unavailable"
                ) from exc
        fsck = self._run_result(
            (
                "git",
                "-C",
                str(root),
                "fsck",
                "--strict",
                "--no-reflogs",
                "--unreachable",
                "--no-progress",
            )
        )
        if fsck.returncode != 0 or fsck.stdout.strip() or fsck.stderr.strip():
            raise PublicBaselineActivationError(
                "public target repository contains invalid or unreachable objects"
            )
        self._require_single_worktree(root)
        self._validate_tracked_owners(root)
        version_path = root / "VERSION"
        try:
            version = version_path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as exc:
            raise PublicBaselineActivationError(
                "public target version is unavailable"
            ) from exc
        versions = {image.version for image in request.images.values()}
        if versions != {version}:
            raise PublicBaselineActivationError(
                "public target version does not match image metadata"
            )

    def _require_single_worktree(self, root: Path) -> None:
        raw = self._git(root, "worktree", "list", "--porcelain")
        worktrees = [
            line.removeprefix("worktree ")
            for line in raw.splitlines()
            if line.startswith("worktree ")
        ]
        if len(worktrees) != 1:
            raise PublicBaselineActivationError(
                "Git worktree inventory is invalid"
            )
        try:
            if not os.path.samefile(worktrees[0], root):
                raise PublicBaselineActivationError(
                    "Git worktree root is invalid"
                )
        except OSError as exc:
            raise PublicBaselineActivationError(
                "Git worktree root is unavailable"
            ) from exc

    def _validate_ignored_inventory(self, root: Path) -> None:
        allowed = PERSISTENT_PATHS + DISCARDABLE_CACHE_PATHS
        if any(
            not _path_is_within_allowlist(path, allowed)
            for path in self._ignored_paths(root)
        ):
            raise PublicBaselineActivationError(
                "active root contains a non-allowlisted ignored path"
            )

    def _ignored_paths(self, root: Path) -> tuple[Path, ...]:
        raw = self._git(
            root,
            "status",
            "--porcelain=v1",
            "-z",
            "--ignored=matching",
            "--untracked-files=all",
        )
        if not raw:
            return ()
        paths: list[Path] = []
        for record in raw.split("\0"):
            if not record:
                continue
            if not record.startswith("!! "):
                raise PublicBaselineActivationError(
                    "Git ignored inventory is invalid"
                )
            relative = Path(record[3:].rstrip("/"))
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or str(relative) in {"", "."}
            ):
                raise PublicBaselineActivationError(
                    "Git ignored path is invalid"
                )
            paths.append(relative)
        return tuple(paths)

    def _validate_persistence(
        self,
        root: Path,
        *,
        active_profile: bool,
        require_virtualenv: bool = False,
    ) -> None:
        if active_profile:
            self._validate_active_root_profile(root)
        else:
            self._validate_staging_root_profile(root)
        device = root.stat().st_dev
        self._validate_dotenv(root / ".env")
        self._validate_secrets(root / "deploy/secrets")
        virtualenv = root / "backend/.venv"
        if _lexists(virtualenv):
            self._validate_virtualenv(
                virtualenv,
                expected_device=device,
            )
        elif require_virtualenv:
            raise PublicBaselineActivationError(
                "persistent virtual environment is unavailable"
            )

    def _validate_dotenv(self, path: Path) -> None:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise PublicBaselineActivationError(
                "persistent environment file is unavailable"
            ) from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != self.expected_uid
            or metadata.st_gid != self.expected_operator_gid
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or metadata.st_size <= 0
        ):
            raise PublicBaselineActivationError(
                "persistent environment file metadata is unsafe"
            )

    def _validate_secrets(self, path: Path) -> None:
        try:
            directory_metadata = path.lstat()
        except OSError as exc:
            raise PublicBaselineActivationError(
                "persistent secrets are unavailable"
            ) from exc
        if (
            stat.S_ISLNK(directory_metadata.st_mode)
            or not stat.S_ISDIR(directory_metadata.st_mode)
            or directory_metadata.st_uid != self.expected_uid
            or directory_metadata.st_gid != self.expected_operator_gid
            or stat.S_IMODE(directory_metadata.st_mode) != 0o700
        ):
            raise PublicBaselineActivationError(
                "persistent secrets metadata is unsafe"
            )
        try:
            entries = list(os.scandir(path))
        except OSError as exc:
            raise PublicBaselineActivationError(
                "persistent secrets inventory is unavailable"
            ) from exc
        if not entries:
            raise PublicBaselineActivationError(
                "persistent secrets inventory is empty"
            )
        for entry in entries:
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise PublicBaselineActivationError(
                    "persistent secret metadata is unavailable"
                ) from exc
            if (
                entry.is_symlink()
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != self.expected_uid
                or metadata.st_gid != self.expected_system_gid
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1
                or metadata.st_size <= 0
            ):
                raise PublicBaselineActivationError(
                    "persistent secret metadata is unsafe"
                )

    def _validate_virtualenv(
        self,
        path: Path,
        *,
        expected_device: int,
    ) -> None:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise PublicBaselineActivationError(
                "persistent virtual environment is unavailable"
            ) from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != self.expected_operator_uid
            or metadata.st_gid != self.expected_operator_gid
            or stat.S_IMODE(metadata.st_mode) != _OPERATOR_DIRECTORY_MODE
            or metadata.st_dev != expected_device
        ):
            raise PublicBaselineActivationError(
                "persistent virtual environment metadata is unsafe"
            )

    def _move_persistence_to_target(
        self,
        request: ActivationRequest,
        journal: dict[str, object],
    ) -> None:
        self._validate_base_root(request, require_persistence=False)
        moved = cast(list[str], journal["moved_persistence"])
        for relative in PERSISTENT_PATHS:
            source = self.active_root / relative
            target = self.staged_root / relative
            source_exists = _lexists(source)
            target_exists = _lexists(target)
            if source_exists and target_exists:
                raise PublicBaselineActivationError(
                    "persistent path exists in both roots"
                )
            if not source_exists and not target_exists:
                if relative == Path("backend/.venv"):
                    continue
                raise PublicBaselineActivationError(
                    "required persistent path is unavailable"
                )
            present = source if source_exists else target
            self._validate_persistent_entry(relative, present)
            if source_exists:
                _require_real_directory(
                    target.parent,
                    "persistent target parent",
                    expected_uid=self.expected_uid,
                    forbid_group_other_write=True,
                )
                os.rename(source, target)
                _fsync_directory(source.parent)
                _fsync_directory(target.parent)
            if relative.as_posix() not in moved:
                moved.append(relative.as_posix())
            journal["state"] = ActivationState.PERSISTENCE_MOVED.value
            self._write_journal(journal)
        self._validate_persistence(
            self.staged_root,
            active_profile=self._has_active_root_profile(self.staged_root),
        )

    def _validate_persistent_entry(self, relative: Path, path: Path) -> None:
        if relative == Path(".env"):
            self._validate_dotenv(path)
        elif relative == Path("deploy/secrets"):
            self._validate_secrets(path)
        elif relative == Path("backend/.venv"):
            self._validate_virtualenv(
                path,
                expected_device=self.active_root.stat().st_dev,
            )
        else:
            raise PublicBaselineActivationError(
                "persistent path is not allowlisted"
            )

    def _restore_persistence_to_base(
        self,
        request: ActivationRequest,
    ) -> None:
        for relative in PERSISTENT_PATHS:
            source = self.staged_root / relative
            target = self.active_root / relative
            source_exists = _lexists(source)
            target_exists = _lexists(target)
            if source_exists and target_exists:
                raise PublicBaselineActivationError(
                    "rollback persistent path exists in both roots"
                )
            if source_exists:
                self._validate_active_persistence_parent(
                    relative,
                    target.parent,
                )
                os.rename(source, target)
                _fsync_directory(source.parent)
                _fsync_directory(target.parent)
        self._validate_base_identity(self.active_root, request.base)

    def _validate_active_persistence_parent(
        self,
        relative: Path,
        parent: Path,
    ) -> None:
        if relative == Path(".env"):
            self._validate_active_root_profile(parent)
            return
        self._validate_operator_directory(
            parent,
            expected_device=self.active_root.stat().st_dev,
            context="rollback persistent target parent",
        )

    def _arrangement(self, request: ActivationRequest) -> str:
        active = self._identity_if_repository(self.active_root)
        staged = self._identity_if_repository(self.staged_root)
        recovery = self._identity_if_repository(self.recovery_root)
        building = self._identity_if_repository(self.building_root)
        if active == request.target and recovery == request.base and staged is None:
            return "applied"
        if active == request.target and staged == request.base and recovery is None:
            return "exchanged"
        if active == request.base and staged == request.target and recovery is None:
            return "before_exchange"
        if (
            active == request.base
            and staged is None
            and recovery is None
            and building in {None, request.target}
        ):
            return "building"
        raise PublicBaselineActivationError(
            "public baseline roots do not match a recoverable arrangement"
        )

    def _identity_if_repository(self, root: Path) -> GitIdentity | None:
        if not _lexists(root):
            return None
        _require_real_directory(root, "activation root")
        return self._git_identity(root)

    def _git_identity(self, root: Path) -> GitIdentity:
        commit = self._git(root, "rev-parse", "--verify", "HEAD^{commit}")
        tree = self._git(root, "rev-parse", "--verify", "HEAD^{tree}")
        if _COMMIT_RE.fullmatch(commit) is None or _COMMIT_RE.fullmatch(tree) is None:
            raise PublicBaselineActivationError(
                "Git identity output is invalid"
            )
        return GitIdentity(commit=commit, tree=tree)

    def _reject_alternates(self, root: Path) -> None:
        if _lexists(root / ".git/objects/info/alternates"):
            raise PublicBaselineActivationError(
                "Git object alternates are forbidden"
            )

    def _git(
        self,
        root: Path,
        *arguments: str,
        allowed_codes: tuple[int, ...] = (0,),
    ) -> str:
        result = self._run_result(("git", "-C", str(root), *arguments))
        if result.returncode not in allowed_codes:
            raise PublicBaselineActivationError(
                "public baseline Git command failed"
            )
        return result.stdout.rstrip("\n")

    def _run_checked(self, argv: Sequence[str]) -> str:
        result = self._run_result(argv)
        if result.returncode != 0:
            raise PublicBaselineActivationError(
                "public baseline command failed"
            )
        return result.stdout.rstrip("\n")

    def _run_result(self, argv: Sequence[str]) -> CommandResult:
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        environment.update(
            {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "LC_ALL": "C",
            }
        )
        return self.runner.run(argv, environment=environment)

    def _new_journal(
        self,
        request: ActivationRequest,
        state: ActivationState,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "activation_id": request.activation_id,
            "request_sha256": request.fingerprint,
            "state": state.value,
            "base": {
                "commit": request.base.commit,
                "tree": request.base.tree,
            },
            "target": {
                "commit": request.target.commit,
                "tree": request.target.tree,
            },
            "version": next(iter(request.images.values())).version,
            "building_root": str(self.building_root),
            "staged_root": str(self.staged_root),
            "recovery_root": str(self.recovery_root),
            "cleanup_root": str(self.cleanup_root),
            "cleanup_dev": None,
            "cleanup_ino": None,
            "moved_persistence": [],
        }

    def _write_journal(
        self,
        journal: Mapping[str, object],
        *,
        must_not_exist: bool = False,
    ) -> None:
        self._validate_journal(journal)
        payload = (
            json.dumps(
                journal,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("ascii")
        temporary = self.journal_path.with_name(
            f".{self.journal_path.name}.tmp-{os.getpid()}"
        )
        if must_not_exist and _lexists(self.journal_path):
            raise PublicBaselineActivationError(
                "public baseline journal already exists"
            )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW
        try:
            descriptor = os.open(temporary, flags, 0o600)
        except OSError as exc:
            raise PublicBaselineActivationError(
                "public baseline journal cannot be created"
            ) from exc
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.journal_path)
            _fsync_directory(self.workspace_root)
        finally:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)

    def _read_journal(
        self,
        request: ActivationRequest | None = None,
    ) -> dict[str, object]:
        flags = os.O_RDONLY | _NOFOLLOW
        try:
            descriptor = os.open(self.journal_path, flags)
        except OSError as exc:
            raise PublicBaselineActivationError(
                "public baseline journal is unavailable"
            ) from exc
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != self.expected_uid
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or not 0 < metadata.st_size <= _MAX_JOURNAL_BYTES
            ):
                raise PublicBaselineActivationError(
                    "public baseline journal metadata is unsafe"
                )
            chunks: list[bytes] = []
            remaining = metadata.st_size
            while remaining:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) != metadata.st_size:
                raise PublicBaselineActivationError(
                    "public baseline journal changed while reading"
                )
        finally:
            os.close(descriptor)
        try:
            value = json.loads(
                raw.decode("ascii"),
                object_pairs_hook=_reject_duplicate_keys,
            )
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise PublicBaselineActivationError(
                "public baseline journal is invalid"
            ) from exc
        if type(value) is not dict:
            raise PublicBaselineActivationError(
                "public baseline journal is invalid"
            )
        journal = cast(dict[str, object], value)
        self._validate_journal(journal)
        if request is not None and journal["request_sha256"] != request.fingerprint:
            raise PublicBaselineActivationError(
                "public baseline journal request does not match"
            )
        return journal

    def _validate_journal(self, journal: Mapping[str, object]) -> None:
        _require_exact_fields(journal, _JOURNAL_FIELDS, "journal")
        request_sha256 = journal["request_sha256"]
        if (
            journal["schema_version"] != 1
            or journal["activation_id"] != self.activation_id
            or type(request_sha256) is not str
            or _SHA256_RE.fullmatch(request_sha256) is None
        ):
            raise PublicBaselineActivationError(
                "public baseline journal identity is invalid"
            )
        try:
            ActivationState(cast(str, journal["state"]))
        except (TypeError, ValueError) as exc:
            raise PublicBaselineActivationError(
                "public baseline journal state is invalid"
            ) from exc
        _parse_git_identity(journal["base"], "journal base")
        _parse_git_identity(journal["target"], "journal target")
        _require_matching_string(
            journal["version"], _VERSION_RE, "journal version"
        )
        expected_paths = {
            "building_root": str(self.building_root),
            "staged_root": str(self.staged_root),
            "recovery_root": str(self.recovery_root),
            "cleanup_root": str(self.cleanup_root),
        }
        if any(journal[key] != value for key, value in expected_paths.items()):
            raise PublicBaselineActivationError(
                "public baseline journal paths are invalid"
            )
        state = ActivationState(cast(str, journal["state"]))
        cleanup_dev = journal["cleanup_dev"]
        cleanup_ino = journal["cleanup_ino"]
        if state in {ActivationState.FINALIZING, ActivationState.CLEANED}:
            if (
                type(cleanup_dev) is not int
                or cleanup_dev <= 0
                or type(cleanup_ino) is not int
                or cleanup_ino <= 0
            ):
                raise PublicBaselineActivationError(
                    "public baseline cleanup identity is invalid"
                )
        elif cleanup_dev is not None or cleanup_ino is not None:
            raise PublicBaselineActivationError(
                "public baseline cleanup identity is invalid"
            )
        moved = journal["moved_persistence"]
        if type(moved) is not list or any(
            type(item) is not str
            or Path(item) not in PERSISTENT_PATHS
            for item in cast(list[object], moved)
        ):
            raise PublicBaselineActivationError(
                "public baseline journal persistence is invalid"
            )
        if len(cast(list[object], moved)) != len(set(cast(list[str], moved))):
            raise PublicBaselineActivationError(
                "public baseline journal persistence is invalid"
            )

    def _request_from_journal(
        self,
        journal: Mapping[str, object],
    ) -> ActivationRequest:
        base = _parse_git_identity(journal["base"], "journal base")
        target = _parse_git_identity(journal["target"], "journal target")
        # rollback/finalize only need immutable Git identities. Placeholder
        # evidence cannot authorize artifact loading or a new prepare/apply.
        placeholder_image = ImageEvidence(
            file="placeholder.tar",
            sha256="0" * 64,
            ref=f"sms-platform-test-api:{target.commit}",
            image_id=f"sha256:{'0' * 64}",
            version=cast(str, journal["version"]),
            revision=target.commit,
            schema_revision=MIGRATION_HEAD,
        )
        return ActivationRequest(
            schema_version=1,
            activation_id=self.activation_id,
            origin_url=PUBLIC_ORIGIN_URL,
            base=base,
            target=target,
            bundle=BundleEvidence(
                file="placeholder.bundle",
                sha256="0" * 64,
                ref=PUBLIC_BUNDLE_REF,
            ),
            images={"api": placeholder_image, "web": placeholder_image},
            migration=MigrationEvidence(
                migration_from=MIGRATION_HEAD,
                target=MIGRATION_HEAD,
                compatibility="none",
            ),
        )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise PublicBaselineActivationError(
                "JSON contains a duplicate field"
            )
        value[key] = item
    return value


def _require_exact_fields(
    value: Mapping[str, object],
    expected: frozenset[str],
    context: str,
) -> None:
    if set(value) != set(expected):
        raise PublicBaselineActivationError(f"{context} fields are invalid")


def _require_matching_string(
    value: object,
    pattern: re.Pattern[str],
    context: str,
) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise PublicBaselineActivationError(f"{context} is invalid")
    return value


def _require_mapping(value: object, context: str) -> dict[str, object]:
    if type(value) is not dict:
        raise PublicBaselineActivationError(f"{context} must be an object")
    return cast(dict[str, object], value)


def _parse_git_identity(value: object, context: str) -> GitIdentity:
    document = _require_mapping(value, context)
    _require_exact_fields(document, _GIT_FIELDS, context)
    return GitIdentity(
        commit=_require_matching_string(
            document["commit"], _COMMIT_RE, f"{context} commit"
        ),
        tree=_require_matching_string(
            document["tree"], _COMMIT_RE, f"{context} tree"
        ),
    )


def _parse_bundle(value: object) -> BundleEvidence:
    document = _require_mapping(value, "bundle")
    _require_exact_fields(document, _BUNDLE_FIELDS, "bundle")
    file_name = _require_artifact_name(document["file"], ".bundle", "bundle")
    if file_name != "public-baseline.bundle":
        raise PublicBaselineActivationError("public bundle file is invalid")
    digest = _require_matching_string(
        document["sha256"], _SHA256_RE, "bundle SHA-256"
    )
    if document["ref"] != PUBLIC_BUNDLE_REF:
        raise PublicBaselineActivationError("public bundle ref is invalid")
    return BundleEvidence(
        file=file_name,
        sha256=digest,
        ref=PUBLIC_BUNDLE_REF,
    )


def _parse_images(
    value: object,
    target_commit: str,
) -> Mapping[str, ImageEvidence]:
    images = _require_mapping(value, "images")
    _require_exact_fields(images, frozenset({"api", "web"}), "images")
    parsed: dict[str, ImageEvidence] = {}
    for component in ("api", "web"):
        document = _require_mapping(images[component], f"image {component}")
        _require_exact_fields(document, _IMAGE_FIELDS, f"image {component}")
        file_name = _require_artifact_name(
            document["file"], ".tar", f"image {component}"
        )
        if file_name != f"{component}.tar":
            raise PublicBaselineActivationError(
                f"image {component} file is invalid"
            )
        digest = _require_matching_string(
            document["sha256"], _SHA256_RE, f"image {component} SHA-256"
        )
        expected_ref = f"sms-platform-test-{component}:{target_commit}"
        if document["ref"] != expected_ref:
            raise PublicBaselineActivationError(
                f"image {component} ref is invalid"
            )
        image_id = _require_matching_string(
            document["id"], _IMAGE_ID_RE, f"image {component} ID"
        )
        version = _require_matching_string(
            document["version"], _VERSION_RE, f"image {component} version"
        )
        if document["revision"] != target_commit:
            raise PublicBaselineActivationError(
                f"image {component} revision is invalid"
            )
        if document["schema_revision"] != MIGRATION_HEAD:
            raise PublicBaselineActivationError(
                f"image {component} schema revision is invalid"
            )
        parsed[component] = ImageEvidence(
            file=file_name,
            sha256=digest,
            ref=expected_ref,
            image_id=image_id,
            version=version,
            revision=target_commit,
            schema_revision=MIGRATION_HEAD,
        )
    if parsed["api"].file == parsed["web"].file:
        raise PublicBaselineActivationError(
            "image archive names must be unique"
        )
    if parsed["api"].image_id == parsed["web"].image_id:
        raise PublicBaselineActivationError("image IDs must be unique")
    if parsed["api"].version != parsed["web"].version:
        raise PublicBaselineActivationError(
            "image versions must match"
        )
    return parsed


def _parse_migration(value: object) -> MigrationEvidence:
    document = _require_mapping(value, "migration")
    _require_exact_fields(document, _MIGRATION_FIELDS, "migration")
    if (
        document["from"] != MIGRATION_HEAD
        or document["target"] != MIGRATION_HEAD
        or document["compatibility"] != "none"
    ):
        raise PublicBaselineActivationError(
            "public baseline migration evidence is invalid"
        )
    return MigrationEvidence(
        migration_from=MIGRATION_HEAD,
        target=MIGRATION_HEAD,
        compatibility="none",
    )


def _require_artifact_name(
    value: object,
    suffix: str,
    context: str,
) -> str:
    name = _require_matching_string(value, _ARCHIVE_RE, f"{context} file")
    if Path(name).name != name or not name.endswith(suffix):
        raise PublicBaselineActivationError(f"{context} file is invalid")
    return name


def _require_absolute_path(path: Path, context: str) -> Path:
    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or ".." in path.parts
        or path == Path("/")
    ):
        raise PublicBaselineActivationError(f"{context} path is invalid")
    return path


def _require_real_directory(
    path: Path,
    context: str,
    *,
    expected_uid: int | None = None,
    exact_mode: int | None = None,
    forbid_group_other_write: bool = False,
) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PublicBaselineActivationError(
            f"{context} is unavailable"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise PublicBaselineActivationError(
            f"{context} must be a real directory"
        )
    if expected_uid is not None and metadata.st_uid != expected_uid:
        raise PublicBaselineActivationError(f"{context} owner is invalid")
    if exact_mode is not None and stat.S_IMODE(metadata.st_mode) != exact_mode:
        raise PublicBaselineActivationError(f"{context} mode is invalid")
    if forbid_group_other_write and stat.S_IMODE(metadata.st_mode) & 0o022:
        raise PublicBaselineActivationError(f"{context} mode is unsafe")


def _path_is_within_allowlist(
    path: Path,
    allowed: Sequence[Path],
) -> bool:
    return any(path == prefix or prefix in path.parents for prefix in allowed)


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _paths_overlap(left: Path, right: Path) -> bool:
    return _is_within(left, right) or _is_within(right, left)


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | _DIRECTORY)
    except OSError as exc:
        raise PublicBaselineActivationError(
            "public baseline directory cannot be synchronized"
        ) from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise PublicBaselineActivationError(
            "public baseline directory cannot be synchronized"
        ) from exc
    finally:
        os.close(descriptor)
