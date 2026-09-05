"""Redis 快速索引与 PostgreSQL 事实源协同的幂等合同。"""

from __future__ import annotations

import asyncio
import hashlib
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from app.services.app_ratelimit import ControlPlaneUnavailable

IDEMPOTENCY_TTL_S = 86400  # Redis 快速索引 TTL；DB 事实源按 scheduled_at+安全窗口延长
IDEMPOTENCY_CLAIM_TTL_S = 30
IDEMPOTENCY_WAIT_ATTEMPTS = 100
IDEMPOTENCY_WAIT_INTERVAL_S = 0.05
IDEMPOTENCY_WAIT_MARGIN_S = 5


@dataclass(frozen=True, slots=True)
class IdempotencyScope:
    """稳定幂等主体：API 为 app，Web 为稳定账号/身份。"""

    kind: str
    id: str

    def __post_init__(self) -> None:
        if self.kind not in {
            "app",
            "account",
            "resend",
            "web-legacy",
            "uncertain-resend",
        }:
            raise ValueError("idempotency scope kind invalid")
        if not self.id or len(self.id) > 64:
            raise ValueError("idempotency scope id invalid")

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.id}"


def usage_request_key(scope: IdempotencyScope, biz_id: str, date_key: str) -> str:
    """把账本请求键绑定到与幂等相同的稳定主体，且不暴露主体或业务键。"""

    if not biz_id or len(biz_id) > 32:
        raise ValueError("biz_id length must be 1..32")
    if len(date_key) != 8 or not date_key.isdigit():
        raise ValueError("date_key must be YYYYMMDD")
    digest = hashlib.sha256(
        b"sms-platform:usage-request:v2\x00"
        + scope.key.encode("utf-8")
        + b"\x00"
        + biz_id.encode("utf-8")
    ).hexdigest()
    return f"acceptance:v2:{digest}:{date_key}"


@dataclass(frozen=True, slots=True)
class IdempotencyFingerprint:
    """PostgreSQL 中的版本化请求 HMAC；旧记录没有该事实。"""

    digest: str
    key_version: int


class IdempotencyCoordinationTimeout(RuntimeError):
    """等待中的幂等 owner 持续存活，协调窗口已耗尽。"""


@dataclass(frozen=True, slots=True)
class IdempotencyClaimView:
    token: str
    fingerprint: str
    generation: int


CLAIM_RELEASE_LUA = """
local current = redis.call('GET', KEYS[1])
if current == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

CLAIM_RENEW_LUA = """
local current = redis.call('GET', KEYS[1])
if current == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

CLAIM_PROJECT_LUA = """
local current = redis.call('GET', KEYS[1])
local payload = ARGV[1]
local ttl = tonumber(ARGV[2])
local new_gen = tonumber(ARGV[3])
if current == false then
  redis.call('SET', KEYS[1], payload, 'EX', ttl)
  return 1
end
if current == payload then
  redis.call('EXPIRE', KEYS[1], ttl)
  return 1
end
local sep1 = string.find(current, ':', 1, true)
if not sep1 then
  redis.call('SET', KEYS[1], payload, 'EX', ttl)
  return 1
end
local sep2 = string.find(current, ':', sep1 + 1, true)
if not sep2 then
  redis.call('SET', KEYS[1], payload, 'EX', ttl)
  return 1
end
local current_gen = tonumber(string.sub(current, sep2 + 1))
if current_gen == nil or current_gen < new_gen then
  redis.call('SET', KEYS[1], payload, 'EX', ttl)
  return 1
end
if current_gen > new_gen then
  return 0
end
return -2
"""


def parse_claim_payload(raw: str) -> IdempotencyClaimView:
    parts = str(raw).split(":", 2)
    if len(parts) < 3:
        return IdempotencyClaimView(str(raw), "", 1)
    return IdempotencyClaimView(parts[0], parts[1], int(parts[2]))


def claim_payload(view: IdempotencyClaimView) -> str:
    return f"{view.token}:{view.fingerprint}:{view.generation}"


def claim_view_from_row(row: dict[str, Any]) -> IdempotencyClaimView:
    return IdempotencyClaimView(
        str(row["token"]).strip(),
        str(row.get("fingerprint") or ""),
        int(row["generation"]),
    )


