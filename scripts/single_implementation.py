"""单点实现与 beat 追踪规则的纯函数判定，供 check_invariants 调用和负向测试复用。"""

from __future__ import annotations

import ast
import re

from call_order import dotted_name

MULTI_SEGMENT_CHARS = 67
_SUCCESS_RATE_SQL = re.compile(r"(?is)\bdelivered\b[^;]{0,120}?/\s*NULLIF\s*\(")
_PLATFORM_PLACEHOLDER = re.compile(r"\\\{\(\[1-9\]|\\\{\(\\d")


def billing_literal_lines(source: str) -> list[int]:
    """返回出现多条计费字数常量 67 的行；该口径只允许写在 services/billing.py。"""

    return sorted(
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant)
        and type(node.value) is int
        and node.value == MULTI_SEGMENT_CHARS
    )


def success_rate_lines(source: str) -> list[int]:
    """返回自行拼接 delivered/(delivered+failed) 成功率的行；口径唯一在 services/stats.py。"""

    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _SUCCESS_RATE_SQL.search(node.value):
                lines.append(node.lineno)
        elif (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Div)
            and "delivered" in dotted_name(node.left)
        ):
            lines.append(node.lineno)
    return sorted(lines)


def template_placeholder_lines(source: str) -> list[int]:
    """返回自行匹配平台占位 {n} 的正则行；模板渲染只在 services/template.py 实现。"""

    return sorted(
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and _PLATFORM_PLACEHOLDER.search(node.value)
    )


def scheduled_task_names(scheduler_source: str) -> set[str]:
    """读取 beat 启动调度表中声明的全部 Celery 任务名。"""

    names: set[str] = set()
    for node in ast.walk(ast.parse(scheduler_source)):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            if (
                isinstance(key, ast.Constant)
                and key.value == "task"
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                names.add(value.value)
    return names


def celery_task_tracking(source: str) -> dict[str, tuple[int, bool]]:
    """返回模块内显式命名的 Celery 任务：任务名 -> (行号, 是否带 @tracked_job)。"""

    tasks: dict[str, tuple[int, bool]] = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        task_name: str | None = None
        tracked = False
        for decorator in node.decorator_list:
            name = dotted_name(decorator)
            if name == "tracked_job" or name.endswith(".tracked_job"):
                tracked = True
            if isinstance(decorator, ast.Call) and name.endswith(".task"):
                for keyword in decorator.keywords:
                    if (
                        keyword.arg == "name"
                        and isinstance(keyword.value, ast.Constant)
                        and isinstance(keyword.value.value, str)
                    ):
                        task_name = keyword.value.value
        if task_name is not None:
            tasks[task_name] = (node.lineno, tracked)
    return tasks


def untracked_scheduled_tasks(
    scheduled: set[str], tasks: dict[str, tuple[int, bool]]
) -> tuple[list[str], list[str]]:
    """返回 (调度表中找不到定义的任务, 已定义但缺少 @tracked_job 的任务)。"""

    missing = sorted(scheduled - tasks.keys())
    untracked = sorted(name for name in scheduled & tasks.keys() if not tasks[name][1])
    return missing, untracked
