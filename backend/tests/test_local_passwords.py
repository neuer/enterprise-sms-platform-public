from __future__ import annotations

import pytest

from app.core.auth.passwords import (
    LocalPasswordHasher,
    PasswordPolicy,
    PasswordPolicyViolation,
    generate_temporary_password,
)


@pytest.mark.parametrize(
    ("password", "message"),
    (
        ("Short@123", "12–128"),
        ("a" * 129, "12–128"),
        ("alllowercase123", "至少三类"),
        ("AdminUser@123", "不能包含用户名"),
    ),
)
def test_password_policy_rejects_invalid_passwords(password: str, message: str) -> None:
    with pytest.raises(PasswordPolicyViolation, match=message):
        PasswordPolicy().validate(password, username="adminuser")


@pytest.mark.parametrize(
    "password",
    (
        "ValidPassword123",
        "Valid@Password",
        "Valid@123456",
        "有效密码Valid@123",
    ),
)
def test_password_policy_accepts_three_or_more_character_classes(password: str) -> None:
    PasswordPolicy().validate(password, username="operator")


def test_password_policy_exposes_frontend_contract() -> None:
    assert PasswordPolicy().public_contract() == {
        "min_length": 12,
        "max_length": 128,
        "required_character_classes": 3,
        "forbid_username": True,
        "description": "12–128 位，至少包含大小写字母、数字、特殊字符中的三类，不能包含用户名",
    }


def test_generated_temporary_password_is_twenty_characters_and_policy_compliant() -> None:
    password = generate_temporary_password(username="admin")

    assert len(password) == 20
    assert any(character.islower() for character in password)
    assert any(character.isupper() for character in password)
    assert any(character.isdigit() for character in password)
    assert any(not character.isalnum() for character in password)
    PasswordPolicy().validate(password, username="admin")


def test_argon2_hash_verification_never_embeds_plaintext() -> None:
    hasher = LocalPasswordHasher()
    password = "Valid@Password123"

    encoded = hasher.hash(password)

    assert encoded.startswith("$argon2id$")
    assert password not in encoded
    assert hasher.verify(encoded, password)
    assert not hasher.verify(encoded, "Wrong@Password123")


def test_missing_or_malformed_hash_uses_uniform_failure_path() -> None:
    hasher = LocalPasswordHasher()

    assert not hasher.verify_or_dummy(None, "Wrong@Password123")
    assert not hasher.verify_or_dummy("not-an-argon2-hash", "Wrong@Password123")
