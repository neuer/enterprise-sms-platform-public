"""真实 PostgreSQL 验证两应用回调隔离及保留的全局硬保护。"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.services.send_admission import SendAdmissionGuard, SendAdmissionRejected, evaluate_capacity
from app.services.send_admission_repository import SqlSendAdmissionRepository
from tests.integration.outbox_isolation import isolated_outbox

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


class NeutralControl:
    async def mget(self, *_keys: str) -> list[None]:
        return [None, None, None]


@pytest.mark.asyncio
async def test_callback_application_isolation_preserves_global_hard_limits() -> None:
    """未来重试与dead关闭所属应用，平台总容量和真实到期年龄仍关闭全部应用。"""

    url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    repository = SqlSendAdmissionRepository(
        cast(Any, SimpleNamespace(database_url=url, is_production=False)), redis=NeutralControl()
    )
    async with isolated_outbox(url, repository._engine()) as (engine, _scoped):
        repository._engine = lambda: engine  # type: ignore[method-assign]
        app_ids: list[int] = []
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        f'CREATE TABLE "{_scoped.schema}".send_admission_state '
                        "(LIKE public.send_admission_state INCLUDING ALL)"
                    )
                )
                await connection.execute(
                    text("""
                    INSERT INTO send_admission_state(scope,state,reason_code,valid_until)
                    VALUES('send','open','ok',now()+interval '15 seconds')
                """)
                )
                for _ in range(2):
                    app_id = await connection.scalar(
                        text("""
                        INSERT INTO app(name,dept,api_key_hash,api_key_prefix,created_by)
                        VALUES(:name,'synthetic',:digest,'isolate','test') RETURNING id
                    """),
                        {"name": f"callback-isolate-{uuid4().hex}", "digest": uuid4().hex * 2},
                    )
                    assert isinstance(app_id, int)
                    app_ids.append(app_id)
                await connection.execute(
                    text("""
                    INSERT INTO callback_task(app_id,event,url,callback_secret_enc,
                      callback_secret_key_version,status,next_retry_at)
                    SELECT :app,'batch.finished','https://callback.invalid',:sealed,
                      1,'retrying',now()+interval '1 hour'
                    FROM generate_series(1,200)
                """),
                    {"app": app_ids[0], "sealed": b"\x00\x01" + b"x" * 30},
                )
                await connection.execute(
                    text("""
                    INSERT INTO outbox_event(id,dedup_key,event_type,aggregate_type,aggregate_id,
                      task_name,queue,args,next_attempt_at,capacity_eligible_at)
                    SELECT gen_random_uuid(),'callback:'||id||':attempt:1','callback.ready',
                      'callback_task',id::text,'app.tasks.deliver_callback','callback',
                      jsonb_build_array(id),now()+interval '1 hour',now()+interval '1 hour'
                    FROM callback_task WHERE app_id=:app
                """),
                    {"app": app_ids[0]},
                )
            facts = await repository.load()
            assert (facts.outbox_active, facts.outbox_shared_active, facts.outbox_oldest_age_s) == (
                200,
                0,
                0,
            )
            guard = SendAdmissionGuard(repository)
            for category in ("notice", "verify", "market"):
                with pytest.raises(SendAdmissionRejected) as rejection:
                    await guard.authorize(
                        app_id=app_ids[0], category=category, channel="api", recipient_count=1
                    )
                assert rejection.value.reason == "app_callback_backlog"
                await guard.authorize(
                    app_id=app_ids[1], category=category, channel="api", recipient_count=1
                )
            async with engine.begin() as connection:
                assert await connection.scalar(text("SELECT count(*) FROM outbox_event")) == 200
                await connection.execute(
                    text("""
                    UPDATE callback_task SET status='done' WHERE app_id=:app
                """),
                    {"app": app_ids[0]},
                )
                await connection.execute(
                    text("""
                    UPDATE callback_task SET status='dead' WHERE id IN
                    (SELECT id FROM callback_task WHERE app_id=:app ORDER BY id LIMIT 20)
                """),
                    {"app": app_ids[0]},
                )
            guard = SendAdmissionGuard(repository)
            with pytest.raises(SendAdmissionRejected) as rejection:
                await guard.authorize(
                    app_id=app_ids[0], category="notice", channel="api", recipient_count=1
                )
            assert rejection.value.reason == "app_callback_backlog"
            await guard.authorize(
                app_id=app_ids[1], category="notice", channel="api", recipient_count=1
            )
            # 所有事实保留，人工恢复/完成一条后应用门允许重新进入原全局检查。
            async with engine.begin() as connection:
                await connection.execute(
                    text("""
                    UPDATE callback_task SET status='done' WHERE id=
                    (SELECT min(id) FROM callback_task WHERE app_id=:app AND status='dead')
                """),
                    {"app": app_ids[0]},
                )
            await SendAdmissionGuard(repository).authorize(
                app_id=app_ids[0], category="notice", channel="api", recipient_count=1
            )
            # 同一应用积累至全局dead硬上限后，健康应用也必须关闭。
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE callback_task SET status='dead' WHERE app_id=:app"),
                    {"app": app_ids[0]},
                )
            assert evaluate_capacity(await repository.load()).state == "closed"
            with pytest.raises(SendAdmissionRejected):
                await SendAdmissionGuard(repository).authorize(
                    app_id=app_ids[1], category="notice", channel="api", recipient_count=1
                )
            # 到期锚点持续计龄，不能把远端重试隔离扩成基础设施年龄豁免。
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE callback_task SET status='done' WHERE app_id=:app"),
                    {"app": app_ids[0]},
                )
                await connection.execute(
                    text("""
                    UPDATE outbox_event SET created_at=now()-interval '2 hours',
                      capacity_eligible_at=now()-interval '601 seconds',
                      next_attempt_at=now()+interval '1 hour'
                """)
                )
            facts = await repository.load()
            assert facts.outbox_oldest_age_s >= 601
            assert evaluate_capacity(facts).capacity_reason == "outbox_oldest"
            # 保持数量硬上限；2000个未来callback不会因软保护排除而放行。
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE outbox_event SET capacity_eligible_at=now()+interval '1 hour'")
                )
                await connection.execute(
                    text("""
                    INSERT INTO outbox_event(id,dedup_key,event_type,aggregate_type,aggregate_id,
                      task_name,queue,args,next_attempt_at,capacity_eligible_at)
                    SELECT gen_random_uuid(),'callback:synthetic:'||n,'callback.ready',
                      'callback_task','1','app.tasks.deliver_callback','callback',
                      '[1]'::jsonb,now()+interval '1 hour',now()+interval '1 hour'
                    FROM generate_series(1,1800) n
                """)
                )
            facts = await repository.load()
            assert (facts.outbox_active, facts.outbox_shared_active) == (2000, 0)
            assert evaluate_capacity(facts).capacity_reason == "outbox_backlog"
            async with engine.begin() as connection:
                assert (
                    await connection.scalar(
                        text("SELECT count(*) FROM callback_task WHERE app_id=:app"),
                        {"app": app_ids[0]},
                    )
                    == 200
                )
        finally:
            async with engine.begin() as connection:
                for app_id in app_ids:
                    await connection.execute(
                        text("DELETE FROM callback_task WHERE app_id=:id"), {"id": app_id}
                    )
                    await connection.execute(text("DELETE FROM app WHERE id=:id"), {"id": app_id})
