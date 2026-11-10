"""真实 PostgreSQL 验证无主报告查询和导出以报告时间而非入库时间过滤。"""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.services.export import ExportFilterSet
from app.services.export_repository import ExportClaim, SqlExportRepository
from app.services.ops import UnmatchedQuery
from app.services.ops_repository import SqlOpsRepository
from tests.integration.test_raw_replay_accept_matrix_postgres import accept_world as accept_world

pytestmark = pytest.mark.skipif(
    not {"SECURITY_SESSION_POSTGRES_DSN", "OUTBOX_POSTGRES_DSN"}.issubset(os.environ),
    reason="requires isolated migrated PostgreSQL",
)


@pytest.mark.asyncio
async def test_unmatched_list_and_export_share_report_time_boundaries(accept_world: Any) -> None:
    world = accept_world
    anchor = datetime.fromisoformat("2026-07-15T10:00:00+08:00")
    phone = f"19{int(world.nonce[:8], 16) % 1_000_000_000:09d}"
    protected = world.crypto.protect_phone(phone, table="unmatched_report")
    times = {
        "before": anchor - timedelta(seconds=1),
        "start": anchor,
        "middle": anchor + timedelta(seconds=1),
        "end": anchor + timedelta(seconds=2),
        "after": anchor + timedelta(seconds=3),
        "legacy-no-time": None,
    }
    async with world.owner.begin() as connection:
        for label, moment in times.items():
            event_key = hashlib.sha256(f"{world.nonce}:{label}".encode()).hexdigest()
            values = {
                "key": event_key,
                "vendor": event_key,
                "custom": event_key,
                "enc": protected.phone_enc,
                "hmac": protected.phone_hmac,
                "mask": protected.phone_mask,
                "version": protected.key_version,
                "label": label,
                "event_time": moment or anchor,
                "report_time": moment,
                "created_at": anchor + timedelta(days=2),
            }
            await connection.execute(
                text("""
                    INSERT INTO report_event(
                      event_key,vendor_task_id,custom_id,phone_enc,phone_hmac,phone_mask,
                      key_version,report_status,message_status,report_desc,report_time
                    ) VALUES (:key,:vendor,:custom,:enc,:hmac,:mask,:version,1,'delivered',
                              :label,:event_time)
                """),
                values,
            )
            await connection.execute(
                text("""
                    INSERT INTO unmatched_report(
                      event_key,vendor_task_id,custom_id,phone_enc,phone_hmac,phone_mask,
                      key_version,report_status,report_desc,report_time,created_at
                    ) VALUES (:key,:vendor,:custom,:enc,:hmac,:mask,:version,1,:label,
                              :report_time,:created_at)
                """),
                values,
            )
    ops = SqlOpsRepository(world.settings, redis=object())
    # 查询语义用真实 PostgreSQL；运行角色边界另由现有导出授权集成套件验证。
    exports = SqlExportRepository(
        SimpleNamespace(database_url_for=lambda _: world.owner.url)  # type: ignore[arg-type]
    )
    hashes = (protected.phone_hmac,)
    cases = [
        (anchor, anchor + timedelta(seconds=2), {"start", "middle", "end"}),
        (anchor, anchor, {"start"}),
        (
            anchor.astimezone(UTC),
            (anchor + timedelta(seconds=2)).astimezone(UTC),
            {"start", "middle", "end"},
        ),
        (None, None, set(times)),
    ]
    for start, end, expected in cases:
        first = await ops.list_unmatched(UnmatchedQuery(hashes, start, end, 1, 2))
        actual = {item.report_desc for item in first.items}
        for page in range(2, (first.total + 1) // 2 + 1):
            result = await ops.list_unmatched(UnmatchedQuery(hashes, start, end, page, 2))
            actual.update(item.report_desc for item in result.items)
        assert first.total == len(expected) and actual == expected
        filters = ExportFilterSet(
            start, end, None, None, None, None, hashes, None, dataset="unmatched"
        )
        assert await exports.count_rows(filters) == len(expected)
        claim = ExportClaim(1, filters, False, uuid4(), anchor + timedelta(days=3))
        rows = [row async for row in exports.rows(claim)]
        assert {row["report_desc"] for row in rows} == expected
        assert len(rows) == len(expected)
    exclusive = replace(
        filters, start=anchor, end=None, end_exclusive=anchor + timedelta(seconds=2)
    )
    assert await exports.count_rows(exclusive) == 2
    rows = [row async for row in exports.rows(replace(claim, filters=exclusive))]
    assert {row["report_desc"] for row in rows} == {"start", "middle"}
