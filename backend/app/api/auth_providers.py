"""管理员维护认证源草稿、启用状态与外部角色映射。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, NoReturn

from fastapi import APIRouter, Depends, Request
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field

from app.api.auth import ERROR_RESPONSE, bearer_scheme
from app.core.audit import audited
from app.core.auth.providers import create_provider_registry
from app.core.auth.roles import Role
from app.core.auth.runtime import AuthFacade, get_auth_facade
from app.core.auth.users import SqlUserRepository
from app.core.client_ip import trusted_client_ip
from app.core.errors import ApiError
from app.services.auth_provider import (
    AuthProviderService,
    DuplicateRoleMapping,
    ExternalRoleMapping,
    ImmutableProvider,
    InvalidProviderConfig,
    ProviderNotFound,
    ProviderRecord,
    ProviderTestResult,
    StaleProviderDraft,
    UntestedProviderConfig,
)
from app.services.auth_provider_repository import SqlAuthProviderRepository
from app.settings import get_settings

router = APIRouter(prefix="/api/v1/web/admin/auth-providers", tags=["admin"])


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LdapConfigModel(StrictModel):
    server: str = Field(min_length=1, max_length=512)
    base_dn: str = Field(min_length=1, max_length=512)
    bind_dn: str = Field(min_length=1, max_length=512)
    user_search_filter: str = Field(min_length=1, max_length=512)
    username_attribute: str = Field(min_length=1, max_length=64)
    display_name_attribute: str = Field(min_length=1, max_length=64)
    dept_attribute: str = Field(min_length=1, max_length=64)
    subject_attribute: str = Field(min_length=1, max_length=64)
    group_attribute: str = Field(min_length=1, max_length=64)
    connect_timeout_s: float = Field(gt=0, le=30)
    receive_timeout_s: float = Field(gt=0, le=30)


class ProviderDraftUpdateModel(StrictModel):
    config: LdapConfigModel


class ProviderAdminModel(StrictModel):
    code: str
    name: str
    kind: str
    enabled: bool
    draft_config: dict[str, object]
    active_config: dict[str, object] | None
    draft_version: int = Field(ge=1)
    tested_version: int | None
    active_version: int | None
    last_tested_at: datetime | None
    last_test_status: str | None
    bind_secret_available: bool
    ca_available: bool


class ProviderTestResultModel(StrictModel):
    success: bool
    result_code: str


class RoleMappingModel(StrictModel):
    external_group: str = Field(min_length=1, max_length=256)
    role: Role


class RoleMappingsModel(StrictModel):
    mappings: list[RoleMappingModel] = Field(max_length=100)


@dataclass(frozen=True, slots=True)
class ProviderRuntimeStatus:
    """只暴露运行凭据和 CA 文件是否就绪，不暴露路径或内容。"""

    bind_secret_available: bool
    ca_available: bool


def _readable_file(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            stream.read(1)
    except OSError:
        return False
    return True


def get_provider_runtime_status() -> ProviderRuntimeStatus:
    settings = get_settings()
    return ProviderRuntimeStatus(
        bind_secret_available=_readable_file(settings.ldap_bind_password_file),
        ca_available=_readable_file(settings.ldap_ca_certs_file),
    )


def get_auth_provider_admin_service() -> AuthProviderService:
    settings = get_settings()
    repository = SqlAuthProviderRepository(settings)
    registry = create_provider_registry(
        settings=settings,
        provider_repository=repository,
        local_repository=SqlUserRepository(settings),
    )
    return AuthProviderService(repository, registry)


def _token(credentials: HTTPAuthorizationCredentials | None) -> str:
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise ApiError(401, "UNAUTHORIZED", "缺少有效的 Bearer 令牌", None)
    return credentials.credentials


async def _admin(
    request: Request,
    facade: AuthFacade,
    credentials: HTTPAuthorizationCredentials | None,
) -> tuple[str, str]:
    claims = await facade.verify(_token(credentials))
    if claims.role != "admin":
        raise ApiError(403, "FORBIDDEN", "仅管理员可管理认证源", None)
    ip = trusted_client_ip(request)
    return claims.login_name, ip


def _provider_model(
    record: ProviderRecord,
    runtime: ProviderRuntimeStatus,
) -> ProviderAdminModel:
    return ProviderAdminModel(
        code=record.code,
        name=record.name,
        kind=record.kind,
        enabled=record.enabled,
        draft_config=record.draft_config,
        active_config=record.active_config,
        draft_version=record.draft_version,
        tested_version=record.tested_version,
        active_version=record.active_version,
        last_tested_at=record.last_tested_at,
        last_test_status=record.last_test_status,
        bind_secret_available=runtime.bind_secret_available,
        ca_available=runtime.ca_available,
    )


def _test_model(result: ProviderTestResult) -> ProviderTestResultModel:
    return ProviderTestResultModel(
        success=result.success,
        result_code=result.result_code,
    )


def _mappings_model(mappings: tuple[ExternalRoleMapping, ...]) -> RoleMappingsModel:
    return RoleMappingsModel(
        mappings=[
            RoleMappingModel(external_group=item.external_group, role=item.role)
            for item in mappings
        ]
    )


def _raise_provider_error(error: Exception) -> NoReturn:
    if isinstance(error, ProviderNotFound):
        raise ApiError(404, "NOT_FOUND", "认证源不存在", None) from None
    if isinstance(error, ImmutableProvider):
        raise ApiError(409, "STATE_CONFLICT", str(error), None) from None
    if isinstance(error, InvalidProviderConfig):
        raise ApiError(422, "INVALID_PROVIDER_CONFIG", str(error), None) from None
    if isinstance(error, DuplicateRoleMapping):
        raise ApiError(409, "STATE_CONFLICT", str(error), None) from None
    if isinstance(error, UntestedProviderConfig):
        raise ApiError(
            409,
            "PROVIDER_CONFIG_UNTESTED",
            "当前认证源草稿尚未通过测试",
            None,
        ) from None
    if isinstance(error, StaleProviderDraft):
        raise ApiError(
            409,
            "PROVIDER_CONFIG_STALE",
            "测试期间认证源草稿已改变，请重新测试",
            None,
        ) from None
    raise error


PROVIDER_ERRORS = (
    ProviderNotFound,
    ImmutableProvider,
    InvalidProviderConfig,
    DuplicateRoleMapping,
    UntestedProviderConfig,
    StaleProviderDraft,
)


@router.get(
    "/{provider_code}",
    response_model=ProviderAdminModel,
    responses={401: ERROR_RESPONSE, 403: ERROR_RESPONSE, 404: ERROR_RESPONSE},
)
async def get_provider(
    provider_code: str,
    request: Request,
    service: Annotated[AuthProviderService, Depends(get_auth_provider_admin_service)],
    runtime: Annotated[ProviderRuntimeStatus, Depends(get_provider_runtime_status)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> ProviderAdminModel:
    await _admin(request, facade, credentials)
    try:
        return _provider_model(await service.get(provider_code), runtime)
    except PROVIDER_ERRORS as error:
        _raise_provider_error(error)


@router.put(
    "/{provider_code}/draft",
    response_model=ProviderAdminModel,
    responses={
        401: ERROR_RESPONSE,
        403: ERROR_RESPONSE,
        404: ERROR_RESPONSE,
        409: ERROR_RESPONSE,
        422: ERROR_RESPONSE,
    },
)
@audited("auth_provider_save_draft")
async def save_provider_draft(
    provider_code: str,
    payload: ProviderDraftUpdateModel,
    request: Request,
    service: Annotated[AuthProviderService, Depends(get_auth_provider_admin_service)],
    runtime: Annotated[ProviderRuntimeStatus, Depends(get_provider_runtime_status)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> ProviderAdminModel:
    actor, ip = await _admin(request, facade, credentials)
    try:
        saved = await service.save_draft(
            provider_code,
            payload.config.model_dump(),
            actor=actor,
            ip=ip,
        )
        return _provider_model(saved, runtime)
    except PROVIDER_ERRORS as error:
        _raise_provider_error(error)


@router.post(
    "/{provider_code}/test",
    response_model=ProviderTestResultModel,
    responses={
        401: ERROR_RESPONSE,
        403: ERROR_RESPONSE,
        404: ERROR_RESPONSE,
        409: ERROR_RESPONSE,
        422: ERROR_RESPONSE,
    },
)
@audited("auth_provider_test")
async def test_provider_draft(
    provider_code: str,
    request: Request,
    service: Annotated[AuthProviderService, Depends(get_auth_provider_admin_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> ProviderTestResultModel:
    actor, ip = await _admin(request, facade, credentials)
    try:
        return _test_model(await service.test_draft(provider_code, actor=actor, ip=ip))
    except PROVIDER_ERRORS as error:
        _raise_provider_error(error)


async def _set_provider_enabled(
    *,
    provider_code: str,
    enabled: bool,
    request: Request,
    service: AuthProviderService,
    runtime: ProviderRuntimeStatus,
    facade: AuthFacade,
    credentials: HTTPAuthorizationCredentials | None,
) -> ProviderAdminModel:
    actor, ip = await _admin(request, facade, credentials)
    try:
        record = (
            await service.activate(provider_code, actor=actor, ip=ip)
            if enabled
            else await service.disable(provider_code, actor=actor, ip=ip)
        )
        return _provider_model(record, runtime)
    except PROVIDER_ERRORS as error:
        _raise_provider_error(error)


@router.post(
    "/{provider_code}/activate",
    response_model=ProviderAdminModel,
    responses={401: ERROR_RESPONSE, 403: ERROR_RESPONSE, 404: ERROR_RESPONSE, 409: ERROR_RESPONSE},
)
@audited("auth_provider_activate")
async def activate_provider(
    provider_code: str,
    request: Request,
    service: Annotated[AuthProviderService, Depends(get_auth_provider_admin_service)],
    runtime: Annotated[ProviderRuntimeStatus, Depends(get_provider_runtime_status)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> ProviderAdminModel:
    return await _set_provider_enabled(
        provider_code=provider_code,
        enabled=True,
        request=request,
        service=service,
        runtime=runtime,
        facade=facade,
        credentials=credentials,
    )


@router.post(
    "/{provider_code}/disable",
    response_model=ProviderAdminModel,
    responses={401: ERROR_RESPONSE, 403: ERROR_RESPONSE, 404: ERROR_RESPONSE, 409: ERROR_RESPONSE},
)
@audited("auth_provider_disable")
async def disable_provider(
    provider_code: str,
    request: Request,
    service: Annotated[AuthProviderService, Depends(get_auth_provider_admin_service)],
    runtime: Annotated[ProviderRuntimeStatus, Depends(get_provider_runtime_status)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> ProviderAdminModel:
    return await _set_provider_enabled(
        provider_code=provider_code,
        enabled=False,
        request=request,
        service=service,
        runtime=runtime,
        facade=facade,
        credentials=credentials,
    )


@router.get(
    "/{provider_code}/role-mappings",
    response_model=RoleMappingsModel,
    responses={401: ERROR_RESPONSE, 403: ERROR_RESPONSE, 404: ERROR_RESPONSE, 409: ERROR_RESPONSE},
)
async def list_provider_role_mappings(
    provider_code: str,
    request: Request,
    service: Annotated[AuthProviderService, Depends(get_auth_provider_admin_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> RoleMappingsModel:
    await _admin(request, facade, credentials)
    try:
        return _mappings_model(await service.list_role_mappings(provider_code))
    except PROVIDER_ERRORS as error:
        _raise_provider_error(error)


@router.put(
    "/{provider_code}/role-mappings",
    response_model=RoleMappingsModel,
    responses={
        401: ERROR_RESPONSE,
        403: ERROR_RESPONSE,
        404: ERROR_RESPONSE,
        409: ERROR_RESPONSE,
        422: ERROR_RESPONSE,
    },
)
@audited("auth_provider_role_mappings_replace")
async def replace_provider_role_mappings(
    provider_code: str,
    payload: RoleMappingsModel,
    request: Request,
    service: Annotated[AuthProviderService, Depends(get_auth_provider_admin_service)],
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> RoleMappingsModel:
    actor, ip = await _admin(request, facade, credentials)
    try:
        mappings = tuple(
            ExternalRoleMapping(item.external_group, item.role) for item in payload.mappings
        )
        return _mappings_model(
            await service.replace_role_mappings(
                provider_code,
                mappings,
                actor=actor,
                ip=ip,
            )
        )
    except PROVIDER_ERRORS as error:
        _raise_provider_error(error)
