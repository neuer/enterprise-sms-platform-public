#!/usr/bin/env python3
"""记录并渲染不含业务数据的 G2 阶段耗时。"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, cast

STAGE_NAMES = (
    "规格一致性与安全规则",
    "后端静态检查",
    "单元与集成测试",
    "迁移一致性",
    "完整契约一致性",
    "干净整栈拉起",
    "运行态安全验收",
    "API 级 E2E",
    "性能冒烟",
    "前端门禁",
    "发布控制恢复烟测",
)
RECORD_FIELDS = frozenset({"stage", "name", "status", "duration_ms"})
StageStatus = Literal["success", "failure"]


@dataclass(frozen=True, slots=True)
class TimingRecord:
    stage: int
    name: str
    status: StageStatus
    duration_ms: int


def _validate_record(
    stage: object,
    name: object,
    status: object,
    duration_ms: object,
) -> TimingRecord:
    if type(stage) is not int or not 0 <= stage < len(STAGE_NAMES):
        raise ValueError("invalid G2 timing stage")
    if name != STAGE_NAMES[stage]:
        raise ValueError("invalid G2 timing stage name")
    if status not in {"success", "failure"}:
        raise ValueError("invalid G2 timing status")
    if type(duration_ms) is not int or duration_ms < 0:
        raise ValueError("invalid G2 timing duration")
    return TimingRecord(
        stage=stage,
        name=name,
        status=cast(StageStatus, status),
        duration_ms=duration_ms,
    )


def append_record(
    path: Path,
    stage: int,
    name: str,
    status: str,
    duration_ms: int,
) -> None:
    """追加一条只含固定阶段元数据的 JSONL 记录。"""

    record = _validate_record(stage, name, status, duration_ms)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(record), ensure_ascii=False, sort_keys=True) + "\n")


def _read_records(path: Path) -> list[TimingRecord]:
    records: list[TimingRecord] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            value = json.loads(line)
            if not isinstance(value, dict) or set(value) != RECORD_FIELDS:
                raise ValueError("invalid G2 timing record shape")
            records.append(
                _validate_record(
                    value["stage"],
                    value["name"],
                    value["status"],
                    value["duration_ms"],
                )
            )
    return records


def _is_valid_prefix(records: list[TimingRecord]) -> bool:
    if not records or [record.stage for record in records] != list(range(len(records))):
        return False
    failure_positions = [
        index for index, record in enumerate(records) if record.status == "failure"
    ]
    return not failure_positions or failure_positions == [len(records) - 1]


def _table(records: list[TimingRecord], *, partial: bool) -> str:
    lines = [
        "## G2 阶段耗时",
        "",
        "| 阶段 | 名称 | 状态 | 耗时 |",
        "|---:|---|---|---:|",
    ]
    for record in records:
        lines.append(
            f"| {record.stage} | {record.name} | {record.status} | "
            f"{record.duration_ms / 1000:.3f}s |"
        )
    total_ms = sum(record.duration_ms for record in records)
    total_status = "failure" if any(record.status == "failure" for record in records) else "success"
    lines.append(f"| 合计 |  | {total_status} | {total_ms / 1000:.3f}s |")
    if partial:
        lines.extend(("", "> G2 未成功完成；上表为截至终止点的部分计时。"))
    return "\n".join(lines) + "\n"


def _append_summary(path: Path, content: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(content)


def render_summary(timing_file: Path, gate_outcome: str, summary_file: Path) -> int:
    """渲染 Actions Summary；成功门禁的计时证据缺失时失败关闭。"""

    try:
        records = _read_records(timing_file)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        if gate_outcome == "success":
            _append_summary(
                summary_file,
                "## G2 阶段耗时\n\n> G2 计时证据无效，门禁按失败关闭处理。\n",
            )
            return 1
        _append_summary(summary_file, "## G2 阶段耗时\n\n> G2 计时不可用；原始门禁结果保持失败。\n")
        return 0

    if gate_outcome == "success":
        complete = len(records) == len(STAGE_NAMES) and _is_valid_prefix(records)
        if not complete or any(record.status != "success" for record in records):
            _append_summary(
                summary_file,
                "## G2 阶段耗时\n\n> G2 计时证据无效，门禁按失败关闭处理。\n",
            )
            return 1
        _append_summary(summary_file, _table(records, partial=False))
        return 0

    if not _is_valid_prefix(records):
        _append_summary(summary_file, "## G2 阶段耗时\n\n> G2 计时不可用；原始门禁结果保持失败。\n")
        return 0
    _append_summary(summary_file, _table(records, partial=True))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    record = subparsers.add_parser("record")
    record.add_argument("--file", type=Path, required=True)
    record.add_argument("--stage", type=int, required=True)
    record.add_argument("--name", required=True)
    record.add_argument("--status", required=True)
    record.add_argument("--duration-ms", type=int, required=True)

    render = subparsers.add_parser("render")
    render.add_argument("--file", type=Path, required=True)
    render.add_argument("--gate-outcome", required=True)
    render.add_argument("--summary-file", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "record":
            append_record(args.file, args.stage, args.name, args.status, args.duration_ms)
            return 0
        return render_summary(args.file, args.gate_outcome, args.summary_file)
    except (OSError, UnicodeError, ValueError):
        print("G2 timing operation failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
