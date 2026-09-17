#!/usr/bin/env python3
"""各门禁共用同一后端 Ruff/Mypy 范围，并拒绝依赖锁漂移。"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MYPY_TARGETS = (
    "app",
    "migrations",
    "scripts_support",
    "../scripts/check_contract.py",
    "../scripts/classify_ci_changes.py",
    "../scripts/verify_ci_results.py",
    "../scripts/g2_timing.py",
    "../scripts/check_public_readiness.py",
    "../scripts/export_public_snapshot.py",
    "../scripts/check_coverage_gates.py",
    "../scripts/release_metadata.py",
    "../scripts/create_release_manifest.py",
    "../scripts/create_offline_image_index.py",
    "../scripts/deploy_release_remote.py",
    "../deploy/scripts/offline_image_archive.py",
    "../deploy/scripts/release_manifest.py",
    "../scripts/security_acceptance.py",
    "../scripts/e2e_api.py",
    "../scripts/perf_smoke.py",
    "../scripts/verify_web_transport.py",
    "../scripts/verify_public_snapshot_cutover.py",
    "../scripts/gate_policy.py",
    "../scripts/gate_snapshot.py",
    "../scripts/check_gate_contracts.py",
    "../scripts/check_backend_static.py",
)


def main() -> int:
    """一次配置驱动本地、CI 和 full G2 的静态检查。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tool", choices=("ruff", "mypy", "all"), default="all")
    args = parser.parse_args()
    ruff_targets = [
        "app",
        "migrations",
        "scripts_support",
        "tests",
        "../scripts",
        "../deploy/scripts",
    ]
    if (ROOT / ".cursor/hooks").is_dir():
        ruff_targets.append("../.cursor/hooks")
    for name, targets in (("ruff", ("check", *ruff_targets)), ("mypy", MYPY_TARGETS)):
        if args.tool in (name, "all"):
            result = subprocess.run(["uv", "run", "--locked", name, *targets], cwd=ROOT / "backend")
            if result.returncode:
                return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
