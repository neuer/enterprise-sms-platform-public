"""AUTH-R4-01 真实 PostgreSQL：权威策略行与对齐加载失败关闭。"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.auth.backends import SessionStateUnavailable
from app.core.auth.jwt import JwtClaims, JwtService
from app.core.auth.session_policy import AuthSessionPolicy, publish_auth_session_policy
from app.core.auth.session_policy_sync import (
    AlignedAuthSessionPolicyLoader,
    policy_from_mapping,
)
from tests.test_auth import TAB_ID, FakeKeyValue
from tests.test_auth_ad_deadline import SECRET


def _postgres_dsn() -> str | None:
    return os.environ.get("SECURITY_SESSION_POSTGRES_DSN") or os.environ.get("OUTBOX_POSTGRES_DSN")


pytestmark = pytest.mark.skipif(
    _postgres_dsn() is None,
    reason="requires isolated migrated PostgreSQL",
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


@pytest.mark.asyncio
async def test_postgres_policy_row_exposes_min_accepted_revision() -> None:
    engine = create_async_engine(make_url(_postgres_dsn()))
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT revision, ad_session_max_age_minutes,
                           EXTRACT(EPOCH FROM updated_at)::bigint AS updated_at_epoch,
                           min_accepted_policy_revision
                    FROM auth_session_policy
                    WHERE id = 1
                    """
                )
            )
            row = result.mappings().first()
        assert row is not None
        policy = policy_from_mapping(row)
        assert policy.revision >= 1
        assert 1 <= policy.min_accepted_policy_revision <= policy.revision
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_ahead_redis_fails_aligned_login() -> None:
    engine = create_async_engine(make_url(_postgres_dsn()))
    store = FakeKeyValue()

    async def postgres() -> AuthSessionPolicy:
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT revision, ad_session_max_age_minutes,
                           EXTRACT(EPOCH FROM updated_at)::bigint AS updated_at_epoch,
                           min_accepted_policy_revision
                    FROM auth_session_policy
                    WHERE id = 1
                    """
                )
            )
            row = result.mappings().first()
        assert row is not None
        return policy_from_mapping(row)

    try:
        authoritative = await postgres()
        await publish_auth_session_policy(
            store,
            AuthSessionPolicy(
                authoritative.revision + 1,
                authoritative.ad_session_max_age_minutes,
                authoritative.updated_at_epoch or 1,
                authoritative.min_accepted_policy_revision,
            ),
        )
        loader = AlignedAuthSessionPolicyLoader(store, postgres_loader=postgres)
        service = JwtService(SECRET, store, session_policy_loader=loader.load)
        with pytest.raises(SessionStateUnavailable, match="ahead"):
            await service.issue_pair(_unbound_ad_claims(), TAB_ID)
        assert not any(key.startswith("auth:jwt:refresh-family:") for key in store.values)
    finally:
        await engine.dispose()
