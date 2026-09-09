#!/usr/bin/env python3
"""在不可变候选工作树执行本地门禁；缓存只在本机复用，不授权 CI 免检。"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from check_pre_vcs_gates import (
    CHECK_MIGRATION,
    CHECK_VENDOR_PG,
    GateError,
    execute_plan,
    isolated_check_env,
    plan_for_paths,
    require_git_hooks,
)


def git(root: Path, *args: str, env: dict[str, str] | None = None) -> str:
    """Git 失败必须传播，不能当作空变更。"""
    return subprocess.check_output(
        ["git", *args],
        cwd=root,
        env=env if env is not None else isolated_check_env(),
        text=True,
        stderr=subprocess.PIPE,
    ).rstrip("\n")


def working_tree(root: Path) -> str:
    """通过独立 index 捕获工作内容，不修改用户暂存区。"""
    with tempfile.TemporaryDirectory(prefix="sms-gate-index-") as directory:
        env = isolated_check_env()
        env["GIT_INDEX_FILE"] = str(Path(directory) / "index")
        git(root, "read-tree", "HEAD", env=env)
        git(root, "add", "-A", env=env)
        return git(root, "write-tree", env=env)


def backend_python() -> str:
    """选择实际运行后端检查的 Python 3.12，而非 hook 启动解释器。"""
    env = isolated_check_env()
    for name in ("UV_PROJECT_ENVIRONMENT", "VIRTUAL_ENV", "PYTHONHOME", "UV_PYTHON"):
        env.pop(name, None)
    return subprocess.check_output(
        ["uv", "python", "find", "3.12"], env=env, text=True,
        cwd=Path(__file__).resolve().parents[1],
    ).strip()


def cache_key(tree: str, base: str, arguments: Sequence[str]) -> str:
    """将候选内容、基线、执行选择和工具版本全部绑定。"""
    runtime = backend_python()
    versions = [platform.platform(), sys.version, runtime,
                subprocess.check_output([runtime, "--version"], text=True).strip()]
    for command in ("uv", "node", "npm"):
        version = subprocess.check_output([command, "--version"], text=True).strip()
        if command == "node" and not version.startswith("v24."):
            raise GateError("Node 24 is required; activate mise.toml before running local gates")
        versions.append(version)
    payload = [2, tree, base, list(arguments), versions]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def test_environment() -> dict[str, str]:
    """本地门禁不继承外部数据库或认证 Redis 的连接。"""
    env = isolated_check_env()
    for key in list(env):
        if key.endswith("_POSTGRES_DSN") or key == "AUTH_GUARD_REDIS_URL":
            del env[key]
    for name in ("UV_PROJECT_ENVIRONMENT", "VIRTUAL_ENV", "PYTHONHOME"):
        env.pop(name, None)
    env.update(UV_LOCKED="1", UV_PYTHON=backend_python(),
               ENVIRONMENT="test", DEBUG="1", AUTH_MOCK="1", VENDOR_MOCK="1")
    return env


def check_candidate(
    root: Path,
    *,
    mode: str,
    commit: str = "",
    arguments: Sequence[str] = (),
) -> None:
    """dev、commit、push 使用同一份候选内容运行并复用完整本地检查。"""
    require_git_hooks(root)
    args = list(arguments) or ["--changed"]
    if args[0] not in {"--changed", "--backend", "--frontend", "--all"}:
        raise GateError("unknown dev-check mode")
    if args[0] == "--changed" and len(args) > 2:
        raise GateError("--changed accepts one base ref")
    base_ref = args[1] if args[0] == "--changed" and len(args) == 2 else "origin/main"
    base = git(root, "rev-parse", "--verify", f"{base_ref}^{{commit}}")
    parent = git(root, "rev-parse", "--verify", f"{commit or 'HEAD'}^{{commit}}")
    # 不相关历史必须失败，不能产生零检查的成功缓存。
    ancestor = git(root, "merge-base", base, parent)
    tree = (
        working_tree(root)
        if mode == "dev"
        else git(root, "write-tree", env=os.environ.copy())
        if mode == "commit"
        else git(root, "rev-parse", f"{parent}^{{tree}}")
    )
    normalized = ["--changed", ancestor] if args[0] == "--changed" else args
    key = cache_key(tree, ancestor, normalized)
    cache = Path(git(root, "rev-parse", "--absolute-git-dir")) / "sms-gate-cache" / key
    if cache.is_file() and cache.read_text() == tree + "\n":
        print(f"dev-check: reused local success tree={tree}", flush=True)
        return
    env = test_environment()
    env.update(
        GIT_AUTHOR_NAME="Local gate snapshot",
        GIT_COMMITTER_NAME="Local gate snapshot",
        GIT_AUTHOR_EMAIL="gate@localhost",
        GIT_COMMITTER_EMAIL="gate@localhost",
    )
    candidate = git(root, "commit-tree", tree, "-p", parent, "-m", "Local gate snapshot", env=env)
    with tempfile.TemporaryDirectory(prefix="sms-gate-snapshot-") as directory:
        snapshot = Path(directory) / "source"
        git(root, "worktree", "add", "--detach", str(snapshot), candidate, env=env)
        try:
            paths = git(
                snapshot, "diff", "--name-only", "--no-renames", "-z", ancestor, candidate
            ).split("\0")
            diffs = {
                path: git(snapshot, "diff", "-U0", ancestor, candidate, "--", path)
                for path in paths
                if path == "schema.sql" or path.startswith("backend/migrations/")
            }
            plan = plan_for_paths(paths, root=snapshot, diffs=diffs)
            # Python 环境仅属于本次候选；安装严格遵守提交中的 lock。
            subprocess.run(
                ["uv", "sync", "--locked"], cwd=snapshot / "backend", env=env, check=True
            )
            frontend = args[0] in {"--frontend", "--all"} or any(
                p.startswith("frontend/") for p in paths
            )
            if frontend:
                subprocess.run(["npm", "ci"], cwd=snapshot / "frontend", env=env, check=True)
            print(f"dev-check: checking tree={tree}", flush=True)
            subprocess.run(
                ["bash", "scripts/dev_check_impl.sh", *normalized],
                cwd=snapshot,
                env=env,
                check=True,
            )
            # 廉价检查已由 dev_check 完成；迁移及真实数据库语义只执行一次。
            plan.checks = {
                k: v for k, v in plan.checks.items() if k in {CHECK_MIGRATION, CHECK_VENDOR_PG}
            }
            if args[0] in {"--changed", "--all"}:
                subprocess.run(
                    ["bash", "scripts/local_test.sh", "prepare"], cwd=snapshot, env=env, check=True
                )
                # execute_plan 自行注入 Mock；临时移除外部 DSN 后恢复调用者环境。
                previous = os.environ.copy()
                try:
                    os.environ.clear()
                    os.environ.update(env)
                    execute_plan(snapshot, plan)
                finally:
                    os.environ.clear()
                    os.environ.update(previous)
            current = (
                working_tree(root)
                if mode == "dev"
                else git(root, "write-tree", env=os.environ.copy())
                if mode == "commit"
                else tree
            )
            if current != tree:
                raise GateError("candidate changed while checks were running; rerun dev_check")
            if git(snapshot, "status", "--porcelain", "--untracked-files=no"):
                raise GateError("checks modified tracked candidate content; refusing success cache")
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(tree + "\n")
            cache.chmod(0o600)
        finally:
            git(root, "worktree", "remove", "--force", str(snapshot), env=env)


def main() -> int:
    try:
        root = Path(__file__).resolve().parents[1]
        check_candidate(root, mode="dev", arguments=sys.argv[1:])
        return 0
    except (GateError, OSError, subprocess.CalledProcessError) as exc:
        print(f"dev-check: failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
