from __future__ import annotations

import pytest

from app.core.auth.identity import (
    InvalidLoginName,
    normalize_login_name,
    validate_local_login_name,
)


def test_normalize_login_name_is_trimmed_and_case_insensitive() -> None:
    assert normalize_login_name("  Admin.User-01  ") == "admin.user-01"


@pytest.mark.parametrize(
    "value",
    (
        "local.user",
        "service_account",
        "admin-01",
        "abc",
        "a" * 64,
    ),
)
def test_validate_local_login_name_returns_normalized_value(value: str) -> None:
    assert validate_local_login_name(value.upper()) == value


@pytest.mark.parametrize(
    "value",
    (
        "ab",
        "bad name",
        "用户01",
        "admin@corp",
        "a" * 65,
        "   ",
    ),
)
def test_validate_local_login_name_rejects_invalid_values(value: str) -> None:
    with pytest.raises(InvalidLoginName, match="3–64"):
        validate_local_login_name(value)
