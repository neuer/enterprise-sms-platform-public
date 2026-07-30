from __future__ import annotations

import ipaddress
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANUAL = ROOT / "docs" / "TEST-MANUAL.md"

PAGE_TITLES = (
    "登录",
    "仪表盘",
    "统计报表",
    "人工发送",
    "审批中心",
    "批次列表",
    "号码搜索",
    "上行回复",
    "模板管理",
    "签名管理",
    "应用管理",
    "黑名单",
    "敏感词",
    "用户与角色",
    "系统参数",
    "回调任务",
    "运维中心",
    "审计日志",
)


def _manual_text() -> str:
    return MANUAL.read_text(encoding="utf-8")


def test_remote_test_manual_is_complete() -> None:
    text = _manual_text()
    assert "${TEST_BASE_URL}/login" in text
    assert "${TEST_BASE_URL}/livez" in text
    assert "${TEST_BASE_URL}/readyz" in text
    for role in ("admin01", "approver01", "operator01", "viewer01"):
        assert role in text
    for section in (
        "## 1. 文档说明",
        "## 2. 测试环境",
        "## 5. 环境冒烟",
        "## 6. 页面与角色测试",
        "## 7. 核心业务 UAT",
        "## 8. 专项测试",
        "## 9. 回归与验收标准",
        "## 10. 测试报告模板",
    ):
        assert section in text
    for page_title in PAGE_TITLES:
        assert page_title in text


def test_every_uat_case_has_execution_and_recovery_details() -> None:
    text = _manual_text()
    for case_no in range(1, 29):
        marker = f"### UAT-{case_no:02d} "
        assert text.count(marker) == 1
        case = text.split(marker, maxsplit=1)[1].split("\n### ", maxsplit=1)[0]
        for field in (
            "优先级",
            "测试角色",
            "前置条件",
            "测试数据",
            "步骤",
            "预期结果",
            "证据",
            "恢复",
        ):
            assert f"**{field}：**" in case, f"UAT-{case_no:02d} 缺少 {field}"


def test_remote_test_manual_does_not_embed_credentials_or_phone_numbers() -> None:
    text = _manual_text()
    lowered = text.lower()
    for forbidden in (
        "sshpass",
        "begin openssh private key",
        "vendor_secret_key=",
        "jwt_secret=",
        "authorization: bearer ey",
        "统一为 `dev",
    ):
        assert forbidden not in lowered
    addresses = re.findall(r"https?://(\d{1,3}(?:\.\d{1,3}){3})", text)
    assert all(not ipaddress.ip_address(value).is_global for value in addresses)
    assert re.search(r"(?<!\d)1\d{10}(?!\d)", text) is None
