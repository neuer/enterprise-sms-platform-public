from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
RELEASE_WORKFLOW = ROOT / ".github/workflows/release-gate.yml"
BOOTSTRAP = ROOT / "BOOTSTRAP.md"
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
    assert triggers["push"]["branches"] == ["main"]
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
    ):
        assert command in changes_commands

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
        "npm run typecheck",
        "npm test",
    ):
        assert command in frontend_commands
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
    assert gate["if"] == "${{ !cancelled() }}"
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
    assert 'GITHUB_STEP_SUMMARY' in summary["run"]


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
    assert "continue-on-error" not in RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert_actions_are_immutable(workflow)
    source = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    assert "secrets." not in source
    assert "actions/attest@36051bcae73b7c2a8a6945a48cbf80953c6baa35" in source
    assert "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in source


def test_bootstrap_documents_ci_checks_and_public_enforcement() -> None:
    documentation = BOOTSTRAP.read_text(encoding="utf-8")

    for check_name in (
        "`changes`",
        "`backend`",
        "`frontend`",
        "`security`",
        "`g2`",
        "`ci-gate`",
    ):
        assert check_name in documentation
    for token in (
        "按变更文件",
        "docs/plans/",
        "未知路径",
        "高风险 PR",
        "普通后端业务 PR",
        "`main` push",
        "不重复运行 G2",
        "每天",
        "02:17",
        "workflow_dispatch",
        "本地 `bash scripts/verify_all.sh`",
        "`main` 唯一 required check",
        "绑定 GitHub Actions 应用",
        "--mode integration",
        "tree 与",
    ):
        assert token in documentation
    assert "私有归档仓库" in documentation
    assert "禁止 force-push/delete" in documentation
    assert "Release Gate" in documentation
    assert "不作为日常 PR" in documentation
    assert "生产 secrets" in documentation
