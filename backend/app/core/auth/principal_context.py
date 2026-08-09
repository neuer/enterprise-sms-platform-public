"""把已验证稳定主体绑定到当前请求/任务上下文，供事务审计使用。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from app.core.auth.accounts import ActorPrincipal

_audit_principal: ContextVar[ActorPrincipal | None] = ContextVar(
    "audit_principal",
    default=None,
)


def current_audit_principal() -> ActorPrincipal | None:
    """只读返回当前已验证主体；未认证上下文必须返回空。"""

    return _audit_principal.get()


def bind_audit_principal(principal: ActorPrincipal) -> None:
    """认证成功后绑定稳定主体；调用方不得传入展示字符串。"""

    _audit_principal.set(principal)


@contextmanager
def audit_principal_scope(
    principal: ActorPrincipal | None = None,
) -> Iterator[None]:
    """为单个 HTTP 请求或显式后台操作隔离审计主体。"""

    token = _audit_principal.set(principal)
    try:
        yield
    finally:
        _audit_principal.reset(token)
