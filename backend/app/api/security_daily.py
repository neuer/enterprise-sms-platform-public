"""管理员安全日报查询、预览与受控独立 mailer 投递接口。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field

from app.api.auth import ERROR_RESPONSE, bearer_scheme
from app.core.audit import audited
from app.core.auth.runtime import AuthFacade, get_auth_facade
from app.core.errors import ApiError
from app.services.security_daily import (
    ConfigurationState,
    DeliveryAction,
    FileSecurityDailyControl,
    SecurityDailyConfiguration,
    SecurityDailyConfigurationError,
    SecurityDailyConfigurationUpdate,
    SecurityDailyControlError,
    SecurityDailyDeliveryRequest,
    SecurityDailyNotFound,
    SecurityDailyPage,
    SecurityDailyQuery,
    SecurityDailyReportRecord,
    SecurityDailyService,
    SecurityDailyStateConflict,
    SecurityDailyUnavailable,
    SecurityStatus,
)
from app.services.security_daily_repository import SqlSecurityDailyRepository
from app.settings import get_settings

router = APIRouter(prefix="/api/v1/web/admin/security-daily", tags=["security-daily"])


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SecurityDailyOverviewModel(StrictModel):
    enabled: bool
    configuration_state: ConfigurationState
    schedule_time: str
    timezone: str
    period_description: str
    last_generated_at: datetime | None
    last_delivered_at: datetime | None
    next_scheduled_at: datetime | None
    latest_failure: str | None
    delivery_status: Literal["not_sent", "pending", "sending", "sent", "failed"] | None
    recipient_count: int = Field(ge=0, le=3)
    resend_configured: bool
    sender_domain: str
    sender_address: str
    beat_restart_required: bool


class SecurityDailyConfigurationModel(StrictModel):
    enabled: bool
    recipients: list[str] = Field(max_length=3)
    resend_api_key_configured: bool
    sender_domain: str
    sender_address: str


class SecurityDailyConfigurationUpdateModel(StrictModel):
    enabled: bool
    recipients: list[str] = Field(max_length=3)
    resend_api_key: str | None = Field(default=None, max_length=512)


class SecurityDailyReportModel(StrictModel):
    id: int = Field(ge=1)
    report_date: date
    period_start: datetime
    period_end: datetime
    status: SecurityStatus
    generation_source: Literal["auto", "manual"]
    generation_status: Literal["pending", "ready", "failed", "unavailable"]
    delivery_status: Literal["not_sent", "pending", "sending", "sent", "failed"]
    generated_at: datetime | None
    delivered_at: datetime | None
    recipient_count: int = Field(ge=0, le=3)
    retry_count: int = Field(ge=0)
    last_error: str | None
    last_error_at: datetime | None
    updated_at: datetime
    payload: dict[str, Any] | None = None
    timeline: list[dict[str, Any]] = Field(default_factory=list)


class SecurityDailyPageModel(StrictModel):
    items: list[SecurityDailyReportModel]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)


class SecurityDailyPreviewModel(StrictModel):
    report_date: date
    status: SecurityStatus
    available: bool
    message: str | None
    html: str
    text: str
    payload: dict[str, Any] | None


class SecurityDailyDeliveryActionModel(StrictModel):
    confirm: Literal[True]


class SecurityDailyDeliveryResponseModel(StrictModel):
    request_id: UUID
    report_date: date
    action: Literal["send", "retry"]
    state: Literal["pending", "sent", "failed"]
    idempotent: bool


def get_security_daily_service() -> SecurityDailyService:
    settings = get_settings()
    return SecurityDailyService(
        SqlSecurityDailyRepository(settings),
        FileSecurityDailyControl(
            settings.security_daily_control_dir,
            settings.security_daily_config_dir,
        ),
        control_dir=settings.security_daily_control_dir,
    )


async def _admin(
    facade: AuthFacade,
    credentials: HTTPAuthorizationCredentials | None,
) -> Any:
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise ApiError(401, "UNAUTHORIZED", "缺少有效的 Bearer 令牌", None)
    claims = await facade.verify(credentials.credentials)
    if claims.role != "admin":
        raise ApiError(403, "FORBIDDEN", "仅管理员可访问安全日报", None)
    return claims


def _ip(request: Request) -> str:
    return request.client.host if request.client is not None else "0.0.0.0"


def _report_model(
    record: SecurityDailyReportRecord,
    service: SecurityDailyService,
    *,
    include_payload: bool,
) -> SecurityDailyReportModel:
    return SecurityDailyReportModel(
        id=record.id,
        report_date=record.report_date,
        period_start=record.period_start,
        period_end=record.period_end,
        status=record.status,
        generation_source=record.generation_source,
        generation_status=record.generation_status,
        delivery_status=record.delivery_status,
        generated_at=record.generated_at,
        delivered_at=record.delivered_at,
        recipient_count=record.recipient_count,
        retry_count=record.retry_count,
        last_error=record.last_error,
        last_error_at=record.last_error_at,
        updated_at=record.updated_at,
        payload=record.payload if include_payload else None,
        timeline=service.timeline(record) if include_payload else [],
    )


def _page_model(page: SecurityDailyPage, service: SecurityDailyService) -> SecurityDailyPageModel:
    return SecurityDailyPageModel(
        items=[_report_model(item, service, include_payload=False) for item in page.items],
        total=page.total,
        page=page.page,
        page_size=page.page_size,
    )


def _delivery_model(
    request: SecurityDailyDeliveryRequest,
) -> SecurityDailyDeliveryResponseModel:
    return SecurityDailyDeliveryResponseModel(
        request_id=request.request_id,
        report_date=request.report_date,
        action=request.action,
        state=request.state,
        idempotent=request.idempotent,
    )


def _unavailable(message: str) -> ApiError:
    return ApiError(503, "SECURITY_DAILY_UNAVAILABLE", message, None)


def _configuration_model(
    configuration: SecurityDailyConfiguration,
) -> SecurityDailyConfigurationModel:
    return SecurityDailyConfigurationModel(
        enabled=configuration.enabled,
        recipients=list(configuration.recipients),
        resend_api_key_configured=bool(configuration.api_key),
        sender_domain="reports.neuer.cn",
        sender_address="security-daily@reports.neuer.cn",
    )


@router.get(
    "/overview",
    response_model=SecurityDailyOverviewModel,
    responses={401: ERROR_RESPONSE, 403: ERROR_RESPONSE, 503: ERROR_RESPONSE},
)
async def security_daily_overview(
    service: Annotated[SecurityDailyService, Depends(get_security_daily_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> SecurityDailyOverviewModel:
    await _admin(facade, credentials)
    try:
        overview = await service.overview()
    except SecurityDailyControlError:
        raise _unavailable("安全日报独立投递控制面不可用") from None
    except SecurityDailyConfigurationError as error:
        raise _unavailable(str(error)) from None
    return SecurityDailyOverviewModel.model_validate(overview, from_attributes=True)


@router.get(
    "/config",
    response_model=SecurityDailyConfigurationModel,
    responses={401: ERROR_RESPONSE, 403: ERROR_RESPONSE, 503: ERROR_RESPONSE},
)
async def get_security_daily_config(
    service: Annotated[SecurityDailyService, Depends(get_security_daily_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> SecurityDailyConfigurationModel:
    await _admin(facade, credentials)
    try:
        configuration = await service.configuration()
    except SecurityDailyConfigurationError as error:
        raise _unavailable(str(error)) from None
    return _configuration_model(configuration)


@router.post(
    "/generate",
    response_model=SecurityDailyReportModel,
    responses={
        401: ERROR_RESPONSE,
        403: ERROR_RESPONSE,
        409: ERROR_RESPONSE,
        503: ERROR_RESPONSE,
    },
)
@audited("security_daily_generate")
async def generate_security_daily_report(
    request: Request,
    service: Annotated[SecurityDailyService, Depends(get_security_daily_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> SecurityDailyReportModel:
    """管理员无条件重生成上一上海自然日的日报并立即提交邮件投递。"""

    claims = await _admin(facade, credentials)
    try:
        record = await service.generate_latest(principal=claims.principal, ip=_ip(request))
    except SecurityDailyStateConflict as error:
        raise ApiError(409, "STATE_CONFLICT", str(error), None) from None
    except SecurityDailyUnavailable as error:
        raise _unavailable(str(error)) from None
    return _report_model(record, service, include_payload=False)


@router.put(
    "/config",
    response_model=SecurityDailyConfigurationModel,
    responses={400: ERROR_RESPONSE, 401: ERROR_RESPONSE, 403: ERROR_RESPONSE, 503: ERROR_RESPONSE},
)
@audited("security_daily_config_update")
async def update_security_daily_config(
    payload: SecurityDailyConfigurationUpdateModel,
    request: Request,
    service: Annotated[SecurityDailyService, Depends(get_security_daily_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> SecurityDailyConfigurationModel:
    claims = await _admin(facade, credentials)
    try:
        configuration = await service.configure(
            SecurityDailyConfigurationUpdate(
                enabled=payload.enabled,
                recipients=tuple(payload.recipients),
                api_key=payload.resend_api_key,
            ),
            principal=claims.principal,
            ip=_ip(request),
        )
    except SecurityDailyConfigurationError as error:
        raise ApiError(400, "INVALID_PARAM", str(error), None) from None
    except SecurityDailyControlError:
        raise _unavailable("安全日报配置同步失败") from None
    return _configuration_model(configuration)


@router.get(
    "/reports",
    response_model=SecurityDailyPageModel,
    responses={400: ERROR_RESPONSE, 401: ERROR_RESPONSE, 403: ERROR_RESPONSE, 503: ERROR_RESPONSE},
)
async def list_security_daily_reports(
    service: Annotated[SecurityDailyService, Depends(get_security_daily_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    date_from: date | None = None,
    date_to: date | None = None,
    report_status: Annotated[SecurityStatus | None, Query(alias="status")] = None,
    generation_status: Literal["pending", "ready", "failed", "unavailable"] | None = None,
    delivery_status: Literal["not_sent", "pending", "sending", "sent", "failed"] | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> SecurityDailyPageModel:
    await _admin(facade, credentials)
    if date_from is not None and date_to is not None and date_from > date_to:
        raise ApiError(400, "INVALID_PARAM", "日期范围无效", None)
    try:
        page_result = await service.list_reports(
            SecurityDailyQuery(
                date_from,
                date_to,
                report_status,
                generation_status,
                delivery_status,
                page,
                page_size,
            )
        )
    except SecurityDailyControlError:
        raise _unavailable("安全日报独立投递控制面不可用") from None
    except SecurityDailyConfigurationError as error:
        raise _unavailable(str(error)) from None
    return _page_model(page_result, service)


@router.get(
    "/reports/{report_id}",
    response_model=SecurityDailyReportModel,
    responses={401: ERROR_RESPONSE, 403: ERROR_RESPONSE, 404: ERROR_RESPONSE, 503: ERROR_RESPONSE},
)
async def get_security_daily_report(
    report_id: int,
    service: Annotated[SecurityDailyService, Depends(get_security_daily_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> SecurityDailyReportModel:
    await _admin(facade, credentials)
    try:
        record = await service.get_report(report_id)
    except SecurityDailyNotFound:
        raise ApiError(404, "NOT_FOUND", "安全日报记录不存在", None) from None
    except SecurityDailyControlError:
        raise _unavailable("安全日报独立投递控制面不可用") from None
    return _report_model(record, service, include_payload=True)


@router.get(
    "/reports/{report_id}/preview",
    response_model=SecurityDailyPreviewModel,
    responses={401: ERROR_RESPONSE, 403: ERROR_RESPONSE, 404: ERROR_RESPONSE, 503: ERROR_RESPONSE},
)
async def preview_security_daily_report(
    report_id: int,
    service: Annotated[SecurityDailyService, Depends(get_security_daily_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> SecurityDailyPreviewModel:
    await _admin(facade, credentials)
    try:
        preview = await service.preview(report_id)
    except SecurityDailyNotFound:
        raise ApiError(404, "NOT_FOUND", "安全日报记录不存在", None) from None
    except SecurityDailyUnavailable:
        try:
            record = await service.get_report(report_id)
        except SecurityDailyNotFound:
            raise ApiError(404, "NOT_FOUND", "安全日报记录不存在", None) from None
        return SecurityDailyPreviewModel(
            report_date=record.report_date,
            status=record.status,
            available=False,
            message="报告数据不可用，当前不生成预览",
            html="",
            text="",
            payload=None,
        )
    except SecurityDailyControlError:
        raise _unavailable("安全日报独立投递控制面不可用") from None
    return SecurityDailyPreviewModel(
        report_date=preview.report_date,
        status=preview.status,
        available=True,
        message=None,
        html=preview.html,
        text=preview.text,
        payload=preview.payload,
    )


async def _request_delivery(
    report_id: int,
    action: DeliveryAction,
    request: Request,
    service: SecurityDailyService,
    facade: AuthFacade,
    credentials: HTTPAuthorizationCredentials | None,
) -> SecurityDailyDeliveryResponseModel:
    claims = await _admin(facade, credentials)
    try:
        result = await service.request_delivery(
            report_id,
            action,
            principal=claims.principal,
            ip=_ip(request),
        )
    except SecurityDailyNotFound:
        raise ApiError(404, "NOT_FOUND", "安全日报记录不存在", None) from None
    except SecurityDailyStateConflict as error:
        raise ApiError(409, "STATE_CONFLICT", str(error), None) from None
    except SecurityDailyUnavailable as error:
        raise _unavailable(str(error)) from None
    except SecurityDailyControlError:
        raise _unavailable("安全日报独立投递控制面不可用") from None
    return _delivery_model(result)


@router.post(
    "/reports/{report_id}/send",
    response_model=SecurityDailyDeliveryResponseModel,
    status_code=status.HTTP_202_ACCEPTED,
    responses={400: ERROR_RESPONSE, 401: ERROR_RESPONSE, 403: ERROR_RESPONSE, 404: ERROR_RESPONSE,
               409: ERROR_RESPONSE, 503: ERROR_RESPONSE},
)
@audited("security_daily_send")
async def send_security_daily_report(
    report_id: int,
    _: SecurityDailyDeliveryActionModel,
    request: Request,
    service: Annotated[SecurityDailyService, Depends(get_security_daily_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> SecurityDailyDeliveryResponseModel:
    return await _request_delivery(report_id, "send", request, service, facade, credentials)


@router.post(
    "/reports/{report_id}/retry",
    response_model=SecurityDailyDeliveryResponseModel,
    status_code=status.HTTP_202_ACCEPTED,
    responses={400: ERROR_RESPONSE, 401: ERROR_RESPONSE, 403: ERROR_RESPONSE, 404: ERROR_RESPONSE,
               409: ERROR_RESPONSE, 503: ERROR_RESPONSE},
)
@audited("security_daily_retry")
async def retry_security_daily_report(
    report_id: int,
    _: SecurityDailyDeliveryActionModel,
    request: Request,
    service: Annotated[SecurityDailyService, Depends(get_security_daily_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> SecurityDailyDeliveryResponseModel:
    return await _request_delivery(report_id, "retry", request, service, facade, credentials)
