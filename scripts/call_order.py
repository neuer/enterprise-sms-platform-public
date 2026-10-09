"""静态调用顺序判定：在拆分后的逻辑模块里比较两类调用的首次可达位置。"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterable

CallPredicate = Callable[[ast.Call], bool]
FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


def dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return dotted_name(node.func)
    return ""


def _calls(node: ast.AST) -> list[ast.Call]:
    found = [item for item in ast.walk(node) if isinstance(item, ast.Call)]
    return sorted(found, key=lambda item: (item.lineno, item.col_offset))


def _callee(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute) and dotted_name(func.value) == "self":
        return func.attr
    return func.id if isinstance(func, ast.Name) else ""


def _functions(sources: Iterable[str]) -> dict[str, FunctionNode]:
    functions: dict[str, FunctionNode] = {}
    for source in sources:
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # Protocol 桩与实现同名时以有调用的实现为准。
            existing = functions.get(node.name)
            if existing is None or not _calls(existing):
                functions[node.name] = node
    return functions


def _reaching(functions: dict[str, FunctionNode], marker: CallPredicate) -> set[str]:
    reached = {name for name, node in functions.items() if any(map(marker, _calls(node)))}
    changed = True
    while changed:
        changed = False
        for name, node in functions.items():
            if name not in reached and any(_callee(c) in reached for c in _calls(node)):
                reached.add(name)
                changed = True
    return reached


def first_reach_precedes(
    sources: Iterable[str],
    *,
    entry: str,
    first: CallPredicate,
    then: CallPredicate,
) -> bool:
    """entry 中首个（直接或经 self.x()/x() 传递）命中 first 的调用须早于命中 then 的调用。

    两者都必须可达；按函数名建图，入口拆成多个阶段方法或模块函数后仍按编排顺序判定。
    """

    functions = _functions(sources)
    root = functions.get(entry)
    if root is None:
        return False

    def first_line(marker: CallPredicate) -> int | None:
        reach = _reaching(functions, marker)
        for call in _calls(root):
            if marker(call) or _callee(call) in reach:
                return call.lineno
        return None

    first_at = first_line(first)
    then_at = first_line(then)
    return first_at is not None and then_at is not None and first_at < then_at
