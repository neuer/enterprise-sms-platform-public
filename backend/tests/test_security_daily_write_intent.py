"""安全日报写入意图不包含凭据，失败路径不能进入文件发布。"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.auth.accounts import SecurityPrincipal
from app.core.auth.admin_authorization import AdminAuthorization
from app.core.errors import ApiError
from app.services.admin_step_up import admin_intent
from app.services.security_daily.contract import (
    SecurityDailyConfigurationUpdate,
    SecurityDailyStateConflict,
    security_daily_write_parameters,
)
from app.services.security_daily.service import SecurityDailyService


@pytest.mark.parametrize("key,action", [(None, "keep"), ("", "clear"), ("re_synthetic", "replace")])
def test_intent_records_only_key_action(key, action):
    update = SecurityDailyConfigurationUpdate(True, (" owner@example.test ",), key)
    parameters = security_daily_write_parameters(update, 7)
    assert parameters == {
        "enabled": True,
        "recipients": ["owner@example.test"],
        "api_key_action": action,
        "expected_config_version": 7,
    }
    intent = admin_intent("security_daily_config_update", "default", parameters)
    assert intent.operation == "security_daily_config_update"
    assert "re_synthetic" not in json.dumps(parameters)
    other_key = "re_different" if action == "replace" else key
    assert intent == admin_intent(
        "security_daily_config_update",
        "default",
        security_daily_write_parameters(
            SecurityDailyConfigurationUpdate(True, ("owner@example.test",), other_key),
            7,
        ),
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("enabled", False),
        ("recipients", ["other@example.test"]),
        ("api_key_action", "clear"),
        ("expected_config_version", 8),
    ],
)
def test_noncredential_change_invalidates_intent(field, value):
    parameters = security_daily_write_parameters(
        SecurityDailyConfigurationUpdate(True, ("owner@example.test",), None),
        7,
    )
    assert admin_intent("security_daily_config_update", "default", parameters) != admin_intent(
        "security_daily_config_update",
        "default",
        {**parameters, field: value},
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("enabled", "true"),
        ("api_key_action", "re_raw_key"),
        ("expected_config_version", True),
        ("expected_config_version", 0),
        ("expected_config_version", "7"),
        ("recipients", "owner@example.test"),
        ("recipients", [None]),
    ],
)
def test_invalid_intent_fails_closed(field, value):
    parameters = security_daily_write_parameters(SecurityDailyConfigurationUpdate(True, ()), 7)
    with pytest.raises(ApiError) as rejected:
        admin_intent("security_daily_config_update", "default", {**parameters, field: value})
    assert rejected.value.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ApiError(401, "STEP_UP_REQUIRED", "synthetic revocation", None),
        SecurityDailyStateConflict("synthetic stale revision"),
        RuntimeError("synthetic audit failure"),
    ],
)
async def test_rejected_database_write_does_not_publish_configuration(error):
    update = SecurityDailyConfigurationUpdate(True, ("owner@example.test",), "re_synthetic")
    grant = AdminAuthorization(
        1,
        2,
        1,
        "local",
        admin_intent(
            "security_daily_config_update",
            "default",
            security_daily_write_parameters(update, 7),
        ),
    )
    repository = SimpleNamespace(update_configuration=AsyncMock(side_effect=error))
    control = SimpleNamespace(sync_configuration=AsyncMock())
    service = SecurityDailyService(repository, control)
    with pytest.raises(type(error)):
        await service.configure(
            update,
            principal=SecurityPrincipal(1, 2, "synthetic", "", "admin"),
            ip="127.0.0.1",
            authorization=grant,
            expected_config_version=7,
        )
    control.sync_configuration.assert_not_awaited()
    repository.update_configuration.assert_awaited_once_with(
        update,
        principal=SecurityPrincipal(1, 2, "synthetic", "", "admin"),
        ip="127.0.0.1",
        authorization=grant,
        expected_config_version=7,
    )
