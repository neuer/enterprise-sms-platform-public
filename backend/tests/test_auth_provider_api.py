from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

import app.api.auth_providers as providers_api
from app.core.auth.jwt import JwtClaims
from app.core.auth.roles import Role
from app.core.auth.runtime import get_auth_facade
from app.main import create_app
from app.services.auth_provider import (
    ExternalRoleMapping,
    ImmutableProvider,
    InvalidProviderConfig,
    ProviderRecord,
    ProviderTestResult,
    UntestedProviderConfig,
)

NOW = datetime(2026, 7, 16, 8, tzinfo=UTC)


def valid_config() -> dict[str, object]:
    return {
        "server": "ldaps://dc01.example.com:636",
        "base_dn": "DC=example,DC=com",
        "bind_dn": "CN=sms-reader,OU=Service,DC=example,DC=com",
        "user_search_filter": "(sAMAccountName={username})",
        "username_attribute": "sAMAccountName",
        "display_name_attribute": "displayName",
        "dept_attribute": "department",
        "subject_attribute": "objectGUID",
        "group_attribute": "memberOf",
        "connect_timeout_s": 5.0,
        "receive_timeout_s": 10.0,
    }


def record(*, code: str = "ad", enabled: bool = False) -> ProviderRecord:
    return ProviderRecord(
        id=2,
        code=code,
        name="AD 账号" if code == "ad" else "本地账号",
        kind="ldap" if code == "ad" else "local",
        enabled=enabled,
        draft_config=valid_config() if code == "ad" else {},
        active_config=None,
        draft_version=2,
        tested_version=None,
        active_version=None,
        last_tested_at=NOW,
        last_test_status="failed",
        created_at=NOW,
        updated_at=NOW,
    )


class FakeFacade:
    def __init__(self, role: Role = "admin") -> None:
        self.role = role

    async def verify(self, token: str) -> JwtClaims:
        del token
        return JwtClaims(
            account_id=1,
            identity_id=11,
            provider_code="local",
            login_name="admin",
            display_name="管理员",
            dept="平台部",
            role=self.role,
        )


class FakeProviderService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.value = record()

    async def get(self, code: str) -> ProviderRecord:
        self.calls.append(("get", code))
        return record(code=code)

    async def save_draft(
        self,
        code: str,
        config: dict[str, object],
        *,
        actor: str,
        ip: str,
    ) -> ProviderRecord:
        self.calls.append(("draft", (code, config, actor, ip)))
        if code == "local":
            raise ImmutableProvider("immutable")
        if config.get("server") == "ldaps://bad.example.com":
            raise InvalidProviderConfig("LDAP 配置无效")
        return self.value

    async def test_draft(
        self,
        code: str,
        *,
        actor: str,
        ip: str,
    ) -> ProviderTestResult:
        self.calls.append(("test", (code, actor, ip)))
        return ProviderTestResult(False, "LDAP_CONNECTION_FAILED")

    async def activate(self, code: str, *, actor: str, ip: str) -> ProviderRecord:
        self.calls.append(("activate", (code, actor, ip)))
        raise UntestedProviderConfig("untested")

    async def disable(self, code: str, *, actor: str, ip: str) -> ProviderRecord:
        self.calls.append(("disable", (code, actor, ip)))
        return self.value

    async def list_role_mappings(self, code: str) -> tuple[ExternalRoleMapping, ...]:
        self.calls.append(("list_mappings", code))
        return (ExternalRoleMapping("CN=SMS-Admins,OU=Groups,DC=example,DC=com", "admin"),)

    async def replace_role_mappings(
        self,
        code: str,
        mappings: tuple[ExternalRoleMapping, ...],
        *,
        actor: str,
        ip: str,
    ) -> tuple[ExternalRoleMapping, ...]:
        self.calls.append(("replace_mappings", (code, mappings, actor, ip)))
        return mappings


def client(role: Role = "admin") -> tuple[TestClient, FakeProviderService]:
    app = create_app()
    service = FakeProviderService()
    app.dependency_overrides[get_auth_facade] = lambda: FakeFacade(role)
    app.dependency_overrides[providers_api.get_auth_provider_admin_service] = lambda: service
    app.dependency_overrides[providers_api.get_provider_runtime_status] = lambda: (
        providers_api.ProviderRuntimeStatus(True, True)
    )
    return TestClient(app), service


