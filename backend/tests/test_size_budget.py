from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from gate_policy import SIZE_BUDGET_EXCEPTIONS  # noqa: E402
from governance_metrics import compute_metrics  # noqa: E402
from size_budget import (  # noqa: E402
    collect_sizes,
    in_budget_scope,
    size_budget_violations,
)


def test_new_files_over_default_limit_are_rejected() -> None:
    sizes = {
        "backend/app/services/new.py": 801,
        "frontend/src/views/NewView.vue": 601,
        "frontend/src/styles/ok.css": 800,
    }

    assert [path for path, _ in size_budget_violations(sizes, {})] == [
        "backend/app/services/new.py",
        "frontend/src/views/NewView.vue",
    ]


def test_registered_files_may_not_grow_past_limit() -> None:
    exceptions = {"backend/app/tasks/big.py": (1000, "存量")}

    grown = size_budget_violations({"backend/app/tasks/big.py": 1001}, exceptions)

    assert size_budget_violations({"backend/app/tasks/big.py": 1000}, exceptions) == []
    assert [path for path, _ in grown] == ["backend/app/tasks/big.py"]


def test_stale_or_unexplained_exceptions_are_rejected() -> None:
    exceptions = {
        "backend/app/a.py": (1000, "存量"),
        "backend/app/gone.py": (1000, "存量"),
        "frontend/src/views/Big.vue": (700, " "),
    }
    sizes = {"backend/app/a.py": 790, "frontend/src/views/Big.vue": 650}

    messages = dict(size_budget_violations(sizes, exceptions))

    assert "删除例外条目" in messages["backend/app/a.py"]
    assert "不存在" in messages["backend/app/gone.py"]
    assert "理由" in messages["frontend/src/views/Big.vue"]


def test_scope_excludes_generated_tests_and_deploy_tooling() -> None:
    assert in_budget_scope("backend/app/settings.py")
    assert in_budget_scope("frontend/src/views/SendView.vue")
    assert not in_budget_scope("frontend/src/api/types.gen.ts")
    assert not in_budget_scope("backend/tests/test_settings.py")
    assert not in_budget_scope("deploy/scripts/release_manager.py")


def test_repository_respects_size_budget() -> None:
    paths = subprocess.run(
        ["git", "ls-files", "backend/app", "frontend/src"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()

    assert size_budget_violations(collect_sizes(ROOT, paths), SIZE_BUDGET_EXCEPTIONS) == []


def test_governance_metrics_follow_d122_definitions() -> None:
    files = {
        "backend/app/a.py": "x\n" * 801 + "except Exception:\n",
        "backend/app/b.py": "try:\n    pass\nexcept Exception as exc:\n",
        "backend/app/c.py": "from typing import Any\n\ndef f(x: Any) -> dict[str, Any]: ...\n",
        "backend/tests/test_a.py": "x\n",
        "frontend/src/views/A.vue": "x\n" * 601,
        "frontend/src/api/types.gen.ts": "x\n" * 5,
        "deploy/sms-compose": "#!/bin/sh\nexit 0\n",
        "deploy/README.md": "doc\n",
        "backend/migrations/versions/0001_init.py": "x\n",
        "AGENTS.md": "规则\n",
    }

    metrics = compute_metrics(files, ["fix(api): a", "feat: b", "Fix: c"])

    assert metrics["app_modules"] == 3
    assert metrics["app_files_over_800"] == 1
    assert metrics["app_except_exception"] == 2
    assert metrics["app_explicit_any"] == 2
    assert metrics["frontend_lines"] == 606
    assert metrics["frontend_vue_over_600"] == 1
    assert metrics["ops_lines"] == 2
    assert metrics["migrations"] == 1
    assert metrics["recent_fix_commits"] == 2
    assert metrics["agents_bytes"] == len("规则\n".encode())
