"""下发前黑名单：verify 放行，已提交分片不退款，命中号码不进入发送列表。"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from app.services.crypto import CryptoService
from app.services.dispatch_blacklist import (
    DispatchAction,
    blacklist_required,
    decide_dispatch,
)
from app.tasks.send import ChunkPayload


def test_category_policy_controls_dispatch_blacklist() -> None:
    assert blacklist_required("verify", 7, False) is False
    assert blacklist_required("market", 7, False) is True
    assert blacklist_required("notice", 7, False) is False
    assert blacklist_required("notice", 7, True) is True
    assert blacklist_required("notice", None, None) is True


class _Result:
    def __init__(self, rows: list[dict[str, object]] | None = None, scalar: object = None) -> None:
        self.rows = rows or []
        self.scalar = scalar

    def mappings(self) -> _Result:
        return self

    def one_or_none(self) -> dict[str, object] | None:
        return self.rows[0] if self.rows else None

    def scalar_one(self) -> object:
        return self.scalar

    def scalar_one_or_none(self) -> object:
        return self.scalar


class _Connection:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.calls: list[str] = []

    async def execute(self, statement: object, _params: Any = None) -> _Result:
        sql = str(statement)
        self.calls.append(sql)
        if "FROM sms_chunk" in sql:
            return _Result(self.rows)
        return _Result([])


def _chunk() -> ChunkPayload:
    return ChunkPayload(
        chunk_id=3,
        batch_id=8,
        custom_id="c",
        phones=("13800138000",),
        content="通知",
        template_id="",
        sign_name="",
        category="notice",
        status="submitting",
    )


@pytest.mark.asyncio
async def test_submitted_chunk_keeps_phones_and_does_not_release() -> None:
    connection = _Connection(
        [
            {
                "status": "submitted",
                "batch_id": 8,
                "category": "notice",
                "app_id": 7,
                "usage_reservation_id": None,
                "blacklist_check": True,
            }
        ]
    )
    crypto = CryptoService.from_secret_values(
        base64.b64encode(b"k" * 32).decode(),
        base64.b64encode(b"h" * 32).decode(),
    )
    decision = await decide_dispatch(connection, crypto, _chunk())
    assert decision.action is DispatchAction.FROZEN
    assert decision.eligible_phones == ()
    assert decision.excluded_count == 0
    assert not any("INSERT INTO sms_dispatch_exclusion" in sql for sql in connection.calls)
    assert not any("usage_quota_entry" in sql for sql in connection.calls)


@pytest.mark.asyncio
async def test_missing_chunk_fails_closed() -> None:
    connection = _Connection([])
    crypto = CryptoService.from_secret_values(
        base64.b64encode(b"k" * 32).decode(),
        base64.b64encode(b"h" * 32).decode(),
    )
    with pytest.raises(RuntimeError, match="dispatch chunk unavailable"):
        await decide_dispatch(connection, crypto, _chunk())
