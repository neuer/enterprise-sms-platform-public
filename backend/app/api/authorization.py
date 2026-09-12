"""业务依赖构造前的显式 Web 认证与角色边界。"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials

from app.api.auth import bearer_scheme
from app.core.auth.jwt import JwtClaims
from app.core.auth.runtime import AuthFacade, get_auth_facade
from app.core.errors import ApiError


async def require_web_actor(
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> JwtClaims:
    """先验证 Bearer 的权威主体；业务构造不得提前访问其依赖。"""
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise ApiError(401, "UNAUTHORIZED", "缺少有效的 Bearer 令牌", None)
    return await facade.verify(credentials.credentials)


WebActor = Annotated[JwtClaims, Depends(require_web_actor)]


async def require_admin_actor(actor: WebActor) -> JwtClaims:
    """管理员专用业务依赖在角色校验后才可构造。"""
    if actor.role != "admin":
        raise ApiError(403, "FORBIDDEN", "仅管理员可操作", None)
    return actor


async def require_approver_actor(actor: WebActor) -> JwtClaims:
    """审批及解密二次认证依赖保留管理员与审批员两种角色。"""
    if actor.role not in {"admin", "approver"}:
        raise ApiError(403, "FORBIDDEN", "仅审批人可操作", None)
    return actor


AdminActor = Annotated[JwtClaims, Depends(require_admin_actor)]
ApproverActor = Annotated[JwtClaims, Depends(require_approver_actor)]


async def require_writer_actor(actor: WebActor) -> JwtClaims:
    """发送与回复写入仅允许操作员和管理员。"""
    if actor.role not in {"admin", "operator"}:
        raise ApiError(403, "FORBIDDEN", "仅操作员或管理员可操作", None)
    return actor


WriterActor = Annotated[JwtClaims, Depends(require_writer_actor)]
