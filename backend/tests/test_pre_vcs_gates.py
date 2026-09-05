from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from check_pre_vcs_gates import (  # noqa: E402
    CHECK_CI_CONTRACTS,
    CHECK_FRONTEND,
    CHECK_MIGRATION,
    CHECK_PYTEST_CHANGED,
    CHECK_RUFF,
    CHECK_SPEC,
    CHECK_VENDOR_PG,
    GateError,
    decide_cursor_command,
    hooks_path_enabled,
    isolated_check_env,
    parse_git_invocation,
    plan_for_paths,
    require_git_hooks,
)


def plan(paths: list[str], diffs: dict[str, str] | None = None):
    return plan_for_paths(paths, root=ROOT, diffs=diffs)


def test_docs_only_does_not_require_migration_or_vendor_recovery() -> None:
    result = plan(["docs/plans/note.md", "README.md", "PROGRESS.md"])

    assert result.required() == ()
    assert CHECK_MIGRATION not in result.checks
    assert CHECK_VENDOR_PG not in result.checks
    assert CHECK_RUFF not in result.checks


def test_spec_docs_run_cheap_consistency_only() -> None:
    result = plan(["AGENTS.md"])

    assert result.required() == (CHECK_SPEC,)
    assert CHECK_MIGRATION not in result.checks
    assert CHECK_VENDOR_PG not in result.checks


def test_frontend_only_does_not_run_heavy_pg_gates() -> None:
    result = plan(["frontend/src/views/DashboardView.vue"])

    assert result.required() == (CHECK_FRONTEND,)
    assert CHECK_MIGRATION not in result.checks
    assert CHECK_VENDOR_PG not in result.checks
    assert CHECK_RUFF not in result.checks


def test_ordinary_backend_python_requires_ruff_not_heavy_gates() -> None:
    result = plan(["backend/app/core/auth/jwt.py"])

    assert result.required() == (CHECK_RUFF,)
    assert result.ruff_files == ["backend/app/core/auth/jwt.py"]
    assert CHECK_MIGRATION not in result.checks
    assert CHECK_VENDOR_PG not in result.checks


def test_changed_unit_tests_are_selected_without_vendor_recovery() -> None:
    result = plan(["backend/tests/test_auth.py"])

    assert CHECK_RUFF in result.checks
    assert CHECK_PYTEST_CHANGED in result.checks
    assert result.pytest_files == ["tests/test_auth.py"]
    assert CHECK_VENDOR_PG not in result.checks
    assert CHECK_MIGRATION not in result.checks


def test_schema_without_inflight_diff_requires_migration_only() -> None:
    result = plan(
        ["schema.sql"],
        diffs={"schema.sql": "- comment on auth\n+ comment on session"},
    )

    assert result.required() == (CHECK_MIGRATION,)
    assert CHECK_VENDOR_PG not in result.checks


def test_schema_inflight_diff_unions_migration_and_vendor_recovery() -> None:
    result = plan(
        ["schema.sql"],
        diffs={
            "schema.sql": "+ CREATE TRIGGER trg_send_inflight_reservation_conservation\n"
        },
    )

    assert CHECK_MIGRATION in result.checks
    assert CHECK_VENDOR_PG in result.checks


def test_migration_parser_tests_are_not_a_catalog_match_substitute() -> None:
    result = plan(["backend/tests/test_migration_baseline.py"])

    assert CHECK_MIGRATION in result.checks
    assert CHECK_PYTEST_CHANGED in result.checks
    assert CHECK_VENDOR_PG not in result.checks


def test_new_inflight_split_test_requires_full_vendor_recovery() -> None:
    result = plan(["backend/tests/integration/test_inflight_split_commit_postgres.py"])

    assert CHECK_VENDOR_PG in result.checks
    assert CHECK_PYTEST_CHANGED not in result.checks
    assert CHECK_MIGRATION not in result.checks


def test_send_inflight_service_requires_vendor_recovery_and_ruff() -> None:
    result = plan(["backend/app/services/send_inflight.py"])

    assert set(result.required()) == {CHECK_RUFF, CHECK_VENDOR_PG}
    assert CHECK_MIGRATION not in result.checks


def test_auth_migration_without_inflight_tokens_skips_vendor_recovery() -> None:
    result = plan(
        ["backend/migrations/versions/0109_auth_session_policy.py"],
        diffs={
            "backend/migrations/versions/0109_auth_session_policy.py": (
                "+ op.add_column('auth_session_policy', sa.Column('x', sa.Int()))\n"
            )
        },
    )

    assert CHECK_MIGRATION in result.checks
    assert CHECK_VENDOR_PG not in result.checks


