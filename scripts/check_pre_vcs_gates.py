#!/usr/bin/env python3
"""按变更路径决定 commit/push 前必须跑哪些本地门禁，而不是一刀切。

主入口是 git hook：``git commit`` → ``.githooks/pre-commit``，
``git push`` → ``.githooks/pre-push``。一次启用：
``scripts/install_git_hooks.sh``（仓库本地 ``core.hooksPath=.githooks``）。
Cursor ``beforeShellExecution`` 只拦 ``--no-verify`` / ``-n`` /
``core.hooksPath=/dev/null``，以及尚未安装 hook 的 commit/push。

默认只跑当前变更集合真正需要的最便宜检查。schema/Alembic 才强制
``check_migration.py``；in-flight / vendor-postgres 恢复面才强制
``verify_vendor_postgres_recovery.sh``。缺 Docker/DSN 时，只有已被点名的
检查失败关闭，未点名的检查直接跳过。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

CHECK_RUFF = "ruff"
CHECK_PYTEST_CHANGED = "pytest_changed"
CHECK_FRONTEND = "frontend"
CHECK_MIGRATION = "check_migration"
CHECK_VENDOR_PG = "vendor_postgres_recovery"
CHECK_SPEC = "spec_consistency"
CHECK_CI_CONTRACTS = "ci_contracts"

Mode = Literal["commit", "push"]
GIT_HOOK_ENV_KEYS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_PREFIX",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_QUARANTINE_PATH",
)

INFLIGHT_SQL_TOKENS = (
    "send_inflight",
    "check_send_inflight_balance_conservation",
    "reconcile_send_inflight",
    "send_inflight_reservation",
    "send_inflight_balance",
    "send_inflight_reconcile",
)
INFLIGHT_PATH_MARKERS = ("send_inflight", "test_inflight_")
VENDOR_RECOVERY_TEST_RE = re.compile(r"tests/integration/test_[A-Za-z0-9_]+\.py")
GIT_VALUE_OPTIONS = {
    "-C",
    "-c",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--config-env",
    "--exec-path",
}
COMMAND_SEPARATORS = {"&&", "||", ";", "|"}
SPEC_DOCS = frozenset(
    {
        "AGENTS.md",
        "CLAUDE.md",
        "HANDOVER.md",
        "MAINTENANCE.md",
        "PRD.md",
        "PUBLICATION.md",
        "docs/DECISIONS.md",
        "docs/TRACEABILITY.md",
        "docs/UAT.md",
        "docs/vendor-api.md",
        "docs/ui-design.md",
    }
)
ORDINARY_DOC_PREFIXES = ("docs/plans/",)
ORDINARY_DOC_NAMES = frozenset(
    {
        "README.md",
        "CONTRIBUTING.md",
        "LICENSE",
        "PROGRESS.md",
        "SECURITY.md",
        "VERSION",
    }
)
CI_CONTRACT_MAP: tuple[tuple[str, tuple[str, ...]], ...] = (
    (".github/workflows/ci.yml", ("tests/test_ci_workflows.py",)),
    (".github/", ("tests/test_ci_workflows.py",)),
    (
        "scripts/verify_vendor_postgres_recovery.sh",
        ("tests/test_gate_scripts.py", "tests/test_ci_workflows.py"),
    ),
    ("scripts/verify_vendor_live_test.sh", ("tests/test_gate_scripts.py",)),
    ("scripts/dev_check.sh", ("tests/test_gate_scripts.py",)),
    ("scripts/test_update.sh", ("tests/test_test_update_contract.py",)),
    ("scripts/classify_ci_changes.py", ("tests/test_ci_change_classifier.py",)),
    ("scripts/verify_ci_results.py", ("tests/test_ci_workflows.py",)),
    (
        "deploy/scripts/protected_path_policy.py",
        ("tests/test_protected_path_policy.py",),
    ),
    ("backend/tests/test_ci_workflows.py", ("tests/test_ci_workflows.py",)),
    ("backend/tests/test_gate_scripts.py", ("tests/test_gate_scripts.py",)),
    (
        "backend/tests/test_protected_path_policy.py",
        ("tests/test_protected_path_policy.py",),
    ),
    (
        "backend/tests/test_ci_change_classifier.py",
        ("tests/test_ci_change_classifier.py",),
    ),
    ("scripts/check_pre_vcs_gates.py", ("tests/test_pre_vcs_gates.py",)),
    ("scripts/install_git_hooks.sh", ("tests/test_pre_vcs_gates.py",)),
    (".cursor/", ("tests/test_pre_vcs_gates.py",)),
    (".githooks/", ("tests/test_pre_vcs_gates.py",)),
)
MIGRATION_TEST_PREFIXES = (
    "backend/tests/test_migration_",
    "backend/tests/test_migration_checker.py",
    "backend/tests/test_migration_baseline.py",
)


class GateError(RuntimeError):
    """当前变更集合要求的门禁无法完成或未通过。"""


@dataclass(frozen=True)
class GitInvocation:
    """从 shell 命令解析出的 git commit/push 调用。"""

    subcommand: Literal["commit", "push"]
    args: tuple[str, ...]
    skips_hooks: bool


@dataclass
class GatePlan:
    """一次 commit/push 需要执行的检查并集。"""

    checks: dict[str, list[str]] = field(default_factory=dict)
    ruff_files: list[str] = field(default_factory=list)
    pytest_files: list[str] = field(default_factory=list)
    contract_tests: list[str] = field(default_factory=list)

    def add(self, check: str, path: str) -> None:
        self.checks.setdefault(check, [])
        if path not in self.checks[check]:
            self.checks[check].append(path)

    def required(self) -> tuple[str, ...]:
        return tuple(sorted(self.checks))

    def explain(self) -> str:
        if not self.checks:
            return "pre-vcs-gates: no required checks for this change-set"
        lines = ["pre-vcs-gates: required checks"]
        for name in self.required():
            triggers = ", ".join(self.checks[name])
            lines.append(f"  {name} <- {triggers}")
        if self.ruff_files:
            lines.append("  ruff files: " + ", ".join(self.ruff_files))
        if self.pytest_files:
            lines.append("  pytest files: " + ", ".join(self.pytest_files))
        if self.contract_tests:
            lines.append("  contract tests: " + ", ".join(self.contract_tests))
        return "\n".join(lines)


EXPECTED_HOOKS_PATH = ".githooks"
HOOKS_ENABLE_HINT = "run scripts/install_git_hooks.sh to enable local git pre-commit/pre-push"


def local_hooks_path(root: Path) -> str:
    """读取仓库本地 core.hooksPath；未设置则返回空串。"""

    result = subprocess.run(
        ["git", "config", "--local", "--get", "core.hooksPath"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def hooks_path_enabled(root: Path) -> bool:
    """本地 hooksPath 是否指向本仓库 .githooks。"""

    value = local_hooks_path(root)
    if value in {EXPECTED_HOOKS_PATH, str((root / EXPECTED_HOOKS_PATH).resolve())}:
        return True
    if not value:
        return False
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        return candidate.resolve() == (root / EXPECTED_HOOKS_PATH).resolve()
    except OSError:
        return False


def require_git_hooks(root: Path) -> None:
    """hooksPath 未启用或 hook 文件不可执行时失败关闭。"""

    if not hooks_path_enabled(root):
        current = local_hooks_path(root) or "unset"
        raise GateError(
            f"local core.hooksPath is {current}, expected .githooks; {HOOKS_ENABLE_HINT}"
        )
    for name in ("pre-commit", "pre-push"):
        path = root / EXPECTED_HOOKS_PATH / name
        if not path.is_file() or not os.access(path, os.X_OK):
            raise GateError(f"{path} is missing or not executable; {HOOKS_ENABLE_HINT}")


def repo_root_from_cwd(cwd: str | None = None) -> Path:
    """以 git 工作树根为准，找不到则失败关闭。"""

    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=cwd or os.getcwd(),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GateError(result.stderr.strip() or "not a git worktree")
    return Path(result.stdout.strip()).resolve()


def git_output(root: Path, args: Sequence[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or "git command failed"
        raise GateError(detail)
    return result.stdout


def git_z_names(root: Path, args: Sequence[str]) -> list[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip() or "git command failed"
        raise GateError(detail)
    return [item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def vendor_recovery_test_paths(root: Path) -> frozenset[str]:
    """从恢复脚本列出的 integration 测试推导 class 5，避免手写死清单。"""

    script = root / "scripts" / "verify_vendor_postgres_recovery.sh"
    if not script.is_file():
        raise GateError("missing scripts/verify_vendor_postgres_recovery.sh")
    text = script.read_text(encoding="utf-8")
    return frozenset(
        f"backend/{match}" for match in VENDOR_RECOVERY_TEST_RE.findall(text)
    )


def is_ordinary_doc(path: str) -> bool:
    name = Path(path).name
    if path in ORDINARY_DOC_NAMES or name in ORDINARY_DOC_NAMES:
        return True
    if any(path.startswith(prefix) for prefix in ORDINARY_DOC_PREFIXES):
        return True
    return bool(re.fullmatch(r"docs/TEST-REPORT-.*", path))


def is_spec_doc(path: str) -> bool:
    return path in SPEC_DOCS or path == "scripts/check_spec_consistency.py"


def is_python_gate_file(path: str) -> bool:
    if not path.endswith(".py"):
        return False
    return path.startswith(
        ("backend/", "scripts/", "deploy/scripts/", ".cursor/hooks/")
    )


def is_migration_class(path: str) -> bool:
    if path in {"schema.sql", "backend/scripts_support/check_migration.py"}:
        return True
    if path.startswith("backend/migrations/"):
        return True
    return path.startswith("backend/tests/test_migration_")


def path_looks_inflight(path: str) -> bool:
    lowered = path.lower()
    return any(marker in lowered for marker in INFLIGHT_PATH_MARKERS)


def diff_mentions_inflight(diff_text: str) -> bool:
    lowered = diff_text.lower()
    if "sms_chunk" in lowered:
        return True
    return any(token in lowered for token in INFLIGHT_SQL_TOKENS)


def is_inflight_recovery_class(
    path: str,
    *,
    root: Path,
    diff_text: str,
    recovery_tests: frozenset[str],
) -> bool:
    if path == "scripts/verify_vendor_postgres_recovery.sh":
        return True
    if path_looks_inflight(path):
        return True
    if path in recovery_tests:
        return True
    if path == "schema.sql" or path.startswith("backend/migrations/"):
        return bool(diff_text) and diff_mentions_inflight(diff_text)
    return False


def contract_tests_for(path: str) -> list[str]:
    matches: list[str] = []
    for prefix, tests in CI_CONTRACT_MAP:
        if path == prefix or (prefix.endswith("/") and path.startswith(prefix)):
            for test in tests:
                if test not in matches:
                    matches.append(test)
    return matches


def plan_for_paths(
    paths: Sequence[str],
    *,
    root: Path,
    diffs: Mapping[str, str] | None = None,
) -> GatePlan:
    """根据路径并集计算必须跑的检查；未点名的检查不会出现。"""

    plan = GatePlan()
    recovery_tests = vendor_recovery_test_paths(root)
    diff_map = diffs or {}
    unique_paths = [path for path in dict.fromkeys(paths) if path]
    for path in unique_paths:
        if is_ordinary_doc(path):
            continue
        if is_spec_doc(path):
            plan.add(CHECK_SPEC, path)
        if path.startswith("frontend/"):
            plan.add(CHECK_FRONTEND, path)
        if is_python_gate_file(path):
            plan.add(CHECK_RUFF, path)
            if path not in plan.ruff_files:
                plan.ruff_files.append(path)
        if (
            path.startswith("backend/tests/")
            and path.endswith(".py")
            and path not in recovery_tests
            and not path_looks_inflight(path)
        ):
            plan.add(CHECK_PYTEST_CHANGED, path)
            rel = path.removeprefix("backend/")
            if rel not in plan.pytest_files:
                plan.pytest_files.append(rel)
        if is_migration_class(path):
            plan.add(CHECK_MIGRATION, path)
        if is_inflight_recovery_class(
            path,
            root=root,
            diff_text=diff_map.get(path, ""),
            recovery_tests=recovery_tests,
        ):
            plan.add(CHECK_VENDOR_PG, path)
        for test in contract_tests_for(path):
            plan.add(CHECK_CI_CONTRACTS, path)
            if test not in plan.contract_tests:
                plan.contract_tests.append(test)
    return plan


def parse_git_invocation(command: str) -> GitInvocation | None:
    """解析 shell 命令中的 git commit/push；解析失败则失败关闭。"""

    try:
        tokens = shlex.split(command, posix=True)
    except ValueError as exc:
        raise GateError(f"cannot parse shell command: {exc}") from exc
    invocations: list[GitInvocation] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if (
            (token.startswith("HUSKY=") or token in {"HUSKY=0", "GIT_HOOKS=0"})
            and ("commit" in tokens or "push" in tokens)
        ):
            raise GateError("hook skip environment is denied")
        if token == "git" or token.endswith("/git"):
            index += 1
            skips = False
            while index < len(tokens) and tokens[index] not in COMMAND_SEPARATORS:
                option = tokens[index]
                if option in GIT_VALUE_OPTIONS:
                    if index + 1 >= len(tokens):
                        raise GateError("git global option is missing a value")
                    value = tokens[index + 1]
                    if option == "-c" and _hooks_path_disabled(value):
                        skips = True
                    index += 2
                    continue
                if option.startswith("--git-dir=") or option.startswith("--work-tree="):
                    index += 1
                    continue
                if option.startswith("-"):
                    index += 1
                    continue
                subcommand = option
                rest: list[str] = []
                index += 1
                while index < len(tokens) and tokens[index] not in COMMAND_SEPARATORS:
                    rest.append(tokens[index])
                    index += 1
                if subcommand in {"commit", "push"}:
                    skip_here = skips or _subcommand_skips_hooks(subcommand, rest)
                    invocations.append(
                        GitInvocation(subcommand, tuple(rest), skip_here)
                    )
                break
            continue
        index += 1
    if not invocations:
        return None
    if any(item.skips_hooks for item in invocations):
        return GitInvocation(invocations[0].subcommand, invocations[0].args, True)
    return invocations[0]


def _hooks_path_disabled(value: str) -> bool:
    key, _, assigned = value.partition("=")
    if key.lower() != "core.hookspath":
        return False
    return assigned in {"", os.devnull, "/dev/null"}


def _subcommand_skips_hooks(subcommand: str, args: Sequence[str]) -> bool:
    for arg in args:
        if arg == "--":
            break
        if arg == "--no-verify":
            return True
        if subcommand == "commit" and arg == "-n":
            return True
        if (
            subcommand == "commit"
            and arg.startswith("-")
            and not arg.startswith("--")
            and "n" in arg[1:]
        ):
            return True
    return False


def collect_paths(root: Path, mode: Mode, invocation: GitInvocation | None) -> list[str]:
    names = git_z_names(
        root, ["diff", "--cached", "--name-only", "--no-renames", "-z"]
    )
    if mode == "commit" and invocation is not None and _commit_all(invocation.args):
        names.extend(
            git_z_names(root, ["diff", "--name-only", "--no-renames", "-z"])
        )
    if mode == "push":
        verify = subprocess.run(
            ["git", "rev-parse", "--verify", "origin/main^{commit}"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
        if verify.returncode != 0:
            raise GateError("origin/main is required to classify a push")
        names.extend(
            git_z_names(
                root,
                ["diff", "--name-only", "--no-renames", "-z", "origin/main...HEAD"],
            )
        )
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def _commit_all(args: Sequence[str]) -> bool:
    for arg in args:
        if arg in {"-a", "--all"}:
            return True
        if arg.startswith("-") and not arg.startswith("--") and "a" in arg[1:]:
            return True
    return False


def collect_diffs(root: Path, paths: Sequence[str], mode: Mode) -> dict[str, str]:
    needed = [
        path
        for path in paths
        if path == "schema.sql" or path.startswith("backend/migrations/")
    ]
    diffs: dict[str, str] = {}
    for path in needed:
        chunks: list[str] = []
        if mode == "push":
            chunks.append(
                git_output(root, ["diff", "-U0", "origin/main...HEAD", "--", path])
            )
        chunks.append(git_output(root, ["diff", "--cached", "-U0", "--", path]))
        diffs[path] = "\n".join(chunk for chunk in chunks if chunk)
    return diffs


def main_worktree_root(root: Path) -> Path | None:
    result = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    common = Path(result.stdout.strip())
    if common.name == ".git":
        return common.parent
    return None


def resolve_backend_tools(root: Path) -> tuple[list[str], list[str], list[str], Path | None]:
    """定位 ruff/python/pytest；不得创建 worktree .venv。"""

    candidates: list[Path] = [root / "backend" / ".venv" / "bin"]
    shared = main_worktree_root(root)
    if shared is not None:
        candidates.append(shared / "backend" / ".venv" / "bin")
    for bin_dir in candidates:
        ruff = bin_dir / "ruff"
        python = bin_dir / "python"
        if ruff.is_file() and python.is_file():
            return (
                [str(ruff)],
                [str(python)],
                [str(python), "-m", "pytest"],
                bin_dir,
            )
    if shutil.which("uv"):
        uv = ["uv", "run", "--project", str(root / "backend")]
        return uv + ["ruff"], uv + ["python"], uv + ["pytest"], None
    raise GateError(
        "neither a repo backend .venv nor uv is available; refuse to create .venv"
    )


def stamp_path(root: Path) -> Path:
    git_dir = git_output(root, ["rev-parse", "--absolute-git-dir"]).strip()
    return Path(git_dir) / "sms-pre-vcs-gates.ok"


def fingerprint(root: Path, mode: Mode, plan: GatePlan, paths: Sequence[str]) -> str:
    head = git_output(root, ["rev-parse", "HEAD"]).strip()
    index = git_output(root, ["write-tree"]).strip()
    payload = {
        "head": head,
        "index": index,
        "mode": mode,
        "paths": list(paths),
        "checks": plan.required(),
        "ruff": plan.ruff_files,
        "pytest": plan.pytest_files,
        "contracts": plan.contract_tests,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def stamp_matches(root: Path, digest: str) -> bool:
    path = stamp_path(root)
    if not path.is_file():
        return False
    try:
        recorded = path.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return recorded == digest


def write_stamp(root: Path, digest: str) -> None:
    path = stamp_path(root)
    path.write_text(digest + "\n", encoding="utf-8")
    path.chmod(0o600)


def isolated_check_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """去掉 git hook 注入的 GIT_*，避免 pytest 里的 git 操作打到当前仓库。"""

    env = dict(base or os.environ)
    for key in GIT_HOOK_ENV_KEYS:
        env.pop(key, None)
    return env


def run_command(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
) -> None:
    print("+ " + " ".join(argv), file=sys.stderr)
    result = subprocess.run(
        argv,
        cwd=cwd,
        env=isolated_check_env(env),
        check=False,
    )
    if result.returncode != 0:
        raise GateError(f"command failed ({result.returncode}): {' '.join(argv)}")


def require_docker() -> None:
    if shutil.which("docker") is None:
        raise GateError("docker is required for this change-set but is not on PATH")
    probe = subprocess.run(
        ["docker", "info"],
        check=False,
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        raise GateError("docker is required but the daemon is unavailable")


def execute_plan(root: Path, plan: GatePlan) -> None:
    """只执行 plan 点名的检查；缺依赖时对已点名检查失败关闭。"""

    print(plan.explain(), file=sys.stderr)
    if not plan.checks:
        return
    tools = None
    if any(
        name in plan.checks
        for name in (CHECK_RUFF, CHECK_PYTEST_CHANGED, CHECK_MIGRATION, CHECK_CI_CONTRACTS)
    ):
        tools = resolve_backend_tools(root)
    ruff_cmd, python_cmd, pytest_cmd, venv_bin = tools or ([], [], [], None)
    env = isolated_check_env()
    if venv_bin is not None:
        env["PATH"] = str(venv_bin) + os.pathsep + env.get("PATH", "")
    test_env = dict(env)
    test_env.update(
        {
            "ENVIRONMENT": "test",
            "DEBUG": "1",
            "AUTH_MOCK": "1",
            "VENDOR_MOCK": "1",
        }
    )

    if CHECK_SPEC in plan.checks:
        run_command(
            [sys.executable, str(root / "scripts" / "check_spec_consistency.py")],
            cwd=root,
            env=env,
        )
    if CHECK_RUFF in plan.checks:
        existing = [path for path in plan.ruff_files if (root / path).is_file()]
        if existing:
            run_command(
                [
                    *ruff_cmd,
                    "check",
                    "--config",
                    str(root / "backend" / "pyproject.toml"),
                    *existing,
                ],
                cwd=root,
                env=env,
            )
    if CHECK_PYTEST_CHANGED in plan.checks and plan.pytest_files:
        contract_set = set(plan.contract_tests)
        existing = [
            path
            for path in plan.pytest_files
            if (root / "backend" / path).is_file() and path not in contract_set
        ]
        if existing:
            run_command([*pytest_cmd, "-q", *existing], cwd=root / "backend", env=test_env)
    if CHECK_FRONTEND in plan.checks:
        if shutil.which("npm") is None:
            raise GateError("npm is required for frontend changes but is not on PATH")
        frontend = root / "frontend"
        for script in ("lint", "format:check", "typecheck", "test"):
            run_command(["npm", "run", script], cwd=frontend, env=env)
    if CHECK_CI_CONTRACTS in plan.checks:
        if not plan.contract_tests:
            raise GateError("CI/gate paths changed but no contract tests were selected")
        existing = [
            path for path in plan.contract_tests if (root / "backend" / path).is_file()
        ]
        if len(existing) != len(plan.contract_tests):
            missing = sorted(set(plan.contract_tests) - set(existing))
            raise GateError("missing contract tests: " + ", ".join(missing))
        run_command([*pytest_cmd, "-q", *existing], cwd=root / "backend", env=test_env)
    if CHECK_MIGRATION in plan.checks:
        require_docker()
        prepare = root / "scripts" / "local_test.sh"
        if prepare.is_file():
            run_command(["bash", str(prepare), "prepare"], cwd=root, env=env)
        run_command(
            [*python_cmd, "scripts_support/check_migration.py"],
            cwd=root / "backend",
            env=test_env,
        )
    if CHECK_VENDOR_PG in plan.checks:
        require_docker()
        run_command(
            ["bash", str(root / "scripts" / "verify_vendor_postgres_recovery.sh")],
            cwd=root,
            env=env,
        )


def run_gates(root: Path, mode: Mode, invocation: GitInvocation | None) -> GatePlan:
    paths = collect_paths(root, mode, invocation)
    diffs = collect_diffs(root, paths, mode)
    plan = plan_for_paths(paths, root=root, diffs=diffs)
    digest = fingerprint(root, mode, plan, paths)
    if stamp_matches(root, digest):
        print(plan.explain(), file=sys.stderr)
        print("pre-vcs-gates: reused successful stamp for this tree", file=sys.stderr)
        return plan
    execute_plan(root, plan)
    write_stamp(root, digest)
    return plan


def cursor_response(
    permission: Literal["allow", "deny"],
    *,
    user_message: str | None = None,
    agent_message: str | None = None,
) -> dict[str, str]:
    payload: dict[str, str] = {"permission": permission}
    if user_message:
        payload["user_message"] = user_message
    if agent_message:
        payload["agent_message"] = agent_message
    return payload


def decide_cursor_command(
    command: str,
    *,
    root: Path,
    runner: Any | None = None,
) -> dict[str, str]:
    """Cursor 只拦绕过 hook；真正的路径门禁由 git pre-commit/pre-push 执行。"""
    invocation = parse_git_invocation(command)
    if invocation is None:
        return cursor_response("allow")
    if invocation.skips_hooks:
        message = (
            "git commit/push may not skip hooks "
            "(--no-verify, -n on commit, or core.hooksPath=/dev/null)."
        )
        return cursor_response(
            "deny",
            user_message=message,
            agent_message=message,
        )
    _ = runner  # Cursor is not the gate runner; .githooks pre-commit/pre-push are.
    try:
        require_git_hooks(root)
    except GateError as exc:
        message = str(exc)
        return cursor_response(
            "deny",
            user_message=message,
            agent_message=message,
        )
    return cursor_response("allow")


def emit_cursor(payload: Mapping[str, str]) -> int:
    json.dump(dict(payload), sys.stdout, ensure_ascii=True)
    sys.stdout.write("\n")
    return 0


def cursor_hook_main() -> int:
    try:
        raw = sys.stdin.read()
        try:
            request = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise GateError(f"invalid hook stdin JSON: {exc}") from exc
        if not isinstance(request, dict):
            raise GateError("hook stdin must be a JSON object")
        command = str(request.get("command") or "")
        cwd = request.get("cwd")
        root = repo_root_from_cwd(str(cwd) if cwd else None)
        return emit_cursor(decide_cursor_command(command, root=root))
    except GateError as exc:
        message = str(exc)
        return emit_cursor(
            cursor_response("deny", user_message=message, agent_message=message)
        )
    except Exception as exc:  # noqa: BLE001 — hook must fail closed with JSON
        message = f"pre-vcs-gates crashed: {exc}"
        return emit_cursor(
            cursor_response("deny", user_message=message, agent_message=message)
        )


def git_hook_main(mode: Mode) -> int:
    try:
        root = repo_root_from_cwd()
        run_gates(root, mode, None)
        return 0
    except GateError as exc:
        print(f"pre-vcs-gates: {exc}", file=sys.stderr)
        return 1


def require_hooks_path_main() -> int:
    try:
        require_git_hooks(repo_root_from_cwd())
        return 0
    except GateError as exc:
        print(f"pre-vcs-gates: {exc}", file=sys.stderr)
        return 1


def plan_main(paths: Iterable[str], root: Path) -> int:
    plan = plan_for_paths(list(paths), root=root, diffs={})
    print(plan.explain())
    json.dump(
        {
            "checks": plan.required(),
            "ruff_files": plan.ruff_files,
            "pytest_files": plan.pytest_files,
            "contract_tests": plan.contract_tests,
            "triggers": plan.checks,
        },
        sys.stdout,
    )
    sys.stdout.write("\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cursor-hook", action="store_true")
    parser.add_argument("--git-hook", choices=("commit", "push"))
    parser.add_argument("--require-hooks-path", action="store_true")
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--paths", nargs="*")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cursor_hook:
        return cursor_hook_main()
    if args.git_hook:
        return git_hook_main(args.git_hook)
    if args.require_hooks_path:
        return require_hooks_path_main()
    if args.plan:
        root = repo_root_from_cwd()
        return plan_main(args.paths or [], root)
    parser.error("choose --cursor-hook, --git-hook, --require-hooks-path, or --plan")
    return 2


if __name__ == "__main__":
    sys.exit(main())
