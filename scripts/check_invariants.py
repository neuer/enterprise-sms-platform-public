#!/usr/bin/env python3
"""对可静态判定的工程硬规则做失败即退出检查。"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

from any_budget import ANY_ROOT, any_budget_violations, collect_any_counts
from gate_policy import (
    EXPLICIT_ANY_BUDGET,
    SIZE_BUDGET_EXCEPTIONS,
    SPLIT_MODULE_PARTS,
    SPLIT_MODULE_UNRELATED,
    logical_module_files,
)
from single_implementation import (
    billing_literal_lines,
    celery_task_tracking,
    scheduled_task_names,
    success_rate_lines,
    template_placeholder_lines,
    untracked_scheduled_tasks,
)
from size_budget import BUDGET_ROOTS, collect_sizes, size_budget_violations

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "backend" / "app"
errors: list[str] = []


def fail(path: Path, message: str, line: int | None = None) -> None:
    rel = path.relative_to(ROOT)
    suffix = f":{line}" if line else ""
    errors.append(f"{rel}{suffix}: {message}")


def dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return dotted_name(node.func)
    return ""


def read_logical_module(path: Path) -> str:
    """读取逻辑模块全文：已登记拆分的入口按入口 + 拆出文件拼接。"""

    if not path.is_relative_to(APP):
        return path.read_text(encoding="utf-8")
    entry = path.relative_to(APP).as_posix()
    return "\n".join(
        (APP / part).read_text(encoding="utf-8") for part in logical_module_files(entry)
    )


def require_fragments(path: Path, *fragments: str) -> str:
    """要求关键安全边界继续保留，返回源文本供顺序检查。"""

    try:
        source = read_logical_module(path)
    except OSError:
        fail(path, "关键边界文件缺失")
        return ""
    for fragment in fragments:
        if fragment not in source:
            fail(path, f"关键边界不变量缺失: {fragment}")
    return source


def literal_string(path: Path, name: str) -> str | None:
    """读取模块级字符串常量；禁止动态拼接。已登记拆分的入口在逻辑模块内唯一定义。"""

    parts = (
        [APP / part for part in logical_module_files(path.relative_to(APP).as_posix())]
        if path.is_relative_to(APP)
        else [path]
    )
    found: list[ast.expr | None] = []
    for part in parts:
        try:
            tree = ast.parse(part.read_text(encoding="utf-8"), filename=str(part))
        except (OSError, SyntaxError) as exc:
            fail(path, f"无法读取字符串常量定义: {type(exc).__name__}")
            return None
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == name for target in targets):
                found.append(node.value)
    if not found:
        fail(path, f"缺少字符串常量 {name}")
        return None
    if len(found) > 1:
        fail(path, f"{name} 在逻辑模块内只能定义一次")
        return None
    value = found[0]
    if isinstance(value, ast.Constant) and type(value.value) is str:
        return value.value
    fail(path, f"{name} 必须是模块级字符串常量")
    return None


def check_vendor_live_invariants() -> None:
    """真实联调已下线：锁定厂商调用边界，并防止联调入口、挂载与契约被恢复。"""

    settings = require_fragments(
        APP / "settings.py",
        "development/test requires VENDOR_MOCK=1",
        "production requires DEBUG=0, AUTH_MOCK=0 and VENDOR_MOCK=0",
    )
    if "vendor_secret_name:" in settings or "vendor_secret_key:" in settings:
        fail(APP / "settings.py", "厂商凭据不得成为环境变量值字段")
    if "def vendor_live_test(" in settings:
        fail(APP / "settings.py", "真实联调模式已下线，不得恢复非生产环境直连真实厂商的开关")
    for retired in (
        "api/vendor_test.py",
        "api/vendor_control_ready.py",
        "services/vendor_control_client.py",
        "services/vendor_control_state.py",
        "services/vendor_test_budget.py",
        "services/vendor_test_guard.py",
        "services/vendor_test_operation.py",
        "services/vendor_test_operation_repository.py",
        "services/vendor_test_recipient.py",
        "services/vendor_test_recipient_repository.py",
        "services/vendor_test_uat.py",
    ):
        if (APP / retired).exists():
            fail(APP / retired, "真实联调功能已下线，不得恢复该模块")
    for relative in ("main.py", "api/messages.py"):
        source = require_fragments(APP / relative)
        for forbidden in ("vendor_test", "uat-send"):
            if forbidden in source:
                fail(APP / relative, f"真实联调入口已下线，不得恢复 {forbidden}")

    worker = require_fragments(
        APP / "tasks/send.py",
        "_preflight_before_invoke(chunk, lane, vendor_id)",
        "_token(lane, vendor_id)",
        "claim_submission(",
        "_gateway_for(vendor_id)",
        "gateway.send(",
    )
    if worker and not (
        worker.index("_token(lane, vendor_id)") < worker.index("gateway.send(")
    ):
        fail(APP / "tasks/send.py", "worker 必须先取令牌并完成预检再调用厂商")

    billing_path = APP / "services/billing.py"
    for path in APP.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in {"calculate_segments", "calculate_quota_cost"}
                and path != billing_path
            ):
                fail(path, "计费条计算只能在 services/billing.py 定义", node.lineno)
    require_fragments(APP / "services/pipeline.py", "from app.services.billing import")

    require_fragments(
        APP / "vendor/codes.py",
        '1010: _policy("IP 校验未通过", alert_level="crit")',
        "pause_queues=True",
        'alert_level="crit"',
    )

    schema_source = (ROOT / "schema.sql").read_text(encoding="utf-8")
    if re.search(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?vendor_test_", schema_source, re.I):
        fail(ROOT / "schema.sql", "真实联调专用表已退役，禁止恢复 vendor_test_* 库表")

    compose = require_fragments(
        ROOT / "deploy/docker-compose.yml",
        "mock-vendor:",
        "profiles: [dev]",
    )
    if compose.count("profiles: [dev]") < 1:
        fail(ROOT / "deploy/docker-compose.yml", "mock-vendor 只允许出现在 dev profile")
    if "docker.sock" in compose:
        fail(ROOT / "deploy/docker-compose.yml", "API/worker 禁止挂载宿主 Docker Socket")
    for retired_mount in ("/run/vendor-test", "/run/vendor-control"):
        if retired_mount in compose:
            fail(ROOT / "deploy/docker-compose.yml", f"已下线的联调挂载不得恢复: {retired_mount}")
    if re.search(
        r"^\s*-\s+.*?/run/sms-platform/secrets",
        compose,
        flags=re.MULTILINE,
    ):
        fail(ROOT / "deploy/docker-compose.yml", "API/worker 禁止挂载宿主运行密钥目录")

    openapi = require_fragments(ROOT / "openapi.yaml")
    for forbidden in ("/vendor-test/", "/messages/uat-send", "CredentialEnvelopeModel"):
        if forbidden in openapi:
            fail(ROOT / "openapi.yaml", f"真实联调接口已下线，契约不得恢复 {forbidden}")

    update_contract = require_fragments(
        ROOT / "deploy/scripts/protected_path_policy.py",
        "VENDOR_LIVE_PROTECTED_EXACT",
        "deploy/scripts/vendor_test_",
        "deploy/scripts/test_update_",
        "deploy/scripts/test_secure_access_",
    )
    for high_risk_path in (
        "backend/app/api/messages.py",
        "backend/app/api/vendor_test.py",
        "backend/app/api/web_messages.py",
        "backend/app/cli.py",
        "backend/app/main.py",
        "backend/app/settings.py",
        "backend/app/services/pipeline.py",
        "backend/app/services/reconcile_repository.py",
        "backend/app/services/vendor_control_client.py",
        "backend/app/services/vendor_control_state.py",
        "backend/app/vendor/zhihui.py",
        "backend/app/vendor/codes.py",
        "backend/app/tasks/send.py",
        "backend/app/tasks/reconcile.py",
        "backend/app/services/billing.py",
        "backend/app/services/vendor_test_guard.py",
        "backend/app/services/vendor_test_budget.py",
        "backend/app/services/vendor_test_operation.py",
        "backend/app/services/vendor_test_operation_repository.py",
        "backend/app/services/vendor_test_pause.py",
        "backend/app/services/vendor_test_recipient.py",
        "backend/app/services/vendor_test_recipient_repository.py",
        "backend/app/services/vendor_test_step_up.py",
        "backend/app/services/vendor_test_uat.py",
        "backend/app/tasks/send_repository.py",
        "backend/migrations/versions/0016_vendor_live_test_budget.py",
        "backend/migrations/versions/0017_vendor_test_web_console.py",
        "backend/migrations/versions/0018_vendor_test_operation_vendor_code.py",
        "backend/migrations/versions/0019_vendor_test_recipient_hmac_alias.py",
        "backend/vendor_control_protocol.py",
        "backend/vendor_control_protocol.pyi",
        "deploy/scripts/install_vendor_credentials.py",
        "deploy/scripts/vendor_control_agent.py",
        "deploy/scripts/vendor_control_journal.py",
        "deploy/scripts/vendor_control_protocol.py",
        "deploy/scripts/vendor_credential_store.py",
        "deploy/scripts/vendor_control_reload.py",
        "deploy/scripts/vendor_seal_sessions.py",
        "deploy/systemd/vendor-control-agent.service",
        "deploy/scripts/install_test_secure_access.py",
        "deploy/scripts/test_secure_access_contract.py",
        "deploy/scripts/test_secure_access_manager.py",
        "deploy/scripts/test_secure_access_runtime.py",
        "deploy/scripts/cloudflare_tunnel_manager.py",
        "deploy/systemd/sms-platform-test-secure-access.service",
        "deploy/systemd/sms-platform-cloudflare-tunnel.service",
        "deploy/sms-compose",
        "scripts/check_invariants.py",
        "scripts/classify_ci_changes.py",
        "frontend/src/api/admin.ts",
        "frontend/src/components/VendorCredentialDialog.vue",
        "frontend/src/components/VendorTestConsole.vue",
        "frontend/src/components/VendorTestRecipientDialog.vue",
        "frontend/src/components/VendorTestUatPanel.vue",
        "frontend/src/lib/vendorSeal.ts",
        "frontend/src/views/ConfigView.vue",
        "openapi.yaml",
        "schema.sql",
        "deploy/docker-compose.yml",
    ):
        covered_by_secure_prefix = (
            high_risk_path.startswith("deploy/scripts/test_secure_access_")
            and "deploy/scripts/test_secure_access_" in update_contract
        )
        if high_risk_path not in update_contract and not covered_by_secure_prefix:
            fail(
                ROOT / "deploy/scripts/protected_path_policy.py",
                f"vendor-live 保护清单缺少高风险路径 {high_risk_path}",
            )

    wrapper = require_fragments(
        ROOT / "deploy/sms-compose",
        "validate_production_launch()",
        '"$seen_debug" != 1 || "$env_debug" != 0',
        '"$seen_auth_mock" != 1 || "$env_auth_mock" != 0',
        '"$seen_vendor_mock" != 1 || "$env_vendor_mock" != 0',
        "reject_production_control_plane test-update",
    )
    for retired in ("vendor-test", "secure-access", "cloudflare-tunnel", "vendor-agent-preflight"):
        if retired in wrapper:
            fail(ROOT / "deploy/sms-compose", f"已退役的测试主机控制入口不得恢复: {retired}")
    update_dispatch = wrapper.split("dispatch_test_update()", maxsplit=1)[-1].split(
        "run_locked_operation()", maxsplit=1
    )[0]
    for forbidden in ("restore", "rollback", "mock-vendor", "reset --hard", "down -v"):
        if forbidden in update_dispatch:
            fail(ROOT / "deploy/sms-compose", f"快速更新包装器禁止 {forbidden}")
    require_fragments(
        ROOT / "scripts/classify_ci_changes.py",
        "protected_change_category",
        'VENDOR_LIVE: RuleResult = (True, True, True, True, "vendor-live")',
        'FRONTEND_SECURITY: RuleResult = (False, True, False, True, "frontend-security")',
        "--name-status",
        "--find-renames",
        "--find-copies-harder",
    )
    require_fragments(
        ROOT / "deploy/scripts/protected_path_policy.py",
        "VENDOR_LIVE_PROTECTED_EXACT",
        "def protected_change_category(",
        "BACKEND_CRITICAL_DOMAINS",
        "FRONTEND_SECURITY_DOMAINS",
        "REVIEWED_ORDINARY_EXACT",
        "REVIEWED_ORDINARY_REASONS",
        "REQUIRED_TRACKED_SOURCE_TREES",
        "REQUIRED_TRACKED_ROOT_GLOBS",
        "def security_domain_category(",
        "def unclassified_tracked_source_paths(",
        "def render_codeowners(",
    )
    require_fragments(
        ROOT / "scripts/verify_release.sh",
        "--severity HIGH,CRITICAL",
        "--exit-code 1",
    )


def check_outbox_invariants() -> None:
    """锁定事务性 Outbox 的原子写、租约、无 PII 与发布生命周期。"""

    require_fragments(
        APP / "services/outbox.py",
        "spec.task_name not in TASK_NAMES",
        '"app.tasks.outbox.deliver_alert"',
        "asyncio.create_task(maintain_lease())",
        'raise OutboxLeaseLost("outbox execution lease was lost")',
    )
    require_fragments(
        APP / "services/outbox_repository.py",
        "FOR UPDATE SKIP LOCKED",
        "failure_count=failure_count+1",
        'state not in {"processing", "completed"}',
        "'outbox_retry','outbox_event'",
    )
    require_fragments(
        APP / "services/outbox_queue.py",
        "task_id=str(event.event_id)",
        "args=[*event.args, str(event.event_id)]",
    )
    for relative in (
        "services/pipeline_repository.py",
        "services/approval_repository.py",
        "services/scheduling_repository.py",
        "services/callback_repository.py",
        "services/alert_repository.py",
    ):
        require_fragments(
            APP / relative,
            "OutboxEventSpec",
            "enqueue_outbox(",
        )
    require_fragments(
        APP / "api/ops.py",
        '"/outbox"',
        '"/outbox/{event_id}/retry"',
        "principal=claims.principal",
    )
    require_fragments(
        ROOT / "backend/migrations/versions/0027_transactional_outbox.py",
        "ck_outbox_args_scalar_refs",
        "ck_outbox_args_no_pii",
        "ck_outbox_refs_no_pii",
        "'app.tasks.outbox.deliver_alert'",
        "REVOKE DELETE,TRUNCATE ON outbox_event FROM sms_app",
        "cannot downgrade transactional outbox with unfinished events",
    )
    compose = require_fragments(
        ROOT / "deploy/docker-compose.yml",
        "outbox-dispatcher:",
        "command: python -m app.outbox_dispatcher",
        "migrate: { condition: service_completed_successfully }",
    )
    dispatcher_section = compose.split("  outbox-dispatcher:", maxsplit=1)[-1].split(
        "\n  mock-vendor:",
        maxsplit=1,
    )[0]
    if "redis:" in dispatcher_section:
        fail(
            ROOT / "deploy/docker-compose.yml",
            "Outbox dispatcher 不得依赖 broker 才能启动",
        )
    for relative in (
        "deploy/sms-compose",
        "deploy/scripts/release_manager.py",
    ):
        require_fragments(ROOT / relative, "outbox-dispatcher")


def check_usage_ledger_invariants() -> None:
    """锁定用量事实、唯一释放、失败关闭和无 PII 恢复入口。"""

    require_fragments(
        APP / "services/usage_ledger.py",
        "INSERT INTO usage_reservation",
        "pg_advisory_xact_lock",
        "state='release_requested'",
        "APPLY_PROJECTION_LUA",
        "current_version > incoming_version",
        "usage_frequency_alias",
        "'usage_projection_rebuild'",
        '"subject_id": str(row["subject_id"])',
        "_list_active_frequency_entry_refs",
        "_write_expired_canonical_tombstone",
        "_apply_release_projection_changes",
        "if drift.mismatches:",
        "RECONCILE_REBUILD_ACTOR",
        "recover_orphans()",
        "measure_drift()",
    )
    actor = literal_string(APP / "services/usage_ledger.py", "RECONCILE_REBUILD_ACTOR")
    schema = require_fragments(ROOT / "schema.sql", "enforce_live_audit_principal")
    start = schema.find("CREATE OR REPLACE FUNCTION enforce_live_audit_principal()")
    end = schema.find("REVOKE ALL ON FUNCTION enforce_live_audit_principal()")
    function = schema[start:end] if start >= 0 and end > start else ""
    realtime = function.split("context_domain='realtime'", maxsplit=1)
    bulk = function.split("context_domain='bulk'", maxsplit=1)
    if actor and (
        len(realtime) < 2
        or actor not in realtime[1].split("context_domain='bulk'", maxsplit=1)[0]
    ):
        fail(
            ROOT / "schema.sql",
            "用量巡检重建 actor 未进入 realtime/sms_send 系统审计白名单",
        )
    if actor and (len(bulk) < 2 or actor not in bulk[1]):
        fail(
            ROOT / "schema.sql",
            "用量巡检重建 actor 未进入 bulk/sms_send 系统审计白名单",
        )
    require_fragments(
        APP / "services/pipeline.py",
        "UsageLedgerPort",
        "usage_reservation_id=acceptance.usage_reservation_id",
        "hmac_aliases=frequency_aliases_by_active",
        'await self._release_usage(acceptance, "idempotent-reuse")',
    )
    for relative in ("api/messages.py", "api/web_messages.py"):
        require_fragments(APP / relative, "UsageLedgerService(")
    require_fragments(
        APP / "services/pipeline_repository.py",
        "commit_usage_reservation(",
        "usage_reservation_id",
    )
    for relative in ("services/approval_repository.py", "services/scheduling_repository.py"):
        require_fragments(APP / relative, "request_usage_release_for_batch(")
    require_fragments(
        APP / "tasks/usage_projection.py",
        '@tracked_job("reconcile_usage_projection", expect_interval_s=300)',
        "reconcile_usage_facts(",
    )
    require_fragments(
        ROOT / "backend/migrations/versions/0028_usage_fact_ledger.py",
        "usage_projection_version_seq",
        "uk_usage_reservation_active_request",
        "ck_usage_request_key_no_pii",
        "release_event_id VARCHAR(192) UNIQUE",
        "cannot downgrade usage fact ledger with active reservations",
    )
    require_fragments(ROOT / "openapi.yaml", "USAGE_PROJECTION_UNAVAILABLE")
    require_fragments(
        ROOT / "docs/runbooks/usage-ledger-recovery.md",
        "usage-projection-rebuild",
        "不得扩展为手机号、密文、HMAC",
        "确认漂移后按事实表执行版本化绝对覆盖",
    )


def check_worker_fencing_invariants() -> None:
    """callback/export 长任务必须由 UUID 租约和 CAS 保护。"""

    require_fragments(
        ROOT / "schema.sql",
        "CREATE TABLE worker_lease_event",
        "CONSTRAINT chk_cb_lease_pair",
        "CONSTRAINT chk_export_lease_pair",
        "uk_callback_task_event_id",
    )
    require_fragments(
        APP / "services/callback_repository.py",
        "lease_id=:lease_id AND lease_expires_at>now()",
        '"heartbeat_lost"',
        '"fencing_miss"',
    )
    require_fragments(
        APP / "services/callback.py",
        '"event_id": str(material.task.event_id)',
        '"X-Sms-Event-Id": str(material.task.event_id)',
    )
    require_fragments(
        APP / "services/export_repository.py",
        "lease_id=:lease_id",
        "lease_expires_at>now()",
        "raise ExportLeaseLost",
    )
    require_fragments(
        APP / "services/export_file.py",
        'f"export-{task_id}-{token}.part"',
        'f"export-{task_id}-{token}.smsx"',
        "fsync_directory",
        "verify_ready",
    )
    require_fragments(
        APP / "services/file_durability.py",
        "def fsync_directory",
        "os.fsync",
        "os.replace",
        "mark_done",
    )
    require_fragments(
        APP / "services/import_file.py",
        "fsync_directory",
    )
    require_fragments(
        APP / "services/export_reconcile.py",
        "mark_unreadable",
        "list_root_artifacts",
        "clear_file",
    )
    require_fragments(
        ROOT / "backend/migrations/versions/0029_worker_fencing_leases.py",
        'revision = "0029_worker_fencing_leases"',
        "cannot downgrade worker fencing with lease evidence",
    )


def check_vendor_event_invariants() -> None:
    """report/reply 事实必须由数据库去重且报告投影单调。"""

    require_fragments(
        ROOT / "schema.sql",
        "CREATE TABLE report_event (",
        "CREATE TABLE report_event_projection (",
        "CREATE TABLE reply_event (",
        "report_event, reply_event, worker_lease_event, audit_log",
        "GRANT INSERT, DELETE ON callback_report_event TO sms_send",
        "GRANT INSERT, UPDATE, DELETE ON callback_task TO sms_send",
    )
    require_fragments(
        APP / "services/report_repository.py",
        "ON CONFLICT(event_key) DO NOTHING",
        "report_event_projection",
        "report_event_key=CAST(:event_key AS char(64))",
        "if not changed:",
    )
    require_fragments(
        APP / "services/reply_repository.py",
        "INSERT INTO reply_event",
        "ON CONFLICT(event_key) DO NOTHING",
        "FROM event_insert",
    )
    require_fragments(
        APP / "services/reply_ingest.py",
        "hmac_candidates[min(hmac_candidates)]",
        "reply_time.astimezone(UTC)",
    )
    require_fragments(
        ROOT / "backend/migrations/versions/0030_vendor_event_facts.py",
        'revision = "0030_vendor_event_facts"',
        "cannot downgrade vendor event facts with ingested evidence",
    )
    require_fragments(
        APP / "services/vendor_event_audit.py",
        "find_legacy_reply_duplicates",
        "raw_id IS NULL",
        "phone_masks",
    )
    require_fragments(
        ROOT / "docs/runbooks/vendor-event-dedup-repair.md",
        "vendor-event-duplicate-audit",
        "DELETE FROM sms_reply",
        "reply_event",
        "ROLLBACK",
    )


def check_import_reservation_invariants() -> None:
    """导入包必须先预留，并在批次事务内固化唯一消费批次。"""

    require_fragments(
        APP / "services/import_repository.py",
        "class ImportReservation",
        "FOR UPDATE OF t",
        "state='reserved'",
        "reservation_expires_at>now()",
        "state='consumed'",
        "consumed_batch_id=:batch_id",
    )
    require_fragments(
        APP / "services/pipeline_repository.py",
        "consume_import_reservation",
        "command.import_reservation_id",
    )
    require_fragments(
        ROOT / "backend/migrations/versions/0031_import_reservation_state.py",
        'revision = "0031_import_reservation_state"',
        "cannot downgrade import reservation state with active evidence",
    )
    require_fragments(
        ROOT / "schema.sql",
        "CONSTRAINT ck_import_task_reservation_state",
        "CONSTRAINT uk_import_reservation_id",
        "CONSTRAINT uk_import_consumed_batch",
        "idx_import_reservation_expiry",
        "payload_purged_at TIMESTAMPTZ",
    )


def check_async_import_invariants() -> None:
    """上传只登记密文源，worker 以租约分块解析且可崩溃恢复。"""

    require_fragments(
        APP / "api/web_messages.py",
        "status_code=202",
        "parser.preflight",
        "codec.stage",
        "repository.attach_source",
    )
    require_fragments(
        APP / "services/import_repository.py",
        "parse_status='processing'",
        "parse_lease_id=:lease_id",
        "parse_lease_expires_at>now()",
        "ON CONFLICT(import_task_id,phone_hmac) DO NOTHING",
    )
    require_fragments(
        APP / "tasks/imports.py",
        "run_bounded",
        "append_parse_batch",
        "release_parse",
        'tracked_job("dispatch_imports", expect_interval_s=30)',
    )
    require_fragments(
        ROOT / "backend/migrations/versions/0032_async_import_runtime.py",
        'revision = "0032_async_import_runtime"',
        "cannot downgrade async import runtime with staged evidence",
    )
    require_fragments(
        ROOT / "schema.sql",
        "CONSTRAINT ck_import_parse_status",
        "CONSTRAINT ck_import_parse_source",
        "CONSTRAINT ck_import_parse_lease",
        "idx_import_parse_due",
    )


def check_size_budget() -> None:
    """应用源码体积预算：新文件不得超限，登记的存量超限文件只许缩小。"""

    paths = [
        path.relative_to(ROOT).as_posix()
        for prefix, suffixes in BUDGET_ROOTS
        for path in (ROOT / prefix).rglob("*")
        if path.is_file() and path.name.endswith(suffixes)
    ]
    for path, message in size_budget_violations(
        collect_sizes(ROOT, paths), SIZE_BUDGET_EXCEPTIONS
    ):
        fail(ROOT / path, message)


def check_any_budget() -> None:
    """backend/app 显式 Any 棘轮：新文件不得使用，登记的存量计数只许下调。"""

    paths = [path.relative_to(ROOT).as_posix() for path in (ROOT / ANY_ROOT).rglob("*.py")]
    for path, message in any_budget_violations(
        collect_any_counts(ROOT, paths), EXPLICIT_ANY_BUDGET
    ):
        fail(ROOT / path, message)


def check_split_module_registry() -> None:
    """拆分模块的登记表必须与磁盘一致，且拆出文件继承入口的 vendor-live 保护。"""

    sys.path.insert(0, str(ROOT / "deploy" / "scripts"))
    from protected_path_policy import protected_change_category  # noqa: E402

    registry = ROOT / "scripts" / "gate_policy.py"
    for entry, parts in SPLIT_MODULE_PARTS.items():
        entry_path = APP / entry
        if not entry_path.is_file():
            fail(registry, f"拆分模块入口不存在: {entry}")
            continue
        prefix = f"{entry_path.stem}_"
        for part in parts:
            part_path = APP / part
            if part_path.parent != entry_path.parent or not part_path.name.startswith(prefix):
                fail(registry, f"拆出文件必须与入口同目录且以 {prefix} 开头: {part}")
            if not part_path.is_file():
                fail(registry, f"登记的拆出文件不存在: {part}")
        registered = {entry, *parts, *SPLIT_MODULE_UNRELATED}
        for sibling in sorted(entry_path.parent.glob(f"{prefix}*.py")):
            relative = sibling.relative_to(APP).as_posix()
            if relative not in registered:
                fail(sibling, f"拆分文件未登记到 gate_policy.SPLIT_MODULE_PARTS[{entry!r}]")
        if protected_change_category(f"backend/app/{entry}") == "vendor-live":
            for part in parts:
                if protected_change_category(f"backend/app/{part}") != "vendor-live":
                    fail(
                        ROOT / "deploy/scripts/protected_path_policy.py",
                        f"vendor-live 入口拆出的文件未进入 vendor-live 保护: {part}",
                    )


def check_protected_path_policy_invariants() -> None:
    """安全域 manifest 必须是分类与 CODEOWNERS 的同一来源。"""

    import subprocess

    sys.path.insert(0, str(ROOT / "deploy" / "scripts"))
    from protected_path_policy import (  # noqa: E402
        BACKEND_CRITICAL_DOMAINS,
        FRONTEND_SECURITY_DOMAINS,
        REQUIRED_TRACKED_ROOT_GLOBS,
        REQUIRED_TRACKED_SOURCE_TREES,
        REVIEWED_ORDINARY_EXACT,
        REVIEWED_ORDINARY_REASONS,
        parse_codeowners_patterns,
        render_codeowners,
        required_codeowners_patterns,
        unclassified_tracked_source_paths,
    )

    codeowners_path = ROOT / ".github" / "CODEOWNERS"
    codeowners = require_fragments(codeowners_path, *required_codeowners_patterns())
    if codeowners != render_codeowners():
        fail(codeowners_path, "CODEOWNERS 必须由 protected_path_policy.render_codeowners 逐字生成")
    try:
        parsed = parse_codeowners_patterns(codeowners)
    except ValueError as exc:
        fail(codeowners_path, str(exc))
        parsed = ()
    if parsed != required_codeowners_patterns():
        fail(codeowners_path, "CODEOWNERS 规则与安全域 manifest 双向不一致")
    if "backend/app/" not in BACKEND_CRITICAL_DOMAINS:
        fail(
            ROOT / "deploy" / "scripts" / "protected_path_policy.py",
            "backend/app/ 必须是 backend-critical 默认域",
        )
    if "frontend/src/views/" not in FRONTEND_SECURITY_DOMAINS:
        fail(
            ROOT / "deploy" / "scripts" / "protected_path_policy.py",
            "frontend/src/views/ 必须是 frontend-security 默认域",
        )
    if "frontend/src/components/" not in FRONTEND_SECURITY_DOMAINS:
        fail(
            ROOT / "deploy" / "scripts" / "protected_path_policy.py",
            "frontend/src/components/ 必须是 frontend-security 默认域",
        )
    if "frontend/src/*.ts" not in FRONTEND_SECURITY_DOMAINS:
        fail(
            ROOT / "deploy" / "scripts" / "protected_path_policy.py",
            "frontend/src/*.ts 必须是 frontend-security 默认域",
        )
    if "frontend/src/*.vue" not in FRONTEND_SECURITY_DOMAINS:
        fail(
            ROOT / "deploy" / "scripts" / "protected_path_policy.py",
            "frontend/src/*.vue 必须是 frontend-security 默认域",
        )
    if REQUIRED_TRACKED_ROOT_GLOBS != ("frontend/src/*.ts", "frontend/src/*.vue"):
        fail(
            ROOT / "deploy" / "scripts" / "protected_path_policy.py",
            "反向枚举必须独立覆盖 Frontend 根目录 *.ts 与 *.vue",
        )
    required_prefixes = {prefix for prefix, _suffixes in REQUIRED_TRACKED_SOURCE_TREES}
    if required_prefixes != {
        "backend/app/",
        "frontend/src/views/",
        "frontend/src/components/",
    }:
        fail(
            ROOT / "deploy" / "scripts" / "protected_path_policy.py",
            "反向枚举树必须独立覆盖 backend/app/、frontend/src/views/ 与 frontend/src/components/",
        )
    policy_source = (ROOT / "deploy" / "scripts" / "protected_path_policy.py").read_text(
        encoding="utf-8"
    )
    if "_BACKEND_CRITICAL_PROTECTED_EXACT" in policy_source:
        fail(
            ROOT / "deploy" / "scripts" / "protected_path_policy.py",
            "禁止继续用 exact allowlist 作为 backend-critical 分类来源",
        )
    try:
        completed = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z"],
            check=True,
            capture_output=True,
        )
        tracked = [item.decode("utf-8") for item in completed.stdout.split(b"\0") if item]
    except (OSError, subprocess.CalledProcessError, UnicodeError):
        fail(ROOT / "deploy" / "scripts" / "protected_path_policy.py", "无法枚举受跟踪文件")
        return
    missing = unclassified_tracked_source_paths(tracked)
    if missing:
        fail(
            ROOT / "deploy" / "scripts" / "protected_path_policy.py",
            f"tracked source 存在未归类路径: {missing[:8]}",
        )
    for relative, reason in REVIEWED_ORDINARY_REASONS.items():
        if relative not in tracked:
            fail(
                ROOT / "deploy" / "scripts" / "protected_path_policy.py",
                f"显式降级路径已不存在: {relative}",
            )
        if not reason.strip():
            fail(
                ROOT / "deploy" / "scripts" / "protected_path_policy.py",
                f"显式降级缺少理由: {relative}",
            )
    if frozenset(REVIEWED_ORDINARY_REASONS) != REVIEWED_ORDINARY_EXACT:
        fail(
            ROOT / "deploy" / "scripts" / "protected_path_policy.py",
            "REVIEWED_ORDINARY_EXACT 必须与理由表同源",
        )


def check_single_implementation_invariants() -> None:
    """计费、成功率、模板占位只允许单点实现；调度表中的每个 beat 任务都必须被追踪。"""

    single_homes = (
        (billing_literal_lines, APP / "services" / "billing.py", "计费条口径只允许在 billing.py"),
        (success_rate_lines, APP / "services" / "stats.py", "成功率口径只允许在 stats.py"),
        (
            template_placeholder_lines,
            APP / "services" / "template.py",
            "平台占位 {n} 解析只允许在 template.py",
        ),
    )
    tasks: dict[str, tuple[int, bool]] = {}
    for path in sorted(APP.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        try:
            for finder, home, message in single_homes:
                if path != home:
                    for line in finder(source):
                        fail(path, message, line)
            if path.parent == APP / "tasks":
                tasks.update(celery_task_tracking(source))
        except SyntaxError:
            continue
    scheduler = APP / "tasks" / "scheduler.py"
    scheduled = scheduled_task_names(scheduler.read_text(encoding="utf-8"))
    if not scheduled:
        fail(scheduler, "无法从 beat 启动调度表解析任务名")
    missing, untracked = untracked_scheduled_tasks(scheduled, tasks)
    for name in missing:
        fail(scheduler, f"beat 调度任务 {name} 找不到显式命名的 Celery 定义")
    for name in untracked:
        fail(scheduler, f"beat 调度任务 {name} 缺少 @tracked_job")


check_vendor_live_invariants()
check_single_implementation_invariants()
check_protected_path_policy_invariants()
check_split_module_registry()
check_size_budget()
check_any_budget()
check_outbox_invariants()
check_usage_ledger_invariants()
check_import_reservation_invariants()
check_async_import_invariants()
check_worker_fencing_invariants()
check_vendor_event_invariants()


if APP.exists():
    allowed_httpx = {
        Path("backend/app/vendor/zhihui.py"),
        Path("backend/app/services/callback.py"),
        Path("backend/app/services/alert.py"),
    }
    beat_modules = {
        "poll_report.py",
        "poll_reply.py",
        "poll_balance.py",
        "anomaly.py",
        "reconcile.py",
        "housekeeping.py",
        "stats.py",
        "scheduler.py",
        "usage_projection.py",
    }

    for path in APP.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(ROOT)

        if re.search(r"(?is)\b(?:UPDATE|DELETE\s+FROM|TRUNCATE)\s+audit_log\b", text):
            fail(path, "应用代码禁止修改、删除或截断 audit_log")
        if re.search(r"\bdatetime\.utcnow\s*\(", text) or re.search(
            r"\bdatetime\.now\s*\(\s*\)", text
        ):
            fail(path, "禁止 naive datetime；必须显式时区")
        if (
            re.search(r"(^|\n)\s*(?:import\s+httpx|from\s+httpx\s+import)", text)
            and rel not in allowed_httpx
        ):
            fail(path, "厂商/回调之外的业务代码禁止直接使用 httpx")
        for line_no, line in enumerate(text.splitlines(), start=1):
            lowered = line.lower()
            if (
                "redis" in lowered
                and ("phone" in lowered or "mobile" in lowered)
                and "phone_hmac" not in lowered
            ):
                fail(
                    path,
                    "Redis key/value 不得使用 phone/mobile 明文字段；应使用 phone_hmac",
                    line_no,
                )

        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError as exc:
            fail(path, f"Python语法错误: {exc.msg}", exc.lineno)
            continue

        if path.name in beat_modules:
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                decorators = {dotted_name(item) for item in node.decorator_list}
                is_celery_task = any(
                    name.endswith(".task") or name == "shared_task" for name in decorators
                )
                is_tracked = any(
                    name.endswith("tracked_job") or name == "tracked_job" for name in decorators
                )
                if is_celery_task and not is_tracked:
                    fail(path, f"beat任务 {node.name} 缺少 @tracked_job", node.lineno)

frontend = ROOT / "frontend" / "src"
if frontend.exists():
    for path in frontend.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {
            ".vue",
            ".ts",
            ".js",
            ".css",
            ".scss",
            ".html",
        }:
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"https?://(?:fonts\.|[^/]*cdn\.)", text, flags=re.IGNORECASE):
            fail(path, "前端运行时禁止外部字体/CDN")
        if "Math.ceil(" in text and ("/67" in text or "70" in text):
            fail(path, "前端禁止重复实现计费公式，必须调用 /billing/preview")

schema_path = ROOT / "schema.sql"
schema = schema_path.read_text(encoding="utf-8")
for match in re.finditer(r"CREATE TABLE\s+(\w+)\s*\((.*?)\n\);", schema, flags=re.DOTALL):
    table, body = match.groups()
    body_without_comments = re.sub(r"--[^\n]*", "", body)
    body_without_literals = re.sub(r"'(?:''|[^'])*'", "''", body_without_comments)
    phone_tokens = {
        token
        for token in ("phone_enc", "phone_hmac", "phone_mask", "key_version")
        if re.search(rf"\b{token}\b", body_without_literals)
    }
    if table == "usage_frequency_alias":
        if phone_tokens != {"phone_hmac", "key_version"}:
            fail(schema_path, "频控 HMAC alias 只能保存不可逆索引与版本")
        continue
    if phone_tokens and phone_tokens != {"phone_enc", "phone_hmac", "phone_mask", "key_version"}:
        fail(schema_path, f"表 {table} 的手机号四列不完整: {sorted(phone_tokens)}")

if re.search(r"raw_vendor_log\s*\([^;]*\bpayload\s+JSONB", schema, flags=re.DOTALL):
    fail(schema_path, "raw_vendor_log 禁止明文 JSONB payload")
if re.search(r"callback_task\s*\([^;]*\bpayload(?:_ref)?\s+JSONB", schema, flags=re.DOTALL):
    fail(schema_path, "callback_task 只能保存消息引用，不得保存 JSONB body")
if "ck_audit_payload_no_pii" not in schema:
    fail(schema_path, "audit_log 缺少数据库级 PII 载荷约束")
for token in ("phone_hmac", "phone_enc", "phone_list", "mobile_list"):
    if token not in schema.split("ck_audit_payload_no_pii", 1)[1]:
        fail(schema_path, f"audit_log PII 约束未覆盖 {token}")

if errors:
    for error in errors:
        print(f"FAIL: {error}", file=sys.stderr)
    print(f"硬规则静态检查失败: {len(errors)} 项", file=sys.stderr)
    raise SystemExit(1)

scope = "backend/frontend尚未创建，仅校验schema" if not APP.exists() else "backend/frontend/schema"
print(f"硬规则静态检查通过: {scope}；厂商调用边界已锁定")
