from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.auth.backends import ProviderCapacityUnavailable
from app.core.auth.roles import Role
from app.core.bounded_executor import ExecutorBackpressure
from app.services.user_management import (
    SelfDisableDenied,
    UserManagementService,
    UserPage,
    UserQuery,
    UserRecord,
)


def record(
    *,
    provider_code: str = "local",
    must_change_password: bool | None = True,
) -> UserRecord:
    return UserRecord(
        account_id=8,
        identity_id=18,
        provider_code=provider_code,
        username="operator01",
        display_name="操作员",
        dept="业务一部",
        role="operator",
        role_override=provider_code == "local",
        status=1,
        identity_status=1,
        must_change_password=must_change_password,
        source_groups=() if provider_code == "local" else ("sms-operators",),
        last_synced_at=(None if provider_code == "local" else datetime(2026, 7, 16, 8, tzinfo=UTC)),
        last_login_at=None,
        security_version=3,
    )


class FakeRepository:
    def __init__(self) -> None:
        self.query: UserQuery | None = None
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.value = record()

    async def list(self, query: UserQuery) -> UserPage:
        self.query = query
        return UserPage((self.value,), 1, query.page, query.page_size)

    async def get(self, account_id: int) -> UserRecord:
        assert account_id == self.value.account_id
        return self.value

    async def create_local(
        self,
        *,
        username: str,
        display_name: str,
        dept: str,
        role: Role,
        password_hash: str,
        actor: str,
        ip: str,
    ) -> UserRecord:
        self.calls.append(
            (
                "create",
                (username, display_name, dept, role, password_hash, actor, ip),
            )
        )
        return self.value

    async def set_role(
        self,
        account_id: int,
        role: Role,
        role_override: bool,
        *,
        actor: str,
        ip: str,
    ) -> UserRecord:
        self.calls.append(("role", (account_id, role, role_override, actor, ip)))
        return self.value

    async def set_status(
        self,
        account_id: int,
        status: int,
        *,
        actor_account_id: int,
        actor: str,
        ip: str,
    ) -> UserRecord:
        self.calls.append(("status", (account_id, status, actor_account_id, actor, ip)))
        return self.value

    async def reset_local_password(
        self,
        account_id: int,
        password_hash: str,
        *,
        actor: str,
        ip: str,
    ) -> UserRecord:
        self.calls.append(("reset", (account_id, password_hash, actor, ip)))
        return self.value


class FakeHasher:
    def hash(self, password: str) -> str:
        return f"encoded:{password}"


@pytest.mark.asyncio
async def test_list_normalizes_all_filters_and_returns_stable_page() -> None:
    repository = FakeRepository()
    service = UserManagementService(repository, FakeHasher())

    page = await service.list("  操作  ", "local", "operator", 1, 2, 20)

    assert page.total == 1 and page.page == 2
    assert repository.query == UserQuery("操作", "local", "operator", 1, 2, 20)


@pytest.mark.asyncio
async def test_create_local_validates_username_and_password_before_hashing() -> None:
    repository = FakeRepository()
    service = UserManagementService(repository, FakeHasher())

    await service.create_local(
        username=" New.User ",
        display_name="新用户",
        dept="业务一部",
        role="viewer",
        temporary_password="Temporary@123",
        actor="admin",
        ip="10.0.0.8",
    )

    assert repository.calls == [
        (
            "create",
            (
                "new.user",
                "新用户",
                "业务一部",
                "viewer",
                "encoded:Temporary@123",
                "admin",
                "10.0.0.8",
            ),
        )
    ]


@pytest.mark.asyncio
async def test_self_disable_is_rejected_before_repository_mutation() -> None:
    repository = FakeRepository()
    service = UserManagementService(repository, FakeHasher())

    with pytest.raises(SelfDisableDenied):
        await service.change_status(
            8,
            0,
            actor_account_id=8,
            actor="admin",
            ip="10.0.0.8",
        )

    assert repository.calls == []


@pytest.mark.asyncio
async def test_role_status_and_local_reset_delegate_numeric_account_id() -> None:
    repository = FakeRepository()
    service = UserManagementService(repository, FakeHasher())

    await service.change_role(
        8,
        "approver",
        True,
        actor="admin",
        ip="10.0.0.8",
    )
    await service.change_status(
        8,
        1,
        actor_account_id=1,
        actor="admin",
        ip="10.0.0.8",
    )
    await service.reset_password(
        8,
        "Reset@Password123",
        actor="admin",
        ip="10.0.0.8",
    )

    assert repository.calls == [
        ("role", (8, "approver", True, "admin", "10.0.0.8")),
        ("status", (8, 1, 1, "admin", "10.0.0.8")),
        ("reset", (8, "encoded:Reset@Password123", "admin", "10.0.0.8")),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("create", "reset"))
async def test_password_mutations_map_hash_pool_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    async def saturated(*_args: object, **_kwargs: object) -> str:
        raise ExecutorBackpressure("full")

    monkeypatch.setattr("app.services.user_management.run_bounded", saturated)
    repository = FakeRepository()
    service = UserManagementService(repository, FakeHasher())

    with pytest.raises(ProviderCapacityUnavailable):
        if operation == "create":
            await service.create_local(
                username="new.user",
                display_name="新用户",
                dept="业务一部",
                role="viewer",
                temporary_password="Temporary@123",
                actor="admin",
                ip="10.0.0.8",
            )
        else:
            await service.reset_password(
                8,
                "Reset@Password123",
                actor="admin",
                ip="10.0.0.8",
            )

    assert repository.calls == []
