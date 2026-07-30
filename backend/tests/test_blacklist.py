from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable

import pytest

from app.services.blacklist import BlacklistEntry, BlacklistService
from app.services.crypto import CryptoService


def crypto() -> CryptoService:
    key = base64.b64encode(b"b" * 32).decode()
    return CryptoService.from_secret_values(key, key)


class FakeRepository:
    def __init__(self) -> None:
        self.entries: dict[str, BlacklistEntry] = {}
        self.audits: list[dict[str, object]] = []

    async def list_entries(self) -> list[BlacklistEntry]:
        return list(self.entries.values())

    async def all_hmacs(self) -> set[str]:
        return set(self.entries)

    async def upsert_many(
        self,
        entries: list[BlacklistEntry],
        *,
        actor: str,
        source: str,
    ) -> int:
        self.entries.update({entry.phone_hmac: entry for entry in entries})
        self.audits.append({"actor": actor, "source": source, "count": len(entries)})
        return len(entries)

    async def delete(self, phone_hmac: str, *, actor: str) -> bool:
        removed = self.entries.pop(phone_hmac, None) is not None
        self.audits.append({"actor": actor, "count": int(removed)})
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


@pytest.mark.asyncio
async def test_blacklist_add_deduplicates_to_hmac_and_audits_count_only() -> None:
    repository = FakeRepository()
    cache = FakeCache()
    service = BlacklistService(repository, cache, crypto())

    entries = await service.add(
        ["13800138000", "13800138000", "13900139000"],
        source="import",
        remark="投诉导入",
        actor="admin01",
    )

    assert len(entries) == 2
    assert all(len(entry.phone_hmac) == 64 for entry in entries)
    assert {entry.phone_mask for entry in entries} == {"138****8000", "139****9000"}
    assert repository.audits == [{"actor": "admin01", "source": "import", "count": 2}]
    assert "13800138000" not in repr(repository.audits)
    assert cache.invalidations == 2


@pytest.mark.asyncio
async def test_blacklist_cache_filters_hmac_and_delete_invalidates_twice() -> None:
    repository = FakeRepository()
    cache = FakeCache()
    service = BlacklistService(repository, cache, crypto())
    entries = await service.add(
        ["13800138000"],
        source="manual",
        remark=None,
        actor="admin01",
    )
    phone_hmac = entries[0].phone_hmac

    assert await service.matches({phone_hmac, "f" * 64}) == {phone_hmac}
    before = cache.invalidations
    assert await service.delete(phone_hmac, actor="admin01") is True
    assert cache.invalidations == before + 2
    assert await service.matches({phone_hmac}) == set()
