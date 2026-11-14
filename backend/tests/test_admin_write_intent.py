"""写入边界重新计算授权用途；拒绝时不得发出锁或业务 SQL。"""

import logging
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import DBAPIError

from app.core.auth.admin_authorization import (
    AdminAuthorization,
    hold_admin_authorization,
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


class _FailingEngine:
    """只模拟 begin() 的事务上下文；授权锁查询按给定结果返回或抛出。"""

    def __init__(self, lock_result):
        self.connection = AsyncMock()
        if isinstance(lock_result, BaseException):
            self.connection.scalar.side_effect = lock_result
        else:
            self.connection.scalar.return_value = lock_result

    def begin(self):
        engine = self

        class _Transaction:
            async def __aenter__(self):
                return engine.connection

            async def __aexit__(self, *_):
                return False

        return _Transaction()


class _DriverError(Exception):
    sqlstate = "23514"


def _database_error():
    return DBAPIError(
        "INSERT INTO app (name) VALUES ('do-not-log-statement')",
        {"name": "do-not-log-parameter"},
        _DriverError("do-not-log-detail"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["lock", "transaction"])
async def test_database_failure_stays_opaque_to_client_but_is_diagnosable(phase, caplog):
    """503 响应不变；日志留下阶段、异常类型与 SQLSTATE，不含 SQL、参数或错误正文。"""
    intent = admin_intent("user_role_change", "7", {"role": "viewer", "role_override": True})
    engine = _FailingEngine(_database_error() if phase == "lock" else True)

    with (
        caplog.at_level(logging.ERROR, logger="app.core.auth.admin_authorization"),
        pytest.raises(ApiError) as denied,
    ):
        async with hold_admin_authorization(
            SimpleNamespace(database_url="unused"),
            authorization(intent),
            operation="user_role_change",
            target="7",
            parameters={"role": "viewer", "role_override": True},
            engine=engine,
        ):
            raise _database_error()

    assert denied.value.status_code == 503
    assert denied.value.code == "AUTH_SESSION_UNAVAILABLE"
    assert denied.value.__cause__ is None
    [record] = caplog.records
    assert record.getMessage() == (
        "admin authorization database failure: "
        f"phase={phase} operation=user_role_change "
        "error=DBAPIError driver_error=_DriverError sqlstate=23514"
    )
    assert "do-not-log" not in caplog.text
