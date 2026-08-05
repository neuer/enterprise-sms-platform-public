"""上行回复掩码查询与退订加黑接口。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel
from redis.asyncio import Redis

from app.api.auth import ERROR_RESPONSE, bearer_scheme
from app.core.audit import audited
from app.core.auth.jwt import JwtClaims
from app.core.auth.runtime import AuthFacade, get_auth_facade
from app.core.errors import ApiError
from app.services.blacklist import RedisBlacklistCache
from app.services.crypto import CryptoService
from app.services.reply_query import (
    ReplyNotFound,
    ReplyQueryService,
    SqlReplyQueryRepository,
)
from app.settings import get_settings

router = APIRouter(prefix="/api/v1/web/replies", tags=["replies"])


class ReplyModel(BaseModel):
    id: int
    phone: str
    content: str
    batch_no: str | None
    reply_time: datetime
    blacklisted: bool


class ReplyPageModel(BaseModel):
    total: int
    items: list[ReplyModel]


async def get_reply_service() -> AsyncIterator[ReplyQueryService]:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_control_url, decode_responses=True)
    try:
        yield ReplyQueryService(
            SqlReplyQueryRepository(settings),
            RedisBlacklistCache(redis),
            CryptoService.from_settings(settings),
        )
    finally:
        await redis.aclose()


async def _claims(
    facade: AuthFacade,
    credentials: HTTPAuthorizationCredentials | None,
    *,
    write: bool,
) -> JwtClaims:
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise ApiError(401, "UNAUTHORIZED", "缺少有效的 Bearer 令牌", None)
    claims = await facade.verify(credentials.credentials)
    allowed = {"operator", "admin"} if write else {
        "viewer",
        "operator",
        "approver",
        "admin",
    }
    if claims.role not in allowed:
        raise ApiError(403, "FORBIDDEN", "当前角色无回复操作权限", None)
    return claims


@router.get("", response_model=ReplyPageModel, responses={400: ERROR_RESPONSE})
async def list_replies(
    service: Annotated[ReplyQueryService, Depends(get_reply_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    phone: Annotated[str | None, Query(pattern=r"^1\d{10}$")] = None,
    start: Annotated[datetime | None, Query()] = None,
    end: Annotated[datetime | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
) -> ReplyPageModel:
    claims = await _claims(facade, credentials, write=False)
    try:
        result = await service.list_page(
            phone=phone,
            start=start,
            end=end,
            page=page,
            dept=None if claims.role == "admin" else claims.dept,
        )
    except ValueError as error:
        raise ApiError(400, "INVALID_PARAM", str(error), None) from None
    return ReplyPageModel(
        total=result.total,
        items=[
            ReplyModel(
                id=item.id,
                phone=item.phone_mask,
                content=item.content,
                batch_no=item.batch_no,
                reply_time=item.reply_time,
                blacklisted=item.blacklisted,
            )
            for item in result.items
        ],
    )


@router.post(
    "/{id}/blacklist",
    response_class=Response,
    responses={403: ERROR_RESPONSE, 404: ERROR_RESPONSE},
)
@audited("reply_optout")
async def blacklist_reply(
    id: int,
    service: Annotated[ReplyQueryService, Depends(get_reply_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> Response:
    claims = await _claims(facade, credentials, write=True)
    try:
        await service.optout(
            id,
            dept=None if claims.role == "admin" else claims.dept,
            actor=claims.username,
        )
    except ReplyNotFound as error:
        raise ApiError(404, "NOT_FOUND", str(error), None) from None
    return Response(status_code=200)
