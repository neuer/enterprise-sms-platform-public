"""HMAC 黑名单管理与仅含 phone_hmac 的 Redis SET 缓存。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, TypeVar

from app.core.auth.accounts import SecurityPrincipal
from app.core.sensitive_text import reject_phone_in_text
from app.services.crypto import PHONE_PATTERN, CryptoService

BLACKLIST_KEY = "blacklist:phone_hmacs"
BLACKLIST_LOADED_KEY = "blacklist:phone_hmacs:loaded"
BLACKLIST_LOCK_KEY = "blacklist:phone_hmacs:lock"
VALID_SOURCES = frozenset({"manual", "reply_optout", "import"})
MAX_PAGE_SIZE = 100
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class BlacklistEntry:
    phone_hmac: str
    phone_enc: bytes
    phone_mask: str
    key_version: int
    source: str
    remark: str | None
    created_at: datetime | None = None
    hmac_candidates: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True, slots=True)
class BlacklistPage:
    total: int
    items: list[BlacklistEntry]


@dataclass(frozen=True, slots=True)
class BlacklistUpsertResult:
    """批量加黑结果：added 为新增号码数，updated 为已存在并更新来源/备注的号码数。"""

    added: int
    updated: int


@dataclass(frozen=True, slots=True)
class BlacklistAddResult:
    entries: list[BlacklistEntry]
    added: int
    updated: int


class BlacklistRepository(Protocol):
    async def list_page(
        self,
        *,
        source: str | None,
        keyword: str | None,
        page: int,
        size: int,
    ) -> BlacklistPage: ...

    async def all_hmacs(self) -> set[str]: ...

    async def upsert_many(
        self,
        entries: list[BlacklistEntry],
        *,
        principal: SecurityPrincipal,
        ip: str,
        source: str,
    ) -> BlacklistUpsertResult: ...

    async def delete(
        self,
        phone_hmac: str,
        *,
        principal: SecurityPrincipal,
        ip: str,
    ) -> bool: ...


class BlacklistCache(Protocol):
    async def invalidate(self) -> None: ...

    async def matches(
        self,
        candidates: set[str],
        loader: Callable[[], Awaitable[set[str]]],
    ) -> set[str]: ...

    async def mutate(self, callback: Callable[[], Awaitable[T]]) -> T: ...


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
        async with self._lock() as _lock:
            if not await self.redis.get(BLACKLIST_LOADED_KEY):
                members = await loader()
                pipe = self.redis.pipeline(transaction=True)
                pipe.delete(BLACKLIST_KEY)
                if members:
                    pipe.sadd(BLACKLIST_KEY, *members)
                pipe.set(BLACKLIST_LOADED_KEY, "1")
                await pipe.execute()
            flags = await self.redis.smismember(BLACKLIST_KEY, list(candidates))
            return {
                value for value, present in zip(candidates, flags, strict=True) if present
            }

    async def mutate(self, callback: Callable[[], Awaitable[T]]) -> T:
        """以同一分布式锁串行化事实变更与缓存重建。"""

        async with self._lock() as _lock:
            await self.redis.delete(BLACKLIST_LOADED_KEY)
            try:
                return await callback()
            finally:
                await self.redis.delete(BLACKLIST_LOADED_KEY)

    def _lock(self) -> Any:
        """有界租约锁，心跳续租避免崩溃后永久堵死。"""

        return _HeartbeatLock(self.redis.lock(BLACKLIST_LOCK_KEY, timeout=120, blocking_timeout=10))


class _HeartbeatLock:
    def __init__(self, lock: Any) -> None:
        self._lock = lock
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Any:
        await self._lock.__aenter__()
        self._stop.clear()

        async def _heartbeat() -> None:
            while not self._stop.is_set():
                await asyncio.sleep(40)
                try:
                    await self._lock.extend(120)
                except Exception:
                    return

        self._task = asyncio.create_task(_heartbeat())
        return self._lock

    async def __aexit__(self, *args: object) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        await self._lock.__aexit__(*args)


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

    async def list_page(
        self,
        *,
        source: str | None = None,
        keyword: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> BlacklistPage:
        """分页查询黑名单；keyword 模糊匹配掩码或备注，永不明文。"""
        if source is not None and source not in VALID_SOURCES:
            raise ValueError("invalid blacklist source")
        if page < 1 or not 1 <= size <= MAX_PAGE_SIZE:
            raise ValueError("invalid pagination")
        keyword = (keyword or "").strip() or None
        return await self.repository.list_page(
            source=source,
            keyword=keyword,
            page=page,
            size=size,
        )

    async def matches(self, candidates: set[str]) -> set[str]:
        return await self.cache.matches(candidates, self.repository.all_hmacs)

    async def add(
        self,
        phones: list[str],
        *,
        source: str,
        remark: str | None,
        principal: SecurityPrincipal,
        ip: str,
    ) -> BlacklistAddResult:
        """批量加黑；先统一校验号码格式，报错只带行号、不带号码明文。"""
        reject_phone_in_text(remark, field_name="remark")
        if source not in VALID_SOURCES:
            raise ValueError("invalid blacklist source")
        if not phones:
            raise ValueError("phones must not be empty")
        invalid_lines = [
            str(index)
            for index, phone in enumerate(phones, start=1)
            if PHONE_PATTERN.fullmatch(phone) is None
        ]
        if invalid_lines:
            shown = "、".join(invalid_lines[:5])
            suffix = f" 等共 {len(invalid_lines)}" if len(invalid_lines) > 5 else ""
            raise ValueError(f"第 {shown}{suffix} 行号码格式错误，应为 11 位手机号")
        entries_by_hmac: dict[str, BlacklistEntry] = {}
        for phone in phones:
            protected = self.crypto.protect_phone(phone, table="blacklist")
            entries_by_hmac[protected.phone_hmac] = BlacklistEntry(
                protected.phone_hmac,
                protected.phone_enc,
                protected.phone_mask,
                protected.key_version,
                source,
                remark,
                hmac_candidates=tuple(self.crypto.hmac_candidates(phone).items()),
            )
        entries = list(entries_by_hmac.values())

        async def mutate() -> BlacklistUpsertResult:
            return await self.repository.upsert_many(
                entries,
                principal=principal,
                ip=ip,
                source=source,
            )

        outcome = await self.cache.mutate(mutate)
        return BlacklistAddResult(entries, outcome.added, outcome.updated)

    async def delete(
        self,
        phone_hmac: str,
        *,
        principal: SecurityPrincipal,
        ip: str,
    ) -> bool:
        return await self.cache.mutate(
            lambda: self.repository.delete(
                phone_hmac,
                principal=principal,
                ip=ip,
            )
        )
