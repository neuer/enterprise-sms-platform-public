from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from classify_ci_changes import (  # noqa: E402
    ZERO_SHA,
    ChangeDetectionError,
    Classification,
    classify_event,
    classify_paths,
    main,
    write_github_outputs,
)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("docs/plans/2026-07-16-note.md", (False, False, False)),
        ("docs/TEST-REPORT-probe.md", (False, False, False)),
        ("PROGRESS.md", (False, False, False)),
        ("MAINTENANCE.md", (True, False, False)),
        ("PUBLICATION.md", (True, False, False)),
        ("PRD.md", (True, False, False)),
        ("docs/UAT.md", (True, False, False)),
        ("deploy/README.md", (True, False, False)),
        ("openapi.yaml", (True, True, True)),
        ("schema.sql", (True, True, True)),
        ("frontend/src/views/DashboardView.vue", (False, True, False)),
        ("frontend/src/App.vue", (False, True, False)),
        ("frontend/tests/app-shell.test.ts", (False, True, False)),
        ("frontend/package-lock.json", (False, True, False)),
        ("frontend/Dockerfile", (False, True, True)),
        ("deploy/nginx.conf", (False, True, True)),
        ("backend/tests/test_auth.py", (True, False, False)),
        ("backend/scripts_support/check_migration.py", (True, False, False)),
        ("scripts/check_contract.py", (True, False, False)),
        ("backend/app/services/dashboard.py", (True, False, False)),
        ("backend/app/outbox_dispatcher.py", (True, False, True)),
        ("backend/app/models/__init__.py", (True, False, True)),
        ("backend/app/healthcheck.py", (True, False, True)),
        ("frontend/src/views/LoginView.vue", (False, True, False)),
        ("frontend/src/views/PasswordChangeView.vue", (False, True, False)),
        ("frontend/src/views/AppManagementView.vue", (False, True, False)),
        ("frontend/src/views/ApprovalView.vue", (False, True, False)),
        ("backend/app/core/auth/jwt.py", (True, False, True)),
        ("backend/app/services/crypto.py", (True, False, True)),
        ("backend/app/services/usage_ledger.py", (True, False, True)),
        ("backend/app/services/raw_spill.py", (True, False, True)),
        ("frontend/src/api/sessionTokens.ts", (False, True, False)),
        ("frontend/src/api/auth.ts", (False, True, False)),
        ("frontend/src/api/webMessages.ts", (False, True, False)),
        ("frontend/src/api/refreshLock.ts", (False, True, False)),
        ("frontend/src/stores/session.ts", (False, True, False)),
        ("frontend/src/api/sessionGeneration.ts", (False, True, False)),
        ("backend/app/main.py", (True, True, True)),
        ("backend/migrations/versions/0013_example.py", (True, False, True)),
        ("backend/uv.lock", (True, False, True)),
        ("deploy/docker-compose.yml", (True, True, True)),
        ("deploy/scripts/release_manager.py", (True, False, True)),
        ("scripts/perf_smoke.py", (True, False, True)),
        ("scripts/verify_all.sh", (True, False, True)),
        ("scripts/classify_ci_changes.py", (True, True, True)),
        ("deploy/scripts/protected_path_policy.py", (True, True, True)),
        ("scripts/verify_ci_results.py", (True, False, True)),
        ("scripts/verify_vendor_live_test.sh", (True, True, True)),
        ("backend/app/services/vendor_test_budget.py", (True, True, True)),
        ("deploy/scripts/vendor_test_manager.py", (True, True, True)),
        ("deploy/scripts/test_update_manager.py", (True, True, True)),
        (".github/workflows/ci.yml", (True, False, True)),
        ("new-top-level/unknown.txt", (True, True, True)),
    ],
)
def test_classifies_repository_paths(
    path: str,
    expected: tuple[bool, bool, bool],
) -> None:
    result = classify_paths([path])

    assert (result.backend, result.frontend, result.g2) == expected


