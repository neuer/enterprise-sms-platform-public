from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "auth_timing_report",
    Path(__file__).resolve().parents[2] / "scripts/auth_timing_report.py",
)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_rank_auc_detects_separation_and_constant_control() -> None:
    assert module.rank_auc([1, 2, 3], [4, 5, 6]) == 1
    assert module.rank_auc([10, 10], [10, 10]) == 0.5
    result = module.report(
        {
            "version": 1,
            "samples": [
                {"case": case, "mode": "cold_single", "duration_ms": 10}
                for case in ("missing", "wrong_password")
            ],
        }
    )
    assert result["comparisons"][0]["auc"] == 0.5
    assert result["acceptance"] == "not_evaluated"


@pytest.mark.parametrize("field", ["username", "dn", "password", "address"])
def test_timing_report_rejects_identity_or_secret_columns(field: str) -> None:
    with pytest.raises(ValueError):
        module.report(
            {
                "version": 1,
                "samples": [
                    {
                        "case": "missing",
                        "mode": "cold_single",
                        "duration_ms": 10,
                        field: "synthetic",
                    }
                ]
                * 2,
            }
        )
