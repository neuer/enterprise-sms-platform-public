#!/usr/bin/env python3
"""根据事件与安全的 Git 路径差异选择 CI job；未知输入一律失败关闭。"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

DEPLOY_SCRIPTS = Path(__file__).resolve().parents[1] / "deploy" / "scripts"
sys.path.insert(0, str(DEPLOY_SCRIPTS))

from test_update_contract import protected_change_category  # noqa: E402


@dataclass(frozen=True)
class Classification:
    backend: bool
    frontend: bool
    g2: bool
    security: bool
    categories: frozenset[str]
    full_fallback: bool = False


RuleResult = tuple[bool, bool, bool, bool, str]

NONE: RuleResult = (False, False, False, False, "ordinary-doc")
BACKEND: RuleResult = (True, False, False, False, "backend-check")
FRONTEND: RuleResult = (False, True, False, False, "frontend")
FRONTEND_SECURITY: RuleResult = (False, True, False, True, "frontend-security")
BACKEND_G2: RuleResult = (True, False, True, True, "backend-production")
BACKEND_CRITICAL: RuleResult = (True, False, True, True, "backend-critical")
FRONTEND_G2: RuleResult = (False, True, True, True, "frontend-runtime")
FULL: RuleResult = (True, True, True, True, "unknown")
VENDOR_LIVE: RuleResult = (True, True, True, True, "vendor-live")

ROOT_SPEC_DOCS = {
    "AGENTS.md",
    "AUTOPILOT.md",
    "BOOTSTRAP.md",
    "CLAUDE.md",
    "HANDOVER.md",
    "PRD.md",
    "PROGRESS.md",
    "RELEASE.md",
    "TASKS.md",
}
BACKEND_RUNTIME_FILES = {
    "backend/Dockerfile",
    "backend/alembic.ini",
    "backend/pyproject.toml",
    "backend/uv.lock",
}
CI_CONTROL_SCRIPTS = {
    "scripts/classify_ci_changes.py",
    "scripts/verify_ci_results.py",
}
ZERO_SHA = "0" * 40
FORCE_ALL_EVENTS = {"workflow_dispatch", "schedule"}


class ChangeDetectionError(RuntimeError):
    """Git 差异无法可靠取得。"""


def _rule(path: str) -> tuple[RuleResult, bool]:
    protected_category = protected_change_category(path)
    if protected_category == "vendor-live":
        return VENDOR_LIVE, False
    if protected_category == "backend-critical":
        return BACKEND_CRITICAL, False
    if path.startswith("docs/plans/") or fnmatchcase(path, "docs/TEST-REPORT-*"):
        return NONE, False
    if path in CI_CONTROL_SCRIPTS or path.startswith(".github/"):
        return BACKEND_G2, False
    if path in {"frontend/Dockerfile", "deploy/nginx.conf"}:
        return FRONTEND_G2, False
    if path in {"frontend/package.json", "frontend/package-lock.json"}:
        return FRONTEND_SECURITY, False
    if path.startswith("frontend/"):
        return FRONTEND, False
    if path.startswith(("backend/tests/", "backend/scripts_support/")):
        return BACKEND, False
    if path.startswith("backend/migrations/"):
        return BACKEND_G2, False
    if path.startswith("backend/app/"):
        return BACKEND, False
    if path in BACKEND_RUNTIME_FILES:
        return BACKEND_G2, False
    if path == "backend/README.md":
        return BACKEND, False
    if path.startswith("deploy/") and path.endswith(".md"):
        return BACKEND, False
    if path.startswith("deploy/"):
        return BACKEND_G2, False
    if path.startswith("scripts/check_") and path.endswith(".py"):
        return BACKEND, False
    if path.startswith("scripts/"):
        return BACKEND_G2, False
    if path in ROOT_SPEC_DOCS or path in {"openapi.yaml", "schema.sql"}:
        return BACKEND, False
    if path.startswith("docs/"):
        return BACKEND, False
    return FULL, True


def classify_paths(paths: Iterable[str]) -> Classification:
    path_list = list(paths)
    if not path_list:
        return Classification(True, True, True, True, frozenset({"empty-diff"}), True)

    backend = frontend = g2 = security = fallback = False
    categories: set[str] = set()
    for path in path_list:
        pure_path = PurePosixPath(path)
        if not path or pure_path.is_absolute() or ".." in pure_path.parts:
            rule, path_fallback = FULL, True
        else:
            rule, path_fallback = _rule(path)
        path_backend, path_frontend, path_g2, path_security, category = rule
        backend |= path_backend
        frontend |= path_frontend
        g2 |= path_g2
        security |= path_security
        fallback |= path_fallback
        categories.add(category)

    return Classification(backend, frontend, g2, security, frozenset(categories), fallback)


def _git_paths(repo: Path, revision_range: str) -> list[str]:
    try:
        completed = subprocess.run(
            ["git", "diff", "--name-only", "--no-renames", "-z", revision_range],
            cwd=repo,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ChangeDetectionError("git diff failed") from exc
    return [os.fsdecode(item) for item in completed.stdout.split(b"\0") if item]


def _full(reason: str) -> Classification:
    return Classification(True, True, True, True, frozenset({reason}), True)


def _without_repeated_push_g2(result: Classification) -> Classification:
    """可靠的已知 main push 不重复 PR/本地 G2；异常分类继续失败关闭。"""

    if not result.g2 or result.full_fallback:
        return result
    return Classification(
        backend=result.backend,
        frontend=result.frontend,
        g2=False,
        security=result.security,
        categories=result.categories | {"main-push-no-repeat-g2"},
        full_fallback=False,
    )


def _classify_event_with_count(
    *,
    repo: Path,
    event_name: str,
    base_sha: str,
    before_sha: str,
    head_sha: str,
) -> tuple[Classification, int]:
    if event_name in FORCE_ALL_EVENTS:
        return _full(f"forced-{event_name}"), 0
    if event_name == "pull_request":
        if not base_sha or not head_sha:
            return _full("missing-pr-sha"), 0
        revision_range = f"{base_sha}...{head_sha}"
    elif event_name == "push":
        if not before_sha or before_sha == ZERO_SHA or not head_sha:
            return _full("missing-push-sha"), 0
        revision_range = f"{before_sha}..{head_sha}"
    else:
        return _full("unsupported-event"), 0

    paths = _git_paths(repo, revision_range)
    result = classify_paths(paths)
    if event_name == "push":
        result = _without_repeated_push_g2(result)
    return result, len(paths)


def classify_event(
    *,
    repo: Path,
    event_name: str,
    base_sha: str,
    before_sha: str,
    head_sha: str,
) -> Classification:
    result, _ = _classify_event_with_count(
        repo=repo,
        event_name=event_name,
        base_sha=base_sha,
        before_sha=before_sha,
        head_sha=head_sha,
    )
    return result


def write_github_outputs(path: Path, result: Classification) -> None:
    lines = (
        f"backend={str(result.backend).lower()}\n"
        f"frontend={str(result.frontend).lower()}\n"
        f"g2={str(result.g2).lower()}\n"
        f"security={str(result.security).lower()}\n"
    )
    with path.open("a", encoding="utf-8") as output:
        output.write(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--base-sha", default="")
    parser.add_argument("--before-sha", default="")
    parser.add_argument("--head-sha", default="")
    parser.add_argument("--github-output", required=True, type=Path)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result, changed_count = _classify_event_with_count(
            repo=args.repo,
            event_name=args.event_name,
            base_sha=args.base_sha,
            before_sha=args.before_sha,
            head_sha=args.head_sha,
        )
        write_github_outputs(args.github_output, result)
    except (ChangeDetectionError, OSError):
        print("CI change classification failed", file=sys.stderr)
        return 2

    categories = ",".join(sorted(result.categories))
    fallback = str(result.full_fallback).lower()
    print(
        f"changed_files={changed_count} categories={categories} full_fallback={fallback}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
