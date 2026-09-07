"""真实错误处理器的迟到响应不得修改后续登录的 Cookie。"""

from __future__ import annotations

import httpx
import pytest
from fastapi import Request, Response

from app.api.auth import REFRESH_COOKIE_NAME, REFRESH_COOKIE_PATH, _set_refresh_cookie
from app.core.errors import ApiError, api_error_handler


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code,status",
    [
        ("AUTH_REAUTH_REQUIRED", 401),
        ("AUTH_CONTEXT_CHANGED", 409),
        ("UNAUTHORIZED", 401),
    ],
)
@pytest.mark.parametrize("path", ["/api/v1/web/admin/users", "/api/v1/web/auth/password/change"])
@pytest.mark.parametrize("old_cookie", [False, True])
@pytest.mark.parametrize("new_session", ["same-account-new-session", "other-account-session"])
async def test_late_error_preserves_new_login_cookie(
    code: str,
    status: int,
    path: str,
    old_cookie: bool,
    new_session: str,
) -> None:
    jar = httpx.Cookies()
    url = "https://session.example.invalid"

    def login_cookie(value: str) -> None:
        response = Response()
        _set_refresh_cookie(response, value, secure=True, max_age=600)
        jar.extract_cookies(
            httpx.Response(
                200,
                headers=response.raw_headers,
                request=httpx.Request("POST", url + REFRESH_COOKIE_PATH + "/login"),
            )
        )

    login_cookie("old-session")
    old_request = httpx.Request("GET", url + path)
    jar.set_cookie_header(old_request)
    assert (REFRESH_COOKIE_NAME in old_request.headers.get("cookie", "")) == ("/auth/" in path)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "path": path,
            "query_string": b"",
            "server": ("session.example.invalid", 443),
            "headers": [(b"cookie", b"sms_refresh_token=old-session")] if old_cookie else [],
        }
    )
    login_cookie(new_session)
    error = ApiError(status, code, "synthetic error", {"reason": "synthetic"})
    response = await api_error_handler(request, error)
    late = httpx.Response(
        status, headers=response.raw_headers, content=response.body, request=old_request
    )
    jar.extract_cookies(late)
    assert late.json() == {
        "code": code,
        "message": "synthetic error",
        "detail": {"reason": "synthetic"},
    }
    assert response.status_code == status
    if code != "UNAUTHORIZED":
        assert late.headers["cache-control"] == "no-store"
    assert late.headers.get_list("set-cookie") == []
    refresh = httpx.Request("POST", url + REFRESH_COOKIE_PATH + "/refresh")
    jar.set_cookie_header(refresh)
    assert refresh.headers["cookie"] == f"{REFRESH_COOKIE_NAME}={new_session}"