def test_sms_chunk_alter_in_migration_requires_vendor_recovery() -> None:
    result = plan(
        ["backend/migrations/versions/0110_chunk_lease.py"],
        diffs={
            "backend/migrations/versions/0110_chunk_lease.py": (
                "+ op.execute('ALTER TABLE sms_chunk ADD COLUMN lease_owner text')\n"
            )
        },
    )

    assert CHECK_MIGRATION in result.checks
    assert CHECK_VENDOR_PG in result.checks


def test_ci_workflow_change_runs_contract_tests_only() -> None:
    result = plan([".github/workflows/ci.yml"])

    assert result.required() == (CHECK_CI_CONTRACTS,)
    assert result.contract_tests == ["tests/test_ci_workflows.py"]
    assert CHECK_MIGRATION not in result.checks
    assert CHECK_VENDOR_PG not in result.checks


def test_mixed_paths_union_without_forcing_unrelated_gates() -> None:
    result = plan(
        [
            "README.md",
            "frontend/src/App.vue",
            "backend/app/core/auth/jwt.py",
            "schema.sql",
        ],
        diffs={"schema.sql": "+ -- auth comment\n"},
    )

    assert CHECK_FRONTEND in result.checks
    assert CHECK_RUFF in result.checks
    assert CHECK_MIGRATION in result.checks
    assert CHECK_VENDOR_PG not in result.checks
    assert "README.md" not in result.checks.get(CHECK_MIGRATION, [])


def test_recovery_script_change_is_vendor_and_contract() -> None:
    result = plan(["scripts/verify_vendor_postgres_recovery.sh"])

    assert CHECK_VENDOR_PG in result.checks
    assert CHECK_CI_CONTRACTS in result.checks
    assert CHECK_MIGRATION not in result.checks


def test_parse_allows_status_and_denies_commit_no_verify() -> None:
    assert parse_git_invocation("git status --short") is None
    assert parse_git_invocation("git diff --cached") is None
    assert parse_git_invocation("git log -1") is None
    assert parse_git_invocation("git add backend/app/main.py") is None
    denied = parse_git_invocation("git commit --no-verify -m ready")
    assert denied is not None and denied.skips_hooks is True
    denied_short = parse_git_invocation("git commit -n -m ready")
    assert denied_short is not None and denied_short.skips_hooks is True
    denied_cluster = parse_git_invocation("git commit -qn -m ready")
    assert denied_cluster is not None and denied_cluster.skips_hooks is True
    allowed = parse_git_invocation("git commit -m ready")
    assert allowed is not None and allowed.skips_hooks is False
    push_skip = parse_git_invocation("git push --no-verify origin HEAD")
    assert push_skip is not None and push_skip.skips_hooks is True
    push_dry = parse_git_invocation("git push -n origin HEAD")
    assert push_dry is not None and push_dry.skips_hooks is False


def test_parse_denies_hookspath_null_and_commit_tree_is_not_gated() -> None:
    skipped = parse_git_invocation("git -c core.hooksPath=/dev/null commit -m x")
    assert skipped is not None and skipped.skips_hooks is True
    assert parse_git_invocation("git commit-tree HEAD^{tree}") is None


def test_cursor_hook_allows_status_and_denies_no_verify(tmp_path: Path) -> None:
    allow = decide_cursor_command("git status", root=ROOT)
    assert allow == {"permission": "allow"}

    deny = decide_cursor_command("git commit --no-verify -m x", root=ROOT)
    assert deny["permission"] == "deny"
    assert "--no-verify" in deny["user_message"]
    assert set(deny) <= {"permission", "user_message", "agent_message"}


def test_cursor_hook_allows_commit_without_running_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[object] = []

    def ok_runner(root: Path, mode: str, invocation: object) -> SimpleNamespace:
        called.append((mode, invocation))
        return plan(["schema.sql"])

    monkeypatch.setattr("check_pre_vcs_gates.hooks_path_enabled", lambda root: True)
    monkeypatch.setattr("check_pre_vcs_gates.require_git_hooks", lambda root: None)
    allow = decide_cursor_command("git commit -m auth", root=ROOT, runner=ok_runner)
    assert allow == {"permission": "allow"}
    assert called == []


def test_cursor_hook_denies_when_hooks_path_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("check_pre_vcs_gates.hooks_path_enabled", lambda root: False)
    monkeypatch.setattr(
        "check_pre_vcs_gates.local_hooks_path",
        lambda root: "",
    )
    deny = decide_cursor_command("git commit -m x", root=ROOT)
    assert deny["permission"] == "deny"
    assert "install_git_hooks.sh" in deny["user_message"]
    assert "check_migration" not in deny["user_message"]


