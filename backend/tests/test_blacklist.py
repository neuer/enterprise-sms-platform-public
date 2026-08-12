from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable

import pytest

from app.core.auth.accounts import SecurityPrincipal
from app.services.blacklist import (
    BlacklistEntry,
    BlacklistPage,
    BlacklistService,
    BlacklistUpsertResult,
    RedisBlacklistCache,
)
from app.services.crypto import CryptoService


def crypto() -> CryptoService:
    key = base64.b64encode(b"b" * 32).decode()
    return CryptoService.from_secret_values(key, key)


PRINCIPAL = SecurityPrincipal(1, 11, "admin01", "平台部", "admin")


class FakeRepository:
    def __init__(self) -> None:
        self.entries: dict[str, BlacklistEntry] = {}
        self.audits: list[dict[str, object]] = []

    async def list_page(
        self,
        *,
        source: str | None,
        keyword: str | None,
        page: int,
        size: int,
    ) -> BlacklistPage:
        items = [
            entry
            for entry in self.entries.values()
            if (source is None or entry.source == source)
            and (
                keyword is None
                or keyword in entry.phone_mask
                or (entry.remark is not None and keyword in entry.remark)
            )
        ]
        return BlacklistPage(total=len(items), items=items[(page - 1) * size : page * size])

    async def all_hmacs(self) -> set[str]:
        return set(self.entries)

    async def upsert_many(
        self,
        entries: list[BlacklistEntry],
        *,
        principal: SecurityPrincipal,
        ip: str,
        source: str,
    ) -> BlacklistUpsertResult:
        updated = sum(1 for entry in entries if entry.phone_hmac in self.entries)
        self.entries.update({entry.phone_hmac: entry for entry in entries})
        self.audits.append(
            {"actor": principal.login_name, "ip": ip, "source": source, "count": len(entries)}
        )
        return BlacklistUpsertResult(added=len(entries) - updated, updated=updated)

    async def delete(
        self,
        phone_hmac: str,
        *,
        principal: SecurityPrincipal,
        ip: str,
    ) -> bool:
        removed = self.entries.pop(phone_hmac, None) is not None
        self.audits.append(
            {"actor": principal.login_name, "ip": ip, "count": int(removed)}
        )
        return removed


class FakeCache:
    def __init__(self) -> None:
        self.members: set[str] = set()
        self.loaded = False
        self.invalidations = 0

    async def invalidate(self) -> None:
        self.loaded = False
        self.invalidations += 1

    async def matches(
        self,
        candidates: set[str],
        loader: Callable[[], Awaitable[set[str]]],
    ) -> set[str]:
        if not self.loaded:
            self.members = await loader()
            self.loaded = True
        return candidates & self.members

    async def mutate(self, callback: Callable[[], Awaitable[object]]) -> object:
        await self.invalidate()
        try:
            return await callback()
        finally:
            await self.invalidate()


@pytest.mark.asyncio
async def test_blacklist_add_deduplicates_to_hmac_and_audits_count_only() -> None:
    repository = FakeRepository()
    cache = FakeCache()
    service = BlacklistService(repository, cache, crypto())

    result = await service.add(
        ["13800138000", "13800138000", "13900139000"],
        source="import",
        remark="投诉导入",
        principal=PRINCIPAL,
        ip="10.0.0.8",
    )

    assert len(result.entries) == 2
    assert (result.added, result.updated) == (2, 0)
    assert all(len(entry.phone_hmac) == 64 for entry in result.entries)
    assert {entry.phone_mask for entry in result.entries} == {"138****8000", "139****9000"}
    assert repository.audits == [
        {"actor": "admin01", "ip": "10.0.0.8", "source": "import", "count": 2}
    ]
    assert "13800138000" not in repr(repository.audits)
    assert cache.invalidations == 2


@pytest.mark.asyncio
async def test_blacklist_add_reports_updated_when_hmac_already_listed() -> None:
    repository = FakeRepository()
    service = BlacklistService(repository, FakeCache(), crypto())

    first = await service.add(
        ["13800138000"], source="manual", remark=None, principal=PRINCIPAL, ip="10.0.0.8"
    )
    second = await service.add(
        ["13800138000"], source="import", remark="投诉", principal=PRINCIPAL, ip="10.0.0.8"
    )

    assert (first.added, first.updated) == (1, 0)
    assert (second.added, second.updated) == (0, 1)


