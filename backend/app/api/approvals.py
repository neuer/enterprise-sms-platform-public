"""审批列表、详情与决策接口。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Query, Request
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from app.api.auth import ERROR_RESPONSE, bearer_scheme
from app.core.audit import audited
from app.core.auth.jwt import JwtClaims
from app.core.auth.runtime import AuthFacade, get_auth_facade
from app.core.client_ip import trusted_client_ip
from app.core.errors import ApiError
from app.services.alert_repository import SqlAlertService
from app.services.approval import ApprovalService, SelfApprovalDenied, StateConflict
from app.services.approval_repository import SqlApprovalRepository
from app.services.crypto import CryptoService
from app.services.queue import CeleryQueuePublisher
from app.services.quota import QuotaRedis, QuotaService
from app.services.sensitive_read_audit import (
    SensitiveReadAuditor,
    get_sensitive_read_auditor,
)
from app.settings import get_settings

router = APIRouter(prefix="/api/v1/web/approvals", tags=["approval"])

ApprovalStatus = Literal["pending", "approved", "rejected", "expired"]


class ApprovalListItem(BaseModel):
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
    batch_status: str
    deferred_reason: str | None
    status: ApprovalStatus
    approver: str | None
    reason: str | None = Field(max_length=256)
    expires_at: datetime
    decided_at: datetime | None
    created_at: datetime


class ApprovalDetail(ApprovalListItem):
    content: str


class ApprovalCounts(BaseModel):
    pending: int
    approved: int
    rejected: int
    expired: int
    pending_urgent: int


class ApprovalPage(BaseModel):
    total: int
    counts: ApprovalCounts
    items: list[ApprovalListItem]


class DecisionOutcome(BaseModel):
    status: ApprovalStatus
    batch_status: str
    deferred_reason: str | None


class DecisionRequest(BaseModel):
    action: Literal["approve", "reject"]
    reason: str | None = Field(default=None, max_length=256)


async def get_approval_service() -> AsyncIterator[ApprovalService]:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_control_url, decode_responses=True)
    try:
        yield ApprovalService(
            SqlApprovalRepository(settings, CryptoService.from_settings(settings)),
            QuotaService(cast(QuotaRedis, redis)),
            CeleryQueuePublisher(),
            SqlAlertService(settings),
        )
    finally:
        await redis.aclose()


def get_approval_repository() -> SqlApprovalRepository:
    settings = get_settings()
    return SqlApprovalRepository(settings, CryptoService.from_settings(settings))


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


@router.get(
    "",
    response_model=ApprovalPage,
    responses={401: ERROR_RESPONSE, 403: ERROR_RESPONSE},
)
async def list_approvals(
    repository: Annotated[SqlApprovalRepository, Depends(get_approval_repository)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    status: Annotated[ApprovalStatus, Query()] = "pending",
    category: Annotated[Literal["notice", "market"] | None, Query()] = None,
    dept: Annotated[str | None, Query(max_length=64)] = None,
    q: Annotated[str | None, Query(max_length=64)] = None,
    sort: Annotated[
        Literal["expires_asc", "created_desc", "decided_desc"] | None, Query()
    ] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=50)] = 20,
) -> dict[str, object]:
    """分页审批列表；正文不在列表解密，敏感读审计只保留在详情端点。"""

    await _approver(facade, credentials)
    effective_sort = sort or ("expires_asc" if status == "pending" else "decided_desc")
    dept_pattern = f"%{dept}%" if dept is not None else None
    keyword = f"%{q}%" if q is not None else None
    result = await repository.list_page(
        status=status,
        dept=dept_pattern,
        category=category,
        q=keyword,
        sort=effective_sort,
        page=page,
        size=size,
    )
    counts = await repository.counts(dept=dept_pattern, category=category, q=keyword)
    return {
        "total": result["total"],
        "counts": counts,
        "items": result["items"],
    }


@router.get(
    "/{id}",
    response_model=ApprovalDetail,
    responses={401: ERROR_RESPONSE, 403: ERROR_RESPONSE, 404: ERROR_RESPONSE},
)
async def get_approval(
    id: int,
    request: Request,
    auditor: Annotated[SensitiveReadAuditor, Depends(get_sensitive_read_auditor)],
    repository: Annotated[SqlApprovalRepository, Depends(get_approval_repository)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> dict[str, object]:
    """审批单详情；唯一正文解密出口，逐条记录敏感读审计。"""

    await _approver(facade, credentials)
    detail = await repository.get_detail(id)
    if detail is None:
        raise ApiError(404, "NOT_FOUND", "审批单不存在", None)
    await auditor.record(
        action="approval_content_read",
        object_type="approval",
        object_id=str(id),
        ip=trusted_client_ip(request),
        count=1,
    )
    return detail


@router.post(
    "/{id}/decision",
    response_model=DecisionOutcome,
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
) -> DecisionOutcome:
    claims = await _approver(facade, credentials)
    try:
        case = await service.decide(
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
    return DecisionOutcome(
        status=cast(ApprovalStatus, case.status),
        batch_status=case.batch_status,
        deferred_reason=case.deferred_reason,
    )
