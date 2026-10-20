from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from single_implementation import (  # noqa: E402
    billing_literal_lines,
    celery_task_tracking,
    scheduled_task_names,
    success_rate_lines,
    template_placeholder_lines,
    untracked_scheduled_tasks,
)


def test_billing_literal_detects_duplicate_segment_formula() -> None:
    source = "import math\n\ndef segments(n):\n    return 1 if n <= 70 else math.ceil(n / 67)\n"

    assert billing_literal_lines(source) == [4]
    assert billing_literal_lines("LIMIT = 670\nflag = True\n") == []


def test_success_rate_detects_python_and_sql_reimplementation() -> None:
    python_source = "def rate(delivered, failed):\n    return delivered / (delivered + failed)\n"
    sql_source = 'SQL = "SELECT delivered::float / NULLIF(delivered + failed, 0) FROM t"\n'

    assert success_rate_lines(python_source) == [2]
    assert success_rate_lines(sql_source) == [1]
    assert success_rate_lines("def ok(total, n):\n    return total / n\n") == []


def test_template_placeholder_regex_outside_template_is_detected() -> None:
    source = 'import re\nPATTERN = re.compile(r"\\{([1-9]\\d*)\\}")\n'

    assert template_placeholder_lines(source) == [2]
    assert template_placeholder_lines('TEXT = "{1} 验证码"\n') == []


def test_scheduled_task_without_tracked_job_is_reported() -> None:
    scheduler = (
        "def build():\n"
        "    return {\n"
        '        "a": {"task": "app.tasks.tracked"},\n'
        '        "b": {"task": "app.tasks.untracked"},\n'
        '        "c": {"task": "app.tasks.absent"},\n'
        "    }\n"
    )
    tasks_source = (
        '@celery_app.task(name="app.tasks.tracked")\n'
        '@tracked_job("tracked", expect_interval_s=60)\n'
        "def tracked():\n    return 1\n\n"
        '@celery_app.task(name="app.tasks.untracked", soft_time_limit=1)\n'
        "def untracked():\n    return 1\n"
    )

    scheduled = scheduled_task_names(scheduler)
    tasks = celery_task_tracking(tasks_source)

    assert scheduled == {"app.tasks.tracked", "app.tasks.untracked", "app.tasks.absent"}
    assert untracked_scheduled_tasks(scheduled, tasks) == (
        ["app.tasks.absent"],
        ["app.tasks.untracked"],
    )


def test_repository_schedule_is_fully_resolved_and_tracked() -> None:
    app = ROOT / "backend" / "app"
    scheduled = scheduled_task_names((app / "tasks" / "scheduler.py").read_text(encoding="utf-8"))
    tasks: dict[str, tuple[int, bool]] = {}
    for path in sorted((app / "tasks").glob("*.py")):
        tasks.update(celery_task_tracking(path.read_text(encoding="utf-8")))

    assert len(scheduled) >= 18
    assert untracked_scheduled_tasks(scheduled, tasks) == ([], [])
