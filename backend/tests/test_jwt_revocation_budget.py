from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.core.auth.backends import (
    AuthenticatedIdentity,
    InvalidCredentials,
    SessionStateUnavailable,
)
from app.core.auth.jwt import (
    MAX_VALID_ACCESS_LIFETIME_MS,
    REFRESH_GRACE_SECONDS,
    JwtService,
    eval_memory_jwt_script,
    max_valid_access_lifetime_ms,
)
from app.core.auth.runtime import AuthFacade, LoginSuccess
from app.core.errors import ApiError
from tests.test_auth import TAB_ID, access_claims
from tests.test_auth_runtime import (
    IP,
    FakeAuthService,
    FakeHasher,
    FakeUserRepository,
    account,
)

SECRET = "a-jwt-secret-that-is-long-enough-for-hs256-tests"


class TtlLuaStore:
    """忠实实现 TTL 过期清理与撤销/轮换 Lua 原子语义的测试替身。"""

    def __init__(self, clock: Any) -> None:
        self.clock = clock
        self.values: dict[str, Any] = {}
        self.expire_at_ms: dict[str, int] = {}
        self.key_types: dict[str, str] = {}
        self.lock = asyncio.Lock()
        self.fail_eval = False

    def now_ms(self) -> int:
        return int(self.clock().timestamp() * 1000)

    def purge(self) -> None:
        now = self.now_ms()
        for key in [name for name, deadline in self.expire_at_ms.items() if deadline <= now]:
            self.values.pop(key, None)
            self.expire_at_ms.pop(key, None)
            self.key_types.pop(key, None)

    def pttl(self, key: str) -> int:
        self.purge()
        if key not in self.values:
            return -2
        if key not in self.expire_at_ms:
            return -1
        return self.expire_at_ms[key] - self.now_ms()

    def persist(self, key: str, value: str = "1") -> None:
        self.values[key] = value
        self.expire_at_ms.pop(key, None)
        self.key_types[key] = "string"

    async def get(self, key: str) -> Any:
        self.purge()
        return self.values.get(key)

    async def set(self, key: str, value: Any, *, ex: int) -> None:
        self.purge()
        self.values[key] = value
        self.expire_at_ms[key] = self.now_ms() + max(1, int(ex)) * 1000
        if isinstance(value, (str, bytes)):
            self.key_types[key] = "string"
        else:
            self.key_types.pop(key, None)

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)
        self.expire_at_ms.pop(key, None)
        self.key_types.pop(key, None)

    async def increment(self, key: str, *, window_s: int) -> int:
        del window_s
        self.purge()
        value = int(self.values.get(key, 0)) + 1
        self.values[key] = value
        return value

    async def eval(self, script: str, numkeys: int, *args: object) -> object:
        del numkeys
        if self.fail_eval:
            raise RuntimeError("revocation store unavailable")
        from app.core.auth.session_policy import eval_memory_session_policy

        async with self.lock:
            self.purge()
            jwt_result = eval_memory_jwt_script(
                self.values,
                script,
                args,
                expire_at_ms=self.expire_at_ms,
                key_types=self.key_types,
                now_ms=self.now_ms(),
            )
            if jwt_result is not None:
                return jwt_result
            policy_result = eval_memory_session_policy(self.values, script, args)
            if policy_result is not None or "auth-session-policy-" in script:
                return policy_result
            raise AssertionError("unexpected Lua script")


def _service(store: TtlLuaStore, moments: list[datetime]) -> JwtService:
    return JwtService(
        SECRET,
        store,
        clock=lambda: moments[0],
        ttl=timedelta(minutes=15),
        refresh_ttl=timedelta(minutes=1),
    )


def _sid(tokens: JwtService, token: str) -> str:
    return str(tokens._decode(token, allow_expired=True)["sid"])


def _jti(tokens: JwtService, token: str) -> str:
    return str(tokens._decode(token, allow_expired=True)["jti"])


def _session_key(sid: str) -> str:
    return f"auth:jwt:session-revoked:{sid}"


def _family_key(sid: str) -> str:
    return f"auth:jwt:refresh-family:{sid}"


def _jti_key(jti: str) -> str:
    return f"auth:jwt:revoked:{jti}"


async def _same_session_pair(
    tokens: JwtService,
) -> tuple[Any, Any]:
    first = await tokens.issue_pair(access_claims(), TAB_ID)
    second = await tokens.rotate_refresh(first.refresh_token, TAB_ID)
    assert _sid(tokens, first.token) == _sid(tokens, second.token)
    assert _jti(tokens, first.token) != _jti(tokens, second.token)
    return first, second


