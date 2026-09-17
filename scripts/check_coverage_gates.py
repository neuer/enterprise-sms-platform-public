#!/usr/bin/env python3
"""对完整 app 覆盖率及五个高风险区域执行独立失败门槛。"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from gate_policy import CRITICAL_MARKERS

THRESHOLDS: Mapping[str, float] = {
    "application": 75.0,
    "services": 80.0,
    "auth": 80.0,
    "pipeline": 85.0,
    "export": 80.0,
    "tasks": 75.0,
    "api": 70.0,
}


class CoverageGateError(ValueError):
    """覆盖证据缺失、畸形或低于门槛。"""


def _matches(group: str) -> Callable[[str], bool]:
    if group == "auth":
        return lambda path: (
            path.startswith("app/core/auth/")
            or path == "app/core/apikey.py"
            or path.startswith("app/api/auth")
            or path.startswith("app/services/auth_provider")
        )
    if group == "pipeline":
        return lambda path: (
            path
            in {
                "app/services/idempotency.py",
                "app/services/pipeline.py",
                "app/services/pipeline_repository.py",
                "app/services/quota.py",
                "app/services/usage_ledger.py",
            }
        )
    if group == "export":
        return lambda path: (
            path.startswith("app/services/export") or path.startswith("app/tasks/export")
        )
    return lambda path: path.startswith(f"app/{group}/")


def _summary(value: object) -> tuple[int, int]:
    if type(value) is not dict:
        raise CoverageGateError("coverage file entry is invalid")
    summary = value.get("summary")
    if type(summary) is not dict:
        raise CoverageGateError("coverage file summary is missing")
    statements = summary.get("num_statements")
    covered = summary.get("covered_lines")
    if (
        type(statements) is not int
        or type(covered) is not int
        or statements < 0
        or not 0 <= covered <= statements
    ):
        raise CoverageGateError("coverage counters are invalid")
    return statements, covered


def evaluate_coverage(
    document: object,
    *,
    expected_files: set[str] | None = None,
) -> dict[str, float]:
    if type(document) is not dict or type(document.get("files")) is not dict:
        raise CoverageGateError("coverage document is invalid")
    files = document["files"]
    if not files or any(type(path) is not str for path in files):
        raise CoverageGateError("coverage file map is empty or invalid")
    if any(not path.startswith("app/") for path in files):
        raise CoverageGateError("coverage must be collected from the complete app package")
    if expected_files is not None and set(files) != expected_files:
        raise CoverageGateError("coverage file inventory does not match the complete app package")

    totals: dict[str, float] = {}
    groups = {name: _matches(name) for name in THRESHOLDS if name != "application"}
    for name in THRESHOLDS:
        selected = (
            files.items()
            if name == "application"
            else ((path, value) for path, value in files.items() if groups[name](path))
        )
        statements = covered = 0
        for _path, value in selected:
            file_statements, file_covered = _summary(value)
            statements += file_statements
            covered += file_covered
        if statements == 0:
            raise CoverageGateError(f"{name} coverage evidence is missing")
        totals[name] = covered * 100 / statements
    failures = [name for name, value in totals.items() if value + 1e-9 < THRESHOLDS[name]]
    if failures:
        rendered = ", ".join(
            f"{name}={totals[name]:.2f}%<{THRESHOLDS[name]:.2f}%" for name in failures
        )
        raise CoverageGateError(f"coverage thresholds failed: {rendered}")
    return totals


def _nodes(value: object) -> set[str]:
    """执行集合必须由唯一且非空的 pytest nodeid 构成。"""
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise CoverageGateError("test node inventory is invalid")
    if len(set(value)) != len(value):
        raise CoverageGateError("duplicate test node evidence")
    return set(value)


def allowed_skip(node: str, reason: str) -> bool:
    """仅允许公开快照的两项文档例外与本地缺少 Lua 的已知替代测试。"""
    docs = {
        ("tests/test_deployment_docs.py::"
         "test_development_high_risk_release_reload_boundary_is_documented"): (
             "private-only reset plan is intentionally absent from public snapshot"
         ),
        ("tests/test_test_update_workflow_docs.py::"
         "test_rehearsal_report_records_five_consecutive_verified_updates"): (
             "private-only rehearsal report is intentionally absent from public snapshot"
         ),
    }
    if node in docs:
        return docs[node] in reason
    return node.startswith("tests/test_vendor_bucket_clock.py::") and (
        "requires Lua; real Redis also covered by integration" in reason
    )


def verify_test_evidence(inventory: Any, parts: Sequence[Any], *, commit: str) -> None:
    """验证分区完整、互斥、同 SHA，并要求每类关键测试实际成功。"""
    if not isinstance(inventory, dict) or inventory.get("shard") != "inventory":
        raise CoverageGateError("independent test inventory is missing")
    if len(parts) != 2 or {part.get("shard") for part in parts if isinstance(part, dict)} != {
        "unit",
        "postgres",
    }:
        raise CoverageGateError("unit/postgres evidence set is incomplete")
    all_nodes = _nodes(inventory.get("collected"))
    isolated = _nodes(inventory.get("isolated"))
    if not all_nodes or not isolated or not isolated <= all_nodes:
        raise CoverageGateError("isolated test inventory is missing")
    seen: set[str] = set()
    passed: set[str] = set()
    for part in (inventory, *parts):
        if not isinstance(part, dict) or part.get("schema") != 1 or part.get("sha") != commit:
            raise CoverageGateError("test evidence SHA/schema mismatch")
        if part.get("exit_code") != 0:
            raise CoverageGateError("test shard did not finish successfully")
    for part in parts:
        nodes = _nodes(part.get("collected"))
        success = _nodes(part.get("passed"))
        failed = _nodes(part.get("failed"))
        skips = part.get("skipped")
        if not isinstance(skips, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in skips.items()
        ):
            raise CoverageGateError("test skip evidence is invalid")
        if (
            not nodes
            or nodes & seen
            or failed
            or success & skips.keys()
            or success | skips.keys() != nodes
        ):
            raise CoverageGateError("test execution evidence is incomplete or overlapping")
        expected = isolated if part["shard"] == "postgres" else all_nodes - isolated
        if nodes != expected:
            raise CoverageGateError("test shard does not match independent inventory")
        if part["shard"] == "postgres" and skips:
            raise CoverageGateError("isolated tests must not skip")
        if any(not allowed_skip(node, reason) for node, reason in skips.items()):
            raise CoverageGateError("unexpected test skip")
        if sum(node.startswith("tests/test_vendor_bucket_clock.py::") for node in skips) > 11:
            raise CoverageGateError("Lua skip allowance expanded")
        seen |= nodes
        passed |= success
    if seen != all_nodes:
        raise CoverageGateError("test partitions do not cover all collected tests")
    markers = inventory.get("markers")
    if not isinstance(markers, dict):
        raise CoverageGateError("critical marker inventory is missing")
    for name in CRITICAL_MARKERS:
        required = _nodes(markers.get(name))
        if not required or not required <= passed:
            raise CoverageGateError(f"critical category did not execute completely: {name}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args(argv)
    try:
        document: Any = json.loads(args.report.read_text(encoding="utf-8"))
        evidence = args.evidence_dir
        verify_test_evidence(
            json.loads((evidence / "inventory.json").read_text(encoding="utf-8")),
            [
                json.loads((evidence / name / "tests.json").read_text(encoding="utf-8"))
                for name in (
                    "unit",
                    "postgres",
                )
            ],
            commit=args.commit,
        )
        backend = Path(__file__).resolve().parents[1] / "backend"
        expected = {
            path.relative_to(backend).as_posix() for path in (backend / "app").rglob("*.py")
        }
        totals = evaluate_coverage(document, expected_files=expected)
    except (OSError, UnicodeError, json.JSONDecodeError, CoverageGateError) as error:
        print(f"coverage-gate: {error}", file=sys.stderr)
        return 1
    print("coverage-gate: passed " + " ".join(f"{name}={totals[name]:.2f}%" for name in THRESHOLDS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
