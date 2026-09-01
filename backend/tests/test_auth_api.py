from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.api.auth as auth_api
from app.core.auth.accounts import PlatformAccount
from app.core.auth.backends import AuthenticatedIdentity
from app.core.auth.jwt import JwtService
from app.core.auth.runtime import (
    AuthFacade,
    LoginSuccess,
    PasswordChangeRequired,
    get_auth_facade,
)
from app.core.errors import ApiError
from app.main import create_app
from app.services.auth_provider import ProviderSummary
from app.settings import Settings
from tests.test_auth import FakeKeyValue
from tests.test_auth_runtime import FakeAuthService, FakeHasher, FakeUserRepository

TAB_ID = "c" * 32


def user() -> PlatformAccount:
    return PlatformAccount(
        account_id=8,
        identity_id=18,
        provider_code="local",
        login_name="operator01",
        normalized_login_name="operator01",
        display_name="测试用户",
        dept="研发部",
        role="operator",
        security_version=3,
        account_enabled=True,
        identity_enabled=True,
    )


class FakeAuthFacade:
    def __init__(self) -> None:
        self.login_calls: list[tuple[str, str, str, str, str, str | None]] = []
        self.initial_changes: list[tuple[str, str, str]] = []
        self.daily_changes: list[tuple[str, str, str, str]] = []
        self.logout_tokens: list[str] = []
        self.logout_calls: list[tuple[str, str, str | None]] = []
        self.force_calls: list[tuple[str, str, str]] = []
        self.refresh_calls: list[tuple[str, str, str]] = []

    async def list_providers(self) -> tuple[ProviderSummary, ...]:
        return (
            ProviderSummary("local", "本地账号", "password"),
            ProviderSummary("ad", "AD 账号", "password"),
        )

    def password_policy(self) -> dict[str, int | bool | str]:
        return {
            "min_length": 12,
            "max_length": 128,
            "required_character_classes": 3,
            "forbid_username": True,
            "description": "12–128 位，至少包含大小写字母、数字、特殊字符中的三类，不能包含用户名",
        }

    async def login(
        self,
        provider_code: str,
        username: str,
        password: str,
        ip: str,
        tab_id: str,
        prior_refresh_token: str | None = None,
    ) -> LoginSuccess | PasswordChangeRequired:
        self.login_calls.append(
            (provider_code, username, password, ip, tab_id, prior_refresh_token)
        )
        if provider_code == "disabled":
            raise ApiError(403, "AUTH_PROVIDER_DISABLED", "所选认证源未启用", None)
        if password == "temporary":
            return PasswordChangeRequired("change.jwt")
        if password != "correct":
            raise ApiError(401, "UNAUTHORIZED", "用户名或密码错误", None)
        return LoginSuccess(
            "signed.jwt",
            "refresh.jwt",
            900,
            604800,
            user(),
        )

    async def refresh(self, refresh_token: str, ip: str, tab_id: str) -> LoginSuccess:
        self.refresh_calls.append((refresh_token, ip, tab_id))
        if refresh_token != "refresh.jwt":
            raise ApiError(401, "UNAUTHORIZED", "刷新令牌无效或已使用", None)
        return LoginSuccess(
            "rotated.jwt",
            "rotated-refresh.jwt",
            900,
            604800,
            user(),
        )

    async def change_initial_password(
        self,
        token: str,
        new_password: str,
        ip: str,
    ) -> None:
        self.initial_changes.append((token, new_password, ip))

    async def change_password(
        self,
        token: str,
        current_password: str,
        new_password: str,
        ip: str,
    ) -> None:
        self.daily_changes.append((token, current_password, new_password, ip))

    async def logout(
        self,
        token: str,
        ip: str,
        refresh_token: str | None = None,
    ) -> None:
        self.logout_tokens.append(token)
        self.logout_calls.append((token, ip, refresh_token))

    async def force_logout(self, token: str, username: str, ip: str) -> None:
        self.force_calls.append((token, username, ip))


def client(facade: FakeAuthFacade) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_auth_facade] = lambda: facade
    return TestClient(app)


def test_public_provider_and_password_policy_contracts_are_no_store() -> None:
    response_client = client(FakeAuthFacade())

    providers = response_client.get("/api/v1/web/auth/providers")
    policy = response_client.get("/api/v1/web/auth/password-policy")

    assert providers.status_code == 200
    assert providers.headers["cache-control"] == "no-store"
    assert providers.json() == [
        {"code": "local", "name": "本地账号", "auth_flow": "password"},
        {"code": "ad", "name": "AD 账号", "auth_flow": "password"},
    ]
    assert policy.status_code == 200
    assert policy.headers["cache-control"] == "no-store"
    assert policy.json()["min_length"] == 12
    assert policy.json()["required_character_classes"] == 3