@pytest.mark.asyncio
async def test_logout_near_refresh_expiry_keeps_other_access_revoked() -> None:
    started = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    moments = [started]
    store = TtlLuaStore(lambda: moments[0])
    tokens = _service(store, moments)
    value = account(must_change_password=False)
    identity = AuthenticatedIdentity(
        provider_code="local",
        login_name="admin",
        external_subject="local:admin",
        display_name="管理员",
        dept="平台部",
        groups=(),
        account=value,
    )
    users = FakeUserRepository(value)
    facade = AuthFacade(FakeAuthService(identity), users, tokens, FakeHasher())
    login = await facade.login("local", "admin", "Valid@Password123", IP, TAB_ID)
    assert isinstance(login, LoginSuccess)
    first_access = login.token
    rotated = await tokens.rotate_refresh(login.refresh_token, TAB_ID)
    assert _sid(tokens, first_access) == _sid(tokens, rotated.token)
    assert _jti(tokens, first_access) != _jti(tokens, rotated.token)

    moments[0] = started + timedelta(seconds=30)
    await facade.logout(rotated.token, IP, rotated.refresh_token)
    sid = _sid(tokens, first_access)
    assert store.pttl(_session_key(sid)) >= MAX_VALID_ACCESS_LIFETIME_MS
    assert _family_key(sid) not in store.values

    moments[0] = started + timedelta(seconds=61)
    with pytest.raises(InvalidCredentials):
        await tokens.verify(first_access)
    with pytest.raises(InvalidCredentials):
        await tokens.verify(rotated.token)
    with pytest.raises(InvalidCredentials):
        await tokens.rotate_refresh(rotated.refresh_token, TAB_ID)
    assert users.logout_audits


@pytest.mark.asyncio
async def test_refresh_revoke_does_not_shorten_session_revocation() -> None:
    started = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    moments = [started]
    store = TtlLuaStore(lambda: moments[0])
    tokens = _service(store, moments)
    first, second = await _same_session_pair(tokens)
    moments[0] = started + timedelta(seconds=30)
    await tokens.revoke_token(second.token)
    sid = _sid(tokens, first.token)
    before = store.pttl(_session_key(sid))
    assert before >= MAX_VALID_ACCESS_LIFETIME_MS
    await tokens.revoke_refresh_token(second.refresh_token)
    after = store.pttl(_session_key(sid))
    assert after >= before
    moments[0] = started + timedelta(seconds=61)
    with pytest.raises(InvalidCredentials):
        await tokens.verify(first.token)


@pytest.mark.asyncio
async def test_revocation_order_and_duplicates_preserve_max_deadline() -> None:
    started = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    moments = [started]
    store = TtlLuaStore(lambda: moments[0])
    tokens = _service(store, moments)

    access_first, access_second = await _same_session_pair(tokens)
    moments[0] = started + timedelta(seconds=30)
    await tokens.revoke_token(access_second.token)
    sid = _sid(tokens, access_first.token)
    peak = store.pttl(_session_key(sid))
    await tokens.revoke_refresh_token(access_second.refresh_token)
    await tokens.revoke_token(access_second.token)
    assert store.pttl(_session_key(sid)) >= peak

    moments[0] = started
    refresh_first, refresh_second = await _same_session_pair(tokens)
    moments[0] = started + timedelta(seconds=30)
    await tokens.revoke_refresh_token(refresh_second.refresh_token)
    sid = _sid(tokens, refresh_first.token)
    peak = store.pttl(_session_key(sid))
    await tokens.revoke_token(refresh_second.token)
    await tokens.revoke_refresh_token(refresh_second.refresh_token)
    assert store.pttl(_session_key(sid)) >= peak
    moments[0] = started + timedelta(seconds=61)
    with pytest.raises(InvalidCredentials):
        await tokens.verify(refresh_first.token)
    with pytest.raises(InvalidCredentials):
        await tokens.verify(access_first.token)


@pytest.mark.asyncio
async def test_concurrent_revocations_are_monotonic() -> None:
    started = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    moments = [started]
    store = TtlLuaStore(lambda: moments[0])
    tokens = _service(store, moments)
    first, second = await _same_session_pair(tokens)
    moments[0] = started + timedelta(seconds=30)
    await asyncio.gather(
        tokens.revoke_token(first.token),
        tokens.revoke_token(second.token),
        tokens.revoke_refresh_token(second.refresh_token),
    )
    sid = _sid(tokens, first.token)
    assert store.pttl(_session_key(sid)) >= MAX_VALID_ACCESS_LIFETIME_MS
    moments[0] = started + timedelta(seconds=61)
    with pytest.raises(InvalidCredentials):
        await tokens.verify(first.token)


@pytest.mark.asyncio
async def test_revocation_preserves_existing_permanent_marker() -> None:
    started = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    moments = [started]
    store = TtlLuaStore(lambda: moments[0])
    tokens = _service(store, moments)
    first, _second = await _same_session_pair(tokens)
    sid = _sid(tokens, first.token)
    store.persist(_session_key(sid), "1")
    await tokens.revoke_token(first.token)
    assert store.values[_session_key(sid)] == "1"
    assert store.pttl(_session_key(sid)) == -1


