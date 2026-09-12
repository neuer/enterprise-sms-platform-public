"""实际 FastAPI 依赖解析必须先授权，再进入业务依赖。"""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.auth.jwt import JwtClaims
from app.core.auth.runtime import get_auth_facade
from app.core.errors import ApiError, api_error_handler

CASES = [
    ("web_messages", "/api/v1/web/batches", "get_settings", "viewer"),
    ("vendor_test", "/api/v1/web/admin/vendor-test/recipients", "get_settings", "admin"),
    ("apps", "/api/v1/web/admin/apps", "get_settings", "admin"),
    ("auth_providers", "/api/v1/web/admin/auth-providers/ad", "get_settings", "admin"),
    ("admin", "/api/v1/web/admin/configs", "get_settings", "admin"),
    ("ops", "/api/v1/web/admin/raw-logs", "SqlOpsRepository", "admin"),
    ("approvals", "/api/v1/web/approvals", "get_settings", "approver"),
    (
        "reports",
        "/api/v1/web/reports/export/aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa",
        "get_settings",
        "viewer",
    ),
]


@pytest.mark.parametrize("module_name,path,business_entry,role", CASES)
@pytest.mark.parametrize("token", [None, "invalid", "revoked", "valid", "wrong_role"])
def test_actual_dependency_graph_authorizes_before_business_io(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    path: str,
    business_entry: str,
    role: str,
    token: str | None,
) -> None:
    module = importlib.import_module("app.api." + module_name)
    calls = []
    wrong_role = token == "wrong_role" and role != "viewer"

    def fail_business(*_: Any, **__: Any) -> None:
        calls.append("business")
        raise ApiError(503, "DEPENDENCY_UNAVAILABLE", "synthetic dependency failure", None)

    class Facade:
        async def verify(self, value: str) -> JwtClaims:
            if value not in {"valid", "wrong_role"}:
                raise ApiError(401, "UNAUTHORIZED", "synthetic invalid session", None)
            return JwtClaims(
                8,
                18,
                "local",
                "synthetic",
                "synthetic",
                "synthetic",
                "viewer" if wrong_role else role,
            )  # type: ignore[arg-type]

    monkeypatch.setattr(module, business_entry, fail_business)
    application = FastAPI()
    application.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    application.dependency_overrides[get_auth_facade] = Facade
    application.include_router(module.router)
    with TestClient(application) as client:
        response = client.get(path, headers={"Authorization": f"Bearer {token}"} if token else {})
    authorized = token in {"valid", "wrong_role"} and not wrong_role
    expected = 503 if authorized else 403 if wrong_role else 401
    assert response.status_code == expected, response.text
    assert len(calls) == (1 if authorized else 0)


@pytest.mark.parametrize(
    "module_name,path,role",
    [
        ("web_messages", "/api/v1/web/messages/import", "operator"),
        ("replies", "/api/v1/web/replies", "viewer"),
        ("replies", "/api/v1/web/replies/1/blacklist", "operator"),
        ("ops", "/api/v1/web/admin/unmatched-reports/export", "admin"),
    ],
)
@pytest.mark.parametrize("token", [None, "invalid", "revoked", "valid", "wrong_role"])
def test_post_dependency_graph_authorizes_before_business_io(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    path: str,
    role: str,
    token: str | None,
) -> None:
    module = importlib.import_module("app.api." + module_name)
    calls: list[str] = []

    def fail_business(*_: Any, **__: Any) -> None:
        calls.append("business")
        raise ApiError(503, "DEPENDENCY_UNAVAILABLE", "synthetic failure", None)

    class Facade:
        async def verify(self, value: str) -> JwtClaims:
            if value not in {"valid", "wrong_role"}:
                raise ApiError(401, "UNAUTHORIZED", "synthetic invalid session", None)
            return JwtClaims(
                8,
                18,
                "local",
                "synthetic",
                "synthetic",
                "synthetic",
                "viewer" if value == "wrong_role" else role,
            )  # type: ignore[arg-type]

    monkeypatch.setattr(module, "get_settings", fail_business)
    # The admin export dependency lives in reports, not the ops module.
    if module_name == "ops":
        from app.api import reports

        monkeypatch.setattr(reports, "get_settings", fail_business)
    application = FastAPI()
    application.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    application.dependency_overrides[get_auth_facade] = Facade
    application.include_router(module.router)
    with TestClient(application) as client:
        response = client.post(path, headers={"Authorization": f"Bearer {token}"} if token else {})
    wrong_role = token == "wrong_role" and role != "viewer"
    authorized = token in {"valid", "wrong_role"} and not wrong_role
    assert response.status_code == (503 if authorized else 403 if wrong_role else 401), (
        response.text
    )
    assert len(calls) == (1 if authorized else 0)
