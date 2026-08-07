"""管理员统一运维中心的安全查询与受控动作接口。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

from app.api.auth import ERROR_RESPONSE, bearer_scheme
from app.api.reports import ExportTaskModel, _response, get_export_service
from app.core.audit import audited
from app.core.auth.jwt import JwtClaims
from app.core.auth.runtime import AuthFacade, get_auth_facade
from app.core.errors import ApiError
from app.core.jobtrack import JOB_SPECS
from app.services.crypto import CryptoService
from app.services.export import (
    ExportForbidden,
    ExportRequestFilters,
    ExportService,
    ExportTooLarge,
)
from app.services.ops import (
    AlertQuery,
    JobNotFound,
    JobOpsService,
    JobRoute,
    OpsPage,
    OpsService,
    QueueRecoveryService,
    QueueResumeConflict,
    QueueResumeResult,
    QueueSnapshot,
    RawLogQuery,
    validate_range,
)
from app.services.ops_dispatch import OutboxBatchSender, OutboxJobSender
from app.services.ops_repository import SqlOpsRepository
from app.services.outbox import OutboxEventPage
from app.services.outbox_repository import SqlOutboxRepository
from app.services.raw_replay import (
    RawIntegrityConflict,
    RawReplayConflict,
    RawReplayNotFound,
    RawReplayService,
)
from app.services.reply_ingest import ReplyIngestService
from app.services.reply_repository import SqlReplyRepository
from app.services.report_ingest import ReportIngestService
from app.services.report_repository import SqlReportRepository
from app.settings import get_settings
from app.tasks import register_task_modules
from app.tasks.scheduler import build_beat_schedule

router = APIRouter(prefix="/api/v1/web/admin", tags=["admin"])


class PageModel(BaseModel):
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)


class AlertModel(BaseModel):
    id: int
    alert_type: str
    level: Literal["info", "warn", "crit"]
    title: str
    detail: dict[str, Any] | None
    channels: str
    created_at: datetime


class AlertPageModel(PageModel):
    items: list[AlertModel]


class RawLogModel(BaseModel):
    id: int
    source: Literal["report", "reply"]
    item_count: int
    custom_id_count: int
    processed: bool
    error: str | None
    fetched_at: datetime


class RawLogPageModel(PageModel):
    items: list[RawLogModel]


class UncertainModel(BaseModel):
    chunk_id: int
    batch_no: str
    custom_id: str
    phone_count: int
    vendor_code: int | None
    uncertain_since: datetime
    age_seconds: int


class UncertainPageModel(PageModel):
    items: list[UncertainModel]


class UnmatchedModel(BaseModel):
    id: int
    vendor_task_id: str | None
    custom_id: str | None
    phone_mask: str
    report_status: int | None
    report_desc: str | None
    report_time: datetime | None
    created_at: datetime


class UnmatchedPageModel(PageModel):
    items: list[UnmatchedModel]


class JobModel(BaseModel):
    job_name: str
    last_run_at: datetime | None
    last_status: Literal["running", "success", "failed"] | None
    last_duration_ms: int | None
    last_items: int
    success_rate_24h: float
    stalled: bool


class QueueStatusModel(BaseModel):
    realtime_code: str | None
    bulk_code: str | None
    balance: int | None
    threshold: int


class QueueResumeModel(BaseModel):
    resumed_batches: int
    paused_codes: list[str]


class ReplayResultModel(BaseModel):
    processed_items: int


class OutboxStatsModel(BaseModel):
    pending: int = Field(ge=0)
    published: int = Field(ge=0)
    processing: int = Field(ge=0)
    dead: int = Field(ge=0)
    failed_attempts: int = Field(ge=0)
    oldest_age_seconds: int = Field(ge=0)


class OutboxEventModel(BaseModel):
    id: UUID
    event_type: str
    aggregate_type: str
    aggregate_id: str
    task_name: str
    queue: str
    state: Literal["pending", "leased", "published", "processing", "completed", "dead"]
    attempts: int = Field(ge=0)
    max_attempts: int = Field(ge=1, le=100)
    failure_count: int = Field(ge=0)
    last_error: str | None
    next_attempt_at: datetime
    created_at: datetime
    updated_at: datetime


class OutboxEventPageModel(PageModel):
    items: list[OutboxEventModel]


class UnmatchedExportModel(BaseModel):
    phone: str | None = Field(default=None, pattern=r"^1\d{10}$")
    start: datetime | None = None
    end: datetime | None = None
    decrypted: bool = False


def get_ops_repository() -> SqlOpsRepository:
    return SqlOpsRepository()


def get_outbox_repository() -> SqlOutboxRepository:
    return SqlOutboxRepository(pooled=True)


def get_ops_service(
    repository: Annotated[SqlOpsRepository, Depends(get_ops_repository)],
) -> OpsService:
    return OpsService(repository, CryptoService.from_settings(get_settings()))


def _job_routes() -> dict[str, JobRoute]:
    routes: dict[str, JobRoute] = {}
    for value in build_beat_schedule({}).values():
        task_name = str(value["task"])
        routes[task_name.rsplit(".", 1)[-1]] = JobRoute(
            task_name,
            str(value["options"]["queue"]),
        )
    return routes


def get_job_ops_service(
    repository: Annotated[SqlOpsRepository, Depends(get_ops_repository)],
) -> JobOpsService:
    register_task_modules()
    return JobOpsService(
        repository,
        OutboxJobSender(),
        JOB_SPECS,
        _job_routes(),
        clock=lambda: datetime.now(UTC),
    )


def get_queue_recovery_service(
    repository: Annotated[SqlOpsRepository, Depends(get_ops_repository)],
) -> QueueRecoveryService:
    return QueueRecoveryService(repository, OutboxBatchSender())


def get_raw_replay_service(
    repository: Annotated[SqlOpsRepository, Depends(get_ops_repository)],
) -> RawReplayService:
    settings = get_settings()
    crypto = CryptoService.from_settings(settings)
    return RawReplayService(
        repository,
        crypto,
        ReportIngestService(None, SqlReportRepository(settings), crypto),
        ReplyIngestService(None, SqlReplyRepository(settings), crypto),
    )


async def _admin(
    facade: AuthFacade,
    credentials: HTTPAuthorizationCredentials | None,
) -> str:
    return (await _admin_claims(facade, credentials)).login_name


async def _admin_claims(
    facade: AuthFacade,
    credentials: HTTPAuthorizationCredentials | None,
) -> JwtClaims:
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise ApiError(401, "UNAUTHORIZED", "缺少有效的 Bearer 令牌", None)
    claims = await facade.verify(credentials.credentials)
    if claims.role != "admin":
        raise ApiError(403, "FORBIDDEN", "仅管理员可访问运维中心", None)
    return claims


def _ip(request: Request) -> str:
    return request.client.host if request.client is not None else "0.0.0.0"


def _page(
    value: OpsPage[Any] | OutboxEventPage,
    model: type[BaseModel],
) -> dict[str, object]:
    return {
        "items": [model.model_validate(item, from_attributes=True) for item in value.items],
        "total": value.total,
        "page": value.page,
        "page_size": value.page_size,
    }


@router.get(
    "/outbox",
    response_model=OutboxStatsModel,
    responses={401: ERROR_RESPONSE, 403: ERROR_RESPONSE},
)
async def outbox_status(
    repository: Annotated[SqlOutboxRepository, Depends(get_outbox_repository)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> OutboxStatsModel:
    await _admin(facade, credentials)
    return OutboxStatsModel.model_validate(
        await repository.stats(),
        from_attributes=True,
    )


@router.get(
    "/outbox/events",
    response_model=OutboxEventPageModel,
    responses={401: ERROR_RESPONSE, 403: ERROR_RESPONSE},
)
async def list_outbox_events(
    repository: Annotated[SqlOutboxRepository, Depends(get_outbox_repository)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    state: Literal[
        "pending", "leased", "published", "processing", "completed", "dead"
    ]
    | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, object]:
    await _admin(facade, credentials)
    return _page(
        await repository.list_events(state, page, page_size),
        OutboxEventModel,
    )


@router.post(
    "/outbox/{event_id}/retry",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        401: ERROR_RESPONSE,
        403: ERROR_RESPONSE,
        409: ERROR_RESPONSE,
    },
)
@audited("outbox_retry")
async def retry_outbox_event(
    event_id: UUID,
    repository: Annotated[SqlOutboxRepository, Depends(get_outbox_repository)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> Response:
    claims = await _admin_claims(facade, credentials)
    if not await repository.retry_dead(event_id, principal=claims.principal):
        raise ApiError(409, "STATE_CONFLICT", "仅 dead 事件允许人工重推", None)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/alerts",
    response_model=AlertPageModel,
    responses={400: ERROR_RESPONSE, 401: ERROR_RESPONSE, 403: ERROR_RESPONSE},
)
async def list_alerts(
    repository: Annotated[SqlOpsRepository, Depends(get_ops_repository)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    alert_type: str | None = None,
    level: Literal["info", "warn", "crit"] | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, object]:
    await _admin(facade, credentials)
    try:
        validate_range(start, end)
    except ValueError as error:
        raise ApiError(400, "INVALID_PARAM", str(error), None) from None
    return _page(
        await repository.list_alerts(AlertQuery(alert_type, level, start, end, page, page_size)),
        AlertModel,
    )


@router.get(
    "/raw-logs",
    response_model=RawLogPageModel,
    responses={401: ERROR_RESPONSE, 403: ERROR_RESPONSE},
)
async def list_raw_logs(
    repository: Annotated[SqlOpsRepository, Depends(get_ops_repository)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    source: Literal["report", "reply"] | None = None,
    processed: bool | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, object]:
    await _admin(facade, credentials)
    return _page(
        await repository.list_raw_logs(RawLogQuery(source, processed, page, page_size)),
        RawLogModel,
    )


@router.get(
    "/chunks/uncertain",
    response_model=UncertainPageModel,
    responses={401: ERROR_RESPONSE, 403: ERROR_RESPONSE},
)
async def list_uncertain(
    repository: Annotated[SqlOpsRepository, Depends(get_ops_repository)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, object]:
    await _admin(facade, credentials)
    return _page(await repository.list_uncertain(page, page_size), UncertainModel)


@router.get(
    "/unmatched-reports",
    response_model=UnmatchedPageModel,
    responses={400: ERROR_RESPONSE, 401: ERROR_RESPONSE, 403: ERROR_RESPONSE},
)
async def list_unmatched(
    service: Annotated[OpsService, Depends(get_ops_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    phone: Annotated[str | None, Query(pattern=r"^1\d{10}$")] = None,
    start: datetime | None = None,
    end: datetime | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, object]:
    await _admin(facade, credentials)
    try:
        value = await service.list_unmatched(phone, start, end, page, page_size)
    except ValueError as error:
        raise ApiError(400, "INVALID_PARAM", str(error), None) from None
    return _page(value, UnmatchedModel)


@router.get(
    "/jobs",
    response_model=list[JobModel],
    responses={401: ERROR_RESPONSE, 403: ERROR_RESPONSE},
)
async def list_jobs(
    service: Annotated[JobOpsService, Depends(get_job_ops_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> list[JobModel]:
    await _admin(facade, credentials)
    return [JobModel.model_validate(item, from_attributes=True) for item in await service.list()]


@router.post(
    "/jobs/{job_name}/trigger",
    status_code=status.HTTP_202_ACCEPTED,
    response_class=Response,
    responses={401: ERROR_RESPONSE, 403: ERROR_RESPONSE, 404: ERROR_RESPONSE},
)
@audited("job_trigger")
async def trigger_job(
    job_name: str,
    request: Request,
    service: Annotated[JobOpsService, Depends(get_job_ops_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> Response:
    actor = await _admin(facade, credentials)
    try:
        await service.trigger(job_name, actor=actor, ip=_ip(request))
    except JobNotFound:
        raise ApiError(404, "NOT_FOUND", "任务不存在或不可手动触发", None) from None
    return Response(status_code=202)


@router.get(
    "/queue/status",
    response_model=QueueStatusModel,
    responses={401: ERROR_RESPONSE, 403: ERROR_RESPONSE},
)
async def queue_status(
    service: Annotated[QueueRecoveryService, Depends(get_queue_recovery_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> QueueSnapshot:
    await _admin(facade, credentials)
    return await service.status()


@router.post(
    "/queue/resume",
    response_model=QueueResumeModel,
    responses={401: ERROR_RESPONSE, 403: ERROR_RESPONSE, 409: ERROR_RESPONSE},
)
@audited("queue_resume")
async def resume_queue(
    request: Request,
    service: Annotated[QueueRecoveryService, Depends(get_queue_recovery_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    force: bool = False,
) -> QueueResumeResult:
    actor = await _admin(facade, credentials)
    try:
        return await service.resume(force=force, actor=actor, ip=_ip(request))
    except QueueResumeConflict as error:
        raise ApiError(409, "STATE_CONFLICT", str(error), None) from None


@router.post(
    "/raw-logs/{id}/replay",
    response_model=ReplayResultModel,
    responses={401: ERROR_RESPONSE, 403: ERROR_RESPONSE, 404: ERROR_RESPONSE, 409: ERROR_RESPONSE},
)
@audited("raw_replay")
async def replay_raw(
    id: int,
    request: Request,
    service: Annotated[RawReplayService, Depends(get_raw_replay_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> ReplayResultModel:
    actor = await _admin(facade, credentials)
    try:
        count = await service.replay(id, actor=actor, ip=_ip(request))
    except RawReplayNotFound:
        raise ApiError(404, "NOT_FOUND", "原始报文不存在", None) from None
    except (RawReplayConflict, RawIntegrityConflict) as error:
        raise ApiError(409, "STATE_CONFLICT", str(error), None) from None
    return ReplayResultModel(processed_items=count)


@router.post(
    "/unmatched-reports/export",
    response_model=ExportTaskModel,
    status_code=status.HTTP_202_ACCEPTED,
    responses={400: ERROR_RESPONSE, 401: ERROR_RESPONSE, 403: ERROR_RESPONSE},
)
@audited("export_create")
async def create_unmatched_export(
    payload: UnmatchedExportModel,
    service: Annotated[ExportService, Depends(get_export_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> ExportTaskModel:
    claims = await _admin_claims(facade, credentials)
    try:
        task = await service.create(
            ExportRequestFilters(
                phone=payload.phone,
                start=payload.start,
                end=payload.end,
                dataset="unmatched",
            ),
            decrypted=payload.decrypted,
            principal=claims.principal,
        )
    except ExportForbidden as error:
        raise ApiError(403, "FORBIDDEN", str(error), None) from None
    except (ExportTooLarge, ValueError) as error:
        raise ApiError(400, "INVALID_PARAM", str(error), None) from None
    return _response(task)
