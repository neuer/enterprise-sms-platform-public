"""真实 FastAPI 路由验证被盗管理员 access 单独不能建立或恢复管理权限。"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import admin_step_up, users
from app.core.auth.runtime import get_auth_facade
from app.core.errors import ApiError, api_error_handler
from tests.test_admin_step_up import CLAIMS, Auth, setup
from tests.test_users_api import FakeService

CASES = [
    (
        "POST",
        "/local",
        {
            "username": "synthetic-admin",
            "display_name": "synthetic",
            "dept": "synthetic",
            "role": "admin",
            "temporary_password": "synthetic-secret",
        },
        "user_create_admin",
        "new",
        {
            "username": "synthetic-admin",
            "display_name": "synthetic",
            "dept": "synthetic",
            "role": "admin",
        },
    ),
    (
        "PUT",
        "/8/role",
        {"role": "admin", "role_override": True},
        "user_role_change",
        "8",
        {"role": "admin", "role_override": True},
    ),
    (
        "PUT",
        "/8/role",
        {"role": "viewer", "role_override": False},
        "user_role_change",
        "8",
        {"role": "viewer", "role_override": False},
    ),
    ("PUT", "/8/status", {"status": 1}, "user_status_change", "8", {"status": 1}),
    (
        "POST",
        "/8/password/reset",
        {"temporary_password": "synthetic-secret"},
        "user_password_reset",
        "8",
        {"action": "reset"},
    ),
]


def client() -> tuple[TestClient, FakeService, Auth]:
    application = FastAPI()
    application.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    grants, _, auth, _ = setup()
    business = FakeService()
    application.dependency_overrides[get_auth_facade] = lambda: auth
    application.dependency_overrides[users.get_user_management_service] = lambda: business
    application.dependency_overrides[admin_step_up.get_admin_step_up_service] = lambda: grants
    application.include_router(users.router)
    application.include_router(admin_step_up.router)
    return TestClient(application, client=("127.0.0.1", 5000)), business, auth


@pytest.mark.parametrize("method,path,payload,operation,target,parameters", CASES)
def test_access_only_fails_then_matching_grant_succeeds_once(
    method: str,
    path: str,
    payload: dict[str, Any],
    operation: str,
    target: str,
    parameters: dict[str, Any],
) -> None:
    browser, service, auth = client()
    headers = {"Authorization": "Bearer synthetic-access"}
    url = "/api/v1/web/admin/users" + path
    denied = browser.request(method, url, json=payload, headers=headers)
    assert denied.status_code == 401, denied.text
    assert denied.json()["code"] == "STEP_UP_REQUIRED"
    assert not service.calls
    issued = browser.post(
        "/api/v1/web/admin/step-up",
        headers=headers,
        json={
            "operation": operation,
            "target_id": target,
            "parameters": parameters,
            "password": "synthetic-valid",
        },
    )
    assert issued.status_code == 200, issued.text
    assert issued.headers["Cache-Control"] == "no-store"
    assert auth.reauth == [(CLAIMS.provider_code, "127.0.0.1")]
    headers["X-Admin-Step-Up"] = issued.json()["token"]
    assert browser.request(method, url, json=payload, headers=headers).status_code == 200
    assert len(service.calls) == 1
    assert browser.request(method, url, json=payload, headers=headers).status_code == 401
    assert len(service.calls) == 1


def test_revoked_current_access_cannot_use_previously_issued_grant() -> None:
    browser, service, auth = client()
    headers = {"Authorization": "Bearer synthetic-access"}
    issued = browser.post(
        "/api/v1/web/admin/step-up",
        headers=headers,
        json={
            "operation": "user_role_change",
            "target_id": "8",
            "parameters": {"role": "admin", "role_override": True},
            "password": "synthetic-valid",
        },
    )
    headers["X-Admin-Step-Up"] = issued.json()["token"]
    auth.invalid = True
    denied = browser.put(
        "/api/v1/web/admin/users/8/role",
        headers=headers,
        json={"role": "admin", "role_override": True},
    )
    assert denied.status_code == 401
    assert not service.calls


@pytest.mark.parametrize("action", ["draft", "activate", "disable", "role-mappings"])
def test_provider_changes_require_exact_single_use_reauthentication(action: str) -> None:
    from app.api import auth_providers
    from tests.test_auth_provider_api import FakeProviderService, valid_config

    application = FastAPI()
    application.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    grants, _, auth, _ = setup()

    class Provider(FakeProviderService):
        async def activate(self, code: str, **kwargs: Any) -> Any:
            self.calls.append(("activate", (code, kwargs)))
            return self.value

    business = Provider()
    application.dependency_overrides[get_auth_facade] = lambda: auth
    application.dependency_overrides[auth_providers.get_auth_provider_admin_service] = lambda: (
        business
    )
    application.dependency_overrides[auth_providers.get_provider_runtime_status] = lambda: (
        auth_providers.ProviderRuntimeStatus(True, True)
    )
    application.dependency_overrides[admin_step_up.get_admin_step_up_service] = lambda: grants
    application.include_router(auth_providers.router)
    application.include_router(admin_step_up.router)
    browser = TestClient(application, client=("127.0.0.1", 5000))
    payload: dict[str, Any] = {}
    method = "POST"
    if action == "draft":
        operation, method = "provider_save_draft", "PUT"
        payload = {"config": valid_config()}
        parameters = payload
    elif action == "role-mappings":
        operation, method = "provider_role_mapping_change", "PUT"
        payload = {
            "expected_revision": "0" * 64,
            "mappings": [{"external_group": "synthetic", "role": "admin", "dept": "synthetic"}],
        }
        parameters = payload
    else:
        operation = "provider_enable_disable"
        parameters = {"enabled": action == "activate", "draft_version": 2}
    headers = {"Authorization": "Bearer synthetic-access"}
    url = "/api/v1/web/admin/auth-providers/ad/" + action
    denied = browser.request(method, url, json=payload, headers=headers)
    assert denied.status_code == 401, denied.text
    assert all(name == "get" for name, _ in business.calls)
    issued = browser.post(
        "/api/v1/web/admin/step-up",
        headers=headers,
        json={
            "operation": operation,
            "target_id": "ad",
            "parameters": parameters,
            "password": "synthetic-valid",
        },
    )
    assert issued.status_code == 200, issued.text
    headers["X-Admin-Step-Up"] = issued.json()["token"]
    accepted = browser.request(method, url, json=payload, headers=headers)
    assert accepted.status_code == 200, accepted.text
    mutations = [name for name, _ in business.calls if name != "get"]
    assert len(mutations) == 1
    assert browser.request(method, url, json=payload, headers=headers).status_code == 401
    assert len([name for name, _ in business.calls if name != "get"]) == 1