@pytest.mark.parametrize(
    "path",
    (
        "frontend/src/views/OpsView.vue",
        "frontend/src/views/UserView.vue",
        "frontend/src/views/SendView.vue",
        "frontend/src/views/AuditView.vue",
    ),
)
def test_sensitive_management_views_select_frontend_security(path: str) -> None:
    result = classify_paths([path])

    assert result.frontend is True
    assert result.security is True
    assert result.g2 is False
    assert result.backend is False
    assert result.categories == frozenset({"frontend-security"})
    assert result.full_fallback is False


def test_dashboard_view_stays_ordinary_frontend() -> None:
    result = classify_paths(["frontend/src/views/DashboardView.vue"])

    assert (result.backend, result.frontend, result.g2, result.security) == (
        False,
        True,
        False,
        False,
    )
    assert result.categories == frozenset({"frontend"})


def test_mixed_changes_take_union() -> None:
    result = classify_paths(["frontend/src/App.vue", "backend/app/main.py"])

    assert result == Classification(
        backend=True,
        frontend=True,
        g2=True,
        security=True,
        categories=frozenset({"frontend-security", "vendor-live"}),
        full_fallback=False,
        performance=True,
    )


def test_progress_is_an_ordinary_doc_without_reducing_mixed_change_risk() -> None:
    progress_only = classify_paths(["PROGRESS.md"])
    mixed = classify_paths(["PROGRESS.md", "backend/app/services/dashboard.py"])
    protected_mixed = classify_paths(
        ["PROGRESS.md", "backend/app/services/crypto.py"]
    )

    assert progress_only == Classification(
        backend=False,
        frontend=False,
        g2=False,
        security=False,
        categories=frozenset({"ordinary-doc"}),
    )
    assert (mixed.backend, mixed.frontend, mixed.g2, mixed.security) == (
        True,
        False,
        False,
        False,
    )
    assert mixed.categories == frozenset({"ordinary-doc", "backend-check"})
    assert (
        protected_mixed.backend,
        protected_mixed.frontend,
        protected_mixed.g2,
        protected_mixed.security,
    ) == (True, False, True, True)
    assert protected_mixed.categories == frozenset(
        {"ordinary-doc", "backend-critical"}
    )


@pytest.mark.parametrize(
    "path",
    [
        "backend/app/api/messages.py",
        "backend/app/api/web_messages.py",
        "backend/app/cli.py",
        "backend/app/settings.py",
        "backend/app/services/pipeline.py",
        "backend/app/services/reconcile_repository.py",
        "backend/app/vendor/zhihui.py",
        "backend/app/vendor/codes.py",
        "backend/app/tasks/send.py",
        "backend/app/tasks/send_repository.py",
        "backend/app/services/billing.py",
        "backend/app/services/vendor_test_guard.py",
        "backend/app/services/vendor_test_budget.py",
        "backend/app/services/vendor_test_pause.py",
        "backend/migrations/versions/0016_vendor_live_test_budget.py",
        "deploy/docker-compose.yml",
        "deploy/scripts/vendor_test_files.py",
        "deploy/scripts/vendor_test_manager.py",
        "deploy/scripts/test_update_manager.py",
        "scripts/verify_vendor_live_test.sh",
    ],
)
def test_vendor_live_high_risk_paths_always_select_full_ci_and_g2(path: str) -> None:
    result = classify_paths([path])

    assert (result.backend, result.frontend, result.g2) == (True, True, True)
    assert result.security is True
    assert "vendor-live" in result.categories


@pytest.mark.parametrize(
    "path",
    [
        "backend/app/api/admin.py",
        "backend/app/api/approvals.py",
        "backend/app/api/auth.py",
        "backend/app/core/apikey.py",
        "backend/app/core/audit.py",
        "backend/app/core/auth/jwt.py",
        "backend/app/core/ratelimit.py",
        "backend/app/services/approval.py",
        "backend/app/services/callback_worker.py",
        "backend/app/services/crypto.py",
        "backend/app/services/export_file.py",
        "backend/app/services/freq.py",
        "backend/app/services/import_repository.py",
        "backend/app/services/masking.py",
        "backend/app/services/pipeline_repository.py",
        "backend/app/services/raw_replay.py",
        "backend/app/services/raw_spill.py",
        "backend/app/services/raw_capture_legacy.py",
        "backend/app/services/ops_repository.py",
        "backend/app/outbox_dispatcher.py",
        "backend/app/models/__init__.py",
        "backend/app/healthcheck.py",
        "backend/app/build_info.py",
        "backend/app/services/usage_ledger.py",
        "backend/app/tasks/poll_report.py",
        "backend/app/vendor/mock_server.py",
    ],
)
def test_backend_security_and_pii_paths_select_backend_and_g2(path: str) -> None:
    result = classify_paths([path])

    assert (result.backend, result.frontend, result.g2) == (True, False, True)
    assert result.security is True
    assert "backend-critical" in result.categories


