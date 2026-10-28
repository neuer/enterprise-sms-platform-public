#!/usr/bin/env python3
"""按 D122 口径输出复杂度治理指标，供每月与基线对比。"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

from any_budget import count_explicit_any

ROOT = Path(__file__).resolve().parents[1]
EXCEPT_EXCEPTION = re.compile(r"\bexcept\s+Exception\b")
FIX_SUBJECT = re.compile(r"^fix\b", re.IGNORECASE)
FRONTEND_SUFFIXES = (".vue", ".ts", ".css")


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout


def tree_files(ref: str) -> dict[str, str]:
    """返回 ref 下全部受跟踪文件的文本内容；二进制或非 UTF-8 文件跳过。"""

    paths = [line for line in _git("ls-tree", "-r", "--name-only", ref).splitlines() if line]
    batch = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        input="".join(f"{ref}:{path}\n" for path in paths).encode(),
    ).stdout
    files: dict[str, str] = {}
    offset = 0
    for path in paths:
        header_end = batch.index(b"\n", offset)
        size = int(batch[offset:header_end].split()[2])
        body = batch[header_end + 1 : header_end + 1 + size]
        offset = header_end + 1 + size + 1
        try:
            files[path] = body.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return files


def _lines(text: str) -> int:
    return text.count("\n")


def _explicit_any(text: str) -> int:
    try:
        return count_explicit_any(text)
    except SyntaxError:
        return 0


def _select(files: Mapping[str, str], prefix: str, suffixes: tuple[str, ...]) -> dict[str, str]:
    return {
        path: text
        for path, text in files.items()
        if path.startswith(prefix) and path.endswith(suffixes)
    }


def compute_metrics(files: Mapping[str, str], subjects: Iterable[str]) -> dict[str, int]:
    """D122 指标：体量、超大文件、裸异常、显式 Any、迁移数、fix 占比与 AGENTS.md 字节数。"""

    app = _select(files, "backend/app/", (".py",))
    tests = _select(files, "backend/tests/", (".py",))
    frontend = _select(files, "frontend/src/", FRONTEND_SUFFIXES)
    ops = {
        path: text
        for path, text in files.items()
        if path.startswith(("scripts/", "deploy/"))
        and (path.endswith((".py", ".sh")) or text.startswith("#!"))
    }
    migrations = _select(files, "backend/migrations/versions/", (".py",))
    recent = list(subjects)
    return {
        "app_lines": sum(map(_lines, app.values())),
        "app_modules": len(app),
        "app_files_over_800": sum(_lines(text) > 800 for text in app.values()),
        "test_lines": sum(map(_lines, tests.values())),
        "test_files": len(tests),
        "frontend_lines": sum(map(_lines, frontend.values())),
        "frontend_vue_over_600": sum(
            _lines(text) > 600 for path, text in frontend.items() if path.endswith(".vue")
        ),
        "ops_lines": sum(map(_lines, ops.values())),
        "migrations": len(migrations),
        "app_except_exception": sum(
            len(EXCEPT_EXCEPTION.findall(text)) for text in app.values()
        ),
        "app_explicit_any": sum(map(_explicit_any, app.values())),
        "recent_commits": len(recent),
        "recent_fix_commits": sum(bool(FIX_SUBJECT.match(subject)) for subject in recent),
        "agents_bytes": len(files.get("AGENTS.md", "").encode("utf-8")),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--commits", type=int, default=150)
    args = parser.parse_args(argv)
    subjects = _git("log", f"-{args.commits}", "--format=%s", args.ref).splitlines()
    metrics = compute_metrics(tree_files(args.ref), subjects)
    print(json.dumps({"ref": _git("rev-parse", "--short", args.ref).strip(), **metrics},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
