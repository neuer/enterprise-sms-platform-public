from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CANONICAL_COMMAND = "scripts/test_update.sh apply --ref origin/<branch>"


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_agents_contracts_the_development_test_update_loop() -> None:
    for contract_path in ("AGENTS.md", "CLAUDE.md"):
        agents = read(contract_path)

        for phrase in (
            "开发测试阶段快速更新",
            CANONICAL_COMMAND,
            "提交并推送",
            "state=verified",
            "不得重复执行 CI/G2",
            "普通 `web-only`/`backend-safe`",
            "ci-gate=success",
            "promote --ref origin/main",
            "无迁移更新不得创建数据库 checkpoint",
            "rolled_back",
            "不得回退 schema",
            "初始化必须事先取得操作者明确确认",
            "管理员初始化",
            "正式厂商 Key",
            "测试号码",
        ):
            assert phrase in agents


def test_runbook_defines_one_canonical_daily_workflow() -> None:
    runbook = read("docs/runbooks/test-fast-update.md")

    assert "export SMS_TEST_UPDATE_ROOT" not in runbook
    assert "部署根目录固定为 `/opt/sms-platform`" in runbook
    assert "不得从历史会话" in runbook
    assert "root/base/target/ref" in runbook

    for phrase in (
        "## 日常标准流程",
        CANONICAL_COMMAND,
        "约 1 分 20 秒",
        "state=verified",
        "浏览器",
        "接口",
        "不重复",
        "无迁移",
        "rolled_back",
        "不自动回退 schema",
        "修复代码",
        "保留 PostgreSQL 数据库、Docker volume",
        "任何初始化必须事先取得操作者明确确认",
        "管理员初始化",
        "正式厂商 Key",
        "真实测试号码",
    ):
        assert phrase in runbook

    order = (
        "完成开发并运行",
        "提交修改",
        "推送目标分支",
        "scripts/test_update.sh plan --ref origin/<branch>",
        "scripts/test_update.sh apply --ref origin/<branch>",
        "state=verified",
        "针对性验收",
        "promote --ref origin/main",
    )
    daily = runbook.split("## 日常标准流程", maxsplit=1)[1]
    positions = [daily.index(phrase) for phrase in order]
    assert positions == sorted(positions)


def test_rehearsal_report_records_five_consecutive_verified_updates() -> None:
    report_path = ROOT / "docs/reports/2026-07-18-test-fast-update-rehearsal.md"
    if not report_path.is_file():
        pytest.skip("private-only rehearsal report is intentionally absent from public snapshot")
    report = report_path.read_text(encoding="utf-8")

    for phrase in (
        "## 连续五次稳定性复验",
        "test-20260718T151140Z-8c1bbe97e144",
        "test-20260718T151450Z-28b21d7da078",
        "test-20260718T151655Z-62014118c966",
        "test-20260718T151853Z-6142b769135d",
        "test-20260718T152118Z-3cc667c48ddb",
        "5/5",
        "79.0 秒",
        "3 / 0 / 0 / 0 / 0 / 50 / 40",
        "5324",
        "5370",
        "四个数据卷",
        "未初始化数据库",
    ):
        assert phrase in report


def test_fast_update_docs_keep_temporary_https_host_install_independent() -> None:
    index = read("deploy/README.md")
    controlled = read("docs/runbooks/controlled-real-vendor-test.md")
    manual = read("docs/TEST-MANUAL.md")
    combined = "\n".join((index, controlled, manual))

    for phrase in (
        CANONICAL_COMMAND,
        "state=verified",
        "一次性主机安装",
        "install_test_secure_access.py",
        "不自动启动",
        "不安装或轮换正式 Key",
        "不登记测试号码",
        "不激活真实联调",
        "不执行管理员初始化",
        "不初始化数据库",
        "保留 PostgreSQL 数据库、Docker volume",
    ):
        assert phrase in combined


def test_public_workspace_docs_forbid_private_object_cutover() -> None:
    publication = read("PUBLICATION.md")
    maintenance = read("MAINTENANCE.md")
    runbook = read("docs/runbooks/test-fast-update.md")
    deployment = read("deploy/README.md")
    decisions = read("docs/DECISIONS.md")

    assert "git fetch <已授权的归档源仓库URL>" not in runbook
    assert "Git pack" in publication
    for document in (publication, maintenance, runbook, deployment):
        assert "隔离临时证据" in document
        assert "公开工作区" in document
    assert "public snapshot cutover 已禁用" in read("scripts/test_update.sh")
    assert "本地 driver 已明确拒绝" in deployment
    assert "D054 公开工作区禁止跨历史 Git 对象切换" in decisions
    assert "本决策取代 D051" in decisions


def test_test_manual_selects_mock_or_controlled_profile_from_status() -> None:
    manual = read("docs/TEST-MANUAL.md")

    for phrase in (
        "scripts/test_update.sh status",
        "development-vendor-live",
        "controlled",
        "VENDOR_TEST_CONSOLE_ONLY",
        "POST /api/v1/messages/uat-send",
        "每日最多 100 个计费条",
        "产生真实短信和实际计费",
        "VENDOR_MOCK=1",
    ):
        assert phrase in manual
    assert "当前服务器是访问受控的 Mock 测试基座" not in manual
    assert "不会发送真实短信" not in manual


def test_github_auth_preflight_is_documented_without_token_export() -> None:
    for path in (
        "MAINTENANCE.md",
        "PUBLICATION.md",
        "CONTRIBUTING.md",
        "docs/runbooks/test-fast-update.md",
    ):
        document = read(path)
        assert "gh auth status --hostname github.com" in document
        assert "export GITHUB_TOKEN=" not in document
        assert "export GH_TOKEN=" not in document


def test_bootstrap_describes_actual_ci_event_and_reuse_policy() -> None:
    bootstrap = read("BOOTSTRAP.md")

    for phrase in (
        "同仓分支只由 `push` 执行一次 CI",
        "fork 没有同仓 push",
        "`scripts/verify_all.sh --mode integration`",
        "只运行 `changes` 与 `ci-gate`",
        "跳过 backend/frontend/security/g2",
    ):
        assert phrase in bootstrap
    assert "执行未裁剪的权威 `scripts/verify_all.sh`" not in bootstrap


def test_active_docs_use_18_secrets_and_do_not_link_private_evidence() -> None:
    active_documents = (
        "AUTOPILOT.md",
        "BOOTSTRAP.md",
        "HANDOVER.md",
        "RELEASE.md",
        "deploy/README.md",
        "docs/ACCEPTANCE.md",
        "docs/LOCAL_TESTING.md",
        "docs/TEST-MANUAL.md",
    )
    forbidden_links = (
        "docs/UAT-report.md",
        "](UAT-report.md)",
        "](docs/reports/",
        "](reports/",
        "](plans/",
    )

    for path in active_documents:
        document = read(path)
        assert "生产八件" not in document
        assert "八件 secrets" not in document
        assert "八件套" not in document
        for link in forbidden_links:
            assert link not in document
    assert "18 件" in read("deploy/secrets.md")
