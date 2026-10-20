"""应用源码文件体积预算：新文件不得超限，存量超限文件只许缩小。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path

DEFAULT_LIMIT = 800
VUE_LIMIT = 600
GROWTH_CEILING = 1000
BUDGET_ROOTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("backend/app/", (".py",)),
    ("frontend/src/", (".vue", ".ts", ".css")),
)
GENERATED_SUFFIXES = (".gen.ts",)


def default_limit(path: str) -> int:
    """页面组件按 600 行，其余应用源码按 800 行。"""

    return VUE_LIMIT if path.endswith(".vue") else DEFAULT_LIMIT


def in_budget_scope(path: str) -> bool:
    """只约束应用源码；测试、迁移、部署工具与生成文件不在范围内。"""

    if path.endswith(GENERATED_SUFFIXES):
        return False
    return any(path.startswith(root) and path.endswith(suffixes) for root, suffixes in BUDGET_ROOTS)


def line_count(text: str) -> int:
    """与 wc -l 一致，按换行符计数。"""

    return text.count("\n")


def collect_sizes(root: Path, paths: Iterable[str]) -> dict[str, int]:
    """读取范围内文件的行数；缺失文件忽略，由登记表检查报告。"""

    sizes: dict[str, int] = {}
    for path in paths:
        if not in_budget_scope(path):
            continue
        try:
            sizes[path] = line_count((root / path).read_text(encoding="utf-8"))
        except OSError:
            continue
    return sizes


def size_budget_violations(
    sizes: Mapping[str, int],
    exceptions: Mapping[str, tuple[int, str]],
) -> list[tuple[str, str]]:
    """返回 (路径, 原因)。登记条目必须有理由、只许下调，文件回到默认上限内即须删除。"""

    violations: list[tuple[str, str]] = []
    for path, (limit, reason) in sorted(exceptions.items()):
        if not reason.strip():
            violations.append((path, "体积预算例外必须写明理由"))
        current = sizes.get(path)
        if current is None:
            violations.append((path, "体积预算例外指向的文件不存在，删除该条目"))
            continue
        if current <= default_limit(path):
            violations.append(
                (path, f"已回到默认上限 {default_limit(path)} 行内（{current} 行），删除例外条目")
            )
        elif current > limit:
            violations.append((path, f"超过登记上限 {limit} 行（{current} 行），先拆分再增加代码"))
    for path, current in sorted(sizes.items()):
        if path in exceptions:
            continue
        if current > default_limit(path):
            violations.append(
                (path, f"超过默认上限 {default_limit(path)} 行（{current} 行），按职责拆分")
            )
    return violations
