from __future__ import annotations

from typing import Any

import pytest

from app.services.app_ratelimit import ControlPlaneUnavailable
from app.services.idempotency import (
    CLAIM_PROJECT_LUA,
    CLAIM_RELEASE_LUA,
    CLAIM_RENEW_LUA,
    IDEMPOTENCY_WAIT_MARGIN_S,
    IdempotencyCoordinator,
    IdempotencyScope,
    parse_claim_payload,
)
from app.services.quota import REFUND_ONCE_LUA, QuotaService


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.set_calls: list[dict[str, Any]] = []
        self.eval_result: Any = [1, 3, 3]
        self.eval_calls: list[tuple[Any, ...]] = []

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, **kwargs: Any) -> bool:
        self.set_calls.append({"key": key, "value": value, **kwargs})
        if kwargs.get("nx") and key in self.values:
            return False
        self.values[key] = value
        return True

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)

    async def eval(self, *args: Any) -> Any:
        self.eval_calls.append(args)
        if args and args[0] == CLAIM_RELEASE_LUA:
            key = str(args[2])
            token = str(args[3])
            current = self.values.get(key)
            if current == token:
                self.values.pop(key, None)
                return 1
            return 0
        if args and args[0] == CLAIM_RENEW_LUA:
            key = str(args[2])
            token = str(args[3])
            current = self.values.get(key)
            return int(current == token)
        if args and args[0] == CLAIM_PROJECT_LUA:
            key = str(args[2])
            payload = str(args[3])
            new_gen = int(args[5])
            current = self.values.get(key)
            if current is None:
                self.values[key] = payload
                return 1
            if current == payload:
                return 1
            viewed = parse_claim_payload(current)
            if viewed.generation < new_gen:
                self.values[key] = payload
                return 1
            if viewed.generation > new_gen:
                return 0
            self.values[key] = payload
            return 1
        return self.eval_result


class FakeIdemRepository:
    def __init__(self) -> None:
        self.batches: set[str] = set()
        self.find_calls = 0

    async def exists(
        self, scope: IdempotencyScope, biz_id: str, batch_no: str
    ) -> bool:
        return batch_no in self.batches

    async def find_existing(
        self, scope: IdempotencyScope, biz_id: str
    ) -> str | None:
        self.find_calls += 1
        return next(iter(self.batches), None)

    async def find_request_fingerprint(
        self, scope: IdempotencyScope, biz_id: str
    ) -> None:
        return None

    async def live_idempotency_claim(
        self, scope: IdempotencyScope, biz_id: str
    ) -> bool:
        return False


class FakeClaimRepository(FakeIdemRepository):
    """补充权威 Claim 状态机，供双写路径单测。"""

    def __init__(self) -> None:
        super().__init__()
        self.rows: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.renew_ok = True
        self.release_calls = 0

    def _key(self, scope: IdempotencyScope, biz_id: str) -> tuple[str, str, str]:
        return (scope.kind, scope.id, biz_id)

    async def reserve_idempotency_claim(
        self,
        scope: IdempotencyScope,
        biz_id: str,
        *,
        token: str,
        fingerprint: str,
        ttl_s: int,
    ) -> int | None:
        key = self._key(scope, biz_id)
        current = self.rows.get(key)
        if current is None:
            self.rows[key] = {
                "token": token,
                "fingerprint": fingerprint,
                "generation": 1,
                "state": "active",
                "lease_valid": True,
                "batch_id": None,
            }
            return 1
        if current["state"] == "completed" or (
            current["state"] == "active" and current["lease_valid"]
        ):
            return None
        current["generation"] = int(current["generation"]) + 1
        current["token"] = token
        current["fingerprint"] = fingerprint
        current["state"] = "active"
        current["lease_valid"] = True
        current["batch_id"] = None
        return int(current["generation"])

    async def renew_idempotency_claim(
        self,
        scope: IdempotencyScope,
        biz_id: str,
        *,
        token: str,
        fingerprint: str,
        generation: int,
        ttl_s: int,
        clock_margin_s: int = 1,
    ) -> bool:
        if not self.renew_ok:
            return False
        current = self.rows.get(self._key(scope, biz_id))
        return (
            current is not None
            and current["state"] == "active"
            and current["lease_valid"]
            and current["token"] == token
            and current["fingerprint"] == fingerprint
            and int(current["generation"]) == generation
        )

    async def release_idempotency_claim(
        self,
        scope: IdempotencyScope,
        biz_id: str,
        *,
        token: str,
        generation: int,
        reason: str = "abandoned",
    ) -> bool:
        self.release_calls += 1
        current = self.rows.get(self._key(scope, biz_id))
        if (
            current is None
            or current["state"] != "active"
            or current["token"] != token
            or int(current["generation"]) != generation
            or current["batch_id"] is not None
        ):
            return False
        current["state"] = "released"
        current["lease_valid"] = False
        return True

    async def load_idempotency_claim(
        self, scope: IdempotencyScope, biz_id: str
    ) -> dict[str, Any] | None:
        current = self.rows.get(self._key(scope, biz_id))
        return dict(current) if current is not None else None

    async def live_idempotency_claim(
        self, scope: IdempotencyScope, biz_id: str
    ) -> bool:
        current = self.rows.get(self._key(scope, biz_id))
        return bool(
            current is not None
            and current["state"] == "active"
            and current["lease_valid"]
        )


