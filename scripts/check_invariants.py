#!/usr/bin/env python3
"""对可静态判定的工程硬规则做失败即退出检查。"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

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


def require_fragments(path: Path, *fragments: str) -> str:
    """要求关键安全边界继续保留，返回源文本供顺序检查。"""

    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        fail(path, "真实厂商受控联调关键文件缺失")
        return ""
    for fragment in fragments:
        if fragment not in source:
            fail(path, f"真实厂商受控联调不变量缺失: {fragment}")
    return source


def literal_integer(path: Path, name: str) -> int | None:
    """读取模块级整数常量；禁止动态或布尔值伪装。"""

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        fail(path, f"无法读取联调上限定义: {type(exc).__name__}")
        return None
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            if isinstance(value, ast.Constant) and type(value.value) is int:
                return value.value
            fail(path, f"{name} 必须是模块级整数常量")
            return None
    fail(path, f"缺少联调上限常量 {name}")
    return None


def check_vendor_live_invariants() -> None:
    """静态锁定真实厂商受控联调的不可绕过边界。"""

    settings = require_fragments(
        APP / "settings.py",
        'vendor_live_test_origin: str = "https://vendor.example.invalid"',
        'self.environment == "development"',
        "and self.auth_mock",
        "and not self.vendor_mock",
        "vendor live test requires the configured exact HTTPS origin",
    )
    if "vendor_secret_name:" in settings or "vendor_secret_key:" in settings:
        fail(APP / "settings.py", "厂商凭据不得成为环境变量值字段")

    for relative in ("api/messages.py", "api/web_messages.py"):
        normal_send = require_fragments(
            APP / relative,
            "settings.vendor_live_test",
            "vendor_test_console_only=settings.vendor_live_test",
        )
        if relative == "api/messages.py" and "VENDOR_TEST_CONSOLE_ONLY" not in normal_send:
            fail(APP / relative, "live-test 必须阻断普通发送入口")
    require_fragments(
        APP / "cli.py",
        "BACKEND_RUNTIME_GID = 10001",
        "destination_mode=0o640",
        "destination_group_id=BACKEND_RUNTIME_GID",
    )
    require_fragments(
        APP / "services/vendor_control_state.py",
        "state.active_recipient_count < 1",
        'state.mode != "controlled"',
        "not state.credential_configured",
    )
    require_fragments(
        APP / "tasks/send_repository.py",
        "self.crypto.hmac_candidates(phone)",
        "vendor_test_recipient",
        "denied_recipient_count=denied_recipient_count",
    )

    pipeline = require_fragments(
        APP / "services/pipeline.py",
        "require_allowed(request.mobiles)",
        "await run_bounded(",
    )
    if pipeline and pipeline.index("require_allowed(request.mobiles)") > pipeline.index(
        "await run_bounded("
    ):
        fail(APP / "services/pipeline.py", "白名单必须在手机号持久化准备前检查")

    worker = require_fragments(
        APP / "tasks/send.py",
        "_guard_chunk(chunk)",
        "_token(lane)",
        "claim_submission(",
        "self.gateway.send(",
        "enforce_live_test_budget=settings.vendor_live_test",
        "enforce_live_test_recipients=settings.vendor_live_test",
    )
    if worker and not (
        worker.index("_guard_chunk(chunk)")
        < worker.index("_token(lane)")
        < worker.index("self.gateway.send(")
    ):
        fail(APP / "tasks/send.py", "worker 必须先复验白名单和额度再调用厂商")

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
    require_fragments(APP / "tasks/send.py", "from app.services.billing import")

    backend_limit = literal_integer(
        APP / "services/vendor_test_budget.py", "LIVE_TEST_DAILY_SEGMENT_LIMIT"
    )
    deploy_limit = literal_integer(
        ROOT / "deploy/scripts/vendor_test_files.py", "DAILY_SEGMENT_LIMIT"
    )
    if backend_limit != 100 or deploy_limit != backend_limit:
        fail(
            APP / "services/vendor_test_budget.py",
            "live-test 每日上限必须由一致的 100 计费条合同约束",
        )
    require_fragments(
        APP / "tasks/send_repository.py",
        "LIVE_TEST_DAILY_SEGMENT_LIMIT",
        "in_flight_segments",
        "confirmed_segments",
        "uncertain_segments",
    )

    require_fragments(
        APP / "vendor/codes.py",
        '1010: _policy("IP 校验未通过", alert_level="crit")',
        "pause_queues=True",
        'alert_level="crit"',
    )

    files_source = require_fragments(
        ROOT / "deploy/scripts/vendor_test_files.py",
        '_ALLOWLIST_FIELDS = frozenset({"schema_version", "entries"})',
        '_ALLOWLIST_ENTRY_FIELDS = frozenset({"key_version", "phone_hmac"})',
        '"SMS_VENDOR_TEST_STATE_DIR": "/var/lib/sms-platform/vendor-test"',
        '"SMS_VENDOR_CONTROL_SOCKET_DIR": "/run/sms-platform/vendor-control"',
        "_FORBIDDEN_EVIDENCE_KEY",
        "phone|mobile|hmac|secret|password|credential|content|digest|hash|token",
    )
    if "phone_enc" in files_source or "phone_mask" in files_source:
        fail(ROOT / "deploy/scripts/vendor_test_files.py", "allowlist 不得保存可解密手机号")

    schema_source = require_fragments(
        ROOT / "schema.sql",
        "CREATE TABLE vendor_test_daily_usage",
        "CREATE TABLE vendor_test_send_attempt",
        "CREATE TABLE vendor_test_recipient_hmac_alias",
        "PRIMARY KEY (recipient_id, hmac_key_version)",
        "UNIQUE (hmac_key_version, hmac_digest)",
    )
    for table in ("vendor_test_daily_usage", "vendor_test_send_attempt"):
        table_body = schema_source.split(f"CREATE TABLE {table}", maxsplit=1)[-1].split(
            ";", maxsplit=1
        )[0]
        for forbidden_field in (
            "phone_enc",
            "phone_hmac",
            "phone_mask",
            "mobile",
            "content",
            "secret",
        ):
            if forbidden_field in table_body.lower():
                fail(
                    ROOT / "schema.sql",
                    f"{table} 禁止控制面 PII/凭据字段 {forbidden_field}",
                )

    operation_table = schema_source.split("CREATE TABLE vendor_test_operation", maxsplit=1)[
        -1
    ].split(");", maxsplit=1)[0]
    for forbidden_field in (
        "phone",
        "mobile",
        "content",
        "secret",
        "payload",
        "request_body",
    ):
        if forbidden_field in operation_table.lower():
            fail(
                ROOT / "schema.sql",
                f"vendor_test_operation 禁止敏感载荷字段 {forbidden_field}",
            )

    compose = require_fragments(
        ROOT / "deploy/docker-compose.yml",
        "mock-vendor:",
        "profiles: [dev]",
    )
    manager = require_fragments(
        ROOT / "deploy/scripts/vendor_test_manager.py",
        'self._run("rm", "-s", "-f", "mock-vendor")',
        "client.get_balance()",
        "except VendorApiError as error:",
        'print(json.dumps({"code": error.code}))',
        "except VendorTestProbeRejected as error:",
        "FROM vendor_test_daily_usage",
        "_BACKEND_RUNTIME_GID = 10001",
        "expected_mode=0o710",
        "class VendorTestRotationManager:",
        "class VendorTestRotationRecoveryManager:",
        "self.operations.create_encrypted_checkpoint()",
        "self.credentials.begin_rotation()",
        "self.credentials.commit_rotation(transaction)",
        "self.credentials.rollback_to_previous(transaction)",
        "self.credentials.complete_rollback(rolling_back)",
        "self.operations.prepare_runtime_secrets()",
        "self.operations.rebuild_backend()",
        "reconcile_pure_mock_dotenv(",
        "queue:paused:vendor-test-rotation-failed:",
        "current_pause_kind()",
        "active_status_counts()",
        "_require_zero_active_counts",
    )
    for consuming_probe in ("client.get_report(", "client.get_reply("):
        if consuming_probe in manager:
            fail(
                ROOT / "deploy/scripts/vendor_test_manager.py",
                "预检只允许 GetBalance，禁止 GetReport/GetReply",
            )
    if compose.count("profiles: [dev]") < 1 or manager.count("client.get_balance()") < 1:
        fail(ROOT / "deploy/docker-compose.yml", "live-test 必须排除 mock-vendor")
    if "docker.sock" in compose:
        fail(ROOT / "deploy/docker-compose.yml", "API/worker 禁止挂载宿主 Docker Socket")
    require_fragments(
        ROOT / "deploy/docker-compose.yml",
        "${SMS_VENDOR_CONTROL_SOCKET_DIR:-./vendor-control-empty}:/run/vendor-control:ro",
    )
    if re.search(
        r"^\s*-\s+.*?/run/sms-platform/secrets",
        compose,
        flags=re.MULTILINE,
    ):
        fail(ROOT / "deploy/docker-compose.yml", "API/worker 禁止挂载宿主运行密钥目录")

    require_fragments(
        APP / "api/vendor_test.py",
        'prefix="/api/v1/web/admin/vendor-test"',
        "CredentialEnvelopeModel",
        "recipient_id: int",
        '"/messages/preview"',
        'response.headers["Cache-Control"] = "no-store"',
    )
    for relative in (
        "api/vendor_test.py",
        "services/vendor_control_client.py",
        "services/vendor_test_uat.py",
    ):
        source = require_fragments(APP / relative)
        for forbidden_import in ("import httpx", "from httpx", "import requests", "aiohttp"):
            if forbidden_import in source:
                fail(APP / relative, "真实联调控制面不得绕过 vendor/zhihui.py 直连 HTTP")

    protocol = require_fragments(
        ROOT / "deploy/scripts/vendor_control_protocol.py",
        'REQUEST_FIELDS = {"schema_version", "operation_id", "operation", "body"}',
        "if fields != _CREDENTIAL_FIELDS",
        'if fields != {"pause_kind"}',
    )
    agent = require_fragments(
        ROOT / "deploy/scripts/vendor_control_agent.py",
        '[WRAPPER, "vendor-test", operation]',
        "shell=False",
        "_synchronize_state",
        "self.write_heartbeat()",
        "self.credential_store.stage(candidate)",
        "self.credential_store.discard_pending()",
        'self.runner.run("rotate")',
        'self.runner.run("recover-rotation")',
        "只执行固定 wrapper 三元组，不接受 argv/path/env",
    )
    for forbidden_field in ('"path"', '"argv"', '"env"'):
        if forbidden_field in protocol:
            fail(ROOT / "deploy/scripts/vendor_control_protocol.py", "agent 协议禁止自由进程字段")
    if "shell=True" in agent:
        fail(ROOT / "deploy/scripts/vendor_control_agent.py", "控制代理禁止 shell 执行")
    fixed_runner = agent.split("class FixedWrapperRunner:", maxsplit=1)[-1].split(
        "def secure_socket(", maxsplit=1
    )[0]
    if "timeout=180" not in fixed_runner:
        fail(
            ROOT / "deploy/scripts/vendor_control_agent.py",
            "控制代理 wrapper 必须 timeout=180，超时视为失败",
        )

    credential_store = require_fragments(
        ROOT / "deploy/scripts/vendor_credential_store.py",
        "def stage(",
        "def begin_rotation(",
        "def commit_rotation(",
        "def rollback_to_previous(",
        "def complete_rollback(",
        "def read_rotation_transaction(",
        "def activate_pending(",
        "def discard_pending(",
        "def recover_pending(",
        '"rotation-state.json"',
    )
    if "secret" not in credential_store:
        fail(ROOT / "deploy/scripts/vendor_credential_store.py", "凭据 generation 合同缺失")

    require_fragments(
        ROOT / "deploy/scripts/vendor_seal_sessions.py",
        "def seal_aad(",
        '"operation": operation',
        '"actor": _context(actor)',
        '"expires_at": expires_at.astimezone(UTC).isoformat()',
        "if (operation, actor) != (session[2], session[3])",
    )

    uat_service = require_fragments(
        APP / "services/vendor_test_uat.py",
        "protected_mobiles=(",
        "ProtectedPhone(",
        "protected_hmac_candidates=recipient.hmac_candidates",
        "await self.config_store.load_config(app.dept)",
    )
    if "decrypt_text(" in uat_service:
        fail(APP / "services/vendor_test_uat.py", "UAT API 服务禁止解密测试手机号")

    recipient_service = require_fragments(
        APP / "services/vendor_test_recipient.py",
        "class RecipientHmacIndexStale",
        "set(candidates) != self.crypto.hmac_versions",
        "async def refresh_hmac_index(",
    )
    if "decrypt_text(" in recipient_service:
        fail(APP / "services/vendor_test_recipient.py", "HMAC 索引刷新禁止解密历史号码")
    require_fragments(
        APP / "services/pipeline.py",
        "protected_hmac_candidates",
        "frequency_hmac_by_active",
        "aliases[min(aliases)]",
    )
    require_fragments(
        ROOT / "deploy/scripts/vendor_control_journal.py",
        '_CHECKPOINT_OPERATIONS = frozenset({"activate", "rotate_credentials"})',
    )

    uat_panel = require_fragments(
        ROOT / "frontend/src/components/VendorTestUatPanel.vue",
        "previewVendorTestUat",
        "app_id: appId.value!",
    )
    if "previewBilling" in uat_panel:
        fail(
            ROOT / "frontend/src/components/VendorTestUatPanel.vue",
            "真实 UAT 预览不得绕过所选应用配置",
        )

    openapi = require_fragments(
        ROOT / "openapi.yaml",
        "CredentialEnvelopeModel:",
        "UatMessageRequestModel:",
        "VendorTestOperationModel:",
    )
    credential_schema = openapi.split("    CredentialEnvelopeModel:\n", maxsplit=1)[-1].split(
        "\n    VendorTestOperationModel:\n", maxsplit=1
    )[0]
    for forbidden_field in ("secret_name", "secret_key", "secretName", "secretKey"):
        if forbidden_field in credential_schema:
            fail(ROOT / "openapi.yaml", "凭据接口禁止接收明文 SecretName/SecretKey")

    audit_repository = require_fragments(
        APP / "services/vendor_test_operation_repository.py",
        '"operation_id": record.operation_id',
        'payload["vendor_code"] = record.vendor_code',
    )
    audit_section = audit_repository.split("    async def _audit(", maxsplit=1)[-1]
    for forbidden_audit_key in (
        'payload["phone',
        'payload["mobile',
        'payload["content',
        'payload["secret',
        'payload["request',
    ):
        if forbidden_audit_key in audit_section.lower():
            fail(
                APP / "services/vendor_test_operation_repository.py",
                "真实联调 operation 审计禁止敏感载荷",
            )

    require_fragments(
        ROOT / "deploy/scripts/test_update_manager.py",
        "FROM vendor_test_daily_usage",
    )

    secure_contract_path = ROOT / "deploy/scripts/test_secure_access_contract.py"
    require_fragments(
        secure_contract_path,
        'CLOUDFLARED_VERSION = "2026.7.2"',
        "ec905ea7b7e327ff8abdde8cb64697a2152de74dbcdbf6aec9db8364eb3886cd",
        'ORIGIN = "http://127.0.0.1:18080"',
        "MAX_LIFETIME_SECONDS = 900",
        "trycloudflare",
    )
    if literal_integer(secure_contract_path, "MAX_LIFETIME_SECONDS") != 900:
        fail(secure_contract_path, "临时 HTTPS 入口硬时限必须为 900 秒")
    secure_runtime = require_fragments(
        ROOT / "deploy/scripts/test_secure_access_runtime.py",
        '"--no-autoupdate"',
        '"--protocol"',
        '"http2"',
        "write_ready_state",
        "subprocess.DEVNULL",
    )
    if "shell=True" in secure_runtime:
        fail(
            ROOT / "deploy/scripts/test_secure_access_runtime.py",
            "临时 HTTPS 运行器禁止 shell 执行",
        )
    secure_manager = require_fragments(
        ROOT / "deploy/scripts/test_secure_access_manager.py",
        '{"start", "status", "stop", "verify-assets"}',
        'else {"start", "status", "stop"}',
        "SMS_SECURE_ACCESS_INTERNAL",
        'action == "verify-assets"',
        '"systemctl"',
        "CLOUDFLARED_SHA256",
        "sms-platform.service",
    )
    if "shell=True" in secure_manager:
        fail(
            ROOT / "deploy/scripts/test_secure_access_manager.py",
            "临时 HTTPS 管理器禁止 shell 执行",
        )
    secure_installer = require_fragments(
        ROOT / "deploy/scripts/install_test_secure_access.py",
        "CLOUDFLARED_SHA256",
        "CLOUDFLARED_VERSION",
        '"disable"',
        '"--now"',
        '"systemd-analyze"',
        '"verify"',
    )
    for forbidden_installer_action in ('"start"', '"enable"'):
        if forbidden_installer_action in secure_installer:
            fail(
                ROOT / "deploy/scripts/install_test_secure_access.py",
                "一次性临时 HTTPS 安装器禁止启动或启用 unit",
            )
    secure_unit = require_fragments(
        ROOT / "deploy/systemd/sms-platform-test-secure-access.service",
        "DynamicUser=yes",
        "RuntimeMaxSec=15min",
        "Restart=no",
        "NoNewPrivileges=yes",
        "CapabilityBoundingSet=",
        "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX",
    )
    for forbidden_unit_fragment in ("[Install]", "WantedBy=", "EnvironmentFile="):
        if forbidden_unit_fragment in secure_unit:
            fail(
                ROOT / "deploy/systemd/sms-platform-test-secure-access.service",
                "临时 HTTPS unit 必须保持 static 且无密钥环境",
            )
    cloudflare_manager = require_fragments(
        ROOT / "deploy/scripts/cloudflare_tunnel_manager.py",
        "install-token",
        "getpass.getpass",
        '"enable"',
        '"--now"',
        "run_probe",
        "127.0.0.1",
        "CLOUDFLARED_SHA256",
    )
    for forbidden_manager_fragment in (
        "shell=True",
        "TUNNEL_TOKEN",
        '--token"',
    ):
        if forbidden_manager_fragment in cloudflare_manager:
            fail(
                ROOT / "deploy/scripts/cloudflare_tunnel_manager.py",
                "持久 Tunnel 管理器禁止通过命令行或环境传递 token",
            )
    cloudflare_unit = require_fragments(
        ROOT / "deploy/systemd/sms-platform-cloudflare-tunnel.service",
        "DynamicUser=yes",
        "LoadCredential=tunnel-token:/etc/sms-platform/cloudflare-tunnel-token",
        "run --token-file %d/tunnel-token",
        "Restart=on-failure",
        "NoNewPrivileges=yes",
        "CapabilityBoundingSet=",
        "WantedBy=multi-user.target",
    )
    for forbidden_tunnel_unit_fragment in ("EnvironmentFile=", "TUNNEL_TOKEN="):
        if forbidden_tunnel_unit_fragment in cloudflare_unit:
            fail(
                ROOT / "deploy/systemd/sms-platform-cloudflare-tunnel.service",
                "持久 Tunnel unit 禁止把 token 放入环境",
            )
    vendor_seal = require_fragments(
        ROOT / "frontend/src/lib/vendorSeal.ts",
        "globalThis.isSecureContext === true",
        "globalThis.crypto.subtle",
        "VENDOR_CREDENTIAL_SECURE_CONTEXT_ERROR",
    )
    seal_function = vendor_seal.split("export async function sealVendorCredentials", maxsplit=1)[-1]
    secure_context_position = seal_function.find("isVendorCredentialSecureContext()")
    credential_position = seal_function.find("credentials.secretName")
    if (
        secure_context_position < 0
        or credential_position < 0
        or secure_context_position > credential_position
    ):
        fail(
            ROOT / "frontend/src/lib/vendorSeal.ts",
            "WebCrypto 安全上下文检查必须先于凭据读取",
        )
    require_fragments(
        ROOT / "frontend/src/components/VendorCredentialDialog.vue",
        "isVendorCredentialSecureContext",
        "当前入口不支持正式凭据安全加密。",
        "打开正式凭据安全入口",
        ':disabled="submitting || !secureContextAvailable"',
    )

    update_contract = require_fragments(
        ROOT / "deploy/scripts/test_update_contract.py",
        "_HIGH_RISK_EXACT",
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
                ROOT / "deploy/scripts/test_update_contract.py",
                f"普通快速更新未阻断高风险路径 {high_risk_path}",
            )

    wrapper = require_fragments(
        ROOT / "deploy/sms-compose",
        "validate_production_launch()",
        '"$seen_debug" != 1 || "$env_debug" != 0',
        '"$seen_auth_mock" != 1 || "$env_auth_mock" != 0',
        '"$seen_vendor_mock" != 1 || "$env_vendor_mock" != 0',
        "reject_production_control_plane test-update",
        "prepare | apply | verify",
        "dispatch_secure_access()",
        "reject_production_control_plane secure-access",
        "start | status | stop",
        "dispatch_cloudflare_tunnel()",
        "reject_production_control_plane cloudflare-tunnel",
        "install-token | start | status | verify | stop",
    )
    update_dispatch = wrapper.split("dispatch_test_update()", maxsplit=1)[-1].split(
        "run_locked_operation()", maxsplit=1
    )[0]
    for forbidden in ("restore", "rollback", "mock-vendor", "reset --hard", "down -v"):
        if forbidden in update_dispatch:
            fail(ROOT / "deploy/sms-compose", f"快速更新包装器禁止 {forbidden}")
    secure_dispatch = wrapper.split("dispatch_secure_access()", maxsplit=1)[-1].split(
        "dispatch_cloudflare_tunnel()", maxsplit=1
    )[0]
    for forbidden in (
        "prepare_runtime_secrets",
        "run_with_lifecycle_lock",
        "credentials",
        "recipient",
        "activate",
    ):
        if forbidden in secure_dispatch:
            fail(ROOT / "deploy/sms-compose", f"临时 HTTPS 包装器禁止 {forbidden}")
    cloudflare_dispatch = wrapper.split("dispatch_cloudflare_tunnel()", maxsplit=1)[-1].split(
        "run_locked_operation()", maxsplit=1
    )[0]
    for forbidden in (
        "prepare_runtime_secrets",
        "run_with_lifecycle_lock",
        "--token ",
        "TUNNEL_TOKEN",
    ):
        if forbidden in cloudflare_dispatch:
            fail(ROOT / "deploy/sms-compose", f"持久 Tunnel 包装器禁止 {forbidden}")

    require_fragments(
        ROOT / "scripts/classify_ci_changes.py",
        "protected_change_category",
        'VENDOR_LIVE: RuleResult = (True, True, True, True, "vendor-live")',
        'FRONTEND_SECURITY: RuleResult = (False, True, False, True, "frontend-security")',
        "--name-status",
    )
    require_fragments(
        ROOT / "deploy/scripts/test_update_contract.py",
        "_VENDOR_LIVE_PROTECTED_EXACT",
        "security_domain_category",
        "def protected_change_category(",
    )
    require_fragments(
        ROOT / "deploy/scripts/protected_path_policy.py",
        "BACKEND_CRITICAL_DOMAINS",
        "FRONTEND_SECURITY_DOMAINS",
        "REVIEWED_ORDINARY_EXACT",
        "def security_domain_category(",
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
        "deploy/scripts/test_update_apply.py",
        "deploy/scripts/test_update_manager.py",
        "deploy/scripts/vendor_test_manager.py",
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
    )
    require_fragments(
        APP / "services/pipeline.py",
        "UsageLedgerPort",
        "usage_reservation_id=usage_reservation_id",
        "hmac_aliases=frequency_aliases_by_active",
        'await release_usage("idempotent-reuse")',
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
        "recover_orphans()",
        "measure_drift()",
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


def check_protected_path_policy_invariants() -> None:
    """安全域 manifest 必须是分类与 CODEOWNERS 的同一来源。"""

    import subprocess

    sys.path.insert(0, str(ROOT / "deploy" / "scripts"))
    from protected_path_policy import (  # noqa: E402
        REQUIRED_CODEOWNERS_PATTERNS,
        REVIEWED_ORDINARY_EXACT,
        unclassified_security_domain_paths,
    )

    codeowners = require_fragments(ROOT / ".github" / "CODEOWNERS", *REQUIRED_CODEOWNERS_PATTERNS)
    for pattern in REQUIRED_CODEOWNERS_PATTERNS:
        if not re.search(rf"^{re.escape(pattern)}\s+@neuer\s*$", codeowners, flags=re.MULTILINE):
            fail(ROOT / ".github" / "CODEOWNERS", f"缺少安全域 CODEOWNERS 规则: {pattern}")
    contract = (ROOT / "deploy" / "scripts" / "test_update_contract.py").read_text(
        encoding="utf-8"
    )
    if "_BACKEND_CRITICAL_PROTECTED_EXACT" in contract:
        fail(
            ROOT / "deploy" / "scripts" / "test_update_contract.py",
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
    missing = unclassified_security_domain_paths(tracked)
    if missing:
        fail(
            ROOT / "deploy" / "scripts" / "protected_path_policy.py",
            f"安全域存在未归类路径: {missing[:8]}",
        )
    for relative in REVIEWED_ORDINARY_EXACT:
        if relative not in tracked:
            fail(
                ROOT / "deploy" / "scripts" / "protected_path_policy.py",
                f"显式降级路径已不存在: {relative}",
            )


check_vendor_live_invariants()
check_protected_path_policy_invariants()
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
print(f"硬规则静态检查通过: {scope}；真实厂商受控联调边界已锁定")
