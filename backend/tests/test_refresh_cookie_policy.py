"""Cookie 前缀迁移、重复输入与当前模式的 HTTP 边界。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from app.core.auth.cookies import (
    PRODUCTION_REFRESH_COOKIE_NAME as NEW,
)
from app.core.auth.cookies import (
    REFRESH_COOKIE_DESCRIPTION,
    RefreshCookiePolicy,
    read_refresh_cookie,
)
from app.core.auth.cookies import (
    REFRESH_COOKIE_NAME as OLD,
)
from tests.test_auth_api import TAB_ID, FakeAuthFacade, client


def request(*headers: str, production: bool = True, scheme: str = "https") -> Request:
    return Request({
        "type": "http", "scheme": scheme, "method": "POST", "path": "/api/v1/web/auth/refresh",
        "server": ("testserver", 443 if scheme == "https" else 80), "query_string": b"",
        "headers": [(b"cookie", header.encode()) for header in headers],
        "app": SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(
            is_production=production,
        ))),
    })


@pytest.mark.parametrize("headers", [
    (f"{OLD}=one; {NEW}=two",),
    (f"{NEW}=one; {NEW}=two",),
    (f"{OLD}=one; {OLD}=two",),
    (f"{NEW}=one", f"{NEW}=one"),
    (f"{NEW}=one", f"{OLD}=two"),
    (f"{NEW} =one; {NEW}=two",),
    (f"{NEW}=; {NEW}=two",),
])
@pytest.mark.parametrize("production", [True, False])
def test_duplicate_cookie_rejected_before_framework_collapsing(
    headers: tuple[str, ...], production: bool,
) -> None:
    result = read_refresh_cookie(request(*headers, production=production))
    assert result.ambiguous and result.current is None and result.prior is None


@pytest.mark.parametrize("production,scheme,name,expected", [
    (True, "https", NEW, "value"),
    (True, "http", NEW, None),
    (True, "https", OLD, None),
    (False, "http", OLD, "value"),
    (False, "https", OLD, "value"),
    (False, "http", NEW, None),
])
def test_refresh_never_falls_back_between_modes(
    production: bool, scheme: str, name: str, expected: str | None,
) -> None:
    result = read_refresh_cookie(request(f"{name}=value", production=production, scheme=scheme))
    assert result.current == expected
    assert not result.ambiguous


def production_client(facade: FakeAuthFacade, scheme: str = "https") -> TestClient:
    original = client(facade)
    original.app.state.settings = SimpleNamespace(is_production=True)
    return TestClient(original.app, base_url=f"{scheme}://testserver")


def login_body() -> dict[str, str]:
    return {"provider_code": "local", "username": "operator01", "password": "correct",
            "session_mode": "refresh", "tab_id": TAB_ID}


def test_production_legacy_refresh_requires_password_relogin_then_rotates_and_logs_out() -> None:
    facade = FakeAuthFacade()
    browser = production_client(facade)
    legacy = browser.post("/api/v1/web/auth/refresh", json={"tab_id": TAB_ID},
                          headers={"Origin": "https://testserver", "Cookie": f"{OLD}=refresh.jwt"})
    assert legacy.status_code == 401
    assert facade.refresh_calls == []
    login = browser.post("/api/v1/web/auth/login", json=login_body(),
                         headers={"Cookie": f"{OLD}=old-family.jwt"})
    assert login.status_code == 200
    assert facade.login_calls[-1][5] == "old-family.jwt"
    issued = login.headers.get_list("set-cookie")
    assert len(issued) == 2
    assert any(header.startswith(f"{OLD}=") and "Max-Age=0" in header for header in issued)
    current = next(header for header in issued if header.startswith(f"{NEW}="))
    for attribute in ("Secure", "HttpOnly", "SameSite=lax", "Path=/api/v1/web/auth"):
        assert attribute in current
    assert "Domain=" not in current
    refresh = browser.post("/api/v1/web/auth/refresh", json={"tab_id": TAB_ID},
                           headers={"Origin": "https://testserver"})
    assert refresh.status_code == 200
    assert facade.refresh_calls[-1][0] == "refresh.jwt"
    logout = browser.post("/api/v1/web/auth/logout", headers={
        "Origin": "https://testserver", "Authorization": "Bearer signed.jwt",
    })
    assert logout.status_code == 200
    assert facade.logout_calls[-1][2] == "rotated-refresh.jwt"
    for header in logout.headers.get_list("set-cookie"):
        for attribute in ("Max-Age=0", "Secure", "HttpOnly", "SameSite=lax",
                          "Path=/api/v1/web/auth"):
            assert attribute in header
    assert NEW not in browser.cookies and OLD not in browser.cookies


@pytest.mark.parametrize("operation", ["login", "refresh", "logout"])
def test_dual_cookie_is_never_selected_or_used_for_fallback(operation: str) -> None:
    facade = FakeAuthFacade()
    browser = production_client(facade)
    response = browser.post(f"/api/v1/web/auth/{operation}",
        json=login_body() if operation == "login" else {"tab_id": TAB_ID},
        headers={"Origin": "https://testserver", "Authorization": "Bearer signed.jwt",
                 "Cookie": f"{NEW}=refresh.jwt; {OLD}=old.jwt"})
    assert response.status_code == (200 if operation == "logout" else 401)
    assert not facade.login_calls and not facade.refresh_calls
    if operation == "logout":
        assert facade.logout_calls[-1][2] is None
    assert len(response.headers.get_list("set-cookie")) == 2


def test_production_refresh_rejects_cookie_even_if_manually_sent_over_http() -> None:
    facade = FakeAuthFacade()
    browser = production_client(facade, "http")
    response = browser.post("/api/v1/web/auth/refresh", json={"tab_id": TAB_ID}, headers={
        "Origin": "http://testserver", "Cookie": f"{NEW}=refresh.jwt",
    })
    assert response.status_code == 401 and facade.refresh_calls == []


def test_openapi_documents_same_policy_as_cookie_reader() -> None:
    facade = FakeAuthFacade()
    app = client(facade).app
    contract = app.openapi()["components"]["securitySchemes"]["RefreshCookie"]
    assert contract["name"] == NEW
    assert contract["description"] == REFRESH_COOKIE_DESCRIPTION
    assert RefreshCookiePolicy.for_request(request()).name == contract["name"]


@pytest.mark.parametrize("other_path", ["/", "/api/v1/web", "/api/v1/web/auth"])
def test_http_cookie_jar_domain_path_conflicts_fail_closed(other_path: str) -> None:
    facade = FakeAuthFacade()
    original = client(facade)
    original.app.state.settings = SimpleNamespace(is_production=True)
    browser = TestClient(original.app, base_url="https://app.example.test")
    browser.cookies.set(NEW, "refresh.jwt", domain="app.example.test", path="/api/v1/web/auth")
    browser.cookies.set(NEW, "other.jwt", domain=".example.test", path=other_path)
    response = browser.post("/api/v1/web/auth/refresh", json={"tab_id": TAB_ID},
                            headers={"Origin": "https://app.example.test"})
    assert response.status_code == 401
    assert facade.refresh_calls == []
    assert all("Max-Age=0" in value for value in response.headers.get_list("set-cookie"))


def test_successful_production_login_removes_legacy_cookie_from_browser_jar() -> None:
    facade = FakeAuthFacade()
    browser = production_client(facade)
    browser.cookies.set(OLD, "old-family.jwt", domain="testserver.local", path="/api/v1/web/auth")
    response = browser.post("/api/v1/web/auth/login", json=login_body())
    assert response.status_code == 200
    assert facade.login_calls[-1][5] == "old-family.jwt"
    assert OLD not in browser.cookies
    assert browser.cookies.get(NEW) == "refresh.jwt"
