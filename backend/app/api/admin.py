"""管理员审计日志与系统参数接口。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

from app.api.auth import ERROR_RESPONSE, bearer_scheme
from app.core.audit import audited
from app.core.auth.accounts import SecurityPrincipal
from app.core.auth.runtime import AuthFacade, get_auth_facade
from app.core.errors import ApiError
from app.services.admin import (
    AdminService,
    AuditQuery,
    ConfigUpdate,
    InvalidAdminQuery,
)
from app.services.admin_repository import SqlAdminRepository
from app.settings import get_settings

router = APIRouter(prefix="/api/v1/web/admin", tags=["admin"])


class AuditModel(BaseModel):
    id: int
    correlation_id: UUID
    actor: str
    actor_subject_kind: Literal["human", "api_app", "system", "legacy_unknown"]
    actor_account_id: Annotated[int | None, Field(ge=1)]
    actor_identity_id: Annotated[int | None, Field(ge=1)]
    actor_app_id: Annotated[int | None, Field(ge=1)]
    role: str | None
    ip: str | None
    action: str
    object_type: str | None
    object_id: str | None
    before_val: dict[str, Any] | None
    after_val: dict[str, Any] | None
    created_at: datetime


class AuditPageModel(BaseModel):
    items: list[AuditModel]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)


class ConfigModel(BaseModel):
    key: str
    value: str | None
    value_type: Literal["str", "int", "bool", "json"]
    description: str | None
    group: str
    sensitive: bool
    configured: bool
    beat_restart_required: bool
    updated_by: str | None
    updated_at: datetime | None


class ConfigUpdateModel(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    value: str | None = Field(default=None, max_length=512)


class ConfigBatchUpdateModel(BaseModel):
    items: list[ConfigUpdateModel] = Field(min_length=1, max_length=50)


def get_admin_service() -> AdminService:
    settings = get_settings()
    return AdminService(
        SqlAdminRepository(settings),
        allowed_smtp_hosts=settings.alert_smtp_allowed_host_set,
    )


async def _admin(
    request: Request,
    facade: AuthFacade,
    credentials: HTTPAuthorizationCredentials | None,
) -> tuple[SecurityPrincipal, str]:
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise ApiError(401, "UNAUTHORIZED", "缺少有效的 Bearer 令牌", None)
    claims = await facade.verify(credentials.credentials)
    if claims.role != "admin":
        raise ApiError(403, "FORBIDDEN", "仅管理员可访问审计与系统参数", None)
    ip = request.client.host if request.client is not None else "0.0.0.0"
    return claims.principal, ip


@router.get(
    "/audit-logs",
    response_model=AuditPageModel,
    responses={400: ERROR_RESPONSE, 401: ERROR_RESPONSE, 403: ERROR_RESPONSE},
)
async def list_audit_logs(
    request: Request,
    service: Annotated[AdminService, Depends(get_admin_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    actor: Annotated[str | None, Query(max_length=64)] = None,
    actor_account_id: Annotated[int | None, Query(ge=1)] = None,
    correlation_id: UUID | None = None,
    action: Annotated[str | None, Query(max_length=48)] = None,
    object_type: Annotated[str | None, Query(max_length=32)] = None,
    start: datetime | None = None,
    end: datetime | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> AuditPageModel:
    await _admin(request, facade, credentials)
    try:
        items, total = await service.list_audits(
            AuditQuery(
                actor,
                action,
                object_type,
                start,
                end,
                page,
                page_size,
                actor_account_id=actor_account_id,
                correlation_id=correlation_id,
            )
        )
    except InvalidAdminQuery as error:
        raise ApiError(400, "INVALID_PARAM", str(error), None) from None
    return AuditPageModel(
        items=[AuditModel.model_validate(item, from_attributes=True) for item in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/configs",
    response_model=list[ConfigModel],
    responses={401: ERROR_RESPONSE, 403: ERROR_RESPONSE},
)
async def list_configs(
    request: Request,
    service: Annotated[AdminService, Depends(get_admin_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> list[ConfigModel]:
    await _admin(request, facade, credentials)
    return [
        ConfigModel.model_validate(item, from_attributes=True)
        for item in await service.list_configs()
    ]


@router.put(
    "/configs",
    response_model=list[ConfigModel],
    responses={400: ERROR_RESPONSE, 401: ERROR_RESPONSE, 403: ERROR_RESPONSE},
)
@audited("config_update")
async def update_configs(
    payload: ConfigBatchUpdateModel,
    request: Request,
    service: Annotated[AdminService, Depends(get_admin_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> list[ConfigModel]:
    principal, ip = await _admin(request, facade, credentials)
    try:
        values = await service.update_configs(
            tuple(ConfigUpdate(item.key, item.value) for item in payload.items),
            principal=principal,
            ip=ip,
        )
    except InvalidAdminQuery as error:
        raise ApiError(400, "INVALID_PARAM", str(error), None) from None
    return [ConfigModel.model_validate(item, from_attributes=True) for item in values]
