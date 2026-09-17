"""分片证据必须证明实际执行，不能仅靠覆盖率百分比放行。"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from check_coverage_gates import CoverageGateError, verify_test_evidence  # noqa: E402
from gate_policy import CRITICAL_MARKERS  # noqa: E402

SHA = "a" * 40


def evidence():
    common = {"schema": 1, "sha": SHA, "exit_code": 0, "failed": [], "skipped": {}}
    inventory = dict(
        common,
        shard="inventory",
        collected=["unit", "pg"],
        isolated=["pg"],
        markers={key: ["unit"] for key in CRITICAL_MARKERS},
    )
    return inventory, [
        dict(common, shard="unit", collected=["unit"], passed=["unit"]),
        dict(common, shard="postgres", collected=["pg"], passed=["pg"]),
    ]


def test_complete_shards_pass():
    inventory, parts = evidence()
    verify_test_evidence(inventory, parts, commit=SHA)


@pytest.mark.parametrize(
    "change", ["missing", "duplicate", "sha", "skip", "failed", "marker", "partial"]
)
def test_incomplete_or_mismatched_evidence_fails(change):
    inventory, parts = copy.deepcopy(evidence())
    if change == "missing":
        parts.pop()
    elif change == "duplicate":
        parts[1]["collected"] = ["unit"]
    elif change == "sha":
        parts[1]["sha"] = "b" * 40
    elif change == "skip":
        parts[1]["passed"] = []
        parts[1]["skipped"] = {"pg": "missing DSN"}
    elif change == "failed":
        parts[1]["exit_code"] = 1
    elif change == "marker":
        inventory["markers"]["authorization"] = []
    elif change == "partial":
        parts[0]["passed"] = []
    with pytest.raises(CoverageGateError):
        verify_test_evidence(inventory, parts, commit=SHA)


def test_plugin_rejects_skipped_postgres_with_existing_external_evidence(tmp_path):
    import os
    import subprocess

    root = tmp_path / "project"
    isolated = root / "tests/integration"
    isolated.mkdir(parents=True)
    (root / "pytest.ini").write_text("[pytest]\ntestpaths=tests\n")
    (isolated / "test_probe.py").write_text(
        'import pytest\ndef test_missing_service(): pytest.skip("missing DSN")\n'
    )
    target = tmp_path / "existing.json"
    target.write_text("{}")
    env = dict(os.environ, SMS_GATE_SHA=SHA)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-c", "pytest.ini", "--rootdir=.",
         "-p", "scripts_support.gate_evidence", "--gate-shard", "postgres",
         "--gate-evidence", str(target)],
        cwd=root, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 1, result.stderr
    import json

    report = json.loads(target.read_text())
    assert report["collected"] == ["tests/integration/test_probe.py::test_missing_service"]
    assert report["exit_code"] == 1
    assert report["skipped"]
