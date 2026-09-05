from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
RELEASE_WORKFLOW = ROOT / ".github/workflows/release-gate.yml"
AUTO_DRAFT_WORKFLOW = ROOT / ".github/workflows/auto-draft-pr.yml"
AUTO_MERGE_WORKFLOW = ROOT / ".github/workflows/auto-merge-owner-pr.yml"
MAINTENANCE = ROOT / "MAINTENANCE.md"
ALLOWED_ACTIONS = {
    "actions/attest",
    "actions/cache",
    "actions/checkout",
    "actions/download-artifact",
    "actions/setup-node",
    "actions/upload-artifact",
    "astral-sh/setup-uv",
}


def load_workflow(path: Path) -> dict[str, Any]:
    assert path.is_file(), f"missing workflow: {path.relative_to(ROOT)}"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def workflow_triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    # PyYAML follows YAML 1.1 and may parse the unquoted GitHub key `on` as True.
    triggers = workflow.get("on")
    if triggers is None:
        triggers = cast(dict[object, Any], workflow).get(True)
    assert isinstance(triggers, dict)
    return triggers


def job_commands(job: dict[str, Any]) -> str:
    steps = job.get("steps")
    assert isinstance(steps, list)
    return "\n".join(str(step["run"]) for step in steps if isinstance(step, dict) and "run" in step)


def assert_actions_are_immutable(workflow: dict[str, Any]) -> None:
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict)
    uses_values: list[str] = []
    for job in jobs.values():
        assert isinstance(job, dict)
        steps = job.get("steps")
        assert isinstance(steps, list)
        uses_values.extend(
            str(step["uses"]) for step in steps if isinstance(step, dict) and "uses" in step
        )

    assert uses_values
    for uses in uses_values:
        action, separator, revision = uses.partition("@")
        assert separator == "@"
        assert action in ALLOWED_ACTIONS
        assert re.fullmatch(r"[0-9a-f]{40}", revision), uses


