from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from fastapi.routing import APIRoute

from app.api.auth import bearer_scheme
from app.api.metrics import authorize_metrics
from app.core.apikey import optional_api_app, require_api_app
from app.main import app

PUBLIC_WEB_ROUTES = frozenset(
    {
        "/api/v1/web/auth/providers",
        "/api/v1/web/auth/password-policy",
        "/api/v1/web/auth/login",
        "/api/v1/web/auth/refresh",
        "/api/v1/web/auth/password/initial",
    }
)
PUBLIC_SYSTEM_ROUTES = frozenset({"/livez", "/healthz", "/readyz"})
PUBLIC_DOCS_ROUTES = frozenset({"/docs", "/redoc", "/openapi.json"})
AUTHENTICATORS = frozenset(
    {bearer_scheme, require_api_app, optional_api_app, authorize_metrics}
)


def _api_routes(container: Any) -> Iterator[APIRoute]:
    for route in container.routes:
        if isinstance(route, APIRoute):
            yield route
        elif hasattr(route, "original_router"):
            yield from _api_routes(route.original_router)


def _dependency_calls(dependant: Any) -> set[Any]:
    calls = {dependant.call}
    for child in dependant.dependencies:
        calls.update(_dependency_calls(child))
    return calls


def test_every_protected_route_declares_its_authentication_dependency() -> None:
    """新增路由若遗漏认证 dependency，本测试必须阻断合并。"""

    for route in _api_routes(app):
        calls = _dependency_calls(route.dependant)
        if route.path in PUBLIC_WEB_ROUTES or route.path in PUBLIC_SYSTEM_ROUTES:
            assert bearer_scheme not in calls
            assert authorize_metrics not in calls
            continue
        if route.path in PUBLIC_DOCS_ROUTES or route.path.startswith("/docs"):
            continue
        if route.path == "/metrics":
            assert authorize_metrics in calls, route.path
            continue
        assert calls & AUTHENTICATORS, route.path
        if route.path.startswith("/api/v1/web/"):
            assert bearer_scheme in calls, route.path
        elif route.path in {
            "/api/v1/messages/send",
            "/api/v1/messages/uat-send",
        }:
            assert require_api_app in calls, route.path
        elif route.path.startswith("/api/v1/messages/batches/"):
            assert optional_api_app in calls, route.path
            assert bearer_scheme in calls, route.path


def test_api_key_authentication_is_not_installed_as_path_guessing_middleware() -> None:
    middleware_names = {
        item.cls.__name__
        for item in app.user_middleware
    }

    assert "ApiKeyMiddleware" not in middleware_names
