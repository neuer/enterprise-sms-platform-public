"""显式认证源登录、密码维护、登出与会话接口。"""

from __future__ import annotations

from typing import Annotated, Literal, Self, cast
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyCookie, HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.audit import audited
from app.core.auth.accounts import PlatformAccount
from app.core.auth.runtime import (
    AuthFacade,
    LoginSuccess,
    PasswordChangeRequired,
    get_auth_facade,
)
from app.core.client_ip import trusted_client_ip
from app.core.errors import ApiError
from app.core.origin import canonical_origin

router = APIRouter(prefix="/api/v1/web", tags=["auth"])
bearer_scheme = HTTPBearer(auto_error=False, scheme_name="BearerAuth")
REFRESH_COOKIE_NAME = "sms_refresh_token"
REFRESH_COOKIE_PATH = "/api/v1/web/auth"
refresh_cookie_scheme = APIKeyCookie(
    name=REFRESH_COOKIE_NAME,
    auto_error=False,
    scheme_name="RefreshCookie",
)
ERROR_RESPONSE = {
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "required": ["code", "message", "detail"],
                "properties": {
                    "code": {"type": "string"},
                    "message": {"type": "string"},
                    "detail": {"anyOf": [{"type": "object"}, {"type": "null"}]},
                },
            }
        }
    }
}
NO_STORE_RESPONSE = {
    "headers": {"Cache-Control": {"schema": {"type": "string"}}},
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderResponse(StrictModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    auth_flow: Literal["password", "redirect"]


class PasswordPolicyResponse(StrictModel):
    min_length: Literal[12]
    max_length: Literal[128]
    required_character_classes: Literal[3]
    forbid_username: Literal[True]
    description: str


class LoginRequest(StrictModel):
    provider_code: str = Field(min_length=1, max_length=64)
    username: str = Field(min_length=1, max_length=64)
    session_mode: Literal["refresh", "access_only"]
    tab_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    password: str = Field(
        min_length=1,
        max_length=128,
        json_schema_extra={"writeOnly": True},
    )

    @model_validator(mode="after")
    def validate_session_mode_binding(self) -> Self:
        """refresh 必须绑定 tab_id；access_only 禁止携带，防止事后补参升级。"""

        if self.session_mode == "refresh":
            if self.tab_id is None:
                raise ValueError("refresh 会话必须提供 tab_id")
        elif self.tab_id is not None:
            raise ValueError("access_only 会话不得携带 tab_id")
        return self


class UserResponse(StrictModel):
    account_id: int = Field(ge=1)
    identity_id: int = Field(ge=1)
    provider_code: str
    username: str
    display_name: str
    dept: str
    role: Literal["admin", "approver", "operator", "viewer"]


class RefreshLoginResponse(StrictModel):
    session_mode: Literal["refresh"]
    token: str
    expires_in: int = Field(ge=1, le=900)
    refresh_expires_in: int = Field(ge=1, le=604800)
    user: UserResponse


class AccessOnlyLoginResponse(StrictModel):
    session_mode: Literal["access_only"]
    token: str
    expires_in: int = Field(ge=1, le=900)
    user: UserResponse


LoginResponse = RefreshLoginResponse | AccessOnlyLoginResponse


class RefreshRequest(StrictModel):
    tab_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class PasswordChangeRequiredResponse(StrictModel):
    change_token: str
    expires_in: Literal[600]
    next_action: Literal["change_password"]


class InitialPasswordChangeRequest(StrictModel):
    change_token: str = Field(
        min_length=1,
        max_length=4096,
        json_schema_extra={"writeOnly": True},
    )
    new_password: str = Field(
        min_length=1,
        max_length=128,
        json_schema_extra={"writeOnly": True},
    )


class PasswordChangeRequest(StrictModel):
    current_password: str = Field(
        min_length=1,
        max_length=128,
        json_schema_extra={"writeOnly": True},
    )
    new_password: str = Field(
        min_length=1,
        max_length=128,
        json_schema_extra={"writeOnly": True},
    )


def _client_ip(request: Request) -> str:
    return trusted_client_ip(request)


def _set_refresh_cookie(
    response: Response,
    token: str,
    *,
    secure: bool,
    max_age: int,
) -> None:
    """Refresh Token 只进入 HttpOnly Cookie，前端 JavaScript 不可读取。"""

    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=token,
        max_age=max_age,
        path=REFRESH_COOKIE_PATH,
        secure=secure,
        httponly=True,
        samesite="lax",
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key=REFRESH_COOKIE_NAME,
        path=REFRESH_COOKIE_PATH,
    )


def _bearer(credentials: HTTPAuthorizationCredentials | None) -> str:
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise ApiError(401, "UNAUTHORIZED", "缺少有效的 Bearer 令牌", None)
    return credentials.credentials


def _user_response(user: PlatformAccount) -> UserResponse:
    return UserResponse(
        account_id=user.account_id,
        identity_id=user.identity_id,
        provider_code=user.provider_code,
        username=user.login_name,
        display_name=user.display_name,
        dept=user.dept,
        role=user.role,
    )


def _login_response(result: LoginSuccess) -> LoginResponse:
    """按会话模式返回联合类型；access_only 不得伪装正数 refresh TTL。"""

    user = _user_response(result.user)
    if result.session_mode == "access_only":
        return AccessOnlyLoginResponse(
            session_mode="access_only",
            token=result.token,
            expires_in=result.expires_in,
            user=user,
        )
    return RefreshLoginResponse(
        session_mode="refresh",
        token=result.token,
        expires_in=result.expires_in,
        refresh_expires_in=result.refresh_expires_in,
        user=user,
    )


def _assert_same_origin(request: Request) -> None:
    """Cookie 写请求必须由同源页面发起，拒绝跨站 CSRF。"""

    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        raise ApiError(403, "FORBIDDEN", "缺少同源校验来源", None)
    try:
        source = urlsplit(origin)
        source_port = source.port
    except (TypeError, ValueError):
        raise ApiError(403, "FORBIDDEN", "请求来源非同源", None) from None
    if (
        source.scheme not in {"http", "https"}
        or not source.hostname
        or source.username is not None
        or source.password is not None
    ):
        raise ApiError(403, "FORBIDDEN", "请求来源非同源", None)
    expected_scheme, expected_host, expected_port = canonical_origin(request)
    source_effective_port = source_port or (443 if source.scheme == "https" else 80)
    if (
        source.scheme != expected_scheme
        or source.hostname.casefold() != expected_host
        or source_effective_port != expected_port
    ):
        raise ApiError(403, "FORBIDDEN", "请求来源非同源", None)


@router.get(
    "/auth/providers",
    response_model=list[ProviderResponse],
    responses={200: NO_STORE_RESPONSE, 503: ERROR_RESPONSE},
)
async def list_providers(
    response: Response,
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
) -> list[ProviderResponse]:
    providers = await facade.list_providers()
    response.headers["Cache-Control"] = "no-store"
    return [
        ProviderResponse(
            code=item.code,
            name=item.name,
            auth_flow=cast(Literal["password", "redirect"], item.auth_flow),
        )
        for item in providers
    ]


@router.get(
    "/auth/password-policy",
    response_model=PasswordPolicyResponse,
    responses={200: NO_STORE_RESPONSE},
)
async def password_policy(
    response: Response,
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
) -> PasswordPolicyResponse:
    response.headers["Cache-Control"] = "no-store"
    return PasswordPolicyResponse.model_validate(facade.password_policy())


@router.post(
    "/auth/login",
    response_model=RefreshLoginResponse | AccessOnlyLoginResponse | PasswordChangeRequiredResponse,
    responses={
        200: NO_STORE_RESPONSE,
        401: ERROR_RESPONSE,
        403: ERROR_RESPONSE,
        409: ERROR_RESPONSE,
        423: ERROR_RESPONSE,
        429: {
            **ERROR_RESPONSE,
            "headers": {
                "Retry-After": {"schema": {"type": "integer", "minimum": 1, "maximum": 300}},
            },
        },
        503: ERROR_RESPONSE,
    },
)
@audited("login")
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
) -> LoginResponse | PasswordChangeRequiredResponse:
    result = await facade.login(
        payload.provider_code,
        payload.username,
        payload.password,
        _client_ip(request),
        payload.tab_id,
        request.cookies.get(REFRESH_COOKIE_NAME),
        session_mode=payload.session_mode,
    )
    response.headers["Cache-Control"] = "no-store"
    if isinstance(result, PasswordChangeRequired):
        if payload.session_mode == "access_only":
            _clear_refresh_cookie(response)
        return PasswordChangeRequiredResponse(
            change_token=result.change_token,
            expires_in=600,
            next_action=result.next_action,
        )
    if not isinstance(result, LoginSuccess):
        raise RuntimeError("unsupported login result")
    if result.session_mode == "access_only":
        # 删除旧 Cookie，同时服务端已吊销其 Family；不得再签发新 Refresh。
        _clear_refresh_cookie(response)
        return _login_response(result)
    if not result.refresh_token or result.refresh_expires_in < 1:
        raise RuntimeError("refresh login missing refresh token")
    _set_refresh_cookie(
        response,
        result.refresh_token,
        secure=bool(
            getattr(request.app.state, "settings", None)
            and request.app.state.settings.is_production
        )
        or request.url.scheme == "https",
        max_age=result.refresh_expires_in,
    )
    return _login_response(result)


