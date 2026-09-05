"""AUTH-R4-01 真实 Redis：签发消费同一策略快照，缺失失败关闭。"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import jwt
import pytest
from redis.asyncio import Redis

from app.core.auth.backends import SessionStateUnavailable
from app.core.auth.jwt import JWT_AUDIENCE, JwtClaims, JwtService
from app.core.auth.service import RedisKeyValue
from app.core.auth.session_policy import (
    AD_SESSION_POLICY_KEY,
    MIN_ACCEPTED_POLICY_KEY,
    AuthSessionPolicy,
    load_auth_session_policy,
    publish_auth_session_policy,
)
from tests.integration.test_auth_guard_redis import PrefixedRedisKeyValue
from tests.test_auth import TAB_ID
from tests.test_auth_ad_deadline import SECRET

pytestmark = pytest.mark.skipif(
    "AUTH_GUARD_REDIS_URL" not in os.environ,
    reason="requires isolated Redis 7",
)


def _unbound_ad_claims() -> JwtClaims:
    return JwtClaims(
        account_id=8,
        identity_id=18,
        provider_code="ad",
        login_name="ad.user",
        display_name="目录用户",
        dept="研发部",
        role="operator",
        security_version=1,
    )


def _decode(token: str) -> dict[str, Any]:
    return jwt.decode(
        token,
        SECRET,
        algorithms=["HS256"],
        audience=JWT_AUDIENCE,
        options={"verify_exp": False, "verify_iat": False},
    )


@pytest.mark.asyncio
async def test_real_redis_issue_binds_shared_policy_revision() -> None:
    url = os.environ["AUTH_GUARD_REDIS_URL"]
    client = Redis.from_url(url, decode_responses=True)
    prefix = f"auth-session-policy:{uuid4().hex}"
    store = PrefixedRedisKeyValue(RedisKeyValue(client), prefix)
    started = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)
    first = JwtService(SECRET, store, clock=lambda: started)
    second = JwtService(SECRET, store, clock=lambda: started)
    try:
        await client.ping()
        await publish_auth_session_policy(store, AuthSessionPolicy(8, 15, 100, 8))
        policy = await load_auth_session_policy(store)
        assert policy.revision == 8
        assert policy.min_accepted_policy_revision == 8
        left = await first.issue_pair(_unbound_ad_claims(), TAB_ID)
        right = await second.issue_pair(_unbound_ad_claims(), TAB_ID)
        assert _decode(left.token)["auth_policy_version"] == 8
        assert _decode(right.token)["auth_policy_version"] == 8
        assert _decode(left.token)["reauth_deadline"] == started.timestamp() + 15 * 60
        assert await first.verify(left.token)
        raw = await client.hgetall(store._key(AD_SESSION_POLICY_KEY))
        assert raw["revision"] == "8"
        assert await client.get(store._key(MIN_ACCEPTED_POLICY_KEY)) == "8"
    finally:
        if store.keys:
            await client.delete(*store.keys)
        await client.aclose()


@pytest.mark.asyncio
async def test_real_redis_missing_policy_fails_closed_without_family() -> None:
    url = os.environ["AUTH_GUARD_REDIS_URL"]
    client = Redis.from_url(url, decode_responses=True)
    prefix = f"auth-session-policy:{uuid4().hex}"
    store = PrefixedRedisKeyValue(RedisKeyValue(client), prefix)
    service = JwtService(SECRET, store)
    try:
        await client.ping()
        with pytest.raises(SessionStateUnavailable):
            await service.issue_pair(_unbound_ad_claims(), TAB_ID)
        assert await client.exists(store._key(AD_SESSION_POLICY_KEY)) == 0
        assert not any("refresh-family" in key for key in store.keys)
    finally:
        if store.keys:
            await client.delete(*store.keys)
        await client.aclose()
