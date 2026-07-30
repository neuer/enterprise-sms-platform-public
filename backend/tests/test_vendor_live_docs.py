from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_controlled_real_vendor_runbook_documents_non_negotiable_controls() -> None:
    runbook = (ROOT / "docs/runbooks/controlled-real-vendor-test.md").read_text(
        encoding="utf-8"
    )
    for phrase in (
        "再次明确授权",
        "系统配置页",
        "真实联调",
        "vendor-control-agent",
        "WebCrypto",
        "单用途",
        "VENDOR_TEST_CONSOLE_ONLY",
        "PostgreSQL",
        "默认按已报备处理",
        "1010",
        "GetBalance",
        "GetReport",
        "GetReply",
        "100 个计费条",
        "uncertain",
        "不清库",
        "不删除 volume",
        "不自动 restore",
        "不回退 Mock",
        "密钥值、长度、摘要、哈希",
        "手机号或 HMAC",
        "critical pause",
        "daily pause",
        "root:10001",
        "0710",
        "0640",
        "status=inactive",
        "status=controlled",
        "/api/v1/messages/uat-send",
        "biz_id",
        "仅通知",
    ):
        assert phrase in runbook
    assert "## TTY 安装凭据和测试号码" not in runbook
    assert "http://<测试服务器>" not in runbook
    assert "urllib.request" in runbook
    assert "HTTPRedirectHandler" in runbook
    assert "redirect_request" in runbook
    assert "getpass.getpass" in runbook
    assert "https://<临时 HTTPS 地址>" in runbook
    assert "本次 biz_id" in runbook
    assert '-H "X-Api-Key: $SMS_API_KEY"' not in runbook
    block = re.search(r"```fish\npython3 -c '\n(?P<code>.*?)\n'\n```", runbook, re.DOTALL)
    assert block is not None
    compile(block.group("code"), "controlled-api-uat-runbook", "exec")
    assert "正常操作均在系统配置页完成" in runbook
    assert "底层固定 wrapper" in runbook
    assert "受控应急" in runbook
    for phrase in (
        "deploy/systemd/vendor-control-agent.service",
        "compose.vendor-test.env.example",
        "sms-compose vendor-test bootstrap",
        "systemctl is-active vendor-control-agent.service",
        "setup_required",
        "版本化凭据根目录不得进入备份",
        "页面安装凭据后才进入 inactive",
        "任何阶段都不自动启动真实发送",
        "只能由管理员在页面手工激活",
    ):
        assert phrase in runbook


def test_controlled_real_vendor_runbook_documents_reset_contract() -> None:
    runbook = (ROOT / "docs/runbooks/controlled-real-vendor-test.md").read_text(
        encoding="utf-8"
    )
    heading = "## 页面清空联调设置"
    assert heading in runbook
    reset_section = runbook.split(heading, maxsplit=1)[1].split("\n## ", maxsplit=1)[0]

    for phrase in (
        "`/configs` 的「真实联调」页是清空联调设置的唯一入口",
        "只在已配置凭据的 `inactive` 状态且没有暂停投影时",
        "`setup_required` 不允许创建新的 reset operation",
        "原 operation id",
        "运行中 operation 时动作仍显示但禁用，不得重复提交",
        "当前 Provider 二次认证",
        "精确输入“清空联调设置”",
        "全部凭据 generation",
        "全部加密测试收件人",
        "保留管理员账号、短信业务数据和审计记录",
        "保留当日 UAT 用量与 uncertain 占额",
        "保留数据库、Docker volume 和运行态目录",
        "最终安全状态必须是 `setup_required`",
        "部分设置可能已经清理",
        "不得夹带到快速更新",
        "不得夹带管理员初始化",
        "不创建 seal session",
        "不得手工恢复 generation",
        "不得自动调用 reset",
    ):
        assert phrase in reset_section


def test_authoritative_contracts_lock_page_only_vendor_test_security_boundary() -> None:
    agent_contract = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    prd = (ROOT / "PRD.md").read_text(encoding="utf-8")
    decisions = (ROOT / "docs/DECISIONS.md").read_text(encoding="utf-8")
    approved_documents = "\n".join((agent_contract, prd, decisions))

    for phrase in (
        "真实联调",
        "vendor-control-agent",
        "WebCrypto",
        "单用途",
        "VENDOR_TEST_CONSOLE_ONLY",
        "PostgreSQL",
        "100 个计费条",
        "/api/v1/messages/uat-send",
        "biz_id",
        "仅通知",
    ):
        assert phrase in approved_documents

    for phrase in (
        "浏览器易失内存",
        "浏览器持久化",
        "普通 API 明文",
    ):
        assert phrase in agent_contract

    assert "完整设计证据保存在受限归档" in decisions
    assert "](plans/" not in decisions
    configs_row = next(line for line in prd.splitlines() if "| /configs |" in line)
    assert "真实联调" in configs_row


def test_fast_update_runbook_documents_safe_and_high_risk_matrix() -> None:
    runbook = (ROOT / "docs/runbooks/test-fast-update.md").read_text(encoding="utf-8")

    for phrase in (
        "web-only",
        "backend-safe",
        "high-risk",
        "ci-gate=success",
        "prepare",
        "apply",
        "verify",
        "自动恢复上一版",
        "不自动回退 schema",
        "不切回 Mock",
        "保留数据库和 volume",
        "/usr/bin/env",
        "SMS_PLATFORM_ROOT=/opt/sms-platform",
        "SMS_SECRETS_MODE=development",
        "SMS_RUNTIME_ROOT=/run/sms-platform/secrets",
        "SMS_VENDOR_CREDENTIAL_ROOT=/var/lib/sms-platform/vendor-test/credentials",
    ):
        assert phrase in runbook
    assert "sudo /usr/local/sbin/sms-compose test-update" not in runbook


def test_deployment_index_and_egress_docs_link_controlled_test_runbook() -> None:
    deploy_readme = (ROOT / "deploy/README.md").read_text(encoding="utf-8")
    egress = (ROOT / "deploy/vendor-egress.md").read_text(encoding="utf-8")

    assert "controlled-real-vendor-test.md" in deploy_readme
    assert "test-fast-update.md" in deploy_readme
    assert "默认按已报备处理" in egress
    assert "收到 1010" in egress
    assert "日常操作只允许在系统配置页完成" in egress
    assert "部署不得自动激活真实联调" in egress
    assert "vendor-control-agent.service" in deploy_readme
