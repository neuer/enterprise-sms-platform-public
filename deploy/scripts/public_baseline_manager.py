#!/usr/bin/env python3
"""公开基线三端对齐的失败关闭编排器。

本模块只编排公开 bundle 激活核心与既有 test-update 状态机。Git 根目录交换由
``public_baseline_activation`` 负责；数据库、Docker volume 和 secrets 从不复制、
删除或初始化。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import json
import os
import re
import stat
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

from test_update_apply import TestUpdateApply
from test_update_backup import require_inherited_lifecycle_lock
from test_update_contract import ChangedScope, TestUpdateRequest
from test_update_manager import (
    FixedCommandRunner,
    HostTestUpdateOperations,
    TestUpdateManager,
    TestUpdateManagerError,
    _read_private_request,
)
from test_update_store import TestUpdateState, TestUpdateStore
from test_update_verify import TestUpdateVerify

ACTIVE_ROOT = Path("/opt/sms-platform")
RUNTIME_ROOT = Path("/run/sms-platform/secrets")
STATE_ROOT = Path("/var/lib/sms-platform/test-updates")
INCOMING_ROOT = STATE_ROOT / "incoming"
MANIFEST_PATH = INCOMING_ROOT / "baseline-manifest.json"
REQUEST_PATH = INCOMING_ROOT / "request.json"
MARKER_PATH = Path("/etc/sms-platform/test-environment")
VENDOR_UNIT_PATH = Path("/etc/systemd/system/vendor-control-agent.service")
VENDOR_UNIT_SOURCE = Path("deploy/systemd/vendor-control-agent.service")
PUBLIC_ORIGIN_URL = "https://github.com/neuer/enterprise-sms-platform-public.git"
PUBLIC_MAIN_REF = "refs/heads/main"
SCHEMA_REVISION = "0039_manual_job_outbox"
COMPONENTS = frozenset({"api", "web"})
OPERATOR_UID = 1000
OPERATOR_GID = 1000
SYSTEM_GID = 0

_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
_VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_ACTIVATION_ID_RE = re.compile(r"test-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}")
_TOP_LEVEL_FIELDS = frozenset(
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
_IDENTITY_FIELDS = frozenset({"commit", "tree"})
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
class PublicBaselineManagerError(RuntimeError):
    """公开基线不满足激活或回退安全契约。"""


@dataclass(frozen=True, slots=True)
class GitIdentity:
    commit: str
    tree: str


@dataclass(frozen=True, slots=True)
class BaselineImage:
    archive_file: str
    archive_sha256: str
    ref: str
    image_id: str
    version: str
    revision: str
    schema_revision: str


@dataclass(frozen=True, slots=True)
class BaselineManifest:
    activation_id: str
    origin_url: str
    base: GitIdentity
    target: GitIdentity
    bundle_file: str
    bundle_sha256: str
    bundle_ref: str
    images: Mapping[str, BaselineImage]
    migration_from: str
    migration_target: str
    migration_compatibility: str


class PreparedActivation(Protocol):
    activation_id: str
    staged_root: Path
    commit: str
    tree: str


class ActivationOutcome(Protocol):
    activation_id: str
    state: str
    active_root: Path
    recovery_root: Path
    commit: str
    tree: str


class ActivationCore(Protocol):
    def prepare(self, request: object) -> PreparedActivation: ...

    def activate(self, request: object) -> ActivationOutcome: ...

    def rollback(self) -> ActivationOutcome: ...

    def finalize(self) -> ActivationOutcome: ...

    def cleanup(self) -> ActivationOutcome: ...


class SourceInspector(Protocol):
    def verify(
        self,
        root: Path,
        *,
        identity: GitIdentity,
        origin_url: str,
    ) -> None: ...

    def observe(self, root: Path) -> GitIdentity: ...


class ImageInspector(Protocol):
    def verify(
        self,
        manifest: BaselineManifest,
    ) -> None: ...


class UnitManager(Protocol):
    def preflight(self, active_root: Path, staged_root: Path) -> None: ...

    def activate(self, outcome: ActivationOutcome) -> None: ...

    def restore(self, outcome: ActivationOutcome) -> None: ...

    def verify(self, active_root: Path) -> None: ...


class UpdateOperationsFactory(Protocol):
    def __call__(
        self,
        root: Path,
        request: TestUpdateRequest,
    ) -> HostTestUpdateOperations: ...


def _reject_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PublicBaselineManagerError("baseline manifest has duplicate fields")
        result[key] = value
    return result


def _exact_mapping(
    value: object,
    fields: frozenset[str],
    context: str,
) -> dict[str, object]:
    if type(value) is not dict or set(value) != set(fields):
        raise PublicBaselineManagerError(f"{context} fields are invalid")
    return cast(dict[str, object], value)


def _string(
    value: object,
    pattern: re.Pattern[str],
    context: str,
) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise PublicBaselineManagerError(f"{context} is invalid")
    return value


def parse_baseline_manifest(raw: bytes) -> BaselineManifest:
    """严格解析公开基线 manifest；拒绝重复字段、扩展字段和宽松值。"""

    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicates,
        )
    except PublicBaselineManagerError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PublicBaselineManagerError("baseline manifest is invalid") from exc
    top = _exact_mapping(document, _TOP_LEVEL_FIELDS, "baseline manifest")
    if top["schema_version"] != 1:
        raise PublicBaselineManagerError("baseline manifest version is invalid")
    activation_id = _string(
        top["activation_id"],
        _ACTIVATION_ID_RE,
        "activation ID",
    )
    if top["origin_url"] != PUBLIC_ORIGIN_URL:
        raise PublicBaselineManagerError("baseline origin is invalid")

    def identity(value: object, context: str) -> GitIdentity:
        fields = _exact_mapping(value, _IDENTITY_FIELDS, context)
        return GitIdentity(
            commit=_string(fields["commit"], _COMMIT_RE, f"{context} commit"),
            tree=_string(fields["tree"], _COMMIT_RE, f"{context} tree"),
        )

    base = identity(top["base"], "baseline base")
    target = identity(top["target"], "baseline target")
    if base == target:
        raise PublicBaselineManagerError("baseline target must differ from base")

    bundle = _exact_mapping(top["bundle"], _BUNDLE_FIELDS, "baseline bundle")
    if bundle["file"] != "public-baseline.bundle" or bundle["ref"] != PUBLIC_MAIN_REF:
        raise PublicBaselineManagerError("baseline bundle binding is invalid")
    bundle_sha256 = _string(
        bundle["sha256"],
        _DIGEST_RE,
        "baseline bundle digest",
    )

    image_values = _exact_mapping(top["images"], COMPONENTS, "baseline images")
    images: dict[str, BaselineImage] = {}
    for component in sorted(COMPONENTS):
        value = _exact_mapping(
            image_values[component],
            _IMAGE_FIELDS,
            f"baseline {component} image",
        )
        archive_file = f"{component}.tar"
        ref = f"sms-platform-test-{component}:{target.commit}"
        if value["file"] != archive_file or value["ref"] != ref:
            raise PublicBaselineManagerError(
                f"baseline {component} image binding is invalid"
            )
        image = BaselineImage(
            archive_file=archive_file,
            archive_sha256=_string(
                value["sha256"],
                _DIGEST_RE,
                f"baseline {component} archive digest",
            ),
            ref=ref,
            image_id=_string(
                value["id"],
                _IMAGE_ID_RE,
                f"baseline {component} image ID",
            ),
            version=_string(
                value["version"],
                _VERSION_RE,
                f"baseline {component} version",
            ),
            revision=_string(
                value["revision"],
                _COMMIT_RE,
                f"baseline {component} revision",
            ),
            schema_revision=_string(
                value["schema_revision"],
                re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"),
                f"baseline {component} schema revision",
            ),
        )
        if image.revision != target.commit or image.schema_revision != SCHEMA_REVISION:
            raise PublicBaselineManagerError(
                f"baseline {component} labels are not target-bound"
            )
        images[component] = image

    migration = _exact_mapping(
        top["migration"],
        _MIGRATION_FIELDS,
        "baseline migration",
    )
    if (
        migration["from"] != SCHEMA_REVISION
        or migration["target"] != SCHEMA_REVISION
        or migration["compatibility"] != "none"
    ):
        raise PublicBaselineManagerError("baseline must not contain a migration")
    return BaselineManifest(
        activation_id=activation_id,
        origin_url=PUBLIC_ORIGIN_URL,
        base=base,
        target=target,
        bundle_file="public-baseline.bundle",
        bundle_sha256=bundle_sha256,
        bundle_ref=PUBLIC_MAIN_REF,
        images=images,
        migration_from=SCHEMA_REVISION,
        migration_target=SCHEMA_REVISION,
        migration_compatibility="none",
    )


def _read_private_bytes(
    path: Path,
    *,
    expected_uid: int,
    maximum_size: int = 256 * 1024,
) -> bytes:
    descriptor = -1
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or not 1 <= metadata.st_size <= maximum_size
        ):
            raise PublicBaselineManagerError("baseline manifest is unsafe")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (
            (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != expected_uid
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_size != metadata.st_size
        ):
            raise PublicBaselineManagerError("baseline manifest is unsafe")
        payload = os.read(descriptor, maximum_size + 1)
        if len(payload) != metadata.st_size:
            raise PublicBaselineManagerError("baseline manifest changed while reading")
        return payload
    except PublicBaselineManagerError:
        raise
    except OSError as exc:
        raise PublicBaselineManagerError("baseline manifest is unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _unlink_verified_artifact(
    path: Path,
    *,
    expected_sha256: str,
    expected_uid: int,
    maximum_size: int,
) -> None:
    """仅删除固定 incoming 下内容、身份和摘要均未漂移的普通文件。"""

    if (
        path.parent != INCOMING_ROOT
        or path.name not in {"public-baseline.bundle", "api.tar", "web.tar"}
        or _DIGEST_RE.fullmatch(expected_sha256) is None
        or maximum_size <= 0
    ):
        raise PublicBaselineManagerError(
            "baseline cleanup artifact binding is invalid"
        )
    directory_descriptor = -1
    artifact_descriptor = -1
    try:
        parent = path.parent.lstat()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or stat.S_ISLNK(parent.st_mode)
            or parent.st_uid != expected_uid
            or parent.st_gid != SYSTEM_GID
            or stat.S_IMODE(parent.st_mode) != 0o700
        ):
            raise PublicBaselineManagerError(
                "baseline cleanup directory is unsafe"
            )
        directory_descriptor = os.open(
            path.parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened_parent = os.fstat(directory_descriptor)
        if (
            (opened_parent.st_dev, opened_parent.st_ino)
            != (parent.st_dev, parent.st_ino)
            or not stat.S_ISDIR(opened_parent.st_mode)
            or opened_parent.st_uid != expected_uid
            or opened_parent.st_gid != SYSTEM_GID
            or stat.S_IMODE(opened_parent.st_mode) != 0o700
        ):
            raise PublicBaselineManagerError(
                "baseline cleanup directory changed"
            )
        try:
            metadata = os.stat(
                path.name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or metadata.st_gid != SYSTEM_GID
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or not 1 <= metadata.st_size <= maximum_size
        ):
            raise PublicBaselineManagerError(
                "baseline cleanup artifact is unsafe"
            )
        artifact_descriptor = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_descriptor,
        )
        opened = os.fstat(artifact_descriptor)
        identity = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )
        if (
            (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
            )
            != identity
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != expected_uid
            or opened.st_gid != SYSTEM_GID
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_nlink != 1
        ):
            raise PublicBaselineManagerError(
                "baseline cleanup artifact changed"
            )
        digest = hashlib.sha256()
        while chunk := os.read(artifact_descriptor, 1024 * 1024):
            digest.update(chunk)
        final = os.fstat(artifact_descriptor)
        if (
            (
                final.st_dev,
                final.st_ino,
                final.st_size,
                final.st_mtime_ns,
                final.st_ctime_ns,
            )
            != identity
            or digest.hexdigest() != expected_sha256
        ):
            raise PublicBaselineManagerError(
                "baseline cleanup artifact digest drifted"
            )
        current = os.stat(
            path.name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (current.st_dev, current.st_ino) != (
            metadata.st_dev,
            metadata.st_ino,
        ):
            raise PublicBaselineManagerError(
                "baseline cleanup artifact was replaced"
            )
        os.unlink(path.name, dir_fd=directory_descriptor)
        os.fsync(directory_descriptor)
    except PublicBaselineManagerError:
        raise
    except OSError as exc:
        raise PublicBaselineManagerError(
            "baseline cleanup artifact failed"
        ) from exc
    finally:
        if artifact_descriptor >= 0:
            os.close(artifact_descriptor)
        if directory_descriptor >= 0:
            os.close(directory_descriptor)


def validate_request_binding(
    manifest: BaselineManifest,
    request: TestUpdateRequest,
    *,
    host_source_commit: str,
) -> None:
    """把标准 test-update 请求完全收窄到公开基线固定范围。"""

    if (
        manifest.activation_id != request.update_id
        or manifest.base.commit != request.base_commit
        or manifest.target.commit != request.commit
        or request.source_ref != "origin/main"
        or request.components != COMPONENTS
        or request.public_cutover is not None
        or request.migration_from != SCHEMA_REVISION
        or request.migration_target != SCHEMA_REVISION
        or request.migration_compatibility != "none"
        or set(request.images) != set(COMPONENTS)
        or host_source_commit != manifest.target.commit
    ):
        raise PublicBaselineManagerError(
            "baseline manifest and standard request are not bound"
        )
    for component in COMPONENTS:
        manifest_image = manifest.images[component]
        request_image = request.images[component]
        if (
            request_image.ref != manifest_image.ref
            or request_image.image_id != manifest_image.image_id
            or request_image.archive_file != manifest_image.archive_file
            or request_image.archive_sha256 != manifest_image.archive_sha256
        ):
            raise PublicBaselineManagerError(
                "baseline image and standard request are not bound"
            )


def _fixed_scope() -> ChangedScope:
    return ChangedScope(
        components=COMPONENTS,
        migration_changed=False,
        backend_tests=(),
        frontend_tests=(),
        runtime_changed=True,
        risk="high-risk",
        high_risk_paths=(),
    )


class HostSourceInspector:
    """以固定 Git argv 复验 staged/active 根目录与公开 origin。"""

    def __init__(self, runner: FixedCommandRunner | None = None) -> None:
        self.runner = runner or FixedCommandRunner()

    def _git(self, root: Path, *arguments: str) -> str:
        payload = self.runner.run(("git", "-C", str(root), *arguments))
        try:
            return payload.decode("utf-8").strip()
        except UnicodeError as exc:
            raise PublicBaselineManagerError(
                "baseline Git observation is invalid"
            ) from exc

    def observe(self, root: Path) -> GitIdentity:
        commit = self._git(root, "rev-parse", "--verify", "HEAD^{commit}")
        tree = self._git(root, "rev-parse", "--verify", "HEAD^{tree}")
        return GitIdentity(
            commit=_string(commit, _COMMIT_RE, "observed commit"),
            tree=_string(tree, _COMMIT_RE, "observed tree"),
        )

    def verify(
        self,
        root: Path,
        *,
        identity: GitIdentity,
        origin_url: str,
    ) -> None:
        if self.observe(root) != identity:
            raise PublicBaselineManagerError("baseline source identity drifted")
        if self._git(root, "status", "--porcelain"):
            raise PublicBaselineManagerError("baseline source root is dirty")
        if self._git(root, "remote", "get-url", "--all", "origin") != origin_url:
            raise PublicBaselineManagerError("baseline origin drifted")
        remote_main = self._git(
            root,
            "rev-parse",
            "--verify",
            "refs/remotes/origin/main^{commit}",
        )
        if remote_main != identity.commit:
            raise PublicBaselineManagerError("baseline origin/main drifted")


class HostImageInspector:
    """一次性绑定镜像 ID、架构和三个固定 OCI label。"""

    def __init__(self, runner: FixedCommandRunner | None = None) -> None:
        self.runner = runner or FixedCommandRunner()

    def _identity(self, image_ref: str) -> str:
        payload = self.runner.run(
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
            )
        )
        try:
            return payload.decode("utf-8").strip()
        except UnicodeError as exc:
            raise PublicBaselineManagerError(
                "loaded image identity is invalid"
            ) from exc

    def verify(self, manifest: BaselineManifest) -> None:
        for component in sorted(COMPONENTS):
            image = manifest.images[component]
            expected = (
                f"{image.image_id}|amd64|{image.version}|"
                f"{manifest.target.commit}|{SCHEMA_REVISION}"
            )
            if self._identity(image.ref) != expected:
                raise PublicBaselineManagerError(
                    f"loaded {component} image identity is invalid"
                )


def _safe_unit_bytes(
    path: Path,
    *,
    expected_uid: int,
    expected_gid: int,
    expected_mode: int,
) -> bytes:
    descriptor = -1
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or metadata.st_gid != expected_gid
            or stat.S_IMODE(metadata.st_mode) != expected_mode
            or not 1 <= metadata.st_size <= 64 * 1024
            or metadata.st_nlink != 1
        ):
            raise PublicBaselineManagerError("vendor control unit is unsafe")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (
            (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != expected_uid
            or opened.st_gid != expected_gid
            or stat.S_IMODE(opened.st_mode) != expected_mode
            or opened.st_size != metadata.st_size
            or opened.st_nlink != 1
        ):
            raise PublicBaselineManagerError("vendor control unit is unsafe")
        payload = os.read(descriptor, 64 * 1024 + 1)
    except PublicBaselineManagerError:
        raise
    except OSError as exc:
        raise PublicBaselineManagerError("vendor control unit is unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(payload) != metadata.st_size:
        raise PublicBaselineManagerError("vendor control unit changed while reading")
    return payload


class HostVendorControlUnitManager:
    """原子更新固定 systemd unit，并在 root 回退后恢复旧版本。"""

    def __init__(
        self,
        *,
        expected_uid: int = 0,
        expected_operator_gid: int = OPERATOR_GID,
        expected_system_gid: int = SYSTEM_GID,
        runner: FixedCommandRunner | None = None,
        unit_path: Path = VENDOR_UNIT_PATH,
    ) -> None:
        self.expected_uid = expected_uid
        self.expected_operator_gid = expected_operator_gid
        self.expected_system_gid = expected_system_gid
        self.runner = runner or FixedCommandRunner()
        self.unit_path = unit_path

    def _source(
        self,
        root: Path,
        *,
        profile: Literal["base", "target"],
    ) -> bytes:
        expected_gid = (
            self.expected_operator_gid
            if profile == "base"
            else self.expected_system_gid
        )
        expected_mode = 0o640 if profile == "base" else 0o644
        return _safe_unit_bytes(
            root / VENDOR_UNIT_SOURCE,
            expected_uid=self.expected_uid,
            expected_gid=expected_gid,
            expected_mode=expected_mode,
        )

    def _restart_and_verify(self, expected: bytes) -> None:
        self.runner.run(("/usr/bin/systemctl", "daemon-reload"))
        self.runner.run(
            ("/usr/bin/systemctl", "restart", "vendor-control-agent.service")
        )
        self.runner.run(
            (
                "/usr/bin/systemctl",
                "is-active",
                "--quiet",
                "vendor-control-agent.service",
            )
        )
        if (
            _safe_unit_bytes(
                self.unit_path,
                expected_uid=self.expected_uid,
                expected_gid=self.expected_system_gid,
                expected_mode=0o644,
            )
            != expected
        ):
            raise PublicBaselineManagerError(
                "installed vendor control unit did not verify"
            )

    def _install(self, payload: bytes) -> None:
        current = _safe_unit_bytes(
            self.unit_path,
            expected_uid=self.expected_uid,
            expected_gid=self.expected_system_gid,
            expected_mode=0o644,
        )
        if current == payload:
            self._restart_and_verify(payload)
            return
        parent = self.unit_path.parent
        try:
            parent_info = parent.lstat()
            if (
                not stat.S_ISDIR(parent_info.st_mode)
                or stat.S_ISLNK(parent_info.st_mode)
                or parent_info.st_uid != self.expected_uid
                or parent_info.st_gid != self.expected_system_gid
                or stat.S_IMODE(parent_info.st_mode) & 0o022
            ):
                raise PublicBaselineManagerError("systemd unit directory is unsafe")
            temporary = parent / (f".{self.unit_path.name}.{uuid.uuid4().hex}.tmp")
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o644,
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.unit_path)
                self.unit_path.chmod(0o644)
                directory = os.open(parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                temporary.unlink(missing_ok=True)
        except PublicBaselineManagerError:
            raise
        except OSError as exc:
            raise PublicBaselineManagerError(
                "vendor control unit update failed"
            ) from exc
        self._restart_and_verify(payload)

    def preflight(self, active_root: Path, staged_root: Path) -> None:
        current = self._source(active_root, profile="base")
        self._source(staged_root, profile="target")
        if (
            _safe_unit_bytes(
                self.unit_path,
                expected_uid=self.expected_uid,
                expected_gid=self.expected_system_gid,
                expected_mode=0o644,
            )
            != current
        ):
            raise PublicBaselineManagerError(
                "installed vendor control unit is not source-bound"
            )

    def activate(self, outcome: ActivationOutcome) -> None:
        old = self._source(outcome.recovery_root, profile="base")
        target = self._source(outcome.active_root, profile="target")
        installed = _safe_unit_bytes(
            self.unit_path,
            expected_uid=self.expected_uid,
            expected_gid=self.expected_system_gid,
            expected_mode=0o644,
        )
        if installed not in {old, target}:
            raise PublicBaselineManagerError(
                "installed vendor control unit changed before activation"
            )
        if installed == old:
            self._install(target)
        else:
            self._restart_and_verify(target)

    def restore(self, outcome: ActivationOutcome) -> None:
        desired = self._source(outcome.active_root, profile="base")
        displaced = self._source(outcome.recovery_root, profile="target")
        installed = _safe_unit_bytes(
            self.unit_path,
            expected_uid=self.expected_uid,
            expected_gid=self.expected_system_gid,
            expected_mode=0o644,
        )
        if installed not in {desired, displaced}:
            raise PublicBaselineManagerError(
                "installed vendor control unit cannot be restored safely"
            )
        self._install(desired)

    def verify(self, active_root: Path) -> None:
        expected = self._source(active_root, profile="target")
        if (
            _safe_unit_bytes(
                self.unit_path,
                expected_uid=self.expected_uid,
                expected_gid=self.expected_system_gid,
                expected_mode=0o644,
            )
            != expected
        ):
            raise PublicBaselineManagerError("installed vendor control unit drifted")
        self.runner.run(
            (
                "/usr/bin/systemctl",
                "is-active",
                "--quiet",
                "vendor-control-agent.service",
            )
        )


class BaselineUpdateOperations:
    """复用 HostTestUpdateOperations，但把无迁移回退升级为 root+unit+镜像。"""

    def __init__(
        self,
        *,
        delegate: HostTestUpdateOperations,
        core: ActivationCore,
        unit_manager: UnitManager,
        source_inspector: SourceInspector,
        image_inspector: ImageInspector,
        manifest: BaselineManifest,
    ) -> None:
        self.delegate = delegate
        self.core = core
        self.unit_manager = unit_manager
        self.source_inspector = source_inspector
        self.image_inspector = image_inspector
        self.manifest = manifest

    def require_lifecycle_lock(self) -> None:
        self.delegate.require_lifecycle_lock()

    def validate_vendor_update_mode(self) -> str:
        return self.delegate.validate_vendor_update_mode()

    def require_owned_update_pauses(self, update_id: str) -> None:
        self.delegate.require_owned_update_pauses(update_id)

    def run_expand_migration(self, source: str, target: str) -> str:
        raise PublicBaselineManagerError("baseline migration is forbidden")

    def replace_backend_services(self, services: tuple[str, ...]) -> None:
        self.source_inspector.verify(
            ACTIVE_ROOT,
            identity=self.manifest.target,
            origin_url=self.manifest.origin_url,
        )
        self.unit_manager.verify(ACTIVE_ROOT)
        self.image_inspector.verify(self.manifest)
        self.delegate.replace_backend_services(services)

    def replace_web(self) -> None:
        raise PublicBaselineManagerError("baseline web-only update is forbidden")

    def verify_web(self) -> None:
        self.delegate.verify_web()

    def verify_budget_conservation(self) -> None:
        self.delegate.verify_budget_conservation()

    def verify_pause_state(self) -> None:
        self.delegate.verify_pause_state()

    def probe_balance(self) -> None:
        self.delegate.probe_balance()

    def verify_backend_services(self) -> None:
        self.delegate.verify_backend_services()
        self.source_inspector.verify(
            ACTIVE_ROOT,
            identity=self.manifest.target,
            origin_url=self.manifest.origin_url,
        )
        self.unit_manager.verify(ACTIVE_ROOT)
        self.image_inspector.verify(self.manifest)

    def restore_owned_update_pauses(self, update_id: str) -> None:
        if update_id != self.manifest.activation_id:
            raise PublicBaselineManagerError("baseline pause restore request is invalid")
        # Keep both lanes owned until VERIFIED is durably recorded.  The outer
        # manager restores them after the state transition and can safely retry
        # that release if the process exits between those two operations.

    def cleanup_rollback_images(self, update_id: str) -> None:
        if update_id != self.manifest.activation_id:
            raise PublicBaselineManagerError("baseline cleanup request is invalid")
        # TestUpdateVerify invokes this before it durably records VERIFIED.  Keep
        # the old-image tags until finalize so a state-write failure can still
        # execute the complete cross-history rollback.

    def hold_fail_closed(self, update_id: str) -> None:
        self.delegate.hold_fail_closed(update_id)

    def rollback_no_migration(
        self,
        kind: str,
        update_id: str,
    ) -> tuple[str, str]:
        if kind != "backend-safe" or update_id != self.manifest.activation_id:
            raise PublicBaselineManagerError("baseline rollback request is invalid")
        self.delegate.hold_fail_closed(update_id)
        outcome = self.core.rollback()
        _validate_outcome(outcome, self.manifest, target=False)
        errors: list[Exception] = []
        try:
            self.unit_manager.restore(outcome)
        except Exception as exc:
            errors.append(exc)
        result: tuple[str, str] | None = None
        try:
            result = self.delegate.rollback_no_migration_preserving_images(
                kind,
                update_id,
            )
        except Exception as exc:
            errors.append(exc)
        if errors or result is None:
            raise PublicBaselineManagerError("baseline rollback was incomplete")
        if result != (
            self.manifest.base.commit,
            self.manifest.migration_from,
        ):
            raise PublicBaselineManagerError("baseline rollback identity is invalid")
        return result


def _validate_prepared(
    outcome: PreparedActivation,
    manifest: BaselineManifest,
) -> None:
    if (
        outcome.activation_id != manifest.activation_id
        or outcome.commit != manifest.target.commit
        or outcome.tree != manifest.target.tree
        or not isinstance(outcome.staged_root, Path)
        or not outcome.staged_root.is_absolute()
        or ".." in outcome.staged_root.parts
    ):
        raise PublicBaselineManagerError("baseline prepared outcome is invalid")


def _validate_outcome(
    outcome: ActivationOutcome,
    manifest: BaselineManifest,
    *,
    target: bool,
) -> None:
    expected = manifest.target if target else manifest.base
    if (
        outcome.activation_id != manifest.activation_id
        or outcome.commit != expected.commit
        or outcome.tree != expected.tree
        or outcome.active_root != ACTIVE_ROOT
        or not isinstance(outcome.recovery_root, Path)
        or not outcome.recovery_root.is_absolute()
        or ".." in outcome.recovery_root.parts
    ):
        raise PublicBaselineManagerError("baseline activation outcome is invalid")


class PublicBaselineManager:
    """公开基线 prepare/apply/verify/status/finalize 生命周期。"""

    def __init__(
        self,
        *,
        manifest: BaselineManifest,
        core_request: object,
        request_raw: str,
        request: TestUpdateRequest,
        store: TestUpdateStore,
        core: ActivationCore,
        operations_factory: UpdateOperationsFactory,
        source_inspector: SourceInspector,
        image_inspector: ImageInspector,
        unit_manager: UnitManager,
        host_source_commit: str,
    ) -> None:
        validate_request_binding(
            manifest,
            request,
            host_source_commit=host_source_commit,
        )
        self.manifest = manifest
        self.core_request = core_request
        self.request_raw = request_raw
        self.request = request
        self.store = store
        self.core = core
        self.operations_factory = operations_factory
        self.source_inspector = source_inspector
        self.image_inspector = image_inspector
        self.unit_manager = unit_manager

    def _operations(self) -> HostTestUpdateOperations:
        return self.operations_factory(ACTIVE_ROOT, self.request)

    def _wrapped_operations(self) -> BaselineUpdateOperations:
        return BaselineUpdateOperations(
            delegate=self._operations(),
            core=self.core,
            unit_manager=self.unit_manager,
            source_inspector=self.source_inspector,
            image_inspector=self.image_inspector,
            manifest=self.manifest,
        )

    def prepare(self) -> None:
        operations = self._operations()
        try:
            operations.require_lifecycle_lock()
        except Exception:
            raise PublicBaselineManagerError(
                "public baseline lifecycle lock is unavailable"
            ) from None
        self.store.create(self.request_raw)
        try:
            if (
                operations.current_migration_head()
                != self.manifest.migration_from
            ):
                raise PublicBaselineManagerError(
                    "baseline migration head drifted before prepare"
                )
            prepared = self.core.prepare(self.core_request)
            _validate_prepared(prepared, self.manifest)
            self.source_inspector.verify(
                prepared.staged_root,
                identity=self.manifest.target,
                origin_url=self.manifest.origin_url,
            )
            self.unit_manager.preflight(ACTIVE_ROOT, prepared.staged_root)
            operations.load_and_validate_images()
            self.image_inspector.verify(self.manifest)
            operations.prepare_rollback_images()
            TestUpdateManager(self.store, operations).prepare(
                _fixed_scope(),
                update_id=self.request.update_id,
                commit=self.request.commit,
                migration_from=self.request.migration_from,
                migration_target=self.request.migration_target,
            )
        except TestUpdateManagerError:
            raise
        except Exception:
            with contextlib.suppress(Exception):
                self.store.block(
                    TestUpdateState.PREPARED,
                    step="baseline_prepare",
                    error_type="validation_failed",
                    actual_commit=self.request.base_commit,
                    actual_migration_head=self.request.migration_from,
                )
            with contextlib.suppress(Exception):
                operations.hold_fail_closed(self.request.update_id)
            raise PublicBaselineManagerError(
                "public baseline prepare blocked"
            ) from None

    def _rollback_before_service_replace(
        self,
        operations: HostTestUpdateOperations,
    ) -> bool:
        try:
            operations.hold_fail_closed(self.request.update_id)
            outcome = self.core.rollback()
            _validate_outcome(outcome, self.manifest, target=False)
            self.unit_manager.restore(outcome)
            restored = operations.rollback_no_migration_preserving_images(
                "backend-safe",
                self.request.update_id,
            )
            if restored != (
                self.manifest.base.commit,
                self.manifest.migration_from,
            ):
                raise PublicBaselineManagerError(
                    "baseline rollback identity is invalid"
                )
            self.store.transition(
                TestUpdateState.PREPARED,
                TestUpdateState.ROLLED_BACK,
                step="baseline_rollback",
                actual_commit=self.request.base_commit,
                actual_migration_head=self.request.migration_from,
            )
            return True
        except Exception:
            with contextlib.suppress(Exception):
                self.store.block(
                    TestUpdateState.PREPARED,
                    step="baseline_activate",
                    error_type="step_failed",
                    actual_commit=self.request.commit,
                    actual_migration_head=self.request.migration_from,
                )
            with contextlib.suppress(Exception):
                operations.hold_fail_closed(self.request.update_id)
            return False

    def apply(self) -> None:
        operations = self._operations()
        current = self.store.read_consistent_state()["state"]
        if current in {
            TestUpdateState.APPLIED.value,
            TestUpdateState.VERIFIED.value,
        }:
            operations.require_lifecycle_lock()
            self.source_inspector.verify(
                ACTIVE_ROOT,
                identity=self.manifest.target,
                origin_url=self.manifest.origin_url,
            )
            self.unit_manager.verify(ACTIVE_ROOT)
            self.image_inspector.verify(self.manifest)
            operations.verify_backend_services()
            return
        if current != TestUpdateState.PREPARED.value:
            raise PublicBaselineManagerError(
                "public baseline apply state is invalid"
            )
        try:
            operations.require_lifecycle_lock()
        except Exception:
            raise PublicBaselineManagerError(
                "public baseline lifecycle lock is unavailable"
            ) from None
        actual_head = operations.current_migration_head()
        if actual_head != self.manifest.migration_from:
            with contextlib.suppress(Exception):
                self.store.block(
                    TestUpdateState.PREPARED,
                    step="baseline_apply_migration_head",
                    error_type="invariant_failed",
                    actual_commit=self.manifest.base.commit,
                    actual_migration_head=actual_head,
                )
            with contextlib.suppress(Exception):
                operations.hold_fail_closed(self.request.update_id)
            raise PublicBaselineManagerError(
                "public baseline apply blocked before root switch"
            )
        try:
            outcome = self.core.activate(self.core_request)
            _validate_outcome(outcome, self.manifest, target=True)
            self.source_inspector.verify(
                outcome.active_root,
                identity=self.manifest.target,
                origin_url=self.manifest.origin_url,
            )
            self.unit_manager.activate(outcome)
            self.image_inspector.verify(self.manifest)
        except Exception:
            if self._rollback_before_service_replace(operations):
                raise PublicBaselineManagerError(
                    "public baseline apply rolled back before service replacement"
                ) from None
            raise PublicBaselineManagerError(
                "public baseline apply blocked before service replacement"
            ) from None

        wrapped = BaselineUpdateOperations(
            delegate=operations,
            core=self.core,
            unit_manager=self.unit_manager,
            source_inspector=self.source_inspector,
            image_inspector=self.image_inspector,
            manifest=self.manifest,
        )
        TestUpdateApply(self.store, wrapped).apply(
            "backend-safe",
            update_id=self.request.update_id,
            commit=self.request.commit,
            migration_from=self.request.migration_from,
            migration_target=self.request.migration_target,
        )

    def verify(self) -> None:
        operations = self._operations()
        current = self.store.read_consistent_state()["state"]
        if current == TestUpdateState.VERIFIED.value:
            operations.require_lifecycle_lock()
            self.source_inspector.verify(
                ACTIVE_ROOT,
                identity=self.manifest.target,
                origin_url=self.manifest.origin_url,
            )
            self.unit_manager.verify(ACTIVE_ROOT)
            self.image_inspector.verify(self.manifest)
            operations.verify_backend_services()
            operations.restore_owned_update_pauses(self.request.update_id)
            return
        if current != TestUpdateState.APPLIED.value:
            raise PublicBaselineManagerError(
                "public baseline verify state is invalid"
            )
        TestUpdateVerify(self.store, self._wrapped_operations()).verify(
            "backend-safe",
            update_id=self.request.update_id,
            commit=self.request.commit,
            migration_from=self.request.migration_from,
            migration_target=self.request.migration_target,
        )
        operations.restore_owned_update_pauses(self.request.update_id)

    def status(self) -> dict[str, object]:
        state = (
            self.store.read_consistent_state()
            if self.store.update_dir.exists()
            else {"state": "incoming"}
        )
        observed = self.source_inspector.observe(ACTIVE_ROOT)
        operations = self._operations()
        return {
            "activation_id": self.manifest.activation_id,
            "state": state["state"],
            "actual_commit": observed.commit,
            "actual_tree": observed.tree,
            "actual_migration_head": operations.current_migration_head(),
            "target_commit": self.manifest.target.commit,
            "target_tree": self.manifest.target.tree,
        }

    def finalize(self) -> None:
        state = self.store.read_consistent_state()
        if state["state"] != TestUpdateState.VERIFIED.value:
            raise PublicBaselineManagerError(
                "only a verified baseline can be finalized"
            )
        operations = self._operations()
        operations.require_lifecycle_lock()
        self.source_inspector.verify(
            ACTIVE_ROOT,
            identity=self.manifest.target,
            origin_url=self.manifest.origin_url,
        )
        self.unit_manager.verify(ACTIVE_ROOT)
        self.image_inspector.verify(self.manifest)
        operations.verify_backend_services()
        operations.restore_owned_update_pauses(self.request.update_id)
        outcome = self.core.finalize()
        _validate_outcome(outcome, self.manifest, target=True)

    def cleanup(self) -> None:
        """在 VERIFIED 后删除旧根和三个大交付文件，保留小型审计证据。"""

        state = self.store.read_consistent_state()
        if state["state"] != TestUpdateState.VERIFIED.value:
            raise PublicBaselineManagerError(
                "only a verified baseline can be cleaned"
            )
        operations = self._operations()
        operations.require_lifecycle_lock()
        self.source_inspector.verify(
            ACTIVE_ROOT,
            identity=self.manifest.target,
            origin_url=self.manifest.origin_url,
        )
        self.unit_manager.verify(ACTIVE_ROOT)
        self.image_inspector.verify(self.manifest)
        operations.verify_backend_services()
        operations.restore_owned_update_pauses(self.request.update_id)
        outcome = self.core.cleanup()
        _validate_outcome(outcome, self.manifest, target=True)
        if outcome.state != "cleaned":
            raise PublicBaselineManagerError(
                "public baseline cleanup state is invalid"
            )
        operations.cleanup_rollback_images_verified(self.request.update_id)
        self._cleanup_large_artifacts()

    def _cleanup_large_artifacts(self) -> None:
        artifacts = {
            self.manifest.bundle_file: (
                self.manifest.bundle_sha256,
                2 * 1024 * 1024 * 1024,
            ),
            **{
                image.archive_file: (
                    image.archive_sha256,
                    16 * 1024 * 1024 * 1024,
                )
                for image in self.manifest.images.values()
            },
        }
        for name, (digest, maximum_size) in artifacts.items():
            _unlink_verified_artifact(
                INCOMING_ROOT / name,
                expected_sha256=digest,
                expected_uid=0,
                maximum_size=maximum_size,
            )


class _InheritedLifecycleGuard:
    def __init__(self, runtime_root: Path) -> None:
        self.runtime_root = runtime_root

    def require_held(self) -> None:
        require_inherited_lifecycle_lock(self.runtime_root)


def _build_operations_factory(
    *,
    runtime_root: Path,
    marker_file: Path,
    state_root: Path,
    host_source_commit: str,
    expected_uid: int,
) -> UpdateOperationsFactory:
    def factory(
        root: Path,
        request: TestUpdateRequest,
    ) -> HostTestUpdateOperations:
        return HostTestUpdateOperations(
            root=root,
            runtime_root=runtime_root,
            marker_file=marker_file,
            state_root=state_root,
            request=request,
            host_source_commit=host_source_commit,
            expected_uid=expected_uid,
        )

    return factory


def _load_core(
    *,
    manifest_raw: bytes,
    activation_id: str,
    expected_uid: int,
) -> tuple[object, ActivationCore]:
    try:
        module = importlib.import_module("public_baseline_activation")
        request_type = module.ActivationRequest
        activator_type = module.PublicBaselineActivator
        core_request = request_type.from_json_bytes(manifest_raw)
        core = activator_type(
            activation_id=activation_id,
            artifacts_root=INCOMING_ROOT,
            lifecycle_guard=_InheritedLifecycleGuard(RUNTIME_ROOT),
            active_root=ACTIVE_ROOT,
            workspace_root=ACTIVE_ROOT.parent,
            expected_uid=expected_uid,
            expected_operator_uid=OPERATOR_UID,
            expected_operator_gid=OPERATOR_GID,
        )
    except Exception as exc:
        raise PublicBaselineManagerError(
            "public baseline activation core is unavailable"
        ) from exc
    return core_request, cast(ActivationCore, core)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="public_baseline_manager.py")
    parser.add_argument(
        "command",
        choices=("prepare", "apply", "verify", "status", "finalize", "cleanup"),
    )
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--marker-file", required=True, type=Path)
    return parser


def _require_cli_paths(args: argparse.Namespace) -> None:
    expected = {
        "root": ACTIVE_ROOT,
        "runtime_root": RUNTIME_ROOT,
        "state_root": STATE_ROOT,
        "manifest": MANIFEST_PATH,
        "request": REQUEST_PATH,
        "marker_file": MARKER_PATH,
    }
    if any(getattr(args, name) != path for name, path in expected.items()):
        raise PublicBaselineManagerError("public baseline control paths are invalid")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        _require_cli_paths(args)
        expected_uid = 0
        manifest_raw = _read_private_bytes(
            args.manifest,
            expected_uid=expected_uid,
        )
        manifest = parse_baseline_manifest(manifest_raw)
        request_raw, request = _read_private_request(
            args.request,
            expected_uid=expected_uid,
        )
        host_source_commit = os.environ.get("SMS_HOST_SOURCE_COMMIT", "")
        core_request, core = _load_core(
            manifest_raw=manifest_raw,
            activation_id=manifest.activation_id,
            expected_uid=expected_uid,
        )
        store = TestUpdateStore(args.state_root, request.update_id)
        runner = FixedCommandRunner()
        manager = PublicBaselineManager(
            manifest=manifest,
            core_request=core_request,
            request_raw=request_raw,
            request=request,
            store=store,
            core=core,
            operations_factory=_build_operations_factory(
                runtime_root=args.runtime_root,
                marker_file=args.marker_file,
                state_root=args.state_root,
                host_source_commit=host_source_commit,
                expected_uid=expected_uid,
            ),
            source_inspector=HostSourceInspector(runner),
            image_inspector=HostImageInspector(runner),
            unit_manager=HostVendorControlUnitManager(
                expected_uid=expected_uid,
                expected_operator_gid=OPERATOR_GID,
                expected_system_gid=SYSTEM_GID,
                runner=runner,
            ),
            host_source_commit=host_source_commit,
        )
        if args.command == "status":
            print(
                json.dumps(
                    manager.status(),
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        else:
            getattr(manager, args.command)()
            print(
                json.dumps(
                    {
                        "activation_id": manifest.activation_id,
                        "status": args.command,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        return 0
    except Exception:
        command = getattr(args, "command", "unknown")
        print(f"public-baseline {command} blocked", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
