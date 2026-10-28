from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from any_budget import (  # noqa: E402
    any_budget_violations,
    collect_any_counts,
    count_explicit_any,
    in_any_scope,
)
from gate_policy import EXPLICIT_ANY_BUDGET  # noqa: E402


def test_counts_aliases_qualified_and_string_annotations_but_not_imports() -> None:
    source = (
        "import typing as t\n"
        "from typing import Any, Any as Loose\n"
        "def f(a: Any, b: 'dict[str, Any]') -> t.Any:\n"
        "    value: Loose = a\n"
        "    return typing.Any\n"
    )

    assert count_explicit_any(source) == 5
    assert count_explicit_any("from typing import Any\nx: object = 1\n") == 0


def test_new_files_may_not_use_any() -> None:
    counts = {"backend/app/services/new.py": 1, "backend/app/services/clean.py": 0}

    assert [path for path, _ in any_budget_violations(counts, {})] == [
        "backend/app/services/new.py"
    ]


def test_registered_counts_must_match_exactly() -> None:
    budget = {"backend/app/a.py": 3}

    assert any_budget_violations({"backend/app/a.py": 3}, budget) == []
    grown = dict(any_budget_violations({"backend/app/a.py": 4}, budget))
    shrunk = dict(any_budget_violations({"backend/app/a.py": 2}, budget))
    assert "超过登记值" in grown["backend/app/a.py"]
    assert "下调" in shrunk["backend/app/a.py"]


def test_cleared_or_missing_entries_must_be_removed() -> None:
    budget = {"backend/app/a.py": 2, "backend/app/gone.py": 1}

    messages = dict(any_budget_violations({"backend/app/a.py": 0}, budget))

    assert "删除登记条目" in messages["backend/app/a.py"]
    assert "不存在" in messages["backend/app/gone.py"]


def test_scope_is_application_python_only() -> None:
    assert in_any_scope("backend/app/services/send_inflight.py")
    assert not in_any_scope("backend/tests/test_any_budget.py")
    assert not in_any_scope("backend/migrations/versions/0001_init.py")
    assert not in_any_scope("scripts/any_budget.py")


def test_repository_respects_any_budget() -> None:
    paths = subprocess.run(
        ["git", "ls-files", "backend/app"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()

    assert any_budget_violations(collect_any_counts(ROOT, paths), EXPLICIT_ANY_BUDGET) == []
