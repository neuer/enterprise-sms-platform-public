#!/usr/bin/env python3
"""验证选择性 CI 的预期 job 与 GitHub 实际结果一致。"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Mapping, Sequence

KNOWN_RESULTS = {"success", "failure", "cancelled", "skipped"}
JOB_NAMES = ("backend", "frontend", "security", "g2")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")


def validate_job(name: str, *, expected: bool, actual: str) -> list[str]:
    if actual not in KNOWN_RESULTS:
        return [f"{name} result is missing or invalid"]
    if expected and actual != "success":
        return [f"{name} expected success, got {actual}"]
    if not expected and actual not in {"success", "skipped"}:
        return [f"{name} was optional but got {actual}"]
    return []


def verify_results(
    *,
    changes: str,
    expected: Mapping[str, bool],
    actual: Mapping[str, str],
    candidate_sha: str,
    tested_sha: Mapping[str, str],
) -> list[str]:
    errors: list[str] = []
    candidate_valid = _COMMIT_RE.fullmatch(candidate_sha) is not None
    if not candidate_valid:
        errors.append("candidate SHA is missing or invalid")
    if changes not in KNOWN_RESULTS:
        errors.append("changes result is missing or invalid")
    elif changes != "success":
        errors.append(f"changes expected success, got {changes}")
    for name in JOB_NAMES:
        errors.extend(validate_job(name, expected=expected[name], actual=actual[name]))
        observed = tested_sha[name]
        if actual[name] == "success":
            if not candidate_valid or observed != candidate_sha:
                errors.append(f"{name} evidence is not bound to candidate SHA")
        elif observed:
            errors.append(f"{name} skipped evidence must be empty")
    return errors


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--changes-result", required=True)
    parser.add_argument("--candidate-sha", required=True)
    for name in JOB_NAMES:
        parser.add_argument(f"--{name}-expected", required=True)
        parser.add_argument(f"--{name}-result", required=True)
        parser.add_argument(f"--{name}-tested-sha", required=True)
    return parser.parse_args(argv)


def parse_expected(value: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError("expected flag must be true or false")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        expected = {
            name: parse_expected(getattr(args, f"{name}_expected")) for name in JOB_NAMES
        }
    except ValueError:
        print("CI result verification input is invalid", file=sys.stderr)
        return 2

    actual = {name: getattr(args, f"{name}_result") for name in JOB_NAMES}
    tested_sha = {name: getattr(args, f"{name}_tested_sha") for name in JOB_NAMES}
    errors = verify_results(
        changes=args.changes_result,
        expected=expected,
        actual=actual,
        candidate_sha=args.candidate_sha,
        tested_sha=tested_sha,
    )
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    print("CI result verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
