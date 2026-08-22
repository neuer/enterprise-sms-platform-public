from __future__ import annotations

import asyncio
import base64
import json
from typing import Any, cast

import pytest

from app.core.auth.backends import InvalidCredentials, SessionStateUnavailable
from app.core.auth.jwt import (
    REFRESH_GRACE_SECONDS,
    _ROTATE_REFRESH_LUA,
    JwtClaims,
    JwtService,
)

TAB_ID = "a" * 32
SECRET = "a-jwt-secret-that-is-long-enough-for-hs256-tests"


class FakeAtomicStore:
    """按新 Lua 的原子语义模拟 Redis，同时保留通用 key/value 接口。"""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.lock = asyncio.Lock()
        self.grace_writes = 0

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: Any, *, ex: int) -> None:
        assert ex > 0
        self.values[key] = str(value)

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)

    async def eval(self, script: str, numkeys: int, *args: object) -> object:
        if script != _ROTATE_REFRESH_LUA:
            raise AssertionError("unexpected Lua script")
        assert numkeys == 2
        family_key, session_state_key = map(str, args[:2])
        expected, replacement, family_ttl, revoke_ttl = map(str, args[2:])
        assert int(family_ttl) > 0
        assert int(revoke_ttl) > 0
        async with self.lock:
            current = self.values.get(family_key)
            if current is None:
                self.values[session_state_key] = "1"
                self.values.pop(family_key, None)
                return [0, ""]
            if current == expected:
                self.values[family_key] = replacement
                self.values[session_state_key] = "grace\n" + expected + "\n" + replacement
                self.grace_writes += 1
                return [1, replacement]
            state = self.values.get(session_state_key, "")
            parts = state.split("\n", 2)
            if (
                len(parts) == 3
                and parts[0] == "grace"
                and parts[1] == expected
                and parts[2] == current
            ):
                return [2, parts[2]]
            self.values[session_state_key] = "1"
            self.values.pop(family_key, None)
            return [-1, ""]


def claims() -> JwtClaims:
    return JwtClaims(
        account_id=8,
        identity_id=18,
        provider_code="local",
        login_name="operator01",
        display_name="开发操作员",
        dept="业务一部",
        role="operator",  # type: ignore[arg-type]
        security_version=1,
    )


def service(store: FakeAtomicStore | None = None) -> tuple[JwtService, FakeAtomicStore]:
    selected = store or FakeAtomicStore()
    return JwtService(SECRET, cast(Any, selected)), selected


def session_state_key(tokens: JwtService, refresh_token: str) -> str:
    claims_value = tokens._claims(tokens._decode(refresh_token))
    return f"auth:jwt:session-revoked:{claims_value.session_id}"


@pytest.mark.asyncio
async def test_two_concurrent_refreshes_share_one_rotation_without_revocation() -> None:
    tokens, store = service()
    first = await tokens.issue_pair(claims(), TAB_ID)

    left, right = await asyncio.gather(
        tokens.rotate_refresh(first.refresh_token, TAB_ID),
        tokens.rotate_refresh(first.refresh_token, TAB_ID),
    )

    assert left.refresh_token == right.refresh_token
    state = store.values[session_state_key(tokens, left.refresh_token)]
    assert state.startswith("grace\n")
    assert first.refresh_token not in state
    assert left.refresh_token not in state
    assert (await tokens.verify(left.token)).account_id == 8
    assert (await tokens.verify(right.token)).account_id == 8
    third = await tokens.rotate_refresh(left.refresh_token, TAB_ID)
    assert third.refresh_token != left.refresh_token


@pytest.mark.asyncio
async def test_lost_response_retry_returns_the_same_refresh_token() -> None:
    tokens, store = service()
    first = await tokens.issue_pair(claims(), TAB_ID)

    rotated_but_lost = await tokens.rotate_refresh(first.refresh_token, TAB_ID)
    retried = await tokens.rotate_refresh(first.refresh_token, TAB_ID)

    assert retried.refresh_token == rotated_but_lost.refresh_token
    assert store.grace_writes == 1


@pytest.mark.asyncio
async def test_grace_hit_does_not_extend_or_create_another_grace() -> None:
    tokens, store = service()
    first = await tokens.issue_pair(claims(), TAB_ID)
    rotated = await tokens.rotate_refresh(first.refresh_token, TAB_ID)
    key = session_state_key(tokens, rotated.refresh_token)
    original = store.values[key]

    replay = await tokens.rotate_refresh(first.refresh_token, TAB_ID)

    assert replay.refresh_token == rotated.refresh_token
    assert store.values[key] == original
    assert store.grace_writes == 1


@pytest.mark.asyncio
async def test_grace_state_does_not_revoke_access_verification() -> None:
    tokens, _store = service()
    first = await tokens.issue_pair(claims(), TAB_ID)
    rotated = await tokens.rotate_refresh(first.refresh_token, TAB_ID)

    assert (await tokens.verify(rotated.token)).account_id == 8


