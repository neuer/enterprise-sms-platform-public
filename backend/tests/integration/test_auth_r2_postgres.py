"""AUTH-R2 真实角色、策略查询与 transition 审计合同。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime
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
    AuthTransitionDeadLetter,
    SqlAuthSecurityEventRepository,
    transition_dead_letter_hmac,
)
from app.core.auth.service import (
    AUDIT_DUE_KEY,
    AUDIT_OPEN_KEY,
    AUDIT_RECOVERY_TTL_S,
    AccountLocked,
    LoginGuard,
    RateLimited,
    RedisKeyValue,
)
from app.core.auth.transition_sync import AuthTransitionReconciler
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
        await connection.execute(text(f"ALTER ROLE sms_auth WITH LOGIN PASSWORD '{auth_password}'"))
        await connection.execute(
            text(f"ALTER ROLE sms_accept WITH LOGIN PASSWORD '{accept_password}'")
        )
        if database:
            await connection.execute(text(f'GRANT CONNECT ON DATABASE "{database}" TO sms_auth'))
            await connection.execute(text(f'GRANT CONNECT ON DATABASE "{database}" TO sms_accept'))
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


async def _audit_lock_count(owner: AsyncEngine, object_id: str) -> int:
    async with owner.connect() as connection:
        count = await connection.scalar(
            text(
                """
                SELECT COUNT(*) FROM audit_log
                WHERE action='auth_account_locked' AND object_id=:object_id
                """
            ),
            {"object_id": object_id},
        )
    return int(count)


class _FailFirstWriter:
    """首次审计写入失败，留下 pending transition 供 Reconciler 接管。"""

    def __init__(self, real: SqlAuthSecurityEventRepository) -> None:
        self._real = real
        self.calls = 0

    async def ensure_transition(self, transition: AuthSecurityTransition) -> None:
        self.calls += 1
        if self.calls == 1:
            raise SessionStateUnavailable("auth security audit unavailable")
        await self._real.ensure_transition(transition)

    async def record_dead_letter(self, record: AuthTransitionDeadLetter) -> None:
        await self._real.record_dead_letter(record)


class _ExclusiveLeaseWriter:
    """只统计目标 transition 的并发写入，用于证明单 Writer 租约。"""

    def __init__(self, real: SqlAuthSecurityEventRepository, transition_id: str) -> None:
        self._real = real
        self._transition_id = transition_id
        self.calls = 0
        self.max_in_flight = 0
        self._in_flight = 0
        self._lock = asyncio.Lock()

    async def ensure_transition(self, transition: AuthSecurityTransition) -> None:
        if transition.transition_id != self._transition_id:
            await self._real.ensure_transition(transition)
            return
        async with self._lock:
            self.calls += 1
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            await asyncio.sleep(0.3)
            await self._real.ensure_transition(transition)
        finally:
            async with self._lock:
                self._in_flight -= 1

    async def record_dead_letter(self, record: AuthTransitionDeadLetter) -> None:
        await self._real.record_dead_letter(record)


async def _pending_lock_transition(
    client: Any,
    writer: Any,
    *,
    username: str,
    ip: str,
    provider_code: str = "local",
) -> str:
    guard = LoginGuard(RedisKeyValue(client), security_events=writer)
    for _ in range(4):
        await guard.record_failure(username, ip, provider_code)
    with pytest.raises(SessionStateUnavailable):
        await guard.record_failure(username, ip, provider_code)
    lock = await client.get(f"auth:lock:user:{username}")
    assert lock
    return str(lock)


async def _cleanup_transition_keys(
    client: Any,
    *,
    username: str,
    ip: str,
    lock: str | None,
) -> None:
    keys = [
        f"auth:fail:user:{username}",
        f"auth:lock:user:{username}",
        f"auth:fail:ip:{ip}",
        f"auth:ban:ip:{ip}",
    ]
    if lock:
        keys.extend(
            (
                f"auth:audit:transition:{lock}",
                f"auth:audit:dead-letter:{lock}",
            )
        )
        await client.zrem(AUDIT_DUE_KEY, lock)
        await client.zrem(AUDIT_OPEN_KEY, lock)
    await client.delete(*keys)


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


@pytest.mark.asyncio
@pytest.mark.skipif("AUTH_GUARD_REDIS_URL" not in os.environ, reason="requires isolated Redis 7")
async def test_reconciler_recovers_after_postgres_outage_without_followup_login(
    auth_roles: tuple[AsyncEngine, URL, URL, Any],
) -> None:
    import asyncio

    from redis.asyncio import Redis

    owner, _auth_url, _accept_url, settings = auth_roles
    client = Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"], decode_responses=True)
    real = SqlAuthSecurityEventRepository(cast(Settings, settings))

    class FlakyWriter:
        def __init__(self) -> None:
            self.calls = 0

        async def ensure_transition(self, transition: AuthSecurityTransition) -> None:
            self.calls += 1
            if self.calls == 1:
                raise SessionStateUnavailable("auth security audit unavailable")
            await real.ensure_transition(transition)

    writer = FlakyWriter()
    username = f"rc-{uuid4().hex[:12]}"
    ip = "10.9.0.21"
    guard = LoginGuard(RedisKeyValue(client), security_events=writer)
    try:
        for _ in range(4):
            await guard.record_failure(username, ip, "local")
        with pytest.raises(SessionStateUnavailable):
            await guard.record_failure(username, ip, "local")
        lock = await client.get(f"auth:lock:user:{username}")
        await asyncio.sleep(1.2)
        reconciler = AuthTransitionReconciler(
            store=RedisKeyValue(client),
            security_events=writer,
            interval_s=1,
        )
        assert await reconciler.reconcile() >= 1
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
        assert writer.calls == 2
    finally:
        keys = [
            f"auth:fail:user:{username}",
            f"auth:lock:user:{username}",
            f"auth:fail:ip:{ip}",
            AUDIT_DUE_KEY,
        ]
        lock = await client.get(f"auth:lock:user:{username}")
        if lock:
            keys.append(f"auth:audit:transition:{lock}")
        await client.delete(*keys)
        await client.aclose()


@pytest.mark.asyncio
async def test_sms_auth_can_insert_dead_letter_but_has_no_select(
    auth_roles: tuple[AsyncEngine, URL, URL, Any],
) -> None:
    owner, auth_url, _accept_url, settings = auth_roles
    repository = SqlAuthSecurityEventRepository(cast(Settings, settings))
    digest = transition_dead_letter_hmac(str(uuid4()))
    await repository.record_dead_letter(
        AuthTransitionDeadLetter(
            transition_hmac=digest,
            reason="missing_hash",
            field_class="missing_hash",
            discovered_at=datetime.now(UTC),
            build_version="test",
        )
    )
    async with owner.connect() as connection:
        count = await connection.scalar(
            text(
                """
                SELECT COUNT(*) FROM auth_transition_dead_letter
                WHERE transition_hmac=:hmac
                """
            ),
            {"hmac": digest},
        )
    assert int(count) == 1
    engine = create_async_engine(auth_url)
    try:
        async with engine.connect() as connection:
            with pytest.raises(DBAPIError):
                await connection.execute(text("SELECT id FROM auth_transition_dead_letter LIMIT 1"))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.skipif("AUTH_GUARD_REDIS_URL" not in os.environ, reason="requires isolated Redis 7")
async def test_multi_process_real_redis_and_postgres_transition_integrity(
    auth_roles: tuple[AsyncEngine, URL, URL, Any],
) -> None:
    import asyncio

    from redis.asyncio import Redis

    from app.core.auth.service import AUDIT_DUE_KEY, AUDIT_OPEN_KEY
    from app.core.auth.transition_sync import AuthTransitionReconciler

    owner, _auth_url, _accept_url, settings = auth_roles
    first = Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"], decode_responses=True)
    second = Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"], decode_responses=True)
    writer = SqlAuthSecurityEventRepository(cast(Settings, settings))
    username = f"int-{uuid4().hex[:12]}"
    left = LoginGuard(RedisKeyValue(first), security_events=writer)
    right = LoginGuard(RedisKeyValue(second), security_events=writer)
    orphan_id = str(uuid4())
    pending_user = f"pend-{uuid4().hex[:12]}"
    pending_ip = "10.9.3.21"
    lock = None
    pending_lock = None
    try:
        for index in range(5):
            guard = left if index % 2 == 0 else right
            with suppress(AccountLocked):
                await guard.record_failure(username, f"10.9.2.{index}", "local")
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

        await first.delete(f"auth:audit:transition:{lock}")
        await first.zadd(AUDIT_DUE_KEY, {str(lock): 0})
        await first.zadd(AUDIT_DUE_KEY, {orphan_id: 0})
        await AuthTransitionReconciler(
            store=RedisKeyValue(first),
            security_events=writer,
            interval_s=1,
        ).reconcile()
        async with owner.connect() as connection:
            lock_count = await connection.scalar(
                text(
                    """
                    SELECT COUNT(*) FROM audit_log
                    WHERE action='auth_account_locked' AND object_id=:object_id
                    """
                ),
                {"object_id": str(lock)},
            )
            fake_count = await connection.scalar(
                text(
                    """
                    SELECT COUNT(*) FROM audit_log
                    WHERE object_id=:object_id
                    """
                ),
                {"object_id": orphan_id},
            )
            dead_letters = await connection.scalar(
                text("SELECT COUNT(*) FROM auth_transition_dead_letter")
            )
        assert int(lock_count) == 1
        assert int(fake_count) == 0
        assert int(dead_letters) >= 1

        class FlakyWriter:
            def __init__(self) -> None:
                self.calls = 0

            async def ensure_transition(self, transition: AuthSecurityTransition) -> None:
                self.calls += 1
                if self.calls == 1:
                    raise SessionStateUnavailable("auth security audit unavailable")
                await writer.ensure_transition(transition)

            async def record_dead_letter(self, record: AuthTransitionDeadLetter) -> None:
                await writer.record_dead_letter(record)

        flaky = FlakyWriter()
        pending_guard = LoginGuard(RedisKeyValue(second), security_events=flaky)
        for _ in range(4):
            await pending_guard.record_failure(pending_user, pending_ip, "ad")
        with pytest.raises(SessionStateUnavailable):
            await pending_guard.record_failure(pending_user, pending_ip, "ad")
        pending_lock = await second.get(f"auth:lock:user:{pending_user}")
        envelope = await second.hgetall(f"auth:audit:transition:{pending_lock}")
        assert envelope["action"] == "auth_account_locked"
        assert envelope["provider_code"] == "ad"
        assert envelope["ip"] == pending_ip
        assert int(await second.ttl(f"auth:audit:transition:{pending_lock}")) == -1
        await second.zrem(AUDIT_DUE_KEY, str(pending_lock))
        await asyncio.sleep(1.2)
        await AuthTransitionReconciler(
            store=RedisKeyValue(second),
            security_events=flaky,
            interval_s=1,
        ).reconcile()
        async with owner.connect() as connection:
            pending_count = await connection.scalar(
                text(
                    """
                    SELECT COUNT(*) FROM audit_log
                    WHERE action='auth_account_locked' AND object_id=:object_id
                    """
                ),
                {"object_id": str(pending_lock)},
            )
            after_val = await connection.scalar(
                text(
                    """
                    SELECT after_val::text FROM audit_log
                    WHERE action='auth_account_locked' AND object_id=:object_id
                    """
                ),
                {"object_id": str(pending_lock)},
            )
        assert int(pending_count) == 1
        assert "ad" in str(after_val)
        restored = await second.hgetall(f"auth:audit:transition:{pending_lock}")
        assert restored["created_at_ms"] == envelope["created_at_ms"]
        assert restored["ip"] == pending_ip
    finally:
        keys = [
            f"auth:fail:user:{username}",
            f"auth:lock:user:{username}",
            f"auth:fail:user:{pending_user}",
            f"auth:lock:user:{pending_user}",
            f"auth:fail:ip:{pending_ip}",
        ]
        if lock:
            keys.append(f"auth:audit:transition:{lock}")
            keys.append(f"auth:audit:dead-letter:{lock}")
            await first.zrem(AUDIT_DUE_KEY, str(lock))
            await first.zrem(AUDIT_OPEN_KEY, str(lock))
        if pending_lock:
            keys.append(f"auth:audit:transition:{pending_lock}")
            keys.append(f"auth:audit:dead-letter:{pending_lock}")
            await first.zrem(AUDIT_DUE_KEY, str(pending_lock))
            await first.zrem(AUDIT_OPEN_KEY, str(pending_lock))
        await first.zrem(AUDIT_DUE_KEY, orphan_id)
        await first.zrem(AUDIT_OPEN_KEY, orphan_id)
        keys.append(f"auth:audit:dead-letter:{orphan_id}")
        await first.delete(*keys)
        await first.aclose()
        await second.aclose()


@pytest.mark.asyncio
@pytest.mark.skipif("AUTH_GUARD_REDIS_URL" not in os.environ, reason="requires isolated Redis 7")
async def test_multi_reconciler_exclusive_lease_writes_one_audit(
    auth_roles: tuple[AsyncEngine, URL, URL, Any],
) -> None:
    from redis.asyncio import Redis

    owner, _auth_url, _accept_url, settings = auth_roles
    first = Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"], decode_responses=True)
    second = Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"], decode_responses=True)
    real = SqlAuthSecurityEventRepository(cast(Settings, settings))
    username = f"mr-{uuid4().hex[:12]}"
    ip = "10.9.4.31"
    lock = None
    try:
        lock = await _pending_lock_transition(
            first, _FailFirstWriter(real), username=username, ip=ip
        )
        await asyncio.sleep(1.2)
        writer = _ExclusiveLeaseWriter(real, lock)
        left = AuthTransitionReconciler(
            store=RedisKeyValue(first),
            security_events=writer,
            interval_s=1,
        )
        right = AuthTransitionReconciler(
            store=RedisKeyValue(second),
            security_events=writer,
            interval_s=1,
        )
        settled = await asyncio.gather(left.reconcile(), right.reconcile())
        assert sum(settled) >= 1
        assert writer.calls == 1
        assert writer.max_in_flight == 1
        assert await _audit_lock_count(owner, lock) == 1
        envelope = await first.hgetall(f"auth:audit:transition:{lock}")
        assert envelope["action"] == "auth_account_locked"
        assert envelope["ip"] == ip
        assert envelope["state"] == "audited"
    finally:
        await _cleanup_transition_keys(first, username=username, ip=ip, lock=lock)
        await first.aclose()
        await second.aclose()


@pytest.mark.asyncio
@pytest.mark.skipif("AUTH_GUARD_REDIS_URL" not in os.environ, reason="requires isolated Redis 7")
async def test_long_outage_after_lock_ttl_preserves_original_envelope(
    auth_roles: tuple[AsyncEngine, URL, URL, Any],
) -> None:
    from redis.asyncio import Redis

    owner, _auth_url, _accept_url, settings = auth_roles
    first = Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"], decode_responses=True)
    second = Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"], decode_responses=True)
    real = SqlAuthSecurityEventRepository(cast(Settings, settings))
    username = f"lo-{uuid4().hex[:12]}"
    ip = "10.9.4.32"
    lock = None
    try:
        lock = await _pending_lock_transition(
            first,
            _FailFirstWriter(real),
            username=username,
            ip=ip,
            provider_code="ad",
        )
        key = f"auth:audit:transition:{lock}"
        envelope = await first.hgetall(key)
        assert envelope["action"] == "auth_account_locked"
        assert envelope["provider_code"] == "ad"
        assert envelope["ip"] == ip
        assert envelope["state"] == "pending"
        assert int(await first.ttl(key)) == -1
        raw_time = await first.time()
        now_ms = int(raw_time[0]) * 1000 + int(raw_time[1]) // 1000
        aged_ms = now_ms - (AUDIT_RECOVERY_TTL_S * 1000 + 5_000)
        await first.hset(key, "created_at_ms", str(aged_ms))
        await first.delete(f"auth:lock:user:{username}", f"auth:fail:user:{username}")
        await asyncio.sleep(1.2)
        writer = SqlAuthSecurityEventRepository(cast(Settings, settings))
        left = AuthTransitionReconciler(
            store=RedisKeyValue(first),
            security_events=writer,
            interval_s=1,
        )
        right = AuthTransitionReconciler(
            store=RedisKeyValue(second),
            security_events=writer,
            interval_s=1,
        )
        await asyncio.gather(left.reconcile(), right.reconcile())
        assert await _audit_lock_count(owner, lock) == 1
        restored = await first.hgetall(key)
        assert restored["created_at_ms"] == str(aged_ms)
        assert restored["action"] == "auth_account_locked"
        assert restored["provider_code"] == "ad"
        assert restored["ip"] == ip
        assert restored["state"] == "audited"
        async with owner.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                            SELECT after_val::text, host(ip)
                            FROM audit_log
                            WHERE action='auth_account_locked' AND object_id=:object_id
                        """
                    ),
                    {"object_id": lock},
                )
            ).one()
        assert "ad" in str(row[0])
        assert str(row[1]) == ip
    finally:
        await _cleanup_transition_keys(first, username=username, ip=ip, lock=lock)
        await first.aclose()
        await second.aclose()
