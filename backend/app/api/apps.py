"""管理员应用 CRUD 与密钥轮换接口。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

from app.api.admin_step_up import AdminStepUp, AdminStepUpToken
from app.api.auth import ERROR_RESPONSE, bearer_scheme
from app.api.authorization import AdminActor
from app.core.audit import audited
from app.core.client_ip import trusted_client_ip
from app.core.errors import ApiError
from app.services.admin_step_up import admin_intent
from app.services.app_contract import (
    AppCreateRequest,
    AppUpdateRequest,
    Category,
    FrequencyOverride,
    IpCidr,
    _frequency_values,
)
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
from app.services.callback_authority import CallbackAuthorityBusy
from app.services.crypto import CryptoService
from app.settings import get_settings

APP_WRITE_ERRORS: dict[int | str, dict[str, Any]] = {
    code: ERROR_RESPONSE for code in (400, 401, 403, 404, 409, 422, 503)
}

router = APIRouter(prefix="/api/v1/web/admin/apps", tags=["admin"])


def _no_store(response: Response) -> None:
    """一次性凭据响应禁止被浏览器或中间缓存保存。"""

    response.headers["Cache-Control"] = "no-store"


class AppResponse(BaseModel):
    id: int
    name: str
    dept: str
    allowed_categories: list[Category]
    default_sign: str | None = None
    daily_quota: int
    rate_limit_per_min: int
    recipient_limit_per_min: int = 10_000
    segment_limit_per_min: int = 10_000
    max_in_flight_chunks: int = 200
    allow_market_api_bulk: bool = False
    blacklist_check: bool
    freq_override: FrequencyOverride | None = None
    allowed_ips: list[IpCidr] = Field(
        default_factory=list,
        max_length=50,
        description="入站来源 IP/CIDR 白名单；生产空数组须填写未过期豁免",
    )
    ip_allowlist_exempt_until: datetime | None = None
    unlimited_quota_exempt_until: datetime | None = None
    admission_exempt_note: str | None = None
    callback_url: str | None = None
    callback_report_enabled: bool = False
    status: Literal[0, 1]
    # 以下为非密只读投影：8 位 Key 前缀与密钥生命周期元数据，不含任何哈希/密文材料
    api_key_prefix: str = Field(description="当前 API Key 的 8 位前缀，仅用于界面辨识")
    old_key_prefix: str | None = Field(
        default=None, description="宽限期旧 Key 前缀；null 表示无宽限期旧 Key"
    )
    old_key_expires_at: datetime | None = Field(
        default=None, description="宽限期旧 Key 到期时间；null 表示无宽限期旧 Key"
    )
    callback_secret_configured: bool = Field(description="回调签名密钥是否已配置")
    created_at: datetime
    updated_at: datetime


class CreateAppResponse(BaseModel):
    id: int
    api_key: str
    callback_secret: str | None


class RotateKeyResponse(BaseModel):
    api_key: str
    old_key_expires_at: datetime


class RotateCallbackSecretResponse(BaseModel):
    callback_secret: str


async def get_app_management_service(_actor: AdminActor) -> AppManagementService:
    settings = get_settings()
    repository = SqlAppRepository(settings)
    grace_hours, allow_cidrs = await repository.load_security_config()
    return AppManagementService(
        repository,
        CryptoService.from_settings(settings),
        CallbackUrlValidator(
            allow_cidrs,
            deployment_allow_cidrs=settings.callback_egress_networks,
            deployment_allow_ports=settings.callback_egress_port_set,
            allow_http=settings.environment != "production",
        ),
        key_grace=timedelta(hours=grace_hours),
    )


def _translate(error: Exception) -> ApiError:
    if isinstance(error, AppNotFound):
        return ApiError(404, "NOT_FOUND", "应用不存在", None)
    if isinstance(error, InvalidAppConfig):
        return ApiError(400, "INVALID_PARAM", str(error), None)
    if isinstance(error, CallbackValidationUnavailable):
        return ApiError(503, "DEPENDENCY_UNAVAILABLE", str(error), None)
    if isinstance(error, CallbackAuthorityBusy):
        return ApiError(409, "STATE_CONFLICT", str(error), None)
    return ApiError(500, "INTERNAL_ERROR", "服务内部错误", None)


@router.get("", response_model=list[AppResponse])
async def list_apps(
    request: Request,
    service: Annotated[AppManagementService, Depends(get_app_management_service)],
    claims: AdminActor,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
) -> list[dict[str, Any]]:
    return await service.list()


@router.post(
    "",
    response_model=CreateAppResponse,
    responses=APP_WRITE_ERRORS,
)
@audited("app_create")
async def create_app(
    payload: AppCreateRequest,
    request: Request,
    response: Response,
    service: Annotated[AppManagementService, Depends(get_app_management_service)],
    claims: AdminActor,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
    step_up: AdminStepUp,
    step_up_token: AdminStepUpToken = None,
) -> dict[str, Any]:
    actor, ip = claims.username, trusted_client_ip(request)
    authorization = await step_up.consume(
        step_up_token,
        access_token=credentials.credentials if credentials is not None else "",
        claims=claims,
        ip=ip,
        intent=admin_intent("app_create", "new", {"config": payload.model_dump()}),
    )
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
            authorization=authorization,
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
    claims: AdminActor,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
) -> dict[str, Any]:
    try:
        return await service.get(id)
    except AppNotFound as error:
        raise _translate(error) from None


@router.put(
    "/{id}",
    response_model=AppResponse,
    responses=APP_WRITE_ERRORS,
)
@audited("app_update")
async def update_app(
    id: int,
    payload: AppUpdateRequest,
    request: Request,
    service: Annotated[AppManagementService, Depends(get_app_management_service)],
    claims: AdminActor,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
    step_up: AdminStepUp,
    expected_updated_at: Annotated[datetime, Header(alias="X-App-Revision")],
    step_up_token: AdminStepUpToken = None,
) -> dict[str, Any]:
    actor, ip = claims.username, trusted_client_ip(request)
    authorization = await step_up.consume(
        step_up_token,
        access_token=credentials.credentials if credentials is not None else "",
        claims=claims,
        ip=ip,
        intent=admin_intent(
            "app_update",
            str(id),
            {"config": payload.model_dump(), "expected_updated_at": expected_updated_at},
        ),
    )
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
            authorization=authorization,
            expected_updated_at=expected_updated_at,
        )
    except (
        AppNotFound,
        CallbackAuthorityBusy,
        InvalidAppConfig,
        CallbackValidationUnavailable,
    ) as error:
        raise _translate(error) from None


@router.delete(
    "/{id}",
    status_code=204,
    response_class=Response,
    responses=APP_WRITE_ERRORS,
)
@audited("app_disable")
async def disable_app(
    id: int,
    request: Request,
    service: Annotated[AppManagementService, Depends(get_app_management_service)],
    claims: AdminActor,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
    step_up: AdminStepUp,
    expected_updated_at: Annotated[datetime, Header(alias="X-App-Revision")],
    step_up_token: AdminStepUpToken = None,
) -> Response:
    actor, ip = claims.username, trusted_client_ip(request)
    authorization = await step_up.consume(
        step_up_token,
        access_token=credentials.credentials if credentials is not None else "",
        claims=claims,
        ip=ip,
        intent=admin_intent("app_disable", str(id), {"expected_updated_at": expected_updated_at}),
    )
    try:
        await service.disable(
            id,
            actor=actor,
            ip=ip,
            authorization=authorization,
            expected_updated_at=expected_updated_at,
        )
    except (AppNotFound, CallbackAuthorityBusy) as error:
        raise _translate(error) from None
    return Response(status_code=204)


@router.post(
    "/{id}/rotate-key",
    response_model=RotateKeyResponse,
    responses=APP_WRITE_ERRORS,
)
@audited("app_rotate_key")
async def rotate_key(
    id: int,
    request: Request,
    response: Response,
    service: Annotated[AppManagementService, Depends(get_app_management_service)],
    claims: AdminActor,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
    step_up: AdminStepUp,
    expected_updated_at: Annotated[datetime, Header(alias="X-App-Revision")],
    step_up_token: AdminStepUpToken = None,
) -> dict[str, Any]:
    actor, ip = claims.username, trusted_client_ip(request)
    authorization = await step_up.consume(
        step_up_token,
        access_token=credentials.credentials if credentials is not None else "",
        claims=claims,
        ip=ip,
        intent=admin_intent(
            "app_rotate_key", str(id), {"expected_updated_at": expected_updated_at}
        ),
    )
    try:
        result = await service.rotate_key(
            id,
            actor=actor,
            ip=ip,
            authorization=authorization,
            expected_updated_at=expected_updated_at,
        )
        _no_store(response)
        return result
    except AppNotFound as error:
        raise _translate(error) from None


@router.post(
    "/{id}/revoke-old-key",
    response_class=Response,
    responses=APP_WRITE_ERRORS,
)
@audited("app_revoke_old_key")
async def revoke_old_key(
    id: int,
    request: Request,
    service: Annotated[AppManagementService, Depends(get_app_management_service)],
    claims: AdminActor,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
    step_up: AdminStepUp,
    expected_updated_at: Annotated[datetime, Header(alias="X-App-Revision")],
    step_up_token: AdminStepUpToken = None,
) -> Response:
    actor, ip = claims.username, trusted_client_ip(request)
    authorization = await step_up.consume(
        step_up_token,
        access_token=credentials.credentials if credentials is not None else "",
        claims=claims,
        ip=ip,
        intent=admin_intent(
            "app_revoke_old_key", str(id), {"expected_updated_at": expected_updated_at}
        ),
    )
    try:
        await service.revoke_old_key(
            id,
            actor=actor,
            ip=ip,
            authorization=authorization,
            expected_updated_at=expected_updated_at,
        )
    except AppNotFound as error:
        raise _translate(error) from None
    return Response(status_code=200)


@router.post(
    "/{id}/rotate-callback-secret",
    response_model=RotateCallbackSecretResponse,
    responses=APP_WRITE_ERRORS,
)
@audited("app_rotate_callback_secret")
async def rotate_callback_secret(
    id: int,
    request: Request,
    response: Response,
    service: Annotated[AppManagementService, Depends(get_app_management_service)],
    claims: AdminActor,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
    step_up: AdminStepUp,
    expected_updated_at: Annotated[datetime, Header(alias="X-App-Revision")],
    step_up_token: AdminStepUpToken = None,
) -> dict[str, str]:
    actor, ip = claims.username, trusted_client_ip(request)
    authorization = await step_up.consume(
        step_up_token,
        access_token=credentials.credentials if credentials is not None else "",
        claims=claims,
        ip=ip,
        intent=admin_intent(
            "app_rotate_callback_secret", str(id), {"expected_updated_at": expected_updated_at}
        ),
    )
    try:
        result = await service.rotate_callback_secret(
            id,
            actor=actor,
            ip=ip,
            authorization=authorization,
            expected_updated_at=expected_updated_at,
        )
        _no_store(response)
        return result
    except (AppNotFound, CallbackAuthorityBusy) as error:
        raise _translate(error) from None