@router.post(
    "/auth/refresh",
    response_model=RefreshLoginResponse,
    responses={
        200: NO_STORE_RESPONSE,
        401: ERROR_RESPONSE,
        403: ERROR_RESPONSE,
        503: ERROR_RESPONSE,
    },
)
@audited("session_refresh")
async def refresh(
    request: Request,
    response: Response,
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    payload: Annotated[RefreshRequest, Body()],
    refresh_token: Annotated[str | None, Depends(refresh_cookie_scheme)],
) -> RefreshLoginResponse | Response:
    _assert_same_origin(request)
    try:
        if not refresh_token:
            raise ApiError(401, "UNAUTHORIZED", "刷新令牌缺失", None)
        result = await facade.refresh(refresh_token, _client_ip(request), payload.tab_id)
        if (
            result.session_mode != "refresh"
            or not result.refresh_token
            or result.refresh_expires_in < 1
        ):
            raise ApiError(401, "UNAUTHORIZED", "刷新令牌无效或已使用", None)
    except ApiError as error:
        if error.status_code != 401:
            raise
        outcome = JSONResponse(
            status_code=error.status_code,
            content={"code": error.code, "message": error.message, "detail": error.detail},
            headers={"Cache-Control": "no-store"},
        )
        _clear_refresh_cookie(outcome)
        return outcome
    response.headers["Cache-Control"] = "no-store"
    _set_refresh_cookie(
        response,
        result.refresh_token,
        secure=bool(
            getattr(request.app.state, "settings", None)
            and request.app.state.settings.is_production
        )
        or request.url.scheme == "https",
        max_age=result.refresh_expires_in,
    )
    login_response = _login_response(result)
    if not isinstance(login_response, RefreshLoginResponse):
        raise RuntimeError("refresh must return refresh session")
    return login_response


