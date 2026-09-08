#!/usr/bin/env python3
"""执行轻量门禁及前端契约，文档或前端专用 CI 同样不能漏跑。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from gate_policy import GATE_CONTRACT_TESTS

if __name__ == "__main__":
    raise SystemExit(
        subprocess.call(
            [sys.executable, "-m", "pytest", "-q", *GATE_CONTRACT_TESTS],
            cwd=Path(__file__).resolve().parents[1] / "backend",
        )
    )
