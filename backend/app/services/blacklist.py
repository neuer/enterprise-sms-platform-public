"""HMAC 黑名单管理与仅含 phone_hmac 的 Redis SET 缓存。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from app.services.crypto import CryptoService

BLACKLIST_KEY = "blacklist:phone_hmacs"
BLACKLIST_LOADED_KEY = "blacklist:phone_hmacs:loaded"
VALID_SOURCES = frozenset({"manual", "reply_optout", "import"})


@dataclass(frozen=True, slots=True)
class BlacklistEntry:
    phone_hmac: str
    phone_enc: bytes
    phone_mask: str
    key_version: int
    source: str
    remark: str | None
    created_at: datetime | None = None


class BlacklistRepository(Protocol):
    async def list_entries(self) -> list[BlacklistEntry]: ...

    async def all_hmacs(self) -> set[str]: ...

    async def upsert_many(
        self,
        entries: list[BlacklistEntry],
        *,
        actor: str,
        source: str,
    ) -> int: ...

    async def delete(self, phone_hmac: str, *, actor: str) -> bool: ...


class BlacklistCache(Protocol):
    async def invalidate(self) -> None: ...

    async def matches(
        self,
        candidates: set[str],
        loader: Callable[[], Awaitable[set[str]]],
    ) -> set[str]: ...


class RedisBlacklistCache:
    """以 loaded 标记区分空集合与尚未装载，Redis 丢数据后自动回源。"""

    def __init__(self, redis: Any) -> None:
        self.redis = redis

    async def invalidate(self) -> None:
        await self.redis.delete(BLACKLIST_LOADED_KEY)

    async def matches(
        self,
        candidates: set[str],
        loader: Callable[[], Awaitable[set[str]]],
    ) -> set[str]:
        if not candidates:
            return set()
        if not await self.redis.get(BLACKLIST_LOADED_KEY):
            members = await loader()
            pipe = self.redis.pipeline(transaction=True)
            pipe.delete(BLACKLIST_KEY)
            if members:
                pipe.sadd(BLACKLIST_KEY, *members)
            pipe.set(BLACKLIST_LOADED_KEY, "1")
            await pipe.execute()
        flags = await self.redis.smismember(BLACKLIST_KEY, list(candidates))
        return {value for value, present in zip(candidates, flags, strict=True) if present}


class BlacklistService:
    def __init__(
        self,
        repository: BlacklistRepository,
        cache: BlacklistCache,
        crypto: CryptoService,
    ) -> None:
        self.repository = repository
        self.cache = cache
        self.crypto = crypto

    async def list_entries(self) -> list[BlacklistEntry]:
        return await self.repository.list_entries()

    async def matches(self, candidates: set[str]) -> set[str]:
        return await self.cache.matches(candidates, self.repository.all_hmacs)

    async def add(
        self,
        phones: list[str],
        *,
        source: str,
        remark: str | None,
        actor: str,
    ) -> list[BlacklistEntry]:
        if source not in VALID_SOURCES:
            raise ValueError("invalid blacklist source")
        if not phones:
            raise ValueError("phones must not be empty")
        protected = {item.phone_hmac: item for item in map(self.crypto.protect_phone, phones)}
        entries = [
            BlacklistEntry(
                item.phone_hmac,
                item.phone_enc,
                item.phone_mask,
                item.key_version,
                source,
                remark,
            )
            for item in protected.values()
        ]
        await self.cache.invalidate()
        try:
            await self.repository.upsert_many(entries, actor=actor, source=source)
        finally:
            await self.cache.invalidate()
        return entries

    async def delete(self, phone_hmac: str, *, actor: str) -> bool:
        await self.cache.invalidate()
        try:
            return await self.repository.delete(phone_hmac, actor=actor)
        finally:
            await self.cache.invalidate()
