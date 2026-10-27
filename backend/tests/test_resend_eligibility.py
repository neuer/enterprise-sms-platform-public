"""回执未到齐不能消耗唯一重发；未覆盖号码必须走显式补发。

审计动作：resend_supplement_preview、resend_supplement_confirm。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from app.services.batch_query import BatchAccessScope
from app.services.resend_eligibility import (
    ResendBlocked,
    decide_resend,
    reject_open_receipts,
)
from app.services.resend_supplement import ResendSupplementService

INTENT = UUID("40000000-0000-4000-8000-000000000882")


def test_open_receipts_block_the_only_ordinary_resend() -> None:
    blocked = decide_resend(
        status="sending",
        active=1,
        failed=1,
        covered=0,
        child_batch_no=None,
        needs_review=False,
    )
    ready = decide_resend(
        status="completed",
        active=0,
        failed=2,
        covered=0,
        child_batch_no=None,
        needs_review=False,
    )
    partial = decide_resend(
        status="completed",
        active=0,
        failed=2,
        covered=1,
        child_batch_no="CHILD-1",
        needs_review=False,
    )

    assert blocked.can_resend_failed is False
    assert blocked.block_reason == "receipts_pending"
    assert ready.can_resend_failed is True and ready.uncovered_failed == 2
    assert partial.block_reason == "supplement_required"
    assert partial.covered_failed == 1 and partial.uncovered_failed == 1


class FakeResult:
    def __init__(self, value: int = 0) -> None:
        self.value = value

    def scalar_one(self) -> int:
        return self.value


class FakeConnection:
    def __init__(self, value: int) -> None:
        self.value = value
        self.calls: list[str] = []

    async def execute(self, statement: object, _params: Any = None) -> FakeResult:
        self.calls.append(str(statement))
        return FakeResult(self.value)


@pytest.mark.asyncio
async def test_locked_source_rejects_pending_receipts_before_any_child() -> None:
    connection = FakeConnection(1)
    with pytest.raises(ResendBlocked) as blocked:
        await reject_open_receipts(connection, 8, "completed")
    assert blocked.value.reason == "receipts_pending"
    assert "pending" in connection.calls[0] and "sent" in connection.calls[0]


@pytest.mark.asyncio
async def test_stale_supplement_releases_members_without_sending() -> None:
    accepted: list[object] = []

    class Connection:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def execute(self, statement: object, _params: Any = None) -> Any:
            sql = str(statement)
            self.calls.append(sql)
            if "claim_state='frozen'" in sql and "SELECT" in sql:
                return _rows(
                    [
                        {
                            "id": 3,
                            "created_at": datetime(2026, 7, 12, tzinfo=UTC),
                            "status": "delivered",
                            "phone_enc": b"",
                            "phone_hmac": "a" * 64,
                            "key_version": 1,
                        }
                    ]
                )
            if "FOR UPDATE OF b" in sql:
                return _rows(
                    [
                        {
                            "id": 8,
                            "batch_no": "SOURCE-1",
                            "category": "notice",
                            "channel": "web",
                            "dept": "平台部",
                            "send_content_enc": b"unused",
                            "sign_name": None,
                            "consent_confirmed": False,
                            "is_test": False,
                        }
                    ]
                )
            if "FROM sms_resend_supplement\n" in sql and "FOR UPDATE" in sql:
                return _rows([{"id": INTENT, "state": "preview", "child_batch_id": None}])
            return _rows([])

    class Engine:
        def __init__(self, connection: Connection) -> None:
            self.connection = connection

        def begin(self) -> Any:
            return _ctx(self.connection)

        async def dispose(self) -> None:
            return None

    connection = Connection()
    service = ResendSupplementService(crypto=None)  # type: ignore[arg-type]
    service._engine = lambda: Engine(connection)  # type: ignore[method-assign]

    async def accept(request: object) -> object:
        accepted.append(request)
        raise AssertionError("stale preview must not send")

    with pytest.raises(ResendBlocked) as blocked:
        await service.confirm(
            "SOURCE-1",
            INTENT,
            BatchAccessScope(dept="平台部"),
            actor=None,
            accept=accept,
        )
    assert blocked.value.reason == "preview_stale"
    assert accepted == []
    assert any("claim_state='released'" in sql for sql in connection.calls)


class _rows:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def mappings(self) -> _rows:
        return self

    def __iter__(self) -> Any:
        return iter(self.rows)

    def one_or_none(self) -> dict[str, object] | None:
        return self.rows[0] if self.rows else None

    def one(self) -> dict[str, object]:
        return self.rows[0]


class _ctx:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    async def __aenter__(self) -> Any:
        return self.connection

    async def __aexit__(self, *_exc: object) -> None:
        return None
