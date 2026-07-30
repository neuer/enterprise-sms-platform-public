from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from check_coverage_gates import CoverageGateError, evaluate_coverage  # noqa: E402


def _entry(statements: int, covered: int) -> dict[str, object]:
    return {"summary": {"num_statements": statements, "covered_lines": covered}}


def _report(*, auth_covered: int = 90) -> dict[str, object]:
    return {
        "files": {
            "app/core/auth/runtime.py": _entry(100, auth_covered),
            "app/services/pipeline.py": _entry(100, 90),
            "app/services/export_worker.py": _entry(100, 85),
            "app/tasks/send.py": _entry(100, 80),
            "app/api/messages.py": _entry(100, 80),
        }
    }


def test_independent_high_risk_coverage_thresholds_pass() -> None:
    totals = evaluate_coverage(_report())

    assert totals["application"] == 85
    assert totals["auth"] == 90
    assert totals["pipeline"] == 90


def test_low_auth_coverage_fails_even_when_application_total_passes() -> None:
    with pytest.raises(CoverageGateError, match=r"auth=79.00%<80.00%"):
        evaluate_coverage(_report(auth_covered=79))


def test_missing_region_or_partial_non_app_report_fails_closed() -> None:
    with pytest.raises(CoverageGateError, match="complete app"):
        evaluate_coverage({"files": {"scripts/probe.py": _entry(100, 100)}})
    with pytest.raises(CoverageGateError, match="export coverage evidence is missing"):
        report = _report()
        del report["files"]["app/services/export_worker.py"]  # type: ignore[index]
        evaluate_coverage(report)
