from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_acceptance_matrix_maps_milestones_security_and_deferred_boundaries() -> None:
    path = ROOT / "docs/ACCEPTANCE.md"
    assert path.is_file(), "缺少 docs/ACCEPTANCE.md"
    document = path.read_text(encoding="utf-8")
    for token in (
        "M1",
        "M2",
        "M3",
        "M4",
        "FR-05a",
        "NFR-03/04",
        "security_acceptance.py",
        "VENDOR_MOCK=1",
        "AUTH_MOCK=1",
        "log-sink",
        "T4.11",
        "T4.12",
        "[HANDOVER]",
        "RTO≤30min",
    ):
        assert token in document
    assert "真实企微" in document and "禁止" in document


def test_traceability_points_security_compliance_to_executable_acceptance() -> None:
    traceability = (ROOT / "docs/TRACEABILITY.md").read_text(encoding="utf-8")
    assert "维护期不再同步历史" in traceability
    security_row = next(
        line for line in traceability.splitlines() if "NFR-03/04" in line
    )
    assert "security_acceptance.py" in security_row
    assert "docs/ACCEPTANCE.md" in security_row


def test_authoritative_account_contract_covers_local_ad_and_future_iam_boundary() -> None:
    prd = (ROOT / "PRD.md").read_text(encoding="utf-8")
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

    for token in (
        "显式选择认证源",
        "禁止自动回退",
        "不开放自助注册",
        "本地账号仅由管理员维护",
        "全局不区分大小写登录名空间",
        "先到先得",
        "首次登录必须修改密码",
        "系统配置页",
        "IAM Provider",
        "仅预留扩展能力，本期不实现",
    ):
        assert token in prd

    for token in (
        "init-admin",
        "仅空系统",
        "20 位临时密码",
        "本地账号",
        "ldap_real",
        "未来 IAM",
    ):
        assert token in agents

    for retired in ("BOOTSTRAP_ADMIN_USERS", "bootstrap_admin"):
        assert retired not in prd
        assert retired not in agents
