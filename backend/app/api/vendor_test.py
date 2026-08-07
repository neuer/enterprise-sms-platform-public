"""系统配置页中的真实运营商受控联调控制台 API。"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, Path, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from app.api.auth import ERROR_RESPONSE, bearer_scheme
from app.api.messages import _error as _send_error
from app.core.audit import audited
from app.core.auth.accounts import SecurityPrincipal
from app.core.auth.jwt import JwtClaims
from app.core.auth.runtime import AuthFacade, get_auth_facade
from app.core.client_ip import trusted_client_ip
from app.core.errors import (
    ApiError,
    api_error_handler,
    internal_error_handler,
)
from app.core.runtime_resources import redis_client
from app.services.billing_preview import BillingPreview
from app.services.crypto import CryptoService
from app.services.vendor_control_client import (
    ControlAgentUnavailable,
    VendorControlClient,
)
from app.services.vendor_control_state import (
    VendorControlStateGuard,
    VendorControlStateUnavailable,
)
from app.services.vendor_test_operation import (
    VendorTestOperation,
    VendorTestOperationConflict,
    VendorTestOperationPending,
    VendorTestOperationService,
)
from app.services.vendor_test_operation_repository import (
    SqlVendorTestOperationRepository,
)
from app.services.vendor_test_recipient import (
    DuplicateVendorTestRecipient,
    InvalidVendorTestRecipient,
    RecipientBusy,
    RecipientHmacIndexStale,
    RecipientNotFound,
    VendorTestRecipientRecord,
    VendorTestRecipientService,
    VendorTestRecipientSummary,
)
from app.services.vendor_test_recipient_repository import (
    SqlVendorTestRecipientRepository,
)
from app.services.vendor_test_reset import VendorTestResetFinalizer
from app.services.vendor_test_security_audit import (
    SqlVendorTestSecurityAuditRepository,
    VendorTestSecurityAudit,
)
from app.services.vendor_test_step_up import (
    InvalidStepUpOperation,
    StepUpExpired,
    VendorTestStepUpService,
)
from app.services.vendor_test_uat import (
    VendorTestAppUnavailable,
    VendorTestUatPreviewService,
    VendorTestUatService,
    build_vendor_test_uat_preview_service,
    build_vendor_test_uat_service,
)
from app.settings import get_settings

MAX_VENDOR_TEST_REQUEST_BYTES = 16_384


class VendorTestRoute(APIRoute):
    """按实际读取字节限制 body，并让成功与错误响应都不可缓存。"""

    def get_route_handler(self) -> Any:
        original = super().get_route_handler()

        async def limited(request: Request) -> Any:
            try:
                raw_length = request.headers.get("content-length")
                if raw_length is not None:
                    try:
                        length = int(raw_length)
                    except ValueError:
                        raise ApiError(
                            400,
                            "INVALID_PARAM",
                            "请求长度无效",
                            None,
                        ) from None
                    if length < 0 or length > MAX_VENDOR_TEST_REQUEST_BYTES:
                        raise ApiError(400, "INVALID_PARAM", "请求体过大", None)

                chunks: list[bytes] = []
                total = 0
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > MAX_VENDOR_TEST_REQUEST_BYTES:
                        raise ApiError(400, "INVALID_PARAM", "请求体过大", None)
                    chunks.append(chunk)
                request._body = b"".join(chunks)  # noqa: SLF001
                response = await original(request)
            except ApiError as error:
                response = await api_error_handler(request, error)
            except RequestValidationError:
                response = await api_error_handler(
                    request,
                    ApiError(400, "INVALID_PARAM", "请求参数不合法", None),
                )
            except Exception as error:
                response = await internal_error_handler(request, error)
            response.headers["Cache-Control"] = "no-store"
            return response

        return limited


router = APIRouter(
    prefix="/api/v1/web/admin/vendor-test",
    tags=["admin"],
    route_class=VendorTestRoute,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VendorTestStatusModel(StrictModel):
    mode: Literal["setup_required", "inactive", "controlled", "blocked"]
    heartbeat_at: datetime
    credential_configured: bool
    active_recipient_count: int = Field(ge=0)
    pause_kind: Literal["manual", "critical", "daily"] | None
    daily_limit: Literal[100]


class StepUpRequestModel(StrictModel):
    operation: Literal[
        "install_credentials",
        "rotate_credentials",
        "activate",
        "resume_critical",
        "reset_configuration",
    ]
    password: SecretStr = Field(min_length=1, max_length=128)


class StepUpResponseModel(StrictModel):
    token: str
    expires_in: Literal[300] = 300


class SealSessionRequestModel(StrictModel):
    operation: Literal["install_credentials", "rotate_credentials"]


class SealSessionResponseModel(StrictModel):
    session_id: str
    public_key: str
    expires_at: datetime
    aad: str


class CredentialEnvelopeModel(StrictModel):
    operation: Literal["install_credentials", "rotate_credentials"]
    step_up_token: SecretStr = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=128)
    wrapped_key: str = Field(min_length=1, max_length=4096)
    nonce: str = Field(min_length=1, max_length=128)
    ciphertext: str = Field(min_length=1, max_length=4096)
    aad: str = Field(min_length=1, max_length=1024)
    algorithm: Literal["RSA-OAEP-256+A256GCM"]


class VendorTestOperationModel(StrictModel):
    operation_id: str
    operation_type: Literal[
        "install_credentials",
        "rotate_credentials",
        "activate",
        "pause",
        "resume",
        "uat_send",
        "reset_configuration",
    ]
    status: Literal["requested", "running", "succeeded", "failed"]
    safe_code: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,63}$")
    vendor_code: int | None = Field(default=None, ge=1, le=99_999)
    batch_no: str | None = Field(default=None, max_length=64)
    checkpoint_id: str | None = Field(default=None, max_length=128)
    requested_at: datetime
    completed_at: datetime | None


class RecipientCreateModel(StrictModel):
    label: str = Field(min_length=1, max_length=64)
    phone: SecretStr = Field(json_schema_extra={"pattern": r"^1\d{10}$"})

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: SecretStr) -> SecretStr:
        if re.fullmatch(r"1\d{10}", value.get_secret_value()) is None:
            raise ValueError("测试手机号格式无效")
        return value


class RecipientRefreshModel(StrictModel):
    phone: SecretStr = Field(json_schema_extra={"pattern": r"^1\d{10}$"})

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: SecretStr) -> SecretStr:
        if re.fullmatch(r"1\d{10}", value.get_secret_value()) is None:
            raise ValueError("测试手机号格式无效")
        return value


class RecipientModel(StrictModel):
    id: int = Field(gt=0)
    label: str
    phone_mask: str
    status: Literal["active", "disabled"]
    created_at: datetime
    disabled_at: datetime | None


class ActivateRequestModel(StrictModel):
    step_up_token: SecretStr = Field(min_length=1, max_length=256)


class VendorTestResetRequestModel(StrictModel):
    step_up_token: SecretStr = Field(min_length=1, max_length=256)


class PauseRequestModel(StrictModel):
    pass


class ResumeRequestModel(StrictModel):
    step_up_token: SecretStr | None = Field(default=None, min_length=1, max_length=256)


class UatMessageRequestModel(StrictModel):
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

    recipient_id: int = Field(gt=0)
    app_id: int = Field(gt=0)
    category: Literal["verify", "notice", "market"]
    content: str | None = Field(default=None, min_length=1, max_length=500)
    template_id: int | None = Field(default=None, gt=0)
    template_params: list[Annotated[str, Field(max_length=500)]] | None = Field(
        default=None,
        max_length=20,
    )
    sign_name: str | None = Field(default=None, max_length=64)
    consent_confirmed: bool = False
    remark: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def content_or_template(self) -> UatMessageRequestModel:
        if (self.content is None) == (self.template_id is None):
            raise ValueError("content 与 template_id 必须且只能提供一个")
        if self.template_id is not None and self.template_params is None:
            raise ValueError("模板发送必须提供 template_params")
        return self


class UatPreviewRequestModel(StrictModel):
    model_config = UatMessageRequestModel.model_config

    app_id: int = Field(gt=0)
    category: Literal["verify", "notice", "market"]
    content: str | None = Field(default=None, min_length=1, max_length=500)
    template_id: int | None = Field(default=None, gt=0)
    template_params: list[Annotated[str, Field(max_length=500)]] | None = Field(
        default=None,
        max_length=20,
    )
    sign_name: str | None = Field(default=None, max_length=64)
    consent_confirmed: bool = False

    @model_validator(mode="after")
    def content_or_template(self) -> UatPreviewRequestModel:
        if (self.content is None) == (self.template_id is None):
            raise ValueError("content 与 template_id 必须且只能提供一个")
        if self.template_id is not None and self.template_params is None:
            raise ValueError("模板预览必须提供 template_params")
        return self


def get_vendor_control_state() -> VendorControlStateGuard:
    return VendorControlStateGuard()


def get_vendor_control_client() -> VendorControlClient:
    return VendorControlClient()


def get_vendor_security_audit() -> VendorTestSecurityAudit:
    return SqlVendorTestSecurityAuditRepository()


def get_vendor_operation_service() -> VendorTestOperationService:
    settings = get_settings()
    recipient_repository = SqlVendorTestRecipientRepository(settings)
    return VendorTestOperationService(
        SqlVendorTestOperationRepository(settings),
        VendorControlClient(),
        finalizers={
            "reset_configuration": VendorTestResetFinalizer(recipient_repository),
        },
    )


def get_vendor_recipient_service() -> VendorTestRecipientService:
    settings = get_settings()
    return VendorTestRecipientService(
        SqlVendorTestRecipientRepository(settings),
        CryptoService.from_settings(settings),
    )


def get_vendor_uat_service() -> VendorTestUatService:
    return build_vendor_test_uat_service()


def get_vendor_uat_preview_service() -> VendorTestUatPreviewService:
    return build_vendor_test_uat_preview_service()


def get_vendor_step_up_service(
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
) -> VendorTestStepUpService:
    settings = get_settings()
    return VendorTestStepUpService(facade, redis_client(settings.redis_auth_url))


async def _admin(
    request: Request,
    facade: AuthFacade,
    credentials: HTTPAuthorizationCredentials | None,
) -> tuple[JwtClaims, str]:
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise ApiError(401, "UNAUTHORIZED", "缺少有效的 Bearer 令牌", None)
    claims = await facade.verify(credentials.credentials)
    if claims.role != "admin":
        raise ApiError(403, "FORBIDDEN", "仅管理员可访问真实联调控制台", None)
    ip = trusted_client_ip(request)
    return claims, ip


def _operation_model(record: VendorTestOperation) -> VendorTestOperationModel:
    return VendorTestOperationModel.model_validate(record, from_attributes=True)


def _recipient_model(
    record: VendorTestRecipientRecord | VendorTestRecipientSummary,
) -> RecipientModel:
    return RecipientModel.model_validate(record, from_attributes=True)


async def _start_control_operation(
    *,
    background_tasks: BackgroundTasks,
    operations: VendorTestOperationService,
    operation_type: Literal[
        "activate",
        "pause",
        "resume",
        "reset_configuration",
    ],
    principal: SecurityPrincipal,
    body: dict[str, object],
) -> VendorTestOperationModel:
    operation_id = str(uuid4())
    values: dict[str, object] = {
        "operation_id": operation_id,
        "operation_type": operation_type,
        "principal": principal,
        "body": body,
    }
    try:
        record = await operations.start(**values)  # type: ignore[arg-type]
    except VendorTestOperationConflict:
        raise ApiError(
            409,
            "CONTROL_OPERATION_IN_PROGRESS",
            "已有真实联调控制操作正在执行",
            None,
        ) from None
    background_tasks.add_task(
        operations.execute_background,
        operation_id=record.operation_id,
        operation_type=operation_type,
        principal=principal,
        body=body,
    )
    return _operation_model(record)


@router.get(
    "/status",
    response_model=VendorTestStatusModel,
    responses={401: ERROR_RESPONSE, 403: ERROR_RESPONSE, 503: ERROR_RESPONSE},
)
async def status(
    request: Request,
    state: Annotated[VendorControlStateGuard, Depends(get_vendor_control_state)],
    recipients: Annotated[
        VendorTestRecipientService,
        Depends(get_vendor_recipient_service),
    ],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> VendorTestStatusModel:
    await _admin(request, facade, credentials)
    try:
        current = state.read_fresh()
    except VendorControlStateUnavailable:
        raise ApiError(
            503,
            "CONTROL_AGENT_UNAVAILABLE",
            "真实联调控制代理状态不可用",
            None,
        ) from None
    try:
        active_count = len(await recipients.list(include_disabled=False))
    except Exception:
        raise ApiError(503, "CONTROL_AGENT_UNAVAILABLE", "测试号码状态不可用", None) from None
    return VendorTestStatusModel(
        mode=current.mode,
        heartbeat_at=current.heartbeat_at,
        credential_configured=current.credential_configured,
        active_recipient_count=active_count,
        pause_kind=current.pause_kind,
        daily_limit=current.daily_limit,
    )


@router.post(
    "/step-up",
    response_model=StepUpResponseModel,
    responses={
        400: ERROR_RESPONSE,
        401: ERROR_RESPONSE,
        403: ERROR_RESPONSE,
        423: ERROR_RESPONSE,
    },
)
@audited("vendor_test_step_up")
async def step_up(
    payload: StepUpRequestModel,
    request: Request,
    service: Annotated[VendorTestStepUpService, Depends(get_vendor_step_up_service)],
    audit: Annotated[VendorTestSecurityAudit, Depends(get_vendor_security_audit)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> StepUpResponseModel:
    claims, ip = await _admin(request, facade, credentials)
    correlation_id = str(uuid4())
    try:
        token = await service.issue(
            claims=claims,
            password=payload.password.get_secret_value(),
            ip=ip,
            operation=payload.operation,
        )
    except InvalidStepUpOperation:
        await audit.record(
            correlation_id=correlation_id,
            principal=claims.principal,
            action="vendor_test_step_up",
            outcome="failed",
            safe_code="INVALID_PARAM",
        )
        raise ApiError(400, "INVALID_PARAM", "二次认证操作无效", None) from None
    except Exception:
        await audit.record(
            correlation_id=correlation_id,
            principal=claims.principal,
            action="vendor_test_step_up",
            outcome="failed",
            safe_code="STEP_UP_REJECTED",
        )
        raise
    await audit.record(
        correlation_id=correlation_id,
        principal=claims.principal,
        action="vendor_test_step_up",
        outcome="succeeded",
    )
    return StepUpResponseModel(token=token)


@router.post(
    "/seal-sessions",
    response_model=SealSessionResponseModel,
    responses={401: ERROR_RESPONSE, 403: ERROR_RESPONSE, 503: ERROR_RESPONSE},
)
@audited("vendor_test_seal_session")
async def create_seal_session(
    payload: SealSessionRequestModel,
    request: Request,
    client: Annotated[VendorControlClient, Depends(get_vendor_control_client)],
    audit: Annotated[VendorTestSecurityAudit, Depends(get_vendor_security_audit)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> SealSessionResponseModel:
    claims, _ = await _admin(request, facade, credentials)
    operation_id = str(uuid4())
    try:
        response = await client.request(
            "create_seal_session",
            operation_id=operation_id,
            body={
                "operation": payload.operation,
                "actor": claims.login_name,
            },
        )
    except ControlAgentUnavailable:
        await audit.record(
            correlation_id=operation_id,
            principal=claims.principal,
            action="vendor_test_seal_session",
            outcome="failed",
            safe_code="CONTROL_AGENT_UNAVAILABLE",
        )
        raise ApiError(
            503,
            "CONTROL_AGENT_UNAVAILABLE",
            "真实联调控制代理不可用",
            None,
        ) from None
    if response.status != "ok":
        await audit.record(
            correlation_id=operation_id,
            principal=claims.principal,
            action="vendor_test_seal_session",
            outcome="failed",
            safe_code="CONTROL_AGENT_UNAVAILABLE",
        )
        raise ApiError(503, "CONTROL_AGENT_UNAVAILABLE", "seal session 创建失败", None)
    try:
        result = SealSessionResponseModel.model_validate(response.body)
    except ValueError:
        await audit.record(
            correlation_id=operation_id,
            principal=claims.principal,
            action="vendor_test_seal_session",
            outcome="failed",
            safe_code="CONTROL_AGENT_UNAVAILABLE",
        )
        raise ApiError(503, "CONTROL_AGENT_UNAVAILABLE", "seal session 响应无效", None) from None
    await audit.record(
        correlation_id=operation_id,
        principal=claims.principal,
        action="vendor_test_seal_session",
        outcome="succeeded",
    )
    return result


@router.put(
    "/credentials",
    response_model=VendorTestOperationModel,
    responses={
        401: ERROR_RESPONSE,
        403: ERROR_RESPONSE,
        409: ERROR_RESPONSE,
        503: ERROR_RESPONSE,
    },
)
@audited("vendor_test_credentials")
async def install_credentials(
    payload: CredentialEnvelopeModel,
    request: Request,
    step_up_service: Annotated[
        VendorTestStepUpService,
        Depends(get_vendor_step_up_service),
    ],
    operations: Annotated[
        VendorTestOperationService,
        Depends(get_vendor_operation_service),
    ],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> VendorTestOperationModel:
    claims, ip = await _admin(request, facade, credentials)
    try:
        await step_up_service.consume(
            payload.step_up_token.get_secret_value(),
            claims,
            ip,
            payload.operation,
        )
    except StepUpExpired:
        raise ApiError(401, "STEP_UP_EXPIRED", "二次认证已过期或已使用", None) from None
    operation_id = str(uuid4())
    try:
        await operations.start(
            operation_id=operation_id,
            operation_type=payload.operation,
            principal=claims.principal,
            body={},
        )
        body = payload.model_dump(
            exclude={"operation", "step_up_token"},
            mode="json",
        )
        record = await operations.execute_reserved(
            operation_id=operation_id,
            operation_type=payload.operation,
            principal=claims.principal,
            body=body,
        )
    except VendorTestOperationConflict:
        raise ApiError(
            409,
            "CONTROL_OPERATION_IN_PROGRESS",
            "已有真实联调控制操作正在执行",
            None,
        ) from None
    except VendorTestOperationPending:
        pending_record = await operations.get(operation_id)
        if pending_record is None:
            raise ApiError(
                503,
                "CONTROL_AGENT_UNAVAILABLE",
                "真实联调控制操作等待对账",
                None,
            ) from None
        record = pending_record
    return _operation_model(record)


@router.get(
    "/recipients",
    response_model=list[RecipientModel],
    responses={401: ERROR_RESPONSE, 403: ERROR_RESPONSE},
)
async def list_recipients(
    request: Request,
    service: Annotated[
        VendorTestRecipientService,
        Depends(get_vendor_recipient_service),
    ],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> list[RecipientModel]:
    await _admin(request, facade, credentials)
    return [_recipient_model(item) for item in await service.list()]


@router.post(
    "/recipients",
    response_model=RecipientModel,
    responses={400: ERROR_RESPONSE, 401: ERROR_RESPONSE, 403: ERROR_RESPONSE, 409: ERROR_RESPONSE},
)
@audited("vendor_test_recipient_add")
async def add_recipient(
    payload: RecipientCreateModel,
    request: Request,
    service: Annotated[
        VendorTestRecipientService,
        Depends(get_vendor_recipient_service),
    ],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> RecipientModel:
    claims, _ = await _admin(request, facade, credentials)
    try:
        record = await service.add(
            label=payload.label,
            phone=payload.phone.get_secret_value(),
            actor=claims.login_name,
        )
    except InvalidVendorTestRecipient as error:
        raise ApiError(400, "INVALID_PARAM", str(error), None) from None
    except DuplicateVendorTestRecipient:
        raise ApiError(409, "STATE_CONFLICT", "测试号码已登记", None) from None
    except RecipientBusy:
        raise ApiError(
            409,
            "RECIPIENT_BUSY",
            "已有真实联调控制操作正在执行",
            None,
        ) from None
    return _recipient_model(record)


@router.post(
    "/recipients/{recipient_id}/refresh-index",
    response_model=RecipientModel,
    responses={
        400: ERROR_RESPONSE,
        401: ERROR_RESPONSE,
        403: ERROR_RESPONSE,
        404: ERROR_RESPONSE,
        409: ERROR_RESPONSE,
    },
)
@audited("vendor_test_recipient_refresh_index")
async def refresh_recipient_hmac_index(
    payload: RecipientRefreshModel,
    request: Request,
    recipient_id: Annotated[int, Path(gt=0)],
    service: Annotated[
        VendorTestRecipientService,
        Depends(get_vendor_recipient_service),
    ],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> RecipientModel:
    claims, _ = await _admin(request, facade, credentials)
    try:
        record = await service.refresh_hmac_index(
            recipient_id,
            phone=payload.phone.get_secret_value(),
            actor=claims.login_name,
        )
    except InvalidVendorTestRecipient:
        raise ApiError(400, "INVALID_PARAM", "输入号码与登记记录不匹配", None) from None
    except RecipientNotFound:
        raise ApiError(404, "NOT_FOUND", "测试号码不存在或已停用", None) from None
    except DuplicateVendorTestRecipient:
        raise ApiError(409, "STATE_CONFLICT", "测试号码已登记", None) from None
    return _recipient_model(record)


@router.delete(
    "/recipients/{recipient_id}",
    response_model=RecipientModel,
    responses={401: ERROR_RESPONSE, 403: ERROR_RESPONSE, 404: ERROR_RESPONSE, 409: ERROR_RESPONSE},
)
@audited("vendor_test_recipient_disable")
async def disable_recipient(
    request: Request,
    recipient_id: Annotated[int, Path(gt=0)],
    service: Annotated[
        VendorTestRecipientService,
        Depends(get_vendor_recipient_service),
    ],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> RecipientModel:
    claims, _ = await _admin(request, facade, credentials)
    try:
        record = await service.disable(recipient_id, actor=claims.login_name)
    except RecipientNotFound:
        raise ApiError(404, "NOT_FOUND", "测试号码不存在", None) from None
    except RecipientBusy:
        raise ApiError(409, "RECIPIENT_BUSY", "存在活动真实联调操作", None) from None
    return _recipient_model(record)


@router.post(
    "/activate",
    response_model=VendorTestOperationModel,
    status_code=202,
    responses={401: ERROR_RESPONSE, 403: ERROR_RESPONSE, 409: ERROR_RESPONSE, 503: ERROR_RESPONSE},
)
@audited("vendor_test_activate")
async def activate(
    payload: ActivateRequestModel,
    request: Request,
    background_tasks: BackgroundTasks,
    step_up_service: Annotated[
        VendorTestStepUpService,
        Depends(get_vendor_step_up_service),
    ],
    operations: Annotated[
        VendorTestOperationService,
        Depends(get_vendor_operation_service),
    ],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> VendorTestOperationModel:
    claims, ip = await _admin(request, facade, credentials)
    try:
        await step_up_service.consume(
            payload.step_up_token.get_secret_value(),
            claims,
            ip,
            "activate",
        )
    except StepUpExpired:
        raise ApiError(401, "STEP_UP_EXPIRED", "二次认证已过期或已使用", None) from None
    return await _start_control_operation(
        background_tasks=background_tasks,
        operations=operations,
        operation_type="activate",
        principal=claims.principal,
        body={},
    )


@router.post(
    "/reset",
    response_model=VendorTestOperationModel,
    status_code=202,
    responses={
        400: ERROR_RESPONSE,
        401: ERROR_RESPONSE,
        403: ERROR_RESPONSE,
        409: ERROR_RESPONSE,
        503: ERROR_RESPONSE,
    },
)
@audited("vendor_test_reset")
async def reset_configuration(
    payload: VendorTestResetRequestModel,
    request: Request,
    background_tasks: BackgroundTasks,
    state: Annotated[VendorControlStateGuard, Depends(get_vendor_control_state)],
    step_up_service: Annotated[
        VendorTestStepUpService,
        Depends(get_vendor_step_up_service),
    ],
    operations: Annotated[
        VendorTestOperationService,
        Depends(get_vendor_operation_service),
    ],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> VendorTestOperationModel:
    """消费当前身份的单次令牌后，仅从安全投影启动受控清理 operation。"""

    claims, ip = await _admin(request, facade, credentials)
    try:
        await step_up_service.consume(
            payload.step_up_token.get_secret_value(),
            claims,
            ip,
            "reset_configuration",
        )
    except StepUpExpired:
        raise ApiError(401, "STEP_UP_EXPIRED", "二次认证已过期或已使用", None) from None
    try:
        current = state.read_fresh()
    except VendorControlStateUnavailable:
        raise ApiError(
            503,
            "CONTROL_AGENT_UNAVAILABLE",
            "真实联调控制代理状态不可用",
            None,
        ) from None
    inactive_ready = (
        current.mode == "inactive"
        and current.credential_configured
        and current.pause_kind is None
    )
    if not inactive_ready:
        raise ApiError(409, "STATE_CONFLICT", "当前真实联调状态不可清空", None)
    return await _start_control_operation(
        background_tasks=background_tasks,
        operations=operations,
        operation_type="reset_configuration",
        principal=claims.principal,
        body={},
    )


@router.post(
    "/pause",
    response_model=VendorTestOperationModel,
    status_code=202,
    responses={401: ERROR_RESPONSE, 403: ERROR_RESPONSE, 409: ERROR_RESPONSE, 503: ERROR_RESPONSE},
)
@audited("vendor_test_pause")
async def pause(
    payload: PauseRequestModel,
    request: Request,
    background_tasks: BackgroundTasks,
    operations: Annotated[
        VendorTestOperationService,
        Depends(get_vendor_operation_service),
    ],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> VendorTestOperationModel:
    del payload
    claims, _ = await _admin(request, facade, credentials)
    return await _start_control_operation(
        background_tasks=background_tasks,
        operations=operations,
        operation_type="pause",
        principal=claims.principal,
        body={"pause_kind": "manual"},
    )


@router.post(
    "/resume",
    response_model=VendorTestOperationModel,
    status_code=202,
    responses={401: ERROR_RESPONSE, 403: ERROR_RESPONSE, 409: ERROR_RESPONSE, 503: ERROR_RESPONSE},
)
@audited("vendor_test_resume")
async def resume(
    payload: ResumeRequestModel,
    request: Request,
    background_tasks: BackgroundTasks,
    state: Annotated[VendorControlStateGuard, Depends(get_vendor_control_state)],
    step_up_service: Annotated[
        VendorTestStepUpService,
        Depends(get_vendor_step_up_service),
    ],
    operations: Annotated[
        VendorTestOperationService,
        Depends(get_vendor_operation_service),
    ],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> VendorTestOperationModel:
    claims, ip = await _admin(request, facade, credentials)
    try:
        pause_kind = state.read().pause_kind
    except VendorControlStateUnavailable:
        raise ApiError(503, "CONTROL_AGENT_UNAVAILABLE", "控制状态不可用", None) from None
    if pause_kind is None:
        raise ApiError(409, "STATE_CONFLICT", "当前环境未暂停", None)
    if pause_kind == "daily":
        raise ApiError(409, "STATE_CONFLICT", "每日额度暂停只能等待次日重置", None)
    if pause_kind == "critical":
        if payload.step_up_token is None:
            raise ApiError(401, "STEP_UP_REQUIRED", "恢复关键暂停需要二次认证", None)
        try:
            await step_up_service.consume(
                payload.step_up_token.get_secret_value(),
                claims,
                ip,
                "resume_critical",
            )
        except StepUpExpired:
            raise ApiError(401, "STEP_UP_EXPIRED", "二次认证已过期或已使用", None) from None
    elif payload.step_up_token is not None:
        raise ApiError(400, "INVALID_PARAM", "人工暂停恢复不接受二次认证令牌", None)
    return await _start_control_operation(
        background_tasks=background_tasks,
        operations=operations,
        operation_type="resume",
        principal=claims.principal,
        body={"pause_kind": pause_kind},
    )


@router.get(
    "/operations/{operation_id}",
    response_model=VendorTestOperationModel,
    responses={401: ERROR_RESPONSE, 403: ERROR_RESPONSE, 404: ERROR_RESPONSE},
)
async def get_operation(
    request: Request,
    operation_id: UUID,
    operations: Annotated[
        VendorTestOperationService,
        Depends(get_vendor_operation_service),
    ],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> VendorTestOperationModel:
    await _admin(request, facade, credentials)
    record = await operations.get(str(operation_id))
    if record is None:
        raise ApiError(404, "NOT_FOUND", "控制操作不存在", None)
    return _operation_model(record)


@router.post(
    "/messages/preview",
    response_model=BillingPreview,
    responses={
        400: ERROR_RESPONSE,
        401: ERROR_RESPONSE,
        403: ERROR_RESPONSE,
        404: ERROR_RESPONSE,
        422: ERROR_RESPONSE,
        503: ERROR_RESPONSE,
    },
)
async def preview_uat_message(
    payload: UatPreviewRequestModel,
    request: Request,
    state: Annotated[VendorControlStateGuard, Depends(get_vendor_control_state)],
    service: Annotated[
        VendorTestUatPreviewService,
        Depends(get_vendor_uat_preview_service),
    ],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> BillingPreview:
    await _admin(request, facade, credentials)
    try:
        state.require_fresh()
    except VendorControlStateUnavailable:
        raise ApiError(
            503,
            "CONTROL_AGENT_UNAVAILABLE",
            "真实联调控制代理状态不可用",
            None,
        ) from None
    try:
        return await service.preview(
            app_id=payload.app_id,
            category=payload.category,
            content=payload.content,
            template_id=payload.template_id,
            template_params=payload.template_params,
            sign_name=payload.sign_name,
            consent_confirmed=payload.consent_confirmed,
        )
    except VendorTestAppUnavailable:
        raise ApiError(404, "NOT_FOUND", "应用不存在、已停用或配置无效", None) from None
    except Exception as error:
        mapped = _send_error(error)
        if mapped.status_code == 500:
            raise
        raise mapped from None


@router.post(
    "/messages",
    response_model=VendorTestOperationModel,
    responses={
        400: ERROR_RESPONSE,
        401: ERROR_RESPONSE,
        403: ERROR_RESPONSE,
        404: ERROR_RESPONSE,
        409: ERROR_RESPONSE,
        422: ERROR_RESPONSE,
        429: ERROR_RESPONSE,
        503: ERROR_RESPONSE,
    },
)
@audited("vendor_test_uat_send")
async def send_uat_message(
    payload: UatMessageRequestModel,
    request: Request,
    state: Annotated[VendorControlStateGuard, Depends(get_vendor_control_state)],
    service: Annotated[VendorTestUatService, Depends(get_vendor_uat_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> VendorTestOperationModel:
    claims, _ = await _admin(request, facade, credentials)
    try:
        state.require_fresh()
    except VendorControlStateUnavailable:
        raise ApiError(
            503,
            "CONTROL_AGENT_UNAVAILABLE",
            "真实联调控制代理状态不可用",
            None,
        ) from None
    operation_id = str(uuid4())
    try:
        record = await service.send(
            operation_id=operation_id,
            recipient_id=payload.recipient_id,
            app_id=payload.app_id,
            category=payload.category,
            principal=claims.principal,
            content=payload.content,
            template_id=payload.template_id,
            template_params=payload.template_params,
            sign_name=payload.sign_name,
            consent_confirmed=payload.consent_confirmed,
            remark=payload.remark,
        )
    except VendorTestOperationPending:
        try:
            pending_record = await service.get(operation_id)
        except Exception:
            pending_record = None
        if (
            pending_record is None
            or pending_record.operation_type != "uat_send"
        ):
            raise ApiError(
                503,
                "CONTROL_AGENT_UNAVAILABLE",
                "UAT 操作等待安全对账",
                None,
            ) from None
        record = pending_record
    except RecipientBusy:
        raise ApiError(409, "RECIPIENT_BUSY", "已有真实 UAT 正在执行", None) from None
    except RecipientNotFound:
        raise ApiError(404, "NOT_FOUND", "测试号码不存在或已停用", None) from None
    except RecipientHmacIndexStale:
        raise ApiError(
            409,
            "STATE_CONFLICT",
            "测试号码索引待刷新",
            None,
        ) from None
    except VendorTestAppUnavailable:
        raise ApiError(404, "NOT_FOUND", "应用不存在、已停用或配置无效", None) from None
    except Exception as error:
        mapped = _send_error(error)
        if mapped.status_code == 500:
            raise
        raise mapped from None
    return _operation_model(record)


@router.get(
    "/messages/{operation_id}",
    response_model=VendorTestOperationModel,
    responses={401: ERROR_RESPONSE, 403: ERROR_RESPONSE, 404: ERROR_RESPONSE},
)
async def get_uat_message(
    request: Request,
    operation_id: UUID,
    service: Annotated[VendorTestUatService, Depends(get_vendor_uat_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> VendorTestOperationModel:
    await _admin(request, facade, credentials)
    record = await service.get(str(operation_id))
    if record is None or record.operation_type != "uat_send":
        raise ApiError(404, "NOT_FOUND", "UAT 操作不存在", None)
    return _operation_model(record)
