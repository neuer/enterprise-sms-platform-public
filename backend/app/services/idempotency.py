"""Redis 快速索引与 PostgreSQL 事实源协同的幂等合同。"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from uuid import uuid4

IDEMPOTENCY_TTL_S = 86400  # Redis 快速索引 TTL；DB 事实源按 scheduled_at+安全窗口延长
IDEMPOTENCY_CLAIM_TTL_S = 30
IDEMPOTENCY_WAIT_ATTEMPTS = 100
IDEMPOTENCY_WAIT_INTERVAL_S = 0.05
IDEMPOTENCY_WAIT_MARGIN_S = 5


class IdempotencyCoordinationTimeout(RuntimeError):
    """等待中的幂等 owner 持续存活，协调窗口已耗尽。"""

CLAIM_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

CLAIM_RENEW_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""


class IdempotencyRedis(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, **kwargs: Any) -> Any: ...

    async def delete(self, key: str) -> Any: ...

    async def eval(self, *args: Any) -> Any: ...


class IdempotencyRepository(Protocol):
    async def exists(
        self, app_id: int | None, biz_id: str, batch_no: str
    ) -> bool: ...

    async def find_existing(
        self, app_id: int | None, biz_id: str
    ) -> str | None: ...

    async def find_request_hash(self, app_id: int | None, biz_id: str) -> str | None: ...


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

    @staticmethod
    def key(app_id: int | None, biz_id: str) -> str:
        if not biz_id or len(biz_id) > 32:
            raise ValueError("biz_id length must be 1..32")
        scope = "web" if app_id is None else str(app_id)
        return f"idem:{scope}:{biz_id}"

    @classmethod
    def claim_key(cls, app_id: int | None, biz_id: str) -> str:
        scope = "web" if app_id is None else str(app_id)
        cls.key(app_id, biz_id)
        return f"idem:claim:{scope}:{biz_id}"

    @classmethod
    def frequency_result_key(cls, app_id: int | None, biz_id: str) -> str:
        """生成幂等请求的逐号码频控结果缓存键。"""

        scope = "web" if app_id is None else str(app_id)
        cls.key(app_id, biz_id)
        return f"idem:freq:{scope}:{biz_id}"

    @classmethod
    def quota_result_key(cls, app_id: int | None, biz_id: str, date_key: str) -> str:
        """生成限定到上海自然日的配额预扣结果键。"""

        scope = "web" if app_id is None else str(app_id)
        cls.key(app_id, biz_id)
        if len(date_key) != 8 or not date_key.isdigit():
            raise ValueError("date_key must be YYYYMMDD")
        return f"idem:quota:{scope}:{biz_id}:{date_key}"

    async def request_hash(self, app_id: int | None, biz_id: str) -> str | None:
        """返回 PostgreSQL 事实源中的请求指纹；旧记录可能为 NULL。"""

        self.key(app_id, biz_id)
        return await self.repository.find_request_hash(app_id, biz_id)

    async def lookup(self, app_id: int | None, biz_id: str) -> str | None:
        key = self.key(app_id, biz_id)
        batch_no = await self.redis.get(key)
        if batch_no is None:
            batch_no = await self.repository.find_existing(app_id, biz_id)
            if batch_no is not None:
                await self.remember(app_id, biz_id, batch_no)
            return batch_no
        if await self.repository.exists(app_id, biz_id, batch_no):
            return batch_no
        await self.redis.delete(key)
        return None

    async def remember(self, app_id: int | None, biz_id: str, batch_no: str) -> None:
        await self.redis.set(
            self.key(app_id, biz_id),
            batch_no,
            nx=True,
            ex=IDEMPOTENCY_TTL_S,
        )

    async def claim(self, app_id: int | None, biz_id: str) -> str | None:
        token = uuid4().hex
        acquired = await self.redis.set(
            self.claim_key(app_id, biz_id),
            token,
            nx=True,
            ex=self.claim_ttl_s,
        )
        return token if acquired else None

    async def wait(self, app_id: int | None, biz_id: str) -> str | None:
        claim_key = self.claim_key(app_id, biz_id)
        for attempt in range(self.wait_attempts):
            if await self.redis.get(claim_key) is None:
                batch_no = await self.repository.find_existing(app_id, biz_id)
                if batch_no is not None:
                    await self.remember(app_id, biz_id, batch_no)
                return batch_no
            if attempt + 1 < self.wait_attempts:
                await self.sleeper(self.wait_interval_s)
        batch_no = await self.repository.find_existing(app_id, biz_id)
        if batch_no is not None:
            await self.remember(app_id, biz_id, batch_no)
            return batch_no
        raise IdempotencyCoordinationTimeout("idempotency wait timed out")

    async def renew(self, app_id: int | None, biz_id: str, token: str) -> bool:
        renewed = await self.redis.eval(
            CLAIM_RENEW_LUA,
            1,
            self.claim_key(app_id, biz_id),
            token,
            self.claim_ttl_s,
        )
        return bool(renewed)

    async def heartbeat(
        self,
        app_id: int | None,
        biz_id: str,
        token: str,
        lost: asyncio.Event,
    ) -> None:
        while not lost.is_set():
            try:
                await self.sleeper(self.heartbeat_interval_s)
                if not await self.renew(app_id, biz_id, token):
                    lost.set()
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                lost.set()
                return

    async def release(self, app_id: int | None, biz_id: str, token: str) -> None:
        await self.redis.eval(
            CLAIM_RELEASE_LUA,
            1,
            self.claim_key(app_id, biz_id),
            token,
        )
