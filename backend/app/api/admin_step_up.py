"""当前管理员重认证签发入口；口令与授权令牌仅通过易失请求传递。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.api.auth import ERROR_RESPONSE, bearer_scheme
from app.core.audit import AuditEvent, audited, insert_audit
from app.core.auth.jwt import JwtClaims
from app.core.auth.runtime import AuthFacade, get_auth_facade
from app.core.client_ip import trusted_client_ip
from app.core.errors import ApiError
from app.core.runtime_resources import database_engine, redis_client
from app.services.admin_step_up import AdminOperation, AdminStepUpService, admin_intent
from app.services.auth_provider import InvalidProviderConfig
from app.settings import get_settings

router = APIRouter(prefix="/api/v1/web/admin", tags=["admin"])
AdminStepUpToken = Annotated[
    str | None, Header(alias="X-Admin-Step-Up", min_length=1, max_length=256)
]


async def require_step_up_admin(
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> JwtClaims:
    """先验证当前 JWT 和管理员角色，再构造二次认证存储。"""
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise ApiError(401, "UNAUTHORIZED", "缺少有效的 Bearer 令牌", None)
    claims = await facade.verify(credentials.credentials)
    if claims.role != "admin":
        raise ApiError(403, "FORBIDDEN", "仅管理员可操作", None)
    return claims


StepUpAdmin = Annotated[JwtClaims, Depends(require_step_up_admin)]


def get_admin_step_up_service(
    _actor: StepUpAdmin,
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
) -> AdminStepUpService:
    settings = get_settings()

    async def audit(event: AuditEvent) -> None:
        async with database_engine(settings.database_url_for("auth")).begin() as connection:
            await insert_audit(connection, event)

    return AdminStepUpService(facade, redis_client(settings.redis_auth_url), audit)


AdminStepUp = Annotated[AdminStepUpService, Depends(get_admin_step_up_service)]


class AdminStepUpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: AdminOperation
    target_id: str = Field(min_length=1, max_length=64)
    parameters: dict[str, Any]
    password: SecretStr = Field(min_length=1, max_length=128, json_schema_extra={"writeOnly": True})


class AdminStepUpResponse(BaseModel):
    token: str
    expires_in: int = 300


@router.post(
    "/step-up",
    response_model=AdminStepUpResponse,
    responses={
        401: ERROR_RESPONSE,
        403: ERROR_RESPONSE,
        422: ERROR_RESPONSE,
        423: ERROR_RESPONSE,
        429: ERROR_RESPONSE,
        503: ERROR_RESPONSE,
    },
)
@audited("admin_step_up")
async def issue_admin_step_up(
    payload: AdminStepUpRequest,
    request: Request,
    response: Response,
    actor: StepUpAdmin,
    service: AdminStepUp,
) -> AdminStepUpResponse:
    try:
        intent = admin_intent(payload.operation, payload.target_id, payload.parameters)
    except (InvalidProviderConfig, TypeError, ValueError):
        raise ApiError(422, "INVALID_PARAM", "二次认证操作参数无效", None) from None
    token = await service.issue(
        claims=actor,
        password=payload.password.get_secret_value(),
        ip=trusted_client_ip(request),
        intent=intent,
    )
    response.headers["Cache-Control"] = "no-store"
    return AdminStepUpResponse(token=token)
