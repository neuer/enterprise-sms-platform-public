"""管理员黑名单 CRUD 与批量导入接口。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Path, Request, Response
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from app.api.auth import ERROR_RESPONSE, bearer_scheme
from app.core.audit import audited
from app.core.auth.runtime import AuthFacade, get_auth_facade
from app.core.errors import ApiError
from app.services.blacklist import BlacklistEntry, BlacklistService, RedisBlacklistCache
from app.services.blacklist_repository import SqlBlacklistRepository
from app.services.crypto import CryptoService
from app.settings import get_settings

router = APIRouter(prefix="/api/v1/web/admin/blacklist", tags=["admin"])


class BlacklistItem(BaseModel):
    phone_hmac: str
    phone_mask: str
    source: Literal["manual", "reply_optout", "import"]
    remark: str | None
    created_at: datetime | None


class AddBlacklistRequest(BaseModel):
    phones: list[str] = Field(min_length=1, max_length=50000)
    source: Literal["manual", "reply_optout", "import"] = "manual"
    remark: str | None = Field(default=None, max_length=128)


class AddBlacklistResponse(BaseModel):
    added: int
    items: list[BlacklistItem]


def _item(entry: BlacklistEntry) -> BlacklistItem:
    return BlacklistItem(
        phone_hmac=entry.phone_hmac,
        phone_mask=entry.phone_mask,
        source=cast(Literal["manual", "reply_optout", "import"], entry.source),
        remark=entry.remark,
        created_at=entry.created_at,
    )


async def get_blacklist_service() -> AsyncIterator[BlacklistService]:
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


async def _admin(
    request: Request,
    facade: AuthFacade,
    credentials: HTTPAuthorizationCredentials | None,
) -> str:
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise ApiError(401, "UNAUTHORIZED", "缺少有效的 Bearer 令牌", None)
    claims = await facade.verify(credentials.credentials)
    if claims.role != "admin":
        raise ApiError(403, "FORBIDDEN", "仅管理员可管理黑名单", None)
    return claims.username


@router.get("", response_model=list[BlacklistItem])
async def list_blacklist(
    service: Annotated[BlacklistService, Depends(get_blacklist_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
    request: Request,
) -> list[BlacklistItem]:
    await _admin(request, facade, credentials)
    return [_item(entry) for entry in await service.list_entries()]


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
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
) -> AddBlacklistResponse:
    actor = await _admin(request, facade, credentials)
    try:
        entries = await service.add(
            payload.phones,
            source=payload.source,
            remark=payload.remark,
            actor=actor,
        )
    except ValueError as error:
        raise ApiError(400, "INVALID_PARAM", str(error), None) from None
    items = [_item(entry) for entry in entries]
    return AddBlacklistResponse(added=len(items), items=items)


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
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
) -> Response:
    actor = await _admin(request, facade, credentials)
    if len(phone_hmac) != 64 or any(char not in "0123456789abcdef" for char in phone_hmac):
        raise ApiError(400, "INVALID_PARAM", "phone_hmac 格式错误", None)
    if not await service.delete(phone_hmac, actor=actor):
        raise ApiError(404, "NOT_FOUND", "黑名单记录不存在", None)
    return Response(status_code=204)
