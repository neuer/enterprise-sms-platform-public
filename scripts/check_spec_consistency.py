#!/usr/bin/env python3
"""检查维护期活动契约与可静态判定的安全边界；只依赖 Python 标准库。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


errors: list[str] = []


def require(condition: bool, message: str) -> None:
    if not condition:
        errors.append(message)


required_files = [
    ".gitignore",
    "AGENTS.md",
    "CLAUDE.md",
    "MAINTENANCE.md",
    "PRD.md",
    "PROGRESS.md",
    "docs/DECISIONS.md",
    "docs/TRACEABILITY.md",
    "docs/UAT.md",
    "docs/runbooks/usage-ledger-recovery.md",
    "docs/runbooks/api-key-pepper-upgrade.md",
    "docs/ui-design.md",
    "docs/sms-ui-prototype.html",
    "docs/vendor-api.md",
    "frontend/package.json",
    "deploy/.env.example",
    "deploy/docker-compose.yml",
    "deploy/dba.md",
    "deploy/initdb/01-create-app-role.sh",
    "openapi.yaml",
    "schema.sql",
    "scripts/verify_all.sh",
    "scripts/check_invariants.py",
    "scripts/check_contract.py",
]
for item in required_files:
    require((ROOT / item).is_file(), f"缺少必需文件: {item}")

schema = read("schema.sql")
uat = read("docs/UAT.md")
prototype = read("docs/sms-ui-prototype.html")
maintenance = read("MAINTENANCE.md")
progress = read("PROGRESS.md")
env_example = read("deploy/.env.example")
compose = read("deploy/docker-compose.yml")
openapi = read("openapi.yaml")
frontend_package = json.loads(read("frontend/package.json"))
frontend_scripts = frontend_package.get("scripts", {})

require(
    "# PROGRESS.md — 活跃外部阻塞" in progress,
    "PROGRESS 必须只作为活跃外部阻塞清单",
)
progress_sections = re.split(r"(?=^## )", progress, flags=re.MULTILINE)[1:]
if progress_sections:
    for index, section in enumerate(progress_sections, start=1):
        for field in ("影响：", "失败关闭边界：", "解除条件："):
            require(field in section, f"PROGRESS 第 {index} 个阻塞缺少字段: {field}")
else:
    require(
        re.search(r"^当前无活跃外部阻塞。$", progress, flags=re.MULTILINE) is not None,
        "PROGRESS 无阻塞时必须保留明确空态",
    )
for stale_phrase in ("建设里程碑", "最近公开基线", "当前维护重点", "下一步："):
    require(stale_phrase not in progress, f"PROGRESS 仍包含状态流水账: {stale_phrase}")
for dynamic_pattern in (
    r"\bPR\s*#\d+\b",
    r"\b[0-9a-f]{7,40}\b",
    r"github\.com/[^ \n]+/actions/runs/\d+",
    r"\brun ID\b",
):
    require(
        re.search(dynamic_pattern, progress, flags=re.IGNORECASE) is None,
        f"PROGRESS 不得保存动态 PR/CI 证据: {dynamic_pattern}",
    )
require(
    "只登记需要仓库外状态变化或操作者协调才能解除的活跃阻塞" in maintenance,
    "MAINTENANCE 必须声明 PROGRESS 的外部阻塞边界",
)
require(
    "PR、CI 和已完成状态不回填" in maintenance
    and "PR 与 CI 事实以 GitHub 为准" in maintenance
    and "发布不可变证据以生产变更单" in maintenance,
    "MAINTENANCE 必须声明动态交付证据的事实源",
)

require(
    frontend_scripts.get("build") == "npm run typecheck && npm run build:g2",
    "frontend build 必须先执行 typecheck，避免各入口重复调用或遗漏类型检查",
)
require(
    frontend_scripts.get("typecheck") == "vue-tsc --noEmit",
    "frontend typecheck 必须执行 vue-tsc --noEmit",
)
require(
    frontend_scripts.get("build:g2") == "vite build",
    "frontend build:g2 必须执行 Vite 生产构建",
)

uat_cases = re.findall(r"^\|\s*(\d{2})\s*\|", uat, flags=re.MULTILINE)
require(len(uat_cases) == len(set(uat_cases)), "UAT 用例编号不得重复")
required_uat_cases = {f"{case_id:02d}" for case_id in range(1, 29)}
require(
    required_uat_cases.issubset(uat_cases),
    "UAT 必须保留核心用例 01-28；允许在其后扩展新用例",
)

require("UI BASELINE" in prototype, "UI 原型仍不是正式 baseline")
require("idempotency_record" in schema, "schema 缺少可过期 idempotency_record")
require("uk_batch_app_biz" not in schema, "schema 仍含永久 biz_id 唯一索引")
require("CREATE TABLE usage_reservation" in schema, "schema 缺少配额/频控事实预留表")
require("CREATE TABLE usage_projection" in schema, "schema 缺少可重建用量投影表")
require(
    "CREATE TABLE password_change_token" in schema,
    "schema 缺少首次改密令牌事务状态表",
)
require(
    "USAGE_PROJECTION_UNAVAILABLE" in openapi,
    "OpenAPI 缺少用量投影失败关闭错误码",
)

require(
    "payload_enc" in schema and "custom_ids" in schema, "raw_vendor_log 未使用加密载荷+安全索引"
)
require("message_ids" in schema and "message_times" in schema, "callback_task 未改为无PII消息引用")
require("CREATE TABLE import_phone" in schema, "schema 缺少 import_phone 三列存储")
require(
    "payload     JSONB" not in schema
    and "phones_enc" not in schema
    and "payload_ref" not in schema,
    "schema 仍含旧明文/整体号码包字段",
)

for token in ("AUTH_MOCK=1", "DEBUG=1", "ENVIRONMENT=development"):
    require(token in env_example, f".env.example 缺少 {token}")
for token in (
    "migrate:",
    "db-role-provision:",
    "audit_context_key",
    "audit_system_api_context_key",
    "audit_system_realtime_context_key",
    "audit_system_bulk_context_key",
    "alert_credential_public_key",
    "alert_credential_private_key",
    "db_owner_password",
    "db_auth_password",
    "db_accept_password",
    "db_send_password",
    "db_callback_password",
    "db_export_password",
    "db_scheduler_password",
    "db_metrics_password",
    "metrics_scrape_token",
    "DB_RUNTIME_ROLE:",
):
    require(token in compose, f"Compose 缺少 {token}")

required_paths = [
    "/api/v1/messages/batches/{batch_no}/reschedule",
    "/api/v1/web/billing/preview",
    "/api/v1/web/messages/{id}/phone/decrypt",
    "/api/v1/web/templates/{id}",
    "/api/v1/web/signs/{id}/sync",
    "/api/v1/web/signs/{id}/adopt-existing",
    "/api/v1/web/replies/{id}/blacklist",
    "/api/v1/web/admin/apps/{id}",
    "/api/v1/web/admin/unmatched-reports/export",
    "/api/v1/web/reports/export/{public_id}",
    "/api/v1/web/reports/export/{public_id}/step-up",
    "/api/v1/web/reports/export/{public_id}/download",
    "/api/v1/web/auth/refresh",
]
for path in required_paths:
    require(f"  {path}:" in openapi, f"OpenAPI 缺少关键路径: {path}")
require(
    "required: [category, mobiles, content]" not in openapi, "应用发送仍把 content 错误设为固定必填"
)
require("required: [category, content]" not in openapi, "Web发送仍把 content 错误设为固定必填")

path_names = re.findall(r"^  (/api/[^:]+):$", openapi, flags=re.MULTILINE)
require(len(path_names) == len(set(path_names)), "OpenAPI 存在重复 path")

require(
    "segsOf(" not in prototype and "Math.ceil(L/67)" not in prototype, "UI 原型仍重复实现计费公式"
)
require("人工判定" not in prototype, "UI 原型仍允许人工迁移 uncertain")
require("http://cdn" not in prototype and "https://cdn" not in prototype, "UI 原型引用外部 CDN")

for script in (
    "scripts/verify_all.sh",
    "deploy/initdb/01-create-app-role.sh",
):
    require(os.access(ROOT / script, os.X_OK), f"脚本不可执行: {script}")

api_docs = read("docs/api-integration.md")
api_test_docs = read("docs/api-test-playground.md")
require(
    re.search(
        r"/api/v1/messages/send[\s\S]{0,2500}required:\s*\[category,\s*mobiles,\s*biz_id\]",
        openapi,
    )
    is not None,
    "OpenAPI send 必须把 biz_id 列为 required",
)
require(
    "| `biz_id` | string | 是 |" in api_docs,
    "docs/api-integration.md 字段表必须把 biz_id 标为必填",
)
require(
    "缺少 `biz_id`" in api_docs
    and "400 `INVALID_PARAM`" in api_docs
    and "IDEMPOTENCY_CONFLICT" in api_docs,
    "docs/api-integration.md 必须给出 biz_id 生成规则与错误示例",
)
require(
    "biz_id` **必填**" in api_test_docs or "biz_id **必填**" in api_test_docs,
    "docs/api-test-playground.md 必须声明 biz_id 必填",
)
playground = read("frontend/public/api-test.html")
require(
    "biz_id（必填" in playground and "biz_id: bizId" in playground,
    "api-test.html 必须校验并提交必填 biz_id",
)
require(
    "completed_unknown" in openapi and "completed_unknown" in api_docs,
    "OpenAPI 与集成文档必须包含 completed_unknown",
)

BATCH_STATUSES = {
    "pending_approval",
    "rejected",
    "scheduled",
    "queued",
    "sending",
    "completed",
    "completed_unknown",
    "cancelled",
    "balance_blocked",
    "expired",
}
schema_batch = set(
    re.findall(
        r"'((?:pending_approval|rejected|scheduled|queued|sending|completed_unknown|completed|cancelled|balance_blocked|expired))'",
        schema[schema.find("CREATE TABLE sms_batch") : schema.find("CREATE TABLE sms_chunk")],
    )
)
require(schema_batch >= BATCH_STATUSES, "schema.sql 批次状态缺少 completed_unknown 等正式枚举")
openapi_batch = set(re.findall(r"completed_unknown|pending_approval|balance_blocked", openapi))
require("completed_unknown" in openapi_batch, "OpenAPI 缺少 completed_unknown")
frontend_status = read("frontend/src/api/webMessages.ts")
require(
    "completed_unknown" in frontend_status,
    "前端 SendResult 必须包含 completed_unknown",
)

if (ROOT / ".git").exists():
    for ignored_path in (".env", "deploy/secrets/dev-apikeys.txt"):
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", ignored_path],
            cwd=ROOT,
            check=False,
        )
        require(ignored.returncode == 0, f"未被 Git 忽略: {ignored_path}")

if errors:
    for error in errors:
        print(f"FAIL: {error}", file=sys.stderr)
    print(f"规格一致性检查失败: {len(errors)} 项", file=sys.stderr)
    raise SystemExit(1)

print(
    f"规格一致性检查通过: {len(required_files)} 个必需文件, "
    f"{len(path_names)} 个API路径"
)
