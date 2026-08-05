"""审批列表与决策接口。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from app.api.auth import ERROR_RESPONSE, bearer_scheme
from app.core.audit import audited
from app.core.auth.jwt import JwtClaims
from app.core.auth.runtime import AuthFacade, get_auth_facade
from app.core.errors import ApiError
from app.services.alert_repository import SqlAlertService
from app.services.approval import ApprovalService, SelfApprovalDenied, StateConflict
from app.services.approval_repository import SqlApprovalRepository
from app.services.queue import CeleryQueuePublisher
from app.services.quota import QuotaRedis, QuotaService
from app.settings import get_settings

router = APIRouter(prefix="/api/v1/web/approvals", tags=["approval"])


class ApprovalItem(BaseModel):
    id: int
    batch_no: str
    category: str
    applicant: str
    dept: str
    total: int
    segments: int = Field(ge=1)
    estimated_segments: int = Field(ge=1)
    scheduled_at: datetime | None
    trigger_threshold: int | None = Field(ge=1)
    trigger_threshold_source: Literal["snapshot", "legacy_unknown"]
    content: str
    status: Literal["pending", "approved", "rejected", "expired"]
    approver: str | None
    reason: str | None = Field(max_length=256)
    expires_at: datetime
    decided_at: datetime | None
    created_at: datetime


class ApprovalPage(BaseModel):
    total: int
    items: list[ApprovalItem]


class DecisionRequest(BaseModel):
    action: Literal["approve", "reject"]
    reason: str | None = Field(default=None, max_length=256)


async def get_approval_service() -> AsyncIterator[ApprovalService]:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_control_url, decode_responses=True)
    try:
        yield ApprovalService(
            SqlApprovalRepository(settings),
            QuotaService(cast(QuotaRedis, redis)),
            CeleryQueuePublisher(),
            SqlAlertService(settings),
        )
    finally:
        await redis.aclose()


def get_approval_repository() -> SqlApprovalRepository:
    return SqlApprovalRepository()


async def _approver(
    facade: AuthFacade,
    credentials: HTTPAuthorizationCredentials | None,
) -> JwtClaims:
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise ApiError(401, "UNAUTHORIZED", "缺少有效的 Bearer 令牌", None)
    claims = await facade.verify(credentials.credentials)
    if claims.role not in {"approver", "admin"}:
        raise ApiError(403, "FORBIDDEN", "仅审批人可操作", None)
    return claims


@router.get("", response_model=ApprovalPage)
async def list_approvals(
    repository: Annotated[SqlApprovalRepository, Depends(get_approval_repository)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    status: Annotated[
        Literal["pending", "approved", "rejected", "expired"], Query()
    ] = "pending",
    page: Annotated[int, Query(ge=1)] = 1,
) -> dict[str, object]:
    await _approver(facade, credentials)
    return await repository.list_page(
        status=status,
        dept=None,
        page=page,
    )


@router.post(
    "/{id}/decision",
    response_class=Response,
    responses={400: ERROR_RESPONSE, 403: ERROR_RESPONSE, 409: ERROR_RESPONSE},
)
@audited("approval_decision")
async def decide_approval(
    id: int,
    payload: DecisionRequest,
    request: Request,
    service: Annotated[ApprovalService, Depends(get_approval_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> Response:
    claims = await _approver(facade, credentials)
    try:
        await service.decide(
            id,
            action=payload.action,
            principal=claims.principal,
            reason=payload.reason,
        )
    except SelfApprovalDenied as error:
        raise ApiError(403, "SELF_APPROVAL_DENIED", str(error), None) from None
    except StateConflict as error:
        raise ApiError(409, "STATE_CONFLICT", str(error), None) from None
    except ValueError as error:
        raise ApiError(400, "INVALID_PARAM", str(error), None) from None
    return Response(status_code=200)
