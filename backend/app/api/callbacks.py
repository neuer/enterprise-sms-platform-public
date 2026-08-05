"""管理员 callback_task 安全摘要查询与 dead 手动重推。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

from app.api.auth import ERROR_RESPONSE, bearer_scheme
from app.core.audit import audited
from app.core.auth.accounts import SecurityPrincipal
from app.core.auth.runtime import AuthFacade, get_auth_facade
from app.core.errors import ApiError
from app.services.callback_repository import (
    CallbackRetryConflict,
    CallbackTaskNotFound,
    SqlCallbackRepository,
)

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


class CallbackPageModel(BaseModel):
    total: int
    dead_total: int
    items: list[CallbackTaskModel]


def get_callback_repository() -> SqlCallbackRepository:
    return SqlCallbackRepository()


async def _admin(
    facade: AuthFacade,
    credentials: HTTPAuthorizationCredentials | None,
) -> SecurityPrincipal:
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise ApiError(401, "UNAUTHORIZED", "缺少有效的 Bearer 令牌", None)
    claims = await facade.verify(credentials.credentials)
    if claims.role != "admin":
        raise ApiError(403, "FORBIDDEN", "仅管理员可管理回调任务", None)
    return claims.principal


@router.get("", response_model=CallbackPageModel)
async def list_callbacks(
    repository: Annotated[SqlCallbackRepository, Depends(get_callback_repository)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    status: Annotated[CallbackStatus | None, Query()] = None,
    app_id: Annotated[int | None, Query(ge=1)] = None,
    event: Annotated[Literal["batch.finished", "message.report"] | None, Query()] = None,
    batch_no: Annotated[str | None, Query(max_length=32)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, object]:
    await _admin(facade, credentials)
    return await repository.list_page(
        status=status,
        app_id=app_id,
        event=event,
        batch_no=batch_no,
        page=page,
        size=size,
    )


@router.post(
    "/{id}/retry",
    response_class=Response,
    responses={403: ERROR_RESPONSE, 404: ERROR_RESPONSE, 409: ERROR_RESPONSE},
)
@audited("callback_retry")
async def retry_callback(
    id: int,
    repository: Annotated[SqlCallbackRepository, Depends(get_callback_repository)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> Response:
    principal = await _admin(facade, credentials)
    try:
        await repository.manual_retry(id, principal=principal)
    except CallbackTaskNotFound as error:
        raise ApiError(404, "NOT_FOUND", str(error), None) from None
    except CallbackRetryConflict as error:
        raise ApiError(409, "STATE_CONFLICT", str(error), None) from None
    return Response(status_code=200)
