from __future__ import annotations

import json
import os
import subprocess
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
    FRONTEND_CI_OVERLAP,
    FRONTEND_HOOK_SCRIPTS,
    RECEIPT_KIND,
    RECEIPT_REF_PREFIX,
    RECEIPT_SCHEMA,
    GateError,
    ReceiptSkip,
    decide_cursor_command,
    evaluate_receipt,
    format_ruff_exclude_args,
    hooks_path_enabled,
    isolated_check_env,
    load_receipt_from_ref,
    main,
    parse_git_invocation,
    plan_for_paths,
    publish_push_receipt,
    receipt_push_only,
    receipt_skips,
    require_git_hooks,
    write_receipt_outputs,
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
    assert "--publish-remote" in pre_push.read_text(encoding="utf-8")
    assert "--receipt-push-only" in pre_push.read_text(encoding="utf-8")
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


COMMIT = "a" * 40
TREE = "b" * 40
OTHER = "c" * 40


def _receipt(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": RECEIPT_KIND,
        "schema": RECEIPT_SCHEMA,
        "commit": COMMIT,
        "tree": TREE,
        "mode": "push",
        "checks": [CHECK_RUFF, CHECK_FRONTEND, CHECK_PYTEST_CHANGED],
        "ruff_files": ["backend/app/core/auth/jwt.py"],
        "pytest_files": ["tests/test_auth.py"],
        "frontend_scripts": list(FRONTEND_HOOK_SCRIPTS),
    }
    payload.update(overrides)
    return payload


def test_missing_receipt_does_not_skip_cheap_ci() -> None:
    skip = receipt_skips(None, commit=COMMIT, tree=TREE)

    assert skip == ReceiptSkip()
    assert skip.skip_frontend_static is False
    assert skip.skip_ruff_files == ()
    assert skip.skip_pytest_changed is False


def test_valid_receipt_skips_only_overlapping_cheap_checks() -> None:
    skip = receipt_skips(_receipt(), commit=COMMIT, tree=TREE)

    assert skip.skip_frontend_static is True
    assert skip.skip_ruff_files == ("backend/app/core/auth/jwt.py",)
    assert skip.skip_pytest_changed is True
    assert set(FRONTEND_CI_OVERLAP) <= set(FRONTEND_HOOK_SCRIPTS)


def test_wrong_tree_or_commit_receipt_is_ignored() -> None:
    skip = receipt_skips(_receipt(), commit=COMMIT, tree=OTHER)
    assert skip == ReceiptSkip()
    skip = receipt_skips(_receipt(), commit=OTHER, tree=TREE)
    assert skip == ReceiptSkip()


def test_frontend_receipt_without_vitest_does_not_skip_static() -> None:
    skip = receipt_skips(
        _receipt(frontend_scripts=["lint", "format:check", "typecheck"]),
        commit=COMMIT,
        tree=TREE,
    )

    assert skip.skip_frontend_static is False
    assert skip.skip_ruff_files == ("backend/app/core/auth/jwt.py",)


def test_malformed_or_unsafe_receipt_is_ignored() -> None:
    assert receipt_skips(_receipt(schema=2), commit=COMMIT, tree=TREE) == ReceiptSkip()
    assert receipt_skips(_receipt(mode="commit"), commit=COMMIT, tree=TREE) == ReceiptSkip()
    assert (
        receipt_skips(_receipt(ruff_files=["../secret.py"]), commit=COMMIT, tree=TREE)
        == ReceiptSkip()
    )
    assert (
        receipt_skips(_receipt(ruff_files=["/tmp/x.py"]), commit=COMMIT, tree=TREE)
        == ReceiptSkip()
    )
    assert receipt_skips(_receipt(checks="ruff"), commit=COMMIT, tree=TREE) == ReceiptSkip()


def test_receipt_push_only_accepts_bound_refs() -> None:
    ref = f"{RECEIPT_REF_PREFIX}{COMMIT}"
    assert receipt_push_only([ref]) is True
    assert receipt_push_only([ref, f"{RECEIPT_REF_PREFIX}{OTHER}"]) is True
    assert receipt_push_only([]) is False
    assert receipt_push_only(["refs/heads/main"]) is False
    assert receipt_push_only([ref, "refs/heads/main"]) is False
    assert receipt_push_only([f"{RECEIPT_REF_PREFIX}not-a-sha"]) is False
    assert main(["--receipt-push-only", ref]) == 0
    assert main(["--receipt-push-only", "refs/heads/main"]) == 1


