#!/usr/bin/env python3
"""在 main squash 提交树等于已通过 PR head 时复用精确 CI 证据。"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from verify_ci_commit import CiEvidenceError, ci_gate_status, github_json

SHA_RE = re.compile(r"[0-9a-f]{40}")
REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def commit_tree(document: object) -> str:
    if type(document) is not dict or type(document.get("tree")) is not dict:
        raise CiEvidenceError("commit response is invalid")
    tree = document["tree"].get("sha")
    if type(tree) is not str or SHA_RE.fullmatch(tree) is None:
        raise CiEvidenceError("commit response is invalid")
    return tree


def merged_pr_head(document: object, *, candidate: str) -> str | None:
    if type(document) is not list:
        raise CiEvidenceError("pull request response is invalid")
    heads: list[str] = []
    for raw in document:
        if type(raw) is not dict:
            raise CiEvidenceError("pull request response is invalid")
        base = raw.get("base")
        head = raw.get("head")
        if (
            raw.get("state") == "closed"
            and raw.get("merged_at") is not None
            and raw.get("merge_commit_sha") == candidate
            and type(base) is dict
            and base.get("ref") == "main"
            and type(head) is dict
            and type(head.get("sha")) is str
            and SHA_RE.fullmatch(head["sha"]) is not None
        ):
            heads.append(head["sha"])
    if len(set(heads)) != 1:
        return None
    return heads[0]


def write_outputs(path: Path, *, reuse: bool, tested_sha: str = "") -> None:
    with path.open("a", encoding="utf-8") as output:
        output.write(f"reuse={str(reuse).lower()}\n")
        output.write(f"tested_sha={tested_sha}\n")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--github-output", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if (
        REPOSITORY_RE.fullmatch(args.repository) is None
        or SHA_RE.fullmatch(args.candidate) is None
    ):
        print("reuse-pr-evidence: invalid input", file=sys.stderr)
        return 2
    if args.event_name != "push":
        write_outputs(args.github_output, reuse=False)
        print("reuse_pr_ci=false reason=not-main-push")
        return 0

    api = f"https://api.github.com/repos/{args.repository}"
    token = os.environ.get("GITHUB_TOKEN")
    try:
        pulls = github_json(
            f"{api}/commits/{args.candidate}/pulls?per_page=100",
            token=token,
        )
        head = merged_pr_head(pulls, candidate=args.candidate)
        if head is None:
            raise CiEvidenceError("unique merged PR is unavailable")
        candidate_tree = commit_tree(
            github_json(f"{api}/git/commits/{args.candidate}", token=token)
        )
        head_tree = commit_tree(github_json(f"{api}/git/commits/{head}", token=token))
        if candidate_tree != head_tree:
            raise CiEvidenceError("merged PR tree does not match main")
        checks = github_json(
            f"{api}/commits/{head}/check-runs?filter=latest&per_page=100",
            token=token,
        )
        if ci_gate_status(checks, commit=head) != "success":
            raise CiEvidenceError("PR ci-gate is not successful")
    except CiEvidenceError:
        write_outputs(args.github_output, reuse=False)
        print("reuse_pr_ci=false reason=evidence-unavailable")
        return 0

    write_outputs(args.github_output, reuse=True, tested_sha=head)
    print(f"reuse_pr_ci=true tested_sha={head}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