@pytest.mark.asyncio
async def test_revocation_replaces_grace_with_full_session_budget() -> None:
    started = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    moments = [started]
    store = TtlLuaStore(lambda: moments[0])
    tokens = _service(store, moments)
    first, second = await _same_session_pair(tokens)
    sid = _sid(tokens, first.token)
    assert str(store.values[_session_key(sid)]).startswith("grace\n")
    assert store.pttl(_session_key(sid)) <= REFRESH_GRACE_SECONDS * 1000
    await tokens.revoke_token(second.token)
    assert store.values[_session_key(sid)] == "1"
    assert store.pttl(_session_key(sid)) >= MAX_VALID_ACCESS_LIFETIME_MS


@pytest.mark.asyncio
async def test_revocation_handles_missing_or_corrupt_state_safely() -> None:
    started = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    moments = [started]
    store = TtlLuaStore(lambda: moments[0])
    tokens = _service(store, moments)
    missing, _ = await _same_session_pair(tokens)
    sid = _sid(tokens, missing.token)
    await store.delete(_session_key(sid))
    await tokens.revoke_token(missing.token)
    assert store.values[_session_key(sid)] == "1"
    assert store.pttl(_session_key(sid)) >= MAX_VALID_ACCESS_LIFETIME_MS

    corrupt, _ = await _same_session_pair(tokens)
    sid = _sid(tokens, corrupt.token)
    store.values[_session_key(sid)] = "not-a-revocation"
    store.expire_at_ms[_session_key(sid)] = store.now_ms() + 60_000
    store.key_types[_session_key(sid)] = "string"
    with pytest.raises(SessionStateUnavailable):
        await tokens.revoke_token(corrupt.token)
    assert store.values[_session_key(sid)] == "not-a-revocation"

    typed, _ = await _same_session_pair(tokens)
    sid = _sid(tokens, typed.token)
    store.values[_session_key(sid)] = {"kind": "hash"}
    store.key_types[_session_key(sid)] = "hash"
    with pytest.raises(SessionStateUnavailable):
        await tokens.revoke_token(typed.token)
    assert store.values[_session_key(sid)] == {"kind": "hash"}


@pytest.mark.asyncio
async def test_rotation_cannot_restore_revoked_session() -> None:
    started = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    moments = [started]
    store = TtlLuaStore(lambda: moments[0])
    tokens = _service(store, moments)
    first, second = await _same_session_pair(tokens)
    sid = _sid(tokens, first.token)
    saved_family = store.values[_family_key(sid)]
    await tokens.revoke_token(second.token)
    assert store.values[_session_key(sid)] == "1"
    store.values[_family_key(sid)] = saved_family
    store.expire_at_ms[_family_key(sid)] = store.now_ms() + 60_000
    with pytest.raises(InvalidCredentials):
        await tokens.rotate_refresh(second.refresh_token, TAB_ID)
    assert store.values[_session_key(sid)] == "1"
    assert not str(store.values[_session_key(sid)]).startswith("grace")


@pytest.mark.asyncio
async def test_revocation_store_failure_maps_to_controlled_503() -> None:
    started = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    moments = [started]
    store = TtlLuaStore(lambda: moments[0])
    tokens = _service(store, moments)
    first, second = await _same_session_pair(tokens)
    store.fail_eval = True
    with pytest.raises(SessionStateUnavailable):
        await tokens.revoke_token(second.token)
    with pytest.raises(SessionStateUnavailable):
        await tokens.revoke_refresh_token(second.refresh_token)

    store.fail_eval = False
    value = account(must_change_password=False)
    identity = AuthenticatedIdentity(
        provider_code="local",
        login_name="admin",
        external_subject="local:admin",
        display_name="管理员",
        dept="平台部",
        groups=(),
        account=value,
    )
    users = FakeUserRepository(value)
    facade = AuthFacade(FakeAuthService(identity), users, tokens, FakeHasher())
    login = await facade.login("local", "admin", "Valid@Password123", IP, TAB_ID)
    assert isinstance(login, LoginSuccess)
    store.fail_eval = True
    with pytest.raises(ApiError) as error:
        await facade.logout(login.token, IP, login.refresh_token)
    assert error.value.status_code == 503
    assert error.value.code == "AUTH_SESSION_UNAVAILABLE"


def test_access_lifetime_budget_covers_protocol_cap() -> None:
    assert max_valid_access_lifetime_ms() == 15 * 60 * 1000
    assert MAX_VALID_ACCESS_LIFETIME_MS == 15 * 60 * 1000