def test_ci_workflow_runs_selected_checks_and_g2_in_parallel_before_gate() -> None:
    workflow = load_workflow(CI_WORKFLOW)
    triggers = workflow_triggers(workflow)

    assert triggers["pull_request"]["branches"] == ["main"]
    assert triggers["push"]["branches"] == ["**"]
    assert triggers["workflow_dispatch"] == {
        "inputs": {
            "post_merge_candidate": {
                "description": (
                    "Internal merge commit binding; leave empty for a full manual run"
                ),
                "required": False,
                "type": "string",
            },
            "post_merge_head": {
                "description": "Internal merged PR head binding",
                "required": False,
                "type": "string",
            },
            "post_merge_pr": {
                "description": "Internal merged PR number binding",
                "required": False,
                "type": "string",
            },
        }
    }
    assert triggers["schedule"] == [{"cron": "17 18 * * *"}]
    assert workflow["permissions"] == {
        "checks": "read",
        "contents": "read",
        "pull-requests": "read",
    }
    assert workflow["concurrency"]["cancel-in-progress"] is True

    jobs = workflow["jobs"]
    assert set(jobs) == {
        "changes",
        "backend-vendor-lint",
        "backend-coverage",
        "backend",
        "frontend",
        "security",
        "g2",
        "ci-gate",
    }

    changes = jobs["changes"]
    assert changes["outputs"] == {
        "candidate_sha": "${{ steps.bind.outputs.sha }}",
        "backend": "${{ steps.classify.outputs.backend }}",
        "frontend": "${{ steps.classify.outputs.frontend }}",
        "security": "${{ steps.classify.outputs.security }}",
        "g2": "${{ steps.classify.outputs.g2 }}",
        "release_control": "${{ steps.classify.outputs.release_control }}",
        "reused_pr_sha": "${{ steps.reuse.outputs.tested_sha }}",
    }
    dispatch = next(step for step in changes["steps"] if step.get("id") == "dispatch")
    assert dispatch["env"] == {
        "POST_MERGE_CANDIDATE": "${{ inputs.post_merge_candidate || '' }}",
        "POST_MERGE_HEAD": "${{ inputs.post_merge_head || '' }}",
        "POST_MERGE_PR": "${{ inputs.post_merge_pr || '' }}",
    }
    for token in (
        "present != 0 && present != 3",
        'test "$GITHUB_EVENT_NAME" = workflow_dispatch',
        'test "$GITHUB_SHA" = "$POST_MERGE_CANDIDATE"',
        "refs/tags/post-merge-ci-${POST_MERGE_CANDIDATE}-",
        '[[ "$suffix" =~ ^[0-9]+-[0-9]+$ ]]',
        "mode=ordinary",
        "mode=post_merge",
    ):
        assert token in dispatch["run"]
    checkout = next(
        step for step in changes["steps"] if step["name"] == "Checkout full history"
    )
    assert checkout["with"] == {"fetch-depth": 0}
    assert "ref" not in checkout["with"]
    assert "echo \"candidate=$GITHUB_SHA\"" not in dispatch["run"]
    changes_commands = job_commands(changes)
    for command in (
        "scripts/check_spec_consistency.py",
        "scripts/check_invariants.py",
        "scripts/check_public_readiness.py",
        "scripts/release_metadata.py",
        "scripts/reuse_pr_ci_evidence.py",
        "scripts/classify_ci_changes.py",
        "github.event.pull_request.head.sha || github.sha",
        'git merge-base origin/main "$GITHUB_SHA"',
        'event_name=pull_request',
        "event_name=post_merge",
        '--expected-head "$POST_MERGE_HEAD"',
        '--pr-number "$POST_MERGE_PR"',
    ):
        assert command in changes_commands
    expected_concurrency_group = (
        "ci-${{ github.workflow }}-${{ github.event_name }}-"
        "${{ github.event.pull_request.head.label || github.ref_name }}"
    )
    assert workflow["concurrency"]["group"] == expected_concurrency_group
    assert "github.event_name != 'pull_request'" in changes["if"]
    assert (
        "github.event.pull_request.head.repo.full_name != github.repository"
        in changes["if"]
    )

    vendor_lint_commands = job_commands(jobs["backend-vendor-lint"])
    coverage_commands = job_commands(jobs["backend-coverage"])
    backend_commands = "\n".join((vendor_lint_commands, coverage_commands))
    for command in (
        "scripts/local_test.sh prepare",
        "npm audit --prefix frontend --audit-level=high",
        "ruff check",
        "mypy",
        "pytest -q",
        "check_migration.py",
        "scripts/check_contract.py",
        "scripts/classify_ci_changes.py",
        "scripts/verify_ci_results.py",
        "scripts/g2_timing.py",
        "scripts/verify_vendor_live_test.sh",
        "SMS_COVERAGE=1 bash ../scripts/verify_vendor_postgres_recovery.sh",
        "--cov-append",
    ):
        assert command in backend_commands
    vendor_live_step = next(
        step
        for step in jobs["backend-vendor-lint"]["steps"]
        if step.get("name") == "Verify controlled vendor-live safety"
    )
    assert vendor_live_step["env"] == {"SMS_SKIP_VENDOR_POSTGRES_RECOVERY": "1"}
    assert "verify_vendor_postgres_recovery.sh" not in vendor_lint_commands
    assert vendor_lint_commands.count("verify_vendor_live_test.sh") == 1
    assert coverage_commands.count(
        "SMS_COVERAGE=1 bash ../scripts/verify_vendor_postgres_recovery.sh"
    ) == 1
    assert coverage_commands.count("verify_vendor_postgres_recovery.sh") == 1
    assert "pytest-xdist" not in backend_commands
    assert "-n auto" not in backend_commands
    assert jobs["backend-vendor-lint"]["needs"] == "changes"
    assert jobs["backend-coverage"]["needs"] == "changes"
    assert jobs["backend"]["needs"] == [
        "changes",
        "backend-vendor-lint",
        "backend-coverage",
    ]
    assert jobs["backend"]["name"] == "backend"
    for job_name in ("backend-vendor-lint", "backend-coverage"):
        assert "needs.changes.outputs.backend == 'true'" in jobs[job_name]["if"]
    for token in (
        "!cancelled()",
        "needs.changes.result == 'success'",
        "needs.changes.outputs.backend == 'true'",
    ):
        assert token in jobs["backend"]["if"]
    aggregator_commands = job_commands(jobs["backend"])
    assert 'needs.backend-vendor-lint.result' in aggregator_commands
    assert 'needs.backend-coverage.result' in aggregator_commands
    assert "pytest -q" not in aggregator_commands
    for job_name in (
        "backend-vendor-lint",
        "backend-coverage",
        "backend",
        "frontend",
        "security",
        "g2",
        "ci-gate",
    ):
        job_checkout = next(
            step
            for step in jobs[job_name]["steps"]
            if step.get("name") == "Checkout"
        )
        assert "ref" not in (job_checkout.get("with") or {})
        bind = next(
            step
            for step in jobs[job_name]["steps"]
            if "Bind" in str(step.get("name", ""))
        )
        assert 'test "$(git rev-parse HEAD)" = "$GITHUB_SHA"' in bind["run"]
        assert (
            'test "$(git rev-parse HEAD)" = "${{ needs.changes.outputs.candidate_sha }}"'
            in bind["run"]
        )

    frontend_commands = job_commands(jobs["frontend"])
    for command in (
        "npm ci",
        "npm audit --audit-level=high",
        "npm run build",
        "npm run lint",
        "npm run format:check",
        "npm test",
    ):
        assert command in frontend_commands
    # lint/format 先于组件测试（失败早报错），typecheck 仍只由 build 承载一次
    assert frontend_commands.index("npm run lint") < frontend_commands.index("npm test")
    assert frontend_commands.index("npm run format:check") < frontend_commands.index(
        "npm test"
    )
    assert "npm run typecheck" not in frontend_commands
    assert jobs["frontend"]["needs"] == "changes"
    assert "needs.changes.outputs.frontend == 'true'" in jobs["frontend"]["if"]

    security_commands = job_commands(jobs["security"])
    for command in (
        "uv run bandit",
        "vuln,misconfig,secret,license",
        "--severity HIGH,CRITICAL",
        "--exit-code 1",
    ):
        assert command in security_commands
    assert jobs["security"]["needs"] == "changes"
    assert "needs.changes.outputs.security == 'true'" in jobs["security"]["if"]

    assert jobs["g2"]["needs"] == ["changes"]
    for token in (
        "!cancelled()",
        "needs.changes.result == 'success'",
        "needs.changes.outputs.g2 == 'true'",
    ):
        assert token in jobs["g2"]["if"]
    for component in ("backend", "frontend", "security"):
        assert f"needs.{component}" not in jobs["g2"]["if"]
    g2_commands = job_commands(jobs["g2"])
    assert "scripts/local_test.sh prepare" in g2_commands
    assert "bash scripts/verify_all.sh" in g2_commands
    assert "--mode integration" in g2_commands
    assert "include-performance" not in g2_commands
    assert "needs.changes.outputs.performance" not in g2_commands
    assert "needs.changes.outputs.release_control" in g2_commands
    assert "verify_vendor_live_test.sh" in (ROOT / "scripts/verify_all.sh").read_text(
        encoding="utf-8"
    )
    assert "check_public_readiness.py" in (ROOT / "scripts/verify_all.sh").read_text(
        encoding="utf-8"
    )
    assert "sudo env" in g2_commands
    assert 'PATH="$PATH"' in g2_commands
    assert jobs["g2"]["timeout-minutes"] == 30

    gate = jobs["ci-gate"]
    assert gate["needs"] == ["changes", "backend", "frontend", "security", "g2"]
    assert gate["name"].endswith(
        "&& 'same-repo-pr-ci-skipped' || 'ci-gate' }}"
    )
    assert "!cancelled()" in gate["if"]
    assert "github.event_name != 'pull_request'" in gate["if"]
    assert (
        "github.event.pull_request.head.repo.full_name != github.repository"
        in gate["if"]
    )
    assert "scripts/verify_ci_results.py" in job_commands(gate)

    assert_actions_are_immutable(workflow)
    source = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "secrets." not in source
    assert "upload-artifact" not in source
    assert "pytest-xdist" not in source
    assert source.count("verify_vendor_postgres_recovery.sh") == 1
    assert source.count(
        "SMS_COVERAGE=1 bash ../scripts/verify_vendor_postgres_recovery.sh"
    ) == 1
    assert "ref: ${{ needs.changes.outputs.candidate_sha }}" not in source
    assert "ref: ${{ steps.dispatch.outputs.candidate }}" not in source