@router.post(
    "/auth/password/initial",
    response_class=Response,
    responses={
        200: NO_STORE_RESPONSE,
        401: ERROR_RESPONSE,
        409: ERROR_RESPONSE,
        422: ERROR_RESPONSE,
        503: ERROR_RESPONSE,
    },
)
@audited("local_password_change")
async def change_initial_password(
    payload: InitialPasswordChangeRequest,
    request: Request,
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
) -> Response:
    await facade.change_initial_password(
        payload.change_token,
        payload.new_password,
        _client_ip(request),
    )
    return Response(status_code=200, headers={"Cache-Control": "no-store"})


@router.post(
    "/auth/password/change",
    response_class=Response,
    responses={
        200: NO_STORE_RESPONSE,
        401: ERROR_RESPONSE,
        409: ERROR_RESPONSE,
        422: ERROR_RESPONSE,
        503: ERROR_RESPONSE,
    },
)
@audited("local_password_change")
async def change_password(
    payload: PasswordChangeRequest,
    request: Request,
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
) -> Response:
    await facade.change_password(
        _bearer(credentials),
        payload.current_password,
        payload.new_password,
        _client_ip(request),
    )
    return Response(status_code=200, headers={"Cache-Control": "no-store"})


@router.post(
    "/auth/logout",
    response_class=Response,
    responses={401: ERROR_RESPONSE, 503: ERROR_RESPONSE},
)
@audited("logout")
async def logout(
    request: Request,
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
) -> Response:
    token = _bearer(credentials)
    _assert_same_origin(request)
    try:
        await facade.logout(
            token,
            _client_ip(request),
            request.cookies.get(REFRESH_COOKIE_NAME),
        )
        outcome: Response = Response(status_code=200)
    except ApiError as error:
        # HttpOnly cookie 无法由前端删除；即使服务端撤销失败也必须终止当前浏览器会话。
        outcome = JSONResponse(
            status_code=error.status_code,
            content={
                "code": error.code,
                "message": error.message,
                "detail": error.detail,
            },
        )
    _clear_refresh_cookie(outcome)
    return outcome