@pytest.mark.parametrize(
    "path",
    [
        "backend/uv.lock",
        "frontend/package-lock.json",
        "backend/Dockerfile",
        "frontend/Dockerfile",
        "deploy/postgres.Dockerfile",
        "deploy/redis.Dockerfile",
        ".github/workflows/release-gate.yml",
    ],
)
def test_dependency_image_and_release_changes_select_security_gate(path: str) -> None:
    assert classify_paths([path]).security is True


def test_empty_and_unknown_changes_fail_closed() -> None:
    assert classify_paths([]).full_fallback is True
    assert classify_paths(["new-top-level/file.txt"]).full_fallback is True


def test_newline_in_known_filename_is_classified_without_splitting() -> None:
    result = classify_paths(["docs/plans/line\nbreak.md"])

    assert (result.backend, result.frontend, result.g2) == (False, False, False)


@pytest.mark.parametrize("path", ["", "/absolute/path", "docs/../secret"])
def test_invalid_git_paths_fail_closed(path: str) -> None:
    result = classify_paths([path])

    assert (result.backend, result.frontend, result.g2) == (True, True, True)
    assert result.full_fallback is True


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "CI Test")
    git(repo, "config", "user.email", "ci-test@example.invalid")
    return repo


def commit_file(repo: Path, path: str, content: str) -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    git(repo, "add", "--", path)
    git(repo, "commit", "-m", "test commit")
    return git(repo, "rev-parse", "HEAD")


def classify_pull_request(repo: Path, *, base_sha: str, head_sha: str) -> Classification:
    return classify_event(
        repo=repo,
        event_name="pull_request",
        base_sha=base_sha,
        before_sha="",
        head_sha=head_sha,
    )


def classify_push(repo: Path, *, before_sha: str, head_sha: str) -> Classification:
    return classify_event(
        repo=repo,
        event_name="push",
        base_sha="",
        before_sha=before_sha,
        head_sha=head_sha,
    )


