"""用真实 Git 仓库验证候选内容、暂存边界和失败关闭。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from gate_snapshot import git, working_tree  # noqa: E402


@pytest.fixture
def repository(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@localhost")
    (tmp_path / "candidate.py").write_text("original\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "initial")
    return tmp_path


def test_working_snapshot_does_not_overwrite_staged_candidate(repository):
    path = repository / "candidate.py"
    path.write_text("import unused\n")
    git(repository, "add", ".")
    staged = git(repository, "write-tree")
    path.write_text("fixed\n")
    working = working_tree(repository)
    assert working != staged
    assert git(repository, "write-tree") == staged
    assert git(repository, "show", f"{staged}:candidate.py") == "import unused"
    assert git(repository, "show", f"{working}:candidate.py") == "fixed"
    path.write_text("changed again\n")
    assert working_tree(repository) != working


def test_missing_merge_base_is_failure(repository):
    with pytest.raises(subprocess.CalledProcessError):
        git(repository, "merge-base", "missing-base", "HEAD")


def prepare_candidate_runner(repository):
    for name in ("pre-commit", "pre-push"):
        path = repository / ".githooks" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    (repository / "backend").mkdir()
    (repository / "backend/.keep").touch()
    (repository / "scripts").mkdir()
    (repository / "scripts/dev_check_impl.sh").write_text(
        "#!/bin/sh\n! grep -q 'import unused' candidate.py\n"
    )
    (repository / "scripts/local_test.sh").write_text("#!/bin/sh\nexit 0\n")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "test gate setup")
    git(repository, "config", "core.hooksPath", ".githooks")
    git(repository, "update-ref", "refs/remotes/origin/main", "HEAD")


def test_commit_runs_staged_content_and_dev_cache_cannot_hide_it(repository, monkeypatch):
    from gate_snapshot import check_candidate

    prepare_candidate_runner(repository)
    original_run = subprocess.run

    def run(argv, **kwargs):
        # 不安装依赖，候选检查本身仍由真实 shell 在独立 Git 工作树执行。
        if argv[:2] == ["uv", "sync"]:
            return subprocess.CompletedProcess(argv, 0)
        return original_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr("gate_snapshot.cache_key", lambda tree, base, args: tree)
    path = repository / "candidate.py"
    path.write_text("import unused\n")
    git(repository, "add", ".")
    path.write_text("fixed\n")
    check_candidate(repository, mode="dev")
    with pytest.raises(subprocess.CalledProcessError):
        check_candidate(repository, mode="commit")
    assert git(repository, "show", ":candidate.py") == "import unused"
    assert path.read_text() == "fixed\n"


def test_push_checks_requested_commit_even_when_head_is_good(repository, monkeypatch):
    from gate_snapshot import check_candidate

    prepare_candidate_runner(repository)
    original_run = subprocess.run

    def run(argv, **kwargs):
        if argv[:2] == ["uv", "sync"]:
            return subprocess.CompletedProcess(argv, 0)
        return original_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr("gate_snapshot.cache_key", lambda tree, base, args: tree)
    path = repository / "candidate.py"
    path.write_text("import unused\n")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "bad candidate")
    bad = git(repository, "rev-parse", "HEAD")
    path.write_text("fixed\n")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "good HEAD")
    with pytest.raises(subprocess.CalledProcessError):
        check_candidate(repository, mode="push", commit=bad)


def prepare_dev_runner(repository, monkeypatch):
    import os
    import shutil

    prepare_candidate_runner(repository)
    source = Path(__file__).resolve().parents[2] / "scripts/dev_check_impl.sh"
    shutil.copyfile(source, repository / "scripts/dev_check_impl.sh")
    for name in ("check_pre_vcs_gates", "check_spec_consistency", "check_invariants",
                 "check_public_readiness", "check_backend_static"):
        (repository / "scripts" / f"{name}.py").write_text("raise SystemExit(0)\n")
    binary = repository / "test-bin"
    binary.mkdir()
    (binary / "uv").write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$GATE_CALLS"\n')
    (binary / "uv").chmod(0o755)
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    calls = repository / "calls.log"
    monkeypatch.setenv("GATE_CALLS", str(calls))
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "runner")
    git(repository, "update-ref", "refs/remotes/origin/main", "HEAD")
    return calls


def test_every_changed_shell_script_is_parsed(repository, monkeypatch):
    prepare_dev_runner(repository, monkeypatch)
    (repository / "scripts/a.sh").write_text("#!/bin/sh\ntrue\n")
    (repository / "scripts/b.sh").write_text("#!/bin/sh\nif then\n")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "two shell scripts")
    result = subprocess.run(["bash", "scripts/dev_check_impl.sh", "--changed"], cwd=repository)
    assert result.returncode != 0


def test_script_plus_unrelated_test_does_not_narrow_backend_suite(repository, monkeypatch):
    calls = prepare_dev_runner(repository, monkeypatch)
    (repository / "scripts/check_example.py").write_text("pass\n")
    tests = repository / "backend/tests"
    tests.mkdir()
    (tests / "test_unrelated.py").write_text("def test_unrelated(): pass\n")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "script and test")
    subprocess.run(["bash", "scripts/dev_check_impl.sh", "--changed"], cwd=repository, check=True)
    lines = calls.read_text().splitlines()
    assert "run --locked python -m pytest -q" in lines
    assert not any("test_unrelated.py" in line for line in lines)