@pytest.mark.asyncio
async def test_two_generations_old_token_cannot_chain_through_stale_grace() -> None:
    tokens, _store = service()
    first = await tokens.issue_pair(claims(), TAB_ID)
    second = await tokens.rotate_refresh(first.refresh_token, TAB_ID)
    third = await tokens.rotate_refresh(second.refresh_token, TAB_ID)

    with pytest.raises(InvalidCredentials):
        await tokens.rotate_refresh(first.refresh_token, TAB_ID)
    with pytest.raises(InvalidCredentials):
        await tokens.verify(third.token)


@pytest.mark.asyncio
async def test_out_of_grace_replay_revokes_current_family_fail_closed() -> None:
    tokens, store = service()
    first = await tokens.issue_pair(claims(), TAB_ID)
    second = await tokens.rotate_refresh(first.refresh_token, TAB_ID)
    store.values.pop(session_state_key(tokens, second.refresh_token))

    with pytest.raises(InvalidCredentials):
        await tokens.rotate_refresh(first.refresh_token, TAB_ID)
    with pytest.raises(InvalidCredentials):
        await tokens.rotate_refresh(second.refresh_token, TAB_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["grace", "grace\nonly-one", b"\xff"])
async def test_corrupt_grace_state_fails_closed(state: object) -> None:
    tokens, store = service()
    first = await tokens.issue_pair(claims(), TAB_ID)
    rotated = await tokens.rotate_refresh(first.refresh_token, TAB_ID)
    key = session_state_key(tokens, rotated.refresh_token)
    store.values[key] = cast(Any, state)

    with pytest.raises(SessionStateUnavailable):
        await tokens.verify(rotated.token)


@pytest.mark.asyncio
async def test_wrong_tab_binding_is_rejected_before_redis_rotation() -> None:
    tokens, store = service()
    first = await tokens.issue_pair(claims(), TAB_ID)

    with pytest.raises(InvalidCredentials, match="标签页"):
        await tokens.rotate_refresh(first.refresh_token, "b" * 32)
    assert store.grace_writes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [["not-an-int"], {}])
async def test_malformed_redis_result_is_fail_closed(malformed: object) -> None:
    class BrokenStore(FakeAtomicStore):
        async def eval(self, script: str, numkeys: int, *args: object) -> object:
            del script, numkeys, args
            return malformed

    tokens, _store = service(BrokenStore())
    first = await tokens.issue_pair(claims(), TAB_ID)
    with pytest.raises(SessionStateUnavailable):
        await tokens.rotate_refresh(first.refresh_token, TAB_ID)


@pytest.mark.asyncio
async def test_replay_can_reconstruct_token_across_active_key_rotation() -> None:
    first_key = base64.b64encode(b"a" * 32).decode()
    second_key = base64.b64encode(b"b" * 32).decode()
    keyring_one = json.dumps(
        {"active_version": 1, "keys": {"1": first_key, "2": second_key}}
    )
    keyring_two = json.dumps(
        {"active_version": 2, "keys": {"1": first_key, "2": second_key}}
    )
    store = FakeAtomicStore()
    first_service = JwtService(keyring_one, cast(Any, store))
    first = await first_service.issue_pair(claims(), TAB_ID)
    rotated = await first_service.rotate_refresh(first.refresh_token, TAB_ID)

    second_service = JwtService(keyring_two, cast(Any, store))
    replay = await second_service.rotate_refresh(first.refresh_token, TAB_ID)

    assert replay.refresh_token == rotated.refresh_token


def test_replacement_is_stable_across_display_name_only_changes() -> None:
    tokens, _store = service()
    original_claims = claims()
    predecessor, predecessor_payload = tokens._encode_refresh(
        original_claims,
        "session-1",
        2_000_000_000,
        TAB_ID,
    )
    renamed_claims = JwtClaims(
        account_id=original_claims.account_id,
        identity_id=original_claims.identity_id,
        provider_code=original_claims.provider_code,
        login_name=original_claims.login_name,
        display_name="更新后的展示名",
        dept=original_claims.dept,
        role=original_claims.role,
        security_version=original_claims.security_version,
        session_id="session-1",
    )

    first, _ = tokens._encode_refresh_replacement(
        original_claims,
        "session-1",
        2_000_000_000,
        TAB_ID,
        predecessor_token=predecessor,
        predecessor_payload=predecessor_payload,
        key_version=1,
    )
    replay, _ = tokens._encode_refresh_replacement(
        renamed_claims,
        "session-1",
        2_000_000_000,
        TAB_ID,
        predecessor_token=predecessor,
        predecessor_payload=predecessor_payload,
        key_version=1,
    )

    assert replay == first


def test_lua_updates_family_and_grace_atomically_without_storing_token() -> None:
    success = _ROTATE_REFRESH_LUA.split("if current == ARGV[1] then", 1)[1].split(
        "end", 1
    )[0]
    assert "SET', KEYS[1]" in success
    assert "SET', KEYS[2]" in success
    assert "local grace = 'grace\\n'" in success
    assert str(REFRESH_GRACE_SECONDS) in success
    assert "replacement_token" not in _ROTATE_REFRESH_LUA
    assert "ARGV[1] == previous_binding" in _ROTATE_REFRESH_LUA
