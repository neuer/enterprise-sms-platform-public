"""管理员应用 CRUD 与密钥轮换接口。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field, with_config
from typing_extensions import TypedDict

from app.api.auth import ERROR_RESPONSE, bearer_scheme
from app.core.audit import audited
from app.core.auth.runtime import AuthFacade, get_auth_facade
from app.core.errors import ApiError
from app.services.app_management import (
    AppCreate,
    AppManagementService,
    AppNotFound,
    AppUpdate,
    CallbackUrlValidator,
    CallbackValidationUnavailable,
    InvalidAppConfig,
)
from app.services.app_repository import SqlAppRepository
from app.services.crypto import CryptoService
from app.settings import get_settings

router = APIRouter(prefix="/api/v1/web/admin/apps", tags=["admin"])
Category = Literal["verify", "notice", "market"]
IpCidr = Annotated[str, Field(max_length=64)]


def _no_store(response: Response) -> None:
    """一次性凭据响应禁止被浏览器或中间缓存保存。"""

    response.headers["Cache-Control"] = "no-store"


@with_config(ConfigDict(extra="forbid"))
class FrequencyOverride(TypedDict, total=False):
    """应用级号码频控覆盖；拒绝未定义键及隐式类型转换。"""

    verify_per_minute: Annotated[int, Field(strict=True, ge=1, le=100)]
    verify_per_day: Annotated[int, Field(strict=True, ge=1, le=10_000)]
    market_per_day: Annotated[int, Field(strict=True, ge=1, le=1_000)]


def _frequency_values(value: FrequencyOverride | None) -> dict[str, int] | None:
    if value is None:
        return None
    result: dict[str, int] = {}
    if "verify_per_minute" in value:
        result["verify_per_minute"] = value["verify_per_minute"]
    if "verify_per_day" in value:
        result["verify_per_day"] = value["verify_per_day"]
    if "market_per_day" in value:
        result["market_per_day"] = value["market_per_day"]
    return result or None


class AppCreateRequest(BaseModel):
    name: str
    dept: str
    allowed_categories: list[Category] = ["verify", "notice", "market"]
    default_sign: str | None = None
    daily_quota: int = Field(default=0, ge=0, le=100_000_000)
    rate_limit_per_min: int = Field(default=60, ge=1, le=60_000)
    blacklist_check: bool = True
    freq_override: FrequencyOverride | None = None
    allowed_ips: list[IpCidr] = Field(
        default_factory=list,
        max_length=50,
        description="入站来源 IP/CIDR 白名单，空数组=不限",
    )
    callback_url: str | None = None
    callback_report_enabled: bool = False


class AppUpdateRequest(BaseModel):
    dept: str
    allowed_categories: list[Category]
    default_sign: str | None = None
    daily_quota: int = Field(ge=0, le=100_000_000)
    rate_limit_per_min: int = Field(ge=1, le=60_000)
    blacklist_check: bool
    freq_override: FrequencyOverride | None = None
    allowed_ips: list[IpCidr] = Field(
        default_factory=list,
        max_length=50,
        description="入站来源 IP/CIDR 白名单，空数组=不限",
    )
    callback_url: str | None = None
    callback_report_enabled: bool = False
    status: Literal[0, 1] = 1


class AppResponse(BaseModel):
    id: int
    name: str
    dept: str
    allowed_categories: list[Category]
    default_sign: str | None = None
    daily_quota: int
    rate_limit_per_min: int
    blacklist_check: bool
    freq_override: FrequencyOverride | None = None
    allowed_ips: list[IpCidr] = Field(
        default_factory=list,
        max_length=50,
        description="入站来源 IP/CIDR 白名单，空数组=不限",
    )
    callback_url: str | None = None
    callback_report_enabled: bool = False
    status: Literal[0, 1]


class CreateAppResponse(BaseModel):
    id: int
    api_key: str
    callback_secret: str | None


class RotateKeyResponse(BaseModel):
    api_key: str
    old_key_expires_at: datetime


class RotateCallbackSecretResponse(BaseModel):
    callback_secret: str


async def get_app_management_service() -> AppManagementService:
    settings = get_settings()
    repository = SqlAppRepository(settings)
    grace_hours, allow_cidrs = await repository.load_security_config()
    return AppManagementService(
        repository,
        CryptoService.from_settings(settings),
        CallbackUrlValidator(
            allow_cidrs,
            allow_http=settings.environment != "production",
        ),
        key_grace=timedelta(hours=grace_hours),
    )


def _token(credentials: HTTPAuthorizationCredentials | None) -> str:
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise ApiError(401, "UNAUTHORIZED", "缺少有效的 Bearer 令牌", None)
    return credentials.credentials


async def _admin(
    request: Request,
    facade: AuthFacade,
    credentials: HTTPAuthorizationCredentials | None,
) -> tuple[str, str]:
    claims = await facade.verify(_token(credentials))
    if claims.role != "admin":
        raise ApiError(403, "FORBIDDEN", "仅管理员可管理应用", None)
    ip = request.client.host if request.client is not None else "0.0.0.0"
    return claims.username, ip


def _translate(error: Exception) -> ApiError:
    if isinstance(error, AppNotFound):
        return ApiError(404, "NOT_FOUND", "应用不存在", None)
    if isinstance(error, InvalidAppConfig):
        return ApiError(422, "INVALID_PARAM", str(error), None)
    if isinstance(error, CallbackValidationUnavailable):
        return ApiError(503, "DEPENDENCY_UNAVAILABLE", str(error), None)
    return ApiError(500, "INTERNAL_ERROR", "服务内部错误", None)


@router.get("", response_model=list[AppResponse])
async def list_apps(
    request: Request,
    service: Annotated[AppManagementService, Depends(get_app_management_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
) -> list[dict[str, Any]]:
    await _admin(request, facade, credentials)
    return await service.list()


@router.post(
    "",
    response_model=CreateAppResponse,
    responses={422: ERROR_RESPONSE, 503: ERROR_RESPONSE},
)
@audited("app_create")
async def create_app(
    payload: AppCreateRequest,
    request: Request,
    response: Response,
    service: Annotated[AppManagementService, Depends(get_app_management_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
) -> dict[str, Any]:
    actor, ip = await _admin(request, facade, credentials)
    try:
        result = await service.create(
            AppCreate(
                **payload.model_dump(
                    exclude={"allowed_categories", "freq_override", "allowed_ips"}
                ),
                allowed_categories=frozenset(payload.allowed_categories),
                freq_override=_frequency_values(payload.freq_override),
                allowed_ips=tuple(payload.allowed_ips),
            ),
            actor=actor,
            ip=ip,
        )
        _no_store(response)
        return result
    except (AppNotFound, InvalidAppConfig, CallbackValidationUnavailable) as error:
        raise _translate(error) from None


@router.get("/{id}", response_model=AppResponse, responses={404: ERROR_RESPONSE})
async def get_app(
    id: int,
    request: Request,
    service: Annotated[AppManagementService, Depends(get_app_management_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
) -> dict[str, Any]:
    await _admin(request, facade, credentials)
    try:
        return await service.get(id)
    except AppNotFound as error:
        raise _translate(error) from None


@router.put(
    "/{id}",
    response_model=AppResponse,
    responses={404: ERROR_RESPONSE, 422: ERROR_RESPONSE, 503: ERROR_RESPONSE},
)
@audited("app_update")
async def update_app(
    id: int,
    payload: AppUpdateRequest,
    request: Request,
    service: Annotated[AppManagementService, Depends(get_app_management_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
) -> dict[str, Any]:
    actor, ip = await _admin(request, facade, credentials)
    try:
        return await service.update(
            id,
            AppUpdate(
                **payload.model_dump(
                    exclude={"allowed_categories", "freq_override", "allowed_ips"}
                ),
                allowed_categories=frozenset(payload.allowed_categories),
                freq_override=_frequency_values(payload.freq_override),
                allowed_ips=tuple(payload.allowed_ips),
            ),
            actor=actor,
            ip=ip,
        )
    except (AppNotFound, InvalidAppConfig, CallbackValidationUnavailable) as error:
        raise _translate(error) from None


@router.delete(
    "/{id}",
    status_code=204,
    response_class=Response,
    responses={404: ERROR_RESPONSE},
)
@audited("app_disable")
async def disable_app(
    id: int,
    request: Request,
    service: Annotated[AppManagementService, Depends(get_app_management_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
) -> Response:
    actor, ip = await _admin(request, facade, credentials)
    try:
        await service.disable(id, actor=actor, ip=ip)
    except AppNotFound as error:
        raise _translate(error) from None
    return Response(status_code=204)


@router.post(
    "/{id}/rotate-key",
    response_model=RotateKeyResponse,
    responses={404: ERROR_RESPONSE},
)
@audited("app_rotate_key")
async def rotate_key(
    id: int,
    request: Request,
    response: Response,
    service: Annotated[AppManagementService, Depends(get_app_management_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
) -> dict[str, Any]:
    actor, ip = await _admin(request, facade, credentials)
    try:
        result = await service.rotate_key(id, actor=actor, ip=ip)
        _no_store(response)
        return result
    except AppNotFound as error:
        raise _translate(error) from None


@router.post(
    "/{id}/revoke-old-key",
    response_class=Response,
    responses={404: ERROR_RESPONSE},
)
@audited("app_revoke_old_key")
async def revoke_old_key(
    id: int,
    request: Request,
    service: Annotated[AppManagementService, Depends(get_app_management_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
) -> Response:
    actor, ip = await _admin(request, facade, credentials)
    try:
        await service.revoke_old_key(id, actor=actor, ip=ip)
    except AppNotFound as error:
        raise _translate(error) from None
    return Response(status_code=200)


@router.post(
    "/{id}/rotate-callback-secret",
    response_model=RotateCallbackSecretResponse,
    responses={404: ERROR_RESPONSE},
)
@audited("app_rotate_callback_secret")
async def rotate_callback_secret(
    id: int,
    request: Request,
    response: Response,
    service: Annotated[AppManagementService, Depends(get_app_management_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
) -> dict[str, str]:
    actor, ip = await _admin(request, facade, credentials)
    try:
        result = await service.rotate_callback_secret(id, actor=actor, ip=ip)
        _no_store(response)
        return result
    except AppNotFound as error:
        raise _translate(error) from None
