#!/usr/bin/env python3
"""验证公开 GitHub 仓库某个精确 commit 的 GitHub Actions ci-gate。"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any

SHA_RE = re.compile(r"[0-9a-f]{40}")
REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
GITHUB_ACTIONS_APP_ID = 15368
FULL_CI_CHECK_NAMES = ("backend", "frontend", "security", "g2", "ci-gate")


class CiEvidenceError(RuntimeError):
    """托管 CI 证据缺失、失败或不可验证。"""


def ci_gate_status(
    document: object,
    *,
    commit: str,
    check_name: str = "ci-gate",
) -> str:
    if type(document) is not dict:
        raise CiEvidenceError("check-runs response is invalid")
    runs = document.get("check_runs")
    if type(runs) is not list:
        raise CiEvidenceError("check-runs response is invalid")
    matches: list[Mapping[str, Any]] = []
    for raw in runs:
        if type(raw) is not dict:
            raise CiEvidenceError("check-runs response is invalid")
        app = raw.get("app")
        if (
            raw.get("name") == check_name
            and raw.get("head_sha") == commit
            and type(raw.get("id")) is int
            and raw["id"] > 0
            and type(app) is dict
            and app.get("id") == GITHUB_ACTIONS_APP_ID
            and app.get("slug") == "github-actions"
        ):
            matches.append(raw)
    if not matches:
        return "missing"
    run = max(matches, key=lambda item: item["id"])
    if run.get("status") != "completed":
        return "pending"
    if run.get("conclusion") == "success":
        return "success"
    return "failure"


def full_ci_status(document: object, *, commit: str) -> str:
    """要求同一最新 check suite 的五个精确 Actions check 全部成功。"""

    if type(document) is not dict:
        raise CiEvidenceError("check-runs response is invalid")
    runs = document.get("check_runs")
    if type(runs) is not list:
        raise CiEvidenceError("check-runs response is invalid")
    anchors: list[Mapping[str, Any]] = []
    for raw in runs:
        if type(raw) is not dict:
            raise CiEvidenceError("check-runs response is invalid")
        app = raw.get("app")
        suite = raw.get("check_suite")
        if (
            raw.get("name") in FULL_CI_CHECK_NAMES
            and raw.get("head_sha") == commit
            and type(raw.get("id")) is int
            and raw["id"] > 0
            and type(app) is dict
            and app.get("id") == GITHUB_ACTIONS_APP_ID
            and app.get("slug") == "github-actions"
            and type(suite) is dict
            and type(suite.get("id")) is int
            and suite["id"] > 0
        ):
            anchors.append(raw)
    if not anchors:
        return "missing"
    latest_check = max(anchors, key=lambda item: item["id"])
    latest_suite = latest_check["check_suite"]["id"]
    suite_document = {
        "check_runs": [
            raw
            for raw in runs
            if type(raw.get("check_suite")) is dict
            and raw["check_suite"].get("id") == latest_suite
        ]
    }
    statuses = {
        name: ci_gate_status(suite_document, commit=commit, check_name=name)
        for name in FULL_CI_CHECK_NAMES
    }
    if all(status == "success" for status in statuses.values()):
        return "success"
    if any(status == "failure" for status in statuses.values()):
        return "failure"
    if any(status == "pending" for status in statuses.values()):
        return "pending"
    return "missing"


def github_json(url: str, *, token: str | None) -> object:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "enterprise-sms-test-update",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status != 200:
                raise CiEvidenceError("GitHub CI evidence request failed")
            return json.load(response)
    except (
        OSError,
        TimeoutError,
        urllib.error.URLError,
        UnicodeError,
        json.JSONDecodeError,
    ) as error:
        raise CiEvidenceError("GitHub CI evidence request failed") from error


def github_token(environ: Mapping[str, str] | None = None) -> str | None:
    source = os.environ if environ is None else environ
    token = source.get("GITHUB_TOKEN") or source.get("GH_TOKEN")
    if token:
        return token.strip() or None
    try:
        result = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--check-name", default="ci-gate")
    parser.add_argument("--require-full", action="store_true")
    parser.add_argument("--status-only", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if (
        REPOSITORY_RE.fullmatch(args.repository) is None
        or SHA_RE.fullmatch(args.commit) is None
        or not args.check_name
    ):
        print("ci-evidence: invalid input", file=sys.stderr)
        return 2
    url = (
        f"https://api.github.com/repos/{args.repository}/commits/"
        f"{args.commit}/check-runs?filter=latest&per_page=100"
    )
    try:
        document = github_json(
            url,
            token=github_token(),
        )
        status = ci_gate_status(
            document,
            commit=args.commit,
            check_name=args.check_name,
        )
        full_status = (
            full_ci_status(document, commit=args.commit)
            if args.require_full
            else None
        )
    except CiEvidenceError as error:
        if args.status_only:
            print("ci_gate=unavailable")
            if args.require_full:
                print("full_ci=unavailable")
            return 0
        print(f"ci-evidence: {error}", file=sys.stderr)
        return 1
    print(f"ci_gate={status}")
    if full_status is not None:
        print(f"full_ci={full_status}")
    if args.status_only or (
        status == "success" and (full_status is None or full_status == "success")
    ):
        return 0
    if full_status is not None:
        print(
            "ci-evidence: exact commit full CI is not successful",
            file=sys.stderr,
        )
    else:
        print("ci-evidence: exact commit ci-gate is not successful", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
