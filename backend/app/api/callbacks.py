"""管理员 callback_task 安全摘要查询与 dead 手动重推。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field

from app.api.auth import ERROR_RESPONSE
from app.api.authorization import AdminActor
from app.core.audit import audited
from app.core.errors import ApiError
from app.core.query_bounds import PAGE_MAX
from app.services.callback_batch_event import (
    FinishedRecoveryRejected,
    FinishedRecoveryService,
)
from app.services.callback_repository import CallbackTaskNotFound, SqlCallbackRepository
from app.services.callback_retry_eligibility import CallbackRetryConflict

router = APIRouter(prefix="/api/v1/web/admin/callbacks", tags=["admin"])
CallbackStatus = Literal["pending", "retrying", "done", "dead"]


class CallbackTaskModel(BaseModel):
    id: int
    event_id: UUID
    correlation_id: UUID
    app_id: int
    app_name: str
    event: Literal["batch.finished", "message.report"]
    batch_no: str | None
    reference_count: int
    status: CallbackStatus
    retry_count: int
    next_retry_at: datetime | None
    lease_id: UUID | None
    lease_expires_at: datetime | None
    takeover_count: Annotated[int, Field(ge=0)]
    stalled: bool
    last_http_code: int | None
    last_error: str | None
    created_at: datetime
    finished_at: datetime | None
    manual_retry_allowed: bool
    retry_block_reason: (
        Literal[
            "config_revoked",
            "app_disabled",
            "config_changed",
            "detail_disabled",
            "not_dead",
            "legacy_snapshot_unavailable",
        ]
        | None
    )
    finished_snapshot: Literal["current", "legacy", "missing"] | None


class FinishedRecoveryModel(BaseModel):
    task_id: int
    event_id: UUID
    created: bool


class CallbackPageModel(BaseModel):
    total: int
    dead_total: int
    items: list[CallbackTaskModel]


def get_callback_repository(_actor: AdminActor) -> SqlCallbackRepository:
    return SqlCallbackRepository()


def get_finished_recovery(_actor: AdminActor) -> FinishedRecoveryService:
    return FinishedRecoveryService()


@router.get("", response_model=CallbackPageModel)
async def list_callbacks(
    repository: Annotated[SqlCallbackRepository, Depends(get_callback_repository)],
    actor: AdminActor,
    status: Annotated[CallbackStatus | None, Query()] = None,
    app_id: Annotated[int | None, Query(ge=1)] = None,
    event: Annotated[Literal["batch.finished", "message.report"] | None, Query()] = None,
    batch_no: Annotated[str | None, Query(max_length=32)] = None,
    page: Annotated[int, Query(ge=1, le=PAGE_MAX)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, object]:
    try:
        return await repository.list_page(
            status=status,
            app_id=app_id,
            event=event,
            batch_no=batch_no,
            page=page,
            size=size,
        )
    except ValueError as error:
        raise ApiError(400, "INVALID_PARAM", str(error), None) from None


@router.post(
    "/{id}/retry",
    response_class=Response,
    responses={403: ERROR_RESPONSE, 404: ERROR_RESPONSE, 409: ERROR_RESPONSE},
)
@audited("callback_retry")
async def retry_callback(
    id: int,
    repository: Annotated[SqlCallbackRepository, Depends(get_callback_repository)],
    actor: AdminActor,
) -> Response:
    principal = actor.principal
    try:
        await repository.manual_retry(id, principal=principal)
    except CallbackTaskNotFound as error:
        raise ApiError(404, "NOT_FOUND", str(error), None) from None
    except CallbackRetryConflict as error:
        raise ApiError(
            409,
            "STATE_CONFLICT",
            str(error),
            {"reason": error.reason},
        ) from None
    return Response(status_code=200)


@router.post(
    "/{id}/recover-finished",
    response_model=FinishedRecoveryModel,
    responses={403: ERROR_RESPONSE, 404: ERROR_RESPONSE, 409: ERROR_RESPONSE},
)
@audited("callback_finished_recover")
async def recover_finished_callback(
    id: int,
    recovery: Annotated[FinishedRecoveryService, Depends(get_finished_recovery)],
    actor: AdminActor,
) -> FinishedRecoveryModel:
    try:
        result = await recovery.recover(id, principal=actor.principal)
        return FinishedRecoveryModel(
            task_id=result.task_id,
            event_id=result.event_id,
            created=result.created,
        )
    except FinishedRecoveryRejected as error:
        if error.missing:
            raise ApiError(404, "NOT_FOUND", str(error), {"reason": error.reason}) from None
        raise ApiError(
            409,
            "STATE_CONFLICT",
            str(error),
            {"reason": error.reason},
        ) from None
