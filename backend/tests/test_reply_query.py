from __future__ import annotations

import base64
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest

from app.services.crypto import CryptoService, EncryptionContext
from app.services.reply_query import (
    ReplyNotFound,
    ReplyPage,
    ReplyQueryService,
    SqlReplyQueryRepository,
)


def crypto() -> CryptoService:
    v1 = base64.b64encode(b"1" * 32).decode()
    v2 = base64.b64encode(b"2" * 32).decode()
    ring1 = '{"active_version":2,"keys":{"1":"' + v1 + '","2":"' + v2 + '"}}'
    return CryptoService.from_secret_values(ring1, ring1)


def protected_reply(event_key: str, content: str) -> bytes:
    return crypto().encrypt_bound_packed_text(
        content,
        EncryptionContext(
            domain="reply-content",
            table="reply_event",
            column="content_enc",
            object_id=event_key,
        ),
    )


class FakeRepository:
    def __init__(self, *, found: bool = True) -> None:
        self.found = found
        self.list_calls: list[dict[str, Any]] = []
        self.optout_calls: list[dict[str, Any]] = []

    async def list_page(self, **values: Any) -> ReplyPage:
        self.list_calls.append(values)
        return ReplyPage(0, ())

    async def optout(self, reply_id: int, **values: Any) -> bool:
        self.optout_calls.append({"reply_id": reply_id, **values})
        return self.found


class FakeCache:
    def __init__(self) -> None:
        self.invalidations = 0

    async def invalidate(self) -> None:
        self.invalidations += 1


@pytest.mark.asyncio
async def test_phone_filter_becomes_all_hmac_candidates_before_repository() -> None:
    repository = FakeRepository()
    service = ReplyQueryService(repository, FakeCache(), crypto())

    await service.list_page(
        phone="13800138000",
        start=datetime.fromisoformat("2026-07-01T00:00:00+08:00"),
        end=datetime.fromisoformat("2026-07-31T23:59:59+08:00"),
        page=2,
        dept="研发部",
    )

    call = repository.list_calls[0]
    assert len(call["phone_hmacs"]) == 2
    assert "13800138000" not in str(call)
    assert call["dept"] == "研发部"
    assert call["page"] == 2


@pytest.mark.asyncio
async def test_naive_query_time_is_rejected() -> None:
    service = ReplyQueryService(FakeRepository(), FakeCache(), crypto())
    with pytest.raises(ValueError, match="timezone"):
        await service.list_page(
            phone=None,
            start=datetime(2026, 7, 1),
            end=None,
            page=1,
            dept=None,
        )


@pytest.mark.asyncio
async def test_optout_invalidates_cache_before_and_after_and_missing_raises() -> None:
    cache = FakeCache()
    repository = FakeRepository(found=False)
    service = ReplyQueryService(repository, cache, crypto())

    with pytest.raises(ReplyNotFound):
        await service.optout(4, dept="研发部", actor="operator01")

    assert cache.invalidations == 2
    assert repository.optout_calls == [{"reply_id": 4, "dept": "研发部", "actor": "operator01"}]


class FakeResult:
    def __init__(
        self,
        *,
        scalar: object = None,
        rows: list[dict[str, object]] | None = None,
    ) -> None:
        self.scalar = scalar
        self.rows = rows or []

    def scalar_one(self) -> object:
        return self.scalar

    def scalar_one_or_none(self) -> object:
        return self.scalar

    def mappings(self) -> FakeResult:
        return self

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self.rows)


class FakeConnection:
    def __init__(self, results: list[FakeResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, Any]] = []

    async def execute(self, statement: object, params: Any = None) -> FakeResult:
        self.calls.append((str(statement), params))
        return self.results.pop(0)


class FakeContext:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, *_: object) -> None:
        return None


class FakeEngine:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def connect(self) -> FakeContext:
        return FakeContext(self.connection)

    def begin(self) -> FakeContext:
        return FakeContext(self.connection)

    async def dispose(self) -> None:
        return None


def bind(repository: SqlReplyQueryRepository, connection: FakeConnection) -> None:
    repository._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_sql_query_filters_hmac_time_department_and_returns_mask_only() -> None:
    repository = SqlReplyQueryRepository(crypto=crypto())
    moment = datetime.fromisoformat("2026-07-12T08:00:00+08:00")
    connection = FakeConnection(
        [
            FakeResult(scalar=1),
            FakeResult(
                rows=[
                    {
                        "id": 5,
                        "phone_mask": "138****8000",
                        "event_key": "e" * 64,
                        "content_enc": protected_reply("e" * 64, "TD"),
                        "batch_no": "BATCH-1",
                        "reply_time": moment,
                        "blacklisted": False,
                    }
                ]
            ),
        ]
    )
    bind(repository, connection)

    page = await repository.list_page(
        phone_hmacs=("a" * 64,),
        start=None,
        end=None,
        page=1,
        dept="研发部",
    )

    assert page.items[0].phone_mask == "138****8000"
    assert page.items[0].content == "TD"
    assert page.items[0].blacklisted is False
    assert all("phone_enc" not in sql for sql, _ in connection.calls)
    assert "phone_hmac = ANY" in connection.calls[0][0]
    assert "b.dept=CAST(:dept AS text)" in connection.calls[0][0]
    rows_sql = connection.calls[1][0]
    assert "EXISTS" in rows_sql and "blacklist" in rows_sql
    assert "bl.phone_hmac=r.phone_hmac" in rows_sql


@pytest.mark.asyncio
async def test_sql_query_casts_nullable_filters_for_asyncpg() -> None:
    repository = SqlReplyQueryRepository()
    connection = FakeConnection([FakeResult(scalar=0), FakeResult(rows=[])])
    bind(repository, connection)

    await repository.list_page(
        phone_hmacs=(),
        start=None,
        end=None,
        page=1,
        dept=None,
    )

    sql = connection.calls[0][0]
    assert "CAST(:dept AS text) IS NULL" in sql
    assert "CAST(:start AS timestamptz) IS NULL" in sql
    assert "CAST(:end AS timestamptz) IS NULL" in sql


@pytest.mark.asyncio
async def test_sql_optout_copies_protected_fields_and_audits_count_only() -> None:
    repository = SqlReplyQueryRepository()
    connection = FakeConnection([FakeResult(scalar=1), FakeResult()])
    bind(repository, connection)

    assert await repository.optout(5, dept="研发部", actor="operator01") is True

    sql, params = connection.calls[0]
    assert "INSERT INTO blacklist" in sql
    for column in ("phone_enc", "phone_hmac", "phone_mask", "key_version"):
        assert column in sql
    assert "reply_optout" in sql
    assert "decrypt" not in sql.casefold()
    assert params == {"reply_id": 5, "dept": "研发部", "actor": "operator01"}
    assert '"count": 1' in connection.calls[1][1]["after"]
    assert "phone" not in connection.calls[1][1]["after"]