def test_ruff_exclude_args_are_backend_relative(capsys: pytest.CaptureFixture[str]) -> None:
    assert format_ruff_exclude_args(
        ["backend/app/core/auth/jwt.py", "scripts/check_pre_vcs_gates.py"]
    ) == [
        "--exclude",
        "app/core/auth/jwt.py",
        "--exclude",
        "../scripts/check_pre_vcs_gates.py",
    ]
    assert format_ruff_exclude_args(["../escape.py", "/abs.py"]) == []
    assert main(
        [
            "--ruff-exclude-args",
            "--skip-ruff-files",
            "backend/app/foo.py,scripts/bar.py",
        ]
    ) == 0
    assert capsys.readouterr().out.splitlines() == [
        "--exclude",
        "app/foo.py",
        "--exclude",
        "../scripts/bar.py",
    ]


def test_evaluate_receipt_ignores_forced_ci_events(tmp_path: Path) -> None:
    skip = evaluate_receipt(
        root=tmp_path,
        event_name="schedule",
        commit=COMMIT,
        receipt_ref=f"{RECEIPT_REF_PREFIX}{COMMIT}",
    )
    assert skip == ReceiptSkip()
    skip = evaluate_receipt(
        root=tmp_path,
        event_name="workflow_dispatch",
        commit=COMMIT,
        receipt_ref=f"{RECEIPT_REF_PREFIX}{COMMIT}",
    )
    assert skip == ReceiptSkip()


def test_evaluate_receipt_reads_matching_git_ref(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    env = isolated_check_env()

    def git(*args: str, input_text: str | None = None) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
            env=env,
            input=input_text,
        )
        return completed.stdout.strip()

    git("init", "-b", "main")
    git("config", "user.name", "Gate Test")
    git("config", "user.email", "gate-test@example.invalid")
    (repo / "README.md").write_text("ok\n", encoding="utf-8")
    git("add", "README.md")
    git("-c", f"core.hooksPath={os.devnull}", "commit", "-m", "init")
    commit = git("rev-parse", "HEAD")
    tree = git("rev-parse", "HEAD^{tree}")
    receipt = _receipt(
        commit=commit,
        tree=tree,
        checks=[CHECK_FRONTEND],
        ruff_files=[],
        pytest_files=[],
    )
    blob = git(
        "hash-object",
        "-w",
        "--stdin",
        input_text=json.dumps(receipt, sort_keys=True, separators=(",", ":")),
    )
    ref = f"{RECEIPT_REF_PREFIX}{commit}"
    git("update-ref", ref, blob)

    skip = evaluate_receipt(
        root=repo,
        event_name="push",
        commit=commit,
        receipt_ref=ref,
    )
    assert skip.skip_frontend_static is True
    assert skip.skip_ruff_files == ()
    assert load_receipt_from_ref(repo, "refs/heads/main") is None


def test_missing_receipt_outputs_do_not_skip(tmp_path: Path) -> None:
    output = tmp_path / "github-output"
    write_receipt_outputs(output, ReceiptSkip())
    assert output.read_text(encoding="utf-8").splitlines() == [
        "skip_frontend_static=false",
        "skip_ruff_files=",
    ]


def test_publish_receipt_failure_does_not_fail_the_hook(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "check_pre_vcs_gates.build_push_receipt",
        lambda root, plan, commit=None: (_ for _ in ()).throw(
            GateError("cannot write receipt blob")
        ),
    )
    publish_push_receipt(ROOT, "origin", plan(["backend/app/core/auth/jwt.py"]))
    assert "CI will re-run cheap checks" in capsys.readouterr().err


def test_receipt_does_not_claim_ci_only_surfaces() -> None:
    skip = receipt_skips(_receipt(), commit=COMMIT, tree=TREE)
    dumped = skip.__dict__
    for forbidden in (
        "g2",
        "coverage",
        "mypy",
        "security",
        "vendor_postgres",
        "build",
        "gen:api-types",
        "audit",
    ):
        assert forbidden not in dumped
