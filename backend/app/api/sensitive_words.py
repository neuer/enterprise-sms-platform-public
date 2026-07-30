"""管理员敏感词 CRUD。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

from app.api.auth import ERROR_RESPONSE, bearer_scheme
from app.core.audit import audited
from app.core.auth.runtime import AuthFacade, get_auth_facade
from app.core.errors import ApiError
from app.services.sensitive import SensitiveWordManager, sensitive_word_index
from app.services.sensitive_repository import SqlSensitiveWordRepository

router = APIRouter(prefix="/api/v1/web/admin/sensitive-words", tags=["admin"])


class SensitiveWordItem(BaseModel):
    id: int
    word: str


class AddSensitiveWordsRequest(BaseModel):
    words: list[str] = Field(min_length=1, max_length=10000)


def get_sensitive_word_manager() -> SensitiveWordManager:
    return SensitiveWordManager(SqlSensitiveWordRepository(), sensitive_word_index)


async def _admin(
    facade: AuthFacade,
    credentials: HTTPAuthorizationCredentials | None,
) -> str:
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise ApiError(401, "UNAUTHORIZED", "缺少有效的 Bearer 令牌", None)
    claims = await facade.verify(credentials.credentials)
    if claims.role != "admin":
        raise ApiError(403, "FORBIDDEN", "仅管理员可管理敏感词", None)
    return claims.username


@router.get("", response_model=list[SensitiveWordItem])
async def list_sensitive_words(
    manager: Annotated[SensitiveWordManager, Depends(get_sensitive_word_manager)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> list[SensitiveWordItem]:
    await _admin(facade, credentials)
    return [SensitiveWordItem(id=item.id, word=item.word) for item in await manager.list_words()]


@router.post("", response_model=list[SensitiveWordItem], responses={400: ERROR_RESPONSE})
@audited("sensitive_word_add")
async def add_sensitive_words(
    payload: AddSensitiveWordsRequest,
    request: Request,
    manager: Annotated[SensitiveWordManager, Depends(get_sensitive_word_manager)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> list[SensitiveWordItem]:
    actor = await _admin(facade, credentials)
    try:
        created = await manager.add(payload.words, actor=actor)
    except ValueError as error:
        raise ApiError(400, "INVALID_PARAM", str(error), None) from None
    return [SensitiveWordItem(id=item.id, word=item.word) for item in created]


@router.delete("/{id}", status_code=204, response_class=Response, responses={404: ERROR_RESPONSE})
@audited("sensitive_word_delete")
async def delete_sensitive_word(
    id: int,
    request: Request,
    manager: Annotated[SensitiveWordManager, Depends(get_sensitive_word_manager)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> Response:
    actor = await _admin(facade, credentials)
    if not await manager.delete(id, actor=actor):
        raise ApiError(404, "NOT_FOUND", "敏感词不存在", None)
    return Response(status_code=204)
