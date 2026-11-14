from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_vendor_boundary_static_invariants_pass_for_repository() -> None:
    result = subprocess.run(
        ["python3", "scripts/check_invariants.py"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "厂商调用边界已锁定" in result.stdout


def test_static_gate_keeps_vendor_boundaries_and_forbids_restoring_live_test() -> None:
    """真实联调已下线：门禁继续锁定厂商调用边界，并阻止入口、挂载与契约被恢复。"""
    source = (ROOT / "scripts/check_invariants.py").read_text(encoding="utf-8")

    for token in (
        "development/test requires VENDOR_MOCK=1",
        "def vendor_live_test(",
        "api/vendor_test.py",
        "uat-send",
        "/run/vendor-test",
        "/run/vendor-control",
        "CredentialEnvelopeModel",
        "pause_queues=True",
        'alert_level="crit"',
        "mock-vendor",
        "docker.sock",
        "VENDOR_LIVE_PROTECTED_EXACT",
        "backend/app/tasks/reconcile.py",
    ):
        assert token in source


def test_retired_control_plane_tables_are_absent_from_current_schema() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE vendor_test_" not in schema
    assert "禁止恢复 vendor_test_* 库表" in (
        ROOT / "scripts/check_invariants.py"
    ).read_text(encoding="utf-8")
