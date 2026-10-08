"""Public security artifacts must not contain scanner-controlled secrets."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "security_evidence", ROOT / "scripts/security_evidence.py"
)
assert SPEC is not None and SPEC.loader is not None
EVIDENCE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EVIDENCE)


class SecurityEvidenceTests(unittest.TestCase):
    def test_clean_scan_passes(self) -> None:
        result = EVIDENCE.summarize({"SchemaVersion": 2, "Results": [{"Target": "ignored"}]}, 0)
        self.assertEqual(result["outcome"], "passed")

    def test_findings_preserve_blocking_result_without_secret_fields(self) -> None:
        sentinel = "private-value-that-must-never-be-published"
        raw = {
            "SchemaVersion": 2,
            "Results": [
                {
                    "Target": sentinel,
                    "Metadata": {"secret": sentinel},
                    "Secrets": [{"Severity": "CRITICAL", "Match": sentinel, "Code": sentinel}],
                    "Vulnerabilities": [
                        {
                            "Severity": "HIGH",
                            "VulnerabilityID": "CVE-2026-102265",
                            "Title": sentinel,
                            "PkgName": sentinel,
                            "References": [sentinel],
                        }
                    ],
                    "Misconfigurations": [
                        {"Severity": "HIGH", "Status": "FAIL", "CauseMetadata": sentinel}
                    ],
                    "Licenses": [{"Severity": "HIGH", "Name": sentinel, "FilePath": sentinel}],
                }
            ],
        }
        result = EVIDENCE.summarize(raw, 1)
        self.assertEqual(result["outcome"], "findings")
        self.assertEqual(result["counts"]["Secrets"]["CRITICAL"], 1)
        self.assertEqual(result["vulnerability_ids"], ["CVE-2026-102265"])
        self.assertNotIn(sentinel, json.dumps(result))

    def test_missing_malformed_or_inconsistent_report_fails_closed(self) -> None:
        for raw, code in [
            (None, 0),
            ({}, 0),
            ({"SchemaVersion": 2, "Results": []}, 0),
            ({"SchemaVersion": 2, "Results": [{}]}, 1),
            ({"SchemaVersion": 2, "Results": [{}]}, 125),
            ({"SchemaVersion": 2, "Results": [{"Secrets": {}}]}, 0),
            ({"SchemaVersion": 2, "Results": [{"Secrets": [{"Severity": "HIGH"}]}]}, 0),
        ]:
            with self.subTest(code=code, raw=raw):
                self.assertEqual(EVIDENCE.summarize(raw, code)["outcome"], "tool_error")

    def test_untrusted_identifier_and_severity_are_not_echoed(self) -> None:
        value = "private-input-value"
        result = EVIDENCE.summarize(
            {
                "SchemaVersion": 2,
                "Results": [
                    {
                        "Vulnerabilities": [{"Severity": value, "VulnerabilityID": value}],
                    }
                ],
            },
            1,
        )
        self.assertNotIn(value, json.dumps(result))
        self.assertEqual(result["counts"]["Vulnerabilities"]["UNKNOWN"], 1)

    def test_passing_misconfiguration_is_not_counted(self) -> None:
        result = EVIDENCE.summarize(
            {
                "SchemaVersion": 2,
                "Results": [
                    {
                        "Misconfigurations": [{"Severity": "HIGH", "Status": "PASS"}],
                    }
                ],
            },
            0,
        )
        self.assertEqual(result["outcome"], "passed")

    def test_bound_evidence_and_scanner_error_are_saved_safely(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw, code, db = (root / name for name in ("raw.json", "exit-code", "db.json"))
            raw.write_text(json.dumps({"SchemaVersion": 2, "Results": [{}]}), encoding="utf-8")
            code.write_text("0", encoding="ascii")
            db.write_text(
                json.dumps(
                    {"Version": 2, "UpdatedAt": "2026-10-08T00:00:00Z", "Private": "never-copy"}
                ),
                encoding="utf-8",
            )
            result = EVIDENCE.build_evidence(
                raw_path=raw, status_path=code, db_path=db, sha="a" * 40, run_id="1", attempt="2"
            )
            self.assertEqual(result["outcome"], "passed")
            self.assertEqual(result["run_attempt"], 2)
            self.assertNotIn("never-copy", json.dumps(result))
            code.write_text("125", encoding="ascii")
            self.assertEqual(
                EVIDENCE.build_evidence(
                    raw_path=raw,
                    status_path=code,
                    db_path=db,
                    sha="a" * 40,
                    run_id="1",
                    attempt="2",
                )["outcome"],
                "tool_error",
            )
            db.unlink()
            code.write_text("0", encoding="ascii")
            self.assertEqual(
                EVIDENCE.build_evidence(
                    raw_path=raw,
                    status_path=code,
                    db_path=db,
                    sha="a" * 40,
                    run_id="1",
                    attempt="2",
                )["outcome"],
                "tool_error",
            )

    def test_reader_rejects_oversize_and_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "raw.json"
            path.write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError):
                EVIDENCE._read_json(path, 1)
            link = path.with_name("link.json")
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                EVIDENCE._read_json(link)

    def test_enforcement_requires_matching_candidate_and_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "summary.json"
            result = {
                "outcome": "passed",
                "candidate_sha": "a" * 40,
                "run_id": 1,
                "run_attempt": 1,
                "report_valid": True,
                "scanner_exit_code": 0,
            }
            path.write_text(json.dumps(result), encoding="utf-8")
            env = {"GITHUB_SHA": "a" * 40, "GITHUB_RUN_ID": "1", "GITHUB_RUN_ATTEMPT": "1"}
            with patch.dict(os.environ, env), patch("sys.argv", ["evidence", "--check", str(path)]):
                self.assertEqual(EVIDENCE.main(), 0)
                os.environ["GITHUB_SHA"] = "b" * 40
                self.assertEqual(EVIDENCE.main(), 1)


if __name__ == "__main__":
    unittest.main()
