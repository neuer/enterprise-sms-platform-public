#!/usr/bin/env python3
"""目标模式开工前的规格防漂移检查；只依赖 Python 标准库。"""

from __future__ import annotations

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
    "AUTOPILOT.md",
    "BOOTSTRAP.md",
    "CLAUDE.md",
    "PRD.md",
    "PROGRESS.md",
    "TASKS.md",
    "docs/DECISIONS.md",
    "docs/TRACEABILITY.md",
    "docs/UAT.md",
    "docs/runbooks/usage-ledger-recovery.md",
    "docs/ui-design.md",
    "docs/sms-ui-prototype.html",
    "docs/vendor-api.md",
    "deploy/.env.example",
    "deploy/docker-compose.yml",
    "deploy/dba.md",
    "deploy/initdb/01-create-app-role.sh",
    "openapi.yaml",
    "schema.sql",
    "scripts/verify_all.sh",
    "scripts/verify_milestone.sh",
    "scripts/check_invariants.py",
    "scripts/check_contract.py",
]
for item in required_files:
    require((ROOT / item).is_file(), f"缺少必需文件: {item}")

prd = read("PRD.md")
schema = read("schema.sql")
uat = read("docs/UAT.md")
prototype = read("docs/sms-ui-prototype.html")
tasks = read("TASKS.md")
agents = read("AGENTS.md")
claude = read("CLAUDE.md")
autopilot = read("AUTOPILOT.md")
progress = read("PROGRESS.md")
env_example = read("deploy/.env.example")
compose = read("deploy/docker-compose.yml")
openapi = read("openapi.yaml")

require("文档版本 | v1.6" in prd, "PRD 版本不是 v1.6")
require("-- v1.6.38  " in schema, "schema.sql 版本头不是 v1.6.38")
require("验收用例集 v1.6" in uat, "UAT 版本不是 v1.6")
require("UI 设计稿 v1.6" in prototype, "UI 原型版本不是 v1.6")
require("UI BASELINE" in prototype, "UI 原型仍不是正式 baseline")

uat_cases = re.findall(r"^\|\s*(\d{2})\s*\|", uat, flags=re.MULTILINE)
require(uat_cases == [f"{i:02d}" for i in range(1, 29)], "UAT 必须连续且仅包含 01-28")

require("idem:{app_id}:{biz_id}" in agents, "AGENTS 缺少规范 idem Redis 键")
require("idempotency_record" in schema, "schema 缺少可过期 idempotency_record")
require("uk_batch_app_biz" not in schema, "schema 仍含永久 biz_id 唯一索引")
require("SETNX biz:" not in tasks, "TASKS 仍使用错误的 biz: Redis 键")
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

positions = {
    name: tasks.find(name)
    for name in ("T0.0", "T0.1", "T0.6", "T4.6", "T4.10", "T4.11", "T4.12", "T4.13")
}
require(all(value >= 0 for value in positions.values()), "TASKS 缺少关键任务编号")
require(
    positions["T0.0"] < positions["T0.1"] < positions["T0.6"],
    "M0 任务依赖顺序错误",
)
require(
    positions["T4.6"]
    < positions["T4.10"]
    < positions["T4.11"]
    < positions["T4.12"]
    < positions["T4.13"],
    "T4.13 必须位于全部功能与验收任务之后",
)

required_paths = [
    "/api/v1/messages/batches/{batch_no}/reschedule",
    "/api/v1/web/billing/preview",
    "/api/v1/web/messages/{id}/phone/decrypt",
    "/api/v1/web/templates/{id}",
    "/api/v1/web/signs/{id}/sync",
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

require("全部 38 条" in autopilot, "AUTOPILOT 未引用全部 38 条硬规则")
next_task = re.search(r"^- \[ \] .*?(T\d+\.\w+)", tasks, flags=re.MULTILINE)
if next_task:
    require(
        next_task.group(1) in progress,
        f"PROGRESS 未指向 TASKS 中第一个未完成任务 {next_task.group(1)}",
    )
else:
    require("DONE" in progress, "TASKS 已全部完成但 PROGRESS 未标记 DONE")
stale_tokens = ["UAT 25例", "UAT 25 例", "secrets 六件套", "全部 36 条", "UI DRAFT 1"]
for token in stale_tokens:
    for path in (
        "AUTOPILOT.md",
        "BOOTSTRAP.md",
        "TASKS.md",
        "docs/UAT.md",
        "docs/sms-ui-prototype.html",
    ):
        require(token not in read(path), f"{path} 仍含过期口径: {token}")

require(
    "segsOf(" not in prototype and "Math.ceil(L/67)" not in prototype, "UI 原型仍重复实现计费公式"
)
require("人工判定" not in prototype, "UI 原型仍允许人工迁移 uncertain")
require("http://cdn" not in prototype and "https://cdn" not in prototype, "UI 原型引用外部 CDN")

# AGENTS/CLAUDE 除标题和第二行适用对象外必须完全一致。
require(
    agents.splitlines()[3:] == claude.splitlines()[3:], "AGENTS.md 与 CLAUDE.md 工程规则发生漂移"
)

for script in (
    "scripts/verify_all.sh",
    "scripts/verify_milestone.sh",
    "deploy/initdb/01-create-app-role.sh",
):
    require(os.access(ROOT / script, os.X_OK), f"脚本不可执行: {script}")

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
    f"{len(path_names)} 个API路径, 28个UAT用例"
)
