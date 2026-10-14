"""合法业务等待与真实处理积压使用不同时间锚点。"""

from __future__ import annotations

import importlib
import os
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.services.callback_repository import SqlCallbackRepository
from app.services.callback_worker import RETRY_DELAYS_S
from app.services.outbox import (
    OUTBOX_CAPACITY_AGE_ELIGIBLE_SQL,
    OUTBOX_CAPACITY_AGE_SQL,
    OutboxEventSpec,
)
from app.services.outbox_repository import SqlOutboxRepository, enqueue_outbox
from app.services.send_admission import decide
from app.services.send_admission_repository import SqlSendAdmissionRepository
from tests.integration.outbox_isolation import isolated_outbox

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


@pytest.mark.asyncio
@pytest.mark.parametrize(("retry_count", "delay_s"), list(enumerate(RETRY_DELAYS_S)))
async def test_callback_planned_retry_is_counted_but_not_aged_before_due(
    monkeypatch: pytest.MonkeyPatch,
    retry_count: int,
    delay_s: int,
) -> None:
    """通过真实 mark_retry 生产事件，长退避不触发最老积压误报。"""

    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    settings = cast(Any, SimpleNamespace(database_url=database_url, is_production=False))
    outbox = SqlOutboxRepository(settings)
    callback = SqlCallbackRepository(settings)
    async with isolated_outbox(database_url, outbox._engine()) as (engine, scoped):
        outbox._engine = lambda: scoped  # type: ignore[method-assign]
        callback._engine = lambda: engine  # type: ignore[method-assign]
        app_id: int | None = None
        task_id: int | None = None
        lease = uuid4()
        try:
            async with engine.begin() as connection:
                app_id = await connection.scalar(
                    text(
                        "INSERT INTO app(name,dept,api_key_hash,api_key_prefix,created_by) "
                        "VALUES(:name,'synthetic',:digest,'capacity','test') RETURNING id"
                    ),
                    {"name": f"capacity-{uuid4().hex}", "digest": "a" * 64},
                )
                task_id = await connection.scalar(
                    text("""
                    INSERT INTO callback_task(
                      app_id,event,url,callback_secret_enc,callback_secret_key_version,
                      signature_version,status,retry_count,lease_id,lease_expires_at
                    ) VALUES(
                      :app,'batch.finished','https://callback.invalid',:sealed,1,1,
                      'retrying',:retry_count,:lease,now()+interval '30 seconds'
                    ) RETURNING id
                """),
                    {
                        "app": app_id,
                        "sealed": b"\x00\x01" + b"x" * 30,
                        "lease": lease,
                        "retry_count": retry_count,
                    },
                )
            assert isinstance(task_id, int)
            await callback.mark_retry(
                task_id,
                lease,
                retry_count=retry_count,
                delay_s=delay_s,
                http_code=503,
                error="SyntheticDeliveryFailure",
            )
            # 真实旧行升级：只有完整业务证明才补锚点，基础设施失败不得获豁免。
            migration = importlib.import_module("migrations.versions.0125_outbox_capacity_clock")
            statements: list[str] = []
            monkeypatch.setattr(migration, "op", SimpleNamespace(execute=statements.append))
            migration.upgrade()
            async with engine.begin() as connection:
                expected = await connection.scalar(
                    text("SELECT capacity_eligible_at FROM outbox_event")
                )
                await connection.execute(text("UPDATE outbox_event SET capacity_eligible_at=NULL"))
                for statement in statements:
                    await connection.execute(text(statement))
                assert (
                    await connection.scalar(text("SELECT capacity_eligible_at FROM outbox_event"))
                    == expected
                )
                await connection.execute(
                    text("UPDATE outbox_event SET capacity_eligible_at=NULL,failure_count=1")
                )
                for statement in statements:
                    await connection.execute(text(statement))
                assert (
                    await connection.scalar(text("SELECT capacity_eligible_at FROM outbox_event"))
                    is None
                )
                await connection.execute(
                    text("UPDATE outbox_event SET capacity_eligible_at=:anchor,failure_count=0"),
                    {"anchor": expected},
                )
            async with engine.begin() as connection:
                # 同步平移所有时间，模拟已等待但尚未到期，不缩短合同退避长度。
                await connection.execute(
                    text("""
                    UPDATE outbox_event SET
                      created_at=created_at-make_interval(secs=>:elapsed),
                      capacity_eligible_at=capacity_eligible_at-make_interval(secs=>:elapsed),
                      next_attempt_at=next_attempt_at-make_interval(secs=>:elapsed)
                """),
                    {"elapsed": min(601, delay_s // 2)},
                )
            assert await outbox.lease_due(limit=1, lease_seconds=30) == []
            stats = await outbox.stats()
            assert stats.pending == 1
            assert stats.oldest_age_seconds == 0
            async with engine.begin() as connection:
                original = (
                    await connection.execute(
                        text("SELECT id,capacity_eligible_at,next_attempt_at FROM outbox_event")
                    )
                ).one()
                assert original.capacity_eligible_at == original.next_attempt_at
                repeated = await enqueue_outbox(
                    connection,
                    OutboxEventSpec(
                        "callback.ready",
                        "callback_task",
                        str(task_id),
                        "app.tasks.deliver_callback",
                        "callback",
                        (task_id,),
                        f"callback:{task_id}:attempt:{retry_count + 1}",
                    ),
                    available_delay_seconds=delay_s,
                    capacity_wait="callback_retry",
                )
                assert repeated == original.id
                assert (
                    await connection.scalar(
                        text("SELECT capacity_eligible_at FROM outbox_event WHERE id=:id"),
                        {"id": repeated},
                    )
                    == original.capacity_eligible_at
                )

            class NeutralControl:
                async def mget(self, *keys: str) -> list[None]:
                    return [None for _ in keys]

            admission = SqlSendAdmissionRepository(settings, redis=NeutralControl())
            admission._engine = lambda: scoped  # type: ignore[method-assign]
            facts = await admission.load()
            assert (facts.outbox_active, facts.outbox_oldest_age_s) == (1, 0)
            assert decide(facts, category="notice", recipient_count=1).allowed

            # 合法等待结束后故意不处理；真实积压及后续基础设施退避仍持续计龄。
            async with engine.begin() as connection:
                await connection.execute(
                    text("""
                    UPDATE outbox_event SET created_at=now()-make_interval(secs=>:age),
                      capacity_eligible_at=now()-interval '601 seconds',
                      next_attempt_at=now()-interval '601 seconds'
                """),
                    {"age": delay_s + 601},
                )
            due = await outbox.stats()
            assert due.oldest_age_seconds >= 601
            blocked = await admission.load()
            assert not decide(blocked, category="notice", recipient_count=1).allowed
            acquired = (await outbox.lease_due(limit=1, lease_seconds=30))[0]
            await outbox.mark_publish_failed(acquired.event_id, acquired.lease_id, "BrokerDown")
            assert await outbox.lease_due(limit=1, lease_seconds=30) == []
            assert (await outbox.stats()).oldest_age_seconds >= due.oldest_age_seconds
            async with engine.begin() as connection:
                await connection.execute(text("UPDATE outbox_event SET next_attempt_at=now()"))
            acquired = (await outbox.lease_due(limit=1, lease_seconds=30))[0]
            await outbox.mark_published(acquired.event_id, acquired.lease_id)
            claim = await outbox.claim_execution(acquired.event_id, lease_seconds=30)
            assert claim is not None
            await outbox.fail_execution(claim.event_id, claim.lease_id, "WorkerFailed")
            assert (await outbox.stats()).oldest_age_seconds >= due.oldest_age_seconds
            async with engine.begin() as connection:
                await connection.execute(text("UPDATE outbox_event SET next_attempt_at=now()"))
            expired = (await outbox.lease_due(limit=1, lease_seconds=30))[0]
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE outbox_event SET lease_expires_at=now()-interval '1 second'")
                )
            recovered = (await outbox.lease_due(limit=1, lease_seconds=30))[0]
            assert recovered.lease_id != expired.lease_id
            assert (await outbox.stats()).oldest_age_seconds >= due.oldest_age_seconds

            # metrics 仍只读，新增锚点授权足够执行真实聚合。
            async with engine.begin() as connection:
                await connection.execute(text("SET LOCAL ROLE sms_metrics"))
                age = await connection.scalar(
                    text(
                        f"SELECT EXTRACT(EPOCH FROM(now()-min({OUTBOX_CAPACITY_AGE_SQL}))) "
                        f"FROM outbox_event WHERE {OUTBOX_CAPACITY_AGE_ELIGIBLE_SQL}"
                    )
                )
                assert age >= 601
            async with engine.begin() as connection:
                await connection.execute(
                    text("""
                    UPDATE outbox_event SET capacity_eligible_at=NULL,
                      next_attempt_at=now()+interval '1 hour'
                """)
                )
            assert (await outbox.stats()).oldest_age_seconds >= delay_s + 601
        finally:
            async with engine.begin() as connection:
                if task_id is not None:
                    await connection.execute(
                        text("DELETE FROM callback_task WHERE id=:id"), {"id": task_id}
                    )
                if app_id is not None:
                    await connection.execute(text("DELETE FROM app WHERE id=:id"), {"id": app_id})
