from __future__ import annotations

import base64
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, TypedDict

import pytest

from app.core.auth.accounts import SecurityPrincipal
from app.services.batch_query import BatchAccessScope
from app.services.crypto import CryptoService
from app.services.operations_query import (
    TIMELINE_EVENT_LIMIT,
    MessageQueryPage,
    OperationsQueryService,
    PhoneBadge,
    QueryNotFound,
    SqlOperationsQueryRepository,
    Timeline,
)


def crypto() -> CryptoService:
    key1 = base64.b64encode(b"1" * 32).decode()
    key2 = base64.b64encode(b"2" * 32).decode()
    return CryptoService.from_secret_values(
        '{"active_version":2,"keys":{"1":"' + key1 + '","2":"' + key2 + '"}}',
        '{"active_version":2,"keys":{"1":"' + key1 + '","2":"' + key2 + '"}}',
    )


class FakeRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        protected = crypto().protect_phone("13800138000")
        self.protected = (
            protected.phone_enc,
            protected.key_version,
            protected.phone_hmac,
        )

    async def search_messages(self, **values: object) -> MessageQueryPage:
        self.calls.append(("search", values))
        return MessageQueryPage(0, ())

    async def timeline(self, **values: object) -> Timeline:
        self.calls.append(("timeline", values))
        return Timeline(PhoneBadge(False, None, 0), ())

    async def authorized_phone(
        self,
        message_id: int,
        *,
        scope: BatchAccessScope,
        principal: SecurityPrincipal,
        ip: str,
    ) -> tuple[bytes, int, str] | None:
        self.calls.append(("decrypt", message_id, scope, principal, ip))
        return self.protected if message_id == 9 else None


class PhoneScopeArgs(TypedDict):
    phone: str
    scope: BatchAccessScope


@pytest.mark.asyncio
async def test_phone_search_uses_all_hmac_versions_and_never_passes_plaintext() -> None:
    repository = FakeRepository()
    service = OperationsQueryService(repository, crypto())
    start = datetime(2026, 7, 1, tzinfo=UTC)
    end = datetime(2026, 7, 12, tzinfo=UTC)

    await service.search_messages(
        phone="13800138000",
        start=start,
        end=end,
        category="notice",
        status="failed",
        page=2,
        size=50,
        scope=BatchAccessScope(dept="平台部"),
    )

    name, values = repository.calls[0]
    assert name == "search"
    assert isinstance(values, dict)
    assert len(values["phone_hmacs"]) == 2
    assert "13800138000" not in repr(values)
    assert values["category"] == "notice" and values["status"] == "failed"
    assert values["page"] == 2 and values["size"] == 50
    assert values["scope"] == BatchAccessScope(dept="平台部")


@pytest.mark.asyncio
async def test_query_rejects_naive_reversed_time_and_invalid_page() -> None:
    service = OperationsQueryService(FakeRepository(), crypto())
    values: PhoneScopeArgs = {
        "phone": "13800138000",
        "scope": BatchAccessScope(all_departments=True),
    }
    with pytest.raises(ValueError, match="timezone"):
        await service.search_messages(
            **values,
            start=datetime(2026, 7, 1),
            end=None,
            category=None,
            status=None,
            page=1,
            size=20,
        )
    with pytest.raises(ValueError, match="later"):
        await service.timeline(
            **values,
            start=datetime(2026, 7, 2, tzinfo=UTC),
            end=datetime(2026, 7, 1, tzinfo=UTC),
        )
    with pytest.raises(ValueError, match="positive"):
        await service.search_messages(
            **values, start=None, end=None, category=None, status=None, page=0, size=20,
        )
    with pytest.raises(ValueError, match="size"):
        await service.search_messages(
            **values, start=None, end=None, category=None, status=None, page=1, size=0,
        )
    with pytest.raises(ValueError, match="size"):
        await service.search_messages(
            **values, start=None, end=None, category=None, status=None, page=1, size=101,
        )


@pytest.mark.asyncio
async def test_timeline_uses_hmac_candidates_and_authorized_decrypt_is_ephemeral() -> None:
    repository = FakeRepository()
    service = OperationsQueryService(repository, crypto())
    scope = BatchAccessScope(dept="平台部")

    timeline = await service.timeline(
        phone="13800138000",
        start=None,
        end=None,
        scope=scope,
    )
    principal = SecurityPrincipal(11, 101, "approver-a", "平台部", "approver")
    phone = await service.decrypt_phone(
        9,
        scope=scope,
        principal=principal,
        ip="127.0.0.1",
    )

    assert timeline.badge.recv_30d == 0
    assert phone == "13800138000"
    assert repository.calls[-1] == ("decrypt", 9, scope, principal, "127.0.0.1")
    with pytest.raises(QueryNotFound):
        await service.decrypt_phone(
            10,
            scope=scope,
            principal=principal,
            ip="127.0.0.1",
        )


class FakeResult:
    def __init__(
        self,
        *,
        rows: list[dict[str, object]] | None = None,
        scalar: object | None = None,
    ) -> None:
        self.rows = rows or []
        self.scalar = scalar

    def mappings(self) -> FakeResult:
        return self

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self.rows)

    def scalar_one(self) -> object:
        assert self.scalar is not None
        return self.scalar

    def first(self) -> dict[str, object] | None:
        return self.rows[0] if self.rows else None


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


