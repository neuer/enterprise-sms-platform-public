"""目录服务的既有错误语义透传，未知错误交给统一安全处理。"""

from __future__ import annotations

import importlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.auth.jwt import JwtClaims
from app.core.auth.runtime import get_auth_facade
from app.core.correlation import correlation_scope
from app.core.errors import ApiError, api_error_handler, internal_error_handler


@pytest.mark.parametrize("name", ["signs", "templates"])
@pytest.mark.parametrize("status", [403, 429, 503, 500])
@pytest.mark.parametrize(
    "method,suffix",
    [
        ("POST", ""),
        ("PUT", "/1"),
        ("DELETE", "/1"),
        ("POST", "/1/sync"),
    ],
)
def test_write_errors_and_request_scoped_auth(name, status, method, suffix):
    module = importlib.import_module("app.api." + name)
    calls = []

    class Facade:
        async def verify(self, token):
            calls.append(token)
            return JwtClaims(1, 11, "local", "admin", "管理员", "平台", "admin")

    class Service:
        def __getattr__(self, name):
            async def fail(*args, **kwargs):
                if status == 500:
                    raise RuntimeError("synthetic private database detail")
                raise ApiError(
                    status,
                    "SYNTHETIC_ERROR",
                    "安全错误",
                    {"retry": True},
                    headers={"Retry-After": "7"},
                )

            return fail

    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(Exception, internal_error_handler)
    app.include_router(module.router)
    app.dependency_overrides[get_auth_facade] = Facade
    service_dependency = getattr(module, "get_" + name[:-1] + "_service")
    app.dependency_overrides[service_dependency] = Service
    app.dependency_overrides[getattr(module, "get_" + name[:-1] + "_job_sender")] = Service
    payload = {"name": "测试"}
    if name == "templates":
        payload.update(content="测试内容", var_specs=[])
    with correlation_scope(), TestClient(app, raise_server_exceptions=False) as client:
        for _ in range(2):
            response = client.request(
                method,
                "/api/v1/web/" + name + suffix,
                headers={"Authorization": "Bearer synthetic"},
                json=payload,
            )
            assert response.status_code == status, response.text
            if status != 500:
                assert response.headers["Retry-After"] == "7"
                assert response.json() == {
                    "code": "SYNTHETIC_ERROR",
                    "message": "安全错误",
                    "detail": {"retry": True},
                }
            else:
                assert response.json()["code"] == "INTERNAL_ERROR"
                assert "synthetic private" not in response.text
                assert "X-Request-ID" in response.headers
    assert calls == ["synthetic", "synthetic"]
