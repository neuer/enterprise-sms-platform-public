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
        ("frontend/src/App.vue", (False, True, False)),
        ("frontend/tests/app-shell.test.ts", (False, True, False)),
        ("frontend/package-lock.json", (False, True, False)),
        ("frontend/Dockerfile", (False, True, True)),
        ("deploy/nginx.conf", (False, True, True)),
        ("backend/tests/test_auth.py", (True, False, False)),
        ("backend/scripts_support/check_migration.py", (True, False, False)),
        ("scripts/check_contract.py", (True, False, False)),
        ("backend/app/services/dashboard.py", (True, False, False)),
        ("backend/app/core/auth/jwt.py", (True, False, True)),
        ("backend/app/services/crypto.py", (True, False, True)),
        ("backend/app/main.py", (True, True, True)),
        ("backend/migrations/versions/0013_example.py", (True, False, True)),
        ("backend/uv.lock", (True, False, True)),
        ("deploy/docker-compose.yml", (True, True, True)),
        ("deploy/scripts/release_manager.py", (True, False, True)),
        ("scripts/perf_smoke.py", (True, False, True)),
        ("scripts/verify_all.sh", (True, False, True)),
        ("scripts/classify_ci_changes.py", (True, True, True)),
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


def test_mixed_changes_take_union() -> None:
    result = classify_paths(["frontend/src/App.vue", "backend/app/main.py"])

    assert result == Classification(
        backend=True,
        frontend=True,
        g2=True,
        security=True,
        categories=frozenset({"frontend", "vendor-live"}),
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
    feature_sha = commit_file(repo, "frontend/src/App.vue", "feature")
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


def test_high_risk_pull_request_still_requires_g2(tmp_path: Path) -> None:
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
        True,
        False,
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


def test_rename_is_classified_as_delete_plus_add(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    before_sha = commit_file(repo, "frontend/src/Old.vue", "old")
    (repo / "new-top-level").mkdir()
    git(repo, "mv", "frontend/src/Old.vue", "new-top-level/renamed.txt")
    git(repo, "commit", "-m", "rename")
    head_sha = git(repo, "rev-parse", "HEAD")

    result = classify_push(repo, before_sha=before_sha, head_sha=head_sha)

    assert (result.backend, result.frontend, result.g2) == (True, True, True)
    assert result.full_fallback is True
    assert result.categories == frozenset({"frontend", "unknown"})


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
    ("event_name", "base_sha", "before_sha", "head_sha", "reason"),
    [
        ("pull_request", "", "", "head", "missing-pr-sha"),
        ("push", "", ZERO_SHA, "head", "missing-push-sha"),
        ("push", "", "before", "", "missing-push-sha"),
        ("repository_dispatch", "", "", "", "unsupported-event"),
    ],
)
def test_unreliable_event_metadata_forces_all(
    tmp_path: Path,
    event_name: str,
    base_sha: str,
    before_sha: str,
    head_sha: str,
    reason: str,
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
        True,
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
