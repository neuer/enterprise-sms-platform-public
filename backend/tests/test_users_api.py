from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

import app.api.users as users_api
from app.core.auth.accounts import AccountSourceConflict
from app.core.auth.jwt import JwtClaims
from app.core.auth.passwords import PasswordPolicyViolation
from app.core.auth.roles import Role
from app.core.auth.runtime import get_auth_facade
from app.main import create_app
from app.services.user_management import (
    LastAdminProtected,
    ProviderActionUnsupported,
    RoleMappingConflict,
    SelfDisableDenied,
    UserNotFound,
    UserPage,
    UserRecord,
)

NOW = datetime(2026, 7, 16, 8, tzinfo=UTC)


def record() -> UserRecord:
    return UserRecord(
        account_id=8,
        identity_id=18,
        provider_code="local",
        username="operator01",
        display_name="本地操作员",
        dept="业务一部",
        role="operator",
        role_override=True,
        status=1,
        identity_status=1,
        must_change_password=True,
        source_groups=(),
        last_synced_at=None,
        last_login_at=NOW,
        security_version=4,
    )


class FakeFacade:
    def __init__(self, role: Role = "admin") -> None:
        self.role = role
        self.force_calls: list[tuple[str, int, str]] = []

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

    async def force_logout(self, token: str, account_id: int, ip: str) -> None:
        self.force_calls.append((token, account_id, ip))


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def list(
        self,
        keyword: str | None,
        provider_code: str | None,
        role: Role | None,
        status: int | None,
        page: int,
        page_size: int,
    ) -> UserPage:
        self.calls.append(("list", (keyword, provider_code, role, status, page, page_size)))
        return UserPage((record(),), 1, page, page_size)

    async def create_local(self, **kwargs: object) -> UserRecord:
        self.calls.append(("create", tuple(kwargs.values())))
        if kwargs["username"] == "conflict":
            raise AccountSourceConflict("conflict")
        if kwargs["temporary_password"] == "weak":
            raise PasswordPolicyViolation("密码不符合规则")
        return record()

    async def change_role(
        self,
        account_id: int,
        role: Role,
        role_override: bool,
        *,
        actor: str,
        ip: str,
    ) -> UserRecord:
        self.calls.append(("role", (account_id, role, role_override, actor, ip)))
        if account_id == 90:
            raise LastAdminProtected("last")
        if account_id == 91:
            raise UserNotFound(account_id)
        if account_id == 92:
            raise RoleMappingConflict("mapping")
        return record()

    async def change_status(
        self,
        account_id: int,
        status: int,
        *,
        actor_account_id: int,
        actor: str,
        ip: str,
    ) -> UserRecord:
        self.calls.append(("status", (account_id, status, actor_account_id, actor, ip)))
        if account_id == actor_account_id and status == 0:
            raise SelfDisableDenied("self")
        return record()

    async def reset_password(
        self,
        account_id: int,
        temporary_password: str,
        *,
        actor: str,
        ip: str,
    ) -> UserRecord:
        self.calls.append(("reset", (account_id, temporary_password, actor, ip)))
        if account_id == 93:
            raise ProviderActionUnsupported("not local")
        return record()


def client(
    role: Role = "admin",
) -> tuple[TestClient, FakeService, FakeFacade]:
    app = create_app()
    service = FakeService()
    facade = FakeFacade(role)
    app.dependency_overrides[get_auth_facade] = lambda: facade
    app.dependency_overrides[users_api.get_user_management_service] = lambda: service
    return TestClient(app), service, facade


def test_admin_list_exposes_provider_credential_and_account_status() -> None:
    browser, service, _ = client()

    response = browser.get(
        "/api/v1/web/admin/users?keyword=操作&provider_code=local&role=operator&status=1&page=2&page_size=20",
        headers={"Authorization": "Bearer admin.jwt"},
    )

    assert response.status_code == 200
    assert response.json()["items"][0] == {
        "account_id": 8,
        "identity_id": 18,
        "provider_code": "local",
        "username": "operator01",
        "display_name": "本地操作员",
        "dept": "业务一部",
        "role": "operator",
        "role_override": True,
        "status": 1,
        "identity_status": 1,
        "credential_status": "must_change",
        "source_groups": [],
        "sync_status": "local",
        "last_synced_at": None,
        "last_login_at": "2026-07-16T08:00:00Z",
    }
    assert service.calls[0] == (
        "list",
        ("操作", "local", "operator", 1, 2, 20),
    )