def test_g2_restores_dependency_caches_and_always_renders_timing() -> None:
    workflow = load_workflow(CI_WORKFLOW)
    g2 = workflow["jobs"]["g2"]

    cache_steps = [
        step
        for step in g2["steps"]
        if str(step.get("uses", "")).startswith("actions/cache@")
    ]
    assert len(cache_steps) == 2
    assert all(
        step["uses"] == "actions/cache@cdf6c1fa76f9f475f3d7449005a359c84ca0f306"
        for step in cache_steps
    )
    build_cache, npm_cache = cache_steps
    assert build_cache["with"]["path"] == "${{ runner.temp }}/g2-buildkit-cache"
    for token in (
        "backend/Dockerfile",
        "backend/uv.lock",
        "frontend/Dockerfile",
        "frontend/package-lock.json",
        "deploy/postgres.Dockerfile",
        "deploy/redis.Dockerfile",
    ):
        assert token in build_cache["with"]["key"]
    assert npm_cache["with"]["path"] == "${{ runner.temp }}/g2-npm-cache"
    assert "frontend/package-lock.json" in npm_cache["with"]["key"]

    gate = next(step for step in g2["steps"] if step.get("id") == "authoritative-g2")
    assert gate["env"] == {
        "G2_DOCKER_CACHE_DIR": "${{ runner.temp }}/g2-buildkit-cache",
        "G2_NPM_CACHE_DIR": "${{ runner.temp }}/g2-npm-cache",
        "G2_TIMING_FILE": "${{ runner.temp }}/g2-timing.jsonl",
    }
    for variable in ("G2_DOCKER_CACHE_DIR", "G2_NPM_CACHE_DIR", "G2_TIMING_FILE"):
        assert f'{variable}="${variable}"' in gate["run"]

    summary = next(
        step for step in g2["steps"] if step.get("name") == "Render G2 timing summary"
    )
    assert summary["if"] == "${{ always() }}"
    assert summary["env"] == {"G2_TIMING_FILE": "${{ runner.temp }}/g2-timing.jsonl"}
    assert "steps.authoritative-g2.outcome" in summary["run"]
    assert "scripts/g2_timing.py render" in summary["run"]
    assert "--expected-stages" in summary["run"]
    assert "steps.authoritative-g2.outputs.expected_stages" in summary["run"]
    assert 'GITHUB_STEP_SUMMARY' in summary["run"]
    assert 'expected_stages="5,6,7"' in gate["run"]
    assert 'expected_stages+=",9"' in gate["run"]