def test_login_requires_explicit_provider_and_returns_account_identity_fields() -> None:
    facade = FakeAuthFacade()
    response_client = client(facade)

    success = response_client.post(
        "/api/v1/web/auth/login",
        json={
            "provider_code": "local",
            "username": "operator01",
            "password": "correct",
            "tab_id": TAB_ID,
        },
    )

    assert success.status_code == 200
    assert success.headers["cache-control"] == "no-store"
    assert success.json() == {
        "token": "signed.jwt",
        "expires_in": 900,
        "refresh_expires_in": 604800,
        "user": {
            "account_id": 8,
            "identity_id": 18,
            "provider_code": "local",
            "username": "operator01",
            "display_name": "测试用户",
            "dept": "研发部",
            "role": "operator",
        },
    }
    login_cookie = success.headers.get("set-cookie", "")
    assert "sms_refresh_token=refresh.jwt" in login_cookie
    assert "HttpOnly" in login_cookie
    assert "Path=/api/v1/web/auth" in login_cookie
    assert "SameSite=lax" in login_cookie
    assert facade.login_calls[0][:3] == ("local", "operator01", "correct")
    assert facade.login_calls[0][5] is None

    refreshed = response_client.post(
        "/api/v1/web/auth/refresh",
        headers={"Origin": "http://testserver"},
        json={"tab_id": TAB_ID},
    )
    assert refreshed.status_code == 200
    assert refreshed.headers["cache-control"] == "no-store"
    assert refreshed.json()["token"] == "rotated.jwt"
    assert "refresh_token" not in refreshed.json()
    refreshed_cookie = refreshed.headers.get("set-cookie", "")
    assert "sms_refresh_token=rotated-refresh.jwt" in refreshed_cookie
    assert "HttpOnly" in refreshed_cookie
    assert vars(auth_api.refresh)["__audited_action__"] == "session_refresh"

    missing_tab = response_client.post(
        "/api/v1/web/auth/refresh",
        headers={"Origin": "http://testserver"},
        json={},
    )
    assert missing_tab.status_code == 400
    assert missing_tab.json()["code"] == "INVALID_PARAM"

    missing_source = response_client.post(
        "/api/v1/web/auth/login",
        json={"username": "operator01", "password": "correct"},
    )
    assert missing_source.status_code == 400
    assert missing_source.json()["code"] == "INVALID_PARAM"


def test_temporary_login_and_both_password_change_endpoints() -> None:
    facade = FakeAuthFacade()
    response_client = client(facade)

    login = response_client.post(
        "/api/v1/web/auth/login",
        json={
            "provider_code": "local",
            "username": "operator01",
            "password": "temporary",
            "tab_id": TAB_ID,
        },
    )

    assert login.status_code == 200
    assert login.json() == {
        "change_token": "change.jwt",
        "expires_in": 600,
        "next_action": "change_password",
    }
    assert "token" not in login.json() and "user" not in login.json()


def test_cookie_refresh_rejects_cross_origin_request() -> None:
    facade = FakeAuthFacade()
    response_client = client(facade)
    response_client.post(
        "/api/v1/web/auth/login",
        json={
            "provider_code": "local",
            "username": "operator01",
            "password": "correct",
            "tab_id": TAB_ID,
        },
    )

    denied = response_client.post(
        "/api/v1/web/auth/refresh",
        headers={"Origin": "http://evil.example"},
        json={"tab_id": TAB_ID},
    )
    assert denied.status_code == 403
    assert denied.json()["code"] == "FORBIDDEN"


@pytest.mark.parametrize(
    "origin",
    [
        "http://testserver:99999",
        "http://testserver:not-a-port",
        "http://user:pass@testserver",
        "javascript:alert(1)",
        "http://",
    ],
)
def test_cookie_refresh_rejects_malformed_origin_with_stable_403(origin: str) -> None:
    facade = FakeAuthFacade()
    response_client = client(facade)
    response_client.post(
        "/api/v1/web/auth/login",
        json={
            "provider_code": "local",
            "username": "operator01",
            "password": "correct",
            "tab_id": TAB_ID,
        },
    )

    denied = response_client.post(
        "/api/v1/web/auth/refresh",
        headers={"Origin": origin},
        json={"tab_id": TAB_ID},
    )
    assert denied.status_code == 403
    assert denied.json()["code"] == "FORBIDDEN"

    initial = response_client.post(
        "/api/v1/web/auth/password/initial",
        json={"change_token": "change.jwt", "new_password": "New@Password123"},
    )
    daily = response_client.post(
        "/api/v1/web/auth/password/change",
        headers={"Authorization": "Bearer current.jwt"},
        json={
            "current_password": "Current@Password123",
            "new_password": "Daily@Password456",
        },
    )

    assert initial.status_code == 200 and initial.headers["cache-control"] == "no-store"
    assert daily.status_code == 200 and daily.headers["cache-control"] == "no-store"
    assert facade.initial_changes[0][:2] == ("change.jwt", "New@Password123")
    assert facade.daily_changes[0][:3] == (
        "current.jwt",
        "Current@Password123",
        "Daily@Password456",
    )
    assert vars(auth_api.change_initial_password)["__audited_action__"] == ("local_password_change")


