from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "scripts" / "redis_ha_preflight.py"


def test_preflight_rejects_standalone_labeled_as_ha() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--mode", "standalone"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "must not be labeled production HA" in result.stderr


def test_preflight_accepts_managed_and_isolated_standalone() -> None:
    for mode in ("managed", "isolated-standalone"):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--mode", mode],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
