"""真实 PostgreSQL CAS 与 Redis Lua 共同验证准入恢复预算。"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.services.metrics_repository import SqlMetricsRepository
from app.services.send_admission import (
    SendAdmissionFacts,
    SendAdmissionGuard,
    SendAdmissionRejected,
)
from app.services.send_admission_repository import SqlSendAdmissionRepository
from tests.integration.outbox_isolation import isolated_outbox

pytestmark = pytest.mark.skipif(
    not {"OUTBOX_POSTGRES_DSN", "AUTH_GUARD_REDIS_URL"}.issubset(os.environ),
    reason="requires isolated PostgreSQL and Redis",
)


@pytest.mark.asyncio
async def test_recovery_hysteresis_cas_reason_changes_share_one_real_budget() -> None:
    """只控制容量事实与服务器秒钟，执行真实状态仓储及原始 Lua。"""
    redis = Redis.from_url(os.environ["AUTH_GUARD_REDIS_URL"], decode_responses=True)
    key = f"admission:recovery:test-{uuid4().hex}"

    class ClockRedis:
        second = 1000

        async def eval(self, script: str, count: int, _key: str, *args: str) -> Any:
            shim = f"""
                local original_call = redis.call
                local redis = {{call = function(cmd, ...)
                  if cmd == 'TIME' then return {{'{self.second}','0'}} end
                  return original_call(cmd, ...)
                end}}
            """
            return await redis.eval(shim + script, count, key, *args)

    clock = ClockRedis()
    url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    settings = cast(Any, SimpleNamespace(database_url=url))
    repository = SqlSendAdmissionRepository(settings, redis=clock)
    other = SqlSendAdmissionRepository(settings, redis=clock)
    payload = SendAdmissionFacts(1200, 0, 0, 0, 0, False, False, 0)

    async def load() -> SendAdmissionFacts:
        return payload

    async def record(**_values: Any) -> None:
        pass

    try:
        async with isolated_outbox(url, repository._engine()) as (engine, scoped):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        f'CREATE TABLE "{scoped.schema}".send_admission_state '
                        "(LIKE public.send_admission_state INCLUDING ALL)"
                    )
                )
                await connection.execute(
                    text("""
                    INSERT INTO send_admission_state(
                      scope,state,reason_code,state_epoch,valid_until)
                    VALUES('send','closed','outbox_backlog',1,now()-interval '1 second')
                """)
                )
                await connection.execute(text(
                    "GRANT SELECT (scope,state,state_epoch,hold_until,valid_until) "
                    "ON send_admission_state TO sms_metrics"
                ))
            for repo in (repository, other):
                repo._engine = lambda: engine  # type: ignore[method-assign]
                repo.load = load  # type: ignore[method-assign]
                repo.record_transition = record  # type: ignore[method-assign]
            guard = SendAdmissionGuard(repository)
            snap = await guard.snapshot()
            assert (snap.state, snap.reason, snap.budget_mode, snap.hold_until) == (
                "closed",
                "capacity_hysteresis",
                "closed",
                None,
            )
            with pytest.raises(SendAdmissionRejected):
                await guard.authorize(category="notice", channel="api", recipient_count=1)

            class MetricsEngine:
                @asynccontextmanager
                async def connect(self):
                    async with engine.begin() as connection:
                        await connection.execute(text("SET LOCAL ROLE sms_metrics"))
                        yield connection

            metrics = SqlMetricsRepository(settings)
            metrics._engine = MetricsEngine  # type: ignore[method-assign]
            assert (await metrics.load()).send_admission_state == "closed"

            # CLOSED 退出后保留定时保持；同秒一次耗尽预算。
            payload = replace(payload, outbox_active=120)
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE send_admission_state SET valid_until=now()-interval '1 second'")
                )
            first = SendAdmissionGuard(repository)
            initial = await first.snapshot()
            assert initial.reason == "recovery_hold" and initial.hold_until is not None
            assert initial.budget_mode == "recovery"
            await first.authorize(
                category="notice",
                channel="api",
                recipient_count=20,
                estimated_segments=20,
                estimated_chunks=1,
            )

            # 原因改为积压但 hold 仍在，不补额度，也不绕过单请求成本限制。
            payload = replace(payload, outbox_active=220)
            changed = SendAdmissionGuard(other)
            updated = await changed.snapshot()
            assert updated.reason == "outbox_backlog"
            assert updated.hold_until == initial.hold_until
            assert updated.state_epoch > initial.state_epoch
            assert updated.budget_mode == "recovery"
            with pytest.raises(SendAdmissionRejected) as error:
                await changed.authorize(
                    category="notice",
                    channel="api",
                    recipient_count=1,
                    estimated_segments=21,
                    estimated_chunks=1,
                )
            assert error.value.reason == "recovery_segment_cost"
            with pytest.raises(SendAdmissionRejected) as error:
                await changed.authorize(
                    category="notice", channel="api", recipient_count=1, estimated_segments=1
                )
            assert error.value.reason == "recovery_rate"
            # 即使新版本的第一次扣款被拒，也推进围栏；旧缓存下一秒不可消费。
            clock.second += 1
            with pytest.raises(SendAdmissionRejected) as error:
                await first.authorize(
                    category="notice", channel="api", recipient_count=1, estimated_segments=1
                )
            assert error.value.reason == "recovery_rate"

            # 两个 API 并发只获得五个批次额度。
            peer = SendAdmissionGuard(repository)

            async def accept(i: int) -> bool:
                try:
                    await (peer if i % 2 else changed).authorize(
                        category="notice",
                        channel="api",
                        recipient_count=1,
                        estimated_segments=1,
                    )
                    return True
                except SendAdmissionRejected as rejected:
                    assert rejected.reason == "recovery_rate"
                    return False

            assert sum(await asyncio.gather(*(accept(i) for i in range(20)))) == 5

            # 期限结束后仍在容量滞回：清除时间但保留恢复预算。
            payload = replace(payload, outbox_active=120)
            async with engine.begin() as connection:
                await connection.execute(
                    text("""
                    UPDATE send_admission_state SET hold_until=now()-interval '1 second',
                      valid_until=now()-interval '1 second'
                """)
                )
            hysteresis = await SendAdmissionGuard(repository).snapshot()
            assert (hysteresis.reason, hysteresis.hold_until, hysteresis.budget_mode) == (
                "capacity_hysteresis",
                None,
                "recovery",
            )
            assert (await metrics.load()).send_admission_state == "degraded"
            # 过期滞回不重新启动 60s；新事实低于退出阈值即可 OPEN。
            payload = replace(payload, outbox_active=50)
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE send_admission_state SET valid_until=now()-interval '1 second'")
                )
            assert (await metrics.load()).send_admission_state == "closed"
            opened = await SendAdmissionGuard(repository).snapshot()
            assert (opened.state, opened.budget_mode, opened.hold_until) == ("open", "normal", None)
            assert (await metrics.load()).send_admission_state == "open"
    finally:
        await redis.delete(key)
        await redis.aclose()