def test_cursor_wrapper_and_hooks_json_are_fail_closed() -> None:
    wrapper = (ROOT / ".cursor/hooks/block-git-without-gates.sh").read_text(
        encoding="utf-8"
    )
    spec = json.loads((ROOT / ".cursor/hooks.json").read_text(encoding="utf-8"))
    hook = spec["hooks"]["beforeShellExecution"][0]

    assert wrapper.startswith("#!/usr/bin/env bash")
    assert "command -v python3" in wrapper
    assert "check_pre_vcs_gates.py" in wrapper
    assert hook["failClosed"] is True
    assert hook["timeout"] == 900
    assert hook["matcher"] == "git commit|git push"
    assert hook["command"] == ".cursor/hooks/block-git-without-gates.sh"


def test_install_git_hooks_change_runs_hook_contract() -> None:
    result = plan(["scripts/install_git_hooks.sh"])

    assert result.required() == (CHECK_CI_CONTRACTS,)
    assert result.contract_tests == ["tests/test_pre_vcs_gates.py"]
    assert CHECK_MIGRATION not in result.checks


def test_git_hooks_call_the_same_classifier() -> None:
    pre_commit = ROOT / ".githooks/pre-commit"
    pre_push = ROOT / ".githooks/pre-push"

    assert pre_commit.is_file() and os.access(pre_commit, os.X_OK)
    assert pre_push.is_file() and os.access(pre_push, os.X_OK)
    assert "check_pre_vcs_gates.py" in pre_commit.read_text(encoding="utf-8")
    assert "--git-hook commit" in pre_commit.read_text(encoding="utf-8")
    assert "check_pre_vcs_gates.py" in pre_push.read_text(encoding="utf-8")
    assert "--git-hook push" in pre_push.read_text(encoding="utf-8")
    assert "check_public_readiness.py" in pre_push.read_text(encoding="utf-8")
    assert "install_git_hooks.sh" in pre_commit.read_text(encoding="utf-8")


def test_install_script_enables_local_githooks_from_worktrees() -> None:
    source = (ROOT / "scripts/install_git_hooks.sh").read_text(encoding="utf-8")

    assert "git config --local core.hooksPath .githooks" in source
    assert "chmod +x .githooks/pre-commit .githooks/pre-push" in source
    assert "rev-parse --is-inside-work-tree" in source
    assert "-d .git" not in source
    assert "Cursor Settings is not required" in source


def test_require_git_hooks_accepts_configured_githooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("check_pre_vcs_gates.local_hooks_path", lambda root: ".githooks")
    assert hooks_path_enabled(ROOT) is True
    require_git_hooks(ROOT)


def test_require_git_hooks_fails_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("check_pre_vcs_gates.local_hooks_path", lambda root: "")
    with pytest.raises(GateError, match="install_git_hooks.sh"):
        require_git_hooks(ROOT)


def test_isolated_check_env_drops_git_hook_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_DIR", "/tmp/fake.git")
    monkeypatch.setenv("GIT_INDEX_FILE", "/tmp/fake.index")

    env = isolated_check_env()

    assert "GIT_DIR" not in env
    assert "GIT_INDEX_FILE" not in env
    assert env.get("PATH")


def test_execute_plan_docs_only_runs_no_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from check_pre_vcs_gates import execute_plan

    calls: list[list[str]] = []
    monkeypatch.setattr(
        "check_pre_vcs_gates.run_command",
        lambda argv, cwd, env=None: calls.append(list(argv)),
    )

    execute_plan(ROOT, plan(["docs/plans/note.md", "README.md"]))
    assert calls == []


def test_execute_plan_invokes_check_migration_for_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from check_pre_vcs_gates import execute_plan

    calls: list[list[str]] = []

    monkeypatch.setattr(
        "check_pre_vcs_gates.resolve_backend_tools",
        lambda root: (["ruff"], ["python"], ["pytest"], None),
    )
    monkeypatch.setattr("check_pre_vcs_gates.require_docker", lambda: None)
    monkeypatch.setattr(
        "check_pre_vcs_gates.run_command",
        lambda argv, cwd, env=None: calls.append(list(argv)),
    )

    result = plan(
        ["schema.sql"],
        diffs={"schema.sql": "+ -- session comment\n"},
    )
    execute_plan(ROOT, result)

    assert any(argv[-1].endswith("scripts_support/check_migration.py") for argv in calls)
    assert not any("verify_vendor_postgres_recovery.sh" in " ".join(argv) for argv in calls)


def test_execute_plan_fails_closed_when_vendor_recovery_lacks_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from check_pre_vcs_gates import execute_plan

    monkeypatch.setattr(
        "check_pre_vcs_gates.resolve_backend_tools",
        lambda root: (["ruff"], ["python"], ["pytest"], None),
    )
    monkeypatch.setattr("check_pre_vcs_gates.run_command", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "check_pre_vcs_gates.require_docker",
        lambda: (_ for _ in ()).throw(GateError("docker is required")),
    )

    result = plan(["backend/app/services/send_inflight.py"])
    with pytest.raises(GateError, match="docker is required"):
        execute_plan(ROOT, result)
