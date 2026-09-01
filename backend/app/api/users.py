"""管理员按稳定 account_id 维护本地与外部账号。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, NoReturn, cast

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field

from app.api.auth import ERROR_RESPONSE, bearer_scheme
from app.core.audit import audited
from app.core.auth.accounts import AccountSourceConflict
from app.core.auth.backends import ProviderCapacityUnavailable
from app.core.auth.identity import InvalidLoginName
from app.core.auth.jwt import JwtClaims
from app.core.auth.passwords import PasswordPolicyViolation
from app.core.auth.roles import Role
from app.core.auth.runtime import AuthFacade, get_auth_facade
from app.core.client_ip import trusted_client_ip
from app.core.errors import ApiError
from app.services.user_management import (
    LastAdminProtected,
    ProviderActionUnsupported,
    RoleMappingConflict,
    SelfDisableDenied,
    UserManagementService,
    UserNotFound,
    UserPage,
    UserRecord,
)
from app.services.user_repository import SqlUserManagementRepository
from app.settings import get_settings

router = APIRouter(prefix="/api/v1/web/admin/users", tags=["admin"])


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UserModel(StrictModel):
    account_id: int = Field(ge=1)
    identity_id: int = Field(ge=1)
    provider_code: str
    username: str
    display_name: str
    dept: str
    role: Role
    role_override: bool
    status: Literal[0, 1]
    identity_status: Literal[0, 1]
    credential_status: Literal["active", "must_change"] | None
    source_groups: list[str]
    sync_status: Literal["local", "synced", "pending", "disabled"]
    last_synced_at: datetime | None
    last_login_at: datetime | None


class UserPageModel(StrictModel):
    items: list[UserModel]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)


class LocalUserCreateModel(StrictModel):
    username: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    dept: str = Field(max_length=128)
    role: Role = "viewer"
    temporary_password: str = Field(
        min_length=1,
        max_length=128,
        json_schema_extra={"writeOnly": True},
    )


class RoleUpdateModel(StrictModel):
    role: Role
    role_override: bool = True


class StatusUpdateModel(StrictModel):
    status: Literal[0, 1]


class PasswordResetModel(StrictModel):
    temporary_password: str = Field(
        min_length=1,
        max_length=128,
        json_schema_extra={"writeOnly": True},
    )


def _token(credentials: HTTPAuthorizationCredentials | None) -> str:
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise ApiError(401, "UNAUTHORIZED", "缺少有效的 Bearer 令牌", None)
    return credentials.credentials


async def _admin(
    facade: AuthFacade,
    credentials: HTTPAuthorizationCredentials | None,
) -> JwtClaims:
    claims = await facade.verify(_token(credentials))
    if claims.role != "admin":
        raise ApiError(403, "FORBIDDEN", "仅管理员可管理用户", None)
    return claims


async def require_admin(
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
) -> JwtClaims:
    """在构造管理员业务依赖前完成 Bearer 与角色校验。"""

    return await _admin(facade, credentials)


async def get_user_management_service(
    _actor: Annotated[JwtClaims, Depends(require_admin)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
) -> UserManagementService:
    return UserManagementService(
        SqlUserManagementRepository(get_settings()),
        passwords=facade.passwords,
    )


def _ip(request: Request) -> str:
    return trusted_client_ip(request)


def _model(user: UserRecord) -> UserModel:
    return UserModel(
        account_id=user.account_id,
        identity_id=user.identity_id,
        provider_code=user.provider_code,
        username=user.username,
        display_name=user.display_name,
        dept=user.dept,
        role=user.role,
        role_override=user.role_override,
        status=cast(Literal[0, 1], user.status),
        identity_status=cast(Literal[0, 1], user.identity_status),
        credential_status=user.credential_status,
        source_groups=list(user.source_groups),
        sync_status=user.sync_status,
        last_synced_at=user.last_synced_at,
        last_login_at=user.last_login_at,
    )


def _page_model(page: UserPage) -> UserPageModel:
    return UserPageModel(
        items=[_model(item) for item in page.items],
        total=page.total,
        page=page.page,
        page_size=page.page_size,
    )


def _raise_user_error(error: Exception) -> NoReturn:
    if isinstance(error, UserNotFound):
        raise ApiError(404, "NOT_FOUND", "用户不存在", None) from None
    if isinstance(error, AccountSourceConflict):
        raise ApiError(
            409,
            "ACCOUNT_SOURCE_CONFLICT",
            "登录名已由其他认证源占用",
            None,
        ) from None
    if isinstance(error, PasswordPolicyViolation):
        raise ApiError(
            422,
            "PASSWORD_POLICY_VIOLATION",
            str(error),
            None,
        ) from None
    if isinstance(error, InvalidLoginName):
        raise ApiError(400, "INVALID_PARAM", str(error), None) from None
    if isinstance(error, LastAdminProtected):
        raise ApiError(
            409,
            "LAST_ADMIN_PROTECTED",
            "不能禁用或降级最后一个有效管理员",
            None,
        ) from None
    if isinstance(error, SelfDisableDenied):
        raise ApiError(403, "FORBIDDEN", "管理员不能禁用自己", None) from None
    if isinstance(error, RoleMappingConflict):
        raise ApiError(409, "STATE_CONFLICT", str(error), None) from None
    if isinstance(error, ProviderActionUnsupported):
        raise ApiError(409, "STATE_CONFLICT", str(error), None) from None
    if isinstance(error, ProviderCapacityUnavailable):
        raise ApiError(
            503,
            "AUTH_PROVIDER_UNAVAILABLE",
            "认证源容量暂不可用，请稍后重试",
            None,
        ) from None
    raise error


USER_ERRORS = (
    UserNotFound,
    AccountSourceConflict,
    PasswordPolicyViolation,
    InvalidLoginName,
    LastAdminProtected,
    SelfDisableDenied,
    RoleMappingConflict,
    ProviderActionUnsupported,
    ProviderCapacityUnavailable,
)


@router.get(
    "",
    response_model=UserPageModel,
    responses={401: ERROR_RESPONSE, 403: ERROR_RESPONSE},
)
async def list_users(
    _actor: Annotated[JwtClaims, Depends(require_admin)],
    service: Annotated[UserManagementService, Depends(get_user_management_service)],
    keyword: Annotated[str | None, Query(max_length=128)] = None,
    provider_code: Annotated[str | None, Query(max_length=64)] = None,
    role: Role | None = None,
    status: Annotated[int | None, Query(ge=0, le=1)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> UserPageModel:
    return _page_model(
        await service.list(
            keyword,
            provider_code,
            role,
            status,
            page,
            page_size,
        )
    )


@router.post(
    "/local",
    response_model=UserModel,
    responses={
        400: ERROR_RESPONSE,
        401: ERROR_RESPONSE,
        403: ERROR_RESPONSE,
        409: ERROR_RESPONSE,
        422: ERROR_RESPONSE,
        503: ERROR_RESPONSE,
    },
)
@audited("local_account_create")
async def create_local_user(
    payload: LocalUserCreateModel,
    request: Request,
    actor: Annotated[JwtClaims, Depends(require_admin)],
    service: Annotated[UserManagementService, Depends(get_user_management_service)],
) -> UserModel:
    try:
        return _model(
            await service.create_local(
                username=payload.username,
                display_name=payload.display_name,
                dept=payload.dept,
                role=payload.role,
                temporary_password=payload.temporary_password,
                actor=actor.login_name,
                ip=_ip(request),
            )
        )
    except USER_ERRORS as error:
        _raise_user_error(error)


@router.put(
    "/{account_id}/role",
    response_model=UserModel,
    responses={
        401: ERROR_RESPONSE,
        403: ERROR_RESPONSE,
        404: ERROR_RESPONSE,
        409: ERROR_RESPONSE,
    },
)
@audited("role_override")
async def update_user_role(
    account_id: int,
    payload: RoleUpdateModel,
    request: Request,
    actor: Annotated[JwtClaims, Depends(require_admin)],
    service: Annotated[UserManagementService, Depends(get_user_management_service)],
) -> UserModel:
    try:
        return _model(
            await service.change_role(
                account_id,
                payload.role,
                payload.role_override,
                actor=actor.login_name,
                ip=_ip(request),
            )
        )
    except USER_ERRORS as error:
        _raise_user_error(error)


@router.put(
    "/{account_id}/status",
    response_model=UserModel,
    responses={401: ERROR_RESPONSE, 403: ERROR_RESPONSE, 404: ERROR_RESPONSE, 409: ERROR_RESPONSE},
)
@audited("account_status_change")
async def update_user_status(
    account_id: int,
    payload: StatusUpdateModel,
    request: Request,
    actor: Annotated[JwtClaims, Depends(require_admin)],
    service: Annotated[UserManagementService, Depends(get_user_management_service)],
) -> UserModel:
    try:
        return _model(
            await service.change_status(
                account_id,
                payload.status,
                actor_account_id=actor.account_id,
                actor=actor.login_name,
                ip=_ip(request),
            )
        )
    except USER_ERRORS as error:
        _raise_user_error(error)


@router.post(
    "/{account_id}/password/reset",
    response_model=UserModel,
    responses={
        401: ERROR_RESPONSE,
        403: ERROR_RESPONSE,
        404: ERROR_RESPONSE,
        409: ERROR_RESPONSE,
        422: ERROR_RESPONSE,
        503: ERROR_RESPONSE,
    },
)
@audited("local_password_reset")
async def reset_user_password(
    account_id: int,
    payload: PasswordResetModel,
    request: Request,
    actor: Annotated[JwtClaims, Depends(require_admin)],
    service: Annotated[UserManagementService, Depends(get_user_management_service)],
) -> UserModel:
    try:
        return _model(
            await service.reset_password(
                account_id,
                payload.temporary_password,
                actor=actor.login_name,
                ip=_ip(request),
            )
        )
    except USER_ERRORS as error:
        _raise_user_error(error)


@router.post(
    "/{account_id}/sessions/revoke",
    response_class=Response,
    responses={401: ERROR_RESPONSE, 403: ERROR_RESPONSE, 404: ERROR_RESPONSE},
)
@audited("force_logout")
async def revoke_user_sessions(
    account_id: int,
    request: Request,
    _actor: Annotated[JwtClaims, Depends(require_admin)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
) -> Response:
    token = _token(credentials)
    await facade.force_logout(token, account_id, _ip(request))
    return Response(status_code=200)
