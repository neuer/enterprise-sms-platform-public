#!/usr/bin/env python3
"""对完整 app 覆盖率及五个高风险区域执行独立失败门槛。"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

THRESHOLDS: Mapping[str, float] = {
    "application": 75.0,
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
        return lambda path: path in {
            "app/services/idempotency.py",
            "app/services/pipeline.py",
            "app/services/pipeline_repository.py",
            "app/services/quota.py",
            "app/services/usage_ledger.py",
        }
    if group == "export":
        return lambda path: path.startswith("app/services/export") or path.startswith(
            "app/tasks/export"
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


def evaluate_coverage(document: object) -> dict[str, float]:
    if type(document) is not dict or type(document.get("files")) is not dict:
        raise CoverageGateError("coverage document is invalid")
    files = document["files"]
    if not files or any(type(path) is not str for path in files):
        raise CoverageGateError("coverage file map is empty or invalid")
    if any(not path.startswith("app/") for path in files):
        raise CoverageGateError("coverage must be collected from the complete app package")

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
    failures = [
        name for name, value in totals.items() if value + 1e-9 < THRESHOLDS[name]
    ]
    if failures:
        rendered = ", ".join(
            f"{name}={totals[name]:.2f}%<{THRESHOLDS[name]:.2f}%"
            for name in failures
        )
        raise CoverageGateError(f"coverage thresholds failed: {rendered}")
    return totals


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args(argv)
    try:
        document: Any = json.loads(args.report.read_text(encoding="utf-8"))
        totals = evaluate_coverage(document)
    except (OSError, UnicodeError, json.JSONDecodeError, CoverageGateError) as error:
        print(f"coverage-gate: {error}", file=sys.stderr)
        return 1
    print(
        "coverage-gate: passed "
        + " ".join(f"{name}={totals[name]:.2f}%" for name in THRESHOLDS)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
