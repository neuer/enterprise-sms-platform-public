"""签名 CRUD、厂商提交与手动状态同步接口。"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field

from app.api.auth import ERROR_RESPONSE, bearer_scheme
from app.core.audit import audited
from app.core.auth.jwt import JwtClaims
from app.core.auth.runtime import AuthFacade, get_auth_facade
from app.core.client_ip import trusted_client_ip
from app.core.errors import ApiError
from app.services.ops_dispatch import (
    OutboxJobSender,
    SignAdoptionSender,
    SignAdoptionUnavailable,
)
from app.services.sign_management import (
    SignManagementService,
    SignNotFound,
    SignRecord,
    SignStateConflict,
)
from app.services.sign_repository import SqlSignRepository
from app.settings import get_settings

router = APIRouter(prefix="/api/v1/web/signs", tags=["signs"])


class SignPayload(BaseModel):
    name: str = Field(min_length=1, max_length=20)


class SignAdoptionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vendor_sign_id: int = Field(strict=True, ge=1, le=2_147_483_647)
    confirmed_name: str = Field(min_length=1, max_length=20)


class SignModel(BaseModel):
    id: int
    name: str
    vendor_sign_id: str | None
    vendor_state: Literal["pending", "approved", "rejected"]
    vendor_reject_reason: str | None


def get_sign_service() -> SignManagementService:
    settings = get_settings()
    return SignManagementService(SqlSignRepository(settings))


def get_sign_job_sender() -> OutboxJobSender:
    return OutboxJobSender(get_settings())


def get_sign_adoption_sender() -> SignAdoptionSender:
    return SignAdoptionSender(get_settings())


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
    if isinstance(error, SignAdoptionUnavailable):
        return ApiError(
            503,
            "DEPENDENCY_UNAVAILABLE",
            "关联意图暂时无法安全持久化",
            None,
        )
    if isinstance(error, SignNotFound):
        return ApiError(404, "NOT_FOUND", str(error), None)
    if isinstance(error, SignStateConflict):
        return ApiError(409, "STATE_CONFLICT", str(error), None)
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


@router.post("", response_model=SignModel, responses={400: ERROR_RESPONSE})
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


@router.post(
    "/{id}/adopt-existing",
    status_code=202,
    response_class=Response,
    responses={
        400: ERROR_RESPONSE,
        401: ERROR_RESPONSE,
        403: ERROR_RESPONSE,
        404: ERROR_RESPONSE,
        409: ERROR_RESPONSE,
        500: ERROR_RESPONSE,
        503: ERROR_RESPONSE,
    },
)
@audited("sign_adopt")
async def adopt_existing_sign(
    id: int,
    payload: SignAdoptionPayload,
    request: Request,
    service: Annotated[SignManagementService, Depends(get_sign_service)],
    sender: Annotated[SignAdoptionSender, Depends(get_sign_adoption_sender)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> Response:
    claims = await _user(facade, credentials, admin=True)
    try:
        current = await service.prepare_adoption(id, confirmed_name=payload.confirmed_name)
        await sender.send_sign(
            current.id,
            payload.vendor_sign_id,
            principal=claims.principal,
            ip=trusted_client_ip(request),
        )
    except Exception as error:
        raise _error(error) from None
    return Response(status_code=202)


@router.post(
    "/{id}/sync",
    status_code=202,
    response_class=Response,
    responses={404: ERROR_RESPONSE, 409: ERROR_RESPONSE},
)
@audited("sign_sync")
async def sync_sign(
    id: int,
    service: Annotated[SignManagementService, Depends(get_sign_service)],
    sender: Annotated[OutboxJobSender, Depends(get_sign_job_sender)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> Response:
    await _user(facade, credentials, admin=True)
    try:
        current = await service.get(id)
        if current.vendor_state != "pending":
            raise SignStateConflict("仅待审核签名可同步")
        await sender.send("app.tasks.sync_signs", "realtime")
    except Exception as error:
        raise _error(error) from None
    return Response(status_code=202)
