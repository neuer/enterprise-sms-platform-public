from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "test_rebuild.sh"


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    "arguments",
    [
        ("--ref",),
        ("--ref", "main"),
        ("--ref", "origin/../main"),
        ("--ref", "origin/main; rm -rf /"),
        ("apply",),
        ("--public-snapshot-cutover",),
    ],
)
def test_rebuild_rejects_invalid_arguments_before_any_network_or_ssh(
    arguments: tuple[str, ...],
) -> None:
    """参数校验先于 fetch、CI 核验与 SSH，非法输入只返回用法错误。"""
    result = _run(*arguments)

    assert result.returncode == 2
    assert "test-rebuild" in result.stderr or "usage:" in result.stderr


def test_rebuild_rejects_config_with_unexpected_keys(tmp_path: Path) -> None:
    """本地连接文件只接受目标与端口，出现其它键立即失败且不回显其值。"""
    checkout = tmp_path / "repo"
    (checkout / "scripts").mkdir(parents=True)
    script = checkout / "scripts" / "test_rebuild.sh"
    script.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    (checkout / ".env.test-update").write_text(
        "SMS_TEST_UPDATE_TARGET=operator@test-host\nSMS_TEST_PASSWORD=do-not-echo\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(script)],
        env={"PATH": os.environ["PATH"]},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "只允许" in result.stderr
    assert "do-not-echo" not in result.stderr + result.stdout


def test_rebuild_verifies_ci_then_guards_live_state_before_destroying_data() -> None:
    """固定顺序：先核验 ci-gate，再连服务器；远端先查真实联调标记，最后才重建。"""
    source = SCRIPT.read_text(encoding="utf-8")

    ci_gate = source.index("scripts/verify_ci_commit.py")
    dry_run_exit = source.index("dry-run，未连接服务器")
    ssh = source.index("ssh -p")
    live_guard = source.index('if [[ -e "$live_marker" ]]')
    checkout = source.index('git checkout --quiet --detach "$commit"')
    reset = source.index("bash scripts/local_test.sh reset")
    record = source.index("scripts/record_test_deployment.sh")
    assert ci_gate < dry_run_exit < ssh < live_guard < checkout < reset < record

    assert 'LIVE_MARKER="/etc/sms-platform/test-environment"' in source
    assert "StrictHostKeyChecking=yes" in source
    assert "BatchMode=yes" in source
    assert "<<'REMOTE'" in source
    for forbidden in ("--no-verify", "reset --hard", "git clean", "eval ", "password"):
        assert forbidden not in source


def test_rebuild_reuses_local_reset_and_is_the_only_documented_test_entry() -> None:
    """重建只复用 local_test.sh reset；文档与规则指向同一入口，旧入口不得残留。"""
    local_test = (ROOT / "scripts" / "local_test.sh").read_text(encoding="utf-8")
    assert "compose down -v" in local_test
    assert 'fail "本地测试要求 VENDOR_MOCK=1"' in local_test

    maintenance = (ROOT / "MAINTENANCE.md").read_text(encoding="utf-8")
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "scripts/test_rebuild.sh --ref origin/main" in maintenance
    assert "state=rebuilt" in maintenance
    assert "state=rebuilt" in agents
    for document in (maintenance, agents, (ROOT / "PUBLICATION.md").read_text(encoding="utf-8")):
        assert "scripts/test_update.sh" not in document
    assert not (ROOT / "scripts" / "test_update.sh").exists()