def test_login_uses_standard_error_envelope_for_credentials_and_provider_state() -> None:
    response_client = client(FakeAuthFacade())

    failed = response_client.post(
        "/api/v1/web/auth/login",
        json={
            "provider_code": "local",
            "username": "unknown",
            "password": "wrong",
            "tab_id": TAB_ID,
        },
    )
    disabled = response_client.post(
        "/api/v1/web/auth/login",
        json={
            "provider_code": "disabled",
            "username": "unknown",
            "password": "correct",
            "tab_id": TAB_ID,
        },
    )

    assert failed.status_code == 401
    assert failed.json() == {
        "code": "UNAUTHORIZED",
        "message": "用户名或密码错误",
        "detail": None,
    }
    assert disabled.status_code == 403
    assert disabled.json()["code"] == "AUTH_PROVIDER_DISABLED"


def test_logout_requires_authorization_and_legacy_revoke_route_is_removed() -> None:
    facade = FakeAuthFacade()
    response_client = client(facade)

    assert response_client.post("/api/v1/web/auth/logout").status_code == 401
    response_client.cookies.set("sms_refresh_token", "refresh.jwt")
    logged_out = response_client.post(
        "/api/v1/web/auth/logout",
        headers={
            "Authorization": "Bearer current.jwt",
            "Origin": "http://testserver",
        },
    )
    assert logged_out.status_code == 200
    cleared = logged_out.headers.get("set-cookie", "")
    assert "sms_refresh_token=" in cleared
    assert "Max-Age=0" in cleared
    assert "Path=/api/v1/web/auth" in cleared
    assert facade.logout_tokens == ["current.jwt"]
    assert facade.logout_calls == [("current.jwt", "0.0.0.0", "refresh.jwt")]

    response = response_client.post(
        "/api/v1/web/admin/sessions/viewer01/revoke",
        headers={"Authorization": "Bearer admin.jwt"},
    )
    assert response.status_code == 404
    assert facade.force_calls == []


def test_logout_failure_still_expires_browser_refresh_cookie() -> None:
    class FailingLogoutFacade(FakeAuthFacade):
        async def logout(
            self,
            token: str,
            ip: str,
            refresh_token: str | None = None,
        ) -> None:
            raise ApiError(503, "AUTH_SESSION_UNAVAILABLE", "会话撤销暂不可用", None)

    response_client = client(FailingLogoutFacade())
    response_client.cookies.set("sms_refresh_token", "refresh.jwt")
    response = response_client.post(
        "/api/v1/web/auth/logout",
        headers={
            "Authorization": "Bearer expired.jwt",
            "Origin": "http://testserver",
        },
    )

    assert response.status_code == 503
    assert response.json()["code"] == "AUTH_SESSION_UNAVAILABLE"
    cleared = response.headers.get("set-cookie", "")
    assert "sms_refresh_token=" in cleared
    assert "Max-Age=0" in cleared


def test_production_login_sets_secure_refresh_cookie(tmp_path: Path) -> None:
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("test-ca", encoding="utf-8")
    settings = Settings(
        _env_file=None,
        environment="production",
        trusted_hosts="testserver,sms.example.test",
        debug=False,
        auth_mock=False,
        vendor_mock=False,
        redis_ha_mode="managed",
        redis_broker_host="broker.redis.example",
        redis_auth_host="auth.redis.example",
        redis_control_host="control.redis.example",
        vendor_base_url="https://vendor.example.test",
        ldap_ca_certs_file=ca_file,
    )
    app = create_app(settings)
    app.state.settings = settings
    app.dependency_overrides[get_auth_facade] = lambda: FakeAuthFacade()
    response_client = TestClient(app)

    login = response_client.post(
        "/api/v1/web/auth/login",
        json={
            "provider_code": "local",
            "username": "operator01",
            "password": "correct",
            "tab_id": TAB_ID,
        },
    )
    assert login.status_code == 200
    assert "Secure" in login.headers.get("set-cookie", "")
    assert "HttpOnly" in login.headers.get("set-cookie", "")
    assert "SameSite=lax" in login.headers.get("set-cookie", "")


def _refresh_cookie(response: Any) -> str:
    header = response.headers.get("set-cookie", "")
    for part in header.split(";"):
        item = part.strip()
        if item.startswith("sms_refresh_token="):
            return item.split("=", 1)[1]
    raise AssertionError("missing sms_refresh_token Set-Cookie")


