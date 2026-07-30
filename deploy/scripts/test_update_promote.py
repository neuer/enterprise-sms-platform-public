#!/usr/bin/env python3
"""在代码树完全相同时把测试服务器 Git 身份提升到正式 main commit。"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from test_secure_access_manager import require_test_host_marker
from test_update_backup import require_inherited_lifecycle_lock

SHA_RE = re.compile(r"[0-9a-f]{40}")
REF_RE = re.compile(r"origin/[A-Za-z0-9._/-]+")
CANONICAL_ROOT = Path("/opt/sms-platform")
CANONICAL_RUNTIME_ROOT = Path("/run/sms-platform/secrets")


class TestUpdatePromoteError(RuntimeError):
    """测试基线身份提升不符合安全合同。"""


def command(*arguments: str, root: Path) -> str:
    try:
        completed = subprocess.run(
            list(arguments),
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise TestUpdatePromoteError("controlled Git command failed") from error
    return completed.stdout.strip()


def promote(*, root: Path, source_ref: str, target: str) -> dict[str, object]:
    if (
        root != CANONICAL_ROOT
        or REF_RE.fullmatch(source_ref) is None
        or ".." in source_ref
        or SHA_RE.fullmatch(target) is None
    ):
        raise TestUpdatePromoteError("promotion input is invalid")
    require_test_host_marker(expected_uid=0)
    require_inherited_lifecycle_lock(CANONICAL_RUNTIME_ROOT)
    if command("git", "status", "--porcelain", root=root):
        raise TestUpdatePromoteError("server checkout must be clean")

    base = command("git", "rev-parse", "HEAD", root=root)
    if SHA_RE.fullmatch(base) is None:
        raise TestUpdatePromoteError("server commit is invalid")
    branch = source_ref.removeprefix("origin/")
    fetched_ref = "refs/test-updates/promote/source"
    command(
        "git",
        "fetch",
        "--prune",
        "--no-tags",
        "origin",
        f"+refs/heads/{branch}:{fetched_ref}",
        root=root,
    )
    resolved = command("git", "rev-parse", f"{fetched_ref}^{{commit}}", root=root)
    if resolved != target:
        raise TestUpdatePromoteError("promotion target is not the pushed source ref")

    base_tree = command("git", "rev-parse", f"{base}^{{tree}}", root=root)
    target_tree = command("git", "rev-parse", f"{target}^{{tree}}", root=root)
    if base_tree != target_tree:
        raise TestUpdatePromoteError("promotion requires identical Git trees")
    if base != target:
        command("git", "checkout", "--detach", target, root=root)
    if (
        command("git", "rev-parse", "HEAD", root=root) != target
        or command("git", "status", "--porcelain", root=root)
    ):
        raise TestUpdatePromoteError("promotion did not reach a clean target")
    return {
        "base_commit": base,
        "commit": target,
        "source_ref": source_ref,
        "state": "promoted" if base != target else "already_current",
        "tree": target_tree,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--target", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if os.geteuid() != 0:
            raise TestUpdatePromoteError("promotion requires root")
        result = promote(
            root=args.root.resolve(),
            source_ref=args.source_ref,
            target=args.target,
        )
    except Exception:
        print("test-update promote blocked", file=sys.stderr)
        return 1
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