def bind(repository: SqlOperationsQueryRepository, connection: FakeConnection) -> None:
    repository._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_sql_search_projects_mask_only_and_applies_department_scope() -> None:
    repository = SqlOperationsQueryRepository()
    row = {
        "id": 9,
        "phone_mask": "138****8000",
        "status": "delivered",
        "report_desc": "DELIVRD",
        "report_time": datetime(2026, 7, 12, 8, 1, tzinfo=UTC),
        "created_at": datetime(2026, 7, 12, 8, 0, tzinfo=UTC),
        "batch_no": "BATCH-1",
        "category": "notice",
        "content": "系统通知",
        "sender": "通知应用",
    }
    connection = FakeConnection([FakeResult(scalar=1), FakeResult(rows=[row])])
    bind(repository, connection)

    page = await repository.search_messages(
        phone_hmacs=("a" * 64,),
        start=None,
        end=None,
        category="notice",
        status="delivered",
        page=1,
        size=20,
        scope=BatchAccessScope(dept="平台部"),
    )

    assert page.total == 1 and page.items[0].phone_mask == "138****8000"
    sql = " ".join(item[0] for item in connection.calls)
    assert "b.dept=:scope_dept" in sql
    assert "b.category=:category" in sql and "m.status=:status" in sql
    assert all(item[1]["category"] == "notice" for item in connection.calls)
    assert all(item[1]["status"] == "delivered" for item in connection.calls)
    assert "phone_mask" in sql
    assert "phone_enc" not in sql and "phone_hmac AS" not in sql
    assert "13800138000" not in repr(connection.calls)
    assert all(
        "CAST(:start AS timestamptz) IS NULL" in statement
        for statement, _params in connection.calls
    )
    assert all(
        "CAST(:end AS timestamptz) IS NULL" in statement for statement, _params in connection.calls
    )


@pytest.mark.asyncio
async def test_sql_timeline_combines_directions_and_badge_without_decryption() -> None:
    repository = SqlOperationsQueryRepository()
    moment = datetime(2026, 7, 12, 8, 0, tzinfo=UTC)
    events = [
        {
            "ts": moment,
            "direction": "out",
            "category": "verify",
            "batch_no": "BATCH-1",
            "content": "验证码******",
            "status": "delivered",
            "sender": "验证码应用",
        },
        {
            "ts": moment,
            "direction": "in",
            "category": "verify",
            "batch_no": "BATCH-1",
            "content": "退订",
            "status": None,
            "sender": "用户",
        },
    ]
    connection = FakeConnection(
        [
            FakeResult(rows=[{"source": "reply_optout"}]),
            FakeResult(scalar=2),
            FakeResult(rows=events),
        ]
    )
    bind(repository, connection)

    timeline = await repository.timeline(
        phone_hmacs=("a" * 64,),
        start=None,
        end=None,
        scope=BatchAccessScope(dept="平台部"),
    )

    assert timeline.badge == PhoneBadge(True, "reply_optout", 2)
    assert timeline.truncated is False
    assert [item.direction for item in timeline.events] == ["out", "in"]
    union_sql = connection.calls[2][0]
    assert "UNION ALL" in union_sql and "send_content_enc" not in union_sql
    assert "LIMIT :event_limit" in union_sql
    assert connection.calls[2][1]["event_limit"] == TIMELINE_EVENT_LIMIT + 1
    assert "phone_enc" not in union_sql and "验证码******" in timeline.events[0].content
    assert "b.dept=:scope_dept" in connection.calls[0][0]
    assert "CAST(:start AS timestamptz) IS NULL" in union_sql
    assert "CAST(:end AS timestamptz) IS NULL" in union_sql


@pytest.mark.asyncio
async def test_sql_timeline_caps_events_and_marks_truncated() -> None:
    repository = SqlOperationsQueryRepository()
    moment = datetime(2026, 7, 12, 8, 0, tzinfo=UTC)
    rows = [
        {
            "ts": moment,
            "direction": "out",
            "category": "notice",
            "batch_no": f"BATCH-{index}",
            "content": "系统通知",
            "status": "delivered",
            "sender": "通知应用",
        }
        for index in range(TIMELINE_EVENT_LIMIT + 1)
    ]
    connection = FakeConnection(
        [
            FakeResult(rows=[]),
            FakeResult(scalar=TIMELINE_EVENT_LIMIT + 1),
            FakeResult(rows=rows),
        ]
    )
    bind(repository, connection)

    timeline = await repository.timeline(
        phone_hmacs=("a" * 64,),
        start=None,
        end=None,
        scope=BatchAccessScope(dept="平台部"),
    )

    assert timeline.truncated is True
    assert len(timeline.events) == TIMELINE_EVENT_LIMIT


@pytest.mark.asyncio
async def test_authorized_phone_audits_reference_only_in_same_transaction() -> None:
    repository = SqlOperationsQueryRepository()
    protected = crypto().protect_phone("13800138000")
    connection = FakeConnection(
        [
            FakeResult(
                rows=[
                    {
                            "phone_enc": protected.phone_enc,
                            "phone_hmac": protected.phone_hmac,
                            "key_version": protected.key_version,
                        "batch_no": "BATCH-1",
                    }
                ]
            ),
            FakeResult(),
        ]
    )
    bind(repository, connection)
    principal = SecurityPrincipal(11, 101, "approver-a", "平台部", "approver")

    material = await repository.authorized_phone(
        9,
        scope=BatchAccessScope(dept="平台部"),
        principal=principal,
        ip="127.0.0.1",
    )

    assert material == (
        protected.phone_enc,
        protected.key_version,
        protected.phone_hmac,
    )
    audit_sql = " ".join(connection.calls[1][0].split())
    assert "INSERT INTO audit_log" in audit_sql
    audit_params = connection.calls[1][1]
    assert "13800138000" not in repr(audit_params)
    assert "phone_enc" not in repr(audit_params).casefold()
    assert "phone_hmac" not in repr(audit_params).casefold()
    assert int(audit_params["account_id"]) == 11
    assert int(audit_params["identity_id"]) == 101
    assert audit_params["ip"] == "127.0.0.1"
    assert audit_params["action"] == "message_phone_decrypt"
