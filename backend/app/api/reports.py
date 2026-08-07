"""异步导出创建、状态与鉴权流式下载接口。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.api.auth import ERROR_RESPONSE, bearer_scheme
from app.core.audit import AuditEvent, audited, insert_audit
from app.core.auth.jwt import JwtClaims
from app.core.auth.runtime import AuthFacade, get_auth_facade
from app.core.errors import ApiError
from app.core.jobtrack import JOB_SPECS
from app.core.runtime_resources import database_engine, redis_client
from app.services.crypto import CryptoService
from app.services.dashboard import DashboardService, DashboardSnapshot
from app.services.dashboard_repository import SqlDashboardRepository
from app.services.export import (
    ExportForbidden,
    ExportNotFound,
    ExportRequestFilters,
    ExportService,
    ExportTaskInfo,
    ExportTooLarge,
)
from app.services.export_file import ExportFileCodec
from app.services.export_repository import SqlExportRepository
from app.services.export_step_up import (
    EXPORT_STEP_UP_TTL_SECONDS,
    ExportStepUpExpired,
    ExportStepUpService,
)
from app.services.reporting import (
    Granularity,
    GroupBy,
    ReportCategory,
    ReportingResult,
    ReportingService,
)
from app.services.reporting_repository import SqlReportingRepository
from app.settings import get_settings
from app.tasks import register_task_modules

router = APIRouter(prefix="/api/v1/web/reports", tags=["reports"])


class ExportFiltersModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: datetime | None = None
    end: datetime | None = None
    category: Literal["verify", "notice", "market"] | None = None
    status: str | None = Field(default=None, max_length=16)
    app_id: int | None = Field(default=None, ge=1)
    dept: str | None = Field(default=None, max_length=128)
    batch_no: str | None = Field(default=None, max_length=32)
    phone: str | None = Field(default=None, pattern=r"^1\d{10}$")


class ExportCreateModel(BaseModel):
    filters: ExportFiltersModel = Field(default_factory=ExportFiltersModel)
    decrypted: bool = False


class ExportTaskModel(BaseModel):
    id: UUID
    status: Literal["pending", "running", "done", "failed"]
    decrypted: bool
    row_count: int | None
    download_url: str | None
    expires_at: datetime | None
    created_at: datetime


class ExportStepUpRequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: SecretStr = Field(min_length=1, max_length=1024)


class ExportStepUpResponseModel(BaseModel):
    token: str
    expires_in: int = EXPORT_STEP_UP_TTL_SECONDS


class DashboardCategoryModel(BaseModel):
    category: Literal["verify", "notice", "market"]
    total: int
    total_segments: int
    delivered: int
    failed: int
    unknown: int
    success_rate: float


class DashboardBalancePointModel(BaseModel):
    stat_date: date
    balance: int


class DashboardAlertModel(BaseModel):
    level: Literal["info", "warn", "crit"]
    title: str
    created_at: datetime


class DashboardJobModel(BaseModel):
    job_name: str
    last_run_at: datetime | None
    last_status: Literal["running", "success", "failed"] | None
    stalled: bool


class DashboardDispositionsModel(BaseModel):
    uncertain: int
    unmatched: int
    callback_dead: int


class DashboardChannelMonitorModel(BaseModel):
    realtime_queue: int | None
    bulk_queue: int | None
    qps_used: int | None
    qps_rate: int = Field(ge=1)
    reserved_realtime_qps: int = Field(ge=0)
    stale: bool
    degraded_reason: Literal["redis_unavailable", "snapshot_incomplete"] | None = None


class DashboardUiPolicyModel(BaseModel):
    test_send_max: int = Field(ge=1)


class DashboardOperationsModel(BaseModel):
    current_balance: int | None
    balances: list[DashboardBalancePointModel]
    alerts: list[DashboardAlertModel]
    dispositions: DashboardDispositionsModel
    jobs: list[DashboardJobModel]
    channel_monitor: DashboardChannelMonitorModel
    balance_alert_threshold: int = Field(ge=1)


class DashboardModel(BaseModel):
    refreshed_at: datetime
    categories: list[DashboardCategoryModel]
    overall_success_rate: float
    pending_approvals: int
    ui_policy: DashboardUiPolicyModel
    operations: DashboardOperationsModel | None = None


class ReportingRowModel(BaseModel):
    period_start: date
    dim_value: str
    dim_label: str
    total: int
    total_segments: int
    delivered: int
    failed: int
    unknown: int
    success_rate: float


class ReportingSummaryModel(BaseModel):
    total: int
    total_segments: int
    delivered: int
    failed: int
    unknown: int
    success_rate: float


class ReportingModel(BaseModel):
    granularity: Granularity
    group_by: GroupBy
    category: ReportCategory
    start: date
    end: date
    can_export_decrypted: bool
    summary: ReportingSummaryModel
    items: list[ReportingRowModel]


async def get_export_service() -> ExportService:
    settings = get_settings()
    repository = SqlExportRepository(settings)
    return ExportService(
        repository,
        CryptoService.from_settings(settings),
        retention_days=await repository.retention_days(),
    )


def get_export_step_up_service(
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
) -> ExportStepUpService:
    settings = get_settings()

    async def audit_step_up(event: AuditEvent) -> None:
        async with database_engine(settings.database_url).connect() as connection:
            await insert_audit(connection, event)

    return ExportStepUpService(
        facade,
        redis_client(settings.redis_auth_url),
        audit_sink=audit_step_up,
    )


def get_dashboard_service() -> DashboardService:
    register_task_modules()
    specs = tuple(JOB_SPECS[name] for name in sorted(JOB_SPECS))
    return DashboardService(SqlDashboardRepository(), specs)


def get_reporting_service() -> ReportingService:
    return ReportingService(SqlReportingRepository())


def get_export_codec() -> ExportFileCodec:
    settings = get_settings()
    return ExportFileCodec(
        CryptoService.from_settings(settings),
        settings.export_storage_dir,
    )


async def _claims(
    facade: AuthFacade,
    credentials: HTTPAuthorizationCredentials | None,
) -> JwtClaims:
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise ApiError(401, "UNAUTHORIZED", "缺少有效的 Bearer 令牌", None)
    return await facade.verify(credentials.credentials)


def _request_filters(value: ExportFiltersModel) -> ExportRequestFilters:
    return ExportRequestFilters(**value.model_dump())


def _response(task: ExportTaskInfo) -> ExportTaskModel:
    available = (
        task.status == "done"
        and task.file_path is not None
        and task.expires_at is not None
        and task.expires_at > datetime.now(UTC)
    )
    return ExportTaskModel(
        id=task.public_id,
        status=cast(Literal["pending", "running", "done", "failed"], task.status),
        decrypted=task.decrypted,
        row_count=task.row_count,
        download_url=(
            f"/api/v1/web/reports/export/{task.public_id}/download" if available else None
        ),
        expires_at=task.expires_at,
        created_at=task.created_at,
    )


def _dashboard_response(snapshot: DashboardSnapshot) -> DashboardModel:
    operations = snapshot.operations
    return DashboardModel(
        refreshed_at=snapshot.refreshed_at,
        categories=[
            DashboardCategoryModel.model_validate(item, from_attributes=True)
            for item in snapshot.categories
        ],
        overall_success_rate=snapshot.overall_success_rate,
        pending_approvals=snapshot.pending_approvals,
        ui_policy=DashboardUiPolicyModel.model_validate(
            snapshot.ui_policy,
            from_attributes=True,
        ),
        operations=(
            DashboardOperationsModel(
                current_balance=operations.current_balance,
                balances=[
                    DashboardBalancePointModel.model_validate(
                        item,
                        from_attributes=True,
                    )
                    for item in operations.balances
                ],
                alerts=[
                    DashboardAlertModel.model_validate(item, from_attributes=True)
                    for item in operations.alerts
                ],
                dispositions=DashboardDispositionsModel(
                    uncertain=operations.uncertain,
                    unmatched=operations.unmatched,
                    callback_dead=operations.callback_dead,
                ),
                jobs=[
                    DashboardJobModel.model_validate(item, from_attributes=True)
                    for item in operations.jobs
                ],
                channel_monitor=DashboardChannelMonitorModel(
                    realtime_queue=operations.realtime_queue,
                    bulk_queue=operations.bulk_queue,
                    qps_used=operations.qps_used,
                    qps_rate=operations.qps_rate,
                    reserved_realtime_qps=operations.reserved_realtime_qps,
                    stale=operations.channel_stale,
                    degraded_reason=operations.degraded_reason,
                ),
                balance_alert_threshold=operations.balance_alert_threshold,
            )
            if operations is not None
            else None
        ),
    )


def _reporting_response(result: ReportingResult) -> ReportingModel:
    return ReportingModel(
        granularity=result.granularity,
        group_by=result.group_by,
        category=result.category,
        start=result.start,
        end=result.end,
        can_export_decrypted=result.can_export_decrypted,
        summary=ReportingSummaryModel.model_validate(
            result.summary,
            from_attributes=True,
        ),
        items=[
            ReportingRowModel.model_validate(item, from_attributes=True)
            for item in result.items
        ],
    )


@router.get(
    "/dashboard",
    response_model=DashboardModel,
    response_model_exclude_none=True,
    responses={401: ERROR_RESPONSE},
)
async def get_dashboard(
    service: Annotated[DashboardService, Depends(get_dashboard_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> DashboardModel:
    claims = await _claims(facade, credentials)
    return _dashboard_response(await service.get(role=claims.role, dept=claims.dept))


@router.get(
    "/stats",
    response_model=ReportingModel,
    responses={400: ERROR_RESPONSE, 401: ERROR_RESPONSE},
)
async def get_reporting_stats(
    service: Annotated[ReportingService, Depends(get_reporting_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    granularity: Granularity = "day",
    group_by: GroupBy = "app",
    category: ReportCategory = "all",
    start: date | None = None,
    end: date | None = None,
) -> ReportingModel:
    claims = await _claims(facade, credentials)
    try:
        result = await service.get(
            granularity=granularity,
            group_by=group_by,
            category=category,
            start=start,
            end=end,
            role=claims.role,
            dept=claims.dept,
        )
    except ValueError as error:
        raise ApiError(400, "INVALID_PARAM", str(error), None) from None
    return _reporting_response(result)


@router.post(
    "/export",
    response_model=ExportTaskModel,
    status_code=status.HTTP_202_ACCEPTED,
)
@audited("export_create")
async def create_export(
    payload: ExportCreateModel,
    service: Annotated[ExportService, Depends(get_export_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> ExportTaskModel:
    claims = await _claims(facade, credentials)
    try:
        task = await service.create(
            _request_filters(payload.filters),
            decrypted=payload.decrypted,
            principal=claims.principal,
        )
    except ExportForbidden as error:
        raise ApiError(403, "FORBIDDEN", str(error), None) from None
    except (ExportTooLarge, ValueError) as error:
        raise ApiError(400, "INVALID_PARAM", str(error), None) from None
    return _response(task)


@router.get(
    "/export/{public_id}",
    response_model=ExportTaskModel,
    responses={404: ERROR_RESPONSE},
)
async def get_export(
    public_id: UUID,
    service: Annotated[ExportService, Depends(get_export_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> ExportTaskModel:
    claims = await _claims(facade, credentials)
    try:
        return _response(
            await service.get(
                public_id,
                principal=claims.principal,
            )
        )
    except ExportNotFound as error:
        raise ApiError(404, "NOT_FOUND", str(error), None) from None


@router.post(
    "/export/{public_id}/step-up",
    response_model=ExportStepUpResponseModel,
    responses={
        400: ERROR_RESPONSE,
        401: ERROR_RESPONSE,
        403: ERROR_RESPONSE,
        404: ERROR_RESPONSE,
        423: ERROR_RESPONSE,
    },
)
@audited("export_step_up")
async def export_step_up(
    public_id: UUID,
    payload: ExportStepUpRequestModel,
    request: Request,
    export_service: Annotated[ExportService, Depends(get_export_service)],
    step_up_service: Annotated[
        ExportStepUpService,
        Depends(get_export_step_up_service),
    ],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> ExportStepUpResponseModel:
    claims = await _claims(facade, credentials)
    try:
        task = await export_service.get(
            public_id,
            principal=claims.principal,
        )
    except ExportNotFound as error:
        raise ApiError(404, "NOT_FOUND", str(error), None) from None
    if not task.decrypted:
        raise ApiError(400, "INVALID_PARAM", "掩码导出不需要二次认证", None)
    try:
        token = await step_up_service.issue(
            claims=claims,
            password=payload.password.get_secret_value(),
            ip=_ip(request),
            public_id=public_id,
        )
    except ExportStepUpExpired:
        raise ApiError(401, "STEP_UP_REQUIRED", "二次认证失败", None) from None
    return ExportStepUpResponseModel(token=token)


@router.get(
    "/export/{public_id}/download",
    response_class=StreamingResponse,
    responses={401: ERROR_RESPONSE, 404: ERROR_RESPONSE},
)
async def download_export(
    public_id: UUID,
    request: Request,
    service: Annotated[ExportService, Depends(get_export_service)],
    codec: Annotated[ExportFileCodec, Depends(get_export_codec)],
    step_up_service: Annotated[
        ExportStepUpService,
        Depends(get_export_step_up_service),
    ],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    export_step_up_token: Annotated[
        str | None,
        Header(alias="X-Export-Step-Up", min_length=1, max_length=256),
    ] = None,
) -> StreamingResponse:
    claims = await _claims(facade, credentials)
    try:
        visible = await service.get(
            public_id,
            principal=claims.principal,
        )
        if visible.decrypted:
            if export_step_up_token is None:
                raise ExportStepUpExpired("明文导出需要二次认证")
            await step_up_service.consume(
                export_step_up_token,
                claims=claims,
                ip=_ip(request),
                public_id=public_id,
            )
        task = await service.get_downloadable(
            public_id,
            principal=claims.principal,
            ip=_ip(request),
        )
        if task.file_path is None:
            raise ExportNotFound("导出文件不存在或已过期")
        path = codec.validate(task.file_path)
    except ExportStepUpExpired as error:
        raise ApiError(401, "STEP_UP_REQUIRED", str(error), None) from None
    except (ExportNotFound, FileNotFoundError, ValueError) as error:
        raise ApiError(404, "NOT_FOUND", str(error), None) from None
    return StreamingResponse(
        codec.iter_decrypted(path),
        media_type="text/csv; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="sms-export-{public_id}.csv"',
        },
    )


def _ip(request: Request) -> str:
    return request.client.host if request.client is not None else "0.0.0.0"
