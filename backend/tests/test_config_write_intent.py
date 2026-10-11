"""全局配置授权绑定键、规范值与版本，凭据只记录操作。"""

import json
from datetime import UTC, datetime

import pytest

from app.core.errors import ApiError
from app.services.admin import (
    ConfigUpdate,
    InvalidAdminQuery,
    canonical_config_intent_items,
    config_write_parameters,
    normalize_config_updates,
)
from app.services.admin_step_up import admin_intent

REVISION = datetime(2026, 1, 2, tzinfo=UTC)


def intent(items):
    return admin_intent("config_update", "default", {"items": items})


def test_order_whitespace_and_timezone_normalize_to_same_write():
    updates = (
        ConfigUpdate("vendor_qps", " 5 ", REVISION),
        ConfigUpdate("unsubscribe_auto_append", " TRUE ", REVISION),
    )
    actual = config_write_parameters(updates)
    supplied = [
        {
            "key": "unsubscribe_auto_append",
            "value": "true",
            "expected_updated_at": "2026-01-02T08:00:00+08:00",
        },
        {"key": "vendor_qps", "value": "5", "expected_updated_at": REVISION},
    ]
    assert intent(supplied) == intent(actual["items"])
    assert [item.value for item in normalize_config_updates(updates)] == ["5", "true"]


@pytest.mark.parametrize(
    "value,action",
    [
        (None, "keep"),
        ("", "clear"),
        ("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=synthetic", "replace"),
    ],
)
def test_sensitive_value_is_excluded_from_intent(value, action):
    parameters = config_write_parameters((ConfigUpdate("alert_wecom_webhook", value, REVISION),))
    assert parameters["items"] == [
        {
            "key": "alert_wecom_webhook",
            "secret_action": action,
            "expected_updated_at": "2026-01-02T00:00:00.000000+00:00",
        }
    ]
    assert "synthetic" not in json.dumps(parameters)
    assert "https" not in json.dumps(parameters)
    assert intent(parameters["items"]).operation == "config_update"


@pytest.mark.parametrize("change", ["value", "key", "revision", "missing-key"])
def test_changed_write_cannot_reuse_grant(change):
    original = config_write_parameters(
        (
            ConfigUpdate("vendor_qps", "5", REVISION),
            ConfigUpdate("report_poll_seconds", "60", REVISION),
        )
    )["items"]
    changed = [dict(item) for item in original]
    if change == "value":
        changed[0]["value"] = "90"
    elif change == "key":
        changed[0]["key"] = "reply_poll_seconds"
    elif change == "revision":
        changed[0]["expected_updated_at"] = "2026-01-02T00:00:01Z"
    else:
        changed.pop()
    assert intent(original) != intent(changed)


@pytest.mark.parametrize(
    "items",
    [
        [],
        {},
        [None],
        [{"key": "vendor_qps", "value": "5", "expected_updated_at": None}],
        [{"key": "vendor_qps", "value": True, "expected_updated_at": REVISION}],
        [{"key": "vendor_qps", "value": "5", "expected_updated_at": "2026-01-02"}],
        [{"key": "__internal", "value": "5", "expected_updated_at": REVISION}],
        [
            {
                "key": "security_daily_resend_api_key",
                "secret_action": "replace",
                "expected_updated_at": REVISION,
            }
        ],
        [
            {
                "key": "security_daily_recipient_set_digest",
                "value": "digest",
                "expected_updated_at": REVISION,
            }
        ],
        [{"key": "alert_wecom_webhook", "value": "secret", "expected_updated_at": REVISION}],
        [
            {
                "key": "alert_wecom_webhook",
                "secret_action": "secret",
                "expected_updated_at": REVISION,
            }
        ],
        [{"key": "vendor_qps", "value": "5", "expected_updated_at": REVISION}] * 2,
    ],
)
def test_invalid_or_credential_bearing_intent_is_rejected(items):
    with pytest.raises(ApiError) as rejected:
        intent(items)
    assert rejected.value.status_code == 422
    assert rejected.value.code == "INVALID_PARAM"


def test_keep_is_only_allowed_for_sensitive_config():
    with pytest.raises(InvalidAdminQuery):
        config_write_parameters((ConfigUpdate("vendor_qps", None, REVISION),))
    with pytest.raises(InvalidAdminQuery):
        canonical_config_intent_items(
            [{"key": "vendor_qps", "secret_action": "keep", "expected_updated_at": REVISION}]
        )