@pytest.mark.asyncio
async def test_idempotency_uses_exact_key_ttl_and_does_not_trust_stale_redis() -> None:
    redis = FakeRedis()
    repo = FakeIdemRepository()
    scope = IdempotencyScope("app", "7")
    redis.values["idem:app:7:biz-1"] = "stale-batch"
    coordinator = IdempotencyCoordinator(redis, repo)

    assert await coordinator.lookup(scope, "biz-1") is None
    assert "idem:app:7:biz-1" not in redis.values
    await coordinator.remember(scope, "biz-1", "new-batch")
    assert redis.set_calls[-1] == {
        "key": "idem:app:7:biz-1",
        "value": "new-batch",
        "nx": True,
        "ex": 86400,
    }


@pytest.mark.asyncio
async def test_idempotency_account_scope_uses_stable_identity_and_hash_port() -> None:
    redis = FakeRedis()
    repo = FakeIdemRepository()
    scope = IdempotencyScope("account", "1:10")
    repo.batches.add("web-batch")
    redis.values["idem:account:1:10:biz-web"] = "web-batch"
    coordinator = IdempotencyCoordinator(redis, repo)

    assert IdempotencyCoordinator.key(scope, "biz-web") == "idem:account:1:10:biz-web"
    assert coordinator.claim_key(scope, "biz-web") == "idem:claim:account:1:10:biz-web"
    assert coordinator.frequency_result_key(scope, "biz-web") == (
        "idem:freq:account:1:10:biz-web"
    )
    assert coordinator.quota_result_key(scope, "biz-web", "20260711") == (
        "idem:quota:account:1:10:biz-web:20260711"
    )
    assert await coordinator.lookup(scope, "biz-web") == "web-batch"
    assert await coordinator.request_fingerprint(scope, "biz-web") is None


@pytest.mark.asyncio
async def test_idempotency_returns_only_database_confirmed_batch() -> None:
    redis = FakeRedis()
    repo = FakeIdemRepository()
    scope = IdempotencyScope("app", "7")
    repo.batches.add("real-batch")
    redis.values["idem:app:7:biz-2"] = "real-batch"
    assert (
        await IdempotencyCoordinator(redis, repo).lookup(scope, "biz-2")
        == "real-batch"
    )


@pytest.mark.asyncio
async def test_idempotency_claim_wait_and_compare_delete_release_are_bounded() -> None:
    redis = FakeRedis()
    repo = FakeIdemRepository()
    coordinator = IdempotencyCoordinator(
        redis,
        repo,
        wait_attempts=2,
        wait_interval_s=0,
    )
    scope = IdempotencyScope("app", "7")

    token = await coordinator.claim(scope, "biz-3")
    assert token is not None
    assert redis.set_calls[-1]["key"] == "idem:claim:app:7:biz-3"
    assert redis.set_calls[-1]["nx"] is True
    assert redis.set_calls[-1]["ex"] > 0
    assert await coordinator.claim(scope, "biz-3") is None
    with pytest.raises(RuntimeError, match="timed out"):
        await coordinator.wait(scope, "biz-3")
    assert repo.find_calls == 1

    payload = redis.values["idem:claim:app:7:biz-3"]
    assert payload.startswith(f"{token}:")
    await coordinator.release(scope, "biz-3", "wrong-token")
    assert redis.values["idem:claim:app:7:biz-3"] == payload
    await coordinator.release(scope, "biz-3", token)
    assert "idem:claim:app:7:biz-3" not in redis.values
    assert "ARGV[1] .. ':'" not in CLAIM_RELEASE_LUA
    assert "current == ARGV[1]" in CLAIM_RELEASE_LUA
    assert coordinator.frequency_result_key(scope, "biz-3") == "idem:freq:app:7:biz-3"
    assert coordinator.quota_result_key(scope, "biz-3", "20260711") == (
        "idem:quota:app:7:biz-3:20260711"
    )


