#!/usr/bin/env python3
"""四镜像统一发布状态机。"""

from __future__ import annotations

import argparse
import ast
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
_REDIS_HA_MODE_KEY = "REDIS_HA_MODE"
_PRODUCTION_REDIS_HA_MODES = frozenset({"managed", "isolated-standalone"})
_BASE_COMPOSE_FILE = "docker-compose.yml"
_PRODUCTION_STORAGE_COMPOSE_FILE = "docker-compose.production-storage.yml"
_REDIS_TLS_COMPOSE_FILE = "docker-compose.redis-tls.yml"
_PRODUCTION_STORAGE_BIND_PATHS = (
    Path("/var/lib/sms-platform/postgres/pgdata"),
    Path("/var/lib/sms-platform/redis/broker"),
    Path("/var/lib/sms-platform/redis/auth"),
    Path("/var/lib/sms-platform/redis/control"),
    Path("/var/lib/sms-platform/runtime/imports"),
    Path("/var/lib/sms-platform/runtime/exports"),
    Path("/var/lib/sms-platform/runtime/raw-spill"),
    Path("/var/lib/sms-platform/runtime/backups"),
)
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
        "metric_scope",
        "business_rto_evidence",
        "snapshot_id",
        "recovery_crypto_generation_id",
        "backup_passphrase_generation_id",
        "git_commit",
        "database",
        "started_at",
        "finished_at",
        "restore_seconds",
        "restore_budget_seconds",
        "within_restore_budget",
        "checks",
        "crypto_probe_receipts",
        "table_counts",
    }
)
_RESTORE_CHECK_FIELDS = frozenset(
    {
        "alembic_version",
        "role_flags",
        "audit_privileges",
        "crypto_generation_binding",
        "historical_ciphertext_validation",
        "pre_migration_crypto_validation",
        "post_migration_crypto_validation",
    }
)
_TABLE_COUNT_FIELDS = frozenset(
    {"sms_batch", "audit_log", "raw_vendor_log", "sms_message"}
)
_RESTORE_CRYPTO_PROBE_FIELDS = frozenset(
    {"schema_version", "status", "counts", "coverage"}
)
_RESTORE_CRYPTO_PROBE_COUNT_FIELDS = frozenset(
    {
        "audit_context_keys",
        "encrypted_columns",
        "encrypted_rows",
        "ciphertext_samples_verified",
        "key_version_columns",
        "referenced_key_versions",
        "sms_message_rows",
    }
)
_RESTORE_CRYPTO_PROBE_COVERAGE_FIELDS = frozenset(
    {
        "app.callback_secret_enc",
        "blacklist.phone_enc",
        "callback_task.callback_secret_enc",
        "import_phone.phone_enc",
        "raw_vendor_log.payload_enc",
        "reply_event.content_enc",
        "reply_event.phone_enc",
        "report_event.phone_enc",
        "sensitive_metadata_archive.value_enc",
        "sms_batch.display_content_enc",
        "sms_batch.send_content_enc",
        "sms_message.phone_enc",
        "sms_reply.phone_enc",
        "sms_template.content_enc",
        "sms_template.name_enc",
        "unmatched_report.phone_enc",
        "vendor_test_recipient.phone_enc",
    }
)
_RESTORE_CRYPTO_PROBE_COVERAGE_VALUE_FIELDS = frozenset(
    {"rows", "key_versions_verified"}
)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_MAX_JSON_BYTES = 1024 * 1024
_CONTROL_SMOKE_PURPOSE = "release_control_failure_injection"
_RUNTIME_ROOT_NAME = "runtime-secrets"
_BOOTSTRAP_STATE_NAME = "bootstrap-state.json"
_RECOVERY_STATE_NAME = "recovery-state.json"
_BOOTSTRAP_STATE_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "release_id",
        "commit",
        "manifest_sha256",
        "production_topology",
        "phase",
        "started_at",
        "updated_at",
        "failure_type",
    }
)
_RECOVERY_SNAPSHOT_FIELDS = frozenset(
    {
        "schema_version",
        "snapshot_id",
        "created_at",
        "git_commit",
        "alembic_version",
        "database",
        "secrets_included",
        "recovery_crypto_generation_id",
        "backup_passphrase_generation_id",
        "files",
    }
)
_RECOVERY_SNAPSHOT_FILE_FIELDS = frozenset({"name", "sha256", "size"})
_RECOVERY_GAP_FENCE_FIELDS = frozenset(
    {
        "schema_version",
        "record_type",
        "status",
        "snapshot_id",
        "snapshot_manifest_sha256",
        "git_commit",
        "migration_head",
        "window_started_at",
        "window_ended_at",
        "upstream_request_count",
        "vendor_accepted_or_sent_count",
        "vendor_not_accepted_count",
        "vendor_unknown_count",
        "old_primary_isolated",
        "upstream_retries_frozen",
        "unknown_results_blocked",
        "automatic_resend_forbidden",
        "approved_by",
        "approved_at",
    }
)
_RECOVERY_RESTORE_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "record_type",
        "status",
        "snapshot_id",
        "snapshot_manifest_sha256",
        "snapshot_database_sha256",
        "git_commit",
        "migration_head",
        "database",
        "recovery_crypto_generation_id",
        "backup_passphrase_generation_id",
        "live_database_fingerprint_sha256",
        "crypto_probe_status",
        "crypto_probe_sha256",
        "restored_at",
        "approved_by",
        "approved_at",
    }
)
_RECOVERY_STATE_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "phase",
        "release_id",
        "commit",
        "manifest_sha256",
        "production_topology",
        "runtime_secrets_target",
        "migration_head",
        "snapshot_id",
        "snapshot_manifest_sha256",
        "snapshot_database_sha256",
        "recovery_crypto_generation_id",
        "backup_passphrase_generation_id",
        "restore_receipt_sha256",
        "live_database_fingerprint_sha256",
        "crypto_probe_status",
        "crypto_probe_sha256",
        "gap_fence_sha256",
        "recovery_watermark_sha256",
        "started_at",
        "updated_at",
        "failure_type",
    }
)
_RECOVERY_CLI_TOPOLOGY_FIELDS = frozenset(
    {
        "schema_version",
        "redis_ha_mode",
        "compose_files",
        "root_env_non_image_sha256",
        "topology_id",
    }
)
_RECOVERY_CLI_TOPOLOGY_FILE_FIELDS = frozenset({"name", "sha256"})
_RECOVERY_CLI_PHASES = frozenset(
    {
        "validated",
        "data_starting",
        "data_started",
        "observed",
        "adopted",
        "api_started",
        "callback_started",
        "workers_started",
        "outbox_started",
        "beat_started",
        "succeeded",
        "failed",
    }
)
_RECOVERY_CLI_BASE_TOPOLOGY_FILES = (
    f"deploy/{_BASE_COMPOSE_FILE}",
    f"deploy/{_PRODUCTION_STORAGE_COMPOSE_FILE}",
)
_RECOVERY_CLI_TLS_TOPOLOGY_FILE = f"deploy/{_REDIS_TLS_COMPOSE_FILE}"
_SENSITIVE_CLI_RESULT_TOKEN_RE = re.compile(
    r"(?:password|passphrase|secret)",
    re.IGNORECASE,
)
_QUIESCE_SERVICES = (
    "beat",
    "outbox-dispatcher",
    "worker-realtime",
    "worker-bulk",
    "worker-callback",
    "api",
)
_BOOTSTRAP_CONTAINMENT_SERVICES = ("web", *_QUIESCE_SERVICES)
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
_RECOVERY_DATA_SERVICES = ("postgres", "redis", "redis-auth", "redis-control")
_RUNTIME_SECRETS_TARGET_RE = re.compile(
    r"generations/generation-[0-9a-f]{32}\Z"
)
_RECOVERY_RESUME_STAGES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("api", ("api",), "api_started"),
    ("callback", ("worker-callback",), "callback_started"),
    ("workers", ("worker-realtime", "worker-bulk"), "workers_started"),
    ("outbox", ("outbox-dispatcher",), "outbox_started"),
    ("beat", ("beat",), "beat_started"),
    ("web", ("web",), "succeeded"),
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


