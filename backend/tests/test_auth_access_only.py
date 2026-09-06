from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import jwt
import pytest
import yaml
from fastapi.testclient import TestClient

from app.core.auth.backends import AuthenticatedIdentity, SessionStateUnavailable
from app.core.auth.jwt import JwtService
from app.core.auth.runtime import AuthFacade, LoginSuccess
from tests.test_auth import FakeKeyValue
from tests.test_auth_api import (
    TAB_ID,
    FakeAuthFacade,
    _login_payload,
    _real_login_facade,
    client,
    user,
)
from tests.test_auth_runtime import FakeAuthService, FakeHasher, FakeUserRepository

ROOT = Path(__file__).resolve().parents[2]
FRONTEND_AUTH = ROOT / "frontend/src/api/auth.ts"
FRONTEND_LOCK = ROOT / "frontend/src/api/refreshLock.ts"


def _cookie_header(response) -> str:
    return response.headers.get("set-cookie", "")


def _refresh_cookie_value(response) -> str | None:
    for part in _cookie_header(response).split(";"):
        item = part.strip()
        if item.startswith("sms_refresh_token="):
            return item.split("=", 1)[1]
    return None


def test_access_only_login_response_contains_no_refresh_cookie() -> None:
    facade = FakeAuthFacade()
    response_client = client(facade)

    success = response_client.post(
        "/api/v1/web/auth/login",
        json=_login_payload(session_mode="access_only"),
    )

    assert success.status_code == 200
    assert success.json() == {
        "session_mode": "access_only",
        "token": "signed.jwt",
        "expires_in": 900,
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
    assert "refresh_expires_in" not in success.json()
    assert "refresh_token" not in success.json()
    cookie = _cookie_header(success)
    assert "sms_refresh_token=" not in cookie or "Max-Age=0" in cookie
    assert facade.login_calls[0][4] is None
    assert facade.login_calls[0][6] == "access_only"


def test_access_only_login_rejects_tab_id() -> None:
    facade = FakeAuthFacade()
    response_client = client(facade)

    denied = response_client.post(
        "/api/v1/web/auth/login",
        json={
            "provider_code": "local",
            "username": "operator01",
            "password": "correct",
            "session_mode": "access_only",
            "tab_id": TAB_ID,
        },
    )

    assert denied.status_code == 400
    assert denied.json()["code"] == "INVALID_PARAM"
    assert facade.login_calls == []


def test_refresh_login_requires_tab_id() -> None:
    facade = FakeAuthFacade()
    response_client = client(facade)

    denied = response_client.post(
        "/api/v1/web/auth/login",
        json={
            "provider_code": "local",
            "username": "operator01",
            "password": "correct",
            "session_mode": "refresh",
        },
    )

    assert denied.status_code == 400
    assert denied.json()["code"] == "INVALID_PARAM"
    assert facade.login_calls == []


@pytest.mark.asyncio
async def test_access_only_login_creates_no_refresh_family() -> None:
    store = FakeKeyValue()
    account = user()
    identity = AuthenticatedIdentity(
        provider_code="local",
        login_name="operator01",
        external_subject="local:operator01",
        display_name="测试用户",
        dept="研发部",
        groups=(),
        account=account,
    )
    users = FakeUserRepository(account)
    tokens = JwtService(
        "a-jwt-secret-that-is-long-enough-for-hs256-tests",
        store,
        security_session_loader=users.load_security_session,
    )
    facade = AuthFacade(FakeAuthService(identity), users, tokens, FakeHasher())

    result = await facade.login(
        "local",
        "operator01",
        "correct",
        "10.0.0.8",
        None,
        session_mode="access_only",
    )

    assert isinstance(result, LoginSuccess)
    assert result.session_mode == "access_only"
    assert result.refresh_token == ""
    assert result.refresh_expires_in == 0
    payload = jwt.decode(
        result.token,
        tokens.secret,
        algorithms=["HS256"],
        options={"verify_exp": False, "verify_aud": False},
    )
    assert payload["session_mode"] == "access_only"
    assert payload["token_type"] == "access"
    assert not any(key.startswith("auth:jwt:refresh-family:") for key in store.values)
    assert store.values[f"auth:jwt:session-mode:{payload['sid']}"] == "access_only"


def test_access_only_login_revokes_presented_old_refresh_family() -> None:
    facade = _real_login_facade()
    response_client = client(facade)

    first = response_client.post("/api/v1/web/auth/login", json=_login_payload())
    assert first.status_code == 200
    old_refresh = _refresh_cookie_value(first)
    assert old_refresh

    second = response_client.post(
        "/api/v1/web/auth/login",
        json=_login_payload(session_mode="access_only"),
    )
    assert second.status_code == 200
    assert second.json()["session_mode"] == "access_only"
    assert "refresh_expires_in" not in second.json()
    cleared = _cookie_header(second)
    assert "sms_refresh_token=" in cleared
    assert "Max-Age=0" in cleared

    late = TestClient(response_client.app)
    late.cookies.set("sms_refresh_token", old_refresh)
    expired = late.post(
        "/api/v1/web/auth/refresh",
        headers={"Origin": "http://testserver"},
        json={"tab_id": TAB_ID},
    )
    assert expired.status_code == 401
    assert expired.json()["code"] == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_late_old_refresh_cannot_restore_valid_cookie_after_access_only_login() -> None:
    facade = _real_login_facade()
    response_client = client(facade)
    first = response_client.post("/api/v1/web/auth/login", json=_login_payload())
    old_refresh = _refresh_cookie_value(first)
    access_only = response_client.post(
        "/api/v1/web/auth/login",
        json=_login_payload(session_mode="access_only"),
    )
    access_token = access_only.json()["token"]
    assert access_only.json()["session_mode"] == "access_only"

    late = TestClient(response_client.app)
    late.cookies.set("sms_refresh_token", old_refresh or "")
    replay = late.post(
        "/api/v1/web/auth/refresh",
        headers={"Origin": "http://testserver"},
        json={"tab_id": TAB_ID},
    )
    assert replay.status_code == 401
    replay_cookie = _cookie_header(replay)
    assert "sms_refresh_token=" in replay_cookie
    assert "Max-Age=0" in replay_cookie

    claims = await facade.verify(access_token)
    assert claims.session_mode == "access_only"


@pytest.mark.asyncio
async def test_access_only_refresh_endpoint_cannot_upgrade_session() -> None:
    facade = _real_login_facade()
    response_client = client(facade)
    login = response_client.post(
        "/api/v1/web/auth/login",
        json=_login_payload(session_mode="access_only"),
    )
    assert login.status_code == 200
    access = login.json()["token"]

    missing = response_client.post(
        "/api/v1/web/auth/refresh",
        headers={"Origin": "http://testserver"},
        json={"tab_id": TAB_ID},
    )
    assert missing.status_code == 401
    assert missing.json()["code"] == "UNAUTHORIZED"

    claims = await facade.verify(access)
    assert claims.session_mode == "access_only"


def test_access_only_password_change_required_clears_old_cookie() -> None:
    facade = FakeAuthFacade()
    response_client = client(facade)
    response_client.cookies.set("sms_refresh_token", "old-family.jwt")

    login = response_client.post(
        "/api/v1/web/auth/login",
        json={
            "provider_code": "local",
            "username": "operator01",
            "password": "temporary",
            "session_mode": "access_only",
        },
    )

    assert login.status_code == 200
    assert login.json() == {
        "change_token": "change.jwt",
        "expires_in": 600,
        "next_action": "change_password",
    }
    cookie = _cookie_header(login)
    assert "sms_refresh_token=" in cookie
    assert "Max-Age=0" in cookie
    assert facade.login_calls[0][6] == "access_only"


def test_access_only_must_change_password_revokes_presented_family() -> None:
    facade = _real_login_facade()
    response_client = client(facade)
    first = response_client.post("/api/v1/web/auth/login", json=_login_payload())
    old_refresh = _refresh_cookie_value(first)
    assert old_refresh
    facade.users.value = replace(facade.users.value, must_change_password=True)

    second = response_client.post(
        "/api/v1/web/auth/login",
        json=_login_payload(session_mode="access_only"),
    )
    assert second.status_code == 200
    assert second.json()["next_action"] == "change_password"
    assert "token" not in second.json()
    cleared = _cookie_header(second)
    assert "sms_refresh_token=" in cleared
    assert "Max-Age=0" in cleared

    late = TestClient(response_client.app)
    late.cookies.set("sms_refresh_token", old_refresh)
    expired = late.post(
        "/api/v1/web/auth/refresh",
        headers={"Origin": "http://testserver"},
        json={"tab_id": TAB_ID},
    )
    assert expired.status_code == 401
    assert expired.json()["code"] == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_late_logout_cannot_destroy_new_access_only_session() -> None:
    facade = _real_login_facade()
    response_client = client(facade)
    first = response_client.post("/api/v1/web/auth/login", json=_login_payload())
    old_access = first.json()["token"]
    old_refresh = _refresh_cookie_value(first)
    access_only = response_client.post(
        "/api/v1/web/auth/login",
        json=_login_payload(session_mode="access_only"),
    )
    new_access = access_only.json()["token"]
    assert access_only.json()["session_mode"] == "access_only"

    late = TestClient(response_client.app)
    late.cookies.set("sms_refresh_token", old_refresh or "")
    logout = late.post(
        "/api/v1/web/auth/logout",
        headers={
            "Authorization": f"Bearer {old_access}",
            "Origin": "http://testserver",
        },
    )
    assert logout.status_code in {200, 401}
    claims = await facade.verify(new_access)
    assert claims.session_mode == "access_only"


def test_access_only_login_revocation_unavailable_fails_closed() -> None:
    class UnavailableTokens(JwtService):
        async def revoke_refresh_token(self, token: str):  # type: ignore[override]
            del token
            raise SessionStateUnavailable("refresh revoke unavailable")

    account = user()
    identity = AuthenticatedIdentity(
        provider_code="local",
        login_name="operator01",
        external_subject="local:operator01",
        display_name="测试用户",
        dept="研发部",
        groups=(),
        account=account,
    )
    users = FakeUserRepository(account)
    tokens = UnavailableTokens(
        "a-jwt-secret-that-is-long-enough-for-hs256-tests",
        FakeKeyValue(),
        security_session_loader=users.load_security_session,
    )
    facade = AuthFacade(FakeAuthService(identity), users, tokens, FakeHasher())
    response_client = client(facade)
    response_client.cookies.set("sms_refresh_token", "old-family.jwt")

    failed = response_client.post(
        "/api/v1/web/auth/login",
        json=_login_payload(session_mode="access_only"),
    )
    assert failed.status_code == 503
    assert failed.json()["code"] == "AUTH_SESSION_UNAVAILABLE"


def test_web_locks_mode_preserves_existing_refresh_rotation_security() -> None:
    facade = _real_login_facade()
    response_client = client(facade)
    login = response_client.post("/api/v1/web/auth/login", json=_login_payload())
    assert login.status_code == 200
    assert login.json()["session_mode"] == "refresh"
    assert "sms_refresh_token=" in _cookie_header(login)
    assert "Max-Age=0" not in _cookie_header(login)

    rotated = response_client.post(
        "/api/v1/web/auth/refresh",
        headers={"Origin": "http://testserver"},
        json={"tab_id": TAB_ID},
    )
    assert rotated.status_code == 200
    assert rotated.json()["session_mode"] == "refresh"
    assert rotated.json()["refresh_expires_in"] >= 1
    assert "sms_refresh_token=" in _cookie_header(rotated)


def test_two_no_web_locks_tabs_have_no_refresh_cookie_writer() -> None:
    facade = FakeAuthFacade()
    first = client(facade)
    second = client(facade)
    one = first.post("/api/v1/web/auth/login", json=_login_payload(session_mode="access_only"))
    two = second.post("/api/v1/web/auth/login", json=_login_payload(session_mode="access_only"))
    assert one.status_code == 200 and two.status_code == 200
    assert one.json()["session_mode"] == "access_only"
    assert two.json()["session_mode"] == "access_only"
    for response in (one, two):
        cookie = _cookie_header(response)
        assert "sms_refresh_token=" not in cookie or "Max-Age=0" in cookie


def test_access_only_mode_is_reflected_in_openapi_and_frontend_types() -> None:
    document = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))
    login_schema = document["components"]["schemas"]["LoginRequest"]
    assert set(login_schema["required"]) == {
        "provider_code",
        "username",
        "password",
        "session_mode",
    }
    assert login_schema["properties"]["session_mode"]["enum"] == ["refresh", "access_only"]
    assert "AccessOnlyLoginSuccess" in document["components"]["schemas"]
    assert "RefreshLoginSuccess" in document["components"]["schemas"]
    assert set(document["components"]["schemas"]["AccessOnlyLoginSuccess"]["required"]) == {
        "session_mode",
        "token",
        "expires_in",
        "user",
    }
    auth_types = FRONTEND_AUTH.read_text(encoding="utf-8")
    lock = FRONTEND_LOCK.read_text(encoding="utf-8")
    assert 'session_mode: "access_only"' in auth_types
    assert 'session_mode: "refresh"' in auth_types
    assert "detectSessionMode" in lock
    assert "navigator.locks.request" in lock
    assert "userAgent" not in lock
