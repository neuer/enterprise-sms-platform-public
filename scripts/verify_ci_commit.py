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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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


LINK_NEXT_RE = re.compile(r"<([^>]+)>\s*;\s*rel=\"?next\"?", re.IGNORECASE)
CHECK_RUNS_PAGE_LIMIT = 50


def parse_link_next(header: str | None) -> str | None:
    if not header:
        return None
    match = LINK_NEXT_RE.search(header)
    if match is None:
        return None
    return match.group(1).strip() or None


def next_page_url(url: str) -> str | None:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    try:
        page = int(query.get("page") or "1")
    except ValueError:
        return None
    query["page"] = str(page + 1)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def github_response(url: str, *, token: str | None) -> tuple[object, Mapping[str, str]]:
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
            document = json.load(response)
            return document, {key: value for key, value in response.headers.items()}
    except (
        OSError,
        TimeoutError,
        urllib.error.URLError,
        UnicodeError,
        json.JSONDecodeError,
    ) as error:
        raise CiEvidenceError("GitHub CI evidence request failed") from error


def github_json(url: str, *, token: str | None) -> object:
    document, _headers = github_response(url, token=token)
    return document


def fetch_check_runs(url: str, *, token: str | None) -> dict[str, Any]:
    """Fail-closed：翻页直到取完；单页 truncated 或页数不足则失败。"""

    collected: list[object] = []
    seen_urls: set[str] = set()
    seen_ids: set[int] = set()
    total_count: int | None = None
    current = url
    pages = 0
    while current:
        pages += 1
        if pages > CHECK_RUNS_PAGE_LIMIT:
            raise CiEvidenceError("check-runs pagination exceeded safety limit")
        if current in seen_urls:
            raise CiEvidenceError("check-runs pagination loop")
        seen_urls.add(current)
        document, headers = github_response(current, token=token)
        if type(document) is not dict:
            raise CiEvidenceError("check-runs response is invalid")
        if document.get("truncated") is True:
            raise CiEvidenceError(
                "check-runs response truncated; paginate via Link or page= until complete"
            )
        raw_total = document.get("total_count")
        if type(raw_total) is not int or raw_total < 0:
            raise CiEvidenceError("check-runs total_count is missing or invalid")
        if total_count is None:
            total_count = raw_total
        elif raw_total != total_count:
            raise CiEvidenceError("check-runs total_count changed across pages")
        runs = document.get("check_runs")
        if type(runs) is not list:
            raise CiEvidenceError("check-runs response is invalid")
        added = 0
        for item in runs:
            run_id = item.get("id") if type(item) is dict else None
            if type(run_id) is int:
                if run_id in seen_ids:
                    continue
                seen_ids.add(run_id)
            collected.append(item)
            added += 1
        next_url = parse_link_next(headers.get("Link") or headers.get("link"))
        if next_url:
            current = next_url
            continue
        if added == 0 or len(collected) >= total_count:
            if len(collected) < total_count:
                raise CiEvidenceError(
                    "check-runs truncated: "
                    f"got {len(collected)} of {total_count}; "
                    "paginate via Link header or page= until complete"
                )
            current = None
            continue
        fallback = next_page_url(current)
        if fallback is not None and fallback not in seen_urls:
            current = fallback
            continue
        raise CiEvidenceError(
            "check-runs truncated: "
            f"got {len(collected)} of {total_count}; "
            "paginate via Link header or page= until complete"
        )
    return {"total_count": total_count or 0, "check_runs": collected}


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
        document = fetch_check_runs(
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