def test_owner_pr_automation_only_opens_draft_for_independent_review() -> None:
    draft = load_workflow(AUTO_DRAFT_WORKFLOW)
    draft_triggers = workflow_triggers(draft)
    assert draft_triggers == {"push": {"branches-ignore": ["main"]}}
    assert draft["permissions"] == {
        "contents": "read",
        "pull-requests": "write",
    }
    draft_job = draft["jobs"]["open"]
    assert draft_job["if"] == "${{ github.actor == github.repository_owner }}"
    draft_commands = job_commands(draft_job)
    assert '"draft=true"' in draft_commands
    assert "Mark Ready only after independent review is available." in draft_commands
    assert "gh pr ready" not in draft_commands
    assert "gh pr merge" not in draft_commands
    assert not AUTO_MERGE_WORKFLOW.exists()


def test_release_workflow_is_manual_or_tag_only_and_fail_closed() -> None:
    workflow = load_workflow(RELEASE_WORKFLOW)
    triggers = workflow_triggers(workflow)

    assert set(triggers) == {"workflow_dispatch", "push"}
    assert triggers["workflow_dispatch"] == {}
    assert triggers["push"]["tags"] == ["v*"]
    assert workflow["permissions"] == {
        "actions": "read",
        "contents": "read",
        "id-token": "write",
        "attestations": "write",
        "artifact-metadata": "write",
    }

    jobs = workflow["jobs"]
    assert set(jobs) == {"build-scan", "close-release"}
    build_job = jobs["build-scan"]
    close_job = jobs["close-release"]
    assert build_job["name"] == "build-scan"
    assert build_job["timeout-minutes"] == 120
    assert close_job["name"] == "close-release"
    assert close_job["needs"] == "build-scan"
    assert close_job["timeout-minutes"] == 30

    build_commands = job_commands(build_job)
    for command in (
        'test "$(git rev-parse HEAD)" = "$GITHUB_SHA"',
        "scripts/release_metadata.py",
        "bash scripts/verify_release.sh",
        "release-evidence/images",
        "release-evidence/scans",
        "--sbom-dir",
        "--archive-dir",
        "--scan-dir",
        'test "$GITHUB_REF" = "refs/heads/main"',
        'git merge-base --is-ancestor "$GITHUB_SHA" refs/remotes/origin/main',
    ):
        assert command in build_commands

    close_commands = job_commands(close_job)
    for command in (
        "chmod 0700",
        "chmod 0600",
        "scripts/create_offline_image_index.py",
        "offline-image-index.json",
    ):
        assert command in close_commands

    source = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    assert source.count("bash scripts/verify_release.sh") == 1
    for removed_contract in (
        "resume_quality_run_id",
        "RESUME_QUALITY_RUN_ID",
        "scripts/verify_all.sh",
        "scripts/check_coverage_gates.py",
        "uv run bandit",
        "npm ci",
        "verify_reproducible_build.sh",
        "reproducibility.json",
    ):
        assert removed_contract not in source
    assert "continue-on-error" not in source
    assert "secrets." not in source
    assert_actions_are_immutable(workflow)

    build_steps = {step["name"]: step for step in build_job["steps"]}
    checkpoint = build_steps["Save the single-build release checkpoint"]
    assert checkpoint["with"] == {
        "name": "release-checkpoint-${{ github.sha }}",
        "path": "${{ runner.temp }}/release-evidence",
        "if-no-files-found": "error",
        "retention-days": 7,
        "compression-level": 0,
        "overwrite": True,
    }

    close_steps = {step["name"]: step for step in close_job["steps"]}
    downloaded = close_steps["Download the single-build release checkpoint"]
    assert downloaded["with"] == {
        "name": "release-checkpoint-${{ github.sha }}",
        "path": "${{ runner.temp }}/release-evidence",
    }
    assert "actions/attest@36051bcae73b7c2a8a6945a48cbf80953c6baa35" in source
    assert "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in source
    assert "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093" in source
    assert (
        "subject-path: ${{ runner.temp }}/release-evidence/offline-image-index.json"
        in source
    )
    assert "path: ${{ runner.temp }}/release-evidence" in source
    final_upload = close_steps["Upload the closed offline release evidence directory"]
    assert final_upload["with"]["name"] == (
        "release-evidence-${{ github.sha }}-${{ github.run_attempt }}"
    )
    assert final_upload["with"]["compression-level"] == 0


def test_maintenance_documents_ci_and_release_boundaries() -> None:
    documentation = MAINTENANCE.read_text(encoding="utf-8")

    for token in (
        "scripts/dev_check.sh --changed",
        "required `ci-gate`",
        "受保护变更进入 G2 integration",
        "origin/main",
        "Release Gate 只负责制品生成",
        "Trivy",
        "SBOM",
        "reproducibility_proven=false",
    ):
        assert token in documentation
    development = documentation.split("## 日常开发", maxsplit=1)[1].split(
        "## 按需测试部署", maxsplit=1
    )[0]
    assert "scripts/test_update.sh" not in development
