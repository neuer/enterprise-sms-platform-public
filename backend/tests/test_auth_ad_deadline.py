from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.auth.backends import InvalidCredentials, SessionStateUnavailable
from app.core.auth.jwt import (
    ACCESS_TOKEN_TYPE,
    JWT_AUDIENCE,
    JWT_ISSUER,
    JwtClaims,
    JwtService,
    ReauthenticationRequired,
)
from app.services.runtime_policy import RuntimePolicy
from tests.test_auth import TAB_ID, FakeKeyValue

SECRET = "a-jwt-secret-that-is-long-enough-for-hs256-tests"


def decode(token: str) -> dict[str, object]:
    return jwt.decode(
        token,
        SECRET,
        algorithms=["HS256"],
        audience=JWT_AUDIENCE,
        options={"verify_exp": False, "verify_iat": False},
    )


def ad_claims(
    *,
    now: datetime,
    deadline_s: int = 600,
    auth_time: float | None = None,
) -> JwtClaims:
    started = auth_time if auth_time is not None else now.timestamp()
    return JwtClaims(
        account_id=8,
        identity_id=18,
        provider_code="ad",
        login_name="ad.user",
        display_name="目录用户",
        dept="研发部",
        role="operator",
        security_version=1,
        auth_time=started,
        reauth_deadline=started + deadline_s,
        auth_policy_version=1,
    )


def local_claims() -> JwtClaims:
    return JwtClaims(
        account_id=8,
        identity_id=18,
        provider_code="local",
        login_name="operator01",
        display_name="本地用户",
        dept="业务部",
        role="operator",
        security_version=1,
    )


@pytest.mark.asyncio
async def test_ad_access_exp_is_truncated_to_reauth_deadline() -> None:
    now = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)
    service = JwtService(SECRET, FakeKeyValue(), clock=lambda: now)
    pair = await service.issue_pair(ad_claims(now=now, deadline_s=120), TAB_ID)
    payload = decode(pair.token)
    assert payload["exp"] == int(now.timestamp()) + 120
    assert pair.expires_in == 120


@pytest.mark.asyncio
async def test_ad_access_verify_rejects_at_exact_deadline() -> None:
    started = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)
    current = [started]
    service = JwtService(SECRET, FakeKeyValue(), clock=lambda: current[0])
    pair = await service.issue_pair(ad_claims(now=started, deadline_s=60), TAB_ID)
    current[0] = started + timedelta(seconds=60)
    with pytest.raises(ReauthenticationRequired):
        await service.verify(pair.token)


@pytest.mark.asyncio
async def test_ad_access_verify_rejects_after_deadline_without_refresh() -> None:
    started = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)
    current = [started]
    service = JwtService(SECRET, FakeKeyValue(), clock=lambda: current[0])
    pair = await service.issue_pair(ad_claims(now=started, deadline_s=60), TAB_ID)
    current[0] = started + timedelta(seconds=61)
    with pytest.raises(ReauthenticationRequired):
        await service.verify(pair.token)


@pytest.mark.asyncio
async def test_ad_refresh_cannot_extend_auth_time_or_deadline() -> None:
    started = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)
    current = [started]
    service = JwtService(SECRET, FakeKeyValue(), clock=lambda: current[0])
    claims = ad_claims(now=started, deadline_s=3600)
    pair = await service.issue_pair(claims, TAB_ID)
    current[0] = started + timedelta(minutes=10)
    rotated = await service.rotate_refresh(pair.refresh_token, TAB_ID)
    access = decode(rotated.token)
    refresh = decode(rotated.refresh_token)
    assert access["auth_time"] == claims.auth_time
    assert access["reauth_deadline"] == claims.reauth_deadline
    assert refresh["auth_time"] == claims.auth_time
    assert refresh["reauth_deadline"] == claims.reauth_deadline


@pytest.mark.asyncio
async def test_ad_grace_reconstruction_preserves_auth_time_and_deadline() -> None:
    started = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)
    current = [started]
    service = JwtService(SECRET, FakeKeyValue(), clock=lambda: current[0])
    claims = ad_claims(now=started, deadline_s=3600)
    pair = await service.issue_pair(claims, TAB_ID)
    first = await service.rotate_refresh(pair.refresh_token, TAB_ID)
    current[0] = started + timedelta(seconds=1)
    replayed_access = decode(first.token)
    assert replayed_access["auth_time"] == claims.auth_time
    assert replayed_access["reauth_deadline"] == claims.reauth_deadline


@pytest.mark.asyncio
async def test_local_access_ttl_remains_unchanged() -> None:
    now = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)
    service = JwtService(SECRET, FakeKeyValue(), clock=lambda: now)
    pair = await service.issue_pair(local_claims(), TAB_ID)
    payload = decode(pair.token)
    assert pair.expires_in == 900
    assert payload["exp"] == int((now + timedelta(minutes=15)).timestamp())
    assert "reauth_deadline" not in payload


@pytest.mark.asyncio
async def test_login_response_returns_dynamic_expires_in() -> None:
    now = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)
    service = JwtService(SECRET, FakeKeyValue(), clock=lambda: now)
    pair = await service.issue_pair(ad_claims(now=now, deadline_s=30), TAB_ID)
    assert 1 <= pair.expires_in <= 900
    assert pair.expires_in == 30


