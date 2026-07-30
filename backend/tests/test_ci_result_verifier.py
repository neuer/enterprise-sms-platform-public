from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from verify_ci_results import main, validate_job, verify_results  # noqa: E402


@pytest.mark.parametrize(
    ("expected", "actual"),
    [
        (True, "success"),
        (False, "skipped"),
        (False, "success"),
    ],
)
def test_expected_job_result_accepts_only_safe_states(expected: bool, actual: str) -> None:
    assert validate_job("backend", expected=expected, actual=actual) == []


@pytest.mark.parametrize("actual", ["failure", "cancelled", "skipped"])
def test_expected_job_must_succeed(actual: str) -> None:
    assert validate_job("backend", expected=True, actual=actual) == [
        f"backend expected success, got {actual}"
    ]


@pytest.mark.parametrize("actual", ["failure", "cancelled"])
def test_unexpected_job_cannot_fail_if_it_runs(actual: str) -> None:
    assert validate_job("frontend", expected=False, actual=actual) == [
        f"frontend was optional but got {actual}"
    ]


@pytest.mark.parametrize("actual", ["", "unknown", "untrusted-state"])
def test_missing_or_invalid_job_result_uses_fixed_error(actual: str) -> None:
    assert validate_job("g2", expected=True, actual=actual) == [
        "g2 result is missing or invalid"
    ]


def test_changes_must_always_succeed() -> None:
    result = verify_results(
        changes="failure",
        expected={
            "backend": False,
            "frontend": False,
            "security": False,
            "g2": False,
        },
        actual={
            "backend": "skipped",
            "frontend": "skipped",
            "security": "skipped",
            "g2": "skipped",
        },
        candidate_sha="c" * 40,
        tested_sha={"backend": "", "frontend": "", "security": "", "g2": ""},
    )

    assert result == ["changes expected success, got failure"]


def test_invalid_changes_result_uses_fixed_error() -> None:
    result = verify_results(
        changes="untrusted-state",
        expected={
            "backend": False,
            "frontend": False,
            "security": False,
            "g2": False,
        },
        actual={
            "backend": "skipped",
            "frontend": "skipped",
            "security": "skipped",
            "g2": "skipped",
        },
        candidate_sha="c" * 40,
        tested_sha={"backend": "", "frontend": "", "security": "", "g2": ""},
    )

    assert result == ["changes result is missing or invalid"]


def cli_args() -> list[str]:
    return [
        "--changes-result",
        "success",
        "--candidate-sha",
        "c" * 40,
        "--backend-expected",
        "false",
        "--backend-result",
        "skipped",
        "--backend-tested-sha",
        "",
        "--frontend-expected",
        "true",
        "--frontend-result",
        "success",
        "--frontend-tested-sha",
        "c" * 40,
        "--security-expected",
        "false",
        "--security-result",
        "skipped",
        "--security-tested-sha",
        "",
        "--g2-expected",
        "false",
        "--g2-result",
        "skipped",
        "--g2-tested-sha",
        "",
    ]


def test_cli_accepts_a_valid_selective_result(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(cli_args())

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.out == "CI result verification passed\n"
    assert captured.err == ""


def test_cli_rejects_invalid_boolean_without_echoing_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = cli_args()
    args[args.index("false")] = "untrusted-boolean"

    exit_code = main(args)

    assert exit_code == 2
    captured = capsys.readouterr()
    assert captured.err == "CI result verification input is invalid\n"
    assert "untrusted-boolean" not in captured.out + captured.err


def test_cli_rejects_missing_or_invalid_result_without_echoing_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = cli_args()
    args[args.index("skipped")] = "untrusted-result"

    exit_code = main(args)

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "backend result is missing or invalid" in captured.err
    assert "untrusted-result" not in captured.out + captured.err


def test_successful_job_evidence_must_match_exact_candidate_sha() -> None:
    args = cli_args()
    args[args.index("c" * 40, 4)] = "d" * 40

    assert main(args) == 1
