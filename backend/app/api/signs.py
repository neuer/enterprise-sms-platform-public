"""签名 CRUD、厂商提交与手动状态同步接口。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

from app.api.auth import ERROR_RESPONSE, bearer_scheme
from app.core.audit import audited
from app.core.auth.jwt import JwtClaims
from app.core.auth.runtime import AuthFacade, get_auth_facade
from app.core.errors import ApiError
from app.services.sign_management import (
    SignManagementService,
    SignNotFound,
    SignRecord,
    SignStateConflict,
)
from app.services.sign_repository import SqlSignRepository
from app.settings import get_settings
from app.vendor.zhihui import VendorApiError, VendorError, ZhihuiClient

router = APIRouter(prefix="/api/v1/web/signs", tags=["signs"])


class SignPayload(BaseModel):
    name: str = Field(min_length=1, max_length=20)


class SignModel(BaseModel):
    id: int
    name: str
    vendor_sign_id: str | None
    vendor_state: Literal["pending", "approved", "rejected"]
    vendor_reject_reason: str | None


async def get_sign_service() -> AsyncIterator[SignManagementService]:
    settings = get_settings()
    async with ZhihuiClient.from_settings(settings) as vendor:
        yield SignManagementService(SqlSignRepository(settings), vendor)


async def _user(
    facade: AuthFacade,
    credentials: HTTPAuthorizationCredentials | None,
    *,
    admin: bool,
) -> JwtClaims:
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise ApiError(401, "UNAUTHORIZED", "缺少有效的 Bearer 令牌", None)
    claims = await facade.verify(credentials.credentials)
    allowed = {"admin"} if admin else {"operator", "approver", "admin"}
    if claims.role not in allowed:
        raise ApiError(403, "FORBIDDEN", "当前角色无签名操作权限", None)
    return claims


def _model(record: SignRecord) -> SignModel:
    return SignModel.model_validate(record, from_attributes=True)


def _error(error: Exception) -> ApiError:
    if isinstance(error, SignNotFound):
        return ApiError(404, "NOT_FOUND", str(error), None)
    if isinstance(error, SignStateConflict):
        return ApiError(409, "STATE_CONFLICT", str(error), None)
    if isinstance(error, VendorApiError):
        return ApiError(
            502,
            "VENDOR_ERROR",
            "厂商签名接口返回错误",
            {"vendor_code": error.code, "vendor_message": error.safe_message},
        )
    if isinstance(error, VendorError):
        return ApiError(502, "VENDOR_ERROR", "厂商签名接口不可用", None)
    if isinstance(error, ValueError):
        return ApiError(400, "INVALID_PARAM", str(error), None)
    return ApiError(500, "INTERNAL_ERROR", "服务内部错误", None)


@router.get("", response_model=list[SignModel])
async def list_signs(
    service: Annotated[SignManagementService, Depends(get_sign_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> list[SignModel]:
    await _user(facade, credentials, admin=False)
    return [_model(record) for record in await service.list_all()]


@router.post("", response_model=SignModel, responses={400: ERROR_RESPONSE, 502: ERROR_RESPONSE})
@audited("sign_create")
async def create_sign(
    payload: SignPayload,
    service: Annotated[SignManagementService, Depends(get_sign_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> SignModel:
    claims = await _user(facade, credentials, admin=True)
    try:
        return _model(await service.create(name=payload.name, actor=claims.username))
    except Exception as error:
        raise _error(error) from None


@router.get("/{id}", response_model=SignModel, responses={404: ERROR_RESPONSE})
async def get_sign(
    id: int,
    service: Annotated[SignManagementService, Depends(get_sign_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> SignModel:
    await _user(facade, credentials, admin=False)
    try:
        return _model(await service.get(id))
    except SignNotFound as error:
        raise _error(error) from None


@router.put("/{id}", response_model=SignModel, responses={409: ERROR_RESPONSE})
@audited("sign_update")
async def update_sign(
    id: int,
    payload: SignPayload,
    service: Annotated[SignManagementService, Depends(get_sign_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> SignModel:
    claims = await _user(facade, credentials, admin=True)
    try:
        return _model(await service.update(id, name=payload.name, actor=claims.username))
    except Exception as error:
        raise _error(error) from None


@router.delete("/{id}", status_code=204, response_class=Response, responses={409: ERROR_RESPONSE})
@audited("sign_delete")
async def delete_sign(
    id: int,
    service: Annotated[SignManagementService, Depends(get_sign_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> Response:
    claims = await _user(facade, credentials, admin=True)
    try:
        await service.delete(id, actor=claims.username)
    except Exception as error:
        raise _error(error) from None
    return Response(status_code=204)


@router.post("/{id}/sync", response_class=Response, responses={404: ERROR_RESPONSE})
@audited("sign_sync")
async def sync_sign(
    id: int,
    service: Annotated[SignManagementService, Depends(get_sign_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> Response:
    await _user(facade, credentials, admin=True)
    try:
        await service.sync_pending(id)
    except Exception as error:
        raise _error(error) from None
    return Response(status_code=200)
