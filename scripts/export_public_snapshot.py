#!/usr/bin/env python3
"""从已提交 Git tree 原子导出无历史、无内部证据的公开快照。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

from check_public_readiness import (
    POLICY_PATH,
    PublicPolicy,
    check_repository,
    is_excluded,
    load_policy,
)

ROOT = Path(__file__).resolve().parents[1]


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise ValueError("unable to resolve or archive the requested Git ref")
    return completed.stdout.strip()


def _remove_private_paths(root: Path, policy: PublicPolicy) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if not path.exists():
            continue
        relative = path.relative_to(root).as_posix()
        if not is_excluded(relative, policy):
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()


def export_snapshot(
    repository: Path,
    *,
    ref: str,
    output: Path,
    policy_path: Path,
) -> str:
    """导出并校验目标 commit；成功返回完整 source SHA。"""

    repository = repository.resolve()
    output = output.resolve()
    if output.exists():
        raise ValueError("public snapshot output must not already exist")
    output.parent.mkdir(parents=True, exist_ok=True)
    commit = _git(repository, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if not re_full_sha(commit):
        raise ValueError("requested Git ref did not resolve to a full commit")
    policy = load_policy(policy_path)
    policy_digest = hashlib.sha256(policy_path.read_bytes()).hexdigest()
    temporary_root = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent)
    )
    archive_path = temporary_root.with_suffix(".tar")
    content_root = temporary_root / "content"
    try:
        subprocess.run(
            [
                "git",
                "archive",
                "--format=tar",
                f"--output={archive_path}",
                commit,
            ],
            cwd=repository,
            check=True,
            capture_output=True,
        )
        content_root.mkdir()
        with tarfile.open(archive_path, mode="r:") as archive:
            archive.extractall(content_root, filter="data")
        _remove_private_paths(content_root, policy)
        (content_root / "PUBLIC-SNAPSHOT.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source_commit": commit,
                    "policy_sha256": policy_digest,
                    "history_included": False,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        findings = check_repository(content_root, policy=policy, snapshot=True)
        if findings:
            raise ValueError(
                f"public snapshot readiness check failed with {len(findings)} finding(s)"
            )
        os.replace(content_root, output)
    except (OSError, subprocess.CalledProcessError, tarfile.TarError) as error:
        raise ValueError("public snapshot export failed") from error
    finally:
        archive_path.unlink(missing_ok=True)
        shutil.rmtree(temporary_root, ignore_errors=True)
    return commit


def re_full_sha(value: str) -> bool:
    return len(value) == 40 and all(character in "0123456789abcdef" for character in value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=ROOT)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--policy", type=Path, default=POLICY_PATH)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        commit = export_snapshot(
            args.repository,
            ref=args.ref,
            output=args.output,
            policy_path=args.policy.resolve(),
        )
    except ValueError as error:
        print(f"公开快照导出失败: {error}")
        return 1
    print(f"公开快照导出完成: source_commit={commit} output={args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
