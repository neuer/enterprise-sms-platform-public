"""轮询原因计数由 sms_send 写、sms_metrics 读，不新增权限。"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.redis_lock import LEASE_FAILURE_REASONS
from app.services.poll_lease_events import SqlPollLeaseEvents

pytestmark = pytest.mark.skipif(
    "SECURITY_SESSION_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


@pytest.mark.asyncio
async def test_all_fixed_reasons_persist_under_runtime_role_and_are_countable(monkeypatch):
    engine = create_async_engine(make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"]))
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                @asynccontextmanager
                async def begin():
                    await connection.execute(text("SET LOCAL ROLE sms_send"))
                    try:
                        yield connection
                    finally:
                        await connection.execute(text("RESET ROLE"))

                monkeypatch.setattr(
                    "app.services.poll_lease_events.database_engine",
                    lambda _: SimpleNamespace(begin=begin),
                )
                recorder = SqlPollLeaseEvents(SimpleNamespace(database_url=None))
                for source in ("report", "reply"):
                    for reason in LEASE_FAILURE_REASONS:
                        await recorder.record(source, reason)
                with pytest.raises(ValueError):
                    await recorder.record("free-form-source", "free-form-reason")
                await connection.execute(text("SET LOCAL ROLE sms_metrics"))
                rows = (await connection.execute(text("""
                    SELECT task_kind,event_type,count(event_type)
                    FROM worker_lease_event WHERE task_kind IN ('poll_report','poll_reply')
                    GROUP BY task_kind,event_type
                """))).all()
                assert {(kind, event) for kind, event, _ in rows} == {
                    (f"poll_{source}", reason)
                    for source in ("report", "reply") for reason in LEASE_FAILURE_REASONS
                }
                assert all(count >= 1 for _, _, count in rows)
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
