"""写入边界重新计算授权用途；拒绝时不得发出锁或业务 SQL。"""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from app.core.auth.admin_authorization import (
    AdminAuthorization,
    lock_admin_authorization,
    require_admin_intent,
)
from app.core.errors import ApiError
from app.services.admin_step_up import admin_intent


def authorization(intent=None):
    return AdminAuthorization(1, 2, 3, "local", intent)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["parameters", "target", "operation", "missing", "fingerprint"])
async def test_mismatch_fails_before_any_database_statement(change):
    intent = admin_intent("user_role_change", "7", {"role": "viewer", "role_override": True})
    grant = authorization(intent)
    actual = dict(
        operation="user_role_change",
        target="7",
        parameters={"role": "viewer", "role_override": True},
    )
    if change == "parameters":
        actual["parameters"]["role"] = "admin"
    elif change == "target":
        actual["target"] = "8"
    elif change == "operation":
        actual.update(operation="user_status_change", parameters={"status": 0})
    elif change == "missing":
        grant = authorization()
    else:
        grant = authorization(replace(intent, fingerprint="0" * 64))
    connection = AsyncMock()
    with pytest.raises(ApiError) as rejected:
        await lock_admin_authorization(connection, grant, **actual)
    assert rejected.value.code == "STEP_UP_REQUIRED"
    assert rejected.value.status_code == 401
    connection.execute.assert_not_awaited()


@pytest.mark.parametrize("role", ["approver", "operator", "viewer"])
def test_only_non_admin_creation_can_omit_step_up_intent(role):
    require_admin_intent(
        authorization(),
        operation="user_create",
        target="new",
        parameters={
            "username": "synthetic",
            "display_name": "Synthetic",
            "dept": "department",
            "role": role,
        },
    )


@pytest.mark.parametrize("change", ["admin", "operation", "target", "extra", "missing"])
def test_non_admin_creation_exception_is_narrow(change):
    actual = dict(
        operation="user_create",
        target="new",
        parameters={
            "username": "synthetic",
            "display_name": "Synthetic",
            "dept": "department",
            "role": "viewer",
        },
    )
    if change == "admin":
        actual["parameters"]["role"] = "admin"
    elif change == "operation":
        actual["operation"] = "user_create_admin"
    elif change == "target":
        actual["target"] = "7"
    elif change == "extra":
        actual["parameters"]["status"] = 1
    else:
        del actual["parameters"]["dept"]
    with pytest.raises(ApiError, match="二次认证用途或参数不匹配"):
        require_admin_intent(authorization(), **actual)


def test_authorized_canonical_creation_matches_normalized_database_input():
    intent = admin_intent(
        "user_create_admin",
        "new",
        {
            "username": "synthetic",
            "display_name": " Synthetic ",
            "dept": " ",
            "role": "admin",
        },
    )
    require_admin_intent(
        authorization(intent),
        operation="user_create_admin",
        target="new",
        parameters={
            "username": "synthetic",
            "display_name": "Synthetic",
            "dept": "",
            "role": "admin",
        },
    )
