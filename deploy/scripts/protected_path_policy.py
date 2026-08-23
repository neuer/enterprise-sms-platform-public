"""安全域分类的唯一来源：目录默认 FULL，显式降级才回到组件门禁。

CI Job 选择与 CODEOWNERS 共用本模块。不要再把新的安全关键文件名追加到
exact allowlist；新文件落在安全域内即默认 backend-critical 或
frontend-security。只有经过独立审查的低风险文件才能写入
``REVIEWED_ORDINARY_EXACT``。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

SecurityDomainCategory = Literal["backend-critical", "frontend-security"]

# 后端运行时安全域。新文件/重命名默认 backend + G2 + security。
BACKEND_CRITICAL_DOMAINS: tuple[str, ...] = (
    "backend/app/core/",
    "backend/app/api/",
    "backend/app/services/",
    "backend/app/tasks/",
    "backend/app/vendor/",
)

# 前端会话与请求面。新文件默认 frontend + security（既有前端/浏览器门禁）。
FRONTEND_SECURITY_DOMAINS: tuple[str, ...] = (
    "frontend/src/App.vue",
    "frontend/src/stores/",
    "frontend/src/router/",
    "frontend/src/api/",
)

# 已审查、必须继续走 backend-critical（含 G2）的前端路径。
# 域默认是 frontend-security；这里只升不降。当前无条目：
# 既有会话文件走域默认 frontend-security（#454），不再升格到 backend job。
BACKEND_CRITICAL_RAISE_EXACT: frozenset[str] = frozenset()

# 安全域内已审查降级为普通组件门禁的路径。新增条目即显式削弱保护。
REVIEWED_ORDINARY_EXACT: frozenset[str] = frozenset(
    {
        "backend/app/services/dashboard.py",
    }
)

CODEOWNERS_OWNER = "@neuer"
REQUIRED_CODEOWNERS_PATTERNS: tuple[str, ...] = (
    *BACKEND_CRITICAL_DOMAINS,
    *FRONTEND_SECURITY_DOMAINS,
    "backend/migrations/",
    "schema.sql",
    "deploy/",
    ".github/workflows/",
    "deploy/scripts/protected_path_policy.py",
)


def matches_domain(path: str, domains: tuple[str, ...]) -> bool:
    """判断路径是否落在域规则内；目录规则以 ``/`` 结尾，否则为精确文件。"""

    for domain in domains:
        if domain.endswith("/"):
            if path.startswith(domain):
                return True
        elif path == domain:
            return True
    return False


def is_security_domain_path(path: str) -> bool:
    """路径是否属于安全域（含前端升格 exact）。"""

    return (
        matches_domain(path, BACKEND_CRITICAL_DOMAINS)
        or matches_domain(path, FRONTEND_SECURITY_DOMAINS)
        or path in BACKEND_CRITICAL_RAISE_EXACT
    )


def security_domain_category(path: str) -> SecurityDomainCategory | None:
    """返回安全域类别；显式降级或不在域内则返回 None。"""

    if path in REVIEWED_ORDINARY_EXACT:
        return None
    if path in BACKEND_CRITICAL_RAISE_EXACT or matches_domain(
        path, BACKEND_CRITICAL_DOMAINS
    ):
        return "backend-critical"
    if matches_domain(path, FRONTEND_SECURITY_DOMAINS):
        return "frontend-security"
    return None


def unclassified_security_domain_paths(paths: Iterable[str]) -> tuple[str, ...]:
    """安全域内既未受保护也未显式降级的路径，供仓库扫描失败关闭。"""

    missing: list[str] = []
    for path in paths:
        if not is_security_domain_path(path):
            continue
        if path in REVIEWED_ORDINARY_EXACT:
            continue
        if security_domain_category(path) is None:
            missing.append(path)
    return tuple(sorted(missing))
