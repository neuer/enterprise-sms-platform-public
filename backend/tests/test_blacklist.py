from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable

import pytest

from app.services.blacklist import (
    BlacklistEntry,
    BlacklistPage,
    BlacklistService,
    BlacklistUpsertResult,
)
from app.services.crypto import CryptoService


def crypto() -> CryptoService:
    key = base64.b64encode(b"b" * 32).decode()
    return CryptoService.from_secret_values(key, key)


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
        actor: str,
        source: str,
    ) -> BlacklistUpsertResult:
        updated = sum(1 for entry in entries if entry.phone_hmac in self.entries)
        self.entries.update({entry.phone_hmac: entry for entry in entries})
        self.audits.append({"actor": actor, "source": source, "count": len(entries)})
        return BlacklistUpsertResult(added=len(entries) - updated, updated=updated)

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

    result = await service.add(
        ["13800138000", "13800138000", "13900139000"],
        source="import",
        remark="投诉导入",
        actor="admin01",
    )

    assert len(result.entries) == 2
    assert (result.added, result.updated) == (2, 0)
    assert all(len(entry.phone_hmac) == 64 for entry in result.entries)
    assert {entry.phone_mask for entry in result.entries} == {"138****8000", "139****9000"}
    assert repository.audits == [{"actor": "admin01", "source": "import", "count": 2}]
    assert "13800138000" not in repr(repository.audits)
    assert cache.invalidations == 1


@pytest.mark.asyncio
async def test_blacklist_add_reports_updated_when_hmac_already_listed() -> None:
    repository = FakeRepository()
    service = BlacklistService(repository, FakeCache(), crypto())

    first = await service.add(["13800138000"], source="manual", remark=None, actor="admin01")
    second = await service.add(["13800138000"], source="import", remark="投诉", actor="admin01")

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
            actor="admin01",
        )

    message = str(caught.value)
    assert "第 2、4、5、6 行" in message
    assert "123" not in message.replace("第 2", "")


@pytest.mark.asyncio
async def test_blacklist_list_page_validates_filters() -> None:
    repository = FakeRepository()
    service = BlacklistService(repository, FakeCache(), crypto())
    await service.add(["13800138000"], source="manual", remark="投诉", actor="admin01")

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
        actor="admin01",
    )
    phone_hmac = result.entries[0].phone_hmac

    assert await service.matches({phone_hmac, "f" * 64}) == {phone_hmac}
    before = cache.invalidations
    assert await service.delete(phone_hmac, actor="admin01") is True
    assert cache.invalidations == before + 1
    assert await service.matches({phone_hmac}) == set()
