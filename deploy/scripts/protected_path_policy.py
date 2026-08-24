"""安全域分类的唯一来源：目录默认 FULL，显式降级才回到组件门禁。

CI Job 选择与 CODEOWNERS 共用本模块。不要再把新的安全关键文件名追加到
exact allowlist；新文件落在安全域内即默认 backend-critical 或
frontend-security。只有经过独立审查的低风险文件才能写入
``REVIEWED_ORDINARY_REASONS``。

T5-05：``backend/app/`` 与 ``frontend/src/views/`` 默认进入安全域。
反向枚举以 ``REQUIRED_TRACKED_SOURCE_TREES`` 为准，不先用域清单过滤，
避免 manifest 漏项被测试跳过。
"""

from __future__ import annotations

from collections.abc import Iterable
from types import MappingProxyType
from typing import Literal

SecurityDomainCategory = Literal["backend-critical", "frontend-security"]

# 后端运行时安全域。backend/app 根模块、models 与域内新文件默认 backend + G2 + security。
BACKEND_CRITICAL_DOMAINS: tuple[str, ...] = ("backend/app/",)

# 前端会话、请求面与敏感视图。views 目录默认 frontend-security；新视图 FAIL CLOSED。
FRONTEND_SECURITY_DOMAINS: tuple[str, ...] = (
    "frontend/src/App.vue",
    "frontend/src/stores/",
    "frontend/src/router/",
    "frontend/src/api/",
    "frontend/src/views/",
)

# 已审查、必须继续走 backend-critical（含 G2）的前端路径。
# 域默认是 frontend-security；这里只升不降。当前无条目：
# 既有会话文件走域默认 frontend-security（#454），不再升格到 backend job。
BACKEND_CRITICAL_RAISE_EXACT: frozenset[str] = frozenset()

# 安全域内已审查降级为普通组件门禁的路径。新增条目即显式削弱保护，必须
# 写独立结构化理由（allowed_apis / excluded / review），不得多页复用泛化文案。
# 含密码、角色、Raw 重放、队列恢复、解密导出、发送或策略写入的视图不得入表。
REVIEWED_ORDINARY_REASONS: MappingProxyType[str, str] = MappingProxyType(
    {
        "backend/app/services/dashboard.py": (
            "allowed_apis=dashboard_read_aggregate; "
            "excluded=auth,crypto,pii-decrypt,send,replay,resume,retry,trigger,"
            "export-step-up,role,session,config-mutation,blacklist,callback,"
            "vendor-bind; review=display-only"
        ),
        "frontend/src/views/DashboardView.vue": (
            "allowed_apis=getDashboard; "
            "excluded=auth,crypto,pii-decrypt,send,replay,resume,retry,trigger,"
            "export-step-up,role,session,config-mutation,blacklist,callback,"
            "vendor-bind; review=display-only"
        ),
    }
)
REVIEWED_ORDINARY_EXACT: frozenset[str] = frozenset(REVIEWED_ORDINARY_REASONS)

# 反向枚举根：与分类域独立。缩小 BACKEND_CRITICAL_DOMAINS 不能让这些树从扫描中消失。
REQUIRED_TRACKED_SOURCE_TREES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("backend/app/", (".py",)),
    ("frontend/src/views/", (".vue",)),
)

CODEOWNERS_OWNER = "@neuer"
CODEOWNERS_HEADER = (
    "# 安全敏感边界：短信、手机号、认证、凭据、会话与发布基础设施的变更应经过独立 Code Review。\n"
    "# 路径集合必须与 deploy/scripts/protected_path_policy.py 的安全域及发布面一致。"
    " 不要手写额外安全域规则。\n"
)
_EXTRA_CODEOWNERS_PATTERNS: tuple[str, ...] = (
    "backend/migrations/",
    "schema.sql",
    "deploy/",
    ".github/workflows/",
    "deploy/scripts/protected_path_policy.py",
)


def required_codeowners_patterns() -> tuple[str, ...]:
    """CODEOWNERS 与 manifest 共用的有序去重规则。"""

    seen: list[str] = []
    for pattern in (
        *BACKEND_CRITICAL_DOMAINS,
        *FRONTEND_SECURITY_DOMAINS,
        *_EXTRA_CODEOWNERS_PATTERNS,
    ):
        if pattern not in seen:
            seen.append(pattern)
    return tuple(seen)


REQUIRED_CODEOWNERS_PATTERNS: tuple[str, ...] = required_codeowners_patterns()


def render_codeowners() -> str:
    """由 manifest 生成 CODEOWNERS 正文；合同测试要求文件与此逐字一致。"""

    lines = [CODEOWNERS_HEADER.rstrip("\n")]
    lines.extend(f"{pattern} {CODEOWNERS_OWNER}" for pattern in required_codeowners_patterns())
    return "\n".join(lines) + "\n"


def parse_codeowners_patterns(text: str) -> tuple[str, ...]:
    """解析 CODEOWNERS 中的路径规则；忽略注释与空行。"""

    patterns: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        pattern, owner = line.rsplit(None, 1)
        if owner != CODEOWNERS_OWNER:
            raise ValueError(f"unexpected CODEOWNERS owner: {owner}")
        patterns.append(pattern)
    return tuple(patterns)


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


def is_required_tracked_source_file(path: str) -> bool:
    """是否属于必须分类的 tracked source 树（不依赖当前域清单）。"""

    for prefix, suffixes in REQUIRED_TRACKED_SOURCE_TREES:
        if path.startswith(prefix) and path.endswith(suffixes):
            return True
    return False


def unclassified_tracked_source_paths(paths: Iterable[str]) -> tuple[str, ...]:
    """反向枚举 tracked source：报告既不在声明域、也未显式降级的文件。

    扫描输入是调用方给出的全部路径（仓库级 ``git ls-files``），过滤条件是
    ``REQUIRED_TRACKED_SOURCE_TREES`` 或已声明安全域，而不是先调用
    ``is_security_domain_path()``。缩小域清单不能让必扫树从 missing 集合消失。
    """

    missing: list[str] = []
    for path in paths:
        required = is_required_tracked_source_file(path)
        in_declared_domain = is_security_domain_path(path)
        if not required and not in_declared_domain:
            continue
        if path in REVIEWED_ORDINARY_EXACT:
            continue
        if security_domain_category(path) is None:
            missing.append(path)
    return tuple(sorted(missing))


def unclassified_security_domain_paths(paths: Iterable[str]) -> tuple[str, ...]:
    """兼容旧名；实现已改为仓库级反向枚举。"""

    return unclassified_tracked_source_paths(paths)
