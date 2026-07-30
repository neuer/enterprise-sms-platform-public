from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from g2_timing import STAGE_NAMES, append_record, render_summary  # noqa: E402


def _write_success_records(path: Path, stages: range = range(11)) -> None:
    for stage in stages:
        append_record(path, stage, STAGE_NAMES[stage], "success", (stage + 1) * 1000)


def test_success_summary_requires_all_eleven_ordered_stages(tmp_path: Path) -> None:
    timing = tmp_path / "g2.jsonl"
    summary = tmp_path / "summary.md"
    _write_success_records(timing)

    assert render_summary(timing, "success", summary) == 0

    markdown = summary.read_text(encoding="utf-8")
    assert "## G2 阶段耗时" in markdown
    assert "| 0 | 规格一致性与安全规则 | success | 1.000s |" in markdown
    assert "| 10 | 发布控制恢复烟测 | success | 11.000s |" in markdown
    assert "| 合计 |  | success | 66.000s |" in markdown


@pytest.mark.parametrize("case", ("missing", "duplicate", "out-of-order", "invalid-json"))
def test_success_summary_fails_closed_on_invalid_timing(tmp_path: Path, case: str) -> None:
    timing = tmp_path / "g2.jsonl"
    summary = tmp_path / "summary.md"
    if case == "missing":
        _write_success_records(timing, range(10))
    elif case == "duplicate":
        _write_success_records(timing)
        append_record(timing, 10, STAGE_NAMES[10], "success", 1)
    elif case == "out-of-order":
        append_record(timing, 1, STAGE_NAMES[1], "success", 1)
        append_record(timing, 0, STAGE_NAMES[0], "success", 1)
        _write_success_records(timing, range(2, 11))
    else:
        timing.write_text("{not-json}\n", encoding="utf-8")

    assert render_summary(timing, "success", summary) == 1
    assert "G2 计时证据无效" in summary.read_text(encoding="utf-8")


def test_failed_gate_renders_partial_records_without_masking_failure(tmp_path: Path) -> None:
    timing = tmp_path / "g2.jsonl"
    summary = tmp_path / "summary.md"
    append_record(timing, 0, STAGE_NAMES[0], "success", 12)
    append_record(timing, 1, STAGE_NAMES[1], "failure", 34)

    assert render_summary(timing, "failure", summary) == 0

    markdown = summary.read_text(encoding="utf-8")
    assert "| 0 | 规格一致性与安全规则 | success | 0.012s |" in markdown
    assert "| 1 | 后端静态检查 | failure | 0.034s |" in markdown
    assert "部分计时" in markdown


@pytest.mark.parametrize("content", (None, "{not-json}\n"))
def test_failed_gate_tolerates_unavailable_timing(
    tmp_path: Path, content: str | None
) -> None:
    timing = tmp_path / "g2.jsonl"
    summary = tmp_path / "summary.md"
    if content is not None:
        timing.write_text(content, encoding="utf-8")

    assert render_summary(timing, "failure", summary) == 0
    assert "计时不可用" in summary.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("stage", "name", "status", "duration_ms"),
    (
        (11, "未知", "success", 1),
        (0, "名称漂移", "success", 1),
        (0, STAGE_NAMES[0], "pending", 1),
        (0, STAGE_NAMES[0], "success", -1),
    ),
)
def test_append_record_rejects_invalid_safe_fields(
    tmp_path: Path,
    stage: int,
    name: str,
    status: str,
    duration_ms: int,
) -> None:
    with pytest.raises(ValueError):
        append_record(tmp_path / "g2.jsonl", stage, name, status, duration_ms)


def test_record_contains_only_fixed_safe_fields(tmp_path: Path) -> None:
    timing = tmp_path / "g2.jsonl"
    append_record(timing, 0, STAGE_NAMES[0], "success", 123)

    record = json.loads(timing.read_text(encoding="utf-8"))
    assert record == {
        "duration_ms": 123,
        "name": STAGE_NAMES[0],
        "stage": 0,
        "status": "success",
    }


def test_cli_records_and_renders_summary(tmp_path: Path) -> None:
    timing = tmp_path / "g2.jsonl"
    summary = tmp_path / "summary.md"
    for stage, name in enumerate(STAGE_NAMES):
        recorded = subprocess.run(
            (
                sys.executable,
                str(SCRIPTS / "g2_timing.py"),
                "record",
                "--file",
                str(timing),
                "--stage",
                str(stage),
                "--name",
                name,
                "--status",
                "success",
                "--duration-ms",
                "1",
            ),
            capture_output=True,
            text=True,
            check=False,
        )
        assert recorded.returncode == 0, recorded.stderr

    rendered = subprocess.run(
        (
            sys.executable,
            str(SCRIPTS / "g2_timing.py"),
            "render",
            "--file",
            str(timing),
            "--gate-outcome",
            "success",
            "--summary-file",
            str(summary),
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert rendered.returncode == 0, rendered.stderr
    assert "发布控制恢复烟测" in summary.read_text(encoding="utf-8")
