"""运维 Python 代码单独可见的语句/分支覆盖门禁；保留完整源码分母。"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

from coverage_runtime import source_files

# 以实测完整分母为首轮 ratchet，不改变 app 的既有门槛。
THRESHOLDS: Mapping[str, tuple[int, int]] = MappingProxyType({
    "scripts": (63, 53), "deploy": (77, 65),
    "release": (82, 72), "restore": (76, 60), "secrets": (83, 72),
    "storage_initialize": (46, 35), "storage_preflight": (78, 72),
})
CRITICAL_FILES = {
    "release": "deploy/scripts/release_manager.py",
    "restore": "deploy/scripts/restore_drill.py",
    "secrets": "deploy/scripts/prepare_runtime_secrets.py",
    "storage_initialize": "deploy/scripts/initialize_production_storage.py",
    "storage_preflight": "deploy/scripts/storage_preflight.py",
}


def normalize(
    document: Any, *, root: Path, report_cwd: Path | None = None,
) -> dict[str, Any]:
    """只接受本 checkout 内的完整三个源码目录，禁止 basename/suffix 路径猜测。"""
    if (not isinstance(document, dict) or not isinstance(document.get("files"), dict)
            or not isinstance(document.get("meta"), dict)
            or document["meta"].get("branch_coverage") is not True):
        raise ValueError("complete branch coverage document is missing")
    files = {}
    for name, value in document["files"].items():
        if not isinstance(name, str):
            raise ValueError("coverage path must be text")
        path = Path(name)
        # 数据文件存绝对路径；coverage JSON 会相对报告命令 cwd 输出部分路径。
        # cwd 由调用入口固定，不能从不可信报告中读取或按 basename 猜测。
        absolute = path if path.is_absolute() else (report_cwd or root / "backend") / path
        relative = absolute.resolve().relative_to(root.resolve()).as_posix()
        if relative in files:
            raise ValueError("duplicate normalized source path")
        files[relative] = value
    if not files or set(files) != source_files(root):
        raise ValueError("complete Python source inventory mismatch")
    return {"meta": document["meta"], "files": files}


def app_document(document: dict[str, Any]) -> dict[str, Any]:
    """保持原 app/* 键与原独立阈值消费者不变。"""
    return {"files": {name.removeprefix("backend/"): value
                      for name, value in document["files"].items()
                      if name.startswith("backend/app/")}}


def _counts(value: Any, total_key: str, covered_key: str) -> tuple[int, int]:
    if not isinstance(value, dict) or not isinstance(value.get("summary"), dict):
        raise ValueError("coverage counters are missing")
    total, covered = value["summary"].get(total_key), value["summary"].get(covered_key)
    if type(total) is not int or type(covered) is not int or not 0 <= covered <= total:
        raise ValueError("coverage counters are invalid")
    return total, covered


def evaluate(document: dict[str, Any]) -> dict[str, dict[str, float]]:
    """高风险入口分别设门槛，不能被其他脚本的高覆盖平均掉。"""
    if not set(CRITICAL_FILES.values()) <= document["files"].keys():
        raise ValueError("critical operations source is missing")
    totals: dict[str, dict[str, float]] = {}
    for group, floors in THRESHOLDS.items():
        if group in CRITICAL_FILES:
            selected = [document["files"][CRITICAL_FILES[group]]]
        else:
            prefix = "scripts/" if group == "scripts" else "deploy/scripts/"
            selected = [value for name, value in document["files"].items()
                        if name.startswith(prefix)]
        totals[group] = {}
        for metric, total_key, covered_key, floor in (
            ("statements", "num_statements", "covered_lines", floors[0]),
            ("branches", "num_branches", "covered_branches", floors[1]),
        ):
            counts = [_counts(value, total_key, covered_key) for value in selected]
            total, covered = sum(x[0] for x in counts), sum(x[1] for x in counts)
            if not total or covered * 100 < total * floor:
                raise ValueError(f"operations {group} {metric} below {floor}%")
            totals[group][metric] = covered * 100 / total
    return totals