@pytest.mark.asyncio
async def test_blacklist_add_rejects_invalid_phones_with_line_numbers_only() -> None:
    service = BlacklistService(FakeRepository(), FakeCache(), crypto())

    with pytest.raises(ValueError) as caught:
        await service.add(
            ["13800138000", "123", "13900139000", "abc", "", "1380013800"],
            source="manual",
            remark=None,
            principal=PRINCIPAL,
            ip="10.0.0.8",
        )

    message = str(caught.value)
    assert "第 2、4、5、6 行" in message
    assert "123" not in message.replace("第 2", "")


@pytest.mark.asyncio
async def test_blacklist_list_page_validates_filters() -> None:
    repository = FakeRepository()
    service = BlacklistService(repository, FakeCache(), crypto())
    await service.add(
        ["13800138000"],
        source="manual",
        remark="投诉",
        principal=PRINCIPAL,
        ip="10.0.0.8",
    )

    page = await service.list_page(source="manual", keyword="投诉", page=1, size=20)
    assert page.total == 1
    assert page.items[0].phone_mask == "138****8000"
    assert (await service.list_page(source="import")).total == 0
    with pytest.raises(ValueError, match="invalid blacklist source"):
        await service.list_page(source="other")
    with pytest.raises(ValueError, match="invalid pagination"):
        await service.list_page(size=0)


@pytest.mark.asyncio
async def test_blacklist_cache_filters_hmac_and_delete_invalidates_once() -> None:
    repository = FakeRepository()
    cache = FakeCache()
    service = BlacklistService(repository, cache, crypto())
    result = await service.add(
        ["13800138000"],
        source="manual",
        remark=None,
        principal=PRINCIPAL,
        ip="10.0.0.8",
    )
    phone_hmac = result.entries[0].phone_hmac

    assert await service.matches({phone_hmac, "f" * 64}) == {phone_hmac}
    before = cache.invalidations
    assert await service.delete(phone_hmac, principal=PRINCIPAL, ip="10.0.0.8") is True
    assert cache.invalidations == before + 2
    assert await service.matches({phone_hmac}) == set()


@pytest.mark.asyncio
async def test_cache_rebuild_cannot_publish_stale_snapshot_after_mutation() -> None:
    class Pipeline:
        def __init__(self, redis: FakeRedis) -> None:
            self.redis = redis
            self.operations: list[tuple[str, object]] = []

        def delete(self, key: str) -> None:
            self.operations.append(("delete", key))

        def sadd(self, key: str, *members: str) -> None:
            self.operations.append(("sadd", (key, members)))

        def set(self, key: str, value: str) -> None:
            self.operations.append(("set", (key, value)))

        async def execute(self) -> None:
            for action, value in self.operations:
                if action == "delete":
                    await self.redis.delete(str(value))
                elif action == "sadd":
                    key, members = value  # type: ignore[misc]
                    self.redis.members.setdefault(key, set()).update(members)
                else:
                    key, stored = value  # type: ignore[misc]
                    self.redis.values[key] = stored

    class FakeRedis:
        def __init__(self) -> None:
            self.values: dict[str, str] = {}
            self.members: dict[str, set[str]] = {}
            self.mutex = asyncio.Lock()

        def lock(self, *_: object, **__: object) -> asyncio.Lock:
            return self.mutex

        async def get(self, key: str) -> str | None:
            return self.values.get(key)

        async def delete(self, key: str) -> None:
            self.values.pop(key, None)
            self.members.pop(key, None)

        def pipeline(self, *, transaction: bool) -> Pipeline:
            assert transaction
            return Pipeline(self)

        async def smismember(self, key: str, candidates: list[str]) -> list[bool]:
            return [candidate in self.members.get(key, set()) for candidate in candidates]

    redis = FakeRedis()
    cache = RedisBlacklistCache(redis)
    source = {"old"}
    loader_started = asyncio.Event()
    release_loader = asyncio.Event()
    mutation_started = asyncio.Event()

    async def loader() -> set[str]:
        snapshot = set(source)
        loader_started.set()
        await release_loader.wait()
        return snapshot

    first_match = asyncio.create_task(cache.matches({"old", "new"}, loader))
    await loader_started.wait()

    async def mutation() -> bool:
        mutation_started.set()
        source.clear()
        source.add("new")
        return True

    mutation_task = asyncio.create_task(cache.mutate(mutation))
    await asyncio.sleep(0)
    assert not mutation_started.is_set()
    release_loader.set()
    assert await first_match == {"old"}
    assert await mutation_task is True
    assert await cache.matches({"old", "new"}, lambda: _immediate(source)) == {"new"}


async def _immediate(values: set[str]) -> set[str]:
    return set(values)
