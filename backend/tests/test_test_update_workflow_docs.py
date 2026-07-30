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
