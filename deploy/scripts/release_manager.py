#!/usr/bin/env python3
"""四镜像统一发布状态机。"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, NoReturn, Protocol, cast

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from release_manifest import (  # noqa: E402
    MigrationCompatibility,
    ReleaseManifest,
    ReleaseManifestError,
    load_manifest,
    load_manifest_bytes,
    validate_changed_images,
)
from release_store import (  # noqa: E402
    ReleaseState,
    ReleaseStore,
    ReleaseStoreError,
)
from render_release_evidence import (  # type: ignore[import-not-found,unused-ignore]  # noqa: E402
    ReleaseEvidenceError,
    parse_postgres_version_output,
    parse_redis_version_output,
)


class ReleaseManagerError(RuntimeError):
    """发布动作不满足严格安全契约。"""


_PRODUCTION_RELEASE_ROOT = Path("/var/lib/sms-platform/releases")
_SMOKE_PARENT_RE = re.compile(r"sms-platform-release-control-[A-Za-z0-9]{8,32}\Z")


def _release_root_error() -> ReleaseManagerError:
    return ReleaseManagerError("release root is invalid")


def _validate_cli_release_root(
    release_root: Path,
    *,
    mode: Literal["development", "production"],
    platform_root: Path,
) -> Path:
    """在 CLI 副作用前验证固定生产根或严格 development smoke 根。"""

    raw = str(release_root)
    if (
        not release_root.is_absolute()
        or ".." in release_root.parts
        or any(ord(character) < 32 or ord(character) == 127 for character in raw)
    ):
        raise _release_root_error()
    if mode == "production":
        if release_root != _PRODUCTION_RELEASE_ROOT:
            raise _release_root_error()
        return _PRODUCTION_RELEASE_ROOT
    if release_root == _PRODUCTION_RELEASE_ROOT:
        return _PRODUCTION_RELEASE_ROOT

    temporary_root = Path(tempfile.gettempdir())
    parent = release_root.parent
    if (
        release_root.name != "releases"
        or parent.parent != temporary_root
        or _SMOKE_PARENT_RE.fullmatch(parent.name) is None
    ):
        raise _release_root_error()
    try:
        parent_info = parent.lstat()
        temporary_resolved = temporary_root.resolve(strict=True)
        parent_resolved = parent.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise _release_root_error() from exc
    if (
        stat.S_ISLNK(parent_info.st_mode)
        or not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != os.geteuid()
        or stat.S_IMODE(parent_info.st_mode) != 0o700
        or parent_resolved.parent != temporary_resolved
    ):
        raise _release_root_error()

    for ancestor in (parent, *parent.parents):
        try:
            (ancestor / ".git").lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise _release_root_error() from exc
        raise _release_root_error()
    try:
        platform_resolved = platform_root.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise _release_root_error() from exc
    if parent_resolved == platform_resolved or platform_resolved in parent_resolved.parents:
        raise _release_root_error()

    try:
        root_info = release_root.lstat()
    except FileNotFoundError:
        return release_root
    except OSError as exc:
        raise _release_root_error() from exc
    if (
        stat.S_ISLNK(root_info.st_mode)
        or not stat.S_ISDIR(root_info.st_mode)
        or root_info.st_uid != os.geteuid()
        or stat.S_IMODE(root_info.st_mode) != 0o700
    ):
        raise _release_root_error()
    try:
        if release_root.resolve(strict=True).parent != parent_resolved:
            raise _release_root_error()
    except OSError as exc:
        raise _release_root_error() from exc
    return release_root


class ReleaseStepKind(StrEnum):
    """统一发布中可审计的固定服务步骤。"""

    QUIESCE_BACKEND = "quiesce_backend"
    WAIT_BEAT_LEASE = "wait_beat_lease"
    RECREATE_POSTGRES = "recreate_postgres"
    RECREATE_REDIS = "recreate_redis"
    RUN_MIGRATE = "run_migrate"
    RECREATE_BACKEND = "recreate_backend"
    RECREATE_WEB = "recreate_web"
    VERIFY = "verify"


@dataclass(frozen=True)
class ReleaseStep:
    """纯计划中的步骤类型与精确 Compose 服务集合。"""

    kind: ReleaseStepKind
    services: tuple[str, ...]


class ReconciliationDecision(StrEnum):
    RESUME = "resume"
    ROLLBACK = "rollback"
    FINALIZE = "finalize"
    RECOVERY_REQUIRED = "recovery_required"


@dataclass(frozen=True)
class RuntimeObservation:
    """重启后从实际 env、容器与 migration 取得的规范化观察。"""

    env_state: str
    service_state: str
    migration_state: str
    healthy: bool
    migration_required: bool = False


def reconcile_release(
    stored_state: Mapping[str, object],
    observed: RuntimeObservation,
) -> ReconciliationDecision:
    """对所有持久化/运行态组合给出唯一失败关闭决策。"""

    state = stored_state.get("state")
    if state in {ReleaseState.SUCCEEDED.value, ReleaseState.ROLLED_BACK.value}:
        return ReconciliationDecision.FINALIZE
    if state in {
        ReleaseState.FAILED.value,
        ReleaseState.RECOVERY_REQUIRED.value,
    }:
        return ReconciliationDecision.RECOVERY_REQUIRED
    if state not in {
        ReleaseState.STAGED.value,
        ReleaseState.PREPARED.value,
        ReleaseState.ACTIVATING.value,
        ReleaseState.ROLLING_BACK.value,
    }:
        return ReconciliationDecision.RECOVERY_REQUIRED
    if (
        observed.env_state not in {"original", "target"}
        or observed.service_state not in {"original", "target", "prefix"}
        or observed.migration_state not in {"original", "target"}
        or not observed.healthy
    ):
        return ReconciliationDecision.RECOVERY_REQUIRED
    if state == ReleaseState.ROLLING_BACK.value:
        return ReconciliationDecision.ROLLBACK
    migration_is_original = observed.migration_state == "original" or (
        not observed.migration_required and observed.migration_state == "target"
    )
    if (
        observed.migration_required
        and observed.migration_state == "original"
        and observed.service_state == "target"
    ):
        return ReconciliationDecision.RECOVERY_REQUIRED
    if state in {ReleaseState.STAGED.value, ReleaseState.PREPARED.value}:
        if observed.env_state == "target" and observed.service_state == "target":
            return ReconciliationDecision.FINALIZE
        if (
            observed.env_state == "original"
            and observed.service_state == "original"
            and migration_is_original
        ):
            return ReconciliationDecision.RESUME
        return ReconciliationDecision.RECOVERY_REQUIRED
    if (
        state == ReleaseState.ACTIVATING.value
        and observed.env_state == "original"
        and observed.service_state == "original"
        and migration_is_original
    ):
        return ReconciliationDecision.RESUME
    if observed.env_state == "original" and observed.service_state != "original":
        return ReconciliationDecision.ROLLBACK
    if observed.env_state == "target":
        if observed.service_state == "target":
            return ReconciliationDecision.FINALIZE
        if observed.service_state in {"original", "prefix"}:
            return ReconciliationDecision.RESUME
    return ReconciliationDecision.RECOVERY_REQUIRED


class _ActivationStepError(RuntimeError):
    def __init__(self, kind: ReleaseStepKind, *, ambiguous: bool = False) -> None:
        super().__init__(kind.value)
        self.kind = kind
        self.ambiguous = ambiguous


class _FinalRuntimeError(ReleaseManagerError):
    def __init__(self, *, ambiguous: bool = False) -> None:
        super().__init__("final runtime verification failed")
        self.ambiguous = ambiguous


class _ReleaseInterrupted(ReleaseManagerError):
    pass


class CommandRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]: ...


class SubprocessRunner:
    """仅通过固定 argv 运行受控只读检查或 development docker load。"""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(argv),
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
        )


_IMAGE_NAMES = ("api", "web", "postgres", "redis")
_ENV_IMAGE_KEYS = {
    "api": "SMS_API_IMAGE",
    "web": "SMS_WEB_IMAGE",
    "postgres": "SMS_POSTGRES_IMAGE",
    "redis": "SMS_REDIS_IMAGE",
}
_IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
_CONTAINER_ID_RE = re.compile(r"[0-9a-f]{64}")
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?")
_MIGRATION_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?")
_REPORT_HASH_RE = re.compile(r"[0-9a-f]{64}")
_APP_VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_WORKFLOW_REPOSITORY_RE = re.compile(
    r"(?:local|[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"
)
_DRILL_DATABASE_RE = re.compile(r"sms_drill_[a-z0-9_]{1,48}")
_REPO_DIGEST_RE = re.compile(r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}")
_POSTGRES_NORMALIZED_VERSION_RE = re.compile(r"[1-9][0-9]*(?:\.[0-9]+){1,2}")
_REDIS_NORMALIZED_VERSION_RE = re.compile(r"[1-9][0-9]*\.[0-9]+\.[0-9]+")
_SAFE_DEVELOPMENT_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,510}[A-Za-z0-9]")
_RELEASE_REPORT_FIELDS = frozenset(
    {
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
)
_RELEASE_SOURCE_FIELDS = frozenset(
    {
        "app_version",
        "git_sha",
        "schema_revision",
        "openapi_sha256",
        "workflow_repository",
        "workflow_run_id",
        "workflow_run_attempt",
        "sbom_sha256",
    }
)
_RELEASE_IMAGE_FIELDS = frozenset(
    {"ref", "image_id", "repo_digests", "scan_report_sha256", "scan_passed"}
)
_PROMOTION_SOURCE_FIELDS = frozenset(
    {"report_sha256", "candidate_commit", "source", "images"}
)
_PROMOTION_IMAGE_FIELDS = frozenset({"ref", "image_id", "scan_report_sha256"})
_CONTROL_SMOKE_REPORT_FIELDS = frozenset(
    {
        "schema_version",
        "gate_type",
        "candidate_commit",
        "generated_at",
        "purpose",
        "scan_performed",
        "authorized_for_control_smoke",
        "images",
    }
)
_CONTROL_SMOKE_IMAGE_FIELDS = frozenset({"ref", "image_id", "platform"})
_DATA_REPORT_FIELDS = frozenset(
    {
        "schema_version",
        "gate_type",
        "candidate_commit",
        "generated_at",
        "images",
        "checks",
        "passed",
    }
)
_DATA_IMAGE_FIELDS = frozenset({"ref", "image_id", "platform", "version", "major"})
_DATA_CHECK_FIELDS = frozenset(
    {
        "postgres_role_constraints",
        "postgres_restart_persistence",
        "redis_aof_restart_persistence",
    }
)
_CHANGE_FIELDS = frozenset(
    {
        "schema_version",
        "record_type",
        "change_id",
        "release_id",
        "target_commit",
        "target_postgres_image_id",
        "approval",
        "restore",
    }
)
_APPROVAL_FIELDS = frozenset({"status", "approved_by", "approved_at"})
_RESTORE_BINDING_FIELDS = frozenset({"snapshot_id", "report_sha256"})
_RESTORE_REPORT_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "snapshot_id",
        "git_commit",
        "database",
        "started_at",
        "finished_at",
        "restore_seconds",
        "rto_limit_seconds",
        "within_rto",
        "checks",
        "table_counts",
    }
)
_RESTORE_CHECK_FIELDS = frozenset({"alembic_version", "role_flags", "audit_privileges"})
_TABLE_COUNT_FIELDS = frozenset({"sms_batch", "audit_log", "raw_vendor_log"})
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_MAX_JSON_BYTES = 1024 * 1024
_CONTROL_SMOKE_PURPOSE = "release_control_failure_injection"
_RUNTIME_ROOT_NAME = "runtime-secrets"
_QUIESCE_SERVICES = (
    "beat",
    "outbox-dispatcher",
    "worker-realtime",
    "worker-bulk",
    "worker-callback",
    "api",
)
_BACKEND_SERVICES = (
    "api",
    "worker-realtime",
    "worker-bulk",
    "worker-callback",
    "outbox-dispatcher",
    "beat",
)
_RUNTIME_SERVICES = (
    "api",
    "web",
    "postgres",
    "redis",
    "redis-auth",
    "redis-control",
    "worker-realtime",
    "worker-bulk",
    "worker-callback",
    "outbox-dispatcher",
    "beat",
)
_WORKER_SERVICES = ("worker-realtime", "worker-bulk", "worker-callback")
_WORKER_PROBE_SERVICE = "worker-realtime"
_HEALTHCHECK_SERVICES = frozenset(
    {"api", "web", "postgres", "redis", "redis-auth", "redis-control"}
)


def _service_image_name(service: str) -> str:
    """把运行服务映射到四类受签名发布镜像。"""

    if service in {"redis-auth", "redis-control"}:
        return "redis"
    return service if service in _IMAGE_NAMES else "api"


def build_activation_plan(manifest: ReleaseManifest) -> tuple[ReleaseStep, ...]:
    """仅由已验证清单生成确定性服务计划，不读取或修改外部状态。"""

    changed = {name for name, spec in manifest.images.items() if spec.changed}
    data_changed = bool({"postgres", "redis"} & changed)
    migration_needed = manifest.migration_target != manifest.migration_from
    backend_needed = data_changed or "api" in changed or migration_needed
    steps: list[ReleaseStep] = []
    if backend_needed:
        steps.append(ReleaseStep(ReleaseStepKind.QUIESCE_BACKEND, _QUIESCE_SERVICES))
        steps.append(ReleaseStep(ReleaseStepKind.WAIT_BEAT_LEASE, ()))
    if "postgres" in changed:
        steps.append(ReleaseStep(ReleaseStepKind.RECREATE_POSTGRES, ("postgres",)))
    if "redis" in changed:
        steps.append(
            ReleaseStep(
                ReleaseStepKind.RECREATE_REDIS,
                ("redis", "redis-auth", "redis-control"),
            )
        )
    if migration_needed:
        steps.append(ReleaseStep(ReleaseStepKind.RUN_MIGRATE, ("migrate",)))
    if backend_needed:
        steps.append(ReleaseStep(ReleaseStepKind.RECREATE_BACKEND, _BACKEND_SERVICES))
    if "web" in changed:
        steps.append(ReleaseStep(ReleaseStepKind.RECREATE_WEB, ("web",)))
    steps.append(ReleaseStep(ReleaseStepKind.VERIFY, ()))
    return tuple(steps)


def activation_commands(root: Path, plan: Sequence[ReleaseStep]) -> list[list[str]]:
    """把纯计划映射为固定 argv；不执行命令。"""

    compose = [
        "docker",
        "compose",
        "--env-file",
        str(root / ".env"),
        "-f",
        str(root / "deploy" / "docker-compose.yml"),
    ]
    commands: list[list[str]] = []
    for step in plan:
        if step.kind is ReleaseStepKind.QUIESCE_BACKEND:
            command = compose + ["stop", *step.services]
        elif step.kind is ReleaseStepKind.WAIT_BEAT_LEASE:
            command = ["sleep", "31"]
        elif step.kind in {
            ReleaseStepKind.RECREATE_POSTGRES,
            ReleaseStepKind.RECREATE_REDIS,
            ReleaseStepKind.RECREATE_BACKEND,
            ReleaseStepKind.RECREATE_WEB,
        }:
            command = compose + [
                "up",
                "-d",
                "--no-deps",
                "--force-recreate",
                "--wait",
                "--wait-timeout",
                "120",
                *step.services,
            ]
        elif step.kind is ReleaseStepKind.RUN_MIGRATE:
            command = compose + ["run", "--rm", *step.services]
        elif step.kind is ReleaseStepKind.VERIFY:
            command = compose + ["config", "--quiet"]
        else:  # pragma: no cover - StrEnum exhaustiveness guard
            raise ReleaseManagerError("activation plan contains an unknown step")
        commands.append(command)
    return commands


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseManagerError("release JSON contains duplicate fields")
        result[key] = value
    return result


def _exact_object(
    value: object,
    fields: frozenset[str],
    context: str,
) -> dict[str, Any]:
    if type(value) is not dict:
        raise ReleaseManagerError(f"{context} must be an exact object")
    result = cast(dict[str, Any], value)
    if set(result) != fields:
        raise ReleaseManagerError(f"{context} has invalid fields")
    return result


def _safe_id(value: object, context: str) -> str:
    if type(value) is not str or _SAFE_ID_RE.fullmatch(value) is None or ".." in value:
        raise ReleaseManagerError(f"{context} is invalid")
    return value


def _read_safe_bytes(
    path: Path,
    *,
    expected_uid: int | None = None,
    expected_mode: int | None = None,
    maximum: int | None = None,
) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise ReleaseManagerError("required regular file is unavailable") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ReleaseManagerError("required path is not a regular file")
    if expected_uid is not None and before.st_uid != expected_uid:
        raise ReleaseManagerError("regular file owner is invalid")
    if expected_mode is not None and stat.S_IMODE(before.st_mode) != expected_mode:
        raise ReleaseManagerError("regular file mode is invalid")
    descriptor = os.open(path, os.O_RDONLY | _NOFOLLOW)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ReleaseManagerError("regular file changed while opening")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if maximum is not None and size > maximum:
                raise ReleaseManagerError("regular file exceeds the allowed size")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _parse_json_bytes(raw: bytes, context: str) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except ReleaseManagerError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseManagerError(f"{context} is not strict JSON") from exc
    if type(value) is not dict:
        raise ReleaseManagerError(f"{context} must be an object")
    return cast(dict[str, Any], value)


def _read_json(path: Path, context: str) -> dict[str, Any]:
    raw = _read_safe_bytes(path, expected_mode=0o600, maximum=_MAX_JSON_BYTES)
    return _parse_json_bytes(raw, context)


def _read_bound_json(path: Path, expected_sha256: str, context: str) -> dict[str, Any]:
    raw = _read_safe_bytes(path, expected_mode=0o600, maximum=_MAX_JSON_BYTES)
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(actual_sha256, expected_sha256):
        raise ReleaseManagerError(f"{context} hash does not match manifest")
    return _parse_json_bytes(raw, context)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | _NOFOLLOW)
    try:
        for chunk in iter(lambda: os.read(descriptor, 1024 * 1024), b""):
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _validate_staging_directory(path: Path, expected_uid: int) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ReleaseManagerError("staging directory is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ReleaseManagerError("staging directory is unsafe")
    if info.st_uid != expected_uid or stat.S_IMODE(info.st_mode) != 0o700:
        raise ReleaseManagerError("staging directory ownership or mode is unsafe")


def _expected_bundle_names(manifest: ReleaseManifest) -> set[str]:
    names = {"manifest.json", cast(str, manifest.evidence["release_gate"])}
    for spec in manifest.images.values():
        if spec.archive_file is not None:
            names.add(spec.archive_file)
    data_name = manifest.evidence["data_images"]
    if data_name is not None:
        names.add(cast(str, data_name))
    backup = manifest.evidence["backup_restore_change"]
    if backup is not None:
        if not isinstance(backup, Mapping):
            raise ReleaseManagerError("staging backup evidence mapping is invalid")
        names.update(backup.values())
    return names


def _validate_staging_bundle(
    manifest_path: Path,
    expected_uid: int,
) -> tuple[ReleaseManifest, bytes, list[Path]]:
    if not manifest_path.is_absolute() or manifest_path.name != "manifest.json":
        raise ReleaseManagerError("staging manifest path must be absolute")
    _validate_staging_directory(manifest_path.parent.parent, expected_uid)
    _validate_staging_directory(manifest_path.parent, expected_uid)
    try:
        manifest_info = manifest_path.lstat()
    except OSError as exc:
        raise ReleaseManagerError("staging manifest is unavailable") from exc
    if (
        stat.S_ISLNK(manifest_info.st_mode)
        or not stat.S_ISREG(manifest_info.st_mode)
        or manifest_info.st_uid != expected_uid
        or stat.S_IMODE(manifest_info.st_mode) != 0o600
    ):
        raise ReleaseManagerError("staging manifest ownership or mode is unsafe")
    try:
        manifest_bytes = _read_safe_bytes(
            manifest_path,
            expected_uid=expected_uid,
            expected_mode=0o600,
            maximum=_MAX_JSON_BYTES,
        )
        manifest = load_manifest_bytes(manifest_bytes)
    except ReleaseManifestError as exc:
        raise ReleaseManagerError("staging manifest is invalid") from exc
    expected = _expected_bundle_names(manifest)
    try:
        entries = list(os.scandir(manifest_path.parent))
    except OSError as exc:
        raise ReleaseManagerError("staging bundle inventory is unavailable") from exc
    if {entry.name for entry in entries} != expected:
        raise ReleaseManagerError("staging bundle is not a closed file set")
    for entry in entries:
        info = entry.stat(follow_symlinks=False)
        if (
            entry.is_symlink()
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != expected_uid
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ReleaseManagerError("staging bundle file ownership or mode is unsafe")
    return manifest, manifest_bytes, [manifest_path.parent / name for name in sorted(expected)]


def _copy_atomic(source: Path, destination: Path) -> None:
    source_info = source.lstat()
    if source_info.st_mode is None or not stat.S_ISREG(source_info.st_mode):
        raise ReleaseManagerError("staging source is not a regular file")
    try:
        destination_info = destination.lstat()
    except FileNotFoundError:
        destination_info = None
    if destination_info is not None:
        if stat.S_ISLNK(destination_info.st_mode) or not stat.S_ISREG(destination_info.st_mode):
            raise ReleaseManagerError("stored artifact path is unsafe")
        if not hmac.compare_digest(_hash_file(source), _hash_file(destination)):
            raise ReleaseManagerError("stored artifact differs from staging")
        return
    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    source_fd = os.open(source, os.O_RDONLY | _NOFOLLOW)
    destination_fd = -1
    try:
        opened = os.fstat(source_fd)
        if (opened.st_dev, opened.st_ino) != (source_info.st_dev, source_info.st_ino):
            raise ReleaseManagerError("staging source changed while opening")
        destination_fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
            0o600,
        )
        os.fchmod(destination_fd, 0o600)
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                if written <= 0:
                    raise OSError("artifact copy made no progress")
                view = view[written:]
        os.fsync(destination_fd)
        os.close(destination_fd)
        destination_fd = -1
        os.replace(temporary, destination)
        os.chmod(destination, 0o600, follow_symlinks=False)
        directory_fd = os.open(destination.parent, os.O_RDONLY | _DIRECTORY | _NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.close(source_fd)
        if destination_fd >= 0:
            os.close(destination_fd)
        with suppress(FileNotFoundError):
            temporary.unlink()


def _parse_utc(value: object, context: str) -> datetime:
    if type(value) is not str:
        raise ReleaseManagerError(f"{context} must be a timezone-aware UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReleaseManagerError(f"{context} must be a timezone-aware UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ReleaseManagerError(f"{context} must be a timezone-aware UTC timestamp")
    return parsed


class ReleaseManager:
    """在现有 lifecycle 控制面后持久化并验证单次发布。"""

    def __init__(
        self,
        *,
        root: Path,
        release_root: Path,
        mode: Literal["development", "production"],
        runner: CommandRunner | None = None,
        expected_staging_uid: int | None = None,
    ) -> None:
        self.root = root.absolute()
        self.release_root = release_root.absolute()
        self.mode = mode
        self.runner = runner or SubprocessRunner()
        self.expected_staging_uid = expected_staging_uid
        self._active_store: ReleaseStore | None = None
        self._stop_signal: int | None = None

    def request_stop(self, signum: int, _frame: object | None) -> None:
        """信号处理器只记录停止请求；持久化发生在安全步骤边界。"""

        if signum not in {signal.SIGTERM, signal.SIGINT, signal.SIGHUP}:
            raise ReleaseManagerError("unsupported release stop signal")
        self._stop_signal = signum

    def _ensure_not_stopped(
        self,
        store: ReleaseStore,
        state: ReleaseState,
        *,
        next_step: str,
    ) -> None:
        if self._stop_signal is None:
            return
        store.checkpoint(
            state,
            interrupted_signal=signal.Signals(self._stop_signal).name,
            next_step=next_step,
        )
        raise _ReleaseInterrupted("release execution interrupted")

    def _staging_uid(self) -> int:
        if self.expected_staging_uid is not None:
            return self.expected_staging_uid
        sudo_uid = os.environ.get("SUDO_UID")
        if sudo_uid is not None:
            if not sudo_uid.isdecimal():
                raise ReleaseManagerError("staging SUDO_UID is invalid")
            return int(sudo_uid)
        if self.mode == "production" and os.geteuid() == 0:
            raise ReleaseManagerError("production staging owner is unavailable")
        return os.geteuid()

    def _compose(self) -> list[str]:
        return [
            "docker",
            "compose",
            "--env-file",
            str(self.root / ".env"),
            "-f",
            str(self.root / "deploy" / "docker-compose.yml"),
        ]

    def _run(self, argv: Sequence[str], context: str) -> subprocess.CompletedProcess[str]:
        if self._active_store is not None:
            self._active_store.record_intent(
                "external_command",
                {"check": context},
            )
        result = self.runner.run(argv, cwd=self.root)
        if result.returncode != 0:
            raise ReleaseManagerError(f"{context} failed")
        if self._active_store is not None:
            self._active_store.record_observation(
                "external_command",
                {"check": context, "passed": True},
            )
        return result

    @staticmethod
    def _line(result: subprocess.CompletedProcess[str], context: str) -> str:
        value = result.stdout
        if value.endswith("\n"):
            value = value[:-1]
        if not value or "\n" in value or "\r" in value:
            raise ReleaseManagerError(f"{context} returned invalid output")
        return value

    def _copy_bundle(
        self,
        store: ReleaseStore,
        manifest_path: Path,
        files: Sequence[Path],
    ) -> None:
        artifacts = store.release_dir / "artifacts"
        for source in files:
            if source == manifest_path:
                continue
            _copy_atomic(source, artifacts / source.name)

    def _validate_git(self, manifest: ReleaseManifest) -> str:
        status = self._run(
            [
                "git",
                "--no-optional-locks",
                "-C",
                str(self.root),
                "status",
                "--porcelain",
                "--untracked-files=normal",
            ],
            "Git cleanliness check",
        )
        if status.stdout:
            raise ReleaseManagerError("Git worktree is not clean")
        commit = self._line(
            self._run(
                ["git", "--no-optional-locks", "-C", str(self.root), "rev-parse", "HEAD"],
                "Git commit check",
            ),
            "Git commit check",
        )
        if not hmac.compare_digest(commit, manifest.commit):
            raise ReleaseManagerError("Git commit does not match the manifest")
        return commit

    def _root_env_refs(self) -> dict[str, str]:
        try:
            lines = (self.root / ".env").read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise ReleaseManagerError("root env is unavailable") from exc
        by_key = {value: name for name, value in _ENV_IMAGE_KEYS.items()}
        refs: dict[str, str] = {}
        seen: set[str] = set()
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key not in by_key:
                continue
            if key in seen or not value or value != value.strip():
                raise ReleaseManagerError("root env image references are invalid")
            seen.add(key)
            refs[by_key[key]] = value
        if seen != set(by_key):
            raise ReleaseManagerError("root env must define exactly four image references")
        for ref in refs.values():
            if self.mode == "production":
                valid = _REPO_DIGEST_RE.fullmatch(ref) is not None
            else:
                valid = (
                    _SAFE_DEVELOPMENT_REF_RE.fullmatch(ref) is not None
                    and ".." not in ref
                    and "//" not in ref
                )
            if not valid:
                raise ReleaseManagerError("root env image reference is unsafe")
        return refs

    def _current_refs(self, manifest: ReleaseManifest) -> dict[str, str]:
        refs = self._root_env_refs()
        try:
            validate_changed_images(manifest, refs)
        except ReleaseManifestError as exc:
            raise ReleaseManagerError("changed images do not match root env") from exc
        return refs

    def _validate_control_smoke_context(self, manifest: ReleaseManifest) -> None:
        if manifest.mode != "development" or self.mode != "development":
            raise ReleaseManagerError(
                "control smoke release evidence is only valid for development"
            )
        if os.environ.get("SMS_RELEASE_SMOKE") != "1":
            raise ReleaseManagerError("control smoke context is not fully gated")
        if os.environ.get("SMS_RELEASE_ROOT") != str(self.release_root):
            raise ReleaseManagerError("control smoke context is not fully gated")
        if self.release_root == _PRODUCTION_RELEASE_ROOT:
            raise ReleaseManagerError("control smoke context is not fully gated")
        validated_root = _validate_cli_release_root(
            self.release_root,
            mode="development",
            platform_root=self.root,
        )
        if validated_root == _PRODUCTION_RELEASE_ROOT:
            raise ReleaseManagerError("control smoke context is not fully gated")
        runtime_value = os.environ.get("SMS_RUNTIME_ROOT")
        if not runtime_value:
            raise ReleaseManagerError("control smoke context is not fully gated")
        runtime_root = Path(runtime_value)
        raw_runtime = str(runtime_root)
        if (
            not runtime_root.is_absolute()
            or ".." in runtime_root.parts
            or runtime_root.name != _RUNTIME_ROOT_NAME
            or runtime_root.parent != self.release_root.parent
            or any(ord(character) < 32 or ord(character) == 127 for character in raw_runtime)
        ):
            raise ReleaseManagerError("control smoke context is not fully gated")
        try:
            runtime_info = runtime_root.lstat()
            runtime_resolved = runtime_root.resolve(strict=True)
            parent_resolved = self.release_root.parent.resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise ReleaseManagerError("control smoke context is not fully gated") from exc
        if (
            stat.S_ISLNK(runtime_info.st_mode)
            or not stat.S_ISDIR(runtime_info.st_mode)
            or runtime_info.st_uid != os.geteuid()
            or stat.S_IMODE(runtime_info.st_mode) != 0o700
            or runtime_resolved.parent != parent_resolved
        ):
            raise ReleaseManagerError("control smoke context is not fully gated")
        compose_project = os.environ.get("COMPOSE_PROJECT_NAME")
        if (
            type(compose_project) is not str
            or compose_project != self.release_root.parent.name
            or _SMOKE_PARENT_RE.fullmatch(compose_project) is None
        ):
            raise ReleaseManagerError("control smoke context is not fully gated")

    def _validate_control_smoke_evidence(
        self,
        manifest: ReleaseManifest,
        artifacts: Path,
    ) -> None:
        path = artifacts / cast(str, manifest.evidence["release_gate"])
        report = _exact_object(
            _read_bound_json(
                path,
                cast(str, manifest.evidence["release_gate_sha256"]),
                "control smoke evidence",
            ),
            _CONTROL_SMOKE_REPORT_FIELDS,
            "control smoke evidence",
        )
        if (
            report["schema_version"] != 1
            or report["gate_type"] != "release_control_smoke"
            or report["candidate_commit"] != manifest.commit
            or report["purpose"] != _CONTROL_SMOKE_PURPOSE
            or report["scan_performed"] is not False
            or report["authorized_for_control_smoke"] is not True
        ):
            raise ReleaseManagerError("control smoke evidence is not a bound authorized report")
        _parse_utc(report["generated_at"], "control smoke evidence generated_at")
        images = _exact_object(
            report["images"],
            frozenset(_IMAGE_NAMES),
            "control smoke evidence images",
        )
        for name in _IMAGE_NAMES:
            image = _exact_object(
                images[name],
                _CONTROL_SMOKE_IMAGE_FIELDS,
                "control smoke image evidence",
            )
            if (
                image["ref"] != manifest.images[name].ref
                or image["image_id"] != manifest.images[name].image_id
                or image["platform"] != "linux/amd64"
            ):
                raise ReleaseManagerError("control smoke image evidence is not bound")

    def _validate_release_evidence(
        self,
        manifest: ReleaseManifest,
        artifacts: Path,
    ) -> tuple[str, bool]:
        gate_kind = cast(str, manifest.evidence["release_gate_kind"])
        if gate_kind == "release_control_smoke":
            self._validate_control_smoke_context(manifest)
            self._validate_control_smoke_evidence(manifest, artifacts)
            return gate_kind, False
        path = artifacts / cast(str, manifest.evidence["release_gate"])
        report = _exact_object(
            _read_bound_json(
                path,
                cast(str, manifest.evidence["release_gate_sha256"]),
                "release evidence",
            ),
            _RELEASE_REPORT_FIELDS,
            "release evidence",
        )
        if (
            report["schema_version"] != 1
            or report["gate_type"] != "release"
            or report["passed"] is not True
            or report["candidate_commit"] != manifest.commit
        ):
            raise ReleaseManagerError("release evidence is not a passing bound report")
        _parse_utc(report["generated_at"], "release evidence generated_at")
        release_source = self._validate_release_source(
            report["source"],
            manifest,
            "release source evidence",
        )
        if (
            type(report["trivy_image"]) is not str
            or _REPO_DIGEST_RE.fullmatch(report["trivy_image"]) is None
        ):
            raise ReleaseManagerError("release evidence scanner is not digest pinned")
        images = _exact_object(
            report["images"],
            frozenset(_IMAGE_NAMES),
            "release evidence images",
        )
        for name in _IMAGE_NAMES:
            image = _exact_object(
                images[name],
                _RELEASE_IMAGE_FIELDS,
                "release image evidence",
            )
            repo_digests = image["repo_digests"]
            if type(repo_digests) is not list or not all(
                type(value) is str and _REPO_DIGEST_RE.fullmatch(value) is not None
                for value in repo_digests
            ):
                raise ReleaseManagerError("release RepoDigests evidence is invalid")
            if (
                image["ref"] != manifest.images[name].ref
                or image["image_id"] != manifest.images[name].image_id
                or type(image["scan_report_sha256"]) is not str
                or _REPORT_HASH_RE.fullmatch(image["scan_report_sha256"]) is None
                or image["scan_passed"] is not True
            ):
                raise ReleaseManagerError("release image evidence is not bound")
            if self.mode == "production" and manifest.images[name].ref not in repo_digests:
                raise ReleaseManagerError("production release evidence lacks target RepoDigest")
        promotion = report["promotion_source"]
        if self.mode == "development":
            if promotion is not None:
                raise ReleaseManagerError("development release evidence cannot be promoted")
            return gate_kind, True
        source = _exact_object(
            promotion,
            _PROMOTION_SOURCE_FIELDS,
            "promotion source evidence",
        )
        if (
            source["candidate_commit"] != manifest.commit
            or type(source["report_sha256"]) is not str
            or _REPORT_HASH_RE.fullmatch(source["report_sha256"]) is None
        ):
            raise ReleaseManagerError("promotion source evidence is not bound")
        promoted_source = self._validate_release_source(
            source["source"],
            manifest,
            "promotion source metadata",
        )
        if promoted_source != release_source:
            raise ReleaseManagerError("promotion source metadata is not bound")
        source_images = _exact_object(
            source["images"],
            frozenset(_IMAGE_NAMES),
            "promotion source images",
        )
        for name in _IMAGE_NAMES:
            source_image = _exact_object(
                source_images[name],
                _PROMOTION_IMAGE_FIELDS,
                "promotion source image",
            )
            if (
                source_image["ref"] != f"sms-platform-release-{name}:{manifest.commit}"
                or source_image["image_id"] != manifest.images[name].image_id
                or type(source_image["scan_report_sha256"]) is not str
                or _REPORT_HASH_RE.fullmatch(source_image["scan_report_sha256"]) is None
            ):
                raise ReleaseManagerError("promotion source image is not bound")
        return gate_kind, True

    def _validate_release_source(
        self,
        value: object,
        manifest: ReleaseManifest,
        context: str,
    ) -> dict[str, Any]:
        source = _exact_object(value, _RELEASE_SOURCE_FIELDS, context)
        sboms = _exact_object(
            source["sbom_sha256"],
            frozenset(_IMAGE_NAMES),
            f"{context} SBOMs",
        )
        workflow_repository = source["workflow_repository"]
        if (
            type(source["app_version"]) is not str
            or _APP_VERSION_RE.fullmatch(source["app_version"]) is None
            or source["git_sha"] != manifest.commit
            or source["schema_revision"] != manifest.migration_target
            or type(source["openapi_sha256"]) is not str
            or _REPORT_HASH_RE.fullmatch(source["openapi_sha256"]) is None
            or type(workflow_repository) is not str
            or _WORKFLOW_REPOSITORY_RE.fullmatch(workflow_repository) is None
            or (self.mode == "production" and workflow_repository == "local")
            or type(source["workflow_run_id"]) is not int
            or type(source["workflow_run_attempt"]) is not int
            or source["workflow_run_id"] < (1 if workflow_repository != "local" else 0)
            or source["workflow_run_attempt"]
            < (1 if workflow_repository != "local" else 0)
            or any(
                type(digest) is not str
                or _REPORT_HASH_RE.fullmatch(digest) is None
                for digest in sboms.values()
            )
        ):
            raise ReleaseManagerError(f"{context} is invalid")
        return source

    def _validate_data_evidence(
        self,
        manifest: ReleaseManifest,
        artifacts: Path,
    ) -> dict[str, dict[str, object]] | None:
        filename = manifest.evidence["data_images"]
        if filename is None:
            return None
        report = _exact_object(
            _read_json(artifacts / cast(str, filename), "data image evidence"),
            _DATA_REPORT_FIELDS,
            "data image evidence",
        )
        if (
            report["schema_version"] != 1
            or report["gate_type"] != "data_images"
            or report["passed"] is not True
            or report["candidate_commit"] != manifest.commit
        ):
            raise ReleaseManagerError("data image evidence is not a passing bound report")
        _parse_utc(report["generated_at"], "data image evidence generated_at")
        checks = _exact_object(report["checks"], _DATA_CHECK_FIELDS, "data image checks")
        if any(checks[name] is not True for name in _DATA_CHECK_FIELDS):
            raise ReleaseManagerError("data image checks did not all pass")
        images = _exact_object(
            report["images"],
            frozenset({"postgres", "redis"}),
            "data image evidence images",
        )
        validated: dict[str, dict[str, object]] = {}
        for name in ("postgres", "redis"):
            image = _exact_object(images[name], _DATA_IMAGE_FIELDS, "data image evidence")
            version = image["version"]
            major = image["major"]
            if (
                type(version) is not str
                or type(major) is not int
                or major <= 0
                or (
                    name == "postgres"
                    and _POSTGRES_NORMALIZED_VERSION_RE.fullmatch(version) is None
                )
                or (name == "redis" and _REDIS_NORMALIZED_VERSION_RE.fullmatch(version) is None)
                or int(version.split(".", 1)[0]) != major
            ):
                raise ReleaseManagerError("data image version evidence is invalid")
            if (
                image["ref"] != manifest.images[name].ref
                or image["image_id"] != manifest.images[name].image_id
                or image["platform"] != "linux/amd64"
            ):
                raise ReleaseManagerError("data image evidence is not bound")
            validated[name] = image
        return validated

    def _validate_backup_evidence(
        self,
        manifest: ReleaseManifest,
        artifacts: Path,
    ) -> None:
        binding = manifest.evidence["backup_restore_change"]
        if binding is None:
            return
        if not isinstance(binding, Mapping):
            raise ReleaseManagerError("backup evidence mapping is invalid")
        change_path = artifacts / binding["record"]
        report_path = artifacts / binding["restore_report"]
        change = _exact_object(
            _read_json(change_path, "backup change record"),
            _CHANGE_FIELDS,
            "backup change record",
        )
        approval = _exact_object(change["approval"], _APPROVAL_FIELDS, "backup approval")
        restore_binding = _exact_object(
            change["restore"],
            _RESTORE_BINDING_FIELDS,
            "backup restore binding",
        )
        if (
            change["schema_version"] != 1
            or change["record_type"] != "postgres_backup_restore_change"
            or change["release_id"] != manifest.release_id
            or change["target_commit"] != manifest.commit
            or change["target_postgres_image_id"] != manifest.images["postgres"].image_id
            or approval["status"] != "approved"
        ):
            raise ReleaseManagerError("backup change record is not approved and bound")
        _safe_id(change["change_id"], "change id")
        _safe_id(approval["approved_by"], "approver")
        _safe_id(restore_binding["snapshot_id"], "snapshot id")
        report_hash = restore_binding["report_sha256"]
        if type(report_hash) is not str or _REPORT_HASH_RE.fullmatch(report_hash) is None:
            raise ReleaseManagerError("restore report binding is invalid")
        if not hmac.compare_digest(_hash_file(report_path), report_hash):
            raise ReleaseManagerError("restore report does not match its change record")
        report = _exact_object(
            _read_json(report_path, "restore report"),
            _RESTORE_REPORT_FIELDS,
            "restore report",
        )
        checks = _exact_object(report["checks"], _RESTORE_CHECK_FIELDS, "restore checks")
        counts = _exact_object(report["table_counts"], _TABLE_COUNT_FIELDS, "table counts")
        if (
            report["schema_version"] != 1
            or report["status"] != "success"
            or report["within_rto"] is not True
            or type(report["database"]) is not str
            or _DRILL_DATABASE_RE.fullmatch(report["database"]) is None
            or report["git_commit"] != manifest.commit
            or report["snapshot_id"] != restore_binding["snapshot_id"]
            or checks["role_flags"] != "false|false|false"
            or checks["audit_privileges"] != "true|false|false"
            or checks["alembic_version"] != manifest.migration_from
            or any(
                type(counts[name]) is not int or counts[name] < 0 for name in _TABLE_COUNT_FIELDS
            )
        ):
            raise ReleaseManagerError("restore report does not satisfy the release contract")
        for duration_field in ("restore_seconds", "rto_limit_seconds"):
            duration = report[duration_field]
            if type(duration) not in {int, float} or duration < 0:
                raise ReleaseManagerError("restore report durations are invalid")
        started = _parse_utc(report["started_at"], "restore started_at")
        finished = _parse_utc(report["finished_at"], "restore finished_at")
        approved = _parse_utc(approval["approved_at"], "approval approved_at")
        if not started <= finished <= approved:
            raise ReleaseManagerError("restore and approval timestamps are out of order")

    def _target_images(
        self,
        manifest: ReleaseManifest,
        artifacts: Path,
    ) -> dict[str, str]:
        if self.mode == "development":
            for spec in manifest.images.values():
                if spec.changed:
                    if spec.archive_file is None:
                        raise ReleaseManagerError("development archive is unavailable")
                    archive = artifacts / spec.archive_file
                    if spec.archive_sha256 is None or not hmac.compare_digest(
                        _hash_file(archive), spec.archive_sha256
                    ):
                        raise ReleaseManagerError("development archive is not bound")
                    self._run(
                        ["docker", "load", "--input", str(archive)],
                        "development image load",
                    )
        target_ids: dict[str, str] = {}
        for name in _IMAGE_NAMES:
            spec = manifest.images[name]
            result = self._run(
                [
                    "docker",
                    "image",
                    "inspect",
                    "--format",
                    "{{.Id}} {{.Os}}/{{.Architecture}} {{json .RepoDigests}}",
                    spec.ref,
                ],
                "target image inspection",
            )
            fields = self._line(result, "target image inspection").split(" ", 2)
            if len(fields) != 3 or fields[1] != "linux/amd64":
                raise ReleaseManagerError("target image platform is invalid")
            image_id, _, raw_digests = fields
            if _IMAGE_ID_RE.fullmatch(image_id) is None or not hmac.compare_digest(
                image_id, spec.image_id
            ):
                raise ReleaseManagerError("target image ID does not match the manifest")
            try:
                repo_digests = json.loads(raw_digests)
            except json.JSONDecodeError as exc:
                raise ReleaseManagerError("target RepoDigests are invalid") from exc
            if type(repo_digests) is not list or not all(
                type(value) is str for value in repo_digests
            ):
                raise ReleaseManagerError("target RepoDigests are invalid")
            if self.mode == "production" and spec.ref not in repo_digests:
                raise ReleaseManagerError("production target RepoDigest is not preloaded")
            target_ids[name] = image_id
        return target_ids

    def _current_runtime(
        self,
        current_refs: Mapping[str, str],
    ) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
        compose = self._compose()
        container_ids: dict[str, str] = {}
        image_ids: dict[str, str] = {}
        service_container_ids: dict[str, str] = {}
        for service in _RUNTIME_SERVICES:
            image_name = _service_image_name(service)
            container = self._line(
                self._run(compose + ["ps", "-q", service], "running container lookup"),
                "running container lookup",
            )
            if re.fullmatch(r"[A-Za-z0-9_.-]+", container) is None:
                raise ReleaseManagerError("running container identifier is invalid")
            inspected = self._line(
                self._run(
                    [
                        "docker",
                        "inspect",
                        "--format",
                        "{{.Id}} {{.Image}} {{.Config.Image}}",
                        container,
                    ],
                    "running container inspection",
                ),
                "running container inspection",
            ).split()
            if (
                len(inspected) != 3
                or _CONTAINER_ID_RE.fullmatch(inspected[0]) is None
                or _IMAGE_ID_RE.fullmatch(inspected[1]) is None
                or inspected[2] != current_refs[image_name]
            ):
                raise ReleaseManagerError("running container does not match root env")
            service_container_ids[service] = inspected[0]
            if image_name in container_ids:
                if not hmac.compare_digest(inspected[1], image_ids[image_name]):
                    raise ReleaseManagerError("backend services do not share the API image")
                continue
            container_ids[image_name] = inspected[0]
            image_ids[image_name] = inspected[1]
        return container_ids, image_ids, service_container_ids

    def _validate_data_majors(
        self,
        evidence: Mapping[str, Mapping[str, object]] | None,
    ) -> None:
        if evidence is None:
            return
        compose = self._compose()
        postgres = self._line(
            self._run(
                compose + ["exec", "-T", "postgres", "postgres", "--version"],
                "current PostgreSQL version observation",
            ),
            "current PostgreSQL version observation",
        )
        redis = self._line(
            self._run(
                compose + ["exec", "-T", "redis", "redis-server", "--version"],
                "current Redis version observation",
            ),
            "current Redis version observation",
        )
        try:
            _, postgres_major = parse_postgres_version_output(postgres)
            _, redis_major = parse_redis_version_output(redis)
        except (ReleaseEvidenceError, ValueError) as exc:
            raise ReleaseManagerError("current data binary version output is invalid") from exc
        if (
            evidence["postgres"]["major"] != postgres_major
            or evidence["redis"]["major"] != redis_major
        ):
            raise ReleaseManagerError("cross-major data image release is forbidden")

    def _migration_head(self, manifest: ReleaseManifest) -> str:
        probe = (
            "exec psql --no-psqlrc --set=ON_ERROR_STOP=1 --tuples-only --no-align "
            '--username "$POSTGRES_USER" --dbname "$POSTGRES_DB" '
            '--command "SELECT version_num FROM alembic_version"'
        )
        result = self._line(
            self._run(
                self._compose() + ["exec", "-T", "postgres", "sh", "-ec", probe],
                "current migration observation",
            ),
            "current migration observation",
        )
        if _MIGRATION_RE.fullmatch(result) is None or result != manifest.migration_from:
            raise ReleaseManagerError("current migration does not match manifest migration.from")
        return result

    def _write_snapshot(
        self,
        store: ReleaseStore,
        *,
        current_commit: str,
        current_refs: Mapping[str, str],
        container_ids: Mapping[str, str],
        image_ids: Mapping[str, str],
        migration_head: str,
        manifest: ReleaseManifest,
        service_container_ids: Mapping[str, str],
        target_image_ids: Mapping[str, str],
    ) -> None:
        payload = {
            "current_commit": current_commit,
            "current_refs": dict(current_refs),
            "container_ids": dict(container_ids),
            "image_ids": dict(image_ids),
            "migration_head": migration_head,
            "service_container_ids": dict(service_container_ids),
            "target_commit": manifest.commit,
            "target_image_ids": dict(target_image_ids),
        }
        rendered = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
        ReleaseStore._atomic_write(store.release_dir / "current-snapshot.json", rendered)

    def prepare(self, manifest_path: Path) -> None:
        """验证并持久化发布；任一失败都不修改 env 或运行容器。"""

        manifest, manifest_bytes, staging_files = _validate_staging_bundle(
            manifest_path,
            self._staging_uid(),
        )
        if manifest.mode != self.mode:
            raise ReleaseManagerError("staging manifest mode does not match manager mode")
        store = ReleaseStore(self.release_root, manifest.release_id)
        try:
            store.create(manifest_bytes)
            state = store.read_state()
            if state.get("state") == ReleaseState.PREPARED.value:
                return
            if state.get("state") != ReleaseState.STAGED.value:
                raise ReleaseManagerError("release is not in a preparable state")
            self._active_store = store
            self._copy_bundle(store, manifest_path, staging_files)
            artifacts = store.release_dir / "artifacts"
            current_commit = self._validate_git(manifest)
            current_refs = self._current_refs(manifest)
            release_gate_kind, release_scan_performed = self._validate_release_evidence(
                manifest,
                artifacts,
            )
            data_evidence = self._validate_data_evidence(manifest, artifacts)
            self._validate_backup_evidence(manifest, artifacts)
            target_ids = self._target_images(manifest, artifacts)
            container_ids, image_ids, service_container_ids = self._current_runtime(current_refs)
            self._validate_data_majors(data_evidence)
            migration_head = self._migration_head(manifest)
            self._write_snapshot(
                store,
                current_commit=current_commit,
                current_refs=current_refs,
                container_ids=container_ids,
                image_ids=image_ids,
                migration_head=migration_head,
                manifest=manifest,
                service_container_ids=service_container_ids,
                target_image_ids=target_ids,
            )
            store.transition(
                ReleaseState.STAGED,
                ReleaseState.PREPARED,
                prepared_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                release_gate_kind=release_gate_kind,
                control_smoke_only=release_gate_kind == "release_control_smoke",
                release_scan_performed=release_scan_performed,
            )
        except Exception as exc:
            with suppress(Exception):
                if store.read_state().get("state") == ReleaseState.STAGED.value:
                    store.transition(
                        ReleaseState.STAGED,
                        ReleaseState.FAILED,
                        failure_type=type(exc).__name__,
                    )
            raise ReleaseManagerError(f"release prepare failed ({type(exc).__name__})") from exc
        finally:
            self._active_store = None

    @staticmethod
    def _render_env_refs(original: bytes, refs: Mapping[str, str]) -> bytes:
        try:
            text = original.decode("utf-8")
        except UnicodeError as exc:
            raise ReleaseManagerError("root env is not valid UTF-8") from exc
        by_key = {value: name for name, value in _ENV_IMAGE_KEYS.items()}
        seen: set[str] = set()
        rendered: list[str] = []
        for line in text.splitlines(keepends=True):
            body = line.rstrip("\r\n")
            ending = line[len(body) :]
            if "=" not in body:
                rendered.append(line)
                continue
            prefix, _ = body.split("=", 1)
            key = prefix.strip()
            if key not in by_key:
                rendered.append(line)
                continue
            if key in seen:
                raise ReleaseManagerError("root env image references are duplicated")
            seen.add(key)
            name = by_key[key]
            rendered.append(f"{prefix}={refs[name]}{ending}")
        if seen != set(by_key):
            raise ReleaseManagerError("root env must define exactly four image references")
        return "".join(rendered).encode()

    @staticmethod
    def _render_target_env(original: bytes, manifest: ReleaseManifest) -> bytes:
        return ReleaseManager._render_env_refs(
            original,
            {name: manifest.images[name].ref for name in _IMAGE_NAMES},
        )

    def _stored_manifest(self, store: ReleaseStore) -> ReleaseManifest:
        try:
            return load_manifest(store.release_dir / "manifest.json")
        except ReleaseManifestError as exc:
            raise ReleaseManagerError("stored release manifest is invalid") from exc

    def configure_activation(self, release_id: str) -> tuple[ReleaseStep, ...]:
        """原子写入四镜像目标引用并验证 Compose 配置，不改变容器。"""

        store = ReleaseStore(self.release_root, release_id)
        env_path = self.root / ".env"
        try:
            state = store.read_state()
            if state.get("state") not in {
                ReleaseState.PREPARED.value,
                ReleaseState.ACTIVATING.value,
            }:
                raise ReleaseManagerError("release is not prepared for configuration")
            manifest = self._stored_manifest(store)
            if manifest.mode != self.mode:
                raise ReleaseManagerError("stored manifest mode does not match manager mode")
            self._current_refs(manifest)
            original = _read_safe_bytes(
                env_path,
                expected_uid=os.geteuid(),
                maximum=_MAX_JSON_BYTES,
            )
            target = self._render_target_env(original, manifest)
            store.snapshot_env(env_path)
            mode = stat.S_IMODE(env_path.stat(follow_symlinks=False).st_mode)
            store.record_intent("env_replace", {"source": "manifest"})
            ReleaseStore._atomic_write(
                env_path,
                target,
                mode=mode,
                private_parent=False,
            )
            store.record_observation("env_replace", {"completed": True})
            self._active_store = store
            self._run(self._compose() + ["config", "--quiet"], "activation configuration")
            store.record_observation("compose_config", {"completed": True})
            return build_activation_plan(manifest)
        except Exception as exc:
            with suppress(Exception):
                store.restore_env(env_path)
            if isinstance(exc, ReleaseManagerError) and "configuration" in str(exc):
                raise
            raise ReleaseManagerError(
                f"activation configuration failed ({type(exc).__name__})"
            ) from exc
        finally:
            self._active_store = None

    @staticmethod
    def _read_events(store: ReleaseStore) -> list[dict[str, Any]]:
        raw = _read_safe_bytes(
            store.release_dir / "events.jsonl",
            expected_uid=os.geteuid(),
            expected_mode=0o600,
            maximum=_MAX_JSON_BYTES,
        )
        events: list[dict[str, Any]] = []
        for line in raw.splitlines():
            if not line:
                continue
            try:
                event = json.loads(line, object_pairs_hook=_reject_duplicate_keys)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise ReleaseManagerError("release events are invalid") from exc
            if type(event) is not dict or set(event) != {
                "kind",
                "step",
                "details",
                "timestamp",
            }:
                raise ReleaseManagerError("release events are invalid")
            if (
                event["kind"] not in {"intent", "observation"}
                or type(event["step"]) is not str
                or type(event["details"]) is not dict
                or type(event["timestamp"]) is not str
            ):
                raise ReleaseManagerError("release events are invalid")
            events.append(cast(dict[str, Any], event))
        return events

    @classmethod
    def _completed_steps(cls, store: ReleaseStore) -> set[ReleaseStepKind]:
        completed: set[ReleaseStepKind] = set()
        for event in cls._read_events(store):
            if event["kind"] != "observation":
                continue
            details = cast(dict[str, Any], event["details"])
            if details.get("completed") is not True:
                continue
            try:
                completed.add(ReleaseStepKind(event["step"]))
            except ValueError:
                continue
        return completed

    def _run_activation_step(
        self,
        store: ReleaseStore,
        manifest: ReleaseManifest,
        step: ReleaseStep,
        command: Sequence[str],
    ) -> None:
        try:
            store.record_intent(step.kind.value, {"services": list(step.services)})
        except Exception as exc:
            raise _ActivationStepError(step.kind) from exc
        if step.kind is ReleaseStepKind.VERIFY:
            try:
                self._verify_final_runtime(store, manifest)
            except _FinalRuntimeError as exc:
                raise _ActivationStepError(step.kind, ambiguous=exc.ambiguous) from exc
            except Exception as exc:
                raise _ActivationStepError(step.kind, ambiguous=True) from exc
            result = subprocess.CompletedProcess(list(command), 0, "", "")
        else:
            try:
                result = self.runner.run(command, cwd=self.root)
            except Exception as exc:
                raise _ActivationStepError(step.kind) from exc
        if result.returncode == 0:
            if step.kind is ReleaseStepKind.RUN_MIGRATE:
                try:
                    migration_state = self._observe_migration_state(store, manifest)
                except Exception as exc:
                    raise _ActivationStepError(step.kind, ambiguous=True) from exc
                if migration_state != "target":
                    raise _ActivationStepError(step.kind, ambiguous=True)
            try:
                store.record_observation(
                    step.kind.value,
                    {"completed": True, "services": list(step.services)},
                )
            except Exception as exc:
                raise _ActivationStepError(step.kind, ambiguous=True) from exc
            return
        ambiguous = False
        if step.kind in {
            ReleaseStepKind.RECREATE_POSTGRES,
            ReleaseStepKind.RECREATE_REDIS,
            ReleaseStepKind.RECREATE_BACKEND,
            ReleaseStepKind.RECREATE_WEB,
        }:
            try:
                runtime = self._observe_step_runtime(store, manifest, step)
                if runtime == "target":
                    store.record_observation(
                        step.kind.value,
                        {
                            "completed": True,
                            "healthy": False,
                            "services": list(step.services),
                        },
                    )
                elif runtime != "original":
                    ambiguous = True
            except Exception as exc:
                raise _ActivationStepError(step.kind, ambiguous=True) from exc
        raise _ActivationStepError(step.kind, ambiguous=ambiguous)

    def _read_snapshot(self, store: ReleaseStore) -> dict[str, Any]:
        snapshot = _read_json(store.release_dir / "current-snapshot.json", "release snapshot")
        required = {
            "current_commit",
            "current_refs",
            "container_ids",
            "image_ids",
            "migration_head",
            "service_container_ids",
            "target_commit",
            "target_image_ids",
        }
        if set(snapshot) != required:
            raise ReleaseManagerError("release snapshot fields are invalid")
        for field in ("current_refs", "container_ids", "image_ids", "target_image_ids"):
            value = snapshot[field]
            if type(value) is not dict or set(value) != set(_IMAGE_NAMES):
                raise ReleaseManagerError("release snapshot mapping is invalid")
        service_ids = snapshot["service_container_ids"]
        if type(service_ids) is not dict or set(service_ids) != set(_RUNTIME_SERVICES):
            raise ReleaseManagerError("release snapshot service mapping is invalid")
        return snapshot

    @staticmethod
    def _selected_runtime_services(manifest: ReleaseManifest) -> set[str]:
        selected: set[str] = set()
        for step in build_activation_plan(manifest):
            if step.kind in {
                ReleaseStepKind.RECREATE_POSTGRES,
                ReleaseStepKind.RECREATE_REDIS,
                ReleaseStepKind.RECREATE_BACKEND,
                ReleaseStepKind.RECREATE_WEB,
            }:
                selected.update(step.services)
        return selected

    def _verify_final_runtime(
        self,
        store: ReleaseStore,
        manifest: ReleaseManifest,
    ) -> None:
        snapshot = self._read_snapshot(store)
        original_ids = cast(dict[str, str], snapshot["service_container_ids"])
        target_ids = cast(dict[str, str], snapshot["target_image_ids"])
        selected = self._selected_runtime_services(manifest)
        worker_hostnames: dict[str, str] = {}
        verified_container_ids: dict[str, str] = {}
        store.record_intent("final_runtime", {"services": list(_RUNTIME_SERVICES)})

        def fail(reason: str, *, ambiguous: bool = False) -> NoReturn:
            with suppress(Exception):
                store.record_observation(
                    "final_runtime",
                    {"completed": False, "reason": reason},
                )
            raise _FinalRuntimeError(ambiguous=ambiguous)

        for service in _RUNTIME_SERVICES:
            image_name = _service_image_name(service)
            lookup = self.runner.run(
                self._compose() + ["ps", "-q", service],
                cwd=self.root,
            )
            if lookup.returncode != 0:
                fail("container_lookup")
            container = lookup.stdout.strip()
            if not container or re.fullmatch(r"[A-Za-z0-9_.-]+", container) is None:
                fail("container_identifier")
            inspected = self.runner.run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{.Id}} {{.Image}} {{.Config.Image}} "
                    '{{if (index .State "Health")}}'
                    '{{(index .State "Health").Status}}'
                    "{{else}}{{.State.Status}}{{end}} {{.Config.Hostname}}",
                    container,
                ],
                cwd=self.root,
            )
            if inspected.returncode != 0:
                fail("container_inspection")
            try:
                fields = self._line(inspected, "final runtime inspection").split()
            except ReleaseManagerError:
                fail("container_inspection_output", ambiguous=True)
            if (
                len(fields) != 5
                or _CONTAINER_ID_RE.fullmatch(fields[0]) is None
                or _IMAGE_ID_RE.fullmatch(fields[1]) is None
                or fields[2] != manifest.images[image_name].ref
                or not hmac.compare_digest(fields[1], target_ids[image_name])
            ):
                fail("image_binding")
            if service in selected:
                if hmac.compare_digest(fields[0], original_ids[service]):
                    fail("selected_container_identity")
            elif not hmac.compare_digest(fields[0], original_ids[service]):
                fail("unselected_container_identity", ambiguous=True)
            expected_status = "healthy" if service in _HEALTHCHECK_SERVICES else "running"
            if fields[3] != expected_status:
                fail("service_health")
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", fields[4]) is None:
                fail("container_hostname", ambiguous=True)
            if service in _WORKER_SERVICES:
                worker_hostnames[service] = fields[4]
            verified_container_ids[service] = fields[0]

        ping = self.runner.run(
            self._compose()
            + [
                "exec",
                "-T",
                _WORKER_PROBE_SERVICE,
                "celery",
                "-A",
                "app.tasks",
                "inspect",
                "ping",
                "--timeout",
                "10",
                "--json",
            ],
            cwd=self.root,
        )
        if ping.returncode != 0:
            fail("worker_ping_command")
        try:
            replies = json.loads(ping.stdout, object_pairs_hook=_reject_duplicate_keys)
        except (UnicodeError, json.JSONDecodeError):
            fail("worker_ping_output", ambiguous=True)
        expected_workers = {f"celery@{worker_hostnames[service]}" for service in _WORKER_SERVICES}
        if type(replies) is not dict or set(replies) != expected_workers:
            fail("worker_ping_membership")
        for reply in replies.values():
            if type(reply) is not dict or reply != {"ok": "pong"}:
                fail("worker_ping_reply")

        for service in _RUNTIME_SERVICES:
            lookup = self.runner.run(
                self._compose() + ["ps", "--all", "-q", service],
                cwd=self.root,
            )
            if lookup.returncode != 0 or lookup.stdout.strip() != verified_container_ids[service]:
                fail("post_ping_container_identity", ambiguous=True)
            inspected = self.runner.run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    '{{.Id}} {{if (index .State "Health")}}'
                    '{{(index .State "Health").Status}}'
                    "{{else}}{{.State.Status}}{{end}}",
                    verified_container_ids[service],
                ],
                cwd=self.root,
            )
            if inspected.returncode != 0:
                fail("post_ping_container_inspection", ambiguous=True)
            try:
                post_ping_fields = self._line(
                    inspected,
                    "post-ping runtime inspection",
                ).split()
            except ReleaseManagerError:
                fail("post_ping_container_output", ambiguous=True)
            expected_status = "healthy" if service in _HEALTHCHECK_SERVICES else "running"
            if (
                len(post_ping_fields) != 2
                or post_ping_fields[0] != verified_container_ids[service]
                or post_ping_fields[1] != expected_status
            ):
                fail("post_ping_service_health")
        try:
            migration_state = self._observe_migration_state(store, manifest)
        except Exception:
            fail("migration_target", ambiguous=True)
        if migration_state != "target":
            fail("migration_target", ambiguous=True)
        store.record_observation(
            "final_runtime",
            {
                "completed": True,
                "services": list(_RUNTIME_SERVICES),
                "tracked_job_heartbeat": "post_release_operational_check",
            },
        )

    def _observe_step_runtime(
        self,
        store: ReleaseStore,
        manifest: ReleaseManifest,
        step: ReleaseStep,
    ) -> str:
        result, _ = self._observe_step_runtime_status(store, manifest, step)
        return result

    def _observe_step_runtime_status(
        self,
        store: ReleaseStore,
        manifest: ReleaseManifest,
        step: ReleaseStep,
    ) -> tuple[str, bool]:
        snapshot = self._read_snapshot(store)
        image_ids = cast(dict[str, str], snapshot["image_ids"])
        original_refs = cast(dict[str, str], snapshot["current_refs"])
        target_ids = cast(dict[str, str], snapshot["target_image_ids"])
        observed: list[str] = []
        healthy = True
        store.record_intent("runtime_probe", {"group": step.kind.value})
        for service in step.services:
            image_name = _service_image_name(service)
            container_result = self.runner.run(
                self._compose() + ["ps", "--all", "-q", service], cwd=self.root
            )
            if container_result.returncode != 0:
                observed.append("unknown")
                healthy = False
                continue
            container = container_result.stdout.strip()
            if not container:
                observed.append("stopped")
                healthy = False
                continue
            if "\n" in container or "\r" in container:
                observed.append("unknown")
                healthy = False
                continue
            inspect = self.runner.run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    '{{.Image}} {{.Config.Image}} {{if (index .State "Health")}}'
                    '{{(index .State "Health").Status}}'
                    "{{else}}{{.State.Status}}{{end}}",
                    container,
                ],
                cwd=self.root,
            )
            if inspect.returncode != 0:
                observed.append("unknown")
                healthy = False
                continue
            try:
                fields = self._line(inspect, "runtime reconciliation").split()
            except ReleaseManagerError:
                observed.append("unknown")
                healthy = False
                continue
            if len(fields) != 3:
                observed.append("unknown")
                healthy = False
                continue
            image_id, image_ref, status = fields
            if status not in {"healthy", "running"}:
                healthy = False
            if image_ref == manifest.images[image_name].ref and hmac.compare_digest(
                image_id,
                target_ids[image_name],
            ):
                observed.append("target")
            elif image_ref == original_refs[image_name] and hmac.compare_digest(
                image_id,
                image_ids[image_name],
            ):
                observed.append("original")
            else:
                observed.append("unknown")
        result = observed[0] if observed and len(set(observed)) == 1 else "ambiguous"
        store.record_observation(
            "runtime_probe",
            {"group": step.kind.value, "healthy": healthy, "result": result},
        )
        return result, healthy

    def _observe_migration_state(
        self,
        store: ReleaseStore,
        manifest: ReleaseManifest,
    ) -> str:
        store.record_intent("migration_probe", {"source": "postgres"})
        probe = (
            "exec psql --no-psqlrc --set=ON_ERROR_STOP=1 --tuples-only --no-align "
            '--username "$POSTGRES_USER" --dbname "$POSTGRES_DB" '
            '--command "SELECT version_num FROM alembic_version"'
        )
        result = self.runner.run(
            self._compose() + ["exec", "-T", "postgres", "sh", "-ec", probe],
            cwd=self.root,
        )
        observed = "ambiguous"
        if result.returncode == 0:
            try:
                line = self._line(result, "migration reconciliation")
            except ReleaseManagerError:
                line = ""
            if line == manifest.migration_target:
                observed = "target"
            elif line == manifest.migration_from:
                observed = "original"
        store.record_observation("migration_probe", {"result": observed})
        return observed

    def _write_compensation_env(
        self,
        store: ReleaseStore,
        manifest: ReleaseManifest,
        keep_data: set[str],
    ) -> None:
        snapshot = self._read_snapshot(store)
        refs = cast(dict[str, str], snapshot["current_refs"]).copy()
        for name in keep_data:
            refs[name] = manifest.images[name].ref
        original = _read_safe_bytes(
            store.release_dir / "original.env",
            expected_uid=os.geteuid(),
            expected_mode=0o600,
            maximum=_MAX_JSON_BYTES,
        )
        env_path = self.root / ".env"
        mode = stat.S_IMODE(env_path.stat(follow_symlinks=False).st_mode)
        store.record_intent("compensation_env", {"retained_data": sorted(keep_data)})
        ReleaseStore._atomic_write(
            env_path,
            self._render_env_refs(original, refs),
            mode=mode,
            private_parent=False,
        )
        store.record_observation("compensation_env", {"completed": True})
        config = self.runner.run(self._compose() + ["config", "--quiet"], cwd=self.root)
        if config.returncode != 0:
            raise ReleaseManagerError("compensation configuration failed")
        store.record_observation("compensation_config", {"completed": True})

    def _preflight_activation(
        self,
        store: ReleaseStore,
        manifest: ReleaseManifest,
    ) -> None:
        snapshot = self._read_snapshot(store)
        store.record_intent("activation_preflight", {"source": "runtime"})
        commit = self._validate_git(manifest)
        refs = self._current_refs(manifest)
        container_ids, image_ids, service_container_ids = self._current_runtime(refs)
        migration_head = self._migration_head(manifest)
        if (
            commit != snapshot["current_commit"]
            or refs != snapshot["current_refs"]
            or container_ids != snapshot["container_ids"]
            or image_ids != snapshot["image_ids"]
            or service_container_ids != snapshot["service_container_ids"]
            or migration_head != snapshot["migration_head"]
        ):
            raise ReleaseManagerError("prepared runtime has drifted")
        store.record_observation("activation_preflight", {"completed": True})

    def _observe_release_runtime(
        self,
        store: ReleaseStore,
        manifest: ReleaseManifest,
    ) -> RuntimeObservation:
        snapshot = self._read_snapshot(store)
        original_refs = cast(dict[str, str], snapshot["current_refs"])
        try:
            env_refs = self._root_env_refs()
        except ReleaseManagerError:
            env_state = "unknown"
        else:
            target_refs = {name: manifest.images[name].ref for name in _IMAGE_NAMES}
            if env_refs == original_refs:
                env_state = "original"
            elif env_refs == target_refs:
                env_state = "target"
            else:
                env_state = "mixed"

        groups = [
            step
            for step in build_activation_plan(manifest)
            if step.kind
            in {
                ReleaseStepKind.RECREATE_POSTGRES,
                ReleaseStepKind.RECREATE_REDIS,
                ReleaseStepKind.RECREATE_BACKEND,
                ReleaseStepKind.RECREATE_WEB,
            }
        ]
        states: list[str] = []
        healthy = True
        completed = self._completed_steps(store)
        for step in groups:
            state, group_healthy = self._observe_step_runtime_status(store, manifest, step)
            if (
                (state == "stopped" or (state == "original" and not group_healthy))
                and step.kind is ReleaseStepKind.RECREATE_BACKEND
                and ReleaseStepKind.RECREATE_BACKEND not in completed
            ):
                states.append("original")
                continue
            states.append(state)
            healthy = healthy and group_healthy
        if not states or all(value == "target" for value in states):
            service_state = "target"
        elif all(value == "original" for value in states):
            service_state = "original"
        elif all(value in {"original", "target", "stopped"} for value in states):
            service_state = "prefix"
        else:
            service_state = "ambiguous"
            healthy = False
        migration_state = self._observe_migration_state(store, manifest)
        return RuntimeObservation(
            env_state=env_state,
            service_state=service_state,
            migration_state=migration_state,
            healthy=healthy,
            migration_required=manifest.migration_from != manifest.migration_target,
        )

    def _mark_recovery_required(
        self,
        store: ReleaseStore,
        current: ReleaseState,
        *,
        reason: str,
    ) -> None:
        if current is ReleaseState.PREPARED:
            store.transition(ReleaseState.PREPARED, ReleaseState.ACTIVATING)
            current = ReleaseState.ACTIVATING
        store.transition(current, ReleaseState.RECOVERY_REQUIRED, failure_type=reason)

    def _resume_forward(
        self,
        store: ReleaseStore,
        manifest: ReleaseManifest,
        observed: RuntimeObservation,
    ) -> None:
        if observed.env_state == "original":
            self.configure_activation(manifest.release_id)
        elif observed.env_state == "target":
            store.record_intent("resume_config", {"source": "runtime"})
            result = self.runner.run(self._compose() + ["config", "--quiet"], cwd=self.root)
            if result.returncode != 0:
                self._mark_recovery_required(
                    store,
                    ReleaseState.ACTIVATING,
                    reason="resume_config",
                )
                raise ReleaseManagerError("release resume ended in recovery_required")
            store.record_observation("resume_config", {"completed": True})
        else:
            self._mark_recovery_required(
                store,
                ReleaseState.ACTIVATING,
                reason="env_ambiguity",
            )
            raise ReleaseManagerError("release resume ended in recovery_required")

        plan = build_activation_plan(manifest)
        commands = activation_commands(self.root, plan)
        completed = self._completed_steps(store)
        recreation = {
            ReleaseStepKind.RECREATE_POSTGRES,
            ReleaseStepKind.RECREATE_REDIS,
            ReleaseStepKind.RECREATE_BACKEND,
            ReleaseStepKind.RECREATE_WEB,
        }
        for step, command in zip(plan, commands, strict=True):
            self._ensure_not_stopped(
                store,
                ReleaseState.ACTIVATING,
                next_step=step.kind.value,
            )
            if step.kind in completed:
                if step.kind in recreation:
                    state, healthy = self._observe_step_runtime_status(store, manifest, step)
                    if state != "target" or not healthy:
                        self._mark_recovery_required(
                            store,
                            ReleaseState.ACTIVATING,
                            reason="completed_step_drift",
                        )
                        raise ReleaseManagerError("release resume ended in recovery_required")
                elif step.kind is ReleaseStepKind.RUN_MIGRATE:
                    migration = self._observe_migration_state(store, manifest)
                    if migration != "target":
                        self._mark_recovery_required(
                            store,
                            ReleaseState.ACTIVATING,
                            reason="migration_drift",
                        )
                        raise ReleaseManagerError("release resume ended in recovery_required")
                continue
            if step.kind in recreation:
                state, healthy = self._observe_step_runtime_status(store, manifest, step)
                if state == "target" and healthy:
                    store.record_observation(
                        step.kind.value,
                        {
                            "completed": True,
                            "recovered": True,
                            "services": list(step.services),
                        },
                    )
                    completed.add(step.kind)
                    continue
                if state not in {"original", "stopped"}:
                    self._mark_recovery_required(
                        store,
                        ReleaseState.ACTIVATING,
                        reason="runtime_ambiguity",
                    )
                    raise ReleaseManagerError("release resume ended in recovery_required")
            elif step.kind is ReleaseStepKind.RUN_MIGRATE:
                migration = self._observe_migration_state(store, manifest)
                if migration == "target":
                    store.record_observation(
                        step.kind.value,
                        {"completed": True, "recovered": True},
                    )
                    completed.add(step.kind)
                    continue
                if migration != "original":
                    self._mark_recovery_required(
                        store,
                        ReleaseState.ACTIVATING,
                        reason="migration_ambiguity",
                    )
                    raise ReleaseManagerError("release resume ended in recovery_required")
            try:
                self._run_activation_step(store, manifest, step, command)
            except _ActivationStepError as exc:
                self._compensate(store, manifest, exc)
            completed.add(step.kind)
        self._ensure_not_stopped(
            store,
            ReleaseState.ACTIVATING,
            next_step="finalize",
        )
        store.transition(ReleaseState.ACTIVATING, ReleaseState.SUCCEEDED)

    def _resume_staged(self, store: ReleaseStore, manifest: ReleaseManifest) -> None:
        self._ensure_not_stopped(
            store,
            ReleaseState.STAGED,
            next_step="prepare",
        )
        try:
            self._active_store = store
            artifacts = store.release_dir / "artifacts"
            current_commit = self._validate_git(manifest)
            current_refs = self._current_refs(manifest)
            self._validate_release_evidence(manifest, artifacts)
            data_evidence = self._validate_data_evidence(manifest, artifacts)
            self._validate_backup_evidence(manifest, artifacts)
            target_ids = self._target_images(manifest, artifacts)
            container_ids, image_ids, service_container_ids = self._current_runtime(current_refs)
            self._validate_data_majors(data_evidence)
            migration_head = self._migration_head(manifest)
            self._write_snapshot(
                store,
                current_commit=current_commit,
                current_refs=current_refs,
                container_ids=container_ids,
                image_ids=image_ids,
                migration_head=migration_head,
                manifest=manifest,
                service_container_ids=service_container_ids,
                target_image_ids=target_ids,
            )
            store.transition(ReleaseState.STAGED, ReleaseState.PREPARED)
        except _ReleaseInterrupted:
            raise
        except Exception as exc:
            with suppress(Exception):
                store.transition(
                    ReleaseState.STAGED,
                    ReleaseState.FAILED,
                    failure_type=type(exc).__name__,
                )
            raise ReleaseManagerError("staged release resume failed") from exc
        finally:
            self._active_store = None

    def _compensate(
        self,
        store: ReleaseStore,
        manifest: ReleaseManifest,
        failure: _ActivationStepError,
        *,
        already_rolling_back: bool = False,
    ) -> None:
        if failure.ambiguous:
            current = ReleaseState.ROLLING_BACK if already_rolling_back else ReleaseState.ACTIVATING
            store.transition(
                current,
                ReleaseState.RECOVERY_REQUIRED,
                failure_step=failure.kind.value,
            )
            raise ReleaseManagerError("release activation ended in recovery_required")
        completed = self._completed_steps(store)
        migration_state = "original"
        if manifest.migration_from != manifest.migration_target and (
            ReleaseStepKind.RUN_MIGRATE in completed or failure.kind is ReleaseStepKind.RUN_MIGRATE
        ):
            migration_state = self._observe_migration_state(store, manifest)
            if migration_state == "ambiguous":
                current = (
                    ReleaseState.ROLLING_BACK if already_rolling_back else ReleaseState.ACTIVATING
                )
                store.transition(
                    current,
                    ReleaseState.RECOVERY_REQUIRED,
                    failure_step=failure.kind.value,
                )
                raise ReleaseManagerError("release activation ended in recovery_required")
        later_stateless_failure = failure.kind in {
            ReleaseStepKind.RECREATE_BACKEND,
            ReleaseStepKind.RECREATE_WEB,
            ReleaseStepKind.VERIFY,
        }
        keep_data = {
            name
            for name, kind in (
                ("postgres", ReleaseStepKind.RECREATE_POSTGRES),
                ("redis", ReleaseStepKind.RECREATE_REDIS),
            )
            if later_stateless_failure and kind in completed
        }
        residual = [f"image:{name}" for name in ("postgres", "redis") if name in keep_data]
        if migration_state == "target":
            if manifest.migration_compatibility is not MigrationCompatibility.EXPAND:
                current = (
                    ReleaseState.ROLLING_BACK if already_rolling_back else ReleaseState.ACTIVATING
                )
                store.transition(
                    current,
                    ReleaseState.RECOVERY_REQUIRED,
                    failure_step=failure.kind.value,
                )
                raise ReleaseManagerError("release activation ended in recovery_required")
            residual.append(f"migration:{manifest.migration_target}")
        if not already_rolling_back:
            store.transition(
                ReleaseState.ACTIVATING,
                ReleaseState.ROLLING_BACK,
                failure_step=failure.kind.value,
            )
        try:
            self._ensure_not_stopped(
                store,
                ReleaseState.ROLLING_BACK,
                next_step="compensation_env",
            )
            self._write_compensation_env(store, manifest, keep_data)
            restore_kinds: list[ReleaseStepKind] = []
            if ReleaseStepKind.RECREATE_WEB in completed:
                restore_kinds.append(ReleaseStepKind.RECREATE_WEB)
            if (
                ReleaseStepKind.RECREATE_BACKEND in completed
                or ReleaseStepKind.QUIESCE_BACKEND in completed
            ):
                restore_kinds.append(ReleaseStepKind.RECREATE_BACKEND)
            if ReleaseStepKind.RECREATE_REDIS in completed and "redis" not in keep_data:
                restore_kinds.append(ReleaseStepKind.RECREATE_REDIS)
            if ReleaseStepKind.RECREATE_POSTGRES in completed and "postgres" not in keep_data:
                restore_kinds.append(ReleaseStepKind.RECREATE_POSTGRES)
            plan_by_kind = {step.kind: step for step in build_activation_plan(manifest)}
            compensated = {
                event["details"].get("group")
                for event in self._read_events(store)
                if event["kind"] == "observation"
                and event["step"] == "compensate"
                and event["details"].get("completed") is True
            }
            for kind in restore_kinds:
                if kind.value in compensated:
                    continue
                step = plan_by_kind[kind]
                command = activation_commands(self.root, [step])[0]
                self._ensure_not_stopped(
                    store,
                    ReleaseState.ROLLING_BACK,
                    next_step=f"compensate_{kind.value}",
                )
                store.record_intent(
                    "compensate", {"group": kind.value, "services": list(step.services)}
                )
                result = self.runner.run(command, cwd=self.root)
                if result.returncode != 0:
                    raise ReleaseManagerError("service compensation failed")
                store.record_observation("compensate", {"completed": True, "group": kind.value})
            store.transition(
                ReleaseState.ROLLING_BACK,
                ReleaseState.ROLLED_BACK,
                residual_changes=residual,
            )
        except _ReleaseInterrupted:
            raise
        except Exception as exc:
            with suppress(Exception):
                store.transition(
                    ReleaseState.ROLLING_BACK,
                    ReleaseState.RECOVERY_REQUIRED,
                    failure_type=type(exc).__name__,
                    residual_changes=residual,
                )
            raise ReleaseManagerError("release activation ended in recovery_required") from exc
        raise ReleaseManagerError("release activation ended in rolled_back")

    def activate(self, release_id: str) -> None:
        store = ReleaseStore(self.release_root, release_id)
        state = store.read_state()
        if state.get("state") != ReleaseState.PREPARED.value:
            raise ReleaseManagerError("release is not prepared for activation")
        manifest = self._stored_manifest(store)
        self._ensure_not_stopped(
            store,
            ReleaseState.PREPARED,
            next_step="activation_preflight",
        )
        store.transition(ReleaseState.PREPARED, ReleaseState.ACTIVATING)
        try:
            self._active_store = store
            self._preflight_activation(store, manifest)
        except Exception as exc:
            with suppress(Exception):
                store.transition(
                    ReleaseState.ACTIVATING,
                    ReleaseState.RECOVERY_REQUIRED,
                    failure_type=type(exc).__name__,
                )
            raise ReleaseManagerError("release activation ended in recovery_required") from exc
        finally:
            self._active_store = None
        try:
            plan = self.configure_activation(release_id)
        except Exception as exc:
            self._compensate(
                store,
                manifest,
                _ActivationStepError(ReleaseStepKind.VERIFY),
            )
            raise ReleaseManagerError("release activation failed") from exc
        commands = activation_commands(self.root, plan)
        for step, command in zip(plan, commands, strict=True):
            self._ensure_not_stopped(
                store,
                ReleaseState.ACTIVATING,
                next_step=step.kind.value,
            )
            try:
                self._run_activation_step(store, manifest, step, command)
            except _ActivationStepError as exc:
                self._compensate(store, manifest, exc)
        self._ensure_not_stopped(
            store,
            ReleaseState.ACTIVATING,
            next_step="finalize",
        )
        store.transition(ReleaseState.ACTIVATING, ReleaseState.SUCCEEDED)

    def status(self, release_id: str) -> dict[str, object]:
        try:
            return ReleaseStore(self.release_root, release_id).read_state()
        except ReleaseStoreError as exc:
            raise ReleaseManagerError("release status is unavailable") from exc

    def resume(self, release_id: str) -> None:
        store = ReleaseStore(self.release_root, release_id)
        state_value = store.read_state().get("state")
        if state_value in {
            ReleaseState.SUCCEEDED.value,
            ReleaseState.ROLLED_BACK.value,
        }:
            return
        if state_value == ReleaseState.RECOVERY_REQUIRED.value:
            raise ReleaseManagerError("recovery_required release refuses automatic resume")
        if state_value == ReleaseState.FAILED.value:
            raise ReleaseManagerError("failed release cannot be resumed")
        manifest = self._stored_manifest(store)
        if state_value == ReleaseState.STAGED.value:
            self._resume_staged(store, manifest)
            self.activate(release_id)
            return
        if state_value not in {
            ReleaseState.PREPARED.value,
            ReleaseState.ACTIVATING.value,
            ReleaseState.ROLLING_BACK.value,
        }:
            raise ReleaseManagerError("release state is unknown")
        current = ReleaseState(state_value)
        self._ensure_not_stopped(store, current, next_step="reconcile")
        observed = self._observe_release_runtime(store, manifest)
        decision = reconcile_release(store.read_state(), observed)
        if decision is ReconciliationDecision.RECOVERY_REQUIRED:
            self._mark_recovery_required(store, current, reason="reconciliation")
            raise ReleaseManagerError("release resume ended in recovery_required")
        if decision is ReconciliationDecision.FINALIZE:
            prepared_without_activation_evidence = current is ReleaseState.PREPARED
            if current is ReleaseState.PREPARED:
                store.transition(ReleaseState.PREPARED, ReleaseState.ACTIVATING)
                current = ReleaseState.ACTIVATING
            if current is ReleaseState.ACTIVATING:
                try:
                    self._verify_final_runtime(store, manifest)
                except _FinalRuntimeError as exc:
                    self._compensate(
                        store,
                        manifest,
                        _ActivationStepError(
                            ReleaseStepKind.VERIFY,
                            ambiguous=exc.ambiguous or prepared_without_activation_evidence,
                        ),
                    )
                except Exception:
                    self._compensate(
                        store,
                        manifest,
                        _ActivationStepError(ReleaseStepKind.VERIFY, ambiguous=True),
                    )
                store.transition(ReleaseState.ACTIVATING, ReleaseState.SUCCEEDED)
                return
            if current is ReleaseState.ROLLING_BACK:
                store.transition(
                    ReleaseState.ROLLING_BACK,
                    ReleaseState.ROLLED_BACK,
                    residual_changes=store.read_state().get("residual_changes", []),
                )
                return
        if decision is ReconciliationDecision.ROLLBACK:
            self.rollback(release_id)
            return
        if decision is ReconciliationDecision.RESUME:
            if current is ReleaseState.PREPARED:
                self.activate(release_id)
            else:
                self._resume_forward(store, manifest, observed)
            return
        raise ReleaseManagerError("release reconciliation decision is invalid")

    def rollback(self, release_id: str) -> None:
        store = ReleaseStore(self.release_root, release_id)
        state = store.read_state()
        state_value = state.get("state")
        if state_value in {
            ReleaseState.STAGED.value,
            ReleaseState.FAILED.value,
            ReleaseState.ROLLED_BACK.value,
        }:
            return
        if state_value == ReleaseState.RECOVERY_REQUIRED.value:
            raise ReleaseManagerError("recovery_required release refuses automatic rollback")
        if state_value == ReleaseState.SUCCEEDED.value:
            raise ReleaseManagerError("succeeded release requires an explicit new release")
        manifest = self._stored_manifest(store)
        already_rolling_back = state_value == ReleaseState.ROLLING_BACK.value
        if state_value == ReleaseState.PREPARED.value:
            self._ensure_not_stopped(
                store,
                ReleaseState.PREPARED,
                next_step="rollback",
            )
            store.snapshot_env(self.root / ".env")
            store.transition(ReleaseState.PREPARED, ReleaseState.ACTIVATING)
        elif state_value not in {
            ReleaseState.ACTIVATING.value,
            ReleaseState.ROLLING_BACK.value,
        }:
            raise ReleaseManagerError("release state is unknown")
        failure_name = state.get("failure_step")
        try:
            failure_kind = (
                ReleaseStepKind(failure_name)
                if isinstance(failure_name, str)
                else ReleaseStepKind.RUN_MIGRATE
            )
        except (TypeError, ValueError):
            failure_kind = ReleaseStepKind.RUN_MIGRATE
        self._compensate(
            store,
            manifest,
            _ActivationStepError(failure_kind),
            already_rolling_back=already_rolling_back,
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="四镜像统一发布状态机")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--release-root", required=True, type=Path)
    parser.add_argument("--mode", choices=("development", "production"), required=True)
    subparsers = parser.add_subparsers(dest="action", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--manifest", required=True, type=Path)
    for action in ("activate", "status", "resume", "rollback"):
        command = subparsers.add_parser(action)
        command.add_argument("--release-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    try:
        release_root = _validate_cli_release_root(
            arguments.release_root,
            mode=arguments.mode,
            platform_root=arguments.root,
        )
    except ReleaseManagerError as exc:
        print(f"sms-compose: {exc}", file=sys.stderr)
        return 1
    manager = ReleaseManager(
        root=arguments.root,
        release_root=release_root,
        mode=arguments.mode,
    )
    managed_signals = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
    previous_handlers = {signum: signal.getsignal(signum) for signum in managed_signals}
    try:
        for signum in managed_signals:
            signal.signal(signum, manager.request_stop)
        if arguments.action == "prepare":
            manager.prepare(arguments.manifest)
        elif arguments.action == "status":
            print(json.dumps(manager.status(arguments.release_id), sort_keys=True))
        else:
            getattr(manager, arguments.action)(arguments.release_id)
    except ReleaseManagerError as exc:
        print(f"sms-compose: {exc}", file=sys.stderr)
        return 1
    finally:
        for signum in managed_signals:
            signal.signal(signum, previous_handlers[signum])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
