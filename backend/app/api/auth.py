"""显式认证源登录、密码维护、登出与会话接口。"""

from __future__ import annotations

from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from app.core.audit import audited
from app.core.auth.runtime import (
    AuthFacade,
    LoginSuccess,
    PasswordChangeRequired,
    get_auth_facade,
)
from app.core.client_ip import trusted_client_ip
from app.core.errors import ApiError

router = APIRouter(prefix="/api/v1/web", tags=["auth"])
bearer_scheme = HTTPBearer(auto_error=False, scheme_name="BearerAuth")
REFRESH_COOKIE_NAME = "sms_refresh_token"
REFRESH_COOKIE_PATH = "/api/v1/web/auth"
ERROR_RESPONSE = {
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "required": ["code", "message", "detail"],
                "properties": {
                    "code": {"type": "string"},
                    "message": {"type": "string"},
                    "detail": {"type": "object", "nullable": True},
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
    password: str = Field(
        min_length=1,
        max_length=128,
        json_schema_extra={"writeOnly": True},
    )


class UserResponse(StrictModel):
    account_id: int = Field(ge=1)
    identity_id: int = Field(ge=1)
    provider_code: str
    username: str
    display_name: str
    dept: str
    role: Literal["admin", "approver", "operator", "viewer"]


class LoginResponse(StrictModel):
    token: str
    refresh_token: str = Field(json_schema_extra={"writeOnly": True})
    expires_in: Literal[900]
    refresh_expires_in: int = Field(ge=1, le=604800)
    user: UserResponse


class RefreshRequest(StrictModel):
    refresh_token: str = Field(
        min_length=1,
        max_length=4096,
        json_schema_extra={"writeOnly": True},
    )


class PasswordChangeRequiredResponse(StrictModel):
    change_token: str
    expires_in: Literal[600]
    next_action: Literal["change_password"]


class InitialPasswordChangeRequest(StrictModel):
    change_token: str = Field(
        min_length=1,
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


def _login_response(result: LoginSuccess) -> LoginResponse:
    user = result.user
    return LoginResponse(
        token=result.token,
        refresh_token=result.refresh_token,
        expires_in=result.expires_in,
        refresh_expires_in=result.refresh_expires_in,
        user=UserResponse(
            account_id=user.account_id,
            identity_id=user.identity_id,
            provider_code=user.provider_code,
            username=user.login_name,
            display_name=user.display_name,
            dept=user.dept,
            role=user.role,
        ),
    )


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
    response_model=LoginResponse | PasswordChangeRequiredResponse,
    responses={
        200: NO_STORE_RESPONSE,
        401: ERROR_RESPONSE,
        403: ERROR_RESPONSE,
        409: ERROR_RESPONSE,
        423: ERROR_RESPONSE,
        429: ERROR_RESPONSE,
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
    )
    response.headers["Cache-Control"] = "no-store"
    if isinstance(result, PasswordChangeRequired):
        return PasswordChangeRequiredResponse(
            change_token=result.change_token,
            expires_in=600,
            next_action=result.next_action,
        )
    if not isinstance(result, LoginSuccess):
        raise RuntimeError("unsupported login result")
    _set_refresh_cookie(
        response,
        result.refresh_token,
        secure=request.url.scheme == "https",
        max_age=result.refresh_expires_in,
    )
    return _login_response(result)


@router.post(
    "/auth/refresh",
    response_model=LoginResponse,
    responses={
        200: NO_STORE_RESPONSE,
        401: ERROR_RESPONSE,
        503: ERROR_RESPONSE,
    },
)
@audited("session_refresh")
async def refresh(
    payload: RefreshRequest,
    request: Request,
    response: Response,
    facade: Annotated[AuthFacade, Depends(get_auth_facade)],
) -> LoginResponse:
    result = await facade.refresh(payload.refresh_token)
    response.headers["Cache-Control"] = "no-store"
    _set_refresh_cookie(
        response,
        result.refresh_token,
        secure=request.url.scheme == "https",
        max_age=result.refresh_expires_in,
    )
    return _login_response(result)


@router.post(
    "/auth/password/initial",
    response_class=Response,
    responses={
        200: NO_STORE_RESPONSE,
        401: ERROR_RESPONSE,
        422: ERROR_RESPONSE,
        500: ERROR_RESPONSE,
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
    await facade.logout(_bearer(credentials), _client_ip(request))
    outcome = Response(status_code=200)
    _clear_refresh_cookie(outcome)
    return outcome