def test_admin_can_create_role_status_reset_and_revoke_by_account_id() -> None:
    browser, service, facade = client()
    headers = {"Authorization": "Bearer admin.jwt"}

    created = browser.post(
        "/api/v1/web/admin/users/local",
        headers=headers,
        json={
            "username": "new.user",
            "display_name": "新用户",
            "dept": "业务一部",
            "role": "viewer",
            "temporary_password": "Temporary@123",
        },
    )
    role = browser.put(
        "/api/v1/web/admin/users/8/role",
        headers=headers,
        json={"role": "approver", "role_override": True},
    )
    status = browser.put(
        "/api/v1/web/admin/users/8/status",
        headers=headers,
        json={"status": 0},
    )
    reset = browser.post(
        "/api/v1/web/admin/users/8/password/reset",
        headers=headers,
        json={"temporary_password": "Reset@Password123"},
    )
    revoked = browser.post(
        "/api/v1/web/admin/users/8/sessions/revoke",
        headers=headers,
    )

    assert {created.status_code, role.status_code, status.status_code, reset.status_code} == {200}
    assert revoked.status_code == 200
    assert [name for name, _ in service.calls] == [
        "create",
        "role",
        "status",
        "reset",
    ]
    assert facade.force_calls[0][:2] == ("admin.jwt", 8)
    assert vars(users_api.create_local_user)["__audited_action__"] == ("local_account_create")
    assert vars(users_api.reset_user_password)["__audited_action__"] == ("local_password_reset")


def test_admin_user_errors_use_confirmed_platform_codes() -> None:
    browser, _, _ = client()
    headers = {"Authorization": "Bearer admin.jwt"}

    conflict = browser.post(
        "/api/v1/web/admin/users/local",
        headers=headers,
        json={
            "username": "conflict",
            "display_name": "冲突",
            "dept": "平台部",
            "role": "viewer",
            "temporary_password": "Temporary@123",
        },
    )
    weak = browser.post(
        "/api/v1/web/admin/users/local",
        headers=headers,
        json={
            "username": "weak.user",
            "display_name": "弱密码",
            "dept": "平台部",
            "role": "viewer",
            "temporary_password": "weak",
        },
    )
    protected = browser.put(
        "/api/v1/web/admin/users/90/role",
        headers=headers,
        json={"role": "viewer", "role_override": True},
    )
    missing = browser.put(
        "/api/v1/web/admin/users/91/role",
        headers=headers,
        json={"role": "viewer", "role_override": True},
    )
    self_disable = browser.put(
        "/api/v1/web/admin/users/1/status",
        headers=headers,
        json={"status": 0},
    )
    wrong_source = browser.post(
        "/api/v1/web/admin/users/93/password/reset",
        headers=headers,
        json={"temporary_password": "Reset@Password123"},
    )

    assert (conflict.status_code, conflict.json()["code"]) == (
        409,
        "ACCOUNT_SOURCE_CONFLICT",
    )
    assert (weak.status_code, weak.json()["code"]) == (
        422,
        "PASSWORD_POLICY_VIOLATION",
    )
    assert (protected.status_code, protected.json()["code"]) == (
        409,
        "LAST_ADMIN_PROTECTED",
    )
    assert (missing.status_code, missing.json()["code"]) == (404, "NOT_FOUND")
    assert (self_disable.status_code, self_disable.json()["code"]) == (
        403,
        "FORBIDDEN",
    )
    assert wrong_source.status_code == 409


def test_non_admin_and_missing_bearer_are_rejected() -> None:
    viewer, _, _ = client("viewer")
    assert viewer.get("/api/v1/web/admin/users").status_code == 401
    forbidden = viewer.get(
        "/api/v1/web/admin/users",
        headers={"Authorization": "Bearer viewer.jwt"},
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "FORBIDDEN"