@pytest.mark.asyncio
async def test_idempotency_wait_returns_database_fact_from_claim_owner() -> None:
    redis = FakeRedis()
    repo = FakeIdemRepository()
    coordinator = IdempotencyCoordinator(
        redis,
        repo,
        wait_attempts=2,
        wait_interval_s=0,
    )
    scope = IdempotencyScope("app", "7")
    assert await coordinator.claim(scope, "biz-4") is not None
    sleeps = 0

    async def publish_fact(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        repo.batches.add("owner-batch")
        redis.values.pop("idem:claim:app:7:biz-4", None)

    coordinator.sleeper = publish_fact
    assert await coordinator.wait(scope, "biz-4") == "owner-batch"
    assert sleeps == 1
    assert repo.find_calls == 1


@pytest.mark.asyncio
async def test_wait_does_not_treat_missing_redis_as_unowned_when_pg_claim_live() -> None:
    redis = FakeRedis()
    repo = FakeIdemRepository()
    async def live(_scope: IdempotencyScope, _biz_id: str) -> bool:
        return True

    repo.live_idempotency_claim = live  # type: ignore[method-assign]
    coordinator = IdempotencyCoordinator(
        redis,
        repo,
        wait_attempts=2,
        wait_interval_s=0,
    )
    scope = IdempotencyScope("app", "7")
    with pytest.raises(RuntimeError, match="timed out"):
        await coordinator.wait(scope, "biz-live")
    assert repo.find_calls >= 1


@pytest.mark.asyncio
async def test_claim_renew_requires_owner_token_and_default_wait_covers_lease() -> None:
    redis = FakeRedis()
    coordinator = IdempotencyCoordinator(redis, FakeIdemRepository())
    scope = IdempotencyScope("app", "7")
    token = await coordinator.claim(scope, "biz-renew")
    assert token is not None

    assert await coordinator.renew(scope, "biz-renew", "wrong-token") is False
    assert await coordinator.renew(scope, "biz-renew", token) is True
    assert "ARGV[1] .. ':'" not in CLAIM_RENEW_LUA
    assert "current == ARGV[1]" in CLAIM_RENEW_LUA
    assert coordinator.wait_attempts * coordinator.wait_interval_s >= (
        coordinator.claim_ttl_s + IDEMPOTENCY_WAIT_MARGIN_S
    )
    viewed = await coordinator.inspect(scope, "biz-renew")
    assert viewed is not None
    assert viewed.generation == 1
    await coordinator.release(scope, "biz-renew", token)
    token2 = await coordinator.claim(scope, "biz-renew", fingerprint="abc")
    assert token2 is not None
    viewed2 = await coordinator.inspect(scope, "biz-renew")
    assert viewed2 is not None
    assert viewed2.generation == 2
    assert viewed2.fingerprint == "abc"
    stale_payload = f"{token}::{1}"
    redis.values[coordinator.claim_key(scope, "biz-renew")] = stale_payload
    assert await coordinator.renew(scope, "biz-renew", token2) is False


@pytest.mark.asyncio
async def test_db_claim_projects_over_stale_redis_generation() -> None:
    redis = FakeRedis()
    repo = FakeClaimRepository()
    coordinator = IdempotencyCoordinator(redis, repo)
    scope = IdempotencyScope("app", "7")
    redis.values["idem:claim:app:7:biz-project"] = "old-token::1"
    token = await coordinator.claim(scope, "biz-project", fingerprint="fp")
    assert token is not None
    viewed = await coordinator.inspect(scope, "biz-project")
    assert viewed is not None
    assert viewed.generation == 1
    assert viewed.token == token
    assert redis.values["idem:claim:app:7:biz-project"].endswith(":1")
    assert "current_gen < new_gen" in CLAIM_PROJECT_LUA


@pytest.mark.asyncio
async def test_db_renew_failure_keeps_redis_and_marks_lost() -> None:
    redis = FakeRedis()
    repo = FakeClaimRepository()
    coordinator = IdempotencyCoordinator(redis, repo)
    scope = IdempotencyScope("app", "7")
    token = await coordinator.claim(scope, "biz-db-renew")
    assert token is not None
    payload = redis.values["idem:claim:app:7:biz-db-renew"]
    repo.renew_ok = False
    assert await coordinator.renew(scope, "biz-db-renew", token) is False
    assert redis.values["idem:claim:app:7:biz-db-renew"] == payload


@pytest.mark.asyncio
async def test_db_renew_success_redis_failure_fails_closed() -> None:
    redis = FakeRedis()
    repo = FakeClaimRepository()
    coordinator = IdempotencyCoordinator(redis, repo)
    scope = IdempotencyScope("app", "7")
    token = await coordinator.claim(scope, "biz-redis-renew")
    assert token is not None

    async def boom(*_args: Any) -> Any:
        raise RuntimeError("redis down")

    redis.eval = boom  # type: ignore[method-assign]
    with pytest.raises(ControlPlaneUnavailable):
        await coordinator.renew(scope, "biz-redis-renew", token)
    assert repo.rows[("app", "7", "biz-redis-renew")]["state"] == "active"


@pytest.mark.asyncio
async def test_db_claim_redis_write_failure_keeps_authoritative_row() -> None:
    redis = FakeRedis()
    repo = FakeClaimRepository()
    coordinator = IdempotencyCoordinator(redis, repo)
    scope = IdempotencyScope("app", "7")

    async def boom(*_args: Any) -> Any:
        raise RuntimeError("redis down")

    redis.eval = boom  # type: ignore[method-assign]
    with pytest.raises(ControlPlaneUnavailable):
        await coordinator.claim(scope, "biz-ghost")
    row = repo.rows[("app", "7", "biz-ghost")]
    assert row["state"] == "active"
    assert row["generation"] == 1


@pytest.mark.asyncio
async def test_inspect_rebuilds_active_owner_after_redis_flush() -> None:
    redis = FakeRedis()
    repo = FakeClaimRepository()
    coordinator = IdempotencyCoordinator(redis, repo)
    scope = IdempotencyScope("app", "7")
    token = await coordinator.claim(scope, "biz-flush", fingerprint="fp")
    assert token is not None
    redis.values.clear()
    viewed = await coordinator.inspect(scope, "biz-flush")
    assert viewed is not None
    assert viewed.token == token
    assert redis.values["idem:claim:app:7:biz-flush"].startswith(f"{token}:")


@pytest.mark.asyncio
async def test_release_updates_postgres_state_not_only_redis() -> None:
    redis = FakeRedis()
    repo = FakeClaimRepository()
    coordinator = IdempotencyCoordinator(redis, repo)
    scope = IdempotencyScope("app", "7")
    token = await coordinator.claim(scope, "biz-release")
    assert token is not None
    await coordinator.release(scope, "biz-release", token)
    assert repo.release_calls == 1
    assert repo.rows[("app", "7", "biz-release")]["state"] == "released"
    assert "idem:claim:app:7:biz-release" not in redis.values


@pytest.mark.asyncio
async def test_quota_refund_once_uses_event_marker_to_prevent_double_credit() -> None:
    redis = FakeRedis()
    redis.eval_result = [1, 7, 9]
    result = await QuotaService(redis).refund_once(
        app_id=7,
        dept="研发部",
        category="market",
        date_key="20260711",
        cost=3,
        event_id="batch-1:cancelled",
        marker_ttl_s=86400,
    )
    assert result.released is True
    assert (result.usage.app_used, result.usage.dept_used) == (7, 9)
    call = redis.eval_calls[0]
    assert call[1] == 4
    assert call[4] == "quota:volume:app:7:market:20260711"
    assert call[5] == "quota:refund:batch-1:cancelled"


def test_refund_once_lua_does_not_create_non_expiring_keys_after_day_rollover() -> None:
    assert "if redis.call('EXISTS', KEYS[1]) == 1" in REFUND_ONCE_LUA
    assert "if redis.call('EXISTS', KEYS[2]) == 1" in REFUND_ONCE_LUA
    assert "if redis.call('EXISTS', KEYS[3]) == 1" in REFUND_ONCE_LUA