def _login_payload(password: str = "correct") -> dict[str, str]:
    return {
        "provider_code": "local",
        "username": "operator01",
        "password": password,
        "tab_id": TAB_ID,
    }


def test_login_forwards_existing_refresh_cookie_and_refresh_body_rejects_token() -> None:
    facade = FakeAuthFacade()
    response_client = client(facade)
    response_client.cookies.set("sms_refresh_token", "old-family.jwt")

    success = response_client.post("/api/v1/web/auth/login", json=_login_payload())

    assert success.status_code == 200
    assert facade.login_calls[0][5] == "old-family.jwt"
    assert "sms_refresh_token=refresh.jwt" in success.headers.get("set-cookie", "")

    body_token = response_client.post(
        "/api/v1/web/auth/refresh",
        headers={"Origin": "http://testserver"},
        json={"tab_id": TAB_ID, "refresh_token": "old-family.jwt"},
    )
    assert body_token.status_code == 400
    assert body_token.json()["code"] == "INVALID_PARAM"
    assert facade.refresh_calls == []


def test_refresh_requires_http_only_cookie() -> None:
    facade = FakeAuthFacade()
    response_client = client(facade)

    missing = response_client.post(
        "/api/v1/web/auth/refresh",
        headers={"Origin": "http://testserver"},
        json={"tab_id": TAB_ID},
    )

    assert missing.status_code == 401
    assert missing.json()["code"] == "UNAUTHORIZED"
    assert missing.headers["cache-control"] == "no-store"
    assert "sms_refresh_token=" in missing.headers.get("set-cookie", "")
    assert "Max-Age=0" in missing.headers.get("set-cookie", "")
    assert facade.refresh_calls == []


def test_ad_reauthentication_required_clears_refresh_cookie() -> None:
    class ReauthenticationFacade(FakeAuthFacade):
        async def refresh(self, refresh_token: str, ip: str, tab_id: str) -> LoginSuccess:
            del refresh_token, ip, tab_id
            raise ApiError(
                401,
                "AUTH_REAUTH_REQUIRED",
                "AD 会话已到期，请重新登录",
                None,
            )

    response_client = client(ReauthenticationFacade())
    response_client.cookies.set("sms_refresh_token", "refresh.jwt")

    expired = response_client.post(
        "/api/v1/web/auth/refresh",
        headers={"Origin": "http://testserver"},
        json={"tab_id": TAB_ID},
    )

    assert expired.status_code == 401
    assert expired.json()["code"] == "AUTH_REAUTH_REQUIRED"
    assert expired.headers["cache-control"] == "no-store"
    cookie = expired.headers.get("set-cookie", "")
    assert "sms_refresh_token=" in cookie
    assert "Max-Age=0" in cookie


def _real_login_facade() -> AuthFacade:
    value = user()
    identity = AuthenticatedIdentity(
        provider_code="local",
        login_name="operator01",
        external_subject="local:operator01",
        display_name="测试用户",
        dept="研发部",
        groups=(),
        account=value,
    )
    users = FakeUserRepository(value)
    tokens = JwtService(
        "a-jwt-secret-that-is-long-enough-for-hs256-tests",
        FakeKeyValue(),
        security_session_loader=users.load_security_session,
    )
    return AuthFacade(FakeAuthService(identity), users, tokens, FakeHasher())


def test_relogin_revokes_prior_cookie_family_and_late_refresh_clears_cookie() -> None:
    facade = _real_login_facade()
    response_client = client(facade)

    first = response_client.post("/api/v1/web/auth/login", json=_login_payload())
    assert first.status_code == 200
    old_refresh = _refresh_cookie(first)
    assert old_refresh
    assert "refresh_token" not in first.json()

    second = response_client.post("/api/v1/web/auth/login", json=_login_payload())
    assert second.status_code == 200
    new_refresh = _refresh_cookie(second)
    assert new_refresh
    assert new_refresh != old_refresh
    assert "refresh_token" not in second.json()

    late_client = client(facade)
    late_client.cookies.set("sms_refresh_token", old_refresh)
    late = late_client.post(
        "/api/v1/web/auth/refresh",
        headers={"Origin": "http://testserver"},
        json={"tab_id": TAB_ID},
    )
    assert late.status_code == 401
    assert late.json()["code"] == "UNAUTHORIZED"
    late_cookie = late.headers.get("set-cookie", "")
    assert "sms_refresh_token=" in late_cookie
    assert "Max-Age=0" in late_cookie

    rotated = response_client.post(
        "/api/v1/web/auth/refresh",
        headers={"Origin": "http://testserver"},
        json={"tab_id": TAB_ID},
    )
    assert rotated.status_code == 200
    assert rotated.json()["user"]["account_id"] == 8
    assert _refresh_cookie(rotated) != new_refresh
