"""分片证据必须证明实际执行，不能仅靠覆盖率百分比放行。"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from check_coverage_gates import CoverageGateError, verify_test_evidence  # noqa: E402
from gate_policy import (  # noqa: E402
    CRITICAL_MARKERS,
    POSTGRES_COSTLY_MODULES,
    UNIT_COSTLY_NODES,
    partition_index,
    partition_name,
)

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


def partitioned_evidence(unit_parts=4, postgres_parts=3):
    inventory, _ = evidence()
    unit = {f"tests/test_unit.py::test_case[{index}]" for index in range(100)}
    postgres = {f"tests/integration/test_db_{index}.py::test_case" for index in range(30)}
    unit |= UNIT_COSTLY_NODES
    postgres |= {f"{module}::test_case" for module in POSTGRES_COSTLY_MODULES}
    inventory.update(collected=sorted(unit | postgres), isolated=sorted(postgres),
                     markers={key: sorted(unit | postgres) for key in CRITICAL_MARKERS})
    parts = []
    for kind, nodes, count in (("unit", unit, unit_parts), ("postgres", postgres, postgres_parts)):
        for index in range(1, count + 1):
            selected = sorted(node for node in nodes if partition_index(node, kind, count) == index)
            parts.append({"schema": 1, "sha": SHA, "shard": partition_name(kind, index, count),
                          "collected": selected, "passed": list(selected), "failed": [],
                          "skipped": {}, "exit_code": 0})
    return inventory, parts


def test_parallel_partitions_are_exhaustive_and_database_modules_stay_together():
    inventory, parts = partitioned_evidence()
    verify_test_evidence(inventory, parts, commit=SHA, unit_parts=4, postgres_parts=3)
    assert partition_index("tests/integration/test_db.py::test_a", "postgres", 3) == (
        partition_index("tests/integration/test_db.py::test_b[7]", "postgres", 3)
    )


def test_expensive_cases_have_exclusive_runners_with_complete_inventory():
    inventory, parts = partitioned_evidence(5, 4)
    verify_test_evidence(inventory, parts, commit=SHA, unit_parts=5, postgres_parts=4)
    by_name = {part["shard"]: part["collected"] for part in parts}
    assert set(by_name["unit-5-of-5"]) == UNIT_COSTLY_NODES
    assert {node.split("::")[0] for node in by_name["postgres-4-of-4"]} == POSTGRES_COSTLY_MODULES


@pytest.mark.parametrize("mutation", ["missing", "relabel", "wrong-part", "sha", "skip"])
def test_parallel_evidence_rejects_omission_relabeling_and_misrouting(mutation):
    inventory, parts = partitioned_evidence()
    if mutation == "missing":
        parts.pop()
    elif mutation == "relabel":
        parts[0]["shard"] = "unit-1-of-3"
    elif mutation == "wrong-part":
        # The union still covers every test: misplaced evidence must also fail.
        parts[0]["collected"], parts[1]["collected"] = parts[1]["collected"], parts[0]["collected"]
        parts[0]["passed"], parts[1]["passed"] = parts[1]["passed"], parts[0]["passed"]
    elif mutation == "sha":
        parts[2]["sha"] = "b" * 40
    else:
        node = parts[-1]["passed"].pop()
        parts[-1]["skipped"][node] = "database unavailable"
    with pytest.raises(CoverageGateError):
        verify_test_evidence(inventory, parts, commit=SHA, unit_parts=4, postgres_parts=3)


@pytest.mark.parametrize("kind,index,count", [("unknown", 1, 1), ("unit", 0, 4),
                                               ("postgres", 4, 3), ("unit", 1, 17)])
def test_invalid_partition_is_rejected(kind, index, count):
    with pytest.raises(ValueError):
        partition_name(kind, index, count)
