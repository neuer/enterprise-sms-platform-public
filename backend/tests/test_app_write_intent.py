"""应用授权参数与版本的确定性绑定；凭据绝不进入配置指纹。"""

from datetime import UTC, datetime

import pytest

from app.core.errors import ApiError
from app.services.admin_step_up import admin_intent
from app.services.app_contract import AppCreateRequest, AppUpdateRequest

REVISION = datetime(2026, 1, 2, tzinfo=UTC)


def test_route_and_sql_normalization_share_the_same_intent():
    config = AppCreateRequest(
        name="synthetic",
        dept="synthetic",
        allowed_categories=["notice", "market", "notice"],
        allowed_ips=["192.0.2.23/24", "192.0.2.0/24"],
        freq_override={},
        admission_exempt_note=" test ",
        callback_url="",
        ip_allowlist_exempt_until="2026-01-02T08:00:00+08:00",
    ).model_dump()
    sql = dict(
        config,
        allowed_categories="market,notice",
        allowed_ips=("192.0.2.0/24",),
        freq_override=None,
        admission_exempt_note="test",
        callback_url=None,
        ip_allowlist_exempt_until=REVISION,
    )
    assert admin_intent("app_create", "new", {"config": config}) == admin_intent(
        "app_create",
        "new",
        {"config": sql},
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("daily_quota", 3),
        ("dept", "other"),
        ("status", 0),
        ("callback_url", "https://callback.example.test/path"),
        ("allowed_ips", ["192.0.2.0/24"]),
        ("blacklist_check", False),
    ],
)
def test_security_configuration_changes_invalidate_grant(field, value):
    config = AppUpdateRequest(
        dept="synthetic",
        allowed_categories=["notice"],
        daily_quota=0,
        rate_limit_per_min=60,
        blacklist_check=True,
    ).model_dump()
    baseline = admin_intent("app_update", "7", {"config": config, "expected_updated_at": REVISION})
    altered = admin_intent(
        "app_update",
        "7",
        {
            "config": {**config, field: value},
            "expected_updated_at": REVISION,
        },
    )
    assert altered != baseline


@pytest.mark.parametrize(
    "operation",
    [
        "app_update",
        "app_disable",
        "app_rotate_key",
        "app_revoke_old_key",
        "app_rotate_callback_secret",
    ],
)
@pytest.mark.parametrize("revision", [None, 123, "2026-01-02", datetime(2026, 1, 2), "bad"])
def test_existing_app_requires_explicit_aware_revision(operation, revision):
    parameters = {"expected_updated_at": revision}
    if operation == "app_update":
        parameters["config"] = AppUpdateRequest(
            dept="synthetic",
            allowed_categories=["notice"],
            daily_quota=0,
            rate_limit_per_min=60,
            blacklist_check=True,
        ).model_dump()
    with pytest.raises(ApiError) as rejected:
        admin_intent(operation, "7", parameters)
    assert rejected.value.code == "INVALID_PARAM"


@pytest.mark.parametrize(
    "key", ["api_key", "api_key_hash", "callback_secret", "callback_secret_enc"]
)
def test_credentials_are_rejected_instead_of_hashed(key):
    config = AppCreateRequest(name="synthetic", dept="synthetic").model_dump()
    config[key] = "synthetic-sensitive"
    with pytest.raises(ApiError):
        admin_intent("app_create", "new", {"config": config})


def test_revision_and_target_bind_rotation():
    parameters = {"expected_updated_at": REVISION}
    original = admin_intent("app_rotate_key", "7", parameters)
    assert original == admin_intent(
        "app_rotate_key",
        "07",
        {
            "expected_updated_at": "2026-01-02T08:00:00+08:00",
        },
    )
    assert original != admin_intent("app_rotate_key", "8", parameters)
    assert original != admin_intent("app_revoke_old_key", "7", parameters)
    assert original != admin_intent(
        "app_rotate_key",
        "7",
        {
            "expected_updated_at": "2026-01-02T00:00:00.000001+00:00",
        },
    )


@pytest.mark.parametrize("note", [None, "", "   ", "\t\n", "\u3000", " rationale "])
@pytest.mark.parametrize("create", [True, False])
def test_note_normalization_is_idempotent_across_write_boundaries(note, create):
    from app.services.app_contract import canonical_app_parameters

    if create:
        config = AppCreateRequest(name="synthetic", dept="synthetic").model_dump()
    else:
        config = AppUpdateRequest(
            dept="synthetic",
            allowed_categories=["notice"],
            daily_quota=100,
            rate_limit_per_min=60,
            blacklist_check=True,
        ).model_dump()
    config["admission_exempt_note"] = note
    canonical = canonical_app_parameters(config, create=create)
    assert canonical == canonical_app_parameters(canonical, create=create)
    assert canonical["admission_exempt_note"] == (note.strip() or None if note else None)
