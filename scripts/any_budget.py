"""backend/app 显式 Any 棘轮：新文件不得使用，存量文件登记值只许下调。"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping
from pathlib import Path

ANY_ROOT = "backend/app/"
TYPING_MODULES = frozenset({"typing", "typing_extensions"})


def in_any_scope(path: str) -> bool:
    return path.startswith(ANY_ROOT) and path.endswith(".py")


def _annotation_strings(tree: ast.AST) -> Iterable[str]:
    """前向引用字符串注解按表达式重新解析，避免用引号绕过计数。"""

    for node in ast.walk(tree):
        annotations: list[ast.expr | None] = []
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            arguments = node.args
            annotations.append(node.returns)
            annotations.extend(
                arg.annotation
                for arg in (
                    *arguments.posonlyargs,
                    *arguments.args,
                    *arguments.kwonlyargs,
                    arguments.vararg,
                    arguments.kwarg,
                )
                if arg is not None
            )
        elif isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
        for annotation in annotations:
            if annotation is None:
                continue
            for item in ast.walk(annotation):
                if isinstance(item, ast.Constant) and isinstance(item.value, str):
                    yield item.value


def count_explicit_any(source: str) -> int:
    """统计 Any 的引用次数（含 typing.Any、导入别名与字符串注解），不计 import 语句本身。"""

    tree = ast.parse(source)
    names = {"Any"}
    modules = set(TYPING_MODULES)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in TYPING_MODULES:
            names.update(
                alias.asname for alias in node.names if alias.name == "Any" and alias.asname
            )
        elif isinstance(node, ast.Import):
            modules.update(
                alias.asname
                for alias in node.names
                if alias.name in TYPING_MODULES and alias.asname
            )

    def count(root: ast.AST) -> int:
        return sum(
            (isinstance(item, ast.Name) and item.id in names)
            or (
                isinstance(item, ast.Attribute)
                and item.attr == "Any"
                and isinstance(item.value, ast.Name)
                and item.value.id in modules
            )
            for item in ast.walk(root)
        )

    total = count(tree)
    for text in _annotation_strings(tree):
        try:
            total += count(ast.parse(text, mode="eval"))
        except SyntaxError:
            continue
    return total


def collect_any_counts(root: Path, paths: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in paths:
        if not in_any_scope(path):
            continue
        try:
            counts[path] = count_explicit_any((root / path).read_text(encoding="utf-8"))
        except OSError:
            continue
    return counts


def any_budget_violations(
    counts: Mapping[str, int],
    budget: Mapping[str, int],
) -> list[tuple[str, str]]:
    """返回 (路径, 原因)。登记值必须等于当前计数：增加即失败，减少须同步下调。"""

    violations: list[tuple[str, str]] = []
    for path, limit in sorted(budget.items()):
        current = counts.get(path)
        if current is None:
            violations.append((path, "显式 Any 登记指向的文件不存在，删除该条目"))
        elif current == 0:
            violations.append((path, "显式 Any 已清零，删除登记条目"))
        elif current > limit:
            violations.append(
                (path, f"显式 Any 超过登记值 {limit} 处（{current} 处），改用具体类型")
            )
        elif current < limit:
            violations.append((path, f"显式 Any 已降到 {current} 处，把登记值从 {limit} 下调"))
    for path, current in sorted(counts.items()):
        if path not in budget and current:
            violations.append(
                (
                    path,
                    f"未登记文件不得使用显式 Any（{current} 处）；改用具体类型、Protocol、"
                    "TypedDict，或以 object 接收后收窄",
                )
            )
    return violations
