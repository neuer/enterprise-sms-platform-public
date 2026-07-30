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


def test_ci_workflow_selects_fast_checks_before_authoritative_g2() -> None:
    workflow = load_workflow(CI_WORKFLOW)
    triggers = workflow_triggers(workflow)

    assert triggers["pull_request"]["branches"] == ["main"]
    assert triggers["push"]["branches"] == ["**"]
    assert "workflow_dispatch" in triggers
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
        "performance": "${{ steps.classify.outputs.performance }}",
        "release_control": "${{ steps.classify.outputs.release_control }}",
        "reused_pr_sha": "${{ steps.reuse.outputs.tested_sha }}",
    }
    assert changes["steps"][0]["with"]["fetch-depth"] == 0
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

    backend_commands = job_commands(jobs["backend"])
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
    assert jobs["backend"]["needs"] == "changes"
    assert "needs.changes.outputs.backend == 'true'" in jobs["backend"]["if"]

    frontend_commands = job_commands(jobs["frontend"])
    for command in (
        "npm ci",
        "npm audit --audit-level=high",
        "npm run build",
        "npm test",
    ):
        assert command in frontend_commands
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

    assert jobs["g2"]["needs"] == ["changes", "backend", "frontend", "security"]
    for token in (
        "!cancelled()",
        "needs.changes.result == 'success'",
        "needs.changes.outputs.g2 == 'true'",
        "needs.backend.result == 'success'",
        "needs.frontend.result == 'success'",
        "needs.security.result == 'success'",
    ):
        assert token in jobs["g2"]["if"]
    g2_commands = job_commands(jobs["g2"])
    assert "scripts/local_test.sh prepare" in g2_commands
    assert "bash scripts/verify_all.sh" in g2_commands
    assert "--mode integration" in g2_commands
    assert "needs.changes.outputs.performance" in g2_commands
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
    assert 'expected_stages+=",8"' in gate["run"]
    assert 'expected_stages+=",10"' in gate["run"]


def test_owner_pr_automation_is_exact_sha_bound_and_never_bypasses_checks() -> None:
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
    assert "Successful push CI will mark this PR Ready and queue squash merge." in (
        draft_commands
    )

    merge = load_workflow(AUTO_MERGE_WORKFLOW)
    merge_triggers = workflow_triggers(merge)
    assert merge_triggers == {
        "workflow_run": {
            "workflows": ["CI"],
            "types": ["completed"],
        }
    }
    assert merge["permissions"] == {
        "contents": "write",
        "pull-requests": "write",
    }
    assert merge["concurrency"] == {
        "group": "auto-merge-owner-pr-${{ github.event.workflow_run.head_branch }}",
        "cancel-in-progress": False,
    }

    jobs = merge["jobs"]
    assert set(jobs) == {"queue"}
    queue = jobs["queue"]
    assert queue["name"] == "auto-merge-owner-pr"
    assert queue["timeout-minutes"] == 5
    for condition in (
        "workflow_run.conclusion == 'success'",
        "workflow_run.event == 'push'",
        "workflow_run.path == '.github/workflows/ci.yml'",
        "workflow_run.head_branch != 'main'",
        "workflow_run.actor.login == github.repository_owner",
        "workflow_run.head_repository.full_name == github.repository",
    ):
        assert condition in queue["if"]

    step = queue["steps"][0]
    assert step["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "HEAD_BRANCH": "${{ github.event.workflow_run.head_branch }}",
        "HEAD_SHA": "${{ github.event.workflow_run.head_sha }}",
        "REPOSITORY": "${{ github.repository }}",
    }
    commands = step["run"]
    for token in (
        "set -euo pipefail",
        "gh pr list",
        "--base main",
        "--state open",
        "--limit 2",
        "test \"$count\" == 1",
        "gh pr view",
        "'.headRefOid'",
        '== "$HEAD_SHA"',
        "'.isCrossRepository'",
        "gh pr ready",
        "gh pr merge",
        "--auto",
        "--squash",
        '--match-head-commit "$HEAD_SHA"',
    ):
        assert token in commands
    assert "--admin" not in commands
    assert "secrets." not in AUTO_MERGE_WORKFLOW.read_text(encoding="utf-8")


def test_release_workflow_is_manual_or_tag_only_and_fail_closed() -> None:
    workflow = load_workflow(RELEASE_WORKFLOW)
    triggers = workflow_triggers(workflow)

    assert set(triggers) == {"workflow_dispatch", "push"}
    assert triggers["push"]["tags"] == ["v*"]
    assert workflow["permissions"] == {
        "contents": "read",
        "id-token": "write",
        "attestations": "write",
        "artifact-metadata": "write",
    }

    jobs = workflow["jobs"]
    assert set(jobs) == {"release-gate"}
    release_job = jobs["release-gate"]
    assert release_job["timeout-minutes"] == 120
    release_commands = job_commands(release_job)
    for command in (
        'test "$(git rev-parse HEAD)" = "$GITHUB_SHA"',
        "scripts/release_metadata.py",
        "scripts/check_coverage_gates.py",
        "property or concurrency or fault_injection or authorization or idempotency",
        "uv run bandit",
        "vuln,misconfig,secret,license",
        "bash scripts/verify_all.sh",
        "bash scripts/verify_release.sh",
        "bash scripts/verify_reproducible_build.sh",
        "reproducibility.json",
        "--sbom-dir",
    ):
        assert command in release_commands
    assert "npm run --prefix frontend typecheck" not in release_commands
    assert "continue-on-error" not in RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert_actions_are_immutable(workflow)
    source = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    assert "secrets." not in source
    assert "actions/attest@36051bcae73b7c2a8a6945a48cbf80953c6baa35" in source
    assert "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in source


def test_maintenance_documents_ci_and_release_boundaries() -> None:
    documentation = MAINTENANCE.read_text(encoding="utf-8")

    for token in (
        "scripts/dev_check.sh --changed",
        "required `ci-gate`",
        "受保护变更进入 G2 integration",
        "origin/main",
        "完整质量、安全与 G2 门禁",
        "Trivy",
        "SBOM",
    ):
        assert token in documentation
    development = documentation.split("## 日常开发", maxsplit=1)[1].split(
        "## 按需测试部署", maxsplit=1
    )[0]
    assert "scripts/test_update.sh" not in development
