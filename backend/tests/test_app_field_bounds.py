import pytest
from pydantic import ValidationError

from app.services.app_contract import AppCreateRequest
from app.services.app_management import AppCreate, AppManagementService, InvalidAppConfig


def _request(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": "oa",
        "dept": "业务一部",
        "allowed_categories": ["notice"],
    }
    payload.update(overrides)
    return payload


def test_app_create_rejects_overlong_sign_and_callback_before_persistence() -> None:
    AppCreateRequest.model_validate(
        _request(default_sign="签" * 32, callback_url="https://x.test/" + "a" * 240)
    )
    with pytest.raises(ValidationError):
        AppCreateRequest.model_validate(_request(default_sign="签" * 33))
    with pytest.raises(ValidationError):
        AppCreateRequest.model_validate(_request(callback_url="https://x.test/" + "a" * 250))


def test_only_app_name_unique_violation_becomes_name_conflict() -> None:
    from sqlalchemy.exc import IntegrityError

    from app.services.app_repository import _is_app_name_conflict

    class NameConflict(Exception):
        sqlstate = "23505"
        constraint_name = "app_name_key"

    class OtherConstraint(Exception):
        sqlstate = "23505"
        constraint_name = "app_pkey"

    assert _is_app_name_conflict(IntegrityError("INSERT", {}, NameConflict()))
    assert not _is_app_name_conflict(IntegrityError("INSERT", {}, OtherConstraint()))


def test_service_validation_rejects_overlong_fields_before_dns() -> None:
    service = AppManagementService.__new__(AppManagementService)
    config = AppCreate(
        name="oa",
        dept="业务一部",
        allowed_categories=frozenset({"notice"}),
        default_sign="签" * 33,
    )
    with pytest.raises(InvalidAppConfig, match="默认签名"):
        service._validate(config)
