"""Refresh Cookie 的环境、迁移与重复输入合同。"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request, Response
from fastapi.security import APIKeyCookie

REFRESH_COOKIE_NAME = "sms_refresh_token"
PRODUCTION_REFRESH_COOKIE_NAME = "__Secure-sms_refresh_token"
REFRESH_COOKIE_PATH = "/api/v1/web/auth"
REFRESH_COOKIE_DESCRIPTION = (
    "生产 HTTPS 使用 __Secure-sms_refresh_token；开发/测试使用 sms_refresh_token。"
    "HttpOnly、SameSite=Lax、Path=/api/v1/web/auth，不设置 Domain。"
    "生产旧名不得刷新，须重新登录；重复同名或新旧并存拒绝，不回退凭据。"
)


@dataclass(frozen=True)
class RefreshCookiePolicy:
    name: str
    secure: bool
    production: bool

    @classmethod
    def for_request(cls, request: Request) -> RefreshCookiePolicy:
        """只按服务端环境选名称，代理归一化后的 HTTPS 决定开发 Secure。"""

        production = bool(
            getattr(getattr(request.app.state, "settings", None), "is_production", False)
        )
        return cls(
            PRODUCTION_REFRESH_COOKIE_NAME if production else REFRESH_COOKIE_NAME,
            production or request.url.scheme == "https",
            production,
        )

    def set(self, response: Response, token: str, *, max_age: int) -> None:
        """签发当前名称；生产成功登录/刷新同时清理旧名称。"""

        if self.production:
            self._delete(response, REFRESH_COOKIE_NAME)
        response.set_cookie(
            key=self.name, value=token, max_age=max_age, path=REFRESH_COOKIE_PATH,
            secure=self.secure, httponly=True, samesite="lax",
        )

    def _delete(self, response: Response, name: str) -> None:
        response.delete_cookie(
            key=name, path=REFRESH_COOKIE_PATH,
            secure=self.secure or name == PRODUCTION_REFRESH_COOKIE_NAME,
            httponly=True, samesite="lax",
        )

    def clear(self, response: Response) -> None:
        """认证入口清理两个名称，前缀删除始终符合 Secure 要求。"""

        self._delete(response, REFRESH_COOKIE_NAME)
        self._delete(response, PRODUCTION_REFRESH_COOKIE_NAME)


@dataclass(frozen=True)
class RefreshCookieInput:
    current: str | None
    prior: str | None
    ambiguous: bool


def read_refresh_cookie(request: Request) -> RefreshCookieInput:
    """在框架合并 Cookie 前识别跨 Header、Path、Domain 的重复凭据。"""

    policy = RefreshCookiePolicy.for_request(request)
    values: dict[str, list[str]] = {REFRESH_COOKIE_NAME: [], PRODUCTION_REFRESH_COOKIE_NAME: []}
    for header in request.headers.getlist("cookie"):
        for part in header.split(";"):
            name, separator, value = part.strip().partition("=")
            name = name.strip()
            if name in values:
                values[name].append(value if separator else "")
    if sum(len(items) for items in values.values()) > 1:
        return RefreshCookieInput(None, None, True)
    current_values = values[policy.name]
    current = (current_values[0] or None) if current_values else None
    # 旧值仅供已重新验证口令的登录流程撤销 prior family，不作为刷新凭据。
    prior = current
    if policy.production and values[REFRESH_COOKIE_NAME]:
        prior = values[REFRESH_COOKIE_NAME][0] or None
    if policy.production and request.url.scheme != "https":
        current = None
    return RefreshCookieInput(current, prior, False)


class RefreshCookieScheme(APIKeyCookie):
    def __init__(self) -> None:
        super().__init__(
            name=PRODUCTION_REFRESH_COOKIE_NAME, auto_error=False,
            scheme_name="RefreshCookie", description=REFRESH_COOKIE_DESCRIPTION,
        )

    async def __call__(self, request: Request) -> str | None:
        """无效与歧义输入交由 refresh 的受控 401 分支统一清理。"""

        return read_refresh_cookie(request).current
