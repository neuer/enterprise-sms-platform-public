from __future__ import annotations

import os
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.services.send_admission import SendAdmissionFacts, decide
from app.services.send_admission_repository import SqlSendAdmissionRepository

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


def _lane_facts(loaded: SendAdmissionFacts) -> SendAdmissionFacts:
    return SendAdmissionFacts(
        outbox_active=0,
        outbox_oldest_age_s=0,
        outbox_dead=0,
        uncertain_overdue=0,
        callback_dead=0,
        realtime_paused=False,
        bulk_paused=False,
        vendor_failures=0,
        realtime_heartbeat_stale=loaded.realtime_heartbeat_stale,
        bulk_heartbeat_stale=loaded.bulk_heartbeat_stale,
        dispatcher_heartbeat_stale=loaded.dispatcher_heartbeat_stale,
    )


class _Redis:
    async def mget(self, *keys: str) -> list[object]:
        del keys
        return [None, None, "0"]


async def _set_leases(engine: Any, *, realtime: bool, bulk: bool, dispatcher: bool) -> None:
    leases = {
        "send-realtime": realtime,
        "send-bulk": bulk,
        "outbox-dispatcher": dispatcher,
    }
    async with engine.begin() as connection:
        for component, fresh in leases.items():
            await connection.execute(
                text(
                    """
                    INSERT INTO send_runtime_heartbeat (
                      component, generation, last_heartbeat_at, lease_until
                    ) VALUES (
                      :component, 1, now(),
                      now() + make_interval(secs => :lease_secs)
                    )
                    ON CONFLICT (component) DO UPDATE SET
                      generation = send_runtime_heartbeat.generation + 1,
                      last_heartbeat_at = now(),
                      lease_until = now() + make_interval(secs => :lease_secs)
                    """
                ),
                {"component": component, "lease_secs": 30 if fresh else -1},
            )


@pytest.mark.asyncio
async def test_bulk_heartbeat_stale_keeps_notice_open() -> None:
    engine = create_async_engine(make_url(os.environ["OUTBOX_POSTGRES_DSN"]))
    repository = SqlSendAdmissionRepository(
        settings=type(
            "S",
            (),
            {
                "database_url": os.environ["OUTBOX_POSTGRES_DSN"],
                "redis_control_url": "redis://localhost/0",
                "is_production": True,
            },
        )(),
        redis=_Redis(),
    )
    repository._engine = lambda: engine  # type: ignore[method-assign]
    try:
        await _set_leases(engine, realtime=True, bulk=False, dispatcher=True)
        bulk_stale = _lane_facts(await repository.load())
        assert bulk_stale.bulk_heartbeat_stale is True
        assert bulk_stale.realtime_heartbeat_stale is False
        assert bulk_stale.dispatcher_heartbeat_stale is False
        notice = decide(bulk_stale, category="notice", recipient_count=1)
        assert notice.allowed is True
        market = decide(bulk_stale, category="market", recipient_count=1)
        assert market.allowed is False
        assert market.reason == "bulk_heartbeat_stale"

        await _set_leases(engine, realtime=False, bulk=True, dispatcher=True)
        realtime_stale = _lane_facts(await repository.load())
        assert decide(realtime_stale, category="market", recipient_count=1).allowed is True
        verify = decide(realtime_stale, category="verify", recipient_count=1)
        assert verify.allowed is False
        assert verify.reason == "realtime_heartbeat_stale"

        await _set_leases(engine, realtime=True, bulk=True, dispatcher=False)
        dispatcher_stale = _lane_facts(await repository.load())
        closed = decide(dispatcher_stale, category="notice", recipient_count=1)
        assert closed.allowed is False
        assert closed.reason == "dispatcher_heartbeat_stale"
    finally:
        await engine.dispose()
