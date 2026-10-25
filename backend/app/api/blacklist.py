"""管理员黑名单 CRUD 与批量导入接口。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from app.api.auth import ERROR_RESPONSE
from app.api.authorization import AdminActor
from app.core.audit import audited
from app.core.client_ip import trusted_client_ip
from app.core.errors import ApiError
from app.core.query_bounds import PAGE_MAX
from app.services.blacklist import (
    BlacklistEntry,
    BlacklistService,
    RedisBlacklistCache,
)
from app.services.blacklist_repository import SqlBlacklistRepository
from app.services.crypto import CryptoService
from app.settings import get_settings

router = APIRouter(prefix="/api/v1/web/admin/blacklist", tags=["admin"])

BlacklistSource = Literal["manual", "reply_optout", "import"]


class BlacklistItem(BaseModel):
    phone_hmac: str
    phone_mask: str
    source: BlacklistSource
    remark: str | None
    created_at: datetime | None


class BlacklistPageModel(BaseModel):
    total: int
    items: list[BlacklistItem]


class AddBlacklistRequest(BaseModel):
    phones: list[str] = Field(min_length=1, max_length=50000)
    source: BlacklistSource = "manual"
    remark: str | None = Field(default=None, max_length=128)


class AddBlacklistResponse(BaseModel):
    added: int
    updated: int
    items: list[BlacklistItem]


def _item(entry: BlacklistEntry) -> BlacklistItem:
    return BlacklistItem(
        phone_hmac=entry.phone_hmac,
        phone_mask=entry.phone_mask,
        source=cast(BlacklistSource, entry.source),
        remark=entry.remark,
        created_at=entry.created_at,
    )


async def get_blacklist_service(_actor: AdminActor) -> AsyncIterator[BlacklistService]:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_control_url, decode_responses=True)
    try:
        yield BlacklistService(
            SqlBlacklistRepository(settings),
            RedisBlacklistCache(redis),
            CryptoService.from_settings(settings),
        )
    finally:
        await redis.aclose()


@router.get("", response_model=BlacklistPageModel)
async def list_blacklist(
    service: Annotated[BlacklistService, Depends(get_blacklist_service)],
    actor: AdminActor,
    request: Request,
    source: Annotated[BlacklistSource | None, Query()] = None,
    keyword: Annotated[str | None, Query(max_length=64)] = None,
    page: Annotated[int, Query(ge=1, le=PAGE_MAX)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> BlacklistPageModel:
    try:
        result = await service.list_page(source=source, keyword=keyword, page=page, size=size)
    except ValueError as error:
        raise ApiError(400, "INVALID_PARAM", str(error), None) from None
    return BlacklistPageModel(total=result.total, items=[_item(entry) for entry in result.items])


@router.post(
    "",
    response_model=AddBlacklistResponse,
    responses={400: ERROR_RESPONSE, 403: ERROR_RESPONSE},
)
@audited("blacklist_add")
async def add_blacklist(
    payload: AddBlacklistRequest,
    request: Request,
    service: Annotated[BlacklistService, Depends(get_blacklist_service)],
    actor: AdminActor,
) -> AddBlacklistResponse:
    principal = actor.principal
    try:
        result = await service.add(
            payload.phones,
            source=payload.source,
            remark=payload.remark,
            principal=principal,
            ip=trusted_client_ip(request),
        )
    except ValueError as error:
        raise ApiError(400, "INVALID_PARAM", str(error), None) from None
    items = [_item(entry) for entry in result.entries]
    return AddBlacklistResponse(added=result.added, updated=result.updated, items=items)


@router.delete(
    "/{phone_hmac}",
    status_code=204,
    response_class=Response,
    responses={404: ERROR_RESPONSE},
)
@audited("blacklist_delete")
async def delete_blacklist(
    phone_hmac: Annotated[str, Path(pattern=r"^[0-9a-f]{64}$")],
    request: Request,
    service: Annotated[BlacklistService, Depends(get_blacklist_service)],
    actor: AdminActor,
) -> Response:
    principal = actor.principal
    if not await service.delete(
        phone_hmac,
        principal=principal,
        ip=trusted_client_ip(request),
    ):
        raise ApiError(404, "NOT_FOUND", "黑名单记录不存在", None)
    return Response(status_code=204)