def activation_commands(
    root: Path,
    plan: Sequence[ReleaseStep],
    *,
    compose: Sequence[str] | None = None,
) -> list[list[str]]:
    """把纯计划映射为固定 argv；不执行命令。"""

    compose_argv = list(compose) if compose is not None else [
        "docker",
        "compose",
        "--env-file",
        str(root / ".env"),
        "-f",
        str(root / "deploy" / _BASE_COMPOSE_FILE),
    ]
    commands: list[list[str]] = []
    for step in plan:
        if step.kind is ReleaseStepKind.QUIESCE_BACKEND:
            command = compose_argv + ["stop", *step.services]
        elif step.kind is ReleaseStepKind.WAIT_BEAT_LEASE:
            command = ["sleep", "31"]
        elif step.kind in {
            ReleaseStepKind.RECREATE_POSTGRES,
            ReleaseStepKind.RECREATE_REDIS,
            ReleaseStepKind.RECREATE_BACKEND,
            ReleaseStepKind.RECREATE_WEB,
        }:
            command = compose_argv + [
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
            command = compose_argv + ["run", "--rm", *step.services]
        elif step.kind is ReleaseStepKind.VERIFY:
            command = compose_argv + ["config", "--quiet"]
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


def _public_cli_schema_version(value: object, context: str) -> int:
    if type(value) is not int or value != 1:
        raise ReleaseManagerError(f"{context} is invalid")
    return value


def _public_cli_enum(
    value: object,
    allowed: frozenset[str],
    context: str,
) -> str:
    if type(value) is not str or len(value) > 64 or value not in allowed:
        raise ReleaseManagerError(f"{context} is invalid")
    return value


def _public_cli_safe_id(value: object, context: str) -> str:
    result = _safe_id(value, context)
    if _SENSITIVE_CLI_RESULT_TOKEN_RE.search(result) is not None:
        raise ReleaseManagerError(f"{context} is invalid")
    return result


def _public_cli_optional_safe_id(value: object, context: str) -> str | None:
    if value is None:
        return None
    return _public_cli_safe_id(value, context)


def _public_cli_commit(value: object, context: str) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise ReleaseManagerError(f"{context} is invalid")
    return value


def _public_cli_sha256(value: object, context: str) -> str:
    if type(value) is not str or _REPORT_HASH_RE.fullmatch(value) is None:
        raise ReleaseManagerError(f"{context} is invalid")
    return value


def _public_cli_optional_sha256(value: object, context: str) -> str | None:
    if value is None:
        return None
    return _public_cli_sha256(value, context)


def _public_cli_migration(value: object, context: str) -> str:
    if (
        type(value) is not str
        or _MIGRATION_RE.fullmatch(value) is None
        or _SENSITIVE_CLI_RESULT_TOKEN_RE.search(value) is not None
    ):
        raise ReleaseManagerError(f"{context} is invalid")
    return value


def _public_cli_timestamp(value: object, context: str) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > 64
        or _SENSITIVE_CLI_RESULT_TOKEN_RE.search(value) is not None
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ReleaseManagerError(f"{context} is invalid")
    _parse_utc(value, context)
    return value


def _public_recovery_topology(value: object) -> dict[str, object]:
    topology = _exact_object(
        value,
        _RECOVERY_CLI_TOPOLOGY_FIELDS,
        "recovery CLI topology",
    )
    compose_files = topology["compose_files"]
    schema_version = _public_cli_schema_version(
        topology["schema_version"],
        "recovery CLI topology schema version",
    )
    redis_ha_mode = _public_cli_enum(
        topology["redis_ha_mode"],
        _PRODUCTION_REDIS_HA_MODES,
        "recovery CLI Redis HA mode",
    )
    expected_names: tuple[str, ...] = _RECOVERY_CLI_BASE_TOPOLOGY_FILES
    if redis_ha_mode == "isolated-standalone":
        expected_names = (*expected_names, _RECOVERY_CLI_TLS_TOPOLOGY_FILE)
    if type(compose_files) is not list or len(compose_files) != len(expected_names):
        raise ReleaseManagerError("recovery CLI topology is invalid")
    public_files: list[dict[str, str]] = []
    for raw_file, expected_name in zip(compose_files, expected_names, strict=True):
        item = _exact_object(
            raw_file,
            _RECOVERY_CLI_TOPOLOGY_FILE_FIELDS,
            "recovery CLI topology file",
        )
        name = item["name"]
        if type(name) is not str or name != expected_name:
            raise ReleaseManagerError("recovery CLI topology file is invalid")
        digest = _public_cli_sha256(
            item["sha256"],
            "recovery CLI topology file digest",
        )
        public_files.append({"name": name, "sha256": digest})
    return {
        "schema_version": schema_version,
        "redis_ha_mode": redis_ha_mode,
        "compose_files": public_files,
        "root_env_non_image_sha256": _public_cli_sha256(
            topology["root_env_non_image_sha256"],
            "recovery CLI root env digest",
        ),
        "topology_id": _public_cli_sha256(
            topology["topology_id"],
            "recovery CLI topology id",
        ),
    }


def _serialize_recovery_cli_result(action: str, value: object) -> str:
    """只把恢复动作已批准的非敏感闭集编码为单行 JSON。"""

    if action == "observe-recovery":
        internal = _exact_object(
            value,
            _RECOVERY_RESTORE_RECEIPT_FIELDS,
            "recovery CLI internal receipt",
        )
        if internal["approved_by"] != [] or internal["approved_at"] is not None:
            raise ReleaseManagerError("recovery CLI receipt approval state is invalid")
        public: dict[str, object] = {
            "schema_version": _public_cli_schema_version(
                internal["schema_version"],
                "recovery CLI receipt schema version",
            ),
            "record_type": _public_cli_enum(
                internal["record_type"],
                frozenset({"production_recovery_restore_receipt"}),
                "recovery CLI receipt record type",
            ),
            "status": _public_cli_enum(
                internal["status"],
                frozenset({"pending_approval"}),
                "recovery CLI receipt status",
            ),
            "snapshot_id": _public_cli_safe_id(
                internal["snapshot_id"],
                "recovery CLI receipt snapshot id",
            ),
            "snapshot_manifest_sha256": _public_cli_sha256(
                internal["snapshot_manifest_sha256"],
                "recovery CLI receipt snapshot manifest digest",
            ),
            "snapshot_database_sha256": _public_cli_sha256(
                internal["snapshot_database_sha256"],
                "recovery CLI receipt snapshot database digest",
            ),
            "git_commit": _public_cli_commit(
                internal["git_commit"],
                "recovery CLI receipt commit",
            ),
            "migration_head": _public_cli_migration(
                internal["migration_head"],
                "recovery CLI receipt migration head",
            ),
            "database": _public_cli_enum(
                internal["database"],
                frozenset({"sms"}),
                "recovery CLI receipt database",
            ),
            "recovery_crypto_generation_id": _public_cli_safe_id(
                internal["recovery_crypto_generation_id"],
                "recovery CLI receipt crypto generation id",
            ),
            "live_database_fingerprint_sha256": _public_cli_sha256(
                internal["live_database_fingerprint_sha256"],
                "recovery CLI receipt database fingerprint",
            ),
            "crypto_probe_status": _public_cli_enum(
                internal["crypto_probe_status"],
                frozenset({"performed", "not_applicable_empty"}),
                "recovery CLI receipt crypto probe status",
            ),
            "crypto_probe_sha256": _public_cli_sha256(
                internal["crypto_probe_sha256"],
                "recovery CLI receipt crypto probe digest",
            ),
            "restored_at": _public_cli_timestamp(
                internal["restored_at"],
                "recovery CLI receipt restored_at",
            ),
            "approved_by": [],
            "approved_at": None,
        }
    elif action in {"start-recovery", "adopt-recovery", "resume-recovery"}:
        internal = _exact_object(
            value,
            _RECOVERY_STATE_FIELDS,
            "recovery CLI internal state",
        )
        probe_status = internal["crypto_probe_status"]
        public = {
            "schema_version": _public_cli_schema_version(
                internal["schema_version"],
                "recovery CLI state schema version",
            ),
            "status": _public_cli_enum(
                internal["status"],
                frozenset({"running", "succeeded", "failed", "recovery_required"}),
                "recovery CLI state status",
            ),
            "phase": _public_cli_enum(
                internal["phase"],
                _RECOVERY_CLI_PHASES,
                "recovery CLI state phase",
            ),
            "release_id": _public_cli_safe_id(
                internal["release_id"],
                "recovery CLI state release id",
            ),
            "commit": _public_cli_commit(
                internal["commit"],
                "recovery CLI state commit",
            ),
            "manifest_sha256": _public_cli_sha256(
                internal["manifest_sha256"],
                "recovery CLI state manifest digest",
            ),
            "production_topology": _public_recovery_topology(
                internal["production_topology"]
            ),
            "migration_head": _public_cli_migration(
                internal["migration_head"],
                "recovery CLI state migration head",
            ),
            "snapshot_id": _public_cli_safe_id(
                internal["snapshot_id"],
                "recovery CLI state snapshot id",
            ),
            "snapshot_manifest_sha256": _public_cli_sha256(
                internal["snapshot_manifest_sha256"],
                "recovery CLI state snapshot manifest digest",
            ),
            "snapshot_database_sha256": _public_cli_sha256(
                internal["snapshot_database_sha256"],
                "recovery CLI state snapshot database digest",
            ),
            "recovery_crypto_generation_id": _public_cli_safe_id(
                internal["recovery_crypto_generation_id"],
                "recovery CLI state crypto generation id",
            ),
            "restore_receipt_sha256": _public_cli_optional_sha256(
                internal["restore_receipt_sha256"],
                "recovery CLI state restore receipt digest",
            ),
            "live_database_fingerprint_sha256": _public_cli_optional_sha256(
                internal["live_database_fingerprint_sha256"],
                "recovery CLI state database fingerprint",
            ),
            "crypto_probe_status": (
                None
                if probe_status is None
                else _public_cli_enum(
                    probe_status,
                    frozenset({"performed", "not_applicable_empty"}),
                    "recovery CLI state crypto probe status",
                )
            ),
            "crypto_probe_sha256": _public_cli_optional_sha256(
                internal["crypto_probe_sha256"],
                "recovery CLI state crypto probe digest",
            ),
            "gap_fence_sha256": _public_cli_optional_sha256(
                internal["gap_fence_sha256"],
                "recovery CLI state gap fence digest",
            ),
            "recovery_watermark_sha256": _public_cli_optional_sha256(
                internal["recovery_watermark_sha256"],
                "recovery CLI state watermark digest",
            ),
            "started_at": _public_cli_timestamp(
                internal["started_at"],
                "recovery CLI state started_at",
            ),
            "updated_at": _public_cli_timestamp(
                internal["updated_at"],
                "recovery CLI state updated_at",
            ),
            "failure_type": _public_cli_optional_safe_id(
                internal["failure_type"],
                "recovery CLI state failure type",
            ),
        }
    else:
        raise ReleaseManagerError("recovery CLI result action is invalid")
    return json.dumps(public, sort_keys=True, allow_nan=False)


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
        self._compose_argv: tuple[str, ...] | None = None
        self._production_redis_mode: str | None = None
        self._bootstrap_execution_release_id: str | None = None

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
        if self._compose_argv is not None:
            return list(self._compose_argv)
        command = [
            "docker",
            "compose",
            "--env-file",
            str(self.root / ".env"),
            "-f",
            str(self.root / "deploy" / _BASE_COMPOSE_FILE),
        ]
        if self.mode == "development":
            self._compose_argv = tuple(command)
            return list(self._compose_argv)
        command.extend(
            [
                "-f",
                str(self.root / "deploy" / _PRODUCTION_STORAGE_COMPOSE_FILE),
            ]
        )
        self._production_redis_mode = self._production_redis_ha_mode()
        if self._production_redis_mode == "isolated-standalone":
            command.extend(
                [
                    "-f",
                    str(self.root / "deploy" / _REDIS_TLS_COMPOSE_FILE),
                ]
            )
        self._compose_argv = tuple(command)
        return list(self._compose_argv)

    def _production_redis_ha_mode(self) -> str:
        try:
            lines = _read_safe_bytes(
                self.root / ".env",
                expected_uid=os.geteuid(),
                maximum=_MAX_JSON_BYTES,
            ).decode("utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise ReleaseManagerError("root env is unavailable") from exc
        values: list[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() != _REDIS_HA_MODE_KEY:
                continue
            if key != _REDIS_HA_MODE_KEY or value != value.strip() or not value:
                raise ReleaseManagerError("root env REDIS_HA_MODE is invalid")
            values.append(value)
        if len(values) != 1 or values[0] not in _PRODUCTION_REDIS_HA_MODES:
            raise ReleaseManagerError("root env REDIS_HA_MODE is invalid")
        return values[0]

    def _production_topology(self) -> dict[str, object]:
        if self.mode != "production":
            raise ReleaseManagerError("production topology is unavailable in development")
        compose = self._compose()
        files: list[dict[str, str]] = []
        for index, token in enumerate(compose):
            if token != "-f":
                continue
            try:
                path = Path(compose[index + 1])
                relative = path.relative_to(self.root).as_posix()
                digest = hashlib.sha256(
                    _read_safe_bytes(path, maximum=_MAX_JSON_BYTES)
                ).hexdigest()
            except (IndexError, OSError, ValueError) as exc:
                raise ReleaseManagerError("production compose topology is invalid") from exc
            files.append({"name": relative, "sha256": digest})
        if not files:
            raise ReleaseManagerError("production compose topology is invalid")
        if self._production_redis_mode is None:
            raise ReleaseManagerError("production compose topology is invalid")
        observed_mode = self._production_redis_ha_mode()
        if observed_mode != self._production_redis_mode:
            raise ReleaseManagerError("production compose topology is unstable")
        identity = {
            "schema_version": 1,
            "redis_ha_mode": observed_mode,
            "compose_files": files,
            "root_env_non_image_sha256": self._root_env_non_image_sha256(),
        }
        topology_id = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return {**identity, "topology_id": topology_id}

    def _root_env_non_image_sha256(self) -> str:
        try:
            raw = _read_safe_bytes(
                self.root / ".env",
                expected_uid=os.geteuid(),
                maximum=_MAX_JSON_BYTES,
            )
            lines = raw.decode("utf-8").splitlines(keepends=True)
        except (OSError, UnicodeError) as exc:
            raise ReleaseManagerError("root env is unavailable") from exc
        image_keys = frozenset(_ENV_IMAGE_KEYS.values())
        retained: list[str] = []
        seen: set[str] = set()
        for line in lines:
            body = line.rstrip("\r\n")
            if "=" in body:
                key = body.split("=", 1)[0].strip()
                if key in image_keys:
                    if key in seen:
                        raise ReleaseManagerError(
                            "root env image references are invalid"
                        )
                    seen.add(key)
                    continue
            retained.append(line)
        if seen != image_keys:
            raise ReleaseManagerError("root env image references are invalid")
        return hashlib.sha256("".join(retained).encode("utf-8")).hexdigest()

    def _assert_production_topology(self, state: Mapping[str, object]) -> None:
        if self.mode != "production":
            return
        try:
            actual = self._production_topology()
        except ReleaseManagerError as exc:
            raise ReleaseManagerError("prepared production topology has drifted") from exc
        if state.get("production_topology") != actual:
            raise ReleaseManagerError("prepared production topology has drifted")

    def _assert_bootstrap_mutation_allowed(self, release_id: str | None = None) -> None:
        if self.mode != "production":
            return
        state = self._read_bootstrap_state()
        if state is None or state["status"] == "succeeded":
            return
        internal_release = self._bootstrap_execution_release_id
        if (
            internal_release == state["release_id"]
            and (release_id is None or release_id == internal_release)
        ):
            return
        raise ReleaseManagerError(
            "unfinished production bootstrap requires manual recovery"
        )

    def assert_production_start_allowed(self) -> None:
        """普通生产启动只能恢复一个已封存、当前且无未决副作用的 release。"""

        try:
            self._assert_production_start_allowed()
        except ReleaseStoreError as exc:
            raise ReleaseManagerError("production release baseline is unavailable") from exc

    def _assert_production_start_allowed(self) -> None:
        if self.mode != "production":
            raise ReleaseManagerError("production start gate is unavailable in development")
        bootstrap = self._read_bootstrap_state()
        if bootstrap is None or bootstrap.get("status") != "succeeded":
            raise ReleaseManagerError(
                "production start requires a succeeded bootstrap baseline"
            )
        recovery = self._read_recovery_state()
        if recovery is not None and recovery.get("status") != "succeeded":
            raise ReleaseManagerError(
                "unfinished production recovery blocks ordinary startup"
            )
        if recovery is not None and (
            recovery.get("release_id") != bootstrap.get("release_id")
            or recovery.get("commit") != bootstrap.get("commit")
            or recovery.get("manifest_sha256") != bootstrap.get("manifest_sha256")
            or recovery.get("production_topology") != bootstrap.get("production_topology")
        ):
            raise ReleaseManagerError("production recovery baseline binding is invalid")
        bootstrap_store = ReleaseStore(
            self.release_root, cast(str, bootstrap["release_id"])
        )
        if bootstrap_store.read_state().get("state") != ReleaseState.SUCCEEDED.value:
            raise ReleaseManagerError("production bootstrap release is not succeeded")
        bootstrap_manifest_bytes = _read_safe_bytes(
            bootstrap_store.release_dir / "manifest.json",
            expected_uid=os.geteuid(),
            expected_mode=0o600,
            maximum=_MAX_JSON_BYTES,
        )
        if not hmac.compare_digest(
            hashlib.sha256(bootstrap_manifest_bytes).hexdigest(),
            cast(str, bootstrap["manifest_sha256"]),
        ):
            raise ReleaseManagerError("production bootstrap manifest binding is invalid")
        bootstrap_manifest = self._stored_manifest(bootstrap_store)
        if (
            bootstrap_manifest.commit != bootstrap.get("commit")
            or bootstrap_manifest.mode != "production"
        ):
            raise ReleaseManagerError("production bootstrap manifest binding is invalid")

        current_commit = self._current_git_commit()
        current_refs = self._root_env_refs()
        current_topology = self._production_topology()
        current_records: list[tuple[ReleaseManifest, dict[str, object]]] = []
        try:
            entries = list(os.scandir(self.release_root))
        except OSError as exc:
            raise ReleaseManagerError("production release baseline is unavailable") from exc
        for entry in entries:
            if entry.name in {_BOOTSTRAP_STATE_NAME, _RECOVERY_STATE_NAME}:
                continue
            try:
                info = entry.stat(follow_symlinks=False)
                release_id = _safe_id(entry.name, "production release id")
            except (OSError, ReleaseManagerError) as exc:
                raise ReleaseManagerError("production release baseline is unsafe") from exc
            if entry.is_symlink() or not stat.S_ISDIR(info.st_mode):
                raise ReleaseManagerError("production release baseline is unsafe")
            store = ReleaseStore(self.release_root, release_id)
            state = store.read_state()
            state_value = state.get("state")
            if state_value in {
                ReleaseState.STAGED.value,
                ReleaseState.PREPARED.value,
                ReleaseState.ACTIVATING.value,
                ReleaseState.ROLLING_BACK.value,
                ReleaseState.RECOVERY_REQUIRED.value,
            }:
                raise ReleaseManagerError(
                    "unfinished production release blocks ordinary startup"
                )
            manifest = self._stored_manifest(store)
            if hmac.compare_digest(manifest.commit, current_commit):
                current_records.append((manifest, state))
        if len(current_records) != 1:
            raise ReleaseManagerError(
                "production start requires exactly one current succeeded release"
            )
        current_manifest, current_state = current_records[0]
        if current_state.get("state") != ReleaseState.SUCCEEDED.value:
            raise ReleaseManagerError("current production release is not succeeded")
        if (
            current_manifest.mode != "production"
            or {name: current_manifest.images[name].ref for name in _IMAGE_NAMES}
            != current_refs
            or current_state.get("production_topology") != current_topology
            or current_state.get("verified_migration_head")
            != current_manifest.migration_target
        ):
            raise ReleaseManagerError("current production release baseline has drifted")
        self._validate_git(current_manifest)
        if (
            self._root_env_refs() != current_refs
            or self._production_topology() != current_topology
        ):
            raise ReleaseManagerError("current production release baseline has drifted")

    @staticmethod
    def _assert_empty_production_storage_sources() -> None:
        """以逐级 no-follow 目录 FD 证明固定生产 bind 源均为空目录。"""

        if _DIRECTORY == 0 or _NOFOLLOW == 0:
            raise ReleaseManagerError(
                "production storage bind source checks are unsupported"
            )
        flags = os.O_RDONLY | _DIRECTORY | _NOFOLLOW
        for path in _PRODUCTION_STORAGE_BIND_PATHS:
            descriptor = -1
            try:
                if not path.is_absolute() or ".." in path.parts:
                    raise ReleaseManagerError(
                        "production storage bind source is invalid"
                    )
                descriptor = os.open(path.anchor, flags)
                for component in path.parts[1:]:
                    child = os.open(component, flags, dir_fd=descriptor)
                    os.close(descriptor)
                    descriptor = child
                if os.listdir(descriptor):
                    raise ReleaseManagerError(
                        "production storage bind source is not empty"
                    )
            except ReleaseManagerError:
                raise
            except OSError as exc:
                raise ReleaseManagerError(
                    "production storage bind source is unavailable or unsafe"
                ) from exc
            finally:
                if descriptor >= 0:
                    os.close(descriptor)

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

    def _current_git_commit(self) -> str:
        """读取干净工作树的唯一 HEAD，供发布与启动门禁共同绑定。"""

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
        if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
            raise ReleaseManagerError("Git commit is invalid")
        return commit

    def _validate_git(self, manifest: ReleaseManifest) -> str:
        commit = self._current_git_commit()
        if not hmac.compare_digest(commit, manifest.commit):
            raise ReleaseManagerError("Git commit does not match the manifest")
        return commit

    def _validate_migration_direction(self, manifest: ReleaseManifest) -> None:
        """静态证明 target 沿 Alembic down_revision 链向前继承自 from。"""

        versions = self.root / "backend" / "migrations" / "versions"
        try:
            metadata = versions.lstat()
            paths = sorted(versions.glob("*.py"))
        except OSError as exc:
            raise ReleaseManagerError("migration graph is unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode) or not paths:
            raise ReleaseManagerError("migration graph is unavailable")

        parents: dict[str, str | None] = {}
        for path in paths:
            try:
                module = ast.parse(
                    _read_safe_bytes(path, maximum=_MAX_JSON_BYTES),
                    filename=path.name,
                )
            except (SyntaxError, ValueError) as exc:
                raise ReleaseManagerError("migration graph is invalid") from exc
            assignments: dict[str, object] = {}
            for statement in module.body:
                target: ast.expr | None = None
                value: ast.expr | None = None
                if isinstance(statement, ast.AnnAssign):
                    target, value = statement.target, statement.value
                elif isinstance(statement, ast.Assign) and len(statement.targets) == 1:
                    target, value = statement.targets[0], statement.value
                if (
                    not isinstance(target, ast.Name)
                    or target.id not in {"revision", "down_revision"}
                    or value is None
                ):
                    continue
                if target.id in assignments:
                    raise ReleaseManagerError("migration graph is invalid")
                try:
                    assignments[target.id] = ast.literal_eval(value)
                except (ValueError, SyntaxError) as exc:
                    raise ReleaseManagerError("migration graph is invalid") from exc
            revision = assignments.get("revision")
            parent = assignments.get("down_revision")
            if (
                type(revision) is not str
                or _MIGRATION_RE.fullmatch(revision) is None
                or (parent is not None and type(parent) is not str)
                or (type(parent) is str and _MIGRATION_RE.fullmatch(parent) is None)
                or revision in parents
            ):
                raise ReleaseManagerError("migration graph is invalid")
            parents[revision] = parent

        if manifest.migration_from not in parents or manifest.migration_target not in parents:
            raise ReleaseManagerError("migration endpoints are absent from the candidate graph")
        cursor = manifest.migration_target
        visited: set[str] = set()
        while cursor != manifest.migration_from:
            if cursor in visited:
                raise ReleaseManagerError("migration graph contains a cycle")
            visited.add(cursor)
            parent = parents.get(cursor)
            if parent is None:
                raise ReleaseManagerError(
                    "migration target is not a forward descendant of migration.from"
                )
            cursor = parent

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

    @staticmethod
    def _validate_restore_crypto_probe_receipt(
        value: object,
        context: str,
    ) -> str:
        probe = _exact_object(value, _RESTORE_CRYPTO_PROBE_FIELDS, context)
        counts = _exact_object(
            probe["counts"],
            _RESTORE_CRYPTO_PROBE_COUNT_FIELDS,
            context,
        )
        coverage = _exact_object(
            probe["coverage"],
            _RESTORE_CRYPTO_PROBE_COVERAGE_FIELDS,
            context,
        )
        status = probe["status"]
        if (
            probe["schema_version"] != 2
            or status not in {"performed", "not_applicable_empty"}
            or any(type(item) is not int or item < 0 for item in counts.values())
        ):
            raise ReleaseManagerError("restore report crypto receipt is invalid")
        encrypted_rows = 0
        samples = 0
        sms_message_rows = -1
        for label in _RESTORE_CRYPTO_PROBE_COVERAGE_FIELDS:
            item = _exact_object(
                coverage[label],
                _RESTORE_CRYPTO_PROBE_COVERAGE_VALUE_FIELDS,
                context,
            )
            rows = item["rows"]
            verified = item["key_versions_verified"]
            if (
                type(rows) is not int
                or rows < 0
                or type(verified) is not int
                or verified < 0
                or (rows == 0) != (verified == 0)
                or verified > rows
            ):
                raise ReleaseManagerError("restore report crypto receipt is invalid")
            encrypted_rows += rows
            samples += verified
            if label == "sms_message.phone_enc":
                sms_message_rows = rows
        if (
            counts["audit_context_keys"] != 4
            or counts["encrypted_columns"]
            != len(_RESTORE_CRYPTO_PROBE_COVERAGE_FIELDS)
            or counts["key_version_columns"] < 1
            or counts["encrypted_rows"] != encrypted_rows
            or counts["ciphertext_samples_verified"] != samples
            or counts["sms_message_rows"] != sms_message_rows
            or (
                status == "performed"
                and (
                    counts["encrypted_rows"] < 1
                    or counts["ciphertext_samples_verified"] < 1
                    or counts["referenced_key_versions"] < 1
                )
            )
            or (
                status == "not_applicable_empty"
                and (
                    counts["encrypted_rows"] != 0
                    or counts["ciphertext_samples_verified"] != 0
                )
            )
        ):
            raise ReleaseManagerError("restore report crypto receipt is invalid")
        return cast(str, status)

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
        crypto_receipts = _exact_object(
            report["crypto_probe_receipts"],
            frozenset({"pre_migration", "post_migration"}),
            "restore crypto probe receipts",
        )
        pre_crypto_status = self._validate_restore_crypto_probe_receipt(
            crypto_receipts["pre_migration"],
            "pre-migration restore crypto probe receipt",
        )
        post_crypto_status = self._validate_restore_crypto_probe_receipt(
            crypto_receipts["post_migration"],
            "post-migration restore crypto probe receipt",
        )
        if any(
            cast(dict[str, object], cast(dict[str, object], crypto_receipts[name])["counts"])[
                "sms_message_rows"
            ]
            != counts["sms_message"]
            for name in ("pre_migration", "post_migration")
        ):
            raise ReleaseManagerError("restore report crypto receipt is not bound")
        if (
            report["schema_version"] != 2
            or report["status"] != "success"
            or report["metric_scope"] != "database_restore"
            or report["business_rto_evidence"] is not False
            or report["within_restore_budget"] is not True
            or type(report["database"]) is not str
            or _DRILL_DATABASE_RE.fullmatch(report["database"]) is None
            or report["git_commit"] != manifest.commit
            or report["snapshot_id"] != restore_binding["snapshot_id"]
            or type(report["recovery_crypto_generation_id"]) is not str
            or _SAFE_ID_RE.fullmatch(report["recovery_crypto_generation_id"]) is None
            or type(report["backup_passphrase_generation_id"]) is not str
            or _SAFE_ID_RE.fullmatch(report["backup_passphrase_generation_id"]) is None
            or checks["role_flags"] != "7|true"
            or checks["audit_privileges"] != "true"
            or checks["crypto_generation_binding"] != "matched_host_generation_ids"
            or checks["alembic_version"] != manifest.migration_target
            or checks["pre_migration_crypto_validation"] != pre_crypto_status
            or checks["post_migration_crypto_validation"] != post_crypto_status
            or checks["historical_ciphertext_validation"] != pre_crypto_status
            or any(
                type(counts[name]) is not int or counts[name] < 0 for name in _TABLE_COUNT_FIELDS
            )
        ):
            raise ReleaseManagerError("restore report does not satisfy the release contract")
        for duration_field in ("restore_seconds", "restore_budget_seconds"):
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
        services: Sequence[str] = _RUNTIME_SERVICES,
    ) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
        compose = self._compose()
        container_ids: dict[str, str] = {}
        image_ids: dict[str, str] = {}
        service_container_ids: dict[str, str] = {}
        for service in services:
            if service not in _RUNTIME_SERVICES:
                raise ReleaseManagerError("runtime service selection is invalid")
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

    def _observed_migration_head(self) -> str:
        """只读取得当前 Alembic head；空库、复数 head 与畸形输出均失败关闭。"""

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
        if _MIGRATION_RE.fullmatch(result) is None:
            raise ReleaseManagerError("current migration observation is invalid")
        return result

    def _migration_head(self, manifest: ReleaseManifest) -> str:
        result = self._observed_migration_head()
        if result != manifest.migration_from:
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

        self._assert_bootstrap_mutation_allowed()
        manifest, manifest_bytes, staging_files = _validate_staging_bundle(
            manifest_path,
            self._staging_uid(),
        )
        self._prepare_validated(
            manifest_path,
            manifest,
            manifest_bytes,
            staging_files,
        )

    def _prepare_validated(
        self,
        manifest_path: Path,
        manifest: ReleaseManifest,
        manifest_bytes: bytes,
        staging_files: Sequence[Path],
        *,
        prepared_fields: Mapping[str, object] | None = None,
    ) -> None:
        """消费一次严格解析结果，并把可选发布类型绑定进 prepared 状态。"""

        if manifest.mode != self.mode:
            raise ReleaseManagerError("staging manifest mode does not match manager mode")
        extra_fields = dict(prepared_fields or {})
        if set(extra_fields) & {
            "prepared_at",
            "release_gate_kind",
            "control_smoke_only",
            "release_scan_performed",
        }:
            raise ReleaseManagerError("prepared release metadata is invalid")
        if self.mode == "production":
            topology = self._production_topology()
            existing_topology = extra_fields.get("production_topology")
            if existing_topology is not None and existing_topology != topology:
                raise ReleaseManagerError("prepared release metadata is invalid")
            extra_fields["production_topology"] = topology
            extra_fields.setdefault("release_kind", "standard")
            self._reject_historical_production_images(manifest)
        store = ReleaseStore(self.release_root, manifest.release_id)
        try:
            try:
                store.release_dir.lstat()
            except FileNotFoundError:
                release_existed = False
            else:
                release_existed = True
            store.create(manifest_bytes)
            state = store.read_state()
            if state.get("state") == ReleaseState.PREPARED.value:
                if any(state.get(key) != value for key, value in extra_fields.items()):
                    raise ReleaseManagerError("prepared release metadata does not match")
                return
            if state.get("state") != ReleaseState.STAGED.value:
                raise ReleaseManagerError("release is not in a preparable state")
            if release_existed:
                if any(state.get(key) != value for key, value in extra_fields.items()):
                    raise ReleaseManagerError("staged release metadata does not match")
            elif extra_fields:
                store.checkpoint(ReleaseState.STAGED, **extra_fields)
            self._active_store = store
            self._copy_bundle(store, manifest_path, staging_files)
            artifacts = store.release_dir / "artifacts"
            current_commit = self._validate_git(manifest)
            self._validate_migration_direction(manifest)
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
                **extra_fields,
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

    def _reject_historical_production_images(self, candidate: ReleaseManifest) -> None:
        """生产 changed 镜像必须是新制品，不能复用任何已进入发布状态的旧制品。"""

        if self.mode != "production":
            return
        try:
            entries = list(os.scandir(self.release_root))
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ReleaseManagerError("production image history is unavailable") from exc
        historical: dict[str, set[str]] = {
            name: set() for name in _IMAGE_NAMES
        }
        eligible = {
            ReleaseState.PREPARED.value,
            ReleaseState.ACTIVATING.value,
            ReleaseState.SUCCEEDED.value,
            ReleaseState.ROLLING_BACK.value,
            ReleaseState.ROLLED_BACK.value,
            ReleaseState.RECOVERY_REQUIRED.value,
        }
        for entry in entries:
            if entry.name == _BOOTSTRAP_STATE_NAME or entry.name == candidate.release_id:
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ReleaseManagerError(
                    "production image history is unavailable"
                ) from exc
            if entry.is_symlink() or not stat.S_ISDIR(info.st_mode):
                raise ReleaseManagerError("production image history is unsafe")
            try:
                release_id = _safe_id(entry.name, "historical release id")
                store = ReleaseStore(self.release_root, release_id)
                state = store.read_state()
                if state.get("state") not in eligible:
                    continue
                manifest = self._stored_manifest(store)
                snapshot = self._read_snapshot(store)
            except (ReleaseManagerError, ReleaseStoreError) as exc:
                raise ReleaseManagerError(
                    "production image history is unavailable"
                ) from exc
            current_refs = cast(dict[str, object], snapshot["current_refs"])
            current_ids = cast(dict[str, object], snapshot["image_ids"])
            for name in _IMAGE_NAMES:
                historical[name].update(
                    {
                        manifest.images[name].ref,
                        manifest.images[name].image_id,
                        cast(str, current_refs[name]),
                        cast(str, current_ids[name]),
                    }
                )
        for name in _IMAGE_NAMES:
            image = candidate.images[name]
            if image.changed and (
                image.ref in historical[name] or image.image_id in historical[name]
            ):
                raise ReleaseManagerError(
                    "production release cannot reuse a historical image"
                )

    def prepare_forward_rollback(
        self,
        source_release_id: str,
        manifest_path: Path,
    ) -> None:
        """把已成功发布的回退实现为保持 schema 的新候选发布。"""

        self._assert_bootstrap_mutation_allowed()
        if self.mode != "production":
            raise ReleaseManagerError("forward rollback candidates are production-only")
        source_store = ReleaseStore(self.release_root, source_release_id)
        source_state = source_store.read_state()
        if source_state.get("state") != ReleaseState.SUCCEEDED.value:
            raise ReleaseManagerError("forward rollback source release is not succeeded")
        self._assert_production_topology(source_state)
        source = self._stored_manifest(source_store)
        if source.mode != self.mode:
            raise ReleaseManagerError("forward rollback source mode does not match")
        snapshot = self._read_snapshot(source_store)
        candidate, manifest_bytes, staging_files = _validate_staging_bundle(
            manifest_path,
            self._staging_uid(),
        )
        self._validate_forward_rollback_candidate(source, snapshot, candidate)
        self._prepare_validated(
            manifest_path,
            candidate,
            manifest_bytes,
            staging_files,
            prepared_fields={
                "release_kind": "forward_rollback",
                "forward_rollback_of": source.release_id,
                "forward_rollback_source_commit": source.commit,
                "schema_retained_at": source.migration_target,
            },
        )

    def _validate_forward_rollback_candidate(
        self,
        source: ReleaseManifest,
        snapshot: Mapping[str, object],
        candidate: ReleaseManifest,
    ) -> None:
        if candidate.mode != "production" or candidate.release_id == source.release_id:
            raise ReleaseManagerError("forward rollback candidate identity is invalid")
        if candidate.commit == source.commit:
            raise ReleaseManagerError("forward rollback requires a new commit")
        if (
            candidate.migration_from != source.migration_target
            or candidate.migration_target != source.migration_target
            or candidate.migration_compatibility is not MigrationCompatibility.NONE
        ):
            raise ReleaseManagerError("forward rollback cannot change or downgrade schema")
        current_refs = self._root_env_refs()
        source_refs = {name: source.images[name].ref for name in _IMAGE_NAMES}
        if current_refs != source_refs:
            raise ReleaseManagerError("forward rollback source is not the current baseline")
        for name in ("postgres", "redis"):
            image = candidate.images[name]
            if (
                image.changed
                or image.ref != source.images[name].ref
                or image.image_id != source.images[name].image_id
            ):
                raise ReleaseManagerError("forward rollback cannot replace data images")
        changed = {name for name in ("api", "web") if candidate.images[name].changed}
        if not changed:
            raise ReleaseManagerError("forward rollback must replace an application image")
        original_refs = cast(dict[str, object], snapshot["current_refs"])
        original_ids = cast(dict[str, object], snapshot["image_ids"])
        for name in ("api", "web"):
            image = candidate.images[name]
            differs = image.ref != source.images[name].ref
            if image.changed is not differs:
                raise ReleaseManagerError("forward rollback changed flags are invalid")
            if not image.changed:
                if image.image_id != source.images[name].image_id:
                    raise ReleaseManagerError("unchanged forward rollback image drifted")
                continue
            if (
                image.image_id == source.images[name].image_id
                or image.ref == original_refs[name]
                or image.image_id == original_ids[name]
            ):
                raise ReleaseManagerError(
                    "forward rollback cannot directly restore a previous image"
                )

    def _validate_staged_production_release(
        self,
        store: ReleaseStore,
        manifest: ReleaseManifest,
        state: Mapping[str, object],
    ) -> None:
        """跨进程恢复前重验 staged 类型；缺失或漂移一律失败关闭。"""

        if store.release_id != manifest.release_id:
            raise ReleaseManagerError("staged release identity is invalid")
        self._assert_production_topology(state)
        self._reject_historical_production_images(manifest)
        release_kind = state.get("release_kind")
        if release_kind == "standard":
            if any(
                field in state
                for field in (
                    "forward_rollback_of",
                    "forward_rollback_source_commit",
                    "schema_retained_at",
                )
            ):
                raise ReleaseManagerError("staged release metadata is invalid")
            return
        if release_kind == "bootstrap":
            raise ReleaseManagerError(
                "staged bootstrap release requires bootstrap manual recovery"
            )
        if release_kind != "forward_rollback":
            raise ReleaseManagerError("staged release metadata is invalid")
        source_release_id = state.get("forward_rollback_of")
        source_commit = state.get("forward_rollback_source_commit")
        retained_schema = state.get("schema_retained_at")
        if type(source_release_id) is not str:
            raise ReleaseManagerError("staged forward rollback metadata is invalid")
        source_store = ReleaseStore(self.release_root, source_release_id)
        source_state = source_store.read_state()
        if source_state.get("state") != ReleaseState.SUCCEEDED.value:
            raise ReleaseManagerError("forward rollback source release is not succeeded")
        self._assert_production_topology(source_state)
        source = self._stored_manifest(source_store)
        if source_commit != source.commit or retained_schema != source.migration_target:
            raise ReleaseManagerError("staged forward rollback metadata is invalid")
        self._validate_forward_rollback_candidate(
            source,
            self._read_snapshot(source_store),
            manifest,
        )

    @property
    def _bootstrap_state_path(self) -> Path:
        return self.release_root / _BOOTSTRAP_STATE_NAME

    def _read_bootstrap_state(self) -> dict[str, Any] | None:
        try:
            self._bootstrap_state_path.lstat()
        except FileNotFoundError:
            return None
        state = _exact_object(
            _read_json(self._bootstrap_state_path, "production bootstrap state"),
            _BOOTSTRAP_STATE_FIELDS,
            "production bootstrap state",
        )
        if (
            state["schema_version"] != 1
            or state["status"] not in {"running", "succeeded", "failed"}
            or type(state["release_id"]) is not str
            or type(state["commit"]) is not str
            or type(state["manifest_sha256"]) is not str
            or _REPORT_HASH_RE.fullmatch(state["manifest_sha256"]) is None
            or type(state["production_topology"]) is not dict
            or type(state["phase"]) is not str
            or type(state["started_at"]) is not str
            or type(state["updated_at"]) is not str
            or (state["failure_type"] is not None and type(state["failure_type"]) is not str)
        ):
            raise ReleaseManagerError("production bootstrap state is invalid")
        _safe_id(state["release_id"], "bootstrap release id")
        _safe_id(state["phase"], "bootstrap phase")
        _parse_utc(state["started_at"], "bootstrap started_at")
        _parse_utc(state["updated_at"], "bootstrap updated_at")
        return state

    def _write_bootstrap_state(self, state: Mapping[str, object]) -> None:
        rendered = (
            json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        ReleaseStore._atomic_write(self._bootstrap_state_path, rendered)

    @property
    def _recovery_state_path(self) -> Path:
        return self.release_root / _RECOVERY_STATE_NAME

    def _read_recovery_state(self) -> dict[str, Any] | None:
        try:
            self._recovery_state_path.lstat()
        except FileNotFoundError:
            return None
        state = _exact_object(
            _read_json(self._recovery_state_path, "production recovery state"),
            _RECOVERY_STATE_FIELDS,
            "production recovery state",
        )
        if (
            state["schema_version"] != 1
            or state["status"]
            not in {"running", "succeeded", "failed", "recovery_required"}
            or state["phase"]
            not in {
                "validated",
                "data_starting",
                "data_started",
                "observed",
                "adopted",
                "api_started",
                "callback_started",
                "workers_started",
                "outbox_started",
                "beat_started",
                "succeeded",
                "failed",
            }
            or type(state["production_topology"]) is not dict
            or any(
                type(state[field]) is not str
                for field in (
                    "release_id",
                    "commit",
                    "manifest_sha256",
                    "migration_head",
                    "snapshot_id",
                    "snapshot_manifest_sha256",
                    "snapshot_database_sha256",
                    "recovery_crypto_generation_id",
                    "backup_passphrase_generation_id",
                    "runtime_secrets_target",
                    "started_at",
                    "updated_at",
                )
            )
            or (
                state["failure_type"] is not None
                and type(state["failure_type"]) is not str
            )
            or (
                state["gap_fence_sha256"] is not None
                and type(state["gap_fence_sha256"]) is not str
            )
            or (
                state["recovery_watermark_sha256"] is not None
                and type(state["recovery_watermark_sha256"]) is not str
            )
            or (
                state["restore_receipt_sha256"] is not None
                and type(state["restore_receipt_sha256"]) is not str
            )
            or (
                state["live_database_fingerprint_sha256"] is not None
                and type(state["live_database_fingerprint_sha256"]) is not str
            )
            or (
                state["crypto_probe_status"] is not None
                and state["crypto_probe_status"]
                not in {"performed", "not_applicable_empty"}
            )
            or (
                state["crypto_probe_sha256"] is not None
                and type(state["crypto_probe_sha256"]) is not str
            )
        ):
            raise ReleaseManagerError("production recovery state is invalid")
        _safe_id(state["release_id"], "recovery release id")
        _safe_id(state["snapshot_id"], "recovery snapshot id")
        if re.fullmatch(r"[0-9a-f]{40}", state["commit"]) is None:
            raise ReleaseManagerError("production recovery state is invalid")
        for field in (
            "manifest_sha256",
            "snapshot_manifest_sha256",
            "snapshot_database_sha256",
        ):
            self._validate_evidence_digest(state[field], "production recovery state")
        for field in (
            "gap_fence_sha256",
            "recovery_watermark_sha256",
            "restore_receipt_sha256",
            "live_database_fingerprint_sha256",
            "crypto_probe_sha256",
        ):
            if state[field] is not None:
                self._validate_evidence_digest(state[field], "production recovery state")
        if _MIGRATION_RE.fullmatch(state["migration_head"]) is None:
            raise ReleaseManagerError("production recovery state is invalid")
        _parse_utc(state["started_at"], "recovery started_at")
        _parse_utc(state["updated_at"], "recovery updated_at")
        return state

    def _write_recovery_state(self, state: Mapping[str, object]) -> None:
        rendered = (
            json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        ReleaseStore._atomic_write(self._recovery_state_path, rendered)

    def _checkpoint_recovery_state(
        self,
        state: Mapping[str, object],
        *,
        status: Literal["running", "succeeded", "failed", "recovery_required"],
        phase: str,
        failure_type: str | None = None,
    ) -> dict[str, object]:
        updated = {
            **state,
            "status": status,
            "phase": phase,
            "failure_type": failure_type,
            "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        self._write_recovery_state(updated)
        return updated

    @staticmethod
    def _validate_evidence_digest(value: str, context: str) -> str:
        if _REPORT_HASH_RE.fullmatch(value) is None:
            raise ReleaseManagerError(f"{context} digest is invalid")
        return value

    @staticmethod
    def _validate_runtime_secrets_target(value: str) -> str:
        if _RUNTIME_SECRETS_TARGET_RE.fullmatch(value) is None:
            raise ReleaseManagerError("recovery runtime secrets target is invalid")
        return value

    @staticmethod
    def _validate_recovery_evidence_path(path: Path, context: str) -> None:
        if not path.is_absolute() or path.name in {"", ".", ".."}:
            raise ReleaseManagerError(f"{context} path is invalid")
        _safe_id(path.name, f"{context} filename")
        _validate_staging_directory(path.parent, os.geteuid())

    def _validate_recovery_snapshot(
        self,
        path: Path,
        expected_sha256: str,
        manifest: ReleaseManifest,
    ) -> dict[str, object]:
        """重新校验完整快照闭包，并绑定恢复 commit、schema 与当前 env。"""

        self._validate_recovery_evidence_path(path, "recovery snapshot manifest")
        if path.name != "manifest.json":
            raise ReleaseManagerError("recovery snapshot manifest filename is invalid")
        expected_sha256 = self._validate_evidence_digest(
            expected_sha256,
            "recovery snapshot manifest",
        )
        raw = _read_safe_bytes(
            path,
            expected_uid=os.geteuid(),
            expected_mode=0o600,
            maximum=_MAX_JSON_BYTES,
        )
        if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected_sha256):
            raise ReleaseManagerError("recovery snapshot manifest digest does not match")
        snapshot = _exact_object(
            _parse_json_bytes(raw, "recovery snapshot manifest"),
            _RECOVERY_SNAPSHOT_FIELDS,
            "recovery snapshot manifest",
        )
        if (
            snapshot["schema_version"] != 1
            or snapshot["secrets_included"] is not False
            or snapshot["git_commit"] != manifest.commit
            or snapshot["alembic_version"] != manifest.migration_target
            or snapshot["database"] != "sms"
        ):
            raise ReleaseManagerError("recovery snapshot manifest is not bound")
        snapshot_id = _safe_id(snapshot["snapshot_id"], "recovery snapshot id")
        recovery_crypto_generation_id = _safe_id(
            snapshot["recovery_crypto_generation_id"],
            "recovery crypto generation id",
        )
        backup_passphrase_generation_id = _safe_id(
            snapshot["backup_passphrase_generation_id"],
            "backup passphrase generation id",
        )
        created_at = _parse_utc(snapshot["created_at"], "recovery snapshot created_at")
        now = datetime.now(UTC)
        if created_at > now or (now - created_at).total_seconds() > 35 * 24 * 60 * 60:
            raise ReleaseManagerError("recovery snapshot is future-dated or beyond retention")
        files = _exact_object(
            snapshot["files"],
            frozenset({"database", "repository_archive", "environment"}),
            "recovery snapshot files",
        )
        expected_entries = {"manifest.json", "SHA256SUMS"}
        checksum_lines = {f"{expected_sha256}  manifest.json"}
        verified: dict[str, dict[str, object]] = {}
        try:
            for label in ("database", "repository_archive", "environment"):
                item = _exact_object(
                    files[label],
                    _RECOVERY_SNAPSHOT_FILE_FIELDS,
                    "recovery snapshot file",
                )
                name = _safe_id(item["name"], "recovery snapshot filename")
                if name in expected_entries:
                    raise ReleaseManagerError("recovery snapshot filenames are not unique")
                digest = item["sha256"]
                size = item["size"]
                if (
                    type(digest) is not str
                    or _REPORT_HASH_RE.fullmatch(digest) is None
                    or type(size) is not int
                    or size <= 0
                ):
                    raise ReleaseManagerError("recovery snapshot file metadata is invalid")
                candidate = path.parent / name
                info = candidate.lstat()
                if (
                    stat.S_ISLNK(info.st_mode)
                    or not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_size != size
                    or not hmac.compare_digest(_hash_file(candidate), digest)
                ):
                    raise ReleaseManagerError("recovery snapshot file verification failed")
                expected_entries.add(name)
                checksum_lines.add(f"{digest}  {name}")
                verified[label] = {"name": name, "sha256": digest, "size": size}
            checksum_path = path.parent / "SHA256SUMS"
            checksum_info = checksum_path.lstat()
            if (
                stat.S_ISLNK(checksum_info.st_mode)
                or not stat.S_ISREG(checksum_info.st_mode)
                or checksum_info.st_uid != os.geteuid()
                or stat.S_IMODE(checksum_info.st_mode) != 0o600
            ):
                raise ReleaseManagerError("recovery snapshot checksum file is unsafe")
            checksum_raw = _read_safe_bytes(
                checksum_path,
                expected_uid=os.geteuid(),
                expected_mode=0o600,
                maximum=_MAX_JSON_BYTES,
            )
            try:
                observed_checksum_lines = checksum_raw.decode("ascii").splitlines()
            except UnicodeError as exc:
                raise ReleaseManagerError(
                    "recovery snapshot checksum file is invalid"
                ) from exc
            if (
                len(observed_checksum_lines) != len(checksum_lines)
                or set(observed_checksum_lines) != checksum_lines
            ):
                raise ReleaseManagerError("recovery snapshot checksum inventory does not match")
            entries = list(os.scandir(path.parent))
            if {entry.name for entry in entries} != expected_entries:
                raise ReleaseManagerError("recovery snapshot is not a closed file set")
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                if entry.is_symlink() or not stat.S_ISREG(info.st_mode):
                    raise ReleaseManagerError("recovery snapshot contains an unsafe path")
        except OSError as exc:
            raise ReleaseManagerError("recovery snapshot is unavailable or unsafe") from exc

        environment = verified["environment"]
        if not hmac.compare_digest(
            _hash_file(self.root / ".env"),
            cast(str, environment["sha256"]),
        ):
            raise ReleaseManagerError("recovery snapshot environment has drifted")
        return {
            "snapshot_id": snapshot_id,
            "created_at": created_at,
            "manifest_sha256": expected_sha256,
            "database_sha256": verified["database"]["sha256"],
            "recovery_crypto_generation_id": recovery_crypto_generation_id,
            "backup_passphrase_generation_id": backup_passphrase_generation_id,
        }

    def _validate_gap_fence_evidence(
        self,
        path: Path,
        expected_sha256: str,
        manifest: ReleaseManifest,
        snapshot: Mapping[str, object],
        recovery_started_at: str,
    ) -> str:
        """验证缺口围栏已双人批准，且未知结果继续禁止自动重发。"""

        self._validate_recovery_evidence_path(path, "recovery gap-fence evidence")
        expected_sha256 = self._validate_evidence_digest(
            expected_sha256,
            "recovery gap-fence evidence",
        )
        raw = _read_safe_bytes(
            path,
            expected_uid=os.geteuid(),
            expected_mode=0o600,
            maximum=_MAX_JSON_BYTES,
        )
        if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected_sha256):
            raise ReleaseManagerError("recovery gap-fence evidence digest does not match")
        evidence = _exact_object(
            _parse_json_bytes(raw, "recovery gap-fence evidence"),
            _RECOVERY_GAP_FENCE_FIELDS,
            "recovery gap-fence evidence",
        )
        if (
            evidence["schema_version"] != 1
            or evidence["record_type"] != "production_recovery_gap_fence"
            or evidence["status"] != "approved"
            or evidence["snapshot_id"] != snapshot["snapshot_id"]
            or evidence["snapshot_manifest_sha256"] != snapshot["manifest_sha256"]
            or evidence["git_commit"] != manifest.commit
            or evidence["migration_head"] != manifest.migration_target
            or any(
                evidence[field] is not True
                for field in (
                    "old_primary_isolated",
                    "upstream_retries_frozen",
                    "unknown_results_blocked",
                    "automatic_resend_forbidden",
                )
            )
        ):
            raise ReleaseManagerError("recovery gap-fence evidence is not approved and bound")
        reviewers = evidence["approved_by"]
        if type(reviewers) is not list or len(reviewers) != 2:
            raise ReleaseManagerError("recovery gap-fence requires two reviewers")
        validated_reviewers = [
            _safe_id(reviewer, "recovery gap-fence reviewer") for reviewer in reviewers
        ]
        if len(set(validated_reviewers)) != 2:
            raise ReleaseManagerError("recovery gap-fence requires two distinct reviewers")
        counts: list[int] = []
        for field in (
            "upstream_request_count",
            "vendor_accepted_or_sent_count",
            "vendor_not_accepted_count",
            "vendor_unknown_count",
        ):
            value = evidence[field]
            if type(value) is not int or value < 0:
                raise ReleaseManagerError("recovery gap-fence counts are invalid")
            counts.append(value)
        if counts[0] != sum(counts[1:]):
            raise ReleaseManagerError("recovery gap-fence counts do not reconcile")
        started = _parse_utc(evidence["window_started_at"], "gap-fence window start")
        ended = _parse_utc(evidence["window_ended_at"], "gap-fence window end")
        approved = _parse_utc(evidence["approved_at"], "gap-fence approval")
        recovery_started = _parse_utc(
            recovery_started_at,
            "recovery started_at",
        )
        now = datetime.now(UTC)
        if (
            started != snapshot["created_at"]
            or not started <= ended <= approved <= now
            or (ended - started).total_seconds() > 24 * 60 * 60
            or approved < recovery_started
        ):
            raise ReleaseManagerError("recovery gap-fence timestamps are not bound")
        return expected_sha256

    def _recovery_live_database_fingerprint(self) -> str:
        """读取无 PII 的生产库身份与事实表水位，绑定实际恢复库而非文件声明。"""

        query = (
            "SELECT json_build_object("
            "'database',current_database(),"
            "'database_oid',(SELECT oid::text FROM pg_database WHERE datname=current_database()),"
            "'migration_head',(SELECT version_num FROM alembic_version),"
            "'batch_rows',(SELECT count(*) FROM sms_batch),"
            "'chunk_rows',(SELECT count(*) FROM sms_chunk),"
            "'outbox_rows',(SELECT count(*) FROM outbox_event),"
            "'max_batch_id',COALESCE((SELECT max(id) FROM sms_batch),0),"
            "'max_chunk_id',COALESCE((SELECT max(id) FROM sms_chunk),0)"
            ")::text"
        )
        probe = (
            "exec psql --no-psqlrc --set=ON_ERROR_STOP=1 --tuples-only --no-align "
            '--username "$POSTGRES_USER" --dbname "$POSTGRES_DB" '
            f"--command \"{query}\""
        )
        raw = self._line(
            self._run(
                self._compose() + ["exec", "-T", "postgres", "sh", "-ec", probe],
                "recovery live database fingerprint",
            ),
            "recovery live database fingerprint",
        )
        try:
            value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ReleaseManagerError("recovery live database fingerprint is invalid") from exc
        fields = frozenset(
            {
                "database",
                "database_oid",
                "migration_head",
                "batch_rows",
                "chunk_rows",
                "outbox_rows",
                "max_batch_id",
                "max_chunk_id",
            }
        )
        fingerprint = _exact_object(value, fields, "recovery live database fingerprint")
        if (
            fingerprint["database"] != "sms"
            or type(fingerprint["database_oid"]) is not str
            or not fingerprint["database_oid"].isdigit()
            or type(fingerprint["migration_head"]) is not str
            or _MIGRATION_RE.fullmatch(fingerprint["migration_head"]) is None
            or any(
                type(fingerprint[field]) is not int or fingerprint[field] < 0
                for field in fields
                - {"database", "database_oid", "migration_head"}
            )
        ):
            raise ReleaseManagerError("recovery live database fingerprint is invalid")
        canonical = json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(canonical).hexdigest()

    def _recovery_crypto_probe(self) -> tuple[str, str]:
        """用当前绑定密钥包只读验证审计 key 与历史密文，输出仅含计数。"""

        result = self._run(
            self._compose()
            + [
                "run",
                "--rm",
                "--no-deps",
                "migrate",
                "python",
                "-m",
                "scripts_support.recovery_crypto_probe",
            ],
            "recovery crypto generation probe",
        )
        if len(result.stdout.encode()) > 8192:
            raise ReleaseManagerError("recovery crypto probe result is invalid")
        try:
            value = json.loads(result.stdout, object_pairs_hook=_reject_duplicate_keys)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ReleaseManagerError("recovery crypto probe result is invalid") from exc
        status = self._validate_restore_crypto_probe_receipt(
            value,
            "recovery crypto probe",
        )
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return status, hashlib.sha256(canonical).hexdigest()

    def _validate_recovery_restore_receipt(
        self,
        path: Path,
        expected_sha256: str,
        manifest: ReleaseManifest,
        snapshot: Mapping[str, object],
        live_fingerprint_sha256: str,
        crypto_probe_status: str,
        crypto_probe_sha256: str,
        recovery_started_at: str,
    ) -> str:
        """验证人工批准的实际恢复回执与机器读取的生产库指纹完全一致。"""

        self._validate_recovery_evidence_path(path, "recovery restore receipt")
        expected_sha256 = self._validate_evidence_digest(
            expected_sha256,
            "recovery restore receipt",
        )
        raw = _read_safe_bytes(
            path,
            expected_uid=os.geteuid(),
            expected_mode=0o600,
            maximum=_MAX_JSON_BYTES,
        )
        if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected_sha256):
            raise ReleaseManagerError("recovery restore receipt digest does not match")
        receipt = _exact_object(
            _parse_json_bytes(raw, "recovery restore receipt"),
            _RECOVERY_RESTORE_RECEIPT_FIELDS,
            "recovery restore receipt",
        )
        if (
            receipt["schema_version"] != 1
            or receipt["record_type"] != "production_recovery_restore_receipt"
            or receipt["status"] != "approved"
            or receipt["snapshot_id"] != snapshot["snapshot_id"]
            or receipt["snapshot_manifest_sha256"] != snapshot["manifest_sha256"]
            or receipt["snapshot_database_sha256"] != snapshot["database_sha256"]
            or receipt["git_commit"] != manifest.commit
            or receipt["migration_head"] != manifest.migration_target
            or receipt["database"] != "sms"
            or receipt["recovery_crypto_generation_id"]
            != snapshot["recovery_crypto_generation_id"]
            or receipt["backup_passphrase_generation_id"]
            != snapshot["backup_passphrase_generation_id"]
            or type(receipt["live_database_fingerprint_sha256"]) is not str
            or not hmac.compare_digest(
                receipt["live_database_fingerprint_sha256"],
                live_fingerprint_sha256,
            )
            or receipt["crypto_probe_status"] != crypto_probe_status
            or type(receipt["crypto_probe_sha256"]) is not str
            or not hmac.compare_digest(
                receipt["crypto_probe_sha256"],
                crypto_probe_sha256,
            )
        ):
            raise ReleaseManagerError("recovery restore receipt is not approved and bound")
        reviewers = receipt["approved_by"]
        if type(reviewers) is not list or len(reviewers) != 2:
            raise ReleaseManagerError("recovery restore receipt requires two reviewers")
        reviewers = [_safe_id(item, "recovery restore reviewer") for item in reviewers]
        if len(set(reviewers)) != 2:
            raise ReleaseManagerError("recovery restore receipt requires distinct reviewers")
        restored_at = _parse_utc(receipt["restored_at"], "recovery restored_at")
        approved_at = _parse_utc(receipt["approved_at"], "recovery restore approved_at")
        recovery_started = _parse_utc(
            recovery_started_at,
            "recovery started_at",
        )
        now = datetime.now(UTC)
        snapshot_created_at = cast(datetime, snapshot["created_at"])
        if (
            not snapshot_created_at <= restored_at <= approved_at <= now
            or restored_at < recovery_started
        ):
            raise ReleaseManagerError("recovery restore receipt timestamps are invalid")
        return expected_sha256

    def _assert_recovery_services_stopped(self, services: Sequence[str]) -> None:
        compose = self._compose()
        for service in services:
            if service not in _BOOTSTRAP_CONTAINMENT_SERVICES:
                raise ReleaseManagerError("recovery containment selection is invalid")
            observed = self.runner.run(compose + ["ps", "-q", service], cwd=self.root)
            if observed.returncode != 0 or observed.stdout.strip():
                raise ReleaseManagerError(
                    "recovery consumer and outbound fence is not closed"
                )

    def _assert_recovery_consumers_stopped(self) -> None:
        self._assert_recovery_services_stopped(_BOOTSTRAP_CONTAINMENT_SERVICES)

    def _contain_recovery_consumers(self) -> None:
        compose = self._compose()
        self._run(
            compose + ["stop", *_BOOTSTRAP_CONTAINMENT_SERVICES],
            "production recovery consumer containment",
        )
        self._assert_recovery_consumers_stopped()

    def _record_recovery_failure(
        self,
        state: Mapping[str, object],
        exc: Exception,
        *,
        store: ReleaseStore | None = None,
    ) -> ReleaseManagerError:
        """任何已建围栏后的异常都先围堵并持久化可读失败状态。"""

        containment_error: Exception | None = None
        try:
            self._contain_recovery_consumers()
        except Exception as failure:
            containment_error = failure
        store_state: object = None
        if store is not None:
            with suppress(Exception):
                store_state = store.read_state().get("state")
            if store_state == ReleaseState.ACTIVATING.value:
                with suppress(Exception):
                    store.transition(
                        ReleaseState.ACTIVATING,
                        ReleaseState.RECOVERY_REQUIRED,
                        recovery_failure_type=type(exc).__name__,
                    )
        critical = (
            containment_error is not None
            or store_state
            in {ReleaseState.ACTIVATING.value, ReleaseState.SUCCEEDED.value}
            or state.get("status") == "succeeded"
        )
        with suppress(Exception):
            self._checkpoint_recovery_state(
                state,
                status="recovery_required" if critical else "failed",
                phase="failed",
                failure_type=(
                    "RecoveryContainmentFailed"
                    if containment_error is not None
                    else type(exc).__name__
                ),
            )
        if containment_error is not None:
            return ReleaseManagerError("recovery containment failed; recovery_required")
        if isinstance(exc, ReleaseManagerError):
            return exc
        return ReleaseManagerError("recovery operation failed")

    def _recovery_watermark(self) -> dict[str, object]:
        """在消费面停止时读取不含 PII 的发送/outbox 稳定水位。"""

        query = (
            "SELECT json_build_object("
            "'batch_queued',(SELECT count(*) FROM sms_batch WHERE status='queued'),"
            "'batch_sending',(SELECT count(*) FROM sms_batch WHERE status='sending'),"
            "'chunk_pending',(SELECT count(*) FROM sms_chunk WHERE status='pending'),"
            "'submitting',(SELECT count(*) FROM sms_chunk WHERE status='submitting'),"
            "'retrying',(SELECT count(*) FROM sms_chunk WHERE status='retrying'),"
            "'submitted',(SELECT count(*) FROM sms_chunk WHERE status='submitted'),"
            "'uncertain',(SELECT count(*) FROM sms_chunk WHERE status='uncertain'),"
            "'max_chunk_id',COALESCE((SELECT max(id) FROM sms_chunk),0),"
            "'outbox_pending',(SELECT count(*) FROM outbox_event WHERE state='pending'),"
            "'outbox_leased',(SELECT count(*) FROM outbox_event WHERE state='leased'),"
            "'outbox_processing',(SELECT count(*) FROM outbox_event WHERE state='processing'),"
            "'max_outbox_created_at',COALESCE("
            "(SELECT max(created_at)::text FROM outbox_event),'')"
            ")::text"
        )
        probe = (
            "exec psql --no-psqlrc --set=ON_ERROR_STOP=1 --tuples-only --no-align "
            '--username "$POSTGRES_USER" --dbname "$POSTGRES_DB" '
            f"--command \"{query}\""
        )
        raw = self._line(
            self._run(
                self._compose() + ["exec", "-T", "postgres", "sh", "-ec", probe],
                "recovery stable watermark observation",
            ),
            "recovery stable watermark observation",
        )
        try:
            value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ReleaseManagerError("recovery stable watermark is invalid") from exc
        fields = frozenset(
            {
                "submitting",
                "retrying",
                "submitted",
                "uncertain",
                "batch_queued",
                "batch_sending",
                "chunk_pending",
                "max_chunk_id",
                "outbox_pending",
                "outbox_leased",
                "outbox_processing",
                "max_outbox_created_at",
            }
        )
        watermark = _exact_object(value, fields, "recovery stable watermark")
        for field in fields - {"max_outbox_created_at"}:
            if type(watermark[field]) is not int or watermark[field] < 0:
                raise ReleaseManagerError("recovery stable watermark is invalid")
        if type(watermark["max_outbox_created_at"]) is not str:
            raise ReleaseManagerError("recovery stable watermark is invalid")
        in_flight_fields = (
            "submitting",
            "retrying",
            "outbox_leased",
            "outbox_processing",
        )
        if any(watermark[field] != 0 for field in in_flight_fields):
            raise ReleaseManagerError("recovery runtime has in-flight sending work")
        return cast(dict[str, object], watermark)

    def _stored_recovery_watermark(
        self,
        store: ReleaseStore,
        expected_sha256: str,
    ) -> dict[str, object]:
        raw = _read_safe_bytes(
            store.release_dir / "artifacts" / "recovery-watermark.json",
            expected_uid=os.geteuid(),
            expected_mode=0o600,
            maximum=_MAX_JSON_BYTES,
        )
        if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected_sha256):
            raise ReleaseManagerError("stored recovery watermark receipt has drifted")
        receipt = _exact_object(
            _parse_json_bytes(raw, "recovery watermark receipt"),
            frozenset({"schema_version", "captured_at", "values"}),
            "recovery watermark receipt",
        )
        if receipt["schema_version"] != 1:
            raise ReleaseManagerError("stored recovery watermark receipt is invalid")
        _parse_utc(receipt["captured_at"], "recovery watermark captured_at")
        if type(receipt["values"]) is not dict:
            raise ReleaseManagerError("stored recovery watermark receipt is invalid")
        return cast(dict[str, object], receipt["values"])

    def start_recovery(
        self,
        manifest_path: Path,
        *,
        snapshot_manifest_path: Path,
        snapshot_manifest_sha256: str,
        runtime_secrets_target: str,
        confirmed_recovered_host: bool,
    ) -> dict[str, object]:
        """建立持久恢复围栏，停止消费面后只启动四个数据服务。"""

        if self.mode != "production" or not confirmed_recovered_host:
            raise ReleaseManagerError(
                "recovery start requires explicit recovered-host confirmation"
            )
        try:
            ReleaseStore._validate_directory(self.release_root)
            if self._read_bootstrap_state() is not None:
                raise ReleaseManagerError("recovery start refuses an initialized release host")
            entries = {entry.name for entry in os.scandir(self.release_root)}
            if entries - {_RECOVERY_STATE_NAME}:
                raise ReleaseManagerError("recovery start release root is not isolated")
            manifest, manifest_bytes, _ = _validate_staging_bundle(
                manifest_path,
                self._staging_uid(),
            )
            if (
                manifest.mode != "production"
                or any(image.changed for image in manifest.images.values())
                or manifest.migration_from != manifest.migration_target
                or manifest.migration_compatibility is not MigrationCompatibility.NONE
            ):
                raise ReleaseManagerError(
                    "recovery start requires a production no-delta manifest"
                )
            topology = self._production_topology()
            refs = self._root_env_refs()
            if refs != {name: manifest.images[name].ref for name in _IMAGE_NAMES}:
                raise ReleaseManagerError("recovery start root env does not match manifest")
            self._validate_git(manifest)
            self._validate_migration_direction(manifest)
            gate_kind, _ = self._validate_release_evidence(
                manifest,
                manifest_path.parent,
            )
            if gate_kind != "release":
                raise ReleaseManagerError("recovery start requires release evidence")
            if self._validate_data_evidence(manifest, manifest_path.parent) is not None:
                raise ReleaseManagerError("recovery start cannot replace data images")
            self._validate_backup_evidence(manifest, manifest_path.parent)
            self._target_images(manifest, manifest_path.parent)
            snapshot = self._validate_recovery_snapshot(
                snapshot_manifest_path,
                snapshot_manifest_sha256,
                manifest,
            )
            manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
            runtime_secrets_target = self._validate_runtime_secrets_target(
                runtime_secrets_target
            )
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            expected: dict[str, object] = {
                "schema_version": 1,
                "status": "running",
                "phase": "validated",
                "release_id": manifest.release_id,
                "commit": manifest.commit,
                "manifest_sha256": manifest_sha256,
                "production_topology": topology,
                "runtime_secrets_target": runtime_secrets_target,
                "migration_head": manifest.migration_target,
                "snapshot_id": snapshot["snapshot_id"],
                "snapshot_manifest_sha256": snapshot["manifest_sha256"],
                "snapshot_database_sha256": snapshot["database_sha256"],
                "recovery_crypto_generation_id": snapshot[
                    "recovery_crypto_generation_id"
                ],
                "backup_passphrase_generation_id": snapshot[
                    "backup_passphrase_generation_id"
                ],
                "restore_receipt_sha256": None,
                "live_database_fingerprint_sha256": None,
                "crypto_probe_status": None,
                "crypto_probe_sha256": None,
                "gap_fence_sha256": None,
                "recovery_watermark_sha256": None,
                "started_at": now,
                "updated_at": now,
                "failure_type": None,
            }
            state = self._read_recovery_state()
            if state is None:
                if entries:
                    raise ReleaseManagerError("recovery start state is unavailable")
                self._write_recovery_state(expected)
                state = expected
            else:
                stable = set(_RECOVERY_STATE_FIELDS) - {
                    "status",
                    "phase",
                    "started_at",
                    "updated_at",
                    "failure_type",
                }
                if any(state[field] != expected[field] for field in stable):
                    raise ReleaseManagerError("recovery start state has drifted")
                if state["status"] != "running":
                    raise ReleaseManagerError("recovery start state requires manual recovery")

            phase = cast(str, state["phase"])
            if phase not in {"validated", "data_starting", "data_started"}:
                raise ReleaseManagerError("recovery data start order is invalid")
            state = self._checkpoint_recovery_state(
                state,
                status="running",
                phase="data_starting",
            )
            try:
                self._contain_recovery_consumers()
                self._run(
                    self._compose()
                    + [
                        "up",
                        "-d",
                        "--no-deps",
                        "--wait",
                        "--wait-timeout",
                        "120",
                        "postgres",
                        "redis",
                        "redis-auth",
                        "redis-control",
                    ],
                    "production recovery data start",
                )
            except Exception as exc:
                raise self._record_recovery_failure(state, exc) from exc
            self._assert_recovery_consumers_stopped()
            self._current_runtime(
                refs,
                ("postgres", "redis", "redis-auth", "redis-control"),
            )
            return self._checkpoint_recovery_state(
                state,
                status="running",
                phase="data_started",
            )
        except ReleaseManagerError as exc:
            try:
                current_recovery = self._read_recovery_state()
            except (ReleaseManagerError, OSError):
                current_recovery = None
            if current_recovery is not None and current_recovery.get("status") in {
                "running",
                "succeeded",
            }:
                raise self._record_recovery_failure(current_recovery, exc) from exc
            raise
        except ReleaseStoreError as exc:
            raise ReleaseManagerError("recovery start state is unavailable") from exc
        except OSError as exc:
            raise ReleaseManagerError("recovery start evidence is unavailable") from exc

    def observe_recovery(
        self,
        manifest_path: Path,
        *,
        snapshot_manifest_path: Path,
        snapshot_manifest_sha256: str,
        output_path: Path,
        runtime_secrets_target: str,
        confirmed_recovered_host: bool,
    ) -> dict[str, object]:
        """在持久围栏内生成无 PII 的实际恢复回执待批模板。"""

        if self.mode != "production" or not confirmed_recovered_host:
            raise ReleaseManagerError(
                "recovery observation requires explicit recovered-host confirmation"
            )
        try:
            ReleaseStore._validate_directory(self.release_root)
            recovery = self._read_recovery_state()
            if (
                recovery is None
                or recovery["status"] != "running"
                or recovery["phase"] != "data_started"
            ):
                raise ReleaseManagerError("recovery observation requires data_started")
            manifest, manifest_bytes, _ = _validate_staging_bundle(
                manifest_path,
                self._staging_uid(),
            )
            topology = self._production_topology()
            refs = self._root_env_refs()
            runtime_secrets_target = self._validate_runtime_secrets_target(
                runtime_secrets_target
            )
            if (
                manifest.mode != "production"
                or any(image.changed for image in manifest.images.values())
                or manifest.migration_from != manifest.migration_target
                or manifest.migration_compatibility is not MigrationCompatibility.NONE
                or hashlib.sha256(manifest_bytes).hexdigest()
                != recovery["manifest_sha256"]
                or manifest.release_id != recovery["release_id"]
                or manifest.commit != recovery["commit"]
                or manifest.migration_target != recovery["migration_head"]
                or topology != recovery["production_topology"]
                or runtime_secrets_target != recovery["runtime_secrets_target"]
                or refs != {name: manifest.images[name].ref for name in _IMAGE_NAMES}
            ):
                raise ReleaseManagerError("recovery observation binding has drifted")
            self._validate_git(manifest)
            self._assert_recovery_consumers_stopped()
            _, image_ids, _ = self._current_runtime(refs, _RECOVERY_DATA_SERVICES)
            if any(
                not hmac.compare_digest(manifest.images[name].image_id, image_id)
                for name, image_id in image_ids.items()
            ):
                raise ReleaseManagerError("recovery observation data images have drifted")
            if self._observed_migration_head() != manifest.migration_target:
                raise ReleaseManagerError("recovery observation migration has drifted")
            snapshot = self._validate_recovery_snapshot(
                snapshot_manifest_path,
                snapshot_manifest_sha256,
                manifest,
            )
            expected_snapshot = {
                "snapshot_id": recovery["snapshot_id"],
                "manifest_sha256": recovery["snapshot_manifest_sha256"],
                "database_sha256": recovery["snapshot_database_sha256"],
                "recovery_crypto_generation_id": recovery[
                    "recovery_crypto_generation_id"
                ],
                "backup_passphrase_generation_id": recovery[
                    "backup_passphrase_generation_id"
                ],
            }
            if any(snapshot[field] != value for field, value in expected_snapshot.items()):
                raise ReleaseManagerError("recovery observation snapshot has drifted")
            live_fingerprint = self._recovery_live_database_fingerprint()
            if not hmac.compare_digest(
                self._recovery_live_database_fingerprint(),
                live_fingerprint,
            ):
                raise ReleaseManagerError("recovery live database fingerprint is not stable")
            crypto_probe_status, crypto_probe_digest = self._recovery_crypto_probe()
            self._assert_recovery_consumers_stopped()
            self._validate_recovery_evidence_path(
                output_path,
                "recovery restore receipt output",
            )
            try:
                output_path.lstat()
            except FileNotFoundError:
                pass
            else:
                raise ReleaseManagerError("recovery restore receipt output already exists")
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            template: dict[str, object] = {
                "schema_version": 1,
                "record_type": "production_recovery_restore_receipt",
                "status": "pending_approval",
                "snapshot_id": snapshot["snapshot_id"],
                "snapshot_manifest_sha256": snapshot["manifest_sha256"],
                "snapshot_database_sha256": snapshot["database_sha256"],
                "git_commit": manifest.commit,
                "migration_head": manifest.migration_target,
                "database": "sms",
                "recovery_crypto_generation_id": snapshot[
                    "recovery_crypto_generation_id"
                ],
                "backup_passphrase_generation_id": snapshot[
                    "backup_passphrase_generation_id"
                ],
                "live_database_fingerprint_sha256": live_fingerprint,
                "crypto_probe_status": crypto_probe_status,
                "crypto_probe_sha256": crypto_probe_digest,
                "restored_at": now,
                "approved_by": [],
                "approved_at": None,
            }
            ReleaseStore._atomic_write(
                output_path,
                (
                    json.dumps(template, sort_keys=True, separators=(",", ":"))
                    + "\n"
                ).encode(),
            )
            self._checkpoint_recovery_state(
                {
                    **recovery,
                    "live_database_fingerprint_sha256": live_fingerprint,
                    "crypto_probe_status": crypto_probe_status,
                    "crypto_probe_sha256": crypto_probe_digest,
                },
                status="running",
                phase="observed",
            )
            return template
        except ReleaseManagerError as exc:
            try:
                current_recovery = self._read_recovery_state()
            except (ReleaseManagerError, OSError):
                current_recovery = None
            if current_recovery is not None and current_recovery.get("status") in {
                "running",
                "succeeded",
            }:
                raise self._record_recovery_failure(current_recovery, exc) from exc
            raise
        except ReleaseStoreError as exc:
            raise ReleaseManagerError("recovery observation state is unavailable") from exc
        except OSError as exc:
            raise ReleaseManagerError("recovery observation evidence is unavailable") from exc

    def adopt_recovery(
        self,
        manifest_path: Path,
        *,
        snapshot_manifest_path: Path,
        snapshot_manifest_sha256: str,
        restore_receipt_path: Path,
        restore_receipt_sha256: str,
        gap_fence_path: Path,
        gap_fence_sha256: str,
        runtime_secrets_target: str,
        confirmed_recovered_host: bool,
    ) -> dict[str, object]:
        """在数据层恢复完成且消费面仍关闭时封存 recovery baseline。"""

        if self.mode != "production" or not confirmed_recovered_host:
            raise ReleaseManagerError(
                "recovery baseline adoption requires explicit recovered-host confirmation"
            )
        try:
            ReleaseStore._validate_directory(self.release_root)
            if self._read_bootstrap_state() is not None:
                raise ReleaseManagerError("recovery adoption refuses an initialized release host")
            recovery = self._read_recovery_state()
            if (
                recovery is None
                or recovery["status"] != "running"
                or recovery["phase"] not in {"observed", "adopted"}
            ):
                raise ReleaseManagerError("recovery adoption requires observed evidence")
            manifest, manifest_bytes, staging_files = _validate_staging_bundle(
                manifest_path,
                self._staging_uid(),
            )
            manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
            if (
                manifest.mode != "production"
                or any(image.changed for image in manifest.images.values())
                or manifest.migration_from != manifest.migration_target
                or manifest.migration_compatibility is not MigrationCompatibility.NONE
            ):
                raise ReleaseManagerError(
                    "recovery baseline adoption requires a production no-delta manifest"
                )
            topology = self._production_topology()
            refs = self._root_env_refs()
            runtime_secrets_target = self._validate_runtime_secrets_target(
                runtime_secrets_target
            )
            target_refs = {name: manifest.images[name].ref for name in _IMAGE_NAMES}
            if refs != target_refs:
                raise ReleaseManagerError("recovery baseline root env does not match manifest")
            self._validate_git(manifest)
            self._validate_migration_direction(manifest)
            gate_kind, release_scan_performed = self._validate_release_evidence(
                manifest,
                manifest_path.parent,
            )
            if gate_kind != "release":
                raise ReleaseManagerError("recovery baseline requires release evidence")
            if self._validate_data_evidence(manifest, manifest_path.parent) is not None:
                raise ReleaseManagerError("recovery baseline cannot replace data images")
            self._validate_backup_evidence(manifest, manifest_path.parent)
            target_ids = self._target_images(manifest, manifest_path.parent)
            self._assert_recovery_consumers_stopped()
            _, data_image_ids, _ = self._current_runtime(refs, _RECOVERY_DATA_SERVICES)
            if any(
                not hmac.compare_digest(target_ids[name], image_id)
                for name, image_id in data_image_ids.items()
            ):
                raise ReleaseManagerError("recovery data image IDs do not match manifest")
            migration_head = self._observed_migration_head()
            if migration_head != manifest.migration_target:
                raise ReleaseManagerError("recovery runtime migration has drifted")
            snapshot = self._validate_recovery_snapshot(
                snapshot_manifest_path,
                snapshot_manifest_sha256,
                manifest,
            )
            live_fingerprint = self._recovery_live_database_fingerprint()
            if not hmac.compare_digest(
                self._recovery_live_database_fingerprint(),
                live_fingerprint,
            ):
                raise ReleaseManagerError("recovery live database fingerprint is not stable")
            crypto_probe_status, crypto_probe_digest = self._recovery_crypto_probe()
            self._assert_recovery_consumers_stopped()
            restore_digest = self._validate_recovery_restore_receipt(
                restore_receipt_path,
                restore_receipt_sha256,
                manifest,
                snapshot,
                live_fingerprint,
                crypto_probe_status,
                crypto_probe_digest,
                cast(str, recovery["started_at"]),
            )
            gap_digest = self._validate_gap_fence_evidence(
                gap_fence_path,
                gap_fence_sha256,
                manifest,
                snapshot,
                cast(str, recovery["started_at"]),
            )
            entries = {entry.name for entry in os.scandir(self.release_root)}
            if entries not in (
                {_RECOVERY_STATE_NAME},
                {_RECOVERY_STATE_NAME, manifest.release_id},
            ):
                raise ReleaseManagerError("recovery baseline release root is not isolated")

            expected_recovery = {
                "release_id": manifest.release_id,
                "commit": manifest.commit,
                "manifest_sha256": manifest_sha256,
                "production_topology": topology,
                "runtime_secrets_target": runtime_secrets_target,
                "migration_head": migration_head,
                "snapshot_id": snapshot["snapshot_id"],
                "snapshot_manifest_sha256": snapshot["manifest_sha256"],
                "snapshot_database_sha256": snapshot["database_sha256"],
                "recovery_crypto_generation_id": snapshot[
                    "recovery_crypto_generation_id"
                ],
                "backup_passphrase_generation_id": snapshot[
                    "backup_passphrase_generation_id"
                ],
                "live_database_fingerprint_sha256": live_fingerprint,
                "crypto_probe_status": crypto_probe_status,
                "crypto_probe_sha256": crypto_probe_digest,
            }
            if any(recovery[field] != value for field, value in expected_recovery.items()):
                raise ReleaseManagerError("recovery adoption state binding has drifted")

            first_watermark = self._recovery_watermark()
            second_watermark = self._recovery_watermark()
            if first_watermark != second_watermark:
                raise ReleaseManagerError("recovery sending watermark is not stable")
            watermark_receipt = {
                "schema_version": 1,
                "captured_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "values": first_watermark,
            }
            watermark_bytes = (
                json.dumps(watermark_receipt, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode()
            watermark_digest = hashlib.sha256(watermark_bytes).hexdigest()

            metadata: dict[str, object] = {
                "production_topology": topology,
                "runtime_secrets_target": runtime_secrets_target,
                "release_kind": "recovery_baseline",
                "release_gate_kind": gate_kind,
                "release_scan_performed": release_scan_performed,
                "control_smoke_only": False,
                "snapshot_id": snapshot["snapshot_id"],
                "snapshot_manifest_sha256": snapshot["manifest_sha256"],
                "snapshot_database_sha256": snapshot["database_sha256"],
                "recovery_crypto_generation_id": snapshot[
                    "recovery_crypto_generation_id"
                ],
                "backup_passphrase_generation_id": snapshot[
                    "backup_passphrase_generation_id"
                ],
                "restore_receipt_sha256": restore_digest,
                "live_database_fingerprint_sha256": live_fingerprint,
                "gap_fence_sha256": gap_digest,
                "recovery_migration_head": migration_head,
                "recovery_watermark_sha256": watermark_digest,
            }
            store = ReleaseStore(self.release_root, manifest.release_id)
            self._freeze_bootstrap_bundle(
                manifest_path,
                manifest_bytes,
                staging_files,
                store,
            )
            artifacts = store.release_dir / "artifacts"
            _copy_atomic(
                snapshot_manifest_path,
                artifacts / "recovery-snapshot-manifest.json",
            )
            _copy_atomic(
                restore_receipt_path,
                artifacts / "recovery-restore-receipt.json",
            )
            _copy_atomic(gap_fence_path, artifacts / "recovery-gap-fence.json")
            ReleaseStore._atomic_write(
                artifacts / "recovery-watermark.json",
                watermark_bytes,
            )
            state = store.read_state()
            for key, value in metadata.items():
                if key in state and state[key] != value:
                    raise ReleaseManagerError("recovery baseline state metadata has drifted")
            state_value = state.get("state")
            if state_value not in {ReleaseState.STAGED.value, ReleaseState.PREPARED.value}:
                raise ReleaseManagerError("recovery baseline state requires manual recovery")

            if state_value == ReleaseState.STAGED.value:
                store.checkpoint(ReleaseState.STAGED, **metadata)
            if (
                self._validate_git(manifest) != manifest.commit
                or self._root_env_refs() != refs
                or self._production_topology() != topology
                or self._observed_migration_head() != migration_head
                or self._validate_recovery_snapshot(
                    snapshot_manifest_path,
                    snapshot_manifest_sha256,
                    manifest,
                )
                != snapshot
                or self._validate_gap_fence_evidence(
                    gap_fence_path,
                    gap_fence_sha256,
                    manifest,
                    snapshot,
                    cast(str, recovery["started_at"]),
                )
                != gap_digest
                or self._validate_recovery_restore_receipt(
                    restore_receipt_path,
                    restore_receipt_sha256,
                    manifest,
                    snapshot,
                    live_fingerprint,
                    crypto_probe_status,
                    crypto_probe_digest,
                    cast(str, recovery["started_at"]),
                )
                != restore_digest
                or self._recovery_crypto_probe()
                != (crypto_probe_status, crypto_probe_digest)
                or not hmac.compare_digest(
                    self._recovery_live_database_fingerprint(),
                    live_fingerprint,
                )
                or self._recovery_watermark() != first_watermark
            ):
                raise ReleaseManagerError("recovery baseline evidence or runtime has drifted")
            self._assert_recovery_consumers_stopped()
            _, final_data_image_ids, _ = self._current_runtime(
                refs,
                _RECOVERY_DATA_SERVICES,
            )
            if final_data_image_ids != data_image_ids:
                raise ReleaseManagerError("recovery data runtime has drifted")
            if state_value == ReleaseState.STAGED.value:
                store.transition(
                    ReleaseState.STAGED,
                    ReleaseState.PREPARED,
                    prepared_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    **metadata,
                )
            return self._checkpoint_recovery_state(
                {
                    **recovery,
                    "gap_fence_sha256": gap_digest,
                    "recovery_watermark_sha256": watermark_digest,
                    "restore_receipt_sha256": restore_digest,
                    "live_database_fingerprint_sha256": live_fingerprint,
                },
                status="running",
                phase="adopted",
            )
        except ReleaseManagerError as exc:
            try:
                current_recovery = self._read_recovery_state()
            except (ReleaseManagerError, OSError):
                current_recovery = None
            if current_recovery is not None and current_recovery.get("status") in {
                "running",
                "succeeded",
            }:
                candidate_store = ReleaseStore(
                    self.release_root,
                    cast(str, current_recovery["release_id"]),
                )
                failure_store_optional: ReleaseStore | None = candidate_store
                try:
                    candidate_store.read_state()
                except ReleaseStoreError:
                    failure_store_optional = None
                raise self._record_recovery_failure(
                    current_recovery,
                    exc,
                    store=failure_store_optional,
                ) from exc
            raise
        except ReleaseStoreError as exc:
            raise ReleaseManagerError("recovery baseline state is unavailable") from exc
        except OSError as exc:
            raise ReleaseManagerError("recovery baseline evidence is unavailable") from exc

    def resume_recovery(
        self,
        *,
        stage: Literal["api", "callback", "workers", "outbox", "beat", "web"],
        runtime_secrets_target: str,
        confirmed_recovered_host: bool,
    ) -> dict[str, object]:
        """按固定顺序恢复服务；最后健康封存前 recovery 围栏始终有效。"""

        if self.mode != "production" or not confirmed_recovered_host:
            raise ReleaseManagerError(
                "recovery resume requires explicit recovered-host confirmation"
            )
        try:
            ReleaseStore._validate_directory(self.release_root)
            recovery = self._read_recovery_state()
            if recovery is None:
                raise ReleaseManagerError("recovery resume requires a persistent fence")
            if recovery["status"] in {"failed", "recovery_required"}:
                raise ReleaseManagerError("recovery resume state requires manual recovery")
            store = ReleaseStore(self.release_root, cast(str, recovery["release_id"]))
            manifest = self._stored_manifest(store)
            manifest_raw = _read_safe_bytes(
                store.release_dir / "manifest.json",
                expected_uid=os.geteuid(),
                expected_mode=0o600,
                maximum=_MAX_JSON_BYTES,
            )
            topology = self._production_topology()
            refs = self._root_env_refs()
            runtime_secrets_target = self._validate_runtime_secrets_target(
                runtime_secrets_target
            )
            if (
                manifest.mode != "production"
                or manifest.commit != recovery["commit"]
                or not hmac.compare_digest(
                    hashlib.sha256(manifest_raw).hexdigest(),
                    cast(str, recovery["manifest_sha256"]),
                )
                or topology != recovery["production_topology"]
                or runtime_secrets_target != recovery["runtime_secrets_target"]
                or refs != {name: manifest.images[name].ref for name in _IMAGE_NAMES}
                or manifest.migration_target != recovery["migration_head"]
            ):
                raise ReleaseManagerError("recovery resume binding has drifted")
            self._validate_git(manifest)
            if self._observed_migration_head() != manifest.migration_target:
                raise ReleaseManagerError("recovery resume migration has drifted")
            live_fingerprint = self._recovery_live_database_fingerprint()
            if not hmac.compare_digest(
                live_fingerprint,
                cast(str, recovery["live_database_fingerprint_sha256"]),
            ):
                raise ReleaseManagerError("recovery resumed database has drifted")

            release_state = store.read_state()
            metadata_fields = (
                "production_topology",
                "runtime_secrets_target",
                "snapshot_id",
                "snapshot_manifest_sha256",
                "snapshot_database_sha256",
                "recovery_crypto_generation_id",
                "backup_passphrase_generation_id",
                "restore_receipt_sha256",
                "live_database_fingerprint_sha256",
                "gap_fence_sha256",
                "recovery_migration_head",
                "recovery_watermark_sha256",
            )
            recovery_to_release = {
                "production_topology": recovery["production_topology"],
                "runtime_secrets_target": recovery["runtime_secrets_target"],
                "snapshot_id": recovery["snapshot_id"],
                "snapshot_manifest_sha256": recovery["snapshot_manifest_sha256"],
                "snapshot_database_sha256": recovery["snapshot_database_sha256"],
                "recovery_crypto_generation_id": recovery[
                    "recovery_crypto_generation_id"
                ],
                "backup_passphrase_generation_id": recovery[
                    "backup_passphrase_generation_id"
                ],
                "restore_receipt_sha256": recovery["restore_receipt_sha256"],
                "live_database_fingerprint_sha256": recovery[
                    "live_database_fingerprint_sha256"
                ],
                "gap_fence_sha256": recovery["gap_fence_sha256"],
                "recovery_migration_head": recovery["migration_head"],
                "recovery_watermark_sha256": recovery[
                    "recovery_watermark_sha256"
                ],
            }
            if any(
                release_state.get(field) != recovery_to_release[field]
                for field in metadata_fields
            ):
                raise ReleaseManagerError("recovery resume release metadata has drifted")

            stage_names = [item[0] for item in _RECOVERY_RESUME_STAGES]
            requested_index = stage_names.index(stage)
            completed_phases = ["adopted", *[item[2] for item in _RECOVERY_RESUME_STAGES]]
            phase = cast(str, recovery["phase"])
            finishing_succeeded = recovery["status"] == "succeeded"
            if recovery["status"] == "succeeded":
                if phase != "succeeded" or stage != "web":
                    raise ReleaseManagerError("recovery resume order is invalid")
                completed_index = len(_RECOVERY_RESUME_STAGES) - 1
            else:
                if recovery["status"] != "running" or phase not in completed_phases[:-1]:
                    raise ReleaseManagerError("recovery resume order is invalid")
                completed_index = completed_phases.index(phase)
                if requested_index + 1 < completed_index:
                    raise ReleaseManagerError("recovery resume order is invalid")
                if requested_index + 1 > completed_index + 1:
                    raise ReleaseManagerError("recovery resume order is invalid")

            authorized_services: list[str] = []
            for _, services, _ in _RECOVERY_RESUME_STAGES[:completed_index]:
                authorized_services.extend(services)
            unauthorized = tuple(
                service
                for service in _BOOTSTRAP_CONTAINMENT_SERVICES
                if service not in authorized_services
                and not (finishing_succeeded and service == "web")
            )
            self._assert_recovery_services_stopped(unauthorized)
            current_services = (*_RECOVERY_DATA_SERVICES, *authorized_services)
            _, observed_images, _ = self._current_runtime(refs, current_services)
            if any(
                not hmac.compare_digest(manifest.images[name].image_id, image_id)
                for name, image_id in observed_images.items()
            ):
                raise ReleaseManagerError("recovery resumed runtime image IDs have drifted")

            already_completed = (
                not finishing_succeeded and requested_index + 1 == completed_index
            )
            if already_completed:
                return recovery
            if stage == "workers":
                stored_watermark = self._stored_recovery_watermark(
                    store,
                    cast(str, recovery["recovery_watermark_sha256"]),
                )
                if self._recovery_watermark() != stored_watermark:
                    raise ReleaseManagerError(
                        "recovery sending watermark changed before workers"
                    )

            _, stage_services, next_phase = _RECOVERY_RESUME_STAGES[requested_index]
            try:
                self._run(
                    self._compose()
                    + [
                        "up",
                        "-d",
                        "--no-deps",
                        "--wait",
                        "--wait-timeout",
                        "120",
                        *stage_services,
                    ],
                    f"production recovery {stage} resume",
                )
                authorized_after = [*authorized_services, *stage_services]
                unauthorized_after = tuple(
                    service
                    for service in _BOOTSTRAP_CONTAINMENT_SERVICES
                    if service not in authorized_after
                )
                self._assert_recovery_services_stopped(unauthorized_after)
                container_ids, image_ids, service_container_ids = self._current_runtime(
                    refs,
                    (*_RECOVERY_DATA_SERVICES, *authorized_after),
                )
                if any(
                    not hmac.compare_digest(manifest.images[name].image_id, image_id)
                    for name, image_id in image_ids.items()
                ):
                    raise ReleaseManagerError(
                        "recovery resumed runtime image IDs do not match manifest"
                    )
                if stage != "web":
                    return self._checkpoint_recovery_state(
                        recovery,
                        status="running",
                        phase=next_phase,
                    )

                state_value = store.read_state().get("state")
                if state_value == ReleaseState.PREPARED.value:
                    if finishing_succeeded:
                        raise ReleaseManagerError(
                            "recovery baseline finalization requires manual recovery"
                        )
                    self._write_snapshot(
                        store,
                        current_commit=manifest.commit,
                        current_refs=refs,
                        container_ids=container_ids,
                        image_ids=image_ids,
                        migration_head=manifest.migration_target,
                        manifest=manifest,
                        service_container_ids=service_container_ids,
                        target_image_ids={
                            name: manifest.images[name].image_id for name in _IMAGE_NAMES
                        },
                    )
                    store.transition(
                        ReleaseState.PREPARED,
                        ReleaseState.ACTIVATING,
                        recovery_sealing_at=datetime.now(UTC)
                        .isoformat()
                        .replace("+00:00", "Z"),
                    )
                    state_value = ReleaseState.ACTIVATING.value
                elif state_value not in {
                    ReleaseState.ACTIVATING.value,
                    ReleaseState.SUCCEEDED.value,
                }:
                    raise ReleaseManagerError(
                        "recovery baseline finalization requires manual recovery"
                    )
                self._verify_final_runtime(store, manifest)
                if recovery["status"] != "succeeded":
                    recovery = self._checkpoint_recovery_state(
                        recovery,
                        status="succeeded",
                        phase="succeeded",
                    )
                if state_value == ReleaseState.ACTIVATING.value:
                    store.transition(
                        ReleaseState.ACTIVATING,
                        ReleaseState.SUCCEEDED,
                        recovery_adopted_at=datetime.now(UTC)
                        .isoformat()
                        .replace("+00:00", "Z"),
                        verified_migration_head=manifest.migration_target,
                    )
                now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
                baseline = {
                    "schema_version": 1,
                    "status": "succeeded",
                    "release_id": manifest.release_id,
                    "commit": manifest.commit,
                    "manifest_sha256": recovery["manifest_sha256"],
                    "production_topology": topology,
                    "phase": "recovery_adopted",
                    "started_at": recovery["started_at"],
                    "updated_at": now,
                    "failure_type": None,
                }
                self._write_bootstrap_state(baseline)
                self.assert_production_start_allowed()
                return recovery
            except Exception as exc:
                raise self._record_recovery_failure(
                    recovery,
                    exc,
                    store=store,
                ) from exc
        except ReleaseManagerError as exc:
            try:
                current_recovery = self._read_recovery_state()
            except (ReleaseManagerError, OSError):
                current_recovery = None
            if current_recovery is not None and current_recovery.get("status") in {
                "running",
                "succeeded",
            }:
                failure_store = ReleaseStore(
                    self.release_root,
                    cast(str, current_recovery["release_id"]),
                )
                raise self._record_recovery_failure(
                    current_recovery,
                    exc,
                    store=failure_store,
                ) from exc
            raise
        except ReleaseStoreError as exc:
            raise ReleaseManagerError("recovery resume state is unavailable") from exc
        except OSError as exc:
            raise ReleaseManagerError("recovery resume evidence is unavailable") from exc

    def _freeze_bootstrap_bundle(
        self,
        manifest_path: Path,
        manifest_bytes: bytes,
        staging_files: Sequence[Path],
        store: ReleaseStore,
    ) -> None:
        """在任何生产变更前把已解析闭包复制进受控 release store。"""

        frozen: dict[str, bytes] = {}
        for path in staging_files:
            frozen[path.name] = _read_safe_bytes(
                path,
                expected_uid=self._staging_uid(),
                expected_mode=0o600,
                maximum=_MAX_JSON_BYTES,
            )
        if frozen.get(manifest_path.name) != manifest_bytes:
            raise ReleaseManagerError(
                "production bootstrap manifest changed before it could be frozen"
            )
        store.create(manifest_bytes)
        artifacts = store.release_dir / "artifacts"
        for name, payload in frozen.items():
            if name == manifest_path.name:
                continue
            ReleaseStore._atomic_write(artifacts / name, payload)

    def _checkpoint_bootstrap(
        self,
        state: Mapping[str, object],
        *,
        status: Literal["running", "succeeded", "failed"],
        phase: str,
        failure_type: str | None = None,
    ) -> dict[str, object]:
        updated = {
            **state,
            "status": status,
            "phase": phase,
            "failure_type": failure_type,
            "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        self._write_bootstrap_state(updated)
        return updated

    def _contain_failed_bootstrap(self, compose: Sequence[str]) -> None:
        """失败首装必须停止入口与全部消费者，同时保留数据服务和卷供取证。"""

        self._run(
            [*compose, "stop", *_BOOTSTRAP_CONTAINMENT_SERVICES],
            "production bootstrap containment stop",
        )
        for service in _BOOTSTRAP_CONTAINMENT_SERVICES:
            observed = self._run(
                [*compose, "ps", "-q", service],
                "production bootstrap containment verification",
            )
            if observed.stdout.strip():
                raise ReleaseManagerError(
                    "production bootstrap containment could not be verified"
                )

    def bootstrap(self, manifest_path: Path, *, confirmed_empty_host: bool) -> dict[str, object]:
        """从已审计基线清单启动空生产主机，并固化为首个 succeeded release。"""

        if self.mode != "production" or not confirmed_empty_host:
            raise ReleaseManagerError(
                "production bootstrap requires explicit empty-host confirmation"
            )
        manifest, manifest_bytes, staging_files = _validate_staging_bundle(
            manifest_path,
            self._staging_uid(),
        )
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        if manifest.mode != "production":
            raise ReleaseManagerError("production bootstrap manifest mode is invalid")
        if (
            any(image.changed for image in manifest.images.values())
            or manifest.migration_from != manifest.migration_target
            or manifest.migration_compatibility is not MigrationCompatibility.NONE
        ):
            raise ReleaseManagerError("production bootstrap requires a no-delta baseline manifest")
        production_topology = self._production_topology()

        ReleaseStore._ensure_directory(self.release_root)
        existing = self._read_bootstrap_state()
        if existing is not None:
            if (
                existing["status"] != "succeeded"
                or existing["release_id"] != manifest.release_id
                or existing["commit"] != manifest.commit
                or existing["manifest_sha256"] != manifest_sha256
                or existing["production_topology"] != production_topology
            ):
                raise ReleaseManagerError("production bootstrap requires manual recovery")
            entries = {entry.name for entry in os.scandir(self.release_root)}
            if entries != {_BOOTSTRAP_STATE_NAME, manifest.release_id}:
                raise ReleaseManagerError("production bootstrap state directory drifted")
            if self.status(manifest.release_id).get("state") != ReleaseState.SUCCEEDED.value:
                raise ReleaseManagerError("production bootstrap baseline is not succeeded")
            return cast(dict[str, object], existing)

        with os.scandir(self.release_root) as directory_entries:
            release_root_is_empty = next(directory_entries, None) is None
        if not release_root_is_empty:
            raise ReleaseManagerError("production bootstrap release root is not empty")
        self._assert_empty_production_storage_sources()
        current_refs = self._root_env_refs()
        target_refs = {name: manifest.images[name].ref for name in _IMAGE_NAMES}
        if current_refs != target_refs:
            raise ReleaseManagerError("production bootstrap root env is not the target baseline")
        self._validate_git(manifest)
        self._validate_migration_direction(manifest)
        compose = self._compose()
        containers = self._run(
            compose + ["ps", "--all", "-q"],
            "production bootstrap container inventory",
        )
        volumes = self._run(
            [
                "docker",
                "volume",
                "ls",
                "--quiet",
                "--filter",
                "name=sms-platform_",
            ],
            "production bootstrap volume inventory",
        )
        if containers.stdout.strip() or volumes.stdout.strip():
            raise ReleaseManagerError("production bootstrap host is not empty")

        store = ReleaseStore(self.release_root, manifest.release_id)
        self._freeze_bootstrap_bundle(
            manifest_path,
            manifest_bytes,
            staging_files,
            store,
        )
        store.checkpoint(
            ReleaseState.STAGED,
            production_topology=production_topology,
            release_kind="bootstrap",
        )

        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        state: dict[str, object] = {
            "schema_version": 1,
            "status": "running",
            "release_id": manifest.release_id,
            "commit": manifest.commit,
            "manifest_sha256": manifest_sha256,
            "production_topology": production_topology,
            "phase": "validated",
            "started_at": now,
            "updated_at": now,
            "failure_type": None,
        }
        self._write_bootstrap_state(state)
        self._bootstrap_execution_release_id = manifest.release_id
        try:
            artifacts = store.release_dir / "artifacts"
            gate_kind, _ = self._validate_release_evidence(manifest, artifacts)
            if gate_kind != "release":
                raise ReleaseManagerError(
                    "production bootstrap requires release evidence"
                )
            if self._validate_data_evidence(manifest, artifacts) is not None:
                raise ReleaseManagerError(
                    "production bootstrap cannot replace data images"
                )
            self._validate_backup_evidence(manifest, artifacts)
            self._target_images(manifest, artifacts)
            if self._production_topology() != production_topology:
                raise ReleaseManagerError("production bootstrap topology drifted")
            if self._root_env_refs() != target_refs:
                raise ReleaseManagerError("production bootstrap root env drifted")
            commands = (
                ("compose_config", compose + ["config", "--quiet"]),
                (
                    "data_services",
                    compose
                    + [
                        "up",
                        "-d",
                        "--no-deps",
                        "--wait",
                        "--wait-timeout",
                        "120",
                        "postgres",
                        "redis",
                        "redis-auth",
                        "redis-control",
                    ],
                ),
                ("migration", compose + ["run", "--rm", "migrate"]),
                (
                    "application_services",
                    compose
                    + [
                        "up",
                        "-d",
                        "--no-deps",
                        "--wait",
                        "--wait-timeout",
                        "120",
                        *_BACKEND_SERVICES,
                        "web",
                    ],
                ),
            )
            for phase, command in commands:
                if self._stop_signal is not None:
                    raise _ReleaseInterrupted("production bootstrap interrupted")
                state = self._checkpoint_bootstrap(state, status="running", phase=phase)
                self._run(command, f"production bootstrap {phase}")
            if self._stop_signal is not None:
                raise _ReleaseInterrupted("production bootstrap interrupted")
            state = self._checkpoint_bootstrap(state, status="running", phase="seal_release")
            if self._production_topology() != production_topology:
                raise ReleaseManagerError("production bootstrap topology drifted")
            self._resume_staged(store, manifest)
            self.activate(manifest.release_id)
            return self._checkpoint_bootstrap(state, status="succeeded", phase="complete")
        except BaseException as exc:
            containment_failed = False
            try:
                self._contain_failed_bootstrap(compose)
            except BaseException:
                containment_failed = True
            with suppress(Exception):
                self._checkpoint_bootstrap(
                    state,
                    status="failed",
                    phase=("containment_failed" if containment_failed else "contained"),
                    failure_type=(
                        "BootstrapContainmentFailed"
                        if containment_failed
                        else type(exc).__name__
                    ),
                )
            if not isinstance(exc, Exception):
                raise
            raise ReleaseManagerError(
                f"production bootstrap failed ({type(exc).__name__})"
            ) from exc
        finally:
            self._bootstrap_execution_release_id = None

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

        self._assert_bootstrap_mutation_allowed(release_id)
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

    def _verify_compensation_result(
        self,
        store: ReleaseStore,
        manifest: ReleaseManifest,
        keep_data: set[str],
        migration_state: str,
    ) -> None:
        """补偿终态必须由 env、容器与 migration 三方观察共同证明。"""

        snapshot = self._read_snapshot(store)
        expected_refs = cast(dict[str, str], snapshot["current_refs"]).copy()
        for name in keep_data:
            expected_refs[name] = manifest.images[name].ref
        if self._root_env_refs() != expected_refs:
            raise ReleaseManagerError("compensation env verification failed")

        recreation = {
            ReleaseStepKind.RECREATE_POSTGRES,
            ReleaseStepKind.RECREATE_REDIS,
            ReleaseStepKind.RECREATE_BACKEND,
            ReleaseStepKind.RECREATE_WEB,
        }
        for step in build_activation_plan(manifest):
            if step.kind not in recreation:
                continue
            observed, healthy = self._observe_step_runtime_status(store, manifest, step)
            image_name = _service_image_name(step.services[0])
            if not manifest.images[image_name].changed:
                allowed = {"original", "target"}
            elif image_name in keep_data:
                allowed = {"target"}
            else:
                allowed = {"original"}
            if observed not in allowed or not healthy:
                raise ReleaseManagerError("compensation runtime verification failed")

        observed_migration = self._observe_migration_state(store, manifest)
        expected_migration = (
            "target"
            if manifest.migration_from == manifest.migration_target
            or migration_state == "target"
            else "original"
        )
        if observed_migration != expected_migration:
            raise ReleaseManagerError("compensation migration verification failed")
        store.record_observation(
            "compensation_final",
            {
                "completed": True,
                "migration": expected_migration,
                "retained_data": sorted(keep_data),
            },
        )

    def _preflight_activation(
        self,
        store: ReleaseStore,
        manifest: ReleaseManifest,
    ) -> None:
        snapshot = self._read_snapshot(store)
        store.record_intent("activation_preflight", {"source": "runtime"})
        commit = self._validate_git(manifest)
        self._validate_migration_direction(manifest)
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
        commands = activation_commands(self.root, plan, compose=self._compose())
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
        store.transition(
            ReleaseState.ACTIVATING,
            ReleaseState.SUCCEEDED,
            verified_migration_head=manifest.migration_target,
        )

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
            self._validate_migration_direction(manifest)
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
            prepared_fields: dict[str, object] = {
                "prepared_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "release_gate_kind": release_gate_kind,
                "control_smoke_only": release_gate_kind == "release_control_smoke",
                "release_scan_performed": release_scan_performed,
            }
            if self.mode == "production":
                prepared_fields["production_topology"] = self._production_topology()
            store.transition(
                ReleaseState.STAGED,
                ReleaseState.PREPARED,
                **prepared_fields,
            )
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
        if migration_state == "target":
            keep_data.update(
                name
                for name in ("postgres", "redis")
                if manifest.images[name].changed
            )
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
                command = activation_commands(
                    self.root,
                    [step],
                    compose=self._compose(),
                )[0]
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
            self._verify_compensation_result(
                store,
                manifest,
                keep_data,
                migration_state,
            )
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

    def _record_rollback_runtime_effects(
        self,
        store: ReleaseStore,
        manifest: ReleaseManifest,
        *,
        already_rolling_back: bool,
    ) -> None:
        """显式回退前以运行态补齐已生效但尚未落 observation 的步骤。"""

        current = (
            ReleaseState.ROLLING_BACK
            if already_rolling_back
            else ReleaseState.ACTIVATING
        )

        def fail_closed() -> NoReturn:
            store.transition(
                current,
                ReleaseState.RECOVERY_REQUIRED,
                failure_type="rollback_runtime_ambiguity",
            )
            raise ReleaseManagerError(
                "release rollback runtime is ambiguous; manual recovery required"
            )

        events = self._read_events(store)
        plan = build_activation_plan(manifest)
        plan_by_kind = {step.kind: step for step in plan}
        recreation = {
            ReleaseStepKind.RECREATE_POSTGRES,
            ReleaseStepKind.RECREATE_REDIS,
            ReleaseStepKind.RECREATE_BACKEND,
            ReleaseStepKind.RECREATE_WEB,
        }
        side_effects = recreation | {
            ReleaseStepKind.QUIESCE_BACKEND,
            ReleaseStepKind.RUN_MIGRATE,
        }
        pending_activation: set[ReleaseStepKind] = set()
        pending_compensation: set[ReleaseStepKind] = set()
        for event in events:
            try:
                event_step = ReleaseStepKind(cast(str, event["step"]))
            except (TypeError, ValueError):
                event_step = None
            if event_step in side_effects:
                if event["kind"] == "intent":
                    pending_activation.add(event_step)
                elif event["details"].get("completed") is True:
                    pending_activation.discard(event_step)
            if event["step"] != "compensate":
                continue
            group = event["details"].get("group")
            try:
                compensation_kind = ReleaseStepKind(cast(str, group))
            except (TypeError, ValueError):
                fail_closed()
            if compensation_kind not in recreation:
                fail_closed()
            if event["kind"] == "intent":
                pending_compensation.add(compensation_kind)
            elif event["details"].get("completed") is True:
                pending_compensation.discard(compensation_kind)

        for kind in sorted(pending_compensation, key=lambda value: value.value):
            step = plan_by_kind.get(kind)
            if step is None:
                fail_closed()
            try:
                observed, healthy = self._observe_step_runtime_status(
                    store,
                    manifest,
                    step,
                )
            except Exception:
                fail_closed()
            image_name = _service_image_name(step.services[0])
            if not manifest.images[image_name].changed:
                if observed not in {"original", "target"} or not healthy:
                    fail_closed()
                store.record_observation(
                    "compensate",
                    {
                        "completed": True,
                        "group": kind.value,
                        "recovered": True,
                    },
                )
                continue
            if observed == "original" and healthy:
                store.record_observation(
                    "compensate",
                    {
                        "completed": True,
                        "group": kind.value,
                        "recovered": True,
                    },
                )
                continue
            if observed == "target":
                continue
            fail_closed()

        for kind in sorted(pending_activation, key=lambda value: value.value):
            if kind is ReleaseStepKind.RUN_MIGRATE:
                continue
            if kind is ReleaseStepKind.QUIESCE_BACKEND:
                step = plan_by_kind.get(ReleaseStepKind.RECREATE_BACKEND)
            else:
                step = plan_by_kind.get(kind)
            if step is None:
                fail_closed()
            try:
                observed, healthy = self._observe_step_runtime_status(
                    store,
                    manifest,
                    step,
                )
            except Exception:
                fail_closed()
            if kind is ReleaseStepKind.QUIESCE_BACKEND:
                if observed == "stopped":
                    store.record_observation(
                        kind.value,
                        {
                            "completed": True,
                            "recovered": True,
                            "services": list(step.services),
                        },
                    )
                elif observed not in {"original", "target"} or not healthy:
                    fail_closed()
                continue
            image_name = _service_image_name(step.services[0])
            if not manifest.images[image_name].changed:
                if observed not in {"original", "target"} or not healthy:
                    fail_closed()
                continue
            if observed == "target":
                store.record_observation(
                    kind.value,
                    {
                        "completed": True,
                        "recovered": True,
                        "services": list(step.services),
                    },
                )
            elif observed != "original" or not healthy:
                fail_closed()

    def activate(self, release_id: str) -> None:
        self._assert_bootstrap_mutation_allowed(release_id)
        store = ReleaseStore(self.release_root, release_id)
        state = store.read_state()
        if state.get("state") != ReleaseState.PREPARED.value:
            raise ReleaseManagerError("release is not prepared for activation")
        self._assert_production_topology(state)
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
        commands = activation_commands(self.root, plan, compose=self._compose())
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
        store.transition(
            ReleaseState.ACTIVATING,
            ReleaseState.SUCCEEDED,
            verified_migration_head=manifest.migration_target,
        )

    def status(self, release_id: str) -> dict[str, object]:
        try:
            return ReleaseStore(self.release_root, release_id).read_state()
        except ReleaseStoreError as exc:
            raise ReleaseManagerError("release status is unavailable") from exc

    def resume(self, release_id: str) -> None:
        self._assert_bootstrap_mutation_allowed(release_id)
        store = ReleaseStore(self.release_root, release_id)
        state = store.read_state()
        state_value = state.get("state")
        if state_value in {
            ReleaseState.SUCCEEDED.value,
            ReleaseState.ROLLED_BACK.value,
        }:
            self._assert_production_topology(state)
            return
        if state_value == ReleaseState.RECOVERY_REQUIRED.value:
            raise ReleaseManagerError("recovery_required release refuses automatic resume")
        if state_value == ReleaseState.FAILED.value:
            raise ReleaseManagerError("failed release cannot be resumed")
        manifest = self._stored_manifest(store)
        if state_value == ReleaseState.STAGED.value:
            if self.mode == "production":
                self._validate_staged_production_release(store, manifest, state)
            self._resume_staged(store, manifest)
            self.activate(release_id)
            return
        if state_value not in {
            ReleaseState.PREPARED.value,
            ReleaseState.ACTIVATING.value,
            ReleaseState.ROLLING_BACK.value,
        }:
            raise ReleaseManagerError("release state is unknown")
        self._assert_production_topology(state)
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
                store.transition(
                    ReleaseState.ACTIVATING,
                    ReleaseState.SUCCEEDED,
                    verified_migration_head=manifest.migration_target,
                )
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
        self._assert_bootstrap_mutation_allowed(release_id)
        store = ReleaseStore(self.release_root, release_id)
        state = store.read_state()
        state_value = state.get("state")
        if state_value in {
            ReleaseState.STAGED.value,
            ReleaseState.FAILED.value,
        }:
            return
        if state_value == ReleaseState.ROLLED_BACK.value:
            self._assert_production_topology(state)
            return
        self._assert_production_topology(state)
        if state_value == ReleaseState.RECOVERY_REQUIRED.value:
            raise ReleaseManagerError("recovery_required release refuses automatic rollback")
        if state_value == ReleaseState.SUCCEEDED.value:
            raise ReleaseManagerError(
                "succeeded release requires a forward rollback candidate"
            )
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
        self._record_rollback_runtime_effects(
            store,
            manifest,
            already_rolling_back=already_rolling_back,
        )
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
    bootstrap = subparsers.add_parser("bootstrap")
    bootstrap.add_argument("--manifest", required=True, type=Path)
    bootstrap.add_argument(
        "--confirm-empty-host",
        action="store_true",
        help="confirm that the fixed production Compose project and volumes are empty",
    )
    recovery_start = subparsers.add_parser("start-recovery")
    recovery_start.add_argument("--manifest", required=True, type=Path)
    recovery_start.add_argument("--snapshot-manifest", required=True, type=Path)
    recovery_start.add_argument("--snapshot-manifest-sha256", required=True)
    recovery_start.add_argument("--runtime-secrets-target", required=True)
    recovery_start.add_argument(
        "--confirm-recovered-host",
        action="store_true",
        help="confirm that this non-empty host is the fenced recovery target",
    )
    recovery_observe = subparsers.add_parser("observe-recovery")
    recovery_observe.add_argument("--manifest", required=True, type=Path)
    recovery_observe.add_argument("--snapshot-manifest", required=True, type=Path)
    recovery_observe.add_argument("--snapshot-manifest-sha256", required=True)
    recovery_observe.add_argument("--output", required=True, type=Path)
    recovery_observe.add_argument("--runtime-secrets-target", required=True)
    recovery_observe.add_argument("--confirm-recovered-host", action="store_true")
    recovery_adopt = subparsers.add_parser("adopt-recovery")
    recovery_adopt.add_argument("--manifest", required=True, type=Path)
    recovery_adopt.add_argument("--snapshot-manifest", required=True, type=Path)
    recovery_adopt.add_argument("--snapshot-manifest-sha256", required=True)
    recovery_adopt.add_argument("--restore-receipt", required=True, type=Path)
    recovery_adopt.add_argument("--restore-receipt-sha256", required=True)
    recovery_adopt.add_argument("--gap-fence-evidence", required=True, type=Path)
    recovery_adopt.add_argument("--gap-fence-sha256", required=True)
    recovery_adopt.add_argument("--runtime-secrets-target", required=True)
    recovery_adopt.add_argument(
        "--confirm-recovered-host",
        action="store_true",
        help="confirm that this non-empty host was restored and fenced under an approved change",
    )
    recovery_resume = subparsers.add_parser("resume-recovery")
    recovery_resume.add_argument(
        "--stage",
        choices=("api", "callback", "workers", "outbox", "beat", "web"),
        required=True,
    )
    recovery_resume.add_argument("--runtime-secrets-target", required=True)
    recovery_resume.add_argument(
        "--confirm-recovered-host",
        action="store_true",
    )
    forward = subparsers.add_parser("prepare-forward-rollback")
    forward.add_argument("--source-release-id", required=True)
    forward.add_argument("--manifest", required=True, type=Path)
    subparsers.add_parser("start-gate")
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
        if arguments.action == "start-gate":
            manager.assert_production_start_allowed()
        elif arguments.action == "prepare":
            manager.prepare(arguments.manifest)
        elif arguments.action == "bootstrap":
            print(
                json.dumps(
                    manager.bootstrap(
                        arguments.manifest,
                        confirmed_empty_host=arguments.confirm_empty_host,
                    ),
                    sort_keys=True,
                )
            )
        elif arguments.action == "start-recovery":
            result = manager.start_recovery(
                arguments.manifest,
                snapshot_manifest_path=arguments.snapshot_manifest,
                snapshot_manifest_sha256=arguments.snapshot_manifest_sha256,
                runtime_secrets_target=arguments.runtime_secrets_target,
                confirmed_recovered_host=arguments.confirm_recovered_host,
            )
            print(_serialize_recovery_cli_result(arguments.action, result))
        elif arguments.action == "observe-recovery":
            result = manager.observe_recovery(
                arguments.manifest,
                snapshot_manifest_path=arguments.snapshot_manifest,
                snapshot_manifest_sha256=arguments.snapshot_manifest_sha256,
                output_path=arguments.output,
                runtime_secrets_target=arguments.runtime_secrets_target,
                confirmed_recovered_host=arguments.confirm_recovered_host,
            )
            print(_serialize_recovery_cli_result(arguments.action, result))
        elif arguments.action == "adopt-recovery":
            result = manager.adopt_recovery(
                arguments.manifest,
                snapshot_manifest_path=arguments.snapshot_manifest,
                snapshot_manifest_sha256=arguments.snapshot_manifest_sha256,
                restore_receipt_path=arguments.restore_receipt,
                restore_receipt_sha256=arguments.restore_receipt_sha256,
                gap_fence_path=arguments.gap_fence_evidence,
                gap_fence_sha256=arguments.gap_fence_sha256,
                runtime_secrets_target=arguments.runtime_secrets_target,
                confirmed_recovered_host=arguments.confirm_recovered_host,
            )
            print(_serialize_recovery_cli_result(arguments.action, result))
        elif arguments.action == "resume-recovery":
            result = manager.resume_recovery(
                stage=arguments.stage,
                runtime_secrets_target=arguments.runtime_secrets_target,
                confirmed_recovered_host=arguments.confirm_recovered_host,
            )
            print(
                _serialize_recovery_cli_result(
                    arguments.action,
                    result,
                )
            )
        elif arguments.action == "prepare-forward-rollback":
            manager.prepare_forward_rollback(
                arguments.source_release_id,
                arguments.manifest,
            )
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