def test_pull_request_uses_three_dot_diff_and_excludes_base_only_changes(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    commit_file(repo, "docs/plans/base.md", "base")
    git(repo, "checkout", "-b", "feature")
    feature_sha = commit_file(repo, "frontend/src/views/DashboardView.vue", "feature")
    git(repo, "checkout", "main")
    base_sha = commit_file(repo, "backend/app/base_only.py", "base only")

    result = classify_pull_request(repo, base_sha=base_sha, head_sha=feature_sha)

    assert result == Classification(
        False,
        True,
        False,
        False,
        frozenset({"frontend"}),
        False,
    )


def test_push_uses_two_dot_diff_for_the_exact_push_range(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    before_sha = commit_file(repo, "docs/plans/base.md", "base")
    head_sha = commit_file(repo, "backend/tests/test_probe.py", "test")

    result = classify_push(repo, before_sha=before_sha, head_sha=head_sha)

    assert result == Classification(
        True,
        False,
        False,
        False,
        frozenset({"backend-check"}),
        False,
    )


def test_main_push_without_verified_pr_evidence_fails_closed_to_g2(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path)
    before_sha = commit_file(repo, "docs/plans/base.md", "base")
    head_sha = commit_file(repo, "backend/app/core/audit.py", "audit")

    result = classify_push(repo, before_sha=before_sha, head_sha=head_sha)

    assert result == Classification(
        True,
        False,
        True,
        True,
        frozenset({"backend-critical"}),
        False,
        True,
        False,
    )


def test_main_push_reuses_only_explicitly_verified_pr_evidence(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    before_sha = commit_file(repo, "docs/plans/base.md", "base")
    head_sha = commit_file(repo, "new-top-level/unknown.txt", "unknown")

    result = classify_event(
        repo=repo,
        event_name="push",
        base_sha="",
        before_sha=before_sha,
        head_sha=head_sha,
        trusted_pr_evidence=True,
    )

    assert result == Classification(
        False,
        False,
        False,
        False,
        frozenset({"reused-pr-ci-evidence"}),
        False,
        False,
        False,
    )


@pytest.mark.parametrize(
    ("trusted", "expected"),
    [
        (
            True,
            Classification(
                False,
                False,
                False,
                False,
                frozenset({"reused-pr-ci-evidence"}),
                False,
                False,
                False,
            ),
        ),
        (
            False,
            Classification(
                True,
                True,
                True,
                True,
                frozenset({"untrusted-post-merge"}),
                True,
                True,
                True,
            ),
        ),
    ],
)
def test_post_merge_reuses_only_trusted_evidence_without_git(
    tmp_path: Path,
    trusted: bool,
    expected: Classification,
) -> None:
    result = classify_event(
        repo=tmp_path / "missing-repo",
        event_name="post_merge",
        base_sha="",
        before_sha="",
        head_sha="",
        trusted_pr_evidence=trusted,
    )

    assert result == expected


def test_high_risk_pull_request_requires_g2_but_defers_performance(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path)
    base_sha = commit_file(repo, "docs/plans/base.md", "base")
    git(repo, "checkout", "-b", "feature")
    head_sha = commit_file(repo, "backend/app/core/audit.py", "audit")

    result = classify_pull_request(repo, base_sha=base_sha, head_sha=head_sha)

    assert result == Classification(
        True,
        False,
        True,
        True,
        frozenset({"backend-critical"}),
        False,
        False,
        False,
    )


def test_pull_request_defers_performance_without_dropping_release_control(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path)
    base_sha = commit_file(repo, "docs/plans/base.md", "base")
    git(repo, "checkout", "-b", "feature")
    head_sha = commit_file(repo, "backend/Dockerfile", "FROM scratch")

    result = classify_pull_request(repo, base_sha=base_sha, head_sha=head_sha)

    assert result == Classification(
        True,
        True,
        True,
        True,
        frozenset({"vendor-live"}),
        False,
        False,
        True,
    )


def test_ordinary_backend_pull_request_uses_fast_backend_only(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    base_sha = commit_file(repo, "docs/plans/base.md", "base")
    git(repo, "checkout", "-b", "feature")
    head_sha = commit_file(repo, "backend/app/services/dashboard.py", "dashboard")

    result = classify_pull_request(repo, base_sha=base_sha, head_sha=head_sha)

    assert result == Classification(
        True,
        False,
        False,
        False,
        frozenset({"backend-check"}),
        False,
    )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            "backend/app/services/raw_capture_legacy.py",
            (True, False, True, True, "backend-critical"),
        ),
        (
            "backend/app/services/ops_repository.py",
            (True, False, True, True, "backend-critical"),
        ),
        (
            "frontend/src/App.vue",
            (False, True, False, True, "frontend-security"),
        ),
    ],
)
def test_raw_capture_ops_and_session_shell_are_not_skipped(
    path: str,
    expected: tuple[bool, bool, bool, bool, str],
) -> None:
    backend, frontend, g2, security, category = expected
    result = classify_paths([path])

    assert (result.backend, result.frontend, result.g2, result.security) == (
        backend,
        frontend,
        g2,
        security,
    )
    assert result.categories == frozenset({category})
    assert result.full_fallback is False


@pytest.mark.parametrize(
    "path",
    [
        "backend/app/services/new_replay_guard.py",
        "backend/app/api/new_privileged_ops.py",
        "backend/app/core/new_origin_guard.py",
        "backend/app/brand_new_root.py",
        "backend/app/models/brand_new_model.py",
        "frontend/src/stores/new_session_lock.ts",
        "frontend/src/api/new_session_client.ts",
        "frontend/src/router/new_guard.ts",
        "frontend/src/views/BrandNewView.vue",
        "frontend/src/components/BrandNewDialog.vue",
        "frontend/src/components/DailyPasswordChangeDialog.vue",
        "frontend/src/components/ApprovalList.vue",
        "frontend/src/main.ts",
        "frontend/src/brand_new_entry.ts",
        "frontend/src/BrandNewRoot.vue",
    ],
)
def test_new_security_domain_files_default_to_full_protection(path: str) -> None:
    result = classify_paths([path])

    assert result.security is True
    assert result.full_fallback is False
    if path.startswith("frontend/"):
        assert (result.backend, result.frontend, result.g2) == (False, True, False)
        assert result.categories == frozenset({"frontend-security"})
    else:
        assert (result.backend, result.frontend, result.g2) == (True, False, True)
        assert result.categories == frozenset({"backend-critical"})


def test_reviewed_ordinary_dashboard_stays_backend_only() -> None:
    result = classify_paths(["backend/app/services/dashboard.py"])

    assert (result.backend, result.frontend, result.g2, result.security) == (
        True,
        False,
        False,
        False,
    )
    assert result.categories == frozenset({"backend-check"})


def test_deleting_protected_domain_file_keeps_g2(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    before_sha = commit_file(repo, "backend/app/services/raw_capture_legacy.py", "old")
    git(repo, "rm", "--", "backend/app/services/raw_capture_legacy.py")
    git(repo, "commit", "-m", "delete")
    head_sha = git(repo, "rev-parse", "HEAD")

    result = classify_push(repo, before_sha=before_sha, head_sha=head_sha)

    assert (result.backend, result.frontend, result.g2, result.security) == (
        True,
        False,
        True,
        True,
    )
    assert result.categories == frozenset({"backend-critical"})
    assert result.full_fallback is False


@pytest.mark.parametrize(
    "path",
    [
        "backend/app/services/usage_ledger.py",
        "backend/app/services/raw_spill.py",
        "backend/app/services/raw_capture_legacy.py",
        "backend/app/services/ops_repository.py",
    ],
)
def test_usage_ledger_raw_spill_and_backend_critical_paths_select_g2(
    path: str,
) -> None:
    result = classify_paths([path])

    assert (result.backend, result.frontend, result.g2, result.security) == (
        True,
        False,
        True,
        True,
    )
    assert result.categories == frozenset({"backend-critical"})
    assert result.full_fallback is False


@pytest.mark.parametrize(
    "path",
    [
        "frontend/src/api/auth.ts",
        "frontend/src/api/refreshLock.ts",
        "frontend/src/api/webMessages.ts",
        "frontend/src/stores/session.ts",
        "frontend/src/api/sessionTokens.ts",
        "frontend/src/api/sessionGeneration.ts",
    ],
)
def test_frontend_session_paths_select_frontend_security_without_backend_job(
    path: str,
) -> None:
    result = classify_paths([path])

    assert (result.backend, result.frontend, result.g2, result.security) == (
        False,
        True,
        False,
        True,
    )
    assert result.categories == frozenset({"frontend-security"})
    assert result.full_fallback is False


def test_copying_protected_file_to_ordinary_path_keeps_security(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path)
    before_sha = commit_file(repo, "backend/app/outbox_dispatcher.py", "protected")
    target = repo / "docs" / "copied-outbox.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("protected", encoding="utf-8")
    git(repo, "add", "--", "docs/copied-outbox.py")
    git(repo, "commit", "-m", "copy")
    head_sha = git(repo, "rev-parse", "HEAD")

    result = classify_push(repo, before_sha=before_sha, head_sha=head_sha)

    assert result.security is True
    assert result.g2 is True
    assert result.backend is True
    assert "backend-critical" in result.categories


def test_renaming_security_file_to_ordinary_path_keeps_both_sides(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path)
    before_sha = commit_file(repo, "frontend/src/views/LoginView.vue", "login")
    (repo / "docs").mkdir(exist_ok=True)
    git(repo, "mv", "frontend/src/views/LoginView.vue", "docs/login-moved.md")
    git(repo, "commit", "-m", "rename-escape")
    head_sha = git(repo, "rev-parse", "HEAD")

    result = classify_push(repo, before_sha=before_sha, head_sha=head_sha)

    assert result.frontend is True
    assert result.security is True
    assert "frontend-security" in result.categories


def test_renaming_sensitive_component_to_ordinary_path_keeps_security(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path)
    before_sha = commit_file(
        repo, "frontend/src/components/DailyPasswordChangeDialog.vue", "password"
    )
    (repo / "docs").mkdir(exist_ok=True)
    git(
        repo,
        "mv",
        "frontend/src/components/DailyPasswordChangeDialog.vue",
        "docs/password-moved.md",
    )
    git(repo, "commit", "-m", "rename-component-escape")
    head_sha = git(repo, "rev-parse", "HEAD")

    result = classify_push(repo, before_sha=before_sha, head_sha=head_sha)

    assert result.frontend is True
    assert result.security is True
    assert "frontend-security" in result.categories


def test_copying_sensitive_component_to_ordinary_path_keeps_security(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path)
    before_sha = commit_file(
        repo, "frontend/src/components/ApprovalList.vue", "approval"
    )
    target = repo / "docs" / "copied-approval.vue"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("approval", encoding="utf-8")
    git(repo, "add", "--", "docs/copied-approval.vue")
    git(repo, "commit", "-m", "copy-component")
    head_sha = git(repo, "rev-parse", "HEAD")

    result = classify_push(repo, before_sha=before_sha, head_sha=head_sha)

    assert result.security is True
    assert result.frontend is True
    assert "frontend-security" in result.categories


def test_renaming_frontend_root_entry_to_ordinary_path_keeps_security(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path)
    before_sha = commit_file(repo, "frontend/src/main.ts", "bootstrap")
    (repo / "docs").mkdir(exist_ok=True)
    git(repo, "mv", "frontend/src/main.ts", "docs/main-moved.md")
    git(repo, "commit", "-m", "rename-root-entry-escape")
    head_sha = git(repo, "rev-parse", "HEAD")

    result = classify_push(repo, before_sha=before_sha, head_sha=head_sha)

    assert result.frontend is True
    assert result.security is True
    assert "frontend-security" in result.categories


def test_copying_frontend_root_entry_to_ordinary_path_keeps_security(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path)
    before_sha = commit_file(repo, "frontend/src/main.ts", "bootstrap")
    target = repo / "docs" / "copied-main.ts"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("bootstrap", encoding="utf-8")
    git(repo, "add", "--", "docs/copied-main.ts")
    git(repo, "commit", "-m", "copy-root-entry")
    head_sha = git(repo, "rev-parse", "HEAD")

    result = classify_push(repo, before_sha=before_sha, head_sha=head_sha)

    assert result.security is True
    assert result.frontend is True
    assert "frontend-security" in result.categories


def test_new_frontend_root_entry_defaults_to_security(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    before_sha = commit_file(repo, "docs/note.md", "note")
    head_sha = commit_file(repo, "frontend/src/bootstrap.ts", "createPinia()")

    result = classify_push(repo, before_sha=before_sha, head_sha=head_sha)

    assert result.frontend is True
    assert result.security is True
    assert "frontend-security" in result.categories


def test_renaming_protected_lifeline_keeps_g2_without_forcing_full(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path)
    before_sha = commit_file(repo, "backend/app/services/usage_ledger.py", "old")
    git(
        repo,
        "mv",
        "backend/app/services/usage_ledger.py",
        "backend/app/services/usage_ledger_renamed.py",
    )
    git(repo, "commit", "-m", "rename")
    head_sha = git(repo, "rev-parse", "HEAD")

    result = classify_push(repo, before_sha=before_sha, head_sha=head_sha)

    assert (result.backend, result.frontend, result.g2, result.security) == (
        True,
        False,
        True,
        True,
    )
    assert result.full_fallback is False
    assert result.categories == frozenset({"backend-critical"})


def test_rename_classifies_both_old_and_new_paths(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    before_sha = commit_file(repo, "frontend/src/Old.vue", "old")
    (repo / "new-top-level").mkdir()
    git(repo, "mv", "frontend/src/Old.vue", "new-top-level/renamed.txt")
    git(repo, "commit", "-m", "rename")
    head_sha = git(repo, "rev-parse", "HEAD")

    result = classify_push(repo, before_sha=before_sha, head_sha=head_sha)

    assert (result.backend, result.frontend, result.g2) == (True, True, True)
    assert result.full_fallback is True
    assert result.categories == frozenset({"frontend-security", "unknown"})


def test_nul_diff_preserves_spaces_and_newlines_in_filename(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    before_sha = commit_file(repo, "docs/plans/base.md", "base")
    head_sha = commit_file(repo, "docs/plans/line with space\nand break.md", "probe")

    result = classify_push(repo, before_sha=before_sha, head_sha=head_sha)

    assert result == Classification(
        False,
        False,
        False,
        False,
        frozenset({"ordinary-doc"}),
        False,
    )


@pytest.mark.parametrize("event_name", ["workflow_dispatch", "schedule"])
def test_manual_and_scheduled_events_force_all_without_git(
    tmp_path: Path,
    event_name: str,
) -> None:
    result = classify_event(
        repo=tmp_path / "missing-repo",
        event_name=event_name,
        base_sha="",
        before_sha="",
        head_sha="",
    )

    assert result == Classification(
        True,
        True,
        True,
        True,
        frozenset({f"forced-{event_name}"}),
        True,
        True,
        True,
    )


@pytest.mark.parametrize(
    ("event_name", "base_sha", "before_sha", "head_sha", "reason", "performance"),
    [
        ("pull_request", "", "", "head", "missing-pr-sha", False),
        ("push", "", ZERO_SHA, "head", "missing-push-sha", True),
        ("push", "", "before", "", "missing-push-sha", True),
        ("repository_dispatch", "", "", "", "unsupported-event", True),
    ],
)
def test_unreliable_event_metadata_fails_closed_while_pr_defers_performance(
    tmp_path: Path,
    event_name: str,
    base_sha: str,
    before_sha: str,
    head_sha: str,
    reason: str,
    performance: bool,
) -> None:
    result = classify_event(
        repo=tmp_path / "missing-repo",
        event_name=event_name,
        base_sha=base_sha,
        before_sha=before_sha,
        head_sha=head_sha,
    )

    assert result == Classification(
        True,
        True,
        True,
        True,
        frozenset({reason}),
        True,
        performance,
        True,
    )


def test_reliable_but_empty_diff_forces_all(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    head_sha = commit_file(repo, "docs/plans/base.md", "base")

    result = classify_push(repo, before_sha=head_sha, head_sha=head_sha)

    assert result == Classification(
        True,
        True,
        True,
        True,
        frozenset({"empty-diff"}),
        True,
        True,
        True,
    )


def test_git_failure_is_not_converted_to_successful_outputs(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    commit_file(repo, "docs/plans/base.md", "base")

    with pytest.raises(ChangeDetectionError, match="git diff failed"):
        classify_push(repo, before_sha="missing-before", head_sha="missing-head")


def test_github_outputs_contain_only_boolean_job_flags(tmp_path: Path) -> None:
    output = tmp_path / "github-output"
    result = Classification(
        False,
        True,
        False,
        False,
        frozenset({"frontend"}),
        False,
    )

    write_github_outputs(output, result)

    assert output.read_text(encoding="utf-8").splitlines() == [
        "backend=false",
        "frontend=true",
        "g2=false",
        "security=false",
        "performance=false",
        "release_control=false",
    ]


def test_cli_writes_forced_outputs_without_echoing_paths(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "github-output"

    exit_code = main(
        [
            "--event-name",
            "schedule",
            "--github-output",
            str(output),
            "--repo",
            str(tmp_path / "missing-repo"),
        ]
    )

    assert exit_code == 0
    assert output.read_text(encoding="utf-8").splitlines() == [
        "backend=true",
        "frontend=true",
        "g2=true",
        "security=true",
        "performance=true",
        "release_control=true",
    ]
    captured = capsys.readouterr()
    assert "changed_files=0" in captured.out
    assert "missing-repo" not in captured.out + captured.err
