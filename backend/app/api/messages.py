"""应用侧短信发送受理接口。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, Query, Request, Response, Security
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.api.auth import ERROR_RESPONSE, bearer_scheme
from app.api.vendor_control_ready import raise_vendor_control_unavailable
from app.core.apikey import ApiAppContext, optional_api_app, require_api_app
from app.core.audit import audited
from app.core.auth.accounts import ActorPrincipal, ApplicationPrincipal
from app.core.auth.runtime import get_auth_facade
from app.core.client_ip import trusted_client_ip
from app.core.errors import ApiError
from app.core.runtime_resources import redis_client
from app.services.app_ratelimit import (
    ApplicationRateLimiter,
    ApplicationRateLimitExceeded,
)
from app.services.batch_query import BatchAccessScope, BatchNotFound, BatchQueryService
from app.services.category import CategoryNotAllowed
from app.services.crypto import CryptoService, ProtectedPhone
from app.services.freq import FrequencyFenceLost, FrequencyLimiter
from app.services.idempotency import IdempotencyCoordinationTimeout, IdempotencyCoordinator
from app.services.pipeline import (
    AcceptancePreauthorization,
    AllFiltered,
    BatchResponse,
    ConsentRequired,
    IdempotencyClaimLost,
    IdempotencyConflict,
    InvalidContent,
    PipelineConfig,
    SendPipeline,
    SendRequest,
    SensitiveWord,
    VendorTestConsoleOnly,
)
from app.services.pipeline_repository import SqlPipelineStore, SqlTemplateRenderer
from app.services.queue import CeleryQueuePublisher
from app.services.quota import QuotaExceeded, QuotaService
from app.services.resend import NoFailedRecipients, ResendService, SqlResendRepository
from app.services.runtime_policy import RuntimePolicy, SqlRuntimePolicyLoader
from app.services.scheduling import SchedulingService
from app.services.scheduling import StateConflict as SchedulingConflict
from app.services.scheduling_repository import SqlSchedulingRepository
from app.services.sensitive_read_audit import (
    SensitiveReadAuditor,
    get_sensitive_read_auditor,
)
from app.services.sign import SignResolver
from app.services.sign_repository import SqlSignRepository
from app.services.template import TemplateParamMismatch
from app.services.usage_ledger import (
    UsageLedgerService,
    UsageProjectionUnavailable,
    UsageReservationConflict,
)
from app.services.vendor_control_state import (
    VendorControlStateGuard,
    VendorControlStateUnavailable,
)
from app.services.vendor_test_guard import (
    VendorTestRecipientDenied,
)
from app.services.vendor_test_recipient import (
    RecipientHmacIndexStale,
    RecipientNotFound,
    VendorTestRecipientForSend,
    VendorTestRecipientService,
)
from app.services.vendor_test_recipient_repository import (
    SqlVendorTestRecipientRepository,
)
from app.settings import get_settings

router = APIRouter(prefix="/api/v1/messages", tags=["messages"])
Category = Literal["verify", "notice", "market"]


class SendRequestModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "oneOf": [
                {"required": ["content"], "not": {"required": ["template_id"]}},
                {
                    "required": ["template_id", "template_params"],
                    "not": {"required": ["content"]},
                },
            ]
        }
    )

    category: Category
    mobiles: list[Annotated[str, Field(pattern=r"^1\d{10}$")]] = Field(
        min_length=1,
        max_length=10000,
    )
    content: str | None = Field(default=None, max_length=500)
    template_id: int | None = None
    template_params: list[str] | None = None
    sign_name: str | None = None
    scheduled_at: datetime | None = None
    biz_id: str = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def content_or_template(self) -> SendRequestModel:
        if (self.content is None) == (self.template_id is None):
            raise ValueError("content 与 template_id 必须且只能提供一个")
        return self


class SendResponseModel(BaseModel):
    batch_no: str
    idempotent: bool
    accepted: int
    removed_duplicate: int
    removed_blacklist: int
    removed_freq_limit: int
    est_segments: int
    quota_cost: int
    status: Literal[
        "queued",
        "scheduled",
        "sending",
        "completed",
        "cancelled",
        "balance_blocked",
    ]
    deferred_reason: Literal["market_window"] | None
    scheduled_at: datetime | None


class VendorTestApiUatRequestModel(BaseModel):
    """应用侧真实联调只接受单号码、通知、直接内容或已审核模板和显式幂等键。"""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "oneOf": [
                {"required": ["content"], "not": {"required": ["template_id"]}},
                {
                    "required": ["template_id", "template_params"],
                    "not": {"required": ["content"]},
                },
            ]
        },
    )

    category: Literal["notice"]
    mobiles: list[Annotated[str, Field(pattern=r"^1\d{10}$")]] = Field(
        min_length=1,
        max_length=1,
    )
    content: str | None = Field(default=None, max_length=500)
    template_id: int | None = None
    template_params: list[str] | None = None
    sign_name: str | None = Field(default=None, max_length=64)
    biz_id: str = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def content_or_template(self) -> VendorTestApiUatRequestModel:
        if (self.content is None) == (self.template_id is None):
            raise ValueError("content 与 template_id 必须且只能提供一个")
        return self


class BatchModel(BaseModel):
    batch_no: str
    category: Category
    channel: Literal["api", "web"]
    app_name: str | None
    creator: str | None
    dept: str
    content: str
    status: Literal[
        "pending_approval",
        "rejected",
        "scheduled",
        "queued",
        "sending",
        "completed",
        "cancelled",
        "balance_blocked",
        "expired",
    ]
    deferred_reason: str | None
    resend_of: str | None
    is_test: bool
    segments: int
    quota_cost: int
    total: int
    removed_freq_limit: int
    delivered: int
    failed: int
    unknown: int
    scheduled_at: datetime | None
    created_at: datetime


MessageStatus = Literal["pending", "sent", "delivered", "failed", "unknown", "other"]


class MessageDetailModel(BaseModel):
    id: int
    phone: str
    status: MessageStatus
    vendor_task_id: str | None
    report_desc: str | None
    report_time: datetime | None


class MessageDetailPage(BaseModel):
    total: int
    items: list[MessageDetailModel]


class RescheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scheduled_at: datetime


class ResendResponseModel(BaseModel):
    batch_no: str
    resend_of: str
    accepted: int
    status: Literal["queued", "scheduled", "pending_approval"]


async def _pipeline(app: ApiAppContext) -> SendPipeline:
    settings = get_settings()
    store = SqlPipelineStore(settings)
    config = await store.load_config(app.dept)
    policy = RuntimePolicy.from_mapping(config)
    redis: Any = redis_client(settings.redis_control_url)
    crypto = CryptoService.from_settings(settings)
    return SendPipeline(
        store=store,
        idempotency=IdempotencyCoordinator(redis, store),
        crypto=crypto,
        frequency=FrequencyLimiter(redis),
        quota=QuotaService(redis),
        publisher=CeleryQueuePublisher(),
        templates=SqlTemplateRenderer(settings),
        signs=SignResolver(SqlSignRepository(settings)),
        vendor_test_console_only=settings.vendor_live_test,
        acceptance_limiter=ApplicationRateLimiter(redis),
        usage_ledger=UsageLedgerService(redis, settings),
        config=PipelineConfig(
            unsubscribe_suffix=policy.unsubscribe_suffix,
            unsubscribe_auto_append=policy.unsubscribe_auto_append,
            verify_otp_mask=policy.verify_otp_mask,
            verify_per_minute=policy.verify_freq_per_minute,
            verify_per_day=policy.verify_freq_per_day,
            market_per_day=policy.market_freq_per_day,
            dept_daily_quota=int(config.get("dept_daily_quota", "0")),
            market_window=policy.market_send_window,
            sensitive_hit_action=policy.sensitive_hit_action,
            approval_threshold=policy.approval_threshold,
            market_approval_threshold=policy.market_approval_threshold,
            approval_expire_hours=policy.approval_expire_hours,
            test_send_max=policy.test_send_max,
        ),
    )


def _batch_queries() -> BatchQueryService:
    settings = get_settings()
    return BatchQueryService(settings, CryptoService.from_settings(settings))


def get_resend_service() -> ResendService:
    settings = get_settings()
    return ResendService(
        SqlResendRepository(settings),
        CryptoService.from_settings(settings),
    )


async def get_scheduling_service() -> SchedulingService:
    settings = get_settings()
    policy = await SqlRuntimePolicyLoader(settings).load()
    redis: Any = redis_client(settings.redis_control_url)
    return SchedulingService(
        SqlSchedulingRepository(settings, pooled=True),
        QuotaService(redis),
        CeleryQueuePublisher(),
        approval_expire_hours=policy.approval_expire_hours,
    )


async def _batch_scope(
    app: ApiAppContext | None,
    credentials: HTTPAuthorizationCredentials | None,
    *,
    write: bool = False,
) -> BatchAccessScope:
    if isinstance(app, ApiAppContext):
        return BatchAccessScope(app_id=app.app_id)
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise ApiError(401, "UNAUTHORIZED", "缺少有效认证凭据", None)
    claims = await get_auth_facade().verify(credentials.credentials)
    if write and claims.role not in {"operator", "admin"}:
        raise ApiError(403, "FORBIDDEN", "仅操作员或管理员可修改批次", None)
    if claims.role in {"approver", "admin"}:
        return BatchAccessScope(all_departments=True)
    return BatchAccessScope(dept=claims.dept)


def _error(error: Exception) -> ApiError:
    if isinstance(error, IdempotencyConflict):
        return ApiError(
            409,
            "IDEMPOTENCY_CONFLICT",
            str(error),
            None,
        )
    if isinstance(error, VendorTestConsoleOnly):
        return ApiError(
            403,
            "VENDOR_TEST_CONSOLE_ONLY",
            "受控真实联调只能从系统配置页发送",
            None,
        )
    if isinstance(error, VendorTestRecipientDenied):
        return ApiError(
            403,
            "FORBIDDEN",
            "真实联调仅允许已登记测试号码",
            {"denied_count": error.denied_count},
        )
    if isinstance(error, CategoryNotAllowed):
        return ApiError(403, "CATEGORY_NOT_ALLOWED", str(error), None)
    if isinstance(error, ConsentRequired):
        return ApiError(422, "CONSENT_REQUIRED", str(error), None)
    if isinstance(error, TemplateParamMismatch):
        return ApiError(422, error.code, str(error), error.detail)
    if isinstance(error, SensitiveWord):
        return ApiError(422, "SENSITIVE_WORD", str(error), None)
    if isinstance(error, AllFiltered):
        return ApiError(422, "ALL_FILTERED", str(error), None)
    if isinstance(error, QuotaExceeded):
        return ApiError(429, "QUOTA_EXCEEDED", str(error), None)
    if isinstance(error, ApplicationRateLimitExceeded):
        return ApiError(429, "RATE_LIMITED", str(error), None)
    if isinstance(error, UsageProjectionUnavailable):
        return ApiError(
            503,
            "USAGE_PROJECTION_UNAVAILABLE",
            "配额与频控投影尚未安全恢复，请稍后重试",
            None,
        )
    if isinstance(error, UsageReservationConflict):
        return ApiError(409, "STATE_CONFLICT", str(error), None)
    if isinstance(
        error,
        (FrequencyFenceLost, IdempotencyCoordinationTimeout, IdempotencyClaimLost),
    ):
        return ApiError(
            503,
            "USAGE_PROJECTION_UNAVAILABLE",
            "配额与频控投影尚未安全恢复，请稍后重试",
            None,
        )
    if isinstance(error, (InvalidContent, ValueError)):
        return ApiError(400, "INVALID_PARAM", str(error), None)
    return ApiError(500, "INTERNAL_ERROR", "服务内部错误", None)


@router.post(
    "/send",
    response_model=SendResponseModel,
    responses={
        400: ERROR_RESPONSE,
        401: ERROR_RESPONSE,
        403: ERROR_RESPONSE,
        409: ERROR_RESPONSE,
        422: ERROR_RESPONSE,
        429: ERROR_RESPONSE,
        503: ERROR_RESPONSE,
    },
)
@audited("message_send")
async def send_message(
    payload: SendRequestModel,
    app: Annotated[ApiAppContext, Depends(require_api_app)],
) -> BatchResponse:
    pipeline = await _pipeline(app)
    try:
        return await pipeline.accept(
            app,
            SendRequest(
                category=payload.category,
                mobiles=payload.mobiles,
                content=payload.content,
                template_id=payload.template_id,
                template_params=payload.template_params,
                sign_name=payload.sign_name,
                scheduled_at=payload.scheduled_at,
                biz_id=payload.biz_id,
                actor=ApplicationPrincipal(app.app_id, app.name, app.dept),
            ),
        )
    except (
        AllFiltered,
        CategoryNotAllowed,
        ConsentRequired,
        IdempotencyConflict,
        InvalidContent,
        ApplicationRateLimitExceeded,
        QuotaExceeded,
        SensitiveWord,
        TemplateParamMismatch,
        ValueError,
        VendorTestConsoleOnly,
        VendorTestRecipientDenied,
        UsageProjectionUnavailable,
        UsageReservationConflict,
        FrequencyFenceLost,
        IdempotencyCoordinationTimeout,
        IdempotencyClaimLost,
    ) as error:
        raise _error(error) from None


async def _require_vendor_test_api_ready() -> None:
    settings = get_settings()
    if not settings.vendor_live_test:
        raise ApiError(
            403,
            "VENDOR_TEST_MODE_REQUIRED",
            "受控 API UAT 仅在真实联调模式开放",
            None,
        )
    try:
        VendorControlStateGuard().require_fresh()
    except VendorControlStateUnavailable as error:
        await raise_vendor_control_unavailable(error)


async def _resolve_vendor_test_api_recipient(
    phone: str,
) -> VendorTestRecipientForSend:
    return await _vendor_test_api_recipient_service().resolve_phone_for_send(phone)


def _vendor_test_api_recipient_service() -> VendorTestRecipientService:
    settings = get_settings()
    return VendorTestRecipientService(
        SqlVendorTestRecipientRepository(settings),
        CryptoService.from_settings(settings),
    )


@router.post(
    "/uat-send",
    response_model=SendResponseModel,
    responses={
        400: ERROR_RESPONSE,
        401: ERROR_RESPONSE,
        403: ERROR_RESPONSE,
        409: ERROR_RESPONSE,
        422: ERROR_RESPONSE,
        429: ERROR_RESPONSE,
        503: ERROR_RESPONSE,
    },
)
@audited("message_send")
async def send_vendor_test_api_uat(
    payload: VendorTestApiUatRequestModel,
    app: Annotated[ApiAppContext, Depends(require_api_app)],
) -> BatchResponse:
    pipeline = await _pipeline(app)
    try:
        preauthorization: AcceptancePreauthorization = await pipeline.preauthorize(
            app,
            payload.category,
        )
    except (CategoryNotAllowed, ApplicationRateLimitExceeded, ValueError) as error:
        raise _error(error) from None
    await _require_vendor_test_api_ready()
    try:
        recipient = await _resolve_vendor_test_api_recipient(payload.mobiles[0])
    except RecipientNotFound:
        raise ApiError(
            403,
            "FORBIDDEN",
            "真实联调仅允许已登记测试号码",
            None,
        ) from None
    except RecipientHmacIndexStale:
        raise ApiError(
            409,
            "STATE_CONFLICT",
            "测试号码索引待刷新",
            None,
        ) from None
    try:
        return await pipeline.accept(
            app,
            SendRequest(
                category=payload.category,
                mobiles=(),
                content=payload.content,
                template_id=payload.template_id,
                template_params=payload.template_params,
                sign_name=payload.sign_name,
                biz_id=payload.biz_id,
                channel="api",
                actor=ApplicationPrincipal(app.app_id, app.name, app.dept),
                is_test=True,
                protected_mobiles=(
                    ProtectedPhone(
                        phone_enc=recipient.phone_enc,
                        phone_hmac=recipient.phone_hmac,
                        phone_mask=recipient.phone_mask,
                        key_version=recipient.key_version,
                    ),
                ),
                protected_hmac_candidates=recipient.hmac_candidates,
                vendor_test_uat=True,
            ),
            preauthorization=preauthorization,
        )
    except (
        AllFiltered,
        CategoryNotAllowed,
        ConsentRequired,
        InvalidContent,
        ApplicationRateLimitExceeded,
        QuotaExceeded,
        SensitiveWord,
        TemplateParamMismatch,
        ValueError,
        VendorTestConsoleOnly,
        VendorTestRecipientDenied,
        UsageProjectionUnavailable,
        UsageReservationConflict,
        FrequencyFenceLost,
        IdempotencyCoordinationTimeout,
        IdempotencyClaimLost,
    ) as error:
        raise _error(error) from None


@router.get(
    "/batches/{batch_no}",
    response_model=BatchModel,
    responses={404: ERROR_RESPONSE},
)
async def get_batch(
    batch_no: str,
    request: Request,
    auditor: Annotated[SensitiveReadAuditor, Depends(get_sensitive_read_auditor)],
    app: Annotated[ApiAppContext | None, Depends(optional_api_app)],
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Security(bearer_scheme),
    ],
) -> dict[str, object]:
    scope = await _batch_scope(app, credentials)
    try:
        result = await _batch_queries().get_batch(batch_no, scope)
    except BatchNotFound:
        raise ApiError(404, "NOT_FOUND", "批次不存在", None) from None
    await auditor.record(
        action="batch_content_read",
        object_type="batch",
        object_id=batch_no,
        ip=trusted_client_ip(request),
        count=1,
    )
    return result


@router.get(
    "/batches/{batch_no}/details",
    response_model=MessageDetailPage,
)
async def list_batch_details(
    batch_no: str,
    app: Annotated[ApiAppContext | None, Depends(optional_api_app)],
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Security(bearer_scheme),
    ],
    status: Annotated[MessageStatus | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> dict[str, object]:
    scope = await _batch_scope(app, credentials)
    return await _batch_queries().list_details(
        batch_no,
        scope,
        status=status,
        page=page,
        size=size,
    )


@router.post(
    "/batches/{batch_no}/cancel",
    response_class=Response,
    responses={401: ERROR_RESPONSE, 403: ERROR_RESPONSE, 409: ERROR_RESPONSE},
)
@audited("batch_cancel")
async def cancel_batch(
    batch_no: str,
    service: Annotated[SchedulingService, Depends(get_scheduling_service)],
    app: Annotated[ApiAppContext | None, Depends(optional_api_app)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer_scheme)],
) -> Response:
    scope = await _batch_scope(app, credentials, write=True)
    try:
        await service.cancel(batch_no, scope)
    except SchedulingConflict as error:
        raise ApiError(409, "STATE_CONFLICT", str(error), None) from None
    return Response(status_code=200)


@router.post(
    "/batches/{batch_no}/reschedule",
    response_class=Response,
    responses={
        400: ERROR_RESPONSE,
        401: ERROR_RESPONSE,
        403: ERROR_RESPONSE,
        409: ERROR_RESPONSE,
    },
)
@audited("batch_reschedule")
async def reschedule_batch(
    batch_no: str,
    payload: RescheduleRequest,
    service: Annotated[SchedulingService, Depends(get_scheduling_service)],
    app: Annotated[ApiAppContext | None, Depends(optional_api_app)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer_scheme)],
) -> Response:
    scope = await _batch_scope(app, credentials, write=True)
    try:
        await service.reschedule(batch_no, scope, payload.scheduled_at)
    except SchedulingConflict as error:
        raise ApiError(409, "STATE_CONFLICT", str(error), None) from None
    except ValueError as error:
        raise ApiError(400, "INVALID_PARAM", str(error), None) from None
    return Response(status_code=200)


@router.post(
    "/batches/{batch_no}/resend-failed",
    response_model=ResendResponseModel,
    responses={
        400: ERROR_RESPONSE,
        401: ERROR_RESPONSE,
        403: ERROR_RESPONSE,
        404: ERROR_RESPONSE,
        409: ERROR_RESPONSE,
        422: ERROR_RESPONSE,
        429: ERROR_RESPONSE,
    },
)
@audited("batch_resend_failed")
async def resend_failed_batch(
    batch_no: str,
    service: Annotated[ResendService, Depends(get_resend_service)],
    app: Annotated[ApiAppContext | None, Depends(optional_api_app)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer_scheme)],
) -> ResendResponseModel:
    actor: ActorPrincipal | None = None
    expected_channel = "api"
    if isinstance(app, ApiAppContext):
        scope = BatchAccessScope(app_id=app.app_id)
        pipeline_app = app
        actor = ApplicationPrincipal(app.app_id, app.name, app.dept)
    else:
        if credentials is None or credentials.scheme.casefold() != "bearer":
            raise ApiError(401, "UNAUTHORIZED", "缺少有效认证凭据", None)
        claims = await get_auth_facade().verify(credentials.credentials)
        if claims.role not in {"operator", "admin"}:
            raise ApiError(403, "FORBIDDEN", "仅操作员或管理员可重发", None)
        scope = (
            BatchAccessScope(all_departments=True)
            if claims.role == "admin"
            else BatchAccessScope(dept=claims.dept)
        )
        actor = claims.principal
        expected_channel = "web"
        pipeline_app = ApiAppContext(
            0,
            "web",
            claims.dept,
            frozenset({"notice", "market"}),
            daily_quota=0,
        )
    try:
        resend_request = await service.build_request(batch_no, scope, actor=actor)
        if resend_request.channel != expected_channel:
            raise ApiError(403, "FORBIDDEN", "认证方式与原批次渠道不匹配", None)
        if expected_channel == "web":
            pipeline_app = ApiAppContext(
                0,
                "web",
                resend_request.resend_dept or pipeline_app.dept,
                frozenset({"notice", "market"}),
                daily_quota=0,
            )
        pipeline = await _pipeline(pipeline_app)
        result = await pipeline.accept(pipeline_app, resend_request)
        return ResendResponseModel(
            batch_no=result.batch_no,
            resend_of=batch_no,
            accepted=result.accepted,
            status=cast(Literal["queued", "scheduled", "pending_approval"], result.status),
        )
    except BatchNotFound:
        raise ApiError(404, "NOT_FOUND", "批次不存在", None) from None
    except NoFailedRecipients as error:
        raise ApiError(409, "STATE_CONFLICT", str(error), None) from None
    except ApiError:
        raise
    except Exception as error:
        mapped = _error(error)
        if mapped.status_code == 500:
            raise
        raise mapped from None
