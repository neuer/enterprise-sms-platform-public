from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.auth.backends import InvalidCredentials
from app.core.auth.jwt import JwtClaims, JwtService, ReauthenticationRequired
from app.services.runtime_policy import RuntimePolicy
from tests.test_auth import FakeKeyValue

SECRET = "a-jwt-secret-that-is-long-enough-for-hs256-tests"
TAB_ID = "f" * 32


def claims(provider_code: str) -> JwtClaims:
    return JwtClaims(
        account_id=8,
        identity_id=18,
        provider_code=provider_code,
        login_name="user01",
        display_name="测试用户",
        dept="研发部",
        role="operator",
        security_version=3,
    )


@pytest.mark.asyncio
async def test_ad_family_uses_original_auth_time_and_revokes_at_exact_boundary() -> None:
    issued_at = datetime(2026, 9, 1, 8, tzinfo=UTC)
    moments = [issued_at]
    values = {"ad_session_max_age_minutes": "480"}

    async def policy() -> RuntimePolicy:
        return RuntimePolicy.from_mapping(values)

    store = FakeKeyValue()
    service = JwtService(
        SECRET,
        store,
        clock=lambda: moments[0],
        runtime_policy_loader=policy,
    )
    first = await service.issue_pair(claims("ad"), TAB_ID)
    moments[0] = issued_at + timedelta(minutes=479, seconds=59)
    second = await service.rotate_refresh(first.refresh_token, TAB_ID)

    moments[0] = issued_at + timedelta(minutes=480)
    with pytest.raises(ReauthenticationRequired):
        await service.rotate_refresh(second.refresh_token, TAB_ID)

    decoded = service._decode(second.refresh_token)  # noqa: SLF001
    session_id = str(decoded["sid"])
    assert f"auth:jwt:refresh-family:{session_id}" not in store.values
    assert store.values[f"auth:jwt:session-revoked:{session_id}"] == "1"

    values["ad_session_max_age_minutes"] = "10080"
    with pytest.raises(InvalidCredentials):
        await service.rotate_refresh(second.refresh_token, TAB_ID)


@pytest.mark.asyncio
async def test_local_refresh_family_is_not_limited_by_ad_max_age() -> None:
    issued_at = datetime(2026, 9, 1, 8, tzinfo=UTC)
    moments = [issued_at]
    service = JwtService(SECRET, FakeKeyValue(), clock=lambda: moments[0])
    first = await service.issue_pair(claims("local"), TAB_ID)

    moments[0] = issued_at + timedelta(days=1)
    rotated = await service.rotate_refresh(first.refresh_token, TAB_ID)

    assert rotated.refresh_token != first.refresh_token


@pytest.mark.asyncio
async def test_ad_policy_failure_does_not_rotate_or_revoke_family() -> None:
    store = FakeKeyValue()
    unavailable = [False]

    async def policy() -> RuntimePolicy:
        if unavailable[0]:
            raise OSError("database unavailable")
        return RuntimePolicy.from_mapping({})

    service = JwtService(SECRET, store, runtime_policy_loader=policy)
    first = await service.issue_pair(claims("ad"), TAB_ID)
    unavailable[0] = True

    rotated = await service.rotate_refresh(first.refresh_token, TAB_ID)
    assert rotated.refresh_token
    decoded = service._decode(first.refresh_token)  # noqa: SLF001
    session_id = str(decoded["sid"])
    assert f"auth:jwt:refresh-family:{session_id}" in store.values
    assert f"auth:jwt:session-revoked:{session_id}" not in store.values