@pytest.mark.asyncio
async def test_ad_expired_access_revokes_refresh_family_atomically() -> None:
    started = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)
    current = [started]
    store = FakeKeyValue()
    service = JwtService(SECRET, store, clock=lambda: current[0])
    pair = await service.issue_pair(ad_claims(now=started, deadline_s=10), TAB_ID)
    current[0] = started + timedelta(seconds=10)
    with pytest.raises(ReauthenticationRequired):
        await service.verify(pair.token)
    with pytest.raises((InvalidCredentials, ReauthenticationRequired, SessionStateUnavailable)):
        await service.rotate_refresh(pair.refresh_token, TAB_ID)


@pytest.mark.asyncio
async def test_concurrent_deadline_requests_are_idempotent() -> None:
    started = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)
    current = [started]
    store = FakeKeyValue()
    service = JwtService(SECRET, store, clock=lambda: current[0])
    pair = await service.issue_pair(
        ad_claims(now=started, deadline_s=10),
        TAB_ID,
    )
    current[0] = started + timedelta(seconds=10)
    with pytest.raises(ReauthenticationRequired):
        await service.verify(pair.token)
    with pytest.raises(ReauthenticationRequired):
        await service.verify(pair.token)


@pytest.mark.asyncio
async def test_redis_failure_at_deadline_fails_closed() -> None:
    class BrokenStore(FakeKeyValue):
        async def get(self, key: str) -> object:
            raise RuntimeError("redis down")

        async def eval(self, script: str, numkeys: int, *args: object) -> object:
            raise RuntimeError("redis down")

    started = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)
    service = JwtService(
        SECRET,
        BrokenStore(),
        clock=lambda: started + timedelta(seconds=10),
    )
    token = jwt.encode(
        {
            "iss": JWT_ISSUER,
            "aud": JWT_AUDIENCE,
            "sub": "8",
            "identity_id": 18,
            "provider_code": "ad",
            "login_name": "ad.user",
            "display_name": "目录用户",
            "dept": "研发部",
            "role": "operator",
            "security_version": 1,
            "token_type": ACCESS_TOKEN_TYPE,
            "sid": "s" * 32,
            "jti": "j" * 32,
            "iat": started.timestamp(),
            "exp": int((started + timedelta(minutes=15)).timestamp()),
            "auth_time": started.timestamp(),
            "reauth_deadline": started.timestamp() + 10,
        },
        SECRET,
        algorithm="HS256",
        headers={"kid": "1"},
    )
    with pytest.raises(SessionStateUnavailable):
        await service.verify(token)


@pytest.mark.asyncio
async def test_policy_decrease_invalidates_overage_ad_sessions() -> None:
    started = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)
    current = [started + timedelta(minutes=30)]
    store = FakeKeyValue()
    service = JwtService(SECRET, store, clock=lambda: current[0])
    pair = await service.issue_pair(ad_claims(now=started, deadline_s=8 * 3600), TAB_ID)
    await service.publish_ad_session_policy(15, version=2)
    with pytest.raises(ReauthenticationRequired):
        await service.verify(pair.token)


@pytest.mark.asyncio
async def test_policy_increase_does_not_extend_existing_deadline() -> None:
    started = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)
    current = [started]
    store = FakeKeyValue()

    async def longer() -> RuntimePolicy:
        return RuntimePolicy.from_mapping({"ad_session_max_age_minutes": "960"})

    service = JwtService(
        SECRET,
        store,
        clock=lambda: current[0],
        runtime_policy_loader=longer,
    )
    pair = await service.issue_pair(ad_claims(now=started, deadline_s=60), TAB_ID)
    current[0] = started + timedelta(seconds=60)
    with pytest.raises(ReauthenticationRequired):
        await service.verify(pair.token)


@pytest.mark.asyncio
async def test_legacy_ad_session_migration_or_forced_reauth_contract() -> None:
    started = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)
    service = JwtService(SECRET, FakeKeyValue(), clock=lambda: started)
    token = jwt.encode(
        {
            "iss": JWT_ISSUER,
            "aud": JWT_AUDIENCE,
            "sub": "8",
            "identity_id": 18,
            "provider_code": "ad",
            "login_name": "ad.user",
            "display_name": "目录用户",
            "dept": "研发部",
            "role": "operator",
            "security_version": 1,
            "token_type": ACCESS_TOKEN_TYPE,
            "sid": "s" * 32,
            "jti": "j" * 32,
            "iat": started.timestamp(),
            "exp": int((started + timedelta(minutes=15)).timestamp()),
        },
        SECRET,
        algorithm="HS256",
        headers={"kid": "1"},
    )
    claims = await service.verify(token)
    assert claims.auth_time == started.timestamp()
    assert claims.reauth_deadline == started.timestamp() + 480 * 60


@pytest.mark.asyncio
async def test_deadline_boundary_with_clock_skew_and_clock_rollback() -> None:
    started = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)
    current = [started + timedelta(seconds=59)]
    service = JwtService(SECRET, FakeKeyValue(), clock=lambda: current[0])
    pair = await service.issue_pair(ad_claims(now=started, deadline_s=60), TAB_ID)
    assert await service.verify(pair.token)
    current[0] = started + timedelta(seconds=30)
    assert await service.verify(pair.token)
    current[0] = started + timedelta(seconds=60)
    with pytest.raises(ReauthenticationRequired):
        await service.verify(pair.token)
