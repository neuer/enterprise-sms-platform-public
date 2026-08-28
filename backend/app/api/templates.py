"""模板 CRUD、厂商提交与手动状态同步接口。"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

from app.api.auth import ERROR_RESPONSE, bearer_scheme
from app.core.audit import audited
from app.core.auth.jwt import JwtClaims
from app.core.auth.runtime import AuthFacade, get_auth_facade
from app.core.client_ip import trusted_client_ip
from app.core.errors import ApiError
from app.services.ops_dispatch import TemplateSyncSender
from app.services.sensitive_read_audit import (
    SensitiveReadAuditor,
    get_sensitive_read_auditor,
)
from app.services.template import TemplateParamMismatch
from app.services.template_management import (
    TemplateManagementService,
    TemplateNotFound,
    TemplateRecord,
    TemplateStateConflict,
)
from app.services.template_repository import SqlTemplateRepository
from app.settings import get_settings

router = APIRouter(prefix="/api/v1/web/templates", tags=["templates"])


class VarSpecModel(BaseModel):
    pos: int = Field(ge=1)
    max_len: int = Field(ge=1, le=100)


class TemplatePayload(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=500)
    var_specs: list[VarSpecModel] = Field(default_factory=list)


class TemplateUpdatePayload(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=500)
    var_specs: list[VarSpecModel]


class TemplateModel(BaseModel):
    id: int
    name: str
    content: str
    var_specs: list[VarSpecModel]
    dept: str
    vendor_template_id: str | None
    vendor_state: Literal["draft", "pending", "approved", "rejected"]
    vendor_reject_reason: str | None


def get_template_service() -> TemplateManagementService:
    settings = get_settings()
    return TemplateManagementService(SqlTemplateRepository(settings))


def get_template_job_sender() -> TemplateSyncSender:
    return TemplateSyncSender(get_settings())


async def _user(
    facade: AuthFacade,
    credentials: HTTPAuthorizationCredentials | None,
    *,
    write: bool,
) -> JwtClaims:
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise ApiError(401, "UNAUTHORIZED", "缺少有效的 Bearer 令牌", None)
    claims = await facade.verify(credentials.credentials)
    allowed = {"operator", "approver", "admin"} if not write else {"operator", "admin"}
    if claims.role not in allowed:
        raise ApiError(403, "FORBIDDEN", "当前角色无模板操作权限", None)
    return claims


def _model(record: TemplateRecord) -> TemplateModel:
    return TemplateModel.model_validate(record, from_attributes=True)


def _error(error: Exception) -> ApiError:
    if isinstance(error, TemplateNotFound):
        return ApiError(404, "NOT_FOUND", str(error), None)
    if isinstance(error, TemplateStateConflict):
        return ApiError(409, "STATE_CONFLICT", str(error), None)
    if isinstance(error, TemplateParamMismatch):
        return ApiError(422, error.code, str(error), error.detail)
    if isinstance(error, ValueError):
        return ApiError(400, "INVALID_PARAM", str(error), None)
    return ApiError(500, "INTERNAL_ERROR", "服务内部错误", None)


@router.get("", response_model=list[TemplateModel])
async def list_templates(
    request: Request,
    auditor: Annotated[SensitiveReadAuditor, Depends(get_sensitive_read_auditor)],
    service: Annotated[TemplateManagementService, Depends(get_template_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> list[TemplateModel]:
    claims = await _user(facade, credentials, write=False)
    records = await service.list_all(dept=None if claims.role == "admin" else claims.dept)
    await auditor.record(
        action="template_content_read",
        object_type="template_page",
        object_id="list",
        ip=trusted_client_ip(request),
        count=len(records),
    )
    return [_model(record) for record in records]


@router.post("", response_model=TemplateModel, responses={422: ERROR_RESPONSE})
@audited("template_create")
async def create_template(
    payload: TemplatePayload,
    service: Annotated[TemplateManagementService, Depends(get_template_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> TemplateModel:
    claims = await _user(facade, credentials, write=True)
    try:
        return _model(
            await service.create(
                name=payload.name,
                content=payload.content,
                var_specs=[item.model_dump() for item in payload.var_specs],
                dept=claims.dept,
                actor=claims.username,
            )
        )
    except Exception as error:
        raise _error(error) from None


@router.get("/{id}", response_model=TemplateModel, responses={404: ERROR_RESPONSE})
async def get_template(
    id: int,
    request: Request,
    auditor: Annotated[SensitiveReadAuditor, Depends(get_sensitive_read_auditor)],
    service: Annotated[TemplateManagementService, Depends(get_template_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> TemplateModel:
    claims = await _user(facade, credentials, write=False)
    try:
        record = await service.get(id, dept=None if claims.role == "admin" else claims.dept)
    except TemplateNotFound as error:
        raise _error(error) from None
    await auditor.record(
        action="template_content_read",
        object_type="template",
        object_id=str(id),
        ip=trusted_client_ip(request),
        count=1,
    )
    return _model(record)


@router.put("/{id}", response_model=TemplateModel, responses={409: ERROR_RESPONSE})
@audited("template_update")
async def update_template(
    id: int,
    payload: TemplateUpdatePayload,
    service: Annotated[TemplateManagementService, Depends(get_template_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> TemplateModel:
    claims = await _user(facade, credentials, write=True)
    try:
        current = await service.get(id, dept=None if claims.role == "admin" else claims.dept)
        return _model(
            await service.update(
                current.id,
                name=payload.name,
                content=payload.content,
                var_specs=[item.model_dump() for item in payload.var_specs],
                actor=claims.username,
            )
        )
    except Exception as error:
        raise _error(error) from None


@router.delete("/{id}", status_code=204, response_class=Response, responses={409: ERROR_RESPONSE})
@audited("template_delete")
async def delete_template(
    id: int,
    service: Annotated[TemplateManagementService, Depends(get_template_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> Response:
    claims = await _user(facade, credentials, write=True)
    try:
        current = await service.get(id, dept=None if claims.role == "admin" else claims.dept)
        await service.delete(current.id, actor=claims.username)
    except Exception as error:
        raise _error(error) from None
    return Response(status_code=204)


@router.post(
    "/{id}/sync",
    status_code=202,
    response_class=Response,
    responses={404: ERROR_RESPONSE, 409: ERROR_RESPONSE},
)
@audited("template_sync")
async def sync_template(
    id: int,
    service: Annotated[TemplateManagementService, Depends(get_template_service)],
    sender: Annotated[TemplateSyncSender, Depends(get_template_job_sender)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> Response:
    claims = await _user(facade, credentials, write=True)
    try:
        current = await service.get(id, dept=None if claims.role == "admin" else claims.dept)
        if (
            current.vendor_state not in {"pending", "rejected"}
            or current.vendor_template_id is None
        ):
            raise TemplateStateConflict("仅待审核或已拒绝且已有厂商编号的模板可同步")
        await sender.send_template(current.id)
        return Response(status_code=202)
    except Exception as error:
        raise _error(error) from None