def test_admin_reads_non_secret_provider_state_with_boolean_secret_metadata() -> None:
    browser, _ = client()

    response = browser.get(
        "/api/v1/web/admin/auth-providers/ad",
        headers={"Authorization": "Bearer admin.jwt"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["draft_config"] == valid_config()
    assert payload["bind_secret_available"] is True
    assert payload["ca_available"] is True
    assert "bind_password" not in str(payload)
    assert "ldap_bind_password" not in str(payload)


def test_admin_can_save_test_disable_and_replace_role_mappings() -> None:
    browser, service = client()
    headers = {"Authorization": "Bearer admin.jwt"}

    saved = browser.put(
        "/api/v1/web/admin/auth-providers/ad/draft",
        headers=headers,
        json={"config": valid_config()},
    )
    tested = browser.post(
        "/api/v1/web/admin/auth-providers/ad/test",
        headers=headers,
    )
    disabled = browser.post(
        "/api/v1/web/admin/auth-providers/ad/disable",
        headers=headers,
    )
    listed = browser.get(
        "/api/v1/web/admin/auth-providers/ad/role-mappings",
        headers=headers,
    )
    replaced = browser.put(
        "/api/v1/web/admin/auth-providers/ad/role-mappings",
        headers=headers,
        json={
            "mappings": [
                {
                    "external_group": "CN=SMS-Operators,OU=Groups,DC=example,DC=com",
                    "role": "operator",
                }
            ]
        },
    )

    assert {saved.status_code, tested.status_code, disabled.status_code, listed.status_code} == {
        200
    }
    assert tested.json() == {"success": False, "result_code": "LDAP_CONNECTION_FAILED"}
    assert replaced.status_code == 200
    assert replaced.json()["mappings"][0]["role"] == "operator"
    assert [name for name, _ in service.calls] == [
        "draft",
        "test",
        "disable",
        "list_mappings",
        "replace_mappings",
    ]
    assert vars(providers_api.save_provider_draft)["__audited_action__"] == (
        "auth_provider_save_draft"
    )
    assert vars(providers_api.replace_provider_role_mappings)["__audited_action__"] == (
        "auth_provider_role_mappings_replace"
    )


def test_provider_admin_rejects_non_admin_local_mutation_and_untested_activation() -> None:
    headers = {"Authorization": "Bearer admin.jwt"}
    forbidden, _ = client("viewer")
    immutable, _ = client()
    untested, _ = client()

    denied = forbidden.get("/api/v1/web/admin/auth-providers/ad", headers=headers)
    local = immutable.put(
        "/api/v1/web/admin/auth-providers/local/draft",
        headers=headers,
        json={"config": valid_config()},
    )
    activation = untested.post(
        "/api/v1/web/admin/auth-providers/ad/activate",
        headers=headers,
    )

    assert denied.status_code == 403 and denied.json()["code"] == "FORBIDDEN"
    assert local.status_code == 409 and local.json()["code"] == "STATE_CONFLICT"
    assert activation.status_code == 409
    assert activation.json()["code"] == "PROVIDER_CONFIG_UNTESTED"


def test_provider_payload_forbids_credentials_and_test_failure_is_safe() -> None:
    browser, _ = client()
    headers = {"Authorization": "Bearer admin.jwt"}

    invalid = browser.put(
        "/api/v1/web/admin/auth-providers/ad/draft",
        headers=headers,
        json={"config": {**valid_config(), "bind_password": "do-not-store"}},
    )
    tested = browser.post(
        "/api/v1/web/admin/auth-providers/ad/test",
        headers=headers,
    )

    assert invalid.status_code == 400
    assert invalid.json()["code"] == "INVALID_PARAM"
    assert "do-not-store" not in invalid.text
    assert tested.json() == {"success": False, "result_code": "LDAP_CONNECTION_FAILED"}
    assert "password" not in tested.text.casefold()
