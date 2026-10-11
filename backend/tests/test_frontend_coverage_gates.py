"""覆盖门禁必须拒绝缺失分母、伪造百分比和失败/被跳过的安全测试。"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from check_frontend_coverage import (  # noqa: E402
    CRITICAL_TEST_FILES,
    THRESHOLDS,
    FrontendCoverageError,
    evaluate_coverage,
    verify_tests,
)


def _entry(covered=100, total=100):
    return {metric: {"total": total, "covered": covered, "skipped": 0, "pct": 100}
            for metric in ("statements", "branches")}


def _total(document):
    document["total"] = {
        metric: {"total": sum(value[metric]["total"] for key, value in document.items()
                              if key != "total"),
                 "covered": sum(value[metric]["covered"] for key, value in document.items()
                                if key != "total"), "skipped": 0}
        for metric in ("statements", "branches")
    }
    return document


@pytest.fixture
def evidence(tmp_path):
    document = {}
    for name in ["src/api/client.ts", "src/stores/session.ts",
                 "src/composables/useAdminStepUp.ts", "src/views/Page.vue"]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("export const value = 1\n")
        document[name] = _entry()
    tests = {"success": True, "numTotalTests": len(CRITICAL_TEST_FILES), "testResults": []}
    for name in sorted(CRITICAL_TEST_FILES):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test source")
        tests["testResults"].append({"name": str(path), "status": "passed",
                                     "assertionResults": [{"status": "passed"}]})
    return tmp_path, _total(document), tests


def test_complete_reports_and_independent_groups_pass(evidence):
    root, report, tests = evidence
    assert evaluate_coverage(report, root=root)["session"] == {"statements": 100, "branches": 100}
    verify_tests(tests, root=root)
    assert dict(THRESHOLDS) == {"frontend": (85, 82), "api": (90, 80),
                               "session": (90, 82), "composables": (93, 87)}


def test_new_unimported_source_cannot_disappear_from_denominator(evidence):
    root, report, _ = evidence
    (root / "src/api/new.ts").write_text("export const newlyAdded = 1\n")
    with pytest.raises(FrontendCoverageError, match="inventory"):
        evaluate_coverage(report, root=root)
    report["src/api/new.ts"] = _entry(0)
    with pytest.raises(FrontendCoverageError, match="below required"):
        evaluate_coverage(_total(report), root=root)


def test_api_floor_cannot_be_hidden_by_high_overall_coverage(evidence):
    root, report, _ = evidence
    report["src/views/Page.vue"] = _entry(10000, 10000)
    report["src/api/client.ts"] = _entry(89)
    with pytest.raises(FrontendCoverageError, match="api statements"):
        evaluate_coverage(_total(report), root=root)


@pytest.mark.parametrize("mutation", ["missing", "outside", "duplicate", "total", "skip"])
def test_missing_or_manipulated_coverage_fails(evidence, mutation):
    root, report, _ = evidence
    if mutation == "missing":
        del report["src/stores/session.ts"]
    elif mutation == "outside":
        report["../outside.ts"] = _entry()
    elif mutation == "duplicate":
        report[str(root / "src/api/client.ts")] = _entry()
    elif mutation == "total":
        report["total"]["statements"]["covered"] = 1
    else:
        report["src/api/client.ts"]["statements"]["skipped"] = 1
    with pytest.raises(FrontendCoverageError):
        evaluate_coverage(report, root=root)


@pytest.mark.parametrize("value", [True, -1, 101, "100", float("nan"), None])
def test_invalid_counters_cannot_use_reported_percentage(evidence, value):
    root, report, _ = evidence
    report["src/api/client.ts"]["branches"]["covered"] = value
    with pytest.raises(FrontendCoverageError):
        evaluate_coverage(report, root=root)


@pytest.mark.parametrize("mutation", ["failed", "skip", "missing", "deleted", "duplicate", "count"])
def test_security_tests_must_exist_and_run_successfully(evidence, mutation):
    root, _, tests = evidence
    if mutation == "failed":
        tests["success"] = False
    elif mutation == "skip":
        tests["testResults"][0]["assertionResults"][0]["status"] = "pending"
    elif mutation == "missing":
        tests["testResults"].pop()
    elif mutation == "deleted":
        Path(tests["testResults"].pop()["name"]).unlink()
    elif mutation == "duplicate":
        tests["testResults"].append(copy.deepcopy(tests["testResults"][0]))
    else:
        tests["numTotalTests"] += 1
    with pytest.raises(FrontendCoverageError):
        verify_tests(tests, root=root)


def _write_evidence(root, report, tests, directory):
    import hashlib
    import json

    from check_frontend_coverage import input_hash

    for name in ("package.json", "package-lock.json", "vite.config.ts", "tsconfig.json"):
        (root / name).write_text("synthetic configuration")
    directory.mkdir()
    for name, value in (("coverage-summary.json", report), ("tests.json", tests)):
        (directory / name).write_text(json.dumps(value))
    manifest = {"schema": 1, "commit": "a" * 40, "inputs": input_hash(root),
                "shard": "frontend", "success": True,
                "reports": {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                            for name in ("coverage-summary.json", "tests.json")}}
    (directory / "manifest.json").write_text(json.dumps(manifest))
    return manifest


def test_same_sha_and_configuration_evidence_passes(evidence, tmp_path):
    from check_frontend_coverage import verify_evidence

    root, report, tests = evidence
    directory = tmp_path / "evidence"
    _write_evidence(root, report, tests, directory)
    assert verify_evidence(directory, root=root, commit="a" * 40)["api"]["branches"] == 100


@pytest.mark.parametrize("mutation", [
    "wrong-sha", "missing", "corrupt", "changed-config", "changed-source", "failed-shard",
    "missing-shard", "report-tampered", "wrong-report-hash", "skip-test",
])
def test_evidence_is_bound_and_required_shard_cannot_be_bypassed(evidence, tmp_path, mutation):
    import hashlib
    import json

    from check_frontend_coverage import verify_evidence

    root, report, tests = evidence
    directory = tmp_path / "evidence"
    manifest = _write_evidence(root, report, tests, directory)
    if mutation == "wrong-sha":
        manifest["commit"] = "b" * 40
    elif mutation == "missing":
        (directory / "coverage-summary.json").unlink()
    elif mutation == "corrupt":
        (directory / "tests.json").write_text("not json")
    elif mutation == "changed-config":
        (root / "vite.config.ts").write_text("expanded exclusions")
    elif mutation == "changed-source":
        (root / "src/api/client.ts").write_text("changed authentication")
    elif mutation == "failed-shard":
        manifest["success"] = False
    elif mutation == "missing-shard":
        del manifest["shard"]
    elif mutation == "report-tampered":
        (directory / "tests.json").write_text("{}")
    elif mutation == "wrong-report-hash":
        manifest["reports"]["tests.json"] = "0" * 64
    else:
        tests["testResults"][0]["assertionResults"][0]["status"] = "skipped"
        (directory / "tests.json").write_text(json.dumps(tests))
        manifest["reports"]["tests.json"] = hashlib.sha256(
            (directory / "tests.json").read_bytes()).hexdigest()
    (directory / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(FrontendCoverageError):
        verify_evidence(directory, root=root, commit="a" * 40)


def test_failed_new_run_invalidates_old_success(evidence, tmp_path, monkeypatch):
    import subprocess

    from check_frontend_coverage import run_coverage, verify_evidence

    root, report, tests = evidence
    directory = tmp_path / "evidence"
    _write_evidence(root, report, tests, directory)

    def fail(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, ["synthetic-vitest"])

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        run_coverage(directory, root=root, commit="a" * 40)
    with pytest.raises(FrontendCoverageError, match="shard failed"):
        verify_evidence(directory, root=root, commit="a" * 40)
