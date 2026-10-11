"""部门边界的等价空白、角色及持久化前拒绝回归。"""

from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.auth.accounts import LocalAccountRecord, SecurityPrincipal
from app.core.auth.backends import InvalidCredentials
from app.core.auth.department import (
    InvalidAccountDepartment,
    normalize_account_department,
    require_account_department,
    valid_account_department,
)
from app.core.auth.local import LocalPasswordProvider
from app.core.errors import ApiError
from app.services.user_management import UserManagementService
from tests.test_local_auth_provider import FakeLocalRepository, RecordingHasher, account
from tests.test_user_management import AUTHORIZATION, FakeRepository


@pytest.mark.parametrize("role", ["operator", "viewer", "approver"])
@pytest.mark.parametrize("dept", ["", " ", "\t\r\n", "\u3000", "\u00a0"])
def test_nonadmin_blank_rejected_at_input_principal_and_scope(role, dept):
    with pytest.raises(InvalidAccountDepartment):
        normalize_account_department(role, dept)
    with pytest.raises(ValueError):
        SecurityPrincipal(1, 2, "synthetic", dept, role)
    with pytest.raises(ApiError) as error:
        require_account_department(role, dept)
    assert error.value.status_code == 403


@pytest.mark.parametrize("role", ["admin", "operator", "viewer", "approver"])
def test_normalization_length_and_authoritative_snapshot(role):
    assert normalize_account_department(role, " 部门 ") == "部门"
    assert normalize_account_department(role, "\t" + "部" * 128) == "部" * 128
    with pytest.raises(InvalidAccountDepartment):
        normalize_account_department(role, "部" * 129)
    assert not valid_account_department(role, " 部门 ")
    assert valid_account_department(role, "部门")
    assert valid_account_department(role, "") == (role == "admin")


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["operator", "viewer", "approver"])
async def test_invalid_creation_never_hashes_or_writes(role):
    repository = FakeRepository()
    hasher = Mock()
    service = UserManagementService(repository, hasher)
    with pytest.raises(InvalidAccountDepartment):
        await service.create_local(
            username="synthetic",
            display_name="Synthetic",
            dept="\u3000",
            role=role,
            temporary_password="Valid@Temporary123",
            authorization=AUTHORIZATION,
            actor="admin",
            ip="127.0.0.1",
        )
    hasher.hash.assert_not_called()
    assert not repository.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["operator", "viewer", "approver"])
@pytest.mark.parametrize("dept", ["", "\u3000", " 部门 "])
async def test_valid_password_cannot_authenticate_invalid_historical_department(role, dept):
    value = replace(account(), role=role, dept=dept)
    provider = LocalPasswordProvider(
        FakeLocalRepository(LocalAccountRecord(value, "synthetic")),
        RecordingHasher(matches=True),
    )
    with pytest.raises(InvalidCredentials):
        await provider.authenticate("admin", "Valid@Password123")


@pytest.mark.asyncio
async def test_empty_admin_remains_usable():
    value = replace(account(), dept="")
    provider = LocalPasswordProvider(
        FakeLocalRepository(LocalAccountRecord(value, "synthetic")),
        RecordingHasher(matches=True),
    )
    assert (await provider.authenticate("admin", "Valid@Password123")).account == value


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["operator", "viewer", "approver"])
async def test_dashboard_rejects_before_repository_query(role):
    from app.services.dashboard import DashboardService

    repository = AsyncMock()
    with pytest.raises(ApiError):
        await DashboardService(repository, ()).get(role=role, dept="")
    repository.load.assert_not_called()


@pytest.mark.parametrize("role", ["admin", "operator", "viewer", "approver"])
@pytest.mark.parametrize("dept", ["", "\u3000", " 部门 ", "部" * 128, "部" * 129])
def test_api_model_uses_same_department_contract(role, dept):
    from pydantic import ValidationError

    from app.api.users import LocalUserCreateModel

    args = dict(
        username="synthetic",
        display_name="Synthetic",
        dept=dept,
        role=role,
        temporary_password="Valid@Temporary123",
    )
    try:
        expected = normalize_account_department(role, dept)
    except InvalidAccountDepartment:
        with pytest.raises(ValidationError):
            LocalUserCreateModel(**args)
    else:
        assert LocalUserCreateModel(**args).dept == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["operator", "viewer", "approver"])
async def test_real_jwt_rejects_signed_empty_department_before_principal_binding(role):
    from app.core.auth.jwt import JwtClaims, JwtService
    from tests.test_auth_runtime import SECRET, FakeKeyValue

    service = JwtService(SECRET, FakeKeyValue())
    claims = JwtClaims(
        account_id=8,
        identity_id=18,
        provider_code="local",
        login_name="synthetic",
        display_name="Synthetic",
        dept="",
        role=role,
        security_version=1,
    )
    token = service.issue(claims)
    with pytest.raises(InvalidCredentials):
        await service.verify(token)
