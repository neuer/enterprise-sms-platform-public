#!/usr/bin/env python3
"""Publish aggregate scan evidence; never copy secret matches, snippets or paths."""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TRIVY_IMAGE = (
    "aquasec/trivy:0.70.0@sha256:"
    "be1190afcb28352bfddc4ddeb71470835d16462af68d310f9f4bca710961a41e"
)
MAX_REPORT_BYTES = 32 * 1024 * 1024
SEVERITIES = ("UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL")
KINDS = ("Vulnerabilities", "Misconfigurations", "Secrets", "Licenses")
IDENTIFIER = re.compile(
    r"(?:CVE-[0-9]{4}-[0-9]{4,9}|GHSA-[23456789cfghjmpqrvwx]{4}"
    r"(?:-[23456789cfghjmpqrvwx]{4}){2})\Z"
)


def _read_json(path: Path, limit: int = MAX_REPORT_BYTES) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ValueError("invalid evidence file")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("evidence exceeds limit")
    return json.loads(raw)


def summarize(raw: object, exit_code: int | None) -> dict[str, Any]:
    """Allowlist-only projection. Scanner strings never enter public diagnostics."""
    counts = {kind: dict.fromkeys(SEVERITIES, 0) for kind in KINDS}
    ids: set[str] = set()
    valid = True
    try:
        if not isinstance(raw, dict) or raw.get("SchemaVersion") != 2:
            raise ValueError("unsupported scanner schema")
        results = raw.get("Results")
        if not isinstance(results, list) or not results:
            raise ValueError("missing scanner results")
        for result in results:
            if not isinstance(result, dict):
                raise ValueError("invalid result")
            for kind in KINDS:
                entries = result.get(kind, [])
                if entries is None:
                    entries = []
                if not isinstance(entries, list):
                    raise ValueError("invalid findings")
                for entry in entries:
                    if not isinstance(entry, dict):
                        raise ValueError("invalid finding")
                    severity = entry.get("Severity", "UNKNOWN")
                    if not isinstance(severity, str) or severity not in SEVERITIES:
                        severity = "UNKNOWN"
                    if kind == "Misconfigurations" and entry.get("Status") == "PASS":
                        continue
                    counts[kind][severity] += 1
                    if kind == "Vulnerabilities":
                        ident = entry.get("VulnerabilityID")
                        if isinstance(ident, str) and IDENTIFIER.fullmatch(ident):
                            ids.add(ident)
    except (ValueError, TypeError, RecursionError):
        valid = False
    total = sum(sum(values.values()) for values in counts.values())
    if not valid or exit_code not in (0, 1):
        outcome = "tool_error"
    elif exit_code == 1:
        outcome = "findings" if total else "tool_error"
    elif any(values["HIGH"] or values["CRITICAL"] for values in counts.values()):
        outcome = "tool_error"
    else:
        outcome = "passed"
    return {
        "outcome": outcome,
        "scanner_exit_code": exit_code,
        "report_valid": valid,
        "counts": counts,
        "vulnerability_ids": sorted(ids),
    }


def _database_metadata(path: Path) -> dict[str, object] | None:
    try:
        raw = _read_json(path, 64 * 1024)
        if not isinstance(raw, dict) or type(raw.get("Version")) is not int:
            return None
        if not 1 <= raw["Version"] <= 100:
            return None
        stamp = raw.get("UpdatedAt")
        if not isinstance(stamp, str) or len(stamp) > 64:
            return None
        instant = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            return None
        return {"version": raw["Version"], "updated_at": instant.astimezone(UTC).isoformat()}
    except (OSError, ValueError, TypeError, RecursionError, OverflowError):
        return None


def build_evidence(
    *, raw_path: Path, status_path: Path, db_path: Path,
    sha: str, run_id: str, attempt: str,
) -> dict[str, Any]:
    if re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        raise ValueError("invalid candidate binding")
    if any(re.fullmatch(r"[1-9][0-9]{0,19}", value) is None for value in (run_id, attempt)):
        raise ValueError("invalid run binding")
    try:
        if status_path.is_symlink() or status_path.stat().st_size > 8:
            raise ValueError("invalid scanner status")
        code = int(status_path.read_text(encoding="ascii").strip())
        if not 0 <= code <= 255:
            raise ValueError("invalid scanner status")
    except (OSError, ValueError, UnicodeError):
        code = None
    try:
        raw = _read_json(raw_path)
    except (OSError, ValueError, TypeError, RecursionError):
        raw = None
    result = summarize(raw, code)
    database = _database_metadata(db_path)
    if database is None and result["outcome"] == "passed":
        result["outcome"] = "tool_error"
    return {
        "schema_version": 1,
        "candidate_sha": sha,
        "run_id": int(run_id),
        "run_attempt": int(attempt),
        "created_at": datetime.now(UTC).isoformat(),
        "scanner": {"image": TRIVY_IMAGE, "version": "0.70.0", "database": database},
        "scope": {
            "target": "repository", "scanners": ["vuln", "misconfig", "secret", "license"],
            "severity": ["HIGH", "CRITICAL"], "include_dev_dependencies": False,
        },
        "publication": "aggregate-only; no matches, snippets, raw paths, tokens or secret hashes",
        **result,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path)
    parser.add_argument("--status-file", type=Path)
    parser.add_argument("--db-metadata", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", type=Path)
    args = parser.parse_args()
    try:
        if args.check is not None:
            result = _read_json(args.check)
            return 0 if (
                isinstance(result, dict) and result.get("outcome") == "passed"
                and result.get("candidate_sha") == os.environ.get("GITHUB_SHA")
                and result.get("run_id") == int(os.environ["GITHUB_RUN_ID"])
                and result.get("run_attempt") == int(os.environ["GITHUB_RUN_ATTEMPT"])
                and result.get("report_valid") is True
                and result.get("scanner_exit_code") == 0
            ) else 1
        if None in (args.raw, args.status_file, args.db_metadata, args.output):
            raise ValueError("missing input")
        result = build_evidence(
            raw_path=args.raw, status_path=args.status_file, db_path=args.db_metadata,
            sha=os.environ["GITHUB_SHA"], run_id=os.environ["GITHUB_RUN_ID"],
            attempt=os.environ["GITHUB_RUN_ATTEMPT"],
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return 0
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
        print("Security evidence unavailable; release gate remains closed.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
