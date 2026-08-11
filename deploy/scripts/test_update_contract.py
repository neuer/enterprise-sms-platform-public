"""开发测试服务器快速更新的严格输入与变更分类契约。"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, cast


class TestUpdateContractError(ValueError):
    """快速更新请求或变更范围不符合安全契约。"""


@dataclass(frozen=True, slots=True)
class ChangedScope:
    """一次 Git 差异对应的运行组件、迁移和相关测试。"""

    components: frozenset[str]
    migration_changed: bool
    backend_tests: tuple[str, ...]
    frontend_tests: tuple[str, ...]
    runtime_changed: bool
    risk: Literal["none", "web-only", "backend-safe", "high-risk"]
    high_risk_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TestImage:
    """与目标 commit 和组件绑定的测试镜像归档。"""

    ref: str
    image_id: str
    archive_file: str
    archive_sha256: str


@dataclass(frozen=True, slots=True)
class PublicCutoverEvidence:
    """无历史公开快照在远端复验所需的最小私有源树证据。"""

    source_commit: str
    private_merge_base: str
    pack_file: str
    pack_sha256: str


@dataclass(frozen=True, slots=True)
class TestUpdateRequest:
    """服务器端可接受的不可变快速更新请求。"""

    update_id: str
    base_commit: str
    commit: str
    source_ref: str
    environment_mode: Literal["pre-live", "live"]
    components: frozenset[str]
    images: Mapping[str, TestImage]
    public_cutover: PublicCutoverEvidence | None
    migration_from: str
    migration_target: str
    migration_compatibility: Literal["none", "expand"]
    operation: Literal["apply", "rebaseline"] = "apply"


_TOP_LEVEL_FIELDS = frozenset(
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
_OPTIONAL_TOP_LEVEL_FIELDS = frozenset({"operation", "public_cutover"})
_IMAGE_FIELDS = frozenset({"ref", "id", "archive_file", "archive_sha256"})
_PUBLIC_CUTOVER_FIELDS = frozenset(
    {"source_commit", "private_merge_base", "pack_file", "pack_sha256"}
)
_MIGRATION_FIELDS = frozenset({"from", "target", "compatibility"})
_COMPONENTS = frozenset({"api", "web"})
_UPDATE_ID_RE = re.compile(r"test-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
_SAFE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_REF_COMPONENT_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
HOST_CONTROL_PATHS = frozenset(
    {
        "deploy/scripts/install_test_secure_access.py",
        "deploy/scripts/test_secure_access_contract.py",
        "deploy/scripts/test_secure_access_runtime.py",
        "deploy/scripts/test_secure_access_manager.py",
        "deploy/scripts/cloudflare_tunnel_manager.py",
        "deploy/scripts/render_trusted_proxy_conf.py",
        "deploy/scripts/vendor_test_files.py",
        "deploy/scripts/check_test_update_migration.py",
        "deploy/scripts/public_baseline_activation.py",
        "deploy/scripts/public_baseline_manager.py",
        "deploy/scripts/public_cutover_bootstrap.py",
        "deploy/scripts/run_with_lifecycle_lock.py",
        "deploy/scripts/test_update_apply.py",
        "deploy/scripts/test_update_backup.py",
        "deploy/scripts/test_update_contract.py",
        "deploy/scripts/test_update_manager.py",
        "deploy/scripts/test_update_promote.py",
        "deploy/scripts/test_update_store.py",
        "deploy/scripts/test_update_verify.py",
        "scripts/check_public_readiness.py",
        "scripts/export_public_snapshot.py",
        "scripts/verify_public_snapshot_cutover.py",
        "scripts/verify_web_transport.py",
        "deploy/sms-compose",
        "deploy/trusted-proxies.conf",
        "deploy/systemd/sms-platform-test-secure-access.service",
        "deploy/systemd/sms-platform-cloudflare-tunnel.service",
    }
)
_VENDOR_LIVE_PROTECTED_EXACT = (
    frozenset(
        {
            ".dockerignore",
            "backend/app/api/messages.py",
            "backend/app/api/vendor_test.py",
            "backend/app/api/web_messages.py",
            "backend/app/cli.py",
            "backend/app/core/auth/runtime.py",
            "backend/app/main.py",
            "backend/app/settings.py",
            "backend/app/services/pipeline.py",
            "backend/app/services/reconcile_repository.py",
            "backend/app/services/vendor_control_client.py",
            "backend/app/services/vendor_control_state.py",
            "backend/app/vendor/zhihui.py",
            "backend/app/vendor/codes.py",
            "backend/app/tasks/reconcile.py",
            "backend/app/tasks/send.py",
            "backend/app/services/billing.py",
            "backend/app/services/vendor_test_guard.py",
            "backend/app/services/vendor_test_budget.py",
            "backend/app/services/vendor_test_operation.py",
            "backend/app/services/vendor_test_operation_repository.py",
            "backend/app/services/vendor_test_pause.py",
            "backend/app/services/vendor_test_recipient.py",
            "backend/app/services/vendor_test_security_audit.py",
            "backend/app/services/vendor_test_recipient_repository.py",
            "backend/app/services/vendor_test_step_up.py",
            "backend/app/services/vendor_test_uat.py",
            "backend/app/tasks/send_repository.py",
            "backend/migrations/versions/0016_vendor_live_test_budget.py",
            "backend/migrations/versions/0017_vendor_test_web_console.py",
            "backend/migrations/versions/0018_vendor_test_operation_vendor_code.py",
            "backend/migrations/versions/0019_vendor_test_recipient_hmac_alias.py",
            "backend/migrations/versions/0022_vendor_test_reset_operation.py",
            "backend/migrations/versions/0023_vendor_uat_acceptance_lease.py",
            "backend/vendor_control_protocol.py",
            "backend/vendor_control_protocol.pyi",
            "backend/Dockerfile",
            "deploy/docker-compose.yml",
            "deploy/.env.example",
            "deploy/scripts/install_vendor_credentials.py",
            "deploy/scripts/install_resend_api_key.py",
            "deploy/scripts/send_security_daily_report_resend.py",
            "deploy/scripts/vendor_control_agent.py",
            "deploy/scripts/vendor_control_journal.py",
            "deploy/scripts/vendor_control_protocol.py",
            "deploy/scripts/vendor_credential_store.py",
            "deploy/scripts/vendor_control_reload.py",
            "deploy/scripts/vendor_seal_sessions.py",
            "deploy/systemd/vendor-control-agent.service",
            "deploy/scripts/install_test_secure_access.py",
            "deploy/systemd/sms-platform-test-secure-access.service",
            "deploy/sms-compose",
            "scripts/check_invariants.py",
            "scripts/classify_ci_changes.py",
            "scripts/test_update.sh",
            "scripts/verify_vendor_live_test.sh",
            "frontend/src/api/admin.ts",
            "frontend/src/components/VendorCredentialDialog.vue",
            "frontend/src/components/VendorTestConsole.vue",
            "frontend/src/components/VendorTestRecipientDialog.vue",
            "frontend/src/components/VendorTestUatPanel.vue",
            "frontend/src/lib/vendorSeal.ts",
            "frontend/src/views/ConfigView.vue",
            "openapi.yaml",
            "schema.sql",
        }
    )
    | HOST_CONTROL_PATHS
)
_VENDOR_LIVE_PROTECTED_PREFIXES = (
    "deploy/scripts/vendor_test_",
    "deploy/scripts/test_update_",
    "deploy/scripts/test_secure_access_",
)
_BACKEND_CRITICAL_PROTECTED_EXACT = frozenset(
    {
        "backend/app/api/admin.py",
        "backend/app/api/approvals.py",
        "backend/app/api/auth.py",
        "backend/app/api/ops.py",
        "backend/app/api/reports.py",
        "backend/app/core/apikey.py",
        "backend/app/core/audit.py",
        "backend/app/core/ratelimit.py",
        "backend/app/services/admin.py",
        "backend/app/services/admin_repository.py",
        "backend/app/services/approval.py",
        "backend/app/services/approval_repository.py",
        "backend/app/services/auth_provider.py",
        "backend/app/services/auth_provider_repository.py",
        "backend/app/services/blacklist.py",
        "backend/app/services/blacklist_repository.py",
        "backend/app/services/callback.py",
        "backend/app/services/callback_repository.py",
        "backend/app/services/callback_worker.py",
        "backend/app/services/category.py",
        "backend/app/services/crypto.py",
        "backend/app/services/export.py",
        "backend/app/services/export_file.py",
        "backend/app/services/export_repository.py",
        "backend/app/services/export_worker.py",
        "backend/app/services/freq.py",
        "backend/app/services/idempotency.py",
        "backend/app/services/import_repository.py",
        "backend/app/services/imports.py",
        "backend/app/services/masking.py",
        "backend/app/services/pipeline_repository.py",
        "backend/app/services/quota.py",
        "backend/app/services/raw_replay.py",
        "backend/app/services/reply_ingest.py",
        "backend/app/services/report_ingest.py",
        "backend/app/services/resend.py",
        "backend/app/services/sensitive.py",
        "backend/app/services/sensitive_repository.py",
        "backend/app/services/uncertain.py",
        "backend/app/services/uncertain_repository.py",
        "backend/app/services/user_management.py",
        "backend/app/services/user_repository.py",
        "backend/app/services/vendor_test_security_audit.py",
    }
)
_BACKEND_CRITICAL_PROTECTED_PREFIXES = (
    "backend/app/core/auth/",
    "backend/app/tasks/",
    "backend/app/vendor/",
)
_SAFE_OPERATIONAL_DOCS = frozenset(
    {
        "AGENTS.md",
        "CLAUDE.md",
        "MAINTENANCE.md",
        "PRD.md",
        "PROGRESS.md",
        "deploy/README.md",
        "docs/previews/security-daily-report-sample.html",
        "docs/previews/security-daily-report-sample.txt",
        "deploy/prometheus.example.yml",
        "deploy/redis-ha.md",
        "deploy/vendor-egress.md",
        "docs/DECISIONS.md",
        "docs/PERFORMANCE.md",
        "docs/TEST-MANUAL.md",
        "docs/UAT.md",
        "docs/api-test-playground.md",
        "docs/api-integration.md",
        "docs/reports/2026-07-18-test-fast-update-rehearsal.md",
        "docs/runbooks/controlled-real-vendor-test.md",
        "docs/runbooks/test-fast-update.md",
    }
)
_SAFE_NON_RUNTIME_GATES = frozenset(
    {
        # 仓库级忽略规则；不进入镜像、服务配置或运行态。
        ".gitignore",
        # 密钥管理与部署说明文档；不包含真实密钥，也不进入镜像或运行态。
        "deploy/secrets.md",
        "deploy/database-roles.md",
        # 仅用于把安全日报 control 运行态目录保留在 Git 中；不进入镜像或服务配置。
        "deploy/security-report-control/.gitignore",
        # 仅用于把 UI 同步的安全日报配置目录保留在 Git 中；实际配置不入库。
        "deploy/security-report-config/.gitignore",
        "docs/runbooks/public-baseline-activation.md",
        "scripts/e2e_api.py",
        # 仅用于本地/隔离测试栈的目录与凭据准备；不进入服务镜像或运行态。
        "scripts/local_test.sh",
        # G2 性能门禁脚本不进入 api/web 运行镜像；阈值变更由精确提交的
        # 托管 CI/G2 负责验证，不能阻断同一提交中的正式运行态更新。
        "scripts/perf_smoke.py",
        "scripts/canonicalize_sbom.py",
        "scripts/create_release_manifest.py",
        "scripts/render_release_evidence.py",
        "scripts/verify_release.sh",
        "scripts/verify_database_roles.sh",
        "scripts/verify_reproducible_build.sh",
        "scripts/verify_reproducible_release.py",
        "scripts/verify_redis_domains.sh",
        "scripts/verify_tls_termination_e2e.py",
        "scripts/verify_all.sh",
        "scripts/verify_vendor_live_test.sh",
        "scripts/verify_release_control.sh",
        "test-update.env.example",
    }
)
_WEB_HIGH_RISK_EXACT = frozenset(
    {
        "deploy/nginx-security-headers.conf",
    }
)
_WEB_RUNTIME_DEPLOY_EXACT = frozenset(
    {
        # Compose 定义 web 服务的挂载/运行身份；变更必须同时重建 web。
        "deploy/docker-compose.yml",
        "deploy/nginx.conf",
    }
)
_MAILER_HIGH_RISK_EXACT = frozenset(
    {
        # 安全日报 mailer 模板：随独立 mailer 镜像发布，不参与 api/web 快速更新构建。
        "deploy/templates/security_daily_report.html",
        "deploy/templates/security_daily_report.txt",
        "deploy/scripts/render_security_daily_report.py",
    }
)
_INFRA_HIGH_RISK_EXACT = frozenset(
    {
        "deploy/redis-domain-entrypoint.sh",
        "deploy/redis-domain-healthcheck.sh",
        "deploy/scripts/collect_security_daily_evidence.py",
        "deploy/systemd/security-report-collector.service",
        "deploy/systemd/security-report-collector.timer",
    }
)
_PUBLIC_CUTOVER_SAFE_NON_RUNTIME_EXACT = frozenset(
    {
        # TEST-BASELINE 跨历史迁移仍需识别这些已删除路径；它们不是活动入口。
        "AUTOPILOT.md",
        "BOOTSTRAP.md",
        "RELEASE.md",
        "TASKS.md",
        "scripts/verify_milestone.sh",
        ".github/workflows/ci.yml",
        ".github/workflows/release-gate.yml",
        "CONTRIBUTING.md",
        "HANDOVER.md",
        "LICENSE",
        "PUBLIC-SNAPSHOT.json",
        "PUBLICATION.md",
        "PROGRESS.md",
        "README.md",
        "SECURITY.md",
        "VERSION",
        "deploy/backup-restore.md",
        "deploy/database-roles.md",
        "deploy/dba.md",
        "deploy/failover.md",
        "deploy/runtime-security.md",
        "deploy/secrets.md",
        "public-repository.json",
        "scripts/check_coverage_gates.py",
        "scripts/check_public_readiness.py",
        "scripts/check_spec_consistency.py",
        "scripts/deploy_release_remote.py",
        "scripts/export_public_snapshot.py",
        "scripts/local_test.sh",
        "scripts/perf_smoke.py",
        "scripts/release_metadata.py",
        "scripts/runtime_credentials.py",
        "scripts/security_acceptance.py",
        "scripts/verify_all.sh",
        "scripts/verify_ci_results.py",
        "scripts/verify_data_images.sh",
        "scripts/verify_database_roles.sh",
        "scripts/verify_public_snapshot_cutover.py",
        "scripts/verify_release_control.sh",
        "scripts/verify_vendor_live_test.sh",
        "scripts/verify_vendor_postgres_recovery.sh",
    }
)
_PUBLIC_CUTOVER_HIGH_RISK_EXACT = frozenset(
    {
        "deploy/initdb/01-create-app-role.sh",
        "deploy/lifecycle.server.example.json",
        "deploy/postgres.Dockerfile",
        "deploy/provision-db-roles.sh",
        "deploy/redis.Dockerfile",
        "deploy/scripts/collect_security_daily_evidence.py",
        "deploy/scripts/lifecycle_manager.py",
        "deploy/scripts/prepare_runtime_secrets.py",
        "deploy/scripts/release_manifest.py",
        "deploy/scripts/release_manager.py",
        "deploy/scripts/restore_drill.py",
        "deploy/scripts/sync_standby.py",
        "deploy/scripts/vendor_runtime_reset.py",
        "deploy/systemd/lifecycle.env.example",
        "deploy/systemd/sms-backup.service",
        "deploy/systemd/sms-backup.timer",
        "deploy/systemd/sms-lifecycle-status.service",
        "deploy/systemd/sms-lifecycle-status.timer",
        "deploy/systemd/sms-partition-maintenance.service",
        "deploy/systemd/sms-partition-maintenance.timer",
        "deploy/systemd/sms-restore-drill.service",
        "deploy/systemd/sms-restore-drill.timer",
        "deploy/systemd/security-report-collector.service",
        "deploy/systemd/security-report-collector.timer",
    }
)
_REBASELINE_SAFE_NON_RUNTIME_EXACT = frozenset(
    {
        # 旧测试基线到当前公开 main 的已审核操作文档与门禁脚本差异。
        "HANDOVER.md",
        "README.md",
        "SECURITY.md",
        "deploy/failover.md",
        "docs/LOCAL_TESTING.md",
        "scripts/check_spec_consistency.py",
        "scripts/verify_ci_commit.py",
        "scripts/verify_vendor_postgres_recovery.sh",
    }
)
_REBASELINE_HIGH_RISK_EXACT = frozenset(
    {
        # 只为同历史、带真实迁移前移的既有测试基线重对齐开放。
        "deploy/scripts/prepare_runtime_secrets.py",
        "deploy/scripts/vendor_runtime_reset.py",
    }
)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TestUpdateContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _require_object(value: object, context: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise TestUpdateContractError(f"{context} has an invalid object value")
    return cast(dict[str, Any], value)


def _require_exact_fields(
    value: dict[str, Any],
    expected: frozenset[str],
    context: str,
) -> None:
    actual = set(value)
    unknown = actual - expected
    missing = expected - actual
    if unknown:
        raise TestUpdateContractError(f"{context} has unknown fields: {sorted(unknown)}")
    if missing:
        raise TestUpdateContractError(f"{context} has missing fields: {sorted(missing)}")


def _require_matching_string(
    value: object,
    pattern: re.Pattern[str],
    context: str,
) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise TestUpdateContractError(f"{context} has an invalid value")
    return value


def _require_source_ref(value: object) -> str:
    if type(value) is not str or not value.startswith("origin/"):
        raise TestUpdateContractError("source_ref has an invalid value")
    parts = value.split("/")
    if len(parts) < 2 or any(_REF_COMPONENT_RE.fullmatch(part) is None for part in parts):
        raise TestUpdateContractError("source_ref has an invalid value")
    if ".." in value or "@{" in value or any(part.endswith(".lock") for part in parts):
        raise TestUpdateContractError("source_ref has an invalid value")
    return value


def _require_components(value: object) -> frozenset[str]:
    if type(value) is not list or not value:
        raise TestUpdateContractError("components has an invalid value")
    raw_components = cast(list[object], value)
    if any(
        type(component) is not str or component not in _COMPONENTS for component in raw_components
    ):
        raise TestUpdateContractError("components has an invalid value")
    components = cast(list[str], raw_components)
    if len(components) != len(set(components)):
        raise TestUpdateContractError("components must not contain duplicates")
    return frozenset(components)


def _parse_images(
    value: object,
    *,
    components: frozenset[str],
    commit: str,
) -> Mapping[str, TestImage]:
    raw_images = _require_object(value, "images")
    if set(raw_images) != set(components):
        raise TestUpdateContractError("images must match selected components exactly")

    images: dict[str, TestImage] = {}
    for component, raw_image in raw_images.items():
        image = _require_object(raw_image, f"image {component}")
        _require_exact_fields(image, _IMAGE_FIELDS, f"image {component}")
        ref = _require_matching_string(
            image["ref"],
            re.compile(rf"sms-platform-test-{re.escape(component)}:{re.escape(commit)}"),
            f"image {component} ref",
        )
        image_id = _require_matching_string(
            image["id"],
            _IMAGE_ID_RE,
            f"image {component} id",
        )
        archive_file = _require_matching_string(
            image["archive_file"],
            re.compile(rf"{re.escape(component)}[.]tar"),
            f"image {component} archive_file",
        )
        if Path(archive_file).name != archive_file:
            raise TestUpdateContractError(f"image {component} archive_file has an invalid value")
        archive_sha256 = _require_matching_string(
            image["archive_sha256"],
            _SHA256_RE,
            f"image {component} archive_sha256",
        )
        images[component] = TestImage(
            ref=ref,
            image_id=image_id,
            archive_file=archive_file,
            archive_sha256=archive_sha256,
        )
    return MappingProxyType(images)


def _parse_public_cutover(value: object) -> PublicCutoverEvidence | None:
    if value is None:
        return None
    evidence = _require_object(value, "public_cutover")
    _require_exact_fields(
        evidence,
        _PUBLIC_CUTOVER_FIELDS,
        "public_cutover",
    )
    source_commit = _require_matching_string(
        evidence["source_commit"],
        _COMMIT_RE,
        "public_cutover source_commit",
    )
    private_merge_base = _require_matching_string(
        evidence["private_merge_base"],
        _COMMIT_RE,
        "public_cutover private_merge_base",
    )
    pack_file = _require_matching_string(
        evidence["pack_file"],
        re.compile(r"cutover-source[.]pack"),
        "public_cutover pack_file",
    )
    if Path(pack_file).name != pack_file:
        raise TestUpdateContractError("public_cutover pack_file has an invalid value")
    pack_sha256 = _require_matching_string(
        evidence["pack_sha256"],
        _SHA256_RE,
        "public_cutover pack_sha256",
    )
    return PublicCutoverEvidence(
        source_commit=source_commit,
        private_merge_base=private_merge_base,
        pack_file=pack_file,
        pack_sha256=pack_sha256,
    )


def parse_test_update_request(raw: str) -> TestUpdateRequest:
    """从 JSON 文本解析并完整校验快速更新请求。"""

    if type(raw) is not str:
        raise TestUpdateContractError("request JSON must be an exact str")
    try:
        decoded = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise TestUpdateContractError("request contains invalid JSON") from exc

    payload = _require_object(decoded, "request")
    actual_fields = set(payload)
    unknown_fields = actual_fields - (set(_TOP_LEVEL_FIELDS) | set(_OPTIONAL_TOP_LEVEL_FIELDS))
    missing_fields = set(_TOP_LEVEL_FIELDS) - actual_fields
    if unknown_fields:
        raise TestUpdateContractError(f"request has unknown fields: {sorted(unknown_fields)}")
    if missing_fields:
        raise TestUpdateContractError(f"request has missing fields: {sorted(missing_fields)}")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise TestUpdateContractError("schema_version has an invalid value")

    update_id = _require_matching_string(payload["update_id"], _UPDATE_ID_RE, "update_id")
    base_commit = _require_matching_string(payload["base_commit"], _COMMIT_RE, "base_commit")
    commit = _require_matching_string(payload["commit"], _COMMIT_RE, "commit")
    source_ref = _require_source_ref(payload["source_ref"])
    environment_mode = payload["environment_mode"]
    if type(environment_mode) is not str or environment_mode not in {"pre-live", "live"}:
        raise TestUpdateContractError("environment_mode has an invalid value")
    operation = payload.get("operation", "apply")
    if type(operation) is not str or operation not in {"apply", "rebaseline"}:
        raise TestUpdateContractError("operation has an invalid value")
    components = _require_components(payload["components"])
    images = _parse_images(payload["images"], components=components, commit=commit)
    public_cutover = _parse_public_cutover(payload.get("public_cutover"))
    if public_cutover is not None and source_ref != "origin/main":
        raise TestUpdateContractError("public_cutover requires source_ref origin/main")

    migration = _require_object(payload["migration"], "migration")
    _require_exact_fields(migration, _MIGRATION_FIELDS, "migration")
    migration_from = _require_matching_string(migration["from"], _SAFE_NAME_RE, "migration from")
    migration_target = _require_matching_string(
        migration["target"], _SAFE_NAME_RE, "migration target"
    )
    compatibility = migration["compatibility"]
    if compatibility not in ("none", "expand") or type(compatibility) is not str:
        raise TestUpdateContractError("migration compatibility has an invalid value")
    if compatibility == "none" and migration_from != migration_target:
        raise TestUpdateContractError("migration compatibility none requires equal heads")
    if compatibility == "expand" and migration_from == migration_target:
        raise TestUpdateContractError("migration compatibility expand requires different heads")
    if operation == "rebaseline":
        if source_ref != "origin/main":
            raise TestUpdateContractError("rebaseline requires source_ref origin/main")
        if public_cutover is not None:
            raise TestUpdateContractError("rebaseline must not carry public_cutover")
        if components != _COMPONENTS:
            raise TestUpdateContractError("rebaseline requires api and web components")
        if compatibility != "expand":
            raise TestUpdateContractError("rebaseline requires expand migration")

    return TestUpdateRequest(
        update_id=update_id,
        base_commit=base_commit,
        commit=commit,
        source_ref=source_ref,
        environment_mode=cast(Literal["pre-live", "live"], environment_mode),
        components=components,
        images=images,
        public_cutover=public_cutover,
        migration_from=migration_from,
        migration_target=migration_target,
        migration_compatibility=cast(Literal["none", "expand"], compatibility),
        operation=cast(Literal["apply", "rebaseline"], operation),
    )


def _require_safe_changed_path(value: object) -> str:
    if type(value) is not str or not value or value.startswith("/") or "\\" in value:
        raise TestUpdateContractError("invalid changed path")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise TestUpdateContractError(f"invalid changed path: {value}")
    return value


def _reject_forbidden_path(path: str) -> None:
    if path.startswith("deploy/") and path != "deploy/nginx.conf":
        raise TestUpdateContractError(f"fast update forbidden for path: {path}")


def protected_change_category(
    path: str,
) -> Literal["vendor-live", "backend-critical"] | None:
    """返回 CI 与测试发布共用的受保护路径类别。"""

    if (
        path in _VENDOR_LIVE_PROTECTED_EXACT
        or path.startswith("deploy/security-report/")
        or path.startswith(_VENDOR_LIVE_PROTECTED_PREFIXES)
    ):
        return "vendor-live"
    if path in _BACKEND_CRITICAL_PROTECTED_EXACT or path.startswith(
        _BACKEND_CRITICAL_PROTECTED_PREFIXES
    ):
        return "backend-critical"
    return None


def _is_high_risk(path: str) -> bool:
    return (
        path in _WEB_HIGH_RISK_EXACT
        or path in _MAILER_HIGH_RISK_EXACT
        or path in _INFRA_HIGH_RISK_EXACT
        or protected_change_category(path) is not None
    )


def classify_changed_paths(paths: Iterable[str]) -> ChangedScope:
    """先拒绝数据和发布控制变更，再归类可快速更新的应用差异。"""

    components: set[str] = set()
    backend_tests: set[str] = set()
    frontend_tests: set[str] = set()
    migration_changed = False
    high_risk_paths: set[str] = set()

    for raw_path in paths:
        path = _require_safe_changed_path(raw_path)
        if (
            path in _SAFE_OPERATIONAL_DOCS
            or path in _SAFE_NON_RUNTIME_GATES
            or path.startswith(".github/")
        ):
            continue
        if path in _WEB_RUNTIME_DEPLOY_EXACT:
            components.add("web")
        if _is_high_risk(path):
            high_risk_paths.add(path)
            if path in _WEB_HIGH_RISK_EXACT:
                components.add("web")
            elif path in _MAILER_HIGH_RISK_EXACT or path.startswith(
                ("backend/", "deploy/", "scripts/")
            ):
                components.add("api")
            if path.startswith("frontend/"):
                components.add("web")
            if path == "schema.sql" or path.startswith("backend/migrations/"):
                components.add("api")
                migration_changed = True
            continue
        _reject_forbidden_path(path)

        if path.startswith("backend/tests/"):
            backend_tests.add(path)
            continue
        if path.startswith("frontend/tests/"):
            frontend_tests.add(path)
            continue
        if (
            path.startswith("docs/plans/")
            or path.startswith("docs/TEST-REPORT-")
            or path == "openapi.yaml"
        ):
            continue
        if path.startswith("backend/migrations/") or path == "schema.sql":
            components.add("api")
            migration_changed = True
            continue
        if path.startswith("backend/"):
            components.add("api")
            continue
        if path.startswith("frontend/") or path == "deploy/nginx.conf":
            components.add("web")
            continue
        raise TestUpdateContractError(f"fast update forbidden for unclassified path: {path}")

    frozen_components = frozenset(components)
    if high_risk_paths:
        risk: Literal["none", "web-only", "backend-safe", "high-risk"] = "high-risk"
    elif frozen_components == {"web"}:
        risk = "web-only"
    elif "api" in frozen_components:
        risk = "backend-safe"
    else:
        risk = "none"
    return ChangedScope(
        components=frozen_components,
        migration_changed=migration_changed,
        backend_tests=tuple(sorted(backend_tests)),
        frontend_tests=tuple(sorted(frontend_tests)),
        runtime_changed=bool(frozen_components),
        risk=risk,
        high_risk_paths=tuple(sorted(high_risk_paths)),
    )


def classify_public_cutover_paths(paths: Iterable[str]) -> ChangedScope:
    """仅为已验真无历史公开快照剥离非运行态发布差异。"""

    regular_paths: list[str] = []
    cutover_high_risk_paths: set[str] = set()
    for raw_path in paths:
        path = _require_safe_changed_path(raw_path)
        if path in _PUBLIC_CUTOVER_SAFE_NON_RUNTIME_EXACT or path.startswith("docs/"):
            continue
        if path in _PUBLIC_CUTOVER_HIGH_RISK_EXACT:
            cutover_high_risk_paths.add(path)
            continue
        regular_paths.append(path)

    regular = classify_changed_paths(regular_paths)
    components = set(regular.components)
    if cutover_high_risk_paths:
        components.add("api")
    high_risk_paths = set(regular.high_risk_paths)
    high_risk_paths.update(cutover_high_risk_paths)
    return ChangedScope(
        components=frozenset(components),
        migration_changed=regular.migration_changed,
        backend_tests=regular.backend_tests,
        frontend_tests=regular.frontend_tests,
        runtime_changed=bool(components),
        risk="high-risk" if high_risk_paths else regular.risk,
        high_risk_paths=tuple(sorted(high_risk_paths)),
    )


def classify_rebaseline_paths(paths: Iterable[str]) -> ChangedScope:
    """严格分类同历史测试基线重对齐，不放宽日常快速更新。"""

    regular_paths: list[str] = []
    rebaseline_high_risk_paths: set[str] = set()
    for raw_path in paths:
        path = _require_safe_changed_path(raw_path)
        if path in _REBASELINE_SAFE_NON_RUNTIME_EXACT:
            continue
        if path in _REBASELINE_HIGH_RISK_EXACT:
            rebaseline_high_risk_paths.add(path)
            continue
        regular_paths.append(path)

    if rebaseline_high_risk_paths != set(_REBASELINE_HIGH_RISK_EXACT):
        raise TestUpdateContractError(
            "rebaseline requires the exact approved runtime-control path set"
        )

    regular = classify_changed_paths(regular_paths)
    if not regular.migration_changed:
        raise TestUpdateContractError("rebaseline requires a migration change")

    high_risk_paths = set(regular.high_risk_paths)
    high_risk_paths.update(rebaseline_high_risk_paths)
    return ChangedScope(
        components=frozenset({"api", "web"}),
        migration_changed=True,
        backend_tests=regular.backend_tests,
        frontend_tests=regular.frontend_tests,
        runtime_changed=True,
        risk="high-risk",
        high_risk_paths=tuple(sorted(high_risk_paths)),
    )


def _classify_nul_stream(raw: bytes, *, rebaseline: bool = False) -> str:
    if len(raw) > 1_048_576:
        raise TestUpdateContractError("changed path input is too large")
    paths = [os.fsdecode(item) for item in raw.split(b"\0") if item]
    scope = classify_rebaseline_paths(paths) if rebaseline else classify_changed_paths(paths)
    return json.dumps(
        {
            "components": sorted(scope.components),
            "high_risk_paths": list(scope.high_risk_paths),
            "migration_changed": scope.migration_changed,
            "risk": scope.risk,
            "runtime_changed": scope.runtime_changed,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def validate_verified_status(
    raw: str,
    *,
    expected_update_id: str,
    expected_commit: str,
    expected_migration_head: str,
) -> None:
    """严格确认远端快速更新终态及其目标身份。"""

    _require_matching_string(expected_update_id, _UPDATE_ID_RE, "expected update_id")
    _require_matching_string(expected_commit, _COMMIT_RE, "expected commit")
    _require_matching_string(
        expected_migration_head,
        _SAFE_NAME_RE,
        "expected migration head",
    )
    if type(raw) is not str or not raw or len(raw.encode("utf-8")) > 4096:
        raise TestUpdateContractError("verified status is invalid")
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, TestUpdateContractError) as exc:
        raise TestUpdateContractError("verified status is invalid") from exc
    if type(value) is not dict or set(value) != {
        "update_id",
        "state",
        "actual_commit",
        "actual_migration_head",
    }:
        raise TestUpdateContractError("verified status is invalid")
    if value != {
        "update_id": expected_update_id,
        "state": "verified",
        "actual_commit": expected_commit,
        "actual_migration_head": expected_migration_head,
    }:
        raise TestUpdateContractError("verified status does not match target")


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    try:
        if arguments == ["classify-nul"]:
            print(_classify_nul_stream(sys.stdin.buffer.read()))
            return 0
        if arguments == ["classify-rebaseline-nul"]:
            print(_classify_nul_stream(sys.stdin.buffer.read(), rebaseline=True))
            return 0
        if len(arguments) == 4 and arguments[0] == "verify-status":
            validate_verified_status(
                sys.stdin.buffer.read(4097).decode("utf-8"),
                expected_update_id=arguments[1],
                expected_commit=arguments[2],
                expected_migration_head=arguments[3],
            )
            print("verified")
            return 0
        raise TestUpdateContractError("unsupported test update contract action")
    except (OSError, UnicodeError, TestUpdateContractError):
        print("test update contract blocked", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
