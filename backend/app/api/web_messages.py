"""Web 人工发送、服务端计费预览与安全号码导入。"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.api.auth import ERROR_RESPONSE, bearer_scheme
from app.api.messages import BatchModel, _error
from app.core.apikey import ApiAppContext
from app.core.audit import audited
from app.core.auth.jwt import JwtClaims
from app.core.auth.runtime import AuthFacade, get_auth_facade
from app.core.bounded_executor import ExecutorBackpressure, run_bounded
from app.core.client_ip import trusted_client_ip
from app.core.correlation import correlation_headers
from app.core.errors import ApiError
from app.core.runtime_resources import redis_client
from app.services.batch_query import BatchAccessScope, BatchQueryService
from app.services.billing_preview import BillingPreview, build_billing_preview
from app.services.blacklist import RedisBlacklistCache
from app.services.blacklist_repository import SqlBlacklistRepository
from app.services.crypto import CryptoService
from app.services.freq import FrequencyLimiter
from app.services.idempotency import IdempotencyCoordinator
from app.services.import_file import ImportFileCodec
from app.services.import_repository import (
    ImportReservation,
    ImportStateConflict,
    SqlImportRepository,
    StoredImport,
)
from app.services.imports import ImportFormatError, ImportLimits, ImportParser, ImportTooLarge
from app.services.operations_query import (
    OperationsQueryService,
    QueryNotFound,
    SqlOperationsQueryRepository,
)
from app.services.pipeline import (
    BatchResponse,
    ConsentRequired,
    PipelineConfig,
    SendPipeline,
    SendRequest,
)
from app.services.pipeline_repository import SqlPipelineStore, SqlTemplateRenderer
from app.services.queue import CeleryQueuePublisher
from app.services.quota import QuotaService
from app.services.runtime_policy import RuntimePolicy, SqlRuntimePolicyLoader
from app.services.sign import SignResolver
from app.services.sign_repository import SqlSignRepository
from app.services.template import TemplateParamMismatch
from app.services.usage_ledger import UsageLedgerService
from app.settings import get_settings
from app.tasks import celery_app

router = APIRouter(prefix="/api/v1/web", tags=["messages"])
LOGGER = logging.getLogger(__name__)
Category = Literal["notice", "market"]
Phone = Annotated[str, Field(pattern=r"^1\d{10}$")]


class WebContentModel(BaseModel):
    model_config = ConfigDict(
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
    content: str | None = Field(default=None, max_length=500)
    template_id: int | None = None
    template_params: list[str] | None = None
    sign_name: str | None = None
    consent_confirmed: bool = False

    @model_validator(mode="after")
    def content_or_template(self) -> WebContentModel:
        if (self.content is None) == (self.template_id is None):
            raise ValueError("content 与 template_id 必须且只能提供一个")
        return self


class BillingPreviewRequest(WebContentModel):
    accepted_count: int = Field(ge=0, le=50_000)


class WebSendRequest(WebContentModel):
    mobiles: list[Phone] | None = Field(default=None, min_length=1, max_length=50_000)
    import_id: UUID | None = None
    scheduled_at: datetime | None = None
    is_test: bool = False
    remark: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def recipient_source(self) -> WebSendRequest:
        if (self.mobiles is None) == (self.import_id is None):
            raise ValueError("mobiles 与 import_id 必须且只能提供一个")
        return self


class ImportResponse(BaseModel):
    import_id: UUID
    valid: int
    invalid: int
    duplicate: int
    blacklisted: int
    invalid_download_url: str | None
    expires_at: datetime
    status: Literal["pending", "processing", "ready", "failed"]
    error: str | None


class WebSendResponse(BaseModel):
    batch_no: str
    idempotent: bool
    accepted: int
    removed_duplicate: int
    removed_blacklist: int
    removed_freq_limit: int
    est_segments: int
    quota_cost: int
    status: Literal["queued", "scheduled", "pending_approval"]
    deferred_reason: str | None
    scheduled_at: datetime | None


class BatchPageModel(BaseModel):
    total: int
    items: list[BatchModel]


class MessageQueryModel(BaseModel):
    id: int
    phone: str
    status: str
    report_desc: str | None
    report_time: datetime | None
    created_at: datetime
    batch_no: str
    category: str
    content: str
    sender: str | None


class MessageQueryPageModel(BaseModel):
    total: int
    items: list[MessageQueryModel]


class PhoneBadgeModel(BaseModel):
    blacklisted: bool
    blacklist_source: str | None
    recv_30d: int


class TimelineEventModel(BaseModel):
    ts: datetime
    direction: Literal["out", "in"]
    category: str | None
    batch_no: str | None
    content: str
    status: str | None
    sender: str | None


class TimelineModel(BaseModel):
    badge: PhoneBadgeModel
    events: list[TimelineEventModel]
    truncated: bool


class DecryptedPhoneModel(BaseModel):
    phone: Phone


class _ImportBlacklist:
    def __init__(self, redis: Any) -> None:
        self.cache = RedisBlacklistCache(redis)
        self.repository = SqlBlacklistRepository()

    async def matches(self, candidates: set[str]) -> set[str]:
        return await self.cache.matches(candidates, self.repository.all_hmacs)


def get_import_repository() -> SqlImportRepository:
    return SqlImportRepository()


def get_pipeline_store() -> SqlPipelineStore:
    return SqlPipelineStore()


def get_batch_query_service() -> BatchQueryService:
    return BatchQueryService()


def get_operations_query_service() -> OperationsQueryService:
    settings = get_settings()
    return OperationsQueryService(
        SqlOperationsQueryRepository(settings),
        CryptoService.from_settings(settings),
    )


def get_template_renderer() -> SqlTemplateRenderer:
    return SqlTemplateRenderer()


def get_crypto_service() -> CryptoService:
    return CryptoService.from_settings(get_settings())


def get_sign_resolver() -> SignResolver:
    return SignResolver(SqlSignRepository())


async def get_import_parser() -> AsyncIterator[ImportParser]:
    settings = get_settings()
    policy = await SqlRuntimePolicyLoader(settings).load()
    yield ImportParser(
        CryptoService.from_settings(settings),
        _ImportBlacklist(redis_client(settings.redis_control_url)),
        limits=ImportLimits.from_policy(policy),
    )


async def _writer(
    facade: AuthFacade,
    credentials: HTTPAuthorizationCredentials | None,
) -> JwtClaims:
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise ApiError(401, "UNAUTHORIZED", "缺少有效的 Bearer 令牌", None)
    claims = await facade.verify(credentials.credentials)
    if claims.role not in {"operator", "admin"}:
        raise ApiError(403, "FORBIDDEN", "仅操作员或管理员可发送短信", None)
    return claims


async def _reader(
    facade: AuthFacade,
    credentials: HTTPAuthorizationCredentials | None,
) -> JwtClaims:
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise ApiError(401, "UNAUTHORIZED", "缺少有效的 Bearer 令牌", None)
    return await facade.verify(credentials.credentials)


def _query_scope(claims: JwtClaims, requested_dept: str | None = None) -> BatchAccessScope:
    if claims.role == "admin":
        return (
            BatchAccessScope(dept=requested_dept)
            if requested_dept is not None
            else BatchAccessScope(all_departments=True)
        )
    if requested_dept is not None and requested_dept != claims.dept:
        raise ApiError(403, "FORBIDDEN", "不能查询其他部门数据", None)
    return BatchAccessScope(dept=claims.dept)


def _config(values: dict[str, str]) -> PipelineConfig:
    policy = RuntimePolicy.from_mapping(values)
    return PipelineConfig(
        unsubscribe_suffix=policy.unsubscribe_suffix,
        unsubscribe_auto_append=policy.unsubscribe_auto_append,
        verify_otp_mask=policy.verify_otp_mask,
        verify_per_minute=policy.verify_freq_per_minute,
        verify_per_day=policy.verify_freq_per_day,
        market_per_day=policy.market_freq_per_day,
        dept_daily_quota=int(values.get("dept_daily_quota", "0")),
        market_window=policy.market_send_window,
        sensitive_hit_action=policy.sensitive_hit_action,
        approval_threshold=policy.approval_threshold,
        market_approval_threshold=policy.market_approval_threshold,
        approval_expire_hours=policy.approval_expire_hours,
        test_send_max=policy.test_send_max,
    )


@asynccontextmanager
async def _pipeline_for(claims: JwtClaims) -> AsyncIterator[SendPipeline]:
    settings = get_settings()
    store = SqlPipelineStore(settings)
    values = await store.load_config(claims.dept)
    redis: Any = redis_client(settings.redis_control_url)
    crypto = CryptoService.from_settings(settings)
    yield SendPipeline(
        store=store,
        idempotency=IdempotencyCoordinator(redis, store),
        crypto=crypto,
        frequency=FrequencyLimiter(redis),
        quota=QuotaService(redis),
        publisher=CeleryQueuePublisher(),
        templates=SqlTemplateRenderer(settings),
        signs=SignResolver(SqlSignRepository(settings)),
        vendor_test_console_only=settings.vendor_live_test,
        usage_ledger=UsageLedgerService(redis, settings),
        config=_config(values),
    )


async def _render(
    payload: WebContentModel,
    renderer: SqlTemplateRenderer,
) -> str:
    if payload.content is not None:
        return payload.content
    if payload.template_id is None:
        raise ValueError("模板编号不能为空")
    return await renderer.render(payload.template_id, payload.template_params or ())


def _import_response(stored: StoredImport) -> ImportResponse:
    return ImportResponse(
        import_id=UUID(stored.import_id),
        valid=stored.valid,
        invalid=stored.invalid,
        duplicate=stored.duplicate,
        blacklisted=stored.blacklisted,
        invalid_download_url=(
            f"/api/v1/web/messages/import/{stored.import_id}/invalid-file"
            if stored.invalid_file is not None
            else None
        ),
        expires_at=stored.expires_at,
        status=cast(
            Literal["pending", "processing", "ready", "failed"],
            stored.status,
        ),
        error=stored.error,
    )


def _upload_size(file: UploadFile) -> int:
    file.file.seek(0, 2)
    size = file.file.tell()
    file.file.seek(0)
    return size


def _enqueue_import(import_id: str) -> None:
    celery_app.send_task(
        "app.tasks.process_import",
        args=[import_id],
        queue="bulk",
        headers=correlation_headers(),
        ignore_result=True,
    )


@router.get(
    "/batches",
    response_model=BatchPageModel,
)
async def list_web_batches(
    service: Annotated[BatchQueryService, Depends(get_batch_query_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    category: Annotated[Literal["verify", "notice", "market"] | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    channel: Annotated[Literal["api", "web"] | None, Query()] = None,
    app_id: Annotated[int | None, Query(ge=1)] = None,
    dept: Annotated[str | None, Query(max_length=128)] = None,
    is_test: Annotated[bool | None, Query()] = None,
    batch_no: Annotated[str | None, Query(max_length=64)] = None,
    start: Annotated[datetime | None, Query()] = None,
    end: Annotated[datetime | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, object]:
    claims = await _reader(facade, credentials)
    try:
        return await service.list_batches(
            scope=_query_scope(claims, dept),
            category=category,
            status=status,
            channel=channel,
            app_id=app_id,
            is_test=is_test,
            batch_no=batch_no,
            start=start,
            end=end,
            page=page,
            size=size,
        )
    except ValueError as error:
        raise ApiError(400, "INVALID_PARAM", str(error), None) from None


@router.get(
    "/messages",
    response_model=MessageQueryPageModel,
)
async def search_web_messages(
    service: Annotated[OperationsQueryService, Depends(get_operations_query_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    phone: Annotated[str, Query(pattern=r"^1\d{10}$")],
    start: Annotated[datetime | None, Query()] = None,
    end: Annotated[datetime | None, Query()] = None,
    category: Annotated[Literal["verify", "notice", "market"] | None, Query()] = None,
    status: Annotated[
        Literal["pending", "sent", "delivered", "failed", "unknown", "other"] | None,
        Query(),
    ] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> MessageQueryPageModel:
    claims = await _reader(facade, credentials)
    try:
        result = await service.search_messages(
            phone=phone,
            start=start,
            end=end,
            category=category,
            status=status,
            page=page,
            size=size,
            scope=_query_scope(claims),
        )
    except ValueError as error:
        raise ApiError(400, "INVALID_PARAM", str(error), None) from None
    return MessageQueryPageModel(
        total=result.total,
        items=[
            MessageQueryModel(
                id=item.id,
                phone=item.phone_mask,
                status=item.status,
                report_desc=item.report_desc,
                report_time=item.report_time,
                created_at=item.created_at,
                batch_no=item.batch_no,
                category=item.category,
                content=item.content,
                sender=item.sender,
            )
            for item in result.items
        ],
    )


@router.get(
    "/messages/timeline",
    response_model=TimelineModel,
)
async def message_timeline(
    service: Annotated[OperationsQueryService, Depends(get_operations_query_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    phone: Annotated[str, Query(pattern=r"^1\d{10}$")],
    start: Annotated[datetime | None, Query()] = None,
    end: Annotated[datetime | None, Query()] = None,
) -> TimelineModel:
    claims = await _reader(facade, credentials)
    try:
        result = await service.timeline(
            phone=phone,
            start=start,
            end=end,
            scope=_query_scope(claims),
        )
    except ValueError as error:
        raise ApiError(400, "INVALID_PARAM", str(error), None) from None
    return TimelineModel(
        badge=PhoneBadgeModel(
            blacklisted=result.badge.blacklisted,
            blacklist_source=result.badge.blacklist_source,
            recv_30d=result.badge.recv_30d,
        ),
        events=[
            TimelineEventModel(
                ts=item.ts,
                direction=cast(Literal["out", "in"], item.direction),
                category=item.category,
                batch_no=item.batch_no,
                content=item.content,
                status=item.status,
                sender=item.sender,
            )
            for item in result.events
        ],
        truncated=result.truncated,
    )


@router.post(
    "/messages/{id}/phone/decrypt",
    response_model=DecryptedPhoneModel,
    responses={403: ERROR_RESPONSE, 404: ERROR_RESPONSE},
)
@audited("message_phone_decrypt")
async def decrypt_message_phone(
    id: int,
    request: Request,
    response: Response,
    service: Annotated[OperationsQueryService, Depends(get_operations_query_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> DecryptedPhoneModel:
    claims = await _reader(facade, credentials)
    if claims.role not in {"approver", "admin"}:
        raise ApiError(403, "FORBIDDEN", "当前角色无手机号解密权限", None)
    try:
        phone = await service.decrypt_phone(
            id,
            scope=_query_scope(claims),
            principal=claims.principal,
            ip=trusted_client_ip(request),
        )
    except QueryNotFound as error:
        raise ApiError(404, "NOT_FOUND", str(error), None) from None
    response.headers["Cache-Control"] = "no-store"
    return DecryptedPhoneModel(phone=phone)


@router.post(
    "/messages/import",
    response_model=ImportResponse,
    status_code=202,
    responses={
        400: ERROR_RESPONSE,
        401: ERROR_RESPONSE,
        403: ERROR_RESPONSE,
        429: ERROR_RESPONSE,
        503: ERROR_RESPONSE,
    },
)
@audited("message_import")
async def import_messages(
    file: Annotated[UploadFile, File()],
    parser: Annotated[ImportParser, Depends(get_import_parser)],
    repository: Annotated[SqlImportRepository, Depends(get_import_repository)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> ImportResponse:
    claims = await _writer(facade, credentials)
    filename = Path(file.filename or "").name
    stored: StoredImport | None = None
    staged_file: str | None = None
    cleanup_staged = False
    try:
        size = file.size
        if size is None:
            size = await run_bounded(_upload_size, file, timeout_s=2)
        await run_bounded(
            parser.preflight,
            filename,
            file.file,
            size=size,
            timeout_s=3,
        )
        stored = await repository.register(
            principal=claims.principal,
            filename=filename,
            source_size=size,
            expire_hours=parser.limits.expire_hours,
        )
        settings = get_settings()
        codec = ImportFileCodec(
            CryptoService.from_settings(settings),
            settings.import_storage_dir,
        )
        staged_file = await run_bounded(
            codec.stage,
            UUID(stored.import_id),
            file.file,
            size=size,
            max_bytes=parser.limits.max_bytes,
            timeout_s=15,
        )
        if staged_file != stored.source_file:
            raise RuntimeError("导入密文文件名与登记事实不一致")
        await repository.attach_source(UUID(stored.import_id), staged_file)
        try:
            await run_bounded(_enqueue_import, stored.import_id, timeout_s=3)
        except Exception as error:
            LOGGER.warning(
                "import direct dispatch unavailable; recovery dispatcher will retry",
                extra={"error_type": type(error).__name__},
            )
        return _import_response(
            StoredImport(
                stored.import_id,
                stored.valid,
                stored.invalid,
                stored.duplicate,
                stored.blacklisted,
                stored.invalid_file,
                stored.expires_at,
                status="pending",
            )
        )
    except ExecutorBackpressure as error:
        if stored is not None:
            cleanup_staged = True
            await repository.fail_registration(
                UUID(stored.import_id),
                "IMPORT_STAGE_FAILED",
            )
        raise ApiError(429, "RATE_LIMITED", str(error), None) from None
    except TimeoutError:
        if stored is not None:
            cleanup_staged = True
            await repository.fail_registration(
                UUID(stored.import_id),
                "IMPORT_STAGE_FAILED",
            )
        raise ApiError(503, "IMPORT_UNAVAILABLE", "导入登记超时，请重试", None) from None
    except (ImportFormatError, ImportTooLarge, ValueError) as error:
        if stored is not None:
            cleanup_staged = True
            await repository.fail_registration(
                UUID(stored.import_id),
                "IMPORT_STAGE_FAILED",
            )
        raise ApiError(400, "INVALID_PARAM", str(error), None) from None
    except Exception:
        if stored is not None:
            cleanup_staged = True
            await repository.fail_registration(
                UUID(stored.import_id),
                "IMPORT_STAGE_FAILED",
            )
        raise
    finally:
        if cleanup_staged and staged_file is not None:
            settings = get_settings()
            codec = ImportFileCodec(
                CryptoService.from_settings(settings),
                settings.import_storage_dir,
            )
            with suppress(Exception):
                await run_bounded(codec.remove, staged_file, timeout_s=3)
        await file.close()


@router.get(
    "/messages/import/{import_id}",
    response_model=ImportResponse,
    responses={401: ERROR_RESPONSE, 403: ERROR_RESPONSE, 404: ERROR_RESPONSE},
)
async def import_status(
    import_id: UUID,
    repository: Annotated[SqlImportRepository, Depends(get_import_repository)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> ImportResponse:
    claims = await _writer(facade, credentials)
    stored = await repository.get_status(
        str(import_id),
        principal=claims.principal,
    )
    if stored is None:
        raise ApiError(404, "NOT_FOUND", "导入任务不存在或已过期", None)
    return _import_response(stored)


@router.get(
    "/messages/import/{import_id}/invalid-file",
    response_class=FileResponse,
    responses={401: ERROR_RESPONSE, 403: ERROR_RESPONSE, 404: ERROR_RESPONSE},
)
async def download_invalid_file(
    import_id: UUID,
    repository: Annotated[SqlImportRepository, Depends(get_import_repository)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> FileResponse:
    claims = await _writer(facade, credentials)
    path = await repository.invalid_file(
        str(import_id),
        principal=claims.principal,
    )
    if path is None:
        raise ApiError(404, "NOT_FOUND", "剔除清单不存在或已过期", None)
    return FileResponse(path, media_type="text/csv", filename="removed-phones.csv")


@router.post(
    "/billing/preview",
    response_model=BillingPreview,
    responses={401: ERROR_RESPONSE, 403: ERROR_RESPONSE, 422: ERROR_RESPONSE},
)
async def billing_preview(
    payload: BillingPreviewRequest,
    store: Annotated[SqlPipelineStore, Depends(get_pipeline_store)],
    renderer: Annotated[SqlTemplateRenderer, Depends(get_template_renderer)],
    signs: Annotated[SignResolver, Depends(get_sign_resolver)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> BillingPreview:
    claims = await _writer(facade, credentials)
    try:
        content = await _render(payload, renderer)
        values = await store.load_config(claims.dept)
        policy = RuntimePolicy.from_mapping(values)
        sign_name = await signs.resolve(payload.sign_name) if payload.sign_name else None
        return build_billing_preview(
            category=payload.category,
            content=content,
            sign_name=sign_name,
            accepted_count=payload.accepted_count,
            consent_confirmed=payload.consent_confirmed,
            unsubscribe_suffix=policy.unsubscribe_suffix,
            unsubscribe_auto_append=policy.unsubscribe_auto_append,
            verify_otp_mask=policy.verify_otp_mask,
            notice_threshold=policy.approval_threshold,
            market_threshold=policy.market_approval_threshold,
        )
    except (ConsentRequired, TemplateParamMismatch, ValueError) as error:
        raise _error(error) from None


@router.post(
    "/messages/send",
    response_model=WebSendResponse,
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
async def send_web_message(
    payload: WebSendRequest,
    repository: Annotated[SqlImportRepository, Depends(get_import_repository)],
    crypto: Annotated[CryptoService, Depends(get_crypto_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> BatchResponse:
    claims = await _writer(facade, credentials)
    reservation: ImportReservation | None = None
    release_reservation = True
    try:
        app = ApiAppContext(
            0,
            "web",
            claims.dept,
            frozenset({"notice", "market"}),
            daily_quota=0,
        )
        async with _pipeline_for(claims) as pipeline:
            mobiles: Sequence[str]
            if payload.import_id is not None:
                reservation = await repository.reserve(
                    str(payload.import_id),
                    principal=claims.principal,
                )
                if reservation.consumed_batch_no is not None:
                    return await pipeline.response_for(reservation.consumed_batch_no)
                mobiles = [
                    crypto.decrypt_phone(
                        item.phone_enc,
                        item.key_version,
                        item.phone_hmac,
                        table="import_phone",
                    )
                    for item in reservation.phones
                ]
            else:
                mobiles = payload.mobiles or ()
            response = await pipeline.accept(
                app,
                SendRequest(
                    category=payload.category,
                    mobiles=mobiles,
                    content=payload.content,
                    template_id=payload.template_id,
                    template_params=payload.template_params,
                    sign_name=payload.sign_name,
                    scheduled_at=payload.scheduled_at,
                    channel="web",
                    consent_confirmed=payload.consent_confirmed,
                    actor=claims.principal,
                    is_test=payload.is_test,
                    remark=payload.remark,
                    import_reservation_id=(
                        reservation.reservation_id if reservation is not None else None
                    ),
                ),
            )
            release_reservation = False
            return response
    except ImportStateConflict as error:
        raise ApiError(409, "STATE_CONFLICT", str(error), None) from None
    except Exception as error:
        mapped = _error(error)
        if mapped.status_code == 500:
            raise
        raise mapped from None
    finally:
        if (
            release_reservation
            and reservation is not None
            and reservation.consumed_batch_no is None
        ):
            try:
                await repository.release(
                    reservation.reservation_id,
                    principal=claims.principal,
                )
            except Exception as error:
                LOGGER.error(
                    "import reservation release unavailable",
                    extra={
                        "account_id": claims.account_id,
                        "error_type": type(error).__name__,
                    },
                )
