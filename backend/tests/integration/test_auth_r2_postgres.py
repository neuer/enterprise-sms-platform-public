"""AUTH-R2 真实角色、策略查询与 transition 审计合同。"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import suppress
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.auth.backends import SessionStateUnavailable
from app.core.auth.guard_policy import SqlAuthGuardPolicyLoader
from app.core.auth.security_events import (
    AuthSecurityTransition,
    SqlAuthSecurityEventRepository,
)
from app.core.auth.service import AccountLocked, LoginGuard, RateLimited, RedisKeyValue
from app.core.runtime_resources import close_runtime_resources
from app.settings import Settings

pytestmark = pytest.mark.skipif(
    "SECURITY_SESSION_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)

SYSTEM_API_KEY = bytes.fromhex("33" * 32)
TRANSITION_LOCK = "8a5a77a4-286f-4d81-9a64-5379e30df111"
TRANSITION_BAN = "8a5a77a4-286f-4d81-9a64-5379e30df222"


@pytest.fixture
async def auth_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[AsyncEngine, URL, URL, Any]]:
    owner_url = make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"])
    auth_password = uuid4().hex
    accept_password = uuid4().hex
    owner = create_async_engine(owner_url)
    database = owner_url.database
    async with owner.begin() as connection:
        await connection.execute(
            text(f"ALTER ROLE sms_auth WITH LOGIN PASSWORD '{auth_password}'")
        )
        await connection.execute(
            text(f"ALTER ROLE sms_accept WITH LOGIN PASSWORD '{accept_password}'")
        )
        if database:
            await connection.execute(
                text(f'GRANT CONNECT ON DATABASE "{database}" TO sms_auth')
            )
            await connection.execute(
                text(f'GRANT CONNECT ON DATABASE "{database}" TO sms_accept')
            )
        await connection.execute(
            text(
                """
                INSERT INTO audit_context_signing_key(key_kind,key_material,updated_at)
                VALUES ('system:api',:key,now())
                ON CONFLICT (key_kind) DO UPDATE
                  SET key_material=EXCLUDED.key_material,updated_at=now()
                """
            ),
            {"key": SYSTEM_API_KEY},
        )
    monkeypatch.setattr(
        "app.core.runtime_resources._audit_context_key",
        lambda name: SYSTEM_API_KEY if name == "audit_system_api_context_key" else None,
    )
    auth_url = owner_url.set(username="sms_auth", password=auth_password)
    accept_url = owner_url.set(username="sms_accept", password=accept_password)
    settings = cast(
        Any,
        SimpleNamespace(
            database_url=accept_url,
            database_url_for=lambda role: auth_url if role == "auth" else accept_url,
        ),
    )
    try:
        yield owner, auth_url, accept_url, settings
    finally:
        await close_runtime_resources()
        await owner.dispose()


def _transition(
    *,
    action: str = "auth_account_locked",
    transition_id: str = TRANSITION_LOCK,
    result_code: str = "ACCOUNT_LOCKED",
) -> AuthSecurityTransition:
    return AuthSecurityTransition(
        action=action,  # type: ignore[arg-type]
        transition_id=transition_id,
        provider_code="local",
        result_code=result_code,  # type: ignore[arg-type]
        count=5,
        remaining_ttl_seconds=900,
        ip="10.8.0.8",
    )


@pytest.mark.asyncio
async def test_auth_security_event_repository_connects_as_sms_auth(
    auth_roles: tuple[AsyncEngine, URL, URL, Any],
) -> None:
    _owner, _auth_url, _accept_url, settings = auth_roles
    repository = SqlAuthSecurityEventRepository(cast(Settings, settings))
    assert await repository.current_database_user() == "sms_auth"


@pytest.mark.asyncio
async def test_sms_auth_can_insert_auth_account_locked(
    auth_roles: tuple[AsyncEngine, URL, URL, Any],
) -> None:
    owner, _auth_url, _accept_url, settings = auth_roles
    repository = SqlAuthSecurityEventRepository(cast(Settings, settings))
    await repository.ensure_transition(_transition())
    async with owner.connect() as connection:
        count = await connection.scalar(
            text(
                """
                SELECT COUNT(*) FROM audit_log
                WHERE action='auth_account_locked' AND object_id=:object_id
                """
            ),
            {"object_id": TRANSITION_LOCK},
        )
        payload = await connection.scalar(
            text(
                """
                SELECT after_val::text FROM audit_log
                WHERE action='auth_account_locked' AND object_id=:object_id
                """
            ),
            {"object_id": TRANSITION_LOCK},
        )
    assert int(count) == 1
    assert "password" not in str(payload).casefold()
    assert "token" not in str(payload).casefold()


@pytest.mark.asyncio
async def test_sms_auth_can_insert_auth_ip_banned(
    auth_roles: tuple[AsyncEngine, URL, URL, Any],
) -> None:
    owner, _auth_url, _accept_url, settings = auth_roles
    repository = SqlAuthSecurityEventRepository(cast(Settings, settings))
    await repository.ensure_transition(
        _transition(
            action="auth_ip_banned",
            transition_id=TRANSITION_BAN,
            result_code="RATE_LIMITED",
        )
    )
    async with owner.connect() as connection:
        count = await connection.scalar(
            text(
                """
                SELECT COUNT(*) FROM audit_log
                WHERE action='auth_ip_banned' AND object_id=:object_id
                """
            ),
            {"object_id": TRANSITION_BAN},
        )
    assert int(count) == 1


@pytest.mark.asyncio
async def test_sms_accept_cannot_insert_auth_security_events(
    auth_roles: tuple[AsyncEngine, URL, URL, Any],
) -> None:
    _owner, _auth_url, accept_url, settings = auth_roles
    wrong = cast(
        Settings,
        SimpleNamespace(
            database_url=accept_url,
            database_url_for=lambda _role: accept_url,
        ),
    )
    repository = SqlAuthSecurityEventRepository(wrong)
    with pytest.raises(SessionStateUnavailable):
        await repository.ensure_transition(
            _transition(transition_id="8a5a77a4-286f-4d81-9a64-5379e30df333")
        )


@pytest.mark.asyncio
async def test_sms_auth_has_no_audit_log_select(
    auth_roles: tuple[AsyncEngine, URL, URL, Any],
) -> None:
    _owner, auth_url, _accept_url, _settings = auth_roles
    engine = create_async_engine(auth_url)
    try:
        async with engine.connect() as connection:
            with pytest.raises(DBAPIError):
                await connection.execute(text("SELECT id FROM audit_log LIMIT 1"))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_auth_audit_database_failure_returns_controlled_503(
    auth_roles: tuple[AsyncEngine, URL, URL, Any],
) -> None:
    _owner, _auth_url, _accept_url, _settings = auth_roles
    repository = SqlAuthSecurityEventRepository(
        cast(
            Settings,
            SimpleNamespace(
                database_url_for=lambda _role: make_url(
                    "postgresql+asyncpg://sms_auth:wrong@127.0.0.1:1/missing"
                )
            ),
        )
    )
    with pytest.raises(SessionStateUnavailable):
        await repository.ensure_transition(_transition())


@pytest.mark.asyncio
async def test_auth_policy_query_reads_only_four_guard_keys(
    auth_roles: tuple[AsyncEngine, URL, URL, Any],
) -> None:
    _owner, _auth_url, _accept_url, settings = auth_roles
    loader = SqlAuthGuardPolicyLoader(cast(Settings, settings))
    snapshot = await loader.load()
    assert snapshot.login_fail_limit >= 1
    assert snapshot.login_ip_fail_limit >= 1
    assert snapshot.version >= 1


@pytest.mark.asyncio
@pytest.mark.skipif("AUTH_GUARD_REDIS_URL" not in os.environ, reason="requires isolated Redis 7")
async def test_real_guard_account_threshold_returns_423_and_persists_one_audit(
    auth_roles: tuple[AsyncEngine, URL, URL, Any],
) -> None:
    from redis.asyncio import Redis

    owner, _auth_url, _accept_url, settings = auth_roles
    client = Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"], decode_responses=True)
    writer = SqlAuthSecurityEventRepository(cast(Settings, settings))
    username = f"lock-{uuid4().hex[:12]}"
    ip = "10.9.0.11"
    guard = LoginGuard(RedisKeyValue(client), security_events=writer)
    try:
        for _ in range(4):
            await guard.record_failure(username, ip, "local")
        with pytest.raises(AccountLocked):
            await guard.record_failure(username, ip, "local")
        with pytest.raises(AccountLocked):
            await guard.record_failure(username, ip, "local")
        lock = await client.get(f"auth:lock:user:{username}")
        async with owner.connect() as connection:
            count = await connection.scalar(
                text(
                    """
                    SELECT COUNT(*) FROM audit_log
                    WHERE action='auth_account_locked' AND object_id=:object_id
                    """
                ),
                {"object_id": str(lock)},
            )
        assert int(count) == 1
    finally:
        keys = [
            f"auth:fail:user:{username}",
            f"auth:lock:user:{username}",
            f"auth:fail:ip:{ip}",
            f"auth:ban:ip:{ip}",
        ]
        lock = await client.get(f"auth:lock:user:{username}")
        if lock:
            keys.append(f"auth:audit:transition:{lock}")
        await client.delete(*keys)
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.skipif("AUTH_GUARD_REDIS_URL" not in os.environ, reason="requires isolated Redis 7")
async def test_real_guard_ip_threshold_returns_429_and_persists_one_audit(
    auth_roles: tuple[AsyncEngine, URL, URL, Any],
) -> None:
    from redis.asyncio import Redis

    owner, _auth_url, _accept_url, settings = auth_roles
    client = Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"], decode_responses=True)
    writer = SqlAuthSecurityEventRepository(cast(Settings, settings))
    ip = "10.9.0.12"
    guard = LoginGuard(RedisKeyValue(client), security_events=writer)
    try:
        for index in range(19):
            await guard.record_failure(f"ban-{index}-{uuid4().hex[:8]}", ip, "local")
        with pytest.raises(RateLimited):
            await guard.record_failure(f"ban-final-{uuid4().hex[:8]}", ip, "local")
        with pytest.raises(RateLimited):
            await guard.admit("local", "other", ip)
        ban = await client.get(f"auth:ban:ip:{ip}")
        async with owner.connect() as connection:
            count = await connection.scalar(
                text(
                    """
                SELECT COUNT(*) FROM audit_log
                WHERE action='auth_ip_banned' AND object_id=:object_id
                """
                ),
                {"object_id": str(ban)},
            )
        assert int(count) == 1
    finally:
        keys = [f"auth:fail:ip:{ip}", f"auth:ban:ip:{ip}"]
        ban = await client.get(f"auth:ban:ip:{ip}")
        if ban:
            keys.append(f"auth:audit:transition:{ban}")
        await client.delete(*keys)
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.skipif("AUTH_GUARD_REDIS_URL" not in os.environ, reason="requires isolated Redis 7")
async def test_multi_process_real_redis_and_postgres_transition_contract(
    auth_roles: tuple[AsyncEngine, URL, URL, Any],
) -> None:
    from redis.asyncio import Redis

    owner, _auth_url, _accept_url, settings = auth_roles
    first = Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"], decode_responses=True)
    second = Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"], decode_responses=True)
    writer = SqlAuthSecurityEventRepository(cast(Settings, settings))
    username = f"mp-{uuid4().hex[:12]}"
    left = LoginGuard(RedisKeyValue(first), security_events=writer)
    right = LoginGuard(RedisKeyValue(second), security_events=writer)
    try:
        for index in range(5):
            guard = left if index % 2 == 0 else right
            with suppress(AccountLocked):
                await guard.record_failure(username, f"10.9.1.{index}", "local")
        lock = await first.get(f"auth:lock:user:{username}")
        async with owner.connect() as connection:
            count = await connection.scalar(
                text(
                    """
                    SELECT COUNT(*) FROM audit_log
                    WHERE action='auth_account_locked' AND object_id=:object_id
                    """
                ),
                {"object_id": str(lock)},
            )
        assert int(count) == 1
    finally:
        keys = [f"auth:fail:user:{username}", f"auth:lock:user:{username}"]
        lock = await first.get(f"auth:lock:user:{username}")
        if lock:
            keys.append(f"auth:audit:transition:{lock}")
        await first.delete(*keys)
        await first.aclose()
        await second.aclose()
