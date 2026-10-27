"""未覆盖失败号码的预览与确认。不复用普通重发幂等键。"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.auth import ERROR_RESPONSE
from app.api.messages import (
    ResendResponseModel,
    _error,
    _pipeline,
    require_batch_actor,
    require_batch_writer,
)
from app.core.apikey import ApiAppContext
from app.core.audit import audited
from app.core.auth.accounts import ActorPrincipal, ApplicationPrincipal
from app.core.auth.jwt import JwtClaims
from app.core.errors import ApiError
from app.services.batch_query import BatchAccessScope, BatchNotFound
from app.services.crypto import CryptoService
from app.services.resend_eligibility import ResendBlocked
from app.services.resend_supplement import ResendSupplementService
from app.settings import get_settings

router = APIRouter(prefix="/api/v1/messages", tags=["messages"])


class SupplementPreviewModel(BaseModel):
    intent_id: UUID
    covered_failed: int
    uncovered_failed: int
    child_batch_no: str | None
    phone_masks: list[str]
    created: bool


def get_supplement_service() -> ResendSupplementService:
    settings = get_settings()
    return ResendSupplementService(CryptoService.from_settings(settings), settings)


def _map(error: Exception) -> ApiError:
    if isinstance(error, ResendBlocked):
        return ApiError(
            409,
            "STATE_CONFLICT",
            str(error),
            {
                "reason": error.reason,
                "covered_failed": error.covered,
                "uncovered_failed": error.uncovered,
                "child_batch_no": error.child_batch_no,
            },
        )
    if isinstance(error, BatchNotFound):
        return ApiError(404, "NOT_FOUND", "批次不存在", None)
    return _error(error)


@router.post(
    "/batches/{batch_no}/resend-supplement",
    response_model=SupplementPreviewModel,
    responses={403: ERROR_RESPONSE, 404: ERROR_RESPONSE, 409: ERROR_RESPONSE},
)
@audited("resend_supplement_preview")
async def preview_resend_supplement(
    batch_no: str,
    service: Annotated[ResendSupplementService, Depends(get_supplement_service)],
    scope: Annotated[BatchAccessScope, Depends(require_batch_writer)],
) -> SupplementPreviewModel:
    try:
        preview = await service.preview(batch_no, scope)
    except Exception as error:
        raise _map(error) from error
    return SupplementPreviewModel(
        intent_id=preview.intent_id,
        covered_failed=preview.covered_failed,
        uncovered_failed=preview.uncovered_failed,
        child_batch_no=preview.child_batch_no,
        phone_masks=list(preview.phone_masks),
        created=preview.created,
    )


@router.post(
    "/batches/{batch_no}/resend-supplement/{intent_id}",
    response_model=ResendResponseModel,
    responses={403: ERROR_RESPONSE, 404: ERROR_RESPONSE, 409: ERROR_RESPONSE},
)
@audited("resend_supplement_confirm")
async def confirm_resend_supplement(
    batch_no: str,
    intent_id: UUID,
    service: Annotated[ResendSupplementService, Depends(get_supplement_service)],
    batch_actor: Annotated[ApiAppContext | JwtClaims, Depends(require_batch_actor)],
    scope: Annotated[BatchAccessScope, Depends(require_batch_writer)],
) -> ResendResponseModel:
    actor: ActorPrincipal
    if isinstance(batch_actor, ApiAppContext):
        actor = ApplicationPrincipal(batch_actor.app_id, batch_actor.name, batch_actor.dept)
        pipeline_app = batch_actor
    else:
        actor = batch_actor.principal
        pipeline_app = ApiAppContext(
            0,
            "web",
            batch_actor.dept,
            frozenset({"notice", "market"}),
            daily_quota=0,
        )

    async def accept(request):  # type: ignore[no-untyped-def]
        app = pipeline_app
        if request.channel == "web":
            app = ApiAppContext(
                0,
                "web",
                request.resend_dept or app.dept,
                frozenset({"notice", "market"}),
                daily_quota=0,
            )
        if request.channel != ("api" if isinstance(batch_actor, ApiAppContext) else "web"):
            raise ApiError(403, "FORBIDDEN", "认证方式与原批次渠道不匹配", None)
        pipeline = await _pipeline(app)
        return await pipeline.accept(app, request)

    try:
        result = await service.confirm(
            batch_no,
            intent_id,
            scope,
            actor=actor,
            accept=accept,
        )
    except ApiError:
        raise
    except Exception as error:
        raise _map(error) from error
    return ResendResponseModel(
        batch_no=result.batch_no,
        resend_of=batch_no,
        accepted=result.accepted,
        status=result.status,
        idempotent=result.idempotent,
    )
