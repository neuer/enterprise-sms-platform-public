"""管理员敏感词 CRUD。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field

from app.api.auth import ERROR_RESPONSE
from app.api.authorization import AdminActor
from app.core.audit import audited
from app.core.errors import ApiError
from app.core.query_bounds import PAGE_MAX
from app.services.sensitive import SensitiveWord, SensitiveWordManager, sensitive_word_index
from app.services.sensitive_repository import SqlSensitiveWordRepository

router = APIRouter(prefix="/api/v1/web/admin/sensitive-words", tags=["admin"])


class SensitiveWordItem(BaseModel):
    id: int
    word: str
    created_at: datetime | None


class SensitiveWordPageModel(BaseModel):
    total: int
    items: list[SensitiveWordItem]


class AddSensitiveWordsRequest(BaseModel):
    words: list[str] = Field(min_length=1, max_length=10000)


class AddSensitiveWordsResponse(BaseModel):
    added: int
    skipped: int
    items: list[SensitiveWordItem]


def _item(entry: SensitiveWord) -> SensitiveWordItem:
    return SensitiveWordItem(id=entry.id, word=entry.word, created_at=entry.created_at)


def get_sensitive_word_manager(_actor: AdminActor) -> SensitiveWordManager:
    return SensitiveWordManager(SqlSensitiveWordRepository(), sensitive_word_index)


@router.get("", response_model=SensitiveWordPageModel)
async def list_sensitive_words(
    manager: Annotated[SensitiveWordManager, Depends(get_sensitive_word_manager)],
    actor: AdminActor,
    keyword: Annotated[str | None, Query(max_length=64)] = None,
    page: Annotated[int, Query(ge=1, le=PAGE_MAX)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> SensitiveWordPageModel:
    try:
        result = await manager.list_page(keyword=keyword, page=page, size=size)
    except ValueError as error:
        raise ApiError(400, "INVALID_PARAM", str(error), None) from None
    return SensitiveWordPageModel(total=result.total, items=[_item(item) for item in result.items])


@router.post("", response_model=AddSensitiveWordsResponse, responses={400: ERROR_RESPONSE})
@audited("sensitive_word_add")
async def add_sensitive_words(
    payload: AddSensitiveWordsRequest,
    request: Request,
    manager: Annotated[SensitiveWordManager, Depends(get_sensitive_word_manager)],
    actor: AdminActor,
) -> AddSensitiveWordsResponse:
    try:
        result = await manager.add(payload.words, actor=actor.username)
    except ValueError as error:
        raise ApiError(400, "INVALID_PARAM", str(error), None) from None
    return AddSensitiveWordsResponse(
        added=len(result.created),
        skipped=result.skipped,
        items=[_item(item) for item in result.created],
    )


@router.delete("/{id}", status_code=204, response_class=Response, responses={404: ERROR_RESPONSE})
@audited("sensitive_word_delete")
async def delete_sensitive_word(
    id: int,
    request: Request,
    manager: Annotated[SensitiveWordManager, Depends(get_sensitive_word_manager)],
    actor: AdminActor,
) -> Response:
    if not await manager.delete(id, actor=actor.username):
        raise ApiError(404, "NOT_FOUND", "敏感词不存在", None)
    return Response(status_code=204)
