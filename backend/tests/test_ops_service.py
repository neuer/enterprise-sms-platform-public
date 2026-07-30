from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.ops import OpsPage, OpsService, UnmatchedQuery, UnmatchedRecord


class FakeCrypto:
    def hmac_candidates(self, phone: str) -> dict[int, str]:
        assert phone == "13800138000"
        return {1: "a" * 64, 2: "b" * 64}


class FakeRepository:
    def __init__(self) -> None:
        self.query: UnmatchedQuery | None = None

    async def list_unmatched(
        self, query: UnmatchedQuery
    ) -> OpsPage[UnmatchedRecord]:
        self.query = query
        return OpsPage((), 0, query.page, query.page_size)


@pytest.mark.asyncio
async def test_unmatched_phone_is_converted_to_hmac_candidates_before_repository() -> None:
    repository = FakeRepository()
    service = OpsService(repository, FakeCrypto())
    start = datetime(2026, 7, 1, tzinfo=UTC)
    end = datetime(2026, 7, 12, tzinfo=UTC)

    await service.list_unmatched("13800138000", start, end, 1, 20)

    assert repository.query is not None
    assert repository.query.phone_hmacs == ("a" * 64, "b" * 64)
    assert repository.query.start == start and repository.query.end == end


@pytest.mark.asyncio
async def test_ops_time_range_rejects_naive_or_reversed_values() -> None:
    service = OpsService(FakeRepository(), FakeCrypto())
    aware = datetime(2026, 7, 12, tzinfo=UTC)
    with pytest.raises(ValueError, match="timezone"):
        await service.list_unmatched(None, datetime(2026, 7, 1), aware, 1, 20)
    with pytest.raises(ValueError, match="start"):
        await service.list_unmatched(None, aware, datetime(2026, 7, 1, tzinfo=UTC), 1, 20)
