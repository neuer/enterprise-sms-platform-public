"""真实升级表上的撤销优先级：历史快照缺口不能恢复永久撤销的回调。"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.auth.accounts import SecurityPrincipal
from app.services.callback_batch_event import FinishedRecoveryRejected, FinishedRecoveryService
from app.services.callback_retry_eligibility import diagnosis_sql

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [True, False])
async def test_revocation_wins_over_snapshot_and_recovery(legacy: bool) -> None:
    """即使当前应用和 URL/密钥已恢复，两个恢复入口仍无新增事件或 Outbox。"""
    engine = create_async_engine(make_url(os.environ["OUTBOX_POSTGRES_DSN"]))
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                nonce = uuid4().hex
                params = {"nonce": nonce, "digest": nonce * 2, "sealed": b"\x00\x01" + b"x" * 32}
                app_id = await connection.scalar(
                    text("""
                    INSERT INTO app(name,dept,api_key_hash,api_key_prefix,created_by,
                                    callback_url,callback_secret_enc)
                    VALUES(:nonce,'synthetic',:digest,'revoked','test',
                           'https://callback.invalid',:sealed) RETURNING id
                """),
                    params,
                )
                batch_id = await connection.scalar(
                    text("""
                    INSERT INTO sms_batch(batch_no,channel,app_id,dept,
                        display_content_enc,send_content_enc,status)
                    VALUES(:nonce,'api',:app,'synthetic',:sealed,:sealed,'completed') RETURNING id
                """),
                    {**params, "app": app_id},
                )
                task_id = await connection.scalar(
                    text("""
                    INSERT INTO callback_task(app_id,event,batch_id,url,callback_secret_enc,
                        callback_secret_key_version,status,last_error)
                    VALUES(:app,'batch.finished',:batch,'https://callback.invalid',:sealed,
                        1,'dead','CallbackConfigRevoked') RETURNING id
                """),
                    {**params, "app": app_id, "batch": batch_id},
                )
                await connection.execute(
                    text("""
                    INSERT INTO callback_batch_event(task_id,batch_id,batch_no,category,
                        status,total,delivered,failed,unknown_cnt,manual_resolution_required,
                        finished_at,legacy_unavailable)
                    VALUES(:task,:batch,:nonce,'notice',:status,:count,:count,:count,:count,
                           :flag,now(),:legacy)
                """),
                    {
                        "task": task_id,
                        "batch": batch_id,
                        "nonce": nonce,
                        "legacy": legacy,
                        "status": None if legacy else "completed",
                        "count": None if legacy else 0,
                        "flag": None if legacy else False,
                    },
                )
                reason = await connection.scalar(text(diagnosis_sql()), {"task_id": task_id})
                assert reason == "config_revoked"
                before = await connection.scalar(text("SELECT count(*) FROM callback_task"))
                before_outbox = await connection.scalar(text("SELECT count(*) FROM outbox_event"))
                with pytest.raises(FinishedRecoveryRejected) as blocked:
                    await FinishedRecoveryService()._recover(
                        connection,
                        int(task_id),
                        SecurityPrincipal(1, 10, "admin", "平台部", "admin"),
                    )
                assert blocked.value.reason == "config_revoked"
                assert await connection.scalar(text("SELECT count(*) FROM callback_task")) == before
                assert (
                    await connection.scalar(text("SELECT count(*) FROM outbox_event"))
                    == before_outbox
                )
                assert (
                    await connection.scalar(
                        text("""
                    SELECT count(*) FROM callback_batch_event WHERE recovered_from_task_id=:task
                """),
                        {"task": task_id},
                    )
                    == 0
                )
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
