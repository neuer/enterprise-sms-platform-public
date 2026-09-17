from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core.auth.admin_authorization import AdminAuthorization
from app.core.auth.password_screening import OfflinePasswordScreen, PasswordScreeningUnavailable
from app.core.auth.passwords import (
    PasswordPolicy,
    PasswordPolicyViolation,
    generate_temporary_password,
)
from app.settings import Settings

AUTHORIZATION = AdminAuthorization(1, 11, 1, "local", None)

BAD = "PreviouslyLeaked@123"


def corpus(path: Path, **changes: object) -> Path:
    data = dict(
        version=1,
        source_ref="synthetic-fixture",
        expires_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
        sha256=[hashlib.sha256(BAD.encode()).hexdigest()],
    )
    data.update(changes)
    path.write_text(json.dumps(data))
    return path


def test_corpus_rejects_leaked_password_and_preserves_temporary_generation(tmp_path: Path) -> None:
    policy = PasswordPolicy(
        screening=OfflinePasswordScreen.from_file(corpus(tmp_path / "corpus.json"))
    )
    with pytest.raises(PasswordPolicyViolation, match="泄露"):
        policy.validate(BAD, username="operator")
    policy.validate("DifferentRandom@123", username="operator")
    temporary = generate_temporary_password(username="operator", policy=policy)
    policy.validate(temporary, username="operator")
    assert BAD not in repr(policy)


@pytest.mark.parametrize(
    "changes",
    [
        dict(version=2),
        dict(sha256=[]),
        dict(sha256=["raw-password"]),
        dict(expires_at="2020-01-01T00:00:00+00:00"),
        dict(expires_at="2030-01-01T00:00:00"),
        dict(source_ref="https://untrusted.invalid"),
    ],
)
def test_invalid_or_expired_corpus_fails_closed(tmp_path: Path, changes: dict[str, object]) -> None:
    screen = OfflinePasswordScreen.from_file(corpus(tmp_path / "corpus.json", **changes))
    with pytest.raises(PasswordScreeningUnavailable):
        screen.contains(BAD)


def test_missing_corpus_is_not_silently_skipped(tmp_path: Path) -> None:
    screen = OfflinePasswordScreen.from_file(tmp_path / "absent.json")
    with pytest.raises(PasswordScreeningUnavailable):
        screen.contains(BAD)
    # 同一失败不会进入临时密码无限重试循环。
    with pytest.raises(PasswordScreeningUnavailable):
        generate_temporary_password(username="operator", policy=PasswordPolicy(screening=screen))


def test_production_requires_corpus_without_affecting_login_hashes() -> None:
    settings = Settings.model_construct(environment="production", auth_password_corpus_file=None)
    with pytest.raises(PasswordScreeningUnavailable):
        OfflinePasswordScreen.from_settings(settings).contains(BAD)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "reset"])
async def test_administrative_password_writes_screen_before_hash(
    tmp_path: Path,
    operation: str,
) -> None:
    from unittest.mock import Mock

    from app.services.user_management import UserManagementService
    from tests.test_user_management import FakeRepository

    repository = FakeRepository()
    hasher = Mock()
    policy = PasswordPolicy(screening=OfflinePasswordScreen.from_file(corpus(tmp_path / "c.json")))
    service = UserManagementService(repository, hasher, policy)
    with pytest.raises(PasswordPolicyViolation):
        if operation == "create":
            await service.create_local(
                username="operator01",
                display_name="Synthetic",
                dept="Test",
                role="operator",
                temporary_password=BAD,
                authorization=AUTHORIZATION,
                actor="admin",
                ip="192.0.2.1",
            )
        else:
            await service.reset_password(
                8, BAD, authorization=AUTHORIZATION, actor="admin", ip="192.0.2.1"
            )
    hasher.hash.assert_not_called()
    assert repository.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("initial", [True, False])
@pytest.mark.parametrize("available", [True, False])
async def test_password_changes_enforce_screen_without_consuming_lease(
    tmp_path: Path,
    initial: bool,
    available: bool,
) -> None:
    from app.core.errors import ApiError
    from tests.test_auth_runtime import IP, TAB_ID, facade

    service, users, tokens, hasher = facade(must_change_password=initial)
    screen = (
        OfflinePasswordScreen.from_file(corpus(tmp_path / "c.json"))
        if available
        else OfflinePasswordScreen(True)
    )
    service.policy = PasswordPolicy(screening=screen)
    login = await service.login("local", "admin", "Temporary@123", IP, TAB_ID)
    with pytest.raises(ApiError) as error:
        if initial:
            await service.change_initial_password(login.change_token, BAD, IP)
        else:
            await service.change_password(login.token, "Current@Password123", BAD, IP)
    assert error.value.code == (
        "PASSWORD_POLICY_VIOLATION" if available else "AUTH_PROVIDER_UNAVAILABLE"
    )
    assert hasher.hashed == [] and users.changed == []
    if initial:
        digest = tokens.password_change_digest(login.change_token)
        assert users.password_change_tokens[digest]["status"] == "available"


@pytest.mark.parametrize("payload", ['{"version":1,"version":2}', "[" * 1500])
def test_malformed_corpus_does_not_escape_as_server_error(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "bad.json"
    path.write_text(payload)
    with pytest.raises(PasswordScreeningUnavailable):
        OfflinePasswordScreen.from_file(path).contains(BAD)
