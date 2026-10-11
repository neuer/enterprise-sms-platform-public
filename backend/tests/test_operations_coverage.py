"""完整运维分母、严格分片绑定及真实子进程/cwd 覆盖回归。"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from check_operations_coverage import (  # noqa: E402
    CRITICAL_FILES,
    THRESHOLDS,
    app_document,
    evaluate,
    normalize,
)
from coverage_runtime import begin, source_files, stamp, verify, write_config  # noqa: E402


def entry(covered=100, total=100):
    return {"summary": {"num_statements": total, "covered_lines": covered,
                        "num_branches": total, "covered_branches": covered}}


@pytest.fixture
def source(tmp_path):
    paths = ["backend/app/main.py", "scripts/main.py", *CRITICAL_FILES.values()]
    for name in paths:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("value = 1\n")
    for name in ("backend/pyproject.toml", "backend/uv.lock", "scripts/run_backend_tests.sh",
                 "scripts/verify_vendor_postgres_recovery.sh"):
        (tmp_path / name).write_text("synthetic configuration")
    return tmp_path, {"meta": {"branch_coverage": True},
                      "files": {str(tmp_path / name): entry() for name in paths}}


def test_complete_inventory_and_independent_groups_pass(source):
    root, report = source
    document = normalize(report, root=root)
    assert set(document["files"]) == source_files(root)
    assert set(app_document(document)["files"]) == {"app/main.py"}
    assert set(evaluate(document)) == set(THRESHOLDS)
    assert THRESHOLDS["storage_initialize"] == (46, 35)


@pytest.mark.parametrize("mutation", ["missing", "outside", "relative", "duplicate", "no-branches"])
def test_path_and_denominator_cannot_be_weakened(source, mutation):
    root, report = source
    name = next(iter(report["files"]))
    if mutation == "missing":
        del report["files"][name]
    elif mutation == "outside":
        report["files"][str(root.parent / "elsewhere.py")] = entry()
    elif mutation == "relative":
        report["files"]["scripts/main.py"] = report["files"].pop(str(root / "scripts/main.py"))
    elif mutation == "duplicate":
        report["files"][str(root / "scripts/../scripts/main.py")] = entry()
    else:
        report["meta"]["branch_coverage"] = False
    with pytest.raises(ValueError):
        normalize(report, root=root)


def test_new_unimported_file_lowers_group_and_cannot_disappear(source):
    root, report = source
    name = root / "scripts/unimported.py"
    name.write_text("untested = True\n")
    with pytest.raises(ValueError, match="inventory"):
        normalize(report, root=root)
    report["files"][str(name)] = entry(0, 1000)
    with pytest.raises(ValueError, match="scripts statements"):
        evaluate(normalize(report, root=root))


@pytest.mark.parametrize("group", CRITICAL_FILES)
def test_critical_entry_cannot_hide_under_aggregate(source, group):
    root, report = source
    report["files"][str(root / "scripts/main.py")] = entry(100000, 100000)
    for name in CRITICAL_FILES.values():
        report["files"][str(root / name)] = entry(100000, 100000)
    report["files"][str(root / CRITICAL_FILES[group])] = entry(1, 100)
    with pytest.raises(ValueError, match=group):
        evaluate(normalize(report, root=root))


@pytest.mark.parametrize("value", [True, -1, 101, "100", None, float("nan")])
def test_invalid_coverage_counters_fail(source, value):
    root, report = source
    report["files"][str(root / "scripts/main.py")]["summary"]["covered_lines"] = value
    with pytest.raises(ValueError):
        evaluate(normalize(report, root=root))


def _shard(root, directory, name="unit"):
    begin(directory, root=root, commit="a" * 40, shard=name)
    (directory / ".coverage").write_bytes(b"synthetic data file")
    (directory / "tests.json").write_text(json.dumps({
        "sha": "a" * 40, "shard": name, "exit_code": 0,
    }))
    stamp(directory, root=root, commit="a" * 40, shard=name)


def test_complete_same_sha_shard_passes(source):
    root, _ = source
    directory = root / "evidence"
    _shard(root, directory)
    verify(directory, root=root, commit="a" * 40, shard="unit")


@pytest.mark.parametrize("mutation", ["data", "sha", "source", "failure", "missing", "wrong-shard"])
def test_stale_or_failed_shard_cannot_merge(source, mutation):
    root, _ = source
    directory = root / "evidence"
    _shard(root, directory)
    if mutation == "data":
        (directory / ".coverage").write_bytes(b"different coverage")
    elif mutation == "source":
        (root / "scripts/main.py").write_text("different = True\n")
    elif mutation == "failure":
        begin(directory, root=root, commit="a" * 40, shard="unit")
    elif mutation == "missing":
        (directory / "coverage-manifest.json").unlink()
    with pytest.raises((ValueError, OSError)):
        verify(directory, root=root, commit=("b" if mutation == "sha" else "a") * 40,
               shard="postgres" if mutation == "wrong-shard" else "unit")


def test_failed_test_shard_never_gets_success_stamp(source):
    root, _ = source
    directory = root / "evidence"
    begin(directory, root=root, commit="a" * 40, shard="unit")
    (directory / ".coverage").write_bytes(b"synthetic")
    (directory / "tests.json").write_text(json.dumps({
        "sha": "a" * 40, "shard": "unit", "exit_code": 1,
    }))
    with pytest.raises(ValueError):
        stamp(directory, root=root, commit="a" * 40, shard="unit")


def test_real_subprocess_different_cwds_and_same_names_are_distinct(tmp_path):
    """真实 coverage patch 收集两个并行子进程，未导入文件也必须有零覆盖记录。"""
    root = tmp_path.resolve()
    for folder in ("backend/app", "scripts", "deploy/scripts"):
        (root / folder).mkdir(parents=True)
    (root / "backend/app/__init__.py").write_text("")
    (root / "scripts/same.py").write_text("if True:\n    script_value = 1\n")
    (root / "deploy/scripts/same.py").write_text("if True:\n    deploy_value = 2\n")
    (root / "scripts/unimported.py").write_text("never_loaded = 3\n")
    runner = root / "runner.py"
    runner.write_text(
        "import subprocess, sys\nfrom pathlib import Path\n"
        "root = Path(__file__).parent\n"
        "children = [subprocess.Popen([sys.executable, 'same.py'], cwd=root / folder) "
        "for folder in ('scripts', 'deploy/scripts')]\n"
        "assert all(child.wait() == 0 for child in children)\n"
    )
    config = root / "coverage.ini"
    write_config(config, root)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("COVERAGE_", "COV_CORE"))}
    env["COVERAGE_FILE"] = str(root / ".coverage")
    commands = (["run", str(runner)], ["combine"], ["json", "-o", str(root / "report.json")])
    for arguments in commands:
        subprocess.run([sys.executable, "-m", "coverage", arguments[0],
                        f"--rcfile={config}", *arguments[1:]], cwd=root, env=env, check=True,
                       capture_output=True, timeout=30)
    document = normalize(
        json.loads((root / "report.json").read_text()), root=root, report_cwd=root,
    )
    assert document["files"]["scripts/same.py"]["summary"]["covered_lines"] > 0
    assert document["files"]["deploy/scripts/same.py"]["summary"]["covered_lines"] > 0
    untouched = document["files"]["scripts/unimported.py"]["summary"]
    assert untouched["num_statements"] > 0 and untouched["covered_lines"] == 0
    altered = copy.deepcopy(document)
    del altered["files"]["scripts/same.py"]
    assert set(altered["files"]) != source_files(root)
