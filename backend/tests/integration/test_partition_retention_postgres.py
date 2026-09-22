from __future__ import annotations

import os
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.runtime_resources import bind_connection_system_audit
from scripts_support.maintain_partitions import maintain

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ, reason="requires isolated migrated PostgreSQL"
)


@pytest.mark.asyncio
async def test_retention_drops_old_message_partition_with_report_projection() -> None:
    engine = create_async_engine(make_url(os.environ["OUTBOX_POSTGRES_DSN"]))
    nonce = uuid4().hex
    moment = datetime(2000, 1, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await bind_connection_system_audit(
                    connection,
                    actor_name="partition-maintenance",
                    action="partition.maintenance",
                    producer_domain="api",
                )
                await connection.execute(
                    text("""
                    CREATE TABLE sms_message_2000_01 PARTITION OF sms_message
                    FOR VALUES FROM ('2000-01-01 00:00:00+08') TO ('2000-02-01 00:00:00+08')
                """)
                )
                batch_id = (
                    await connection.execute(
                        text("""
                    INSERT INTO sms_batch(batch_no,channel,dept,content,display_content_enc,
                                          send_content_enc,status,total)
                    VALUES(:nonce,'web','test','[encrypted]',:cipher,:cipher,'completed',1)
                    RETURNING id
                """),
                        {"nonce": nonce, "cipher": b"synthetic-cipher"},
                    )
                ).scalar_one()
                message_id = (
                    await connection.execute(
                        text("""
                    INSERT INTO sms_message(batch_id,phone_enc,phone_hmac,phone_mask,key_version,
                                              status,created_at)
                    VALUES(:batch,:cipher,:hash,'138****8000',1,'delivered',:moment) RETURNING id
                """),
                        {
                            "batch": batch_id,
                            "cipher": b"synthetic-cipher",
                            "hash": "a" * 64,
                            "moment": moment,
                        },
                    )
                ).scalar_one()
                event_key = nonce * 2
                await connection.execute(
                    text("""
                    INSERT INTO report_event(event_key,vendor_task_id,custom_id,phone_enc,
                      phone_hmac,phone_mask,key_version,report_status,message_status,report_time)
                    VALUES(:key,CAST(:hash AS varchar),CAST(:hash AS varchar),:cipher,
                           CAST(:hash AS char(64)),'138****8000',1,1,'delivered',:moment)
                """),
                    {
                        "key": event_key,
                        "hash": "a" * 64,
                        "cipher": b"synthetic-cipher",
                        "moment": moment,
                    },
                )
                await connection.execute(
                    text("""
                    INSERT INTO report_event_projection(event_key,batch_id,message_id,
                                                        message_created_at,projection_changed)
                    VALUES(:key,:batch,:message,:moment,true)
                """),
                    {"key": event_key, "batch": batch_id, "message": message_id, "moment": moment},
                )
                result = await maintain(connection, future_months=3)
                assert result.dropped >= 1
                assert (
                    await connection.execute(text("SELECT to_regclass('sms_message_2000_01')"))
                ).scalar_one() is None
                # 原始事件是审计事实；只淘汰过期消息的投影关联。
                assert (
                    await connection.execute(
                        text("SELECT count(*) FROM report_event WHERE event_key=:key"),
                        {"key": event_key},
                    )
                ).scalar_one() == 1
                assert (
                    await connection.execute(
                        text("SELECT count(*) FROM report_event_projection WHERE event_key=:key"),
                        {"key": event_key},
                    )
                ).scalar_one() == 0
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