def claim_row_is_live(row: dict[str, Any]) -> bool:
    return str(row["state"]) == "active" and bool(row["lease_valid"])


class IdempotencyRedis(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, **kwargs: Any) -> Any: ...

    async def delete(self, key: str) -> Any: ...

    async def eval(self, *args: Any) -> Any: ...


class IdempotencyRepository(Protocol):
    async def exists(
        self, scope: IdempotencyScope, biz_id: str, batch_no: str
    ) -> bool: ...

    async def find_existing(
        self, scope: IdempotencyScope, biz_id: str
    ) -> str | None: ...

    async def find_request_fingerprint(
        self, scope: IdempotencyScope, biz_id: str
    ) -> IdempotencyFingerprint | None: ...


class IdempotencyCoordinator:
    """Redis 命中必须由数据库未过期记录确认，避免缓存孤儿误判。"""

    def __init__(
        self,
        redis: IdempotencyRedis,
        repository: IdempotencyRepository,
        *,
        claim_ttl_s: int = IDEMPOTENCY_CLAIM_TTL_S,
        heartbeat_interval_s: float | None = None,
        wait_attempts: int | None = None,
        wait_interval_s: float = IDEMPOTENCY_WAIT_INTERVAL_S,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        selected_heartbeat = (
            claim_ttl_s / 3 if heartbeat_interval_s is None else heartbeat_interval_s
        )
        if (
            claim_ttl_s < 1
            or not 0 < selected_heartbeat < claim_ttl_s
            or wait_interval_s < 0
        ):
            raise ValueError("invalid idempotency wait bounds")
        if wait_attempts is None:
            if wait_interval_s == 0:
                raise ValueError("default idempotency wait interval must be positive")
            wait_attempts = (
                math.ceil(
                    (claim_ttl_s + IDEMPOTENCY_WAIT_MARGIN_S) / wait_interval_s
                )
                + 1
            )
        if wait_attempts < 1:
            raise ValueError("invalid idempotency wait bounds")
        self.redis = redis
        self.repository = repository
        self.claim_ttl_s = claim_ttl_s
        self.heartbeat_interval_s = selected_heartbeat
        self.wait_attempts = wait_attempts
        self.wait_interval_s = wait_interval_s
        self.sleeper = sleeper
        self._payloads: dict[str, str] = {}
        self._local_generations: dict[str, int] = {}

    @staticmethod
    def key(scope: IdempotencyScope, biz_id: str) -> str:
        if not biz_id or len(biz_id) > 32:
            raise ValueError("biz_id length must be 1..32")
        return f"idem:{scope.key}:{biz_id}"

    @classmethod
    def claim_key(cls, scope: IdempotencyScope, biz_id: str) -> str:
        cls.key(scope, biz_id)
        return f"idem:claim:{scope.key}:{biz_id}"

    @classmethod
    def frequency_result_key(cls, scope: IdempotencyScope, biz_id: str) -> str:
        """生成幂等请求的逐号码频控结果缓存键。"""

        cls.key(scope, biz_id)
        return f"idem:freq:{scope.key}:{biz_id}"

    @classmethod
    def quota_result_key(
        cls, scope: IdempotencyScope, biz_id: str, date_key: str
    ) -> str:
        """生成限定到上海自然日的配额预扣结果键。"""

        cls.key(scope, biz_id)
        if len(date_key) != 8 or not date_key.isdigit():
            raise ValueError("date_key must be YYYYMMDD")
        return f"idem:quota:{scope.key}:{biz_id}:{date_key}"

    async def request_fingerprint(
        self, scope: IdempotencyScope, biz_id: str
    ) -> IdempotencyFingerprint | None:
        """返回 PostgreSQL 事实源中的版本化请求 HMAC；旧记录可能为空。"""

        self.key(scope, biz_id)
        return await self.repository.find_request_fingerprint(scope, biz_id)

    async def lookup(self, scope: IdempotencyScope, biz_id: str) -> str | None:
        key = self.key(scope, biz_id)
        try:
            batch_no = await self.redis.get(key)
        except Exception as exc:
            raise ControlPlaneUnavailable("幂等控制面不可用") from exc
        if batch_no is None:
            batch_no = await self.repository.find_existing(scope, biz_id)
            if batch_no is not None:
                await self.remember(scope, biz_id, batch_no)
            return batch_no
        if await self.repository.exists(scope, biz_id, batch_no):
            return batch_no
        await self.redis.delete(key)
        return None

    async def remember(
        self, scope: IdempotencyScope, biz_id: str, batch_no: str
    ) -> None:
        await self.redis.set(
            self.key(scope, biz_id),
            batch_no,
            nx=True,
            ex=IDEMPOTENCY_TTL_S,
        )

    async def inspect(
        self, scope: IdempotencyScope, biz_id: str
    ) -> IdempotencyClaimView | None:
        try:
            raw = await self.redis.get(self.claim_key(scope, biz_id))
        except Exception as exc:
            raise ControlPlaneUnavailable("幂等控制面不可用") from exc
        viewed = parse_claim_payload(str(raw)) if raw else None
        authority = await self._authority(scope, biz_id)
        if authority is None:
            return viewed
        if str(authority["state"]) == "completed" or not claim_row_is_live(authority):
            return None
        official = claim_view_from_row(authority)
        if (
            viewed is None
            or viewed.generation < official.generation
            or viewed.token != official.token
        ) and await self._project_view(scope, biz_id, official) == 0:
            return None
        return official

    def _payload_for(self, scope: IdempotencyScope, biz_id: str, token: str) -> str | None:
        key = self.claim_key(scope, biz_id)
        payload = self._payloads.get(key)
        if payload is not None and payload.startswith(f"{token}:"):
            return payload
        return None

    async def _authority(
        self, scope: IdempotencyScope, biz_id: str
    ) -> dict[str, Any] | None:
        loader = getattr(self.repository, "load_idempotency_claim", None)
        if loader is None:
            return None
        loaded = await loader(scope, biz_id)
        return dict(loaded) if loaded is not None else None

    async def _project_view(
        self,
        scope: IdempotencyScope,
        biz_id: str,
        viewed: IdempotencyClaimView,
    ) -> int:
        payload = claim_payload(viewed)
        try:
            projected = int(
                await self.redis.eval(
                    CLAIM_PROJECT_LUA,
                    1,
                    self.claim_key(scope, biz_id),
                    payload,
                    self.claim_ttl_s,
                    viewed.generation,
                )
            )
        except Exception as exc:
            raise ControlPlaneUnavailable("幂等控制面不可用") from exc
        if projected == -2:
            try:
                await self.redis.set(
                    self.claim_key(scope, biz_id),
                    payload,
                    ex=self.claim_ttl_s,
                )
            except Exception as exc:
                raise ControlPlaneUnavailable("幂等控制面不可用") from exc
            return 1
        return projected

    async def claim(
        self,
        scope: IdempotencyScope,
        biz_id: str,
        *,
        fingerprint: str = "",
    ) -> str | None:
        token = uuid4().hex
        claim_key = self.claim_key(scope, biz_id)
        reserver = getattr(self.repository, "reserve_idempotency_claim", None)
        if reserver is not None:
            generation = await reserver(
                scope,
                biz_id,
                token=token,
                fingerprint=fingerprint,
                ttl_s=self.claim_ttl_s,
            )
            if generation is None:
                return None
            viewed = IdempotencyClaimView(token, fingerprint, int(generation))
            if await self._project_view(scope, biz_id, viewed) != 1:
                return None
            self._payloads[claim_key] = claim_payload(viewed)
            return token
        generation = self._local_generations.get(claim_key, 0) + 1
        self._local_generations[claim_key] = generation
        payload = f"{token}:{fingerprint}:{generation}"
        try:
            acquired = await self.redis.set(
                claim_key,
                payload,
                nx=True,
                ex=self.claim_ttl_s,
            )
        except Exception as exc:
            raise ControlPlaneUnavailable("幂等控制面不可用") from exc
        if not acquired:
            return None
        self._payloads[claim_key] = payload
        return token

    async def wait(self, scope: IdempotencyScope, biz_id: str) -> str | None:
        claim_key = self.claim_key(scope, biz_id)
        live_checker = getattr(self.repository, "live_idempotency_claim", None)
        authority = await self._authority(scope, biz_id)
        if authority is not None and str(authority["state"]) == "completed":
            batch_no = await self.repository.find_existing(scope, biz_id)
            if batch_no is not None:
                await self.remember(scope, biz_id, batch_no)
                return batch_no
        if authority is not None and claim_row_is_live(authority):
            await self._project_view(scope, biz_id, claim_view_from_row(authority))
        for attempt in range(self.wait_attempts):
            owned = await self.redis.get(claim_key)
            if owned is None:
                batch_no = await self.repository.find_existing(scope, biz_id)
                if batch_no is not None:
                    await self.remember(scope, biz_id, batch_no)
                    return batch_no
                if live_checker is not None and await live_checker(scope, biz_id):
                    if attempt + 1 < self.wait_attempts:
                        await self.sleeper(self.wait_interval_s)
                    continue
                if authority is not None and claim_row_is_live(authority):
                    if attempt + 1 < self.wait_attempts:
                        await self.sleeper(self.wait_interval_s)
                    continue
                return None
            if attempt + 1 < self.wait_attempts:
                await self.sleeper(self.wait_interval_s)
        batch_no = await self.repository.find_existing(scope, biz_id)
        if batch_no is not None:
            await self.remember(scope, biz_id, batch_no)
            return batch_no
        if await self.redis.get(claim_key) is not None:
            raise IdempotencyCoordinationTimeout("idempotency wait timed out")
        if live_checker is not None and await live_checker(scope, biz_id):
            raise IdempotencyCoordinationTimeout("idempotency wait timed out")
        if authority is not None and claim_row_is_live(authority):
            raise IdempotencyCoordinationTimeout("idempotency wait timed out")
        return None

    async def renew(
        self, scope: IdempotencyScope, biz_id: str, token: str
    ) -> bool:
        payload = self._payload_for(scope, biz_id, token)
        if payload is None:
            viewed = await self.inspect(scope, biz_id)
            if viewed is None or viewed.token != token:
                return False
            payload = claim_payload(viewed)
        viewed = parse_claim_payload(payload)
        renewer = getattr(self.repository, "renew_idempotency_claim", None)
        if renewer is not None:
            renewed = await renewer(
                scope,
                biz_id,
                token=token,
                fingerprint=viewed.fingerprint,
                generation=viewed.generation,
                ttl_s=self.claim_ttl_s,
            )
            if not renewed:
                return False
            try:
                redis_ok = await self.redis.eval(
                    CLAIM_RENEW_LUA,
                    1,
                    self.claim_key(scope, biz_id),
                    payload,
                    self.claim_ttl_s,
                )
            except Exception as exc:
                raise ControlPlaneUnavailable("幂等控制面不可用") from exc
            if not redis_ok and await self._project_view(scope, biz_id, viewed) != 1:
                raise ControlPlaneUnavailable("幂等投影续租失败")
            return True
        renewed = await self.redis.eval(
            CLAIM_RENEW_LUA,
            1,
            self.claim_key(scope, biz_id),
            payload,
            self.claim_ttl_s,
        )
        return bool(renewed)

    async def heartbeat(
        self,
        scope: IdempotencyScope,
        biz_id: str,
        token: str,
        lost: asyncio.Event,
    ) -> None:
        while not lost.is_set():
            try:
                await self.sleeper(self.heartbeat_interval_s)
                if not await self.renew(scope, biz_id, token):
                    lost.set()
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                lost.set()
                return

    async def release(
        self, scope: IdempotencyScope, biz_id: str, token: str
    ) -> None:
        payload = self._payload_for(scope, biz_id, token)
        if payload is None:
            viewed = await self.inspect(scope, biz_id)
            if viewed is None or viewed.token != token:
                return
            payload = claim_payload(viewed)
        releaser = getattr(self.repository, "release_idempotency_claim", None)
        if releaser is not None:
            viewed = parse_claim_payload(payload)
            await releaser(
                scope,
                biz_id,
                token=token,
                generation=viewed.generation,
                reason="abandoned",
            )
        await self.redis.eval(
            CLAIM_RELEASE_LUA,
            1,
            self.claim_key(scope, biz_id),
            payload,
        )
        self._payloads.pop(self.claim_key(scope, biz_id), None)
