from __future__ import annotations

import asyncio
import base64
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.runtime_resources import bind_connection_system_audit
from app.services.crypto import CryptoService
from app.services.reply_ingest import ReplyIngestService
from app.services.reply_repository import SqlReplyRepository
from app.services.report_ingest import ProtectedReport, ReportApplyResult
from app.services.report_repository import SqlReportRepository
from scripts_support.maintain_partitions import maintain

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


def _crypto(*, rotated: bool = False) -> CryptoService:
    first = base64.b64encode(b"e" * 32).decode()
    if not rotated:
        return CryptoService.from_secret_values(first, first)
    second = base64.b64encode(b"n" * 32).decode()
    ring = json.dumps({"active_version": 2, "keys": {"1": first, "2": second}})
    return CryptoService.from_secret_values(ring, ring)


def _report(
    crypto: CryptoService,
    *,
    event_key: str,
    custom_id: str,
    status: int,
    event_time: datetime,
) -> ProtectedReport:
    protected = crypto.protect_phone("13800138000")
    states = {0: "unknown", 1: "delivered", 2: "failed", 99: "failed"}
    return ProtectedReport(
        event_key=event_key,
        vendor_task_id=f"vendor-{custom_id}",
        custom_id=custom_id,
        phone_enc=protected.phone_enc,
        phone_hmac=protected.phone_hmac,
        phone_mask=protected.phone_mask,
        key_version=protected.key_version,
        report_status=status,
        message_status=states.get(status, "other"),
        report_desc=f"status-{status}",
        report_time=event_time,
        phone_hmacs=tuple(crypto.hmac_candidates("13800138000").values()),
    )


@pytest.mark.asyncio
async def test_report_projection_is_monotonic_and_reply_dedup_survives_rotation() -> None:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    settings = cast(Any, SimpleNamespace(database_url=database_url))
    engine = create_async_engine(database_url)
    reports = SqlReportRepository(settings)
    replies = SqlReplyRepository(settings)
    nonce = uuid4().hex
    event_keys: set[str] = set()
    reply_keys: set[str] = set()
    raw_ids: list[int] = []
    batch_ids: list[int] = []
    chunk_ids: list[int] = []
    message_refs: list[tuple[int, datetime]] = []
    app_id: int | None = None
    base_time = datetime(2026, 7, 15, 8, 0, tzinfo=UTC)
    protected = _crypto().protect_phone("13800138000")

    # The isolated CI database is created from the static migration snapshot;
    # keep this integration test deterministic across month rollovers by using
    # the same owner-scoped partition maintenance entrypoint as production.
    async with engine.begin() as connection:
        await bind_connection_system_audit(
            connection,
            actor_name="partition-maintenance",
            action="partition.maintenance",
            producer_domain="api",
        )
        await maintain(connection, future_months=3)

    async def create_message(index: int) -> tuple[int, str, datetime]:
        custom_id = f"{nonce[:24]}{index:08d}"
        created_at = base_time + timedelta(minutes=index)
        async with engine.begin() as connection:
            batch_id = int(
                (
                    await connection.execute(
                        text(
                            """
                            INSERT INTO sms_batch(
                              batch_no,channel,app_id,dept,content,
                              display_content_enc,send_content_enc,status,total
                            ) VALUES(
                              :batch_no,'api',:app_id,'平台部','[encrypted]',
                              :content_enc,:content_enc,'sending',1
                            ) RETURNING id
                            """
                        ),
                        {
                            "batch_no": f"{nonce[:24]}{index:08d}",
                            "app_id": app_id,
                            "content_enc": b"cipher-content",
                        },
                    )
                ).scalar_one()
            )
            chunk_id = int(
                (
                    await connection.execute(
                        text(
                            """
                            INSERT INTO sms_chunk(
                              batch_id,chunk_no,custom_id,vendor_task_id,
                              phone_count,status,submitted_at
                            ) VALUES(
                              :batch_id,1,:custom_id,:vendor_task_id,
                              1,'submitted',:submitted_at
                            ) RETURNING id
                            """
                        ),
                        {
                            "batch_id": batch_id,
                            "custom_id": custom_id,
                            "vendor_task_id": f"vendor-{custom_id}",
                            "submitted_at": created_at,
                        },
                    )
                ).scalar_one()
            )
            message_id = int(
                (
                    await connection.execute(
                        text(
                            """
                            INSERT INTO sms_message(
                              batch_id,chunk_id,phone_enc,phone_hmac,phone_mask,
                              key_version,status,created_at
                            ) VALUES(
                              :batch_id,:chunk_id,:phone_enc,:phone_hmac,:phone_mask,
                              :key_version,'sent',:created_at
                            ) RETURNING id
                            """
                        ),
                        {
                            "batch_id": batch_id,
                            "chunk_id": chunk_id,
                            "phone_enc": protected.phone_enc,
                            "phone_hmac": protected.phone_hmac,
                            "phone_mask": protected.phone_mask,
                            "key_version": protected.key_version,
                            "created_at": created_at,
                        },
                    )
                ).scalar_one()
            )
        batch_ids.append(batch_id)
        chunk_ids.append(chunk_id)
        message_refs.append((message_id, created_at))
        return batch_id, custom_id, created_at

    async def raw(source: str, marker: str) -> int:
        async with engine.begin() as connection:
            raw_id = int(
                (
                    await connection.execute(
                        text(
                            """
                            INSERT INTO raw_vendor_log(
                              source,payload_enc,payload_sha256,key_version,
                              custom_ids,item_count
                            ) VALUES(
                              :source,:payload_enc,:sha,1,ARRAY[:marker],1
                            ) RETURNING id
                            """
                        ),
                        {
                            "source": source,
                            "payload_enc": f"cipher-{marker}".encode(),
                            "sha": "a" * 64,
                            "marker": marker,
                        },
                    )
                ).scalar_one()
            )
        raw_ids.append(raw_id)
        return raw_id

    try:
        async with engine.begin() as connection:
            app_id = int(
                (
                    await connection.execute(
                        text(
                            """
                            INSERT INTO app(
                              name,dept,api_key_hash,api_key_prefix,
                              callback_url,callback_secret_enc,
                              callback_report_enabled,created_by
                            ) VALUES(
                              :name,'平台部',:api_key_hash,:api_key_prefix,
                              'https://callback.invalid',:secret,true,'test'
                            ) RETURNING id
                            """
                        ),
                        {
                            "name": f"event-facts-{nonce}",
                            "api_key_hash": "b" * 64,
                            "api_key_prefix": nonce[:8],
                            "secret": b"cipher-secret",
                        },
                    )
                ).scalar_one()
            )

        first_batch, first_custom, first_created = await create_message(1)
        delivered_key = "1" * 64
        stale_failed_key = "2" * 64
        event_keys.update((delivered_key, stale_failed_key))
        delivered = _report(
            _crypto(),
            event_key=delivered_key,
            custom_id=first_custom,
            status=1,
            event_time=first_created + timedelta(seconds=20),
        )
        stale_failed = _report(
            _crypto(),
            event_key=stale_failed_key,
            custom_id=first_custom,
            status=2,
            event_time=first_created + timedelta(seconds=10),
        )
        assert await reports.apply_report(
            await raw("report", "delivered"),
            delivered,
        ) == ReportApplyResult(first_batch, True)
        assert await reports.apply_report(
            await raw("report", "stale-failed"),
            stale_failed,
        ) == ReportApplyResult(first_batch, False)

        second_batch, second_custom, second_created = await create_message(2)
        same_failed = _report(
            _crypto(),
            event_key="3" * 64,
            custom_id=second_custom,
            status=2,
            event_time=second_created + timedelta(seconds=10),
        )
        same_delivered = replace(
            same_failed,
            event_key="4" * 64,
            report_status=1,
            message_status="delivered",
            report_desc="status-1",
        )
        event_keys.update((same_failed.event_key, same_delivered.event_key))
        assert (
            await reports.apply_report(await raw("report", "same-failed"), same_failed)
        ) == ReportApplyResult(second_batch, True)
        assert (
            await reports.apply_report(
                await raw("report", "same-delivered"),
                same_delivered,
            )
        ) == ReportApplyResult(second_batch, True)

        third_batch, third_custom, third_created = await create_message(3)
        duplicate = _report(
            _crypto(),
            event_key="5" * 64,
            custom_id=third_custom,
            status=1,
            event_time=third_created + timedelta(seconds=10),
        )
        event_keys.add(duplicate.event_key)
        duplicate_raw = await raw("report", "duplicate")
        concurrent = await asyncio.gather(
            reports.apply_report(duplicate_raw, duplicate),
            reports.apply_report(duplicate_raw, duplicate),
        )
        assert sorted(item.changed for item in concurrent if item is not None) == [
            False,
            True,
        ]
        assert all(
            item is not None and item.batch_id == third_batch for item in concurrent
        )

        reply_item = {
            "taskId": f"vendor-{first_custom}",
            "customId": first_custom,
            "phone": "13800138000",
            "extCode": "01",
            "contents": "TD",
            "replyTime": "2026-07-15T08:30:00+08:00",
        }
        before = ReplyIngestService(None, cast(Any, object()), _crypto())._parse(
            reply_item
        )
        after = ReplyIngestService(
            None,
            cast(Any, object()),
            _crypto(rotated=True),
        )._parse(reply_item | {"replyTime": "2026-07-15T00:30:00Z"})
        assert before.dedup_hash == after.dedup_hash
        reply_keys.add(before.dedup_hash)
        reply_raw_ids = await asyncio.gather(
            raw("reply", "reply-v1"),
            raw("reply", "reply-v2"),
        )
        await asyncio.gather(
            replies.store_reply(reply_raw_ids[0], before),
            replies.store_reply(reply_raw_ids[1], after),
        )

        async with engine.connect() as connection:
            first_state = (
                await connection.execute(
                    text(
                        """
                        SELECT m.status,m.report_status,b.delivered,b.failed
                        FROM sms_message m JOIN sms_batch b ON b.id=m.batch_id
                        WHERE m.id=:message_id AND m.created_at=:created_at
                        """
                    ),
                    {
                        "message_id": message_refs[0][0],
                        "created_at": message_refs[0][1],
                    },
                )
            ).mappings().one()
            second_status = (
                await connection.execute(
                    text(
                        """
                        SELECT status FROM sms_message
                        WHERE id=:message_id AND created_at=:created_at
                        """
                    ),
                    {
                        "message_id": message_refs[1][0],
                        "created_at": message_refs[1][1],
                    },
                )
            ).scalar_one()
            fact_counts = (
                await connection.execute(
                    text(
                        """
                        SELECT
                          (SELECT count(*) FROM report_event
                           WHERE event_key=ANY(CAST(:event_keys AS char(64)[]))) reports,
                          (SELECT count(*) FROM report_event_projection
                           WHERE event_key=ANY(CAST(:event_keys AS char(64)[]))
                             AND projection_changed) changed,
                          (SELECT count(*) FROM callback_report_event
                           WHERE event_key IN (
                             CAST(:delivered_key AS char(64)),
                             CAST(:stale_failed_key AS char(64))
                           )) first_callbacks,
                          (SELECT count(*) FROM reply_event
                           WHERE event_key=ANY(CAST(:reply_keys AS char(64)[]))) replies,
                          (SELECT count(*) FROM sms_reply
                           WHERE event_key=ANY(CAST(:reply_keys AS char(64)[])))
                            reply_projections
                        """
                    ),
                    {
                        "event_keys": list(event_keys),
                        "reply_keys": list(reply_keys),
                        "delivered_key": delivered_key,
                        "stale_failed_key": stale_failed_key,
                    },
                )
            ).mappings().one()

        assert dict(first_state) == {
            "status": "delivered",
            "report_status": 1,
            "delivered": 1,
            "failed": 0,
        }
        assert second_status == "delivered"
        assert dict(fact_counts) == {
            "reports": 5,
            "changed": 4,
            "first_callbacks": 1,
            "replies": 1,
            "reply_projections": 1,
        }
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    DELETE FROM outbox_event
                    WHERE aggregate_type='callback_task'
                      AND aggregate_id IN (
                        SELECT id::text FROM callback_task
                        WHERE batch_id=ANY(CAST(:batch_ids AS bigint[]))
                      )
                    """
                ),
                {"batch_ids": batch_ids or [0]},
            )
            await connection.execute(
                text(
                    "DELETE FROM callback_task "
                    "WHERE batch_id=ANY(CAST(:batch_ids AS bigint[]))"
                ),
                {"batch_ids": batch_ids or [0]},
            )
            await connection.execute(
                text(
                    "DELETE FROM callback_report_event "
                    "WHERE batch_id=ANY(CAST(:batch_ids AS bigint[]))"
                ),
                {"batch_ids": batch_ids or [0]},
            )
            await connection.execute(
                text(
                    "DELETE FROM report_event_projection "
                    "WHERE batch_id=ANY(CAST(:batch_ids AS bigint[]))"
                ),
                {"batch_ids": batch_ids or [0]},
            )
            await connection.execute(
                text(
                    "DELETE FROM sms_reply "
                    "WHERE event_key=ANY(CAST(:reply_keys AS char(64)[]))"
                ),
                {"reply_keys": list(reply_keys) or ["0" * 64]},
            )
            await connection.execute(
                text(
                    "DELETE FROM sms_message "
                    "WHERE batch_id=ANY(CAST(:batch_ids AS bigint[]))"
                ),
                {"batch_ids": batch_ids or [0]},
            )
            await connection.execute(
                text(
                    "DELETE FROM report_event "
                    "WHERE event_key=ANY(CAST(:event_keys AS char(64)[]))"
                ),
                {"event_keys": list(event_keys) or ["0" * 64]},
            )
            await connection.execute(
                text(
                    "DELETE FROM reply_event "
                    "WHERE event_key=ANY(CAST(:reply_keys AS char(64)[]))"
                ),
                {"reply_keys": list(reply_keys) or ["0" * 64]},
            )
            await connection.execute(
                text(
                    "DELETE FROM raw_vendor_log WHERE id=ANY(CAST(:ids AS bigint[]))"
                ),
                {"ids": raw_ids or [0]},
            )
            await connection.execute(
                text(
                    "DELETE FROM sms_chunk WHERE id=ANY(CAST(:ids AS bigint[]))"
                ),
                {"ids": chunk_ids or [0]},
            )
            await connection.execute(
                text(
                    "DELETE FROM sms_batch WHERE id=ANY(CAST(:ids AS bigint[]))"
                ),
                {"ids": batch_ids or [0]},
            )
            if app_id is not None:
                await connection.execute(
                    text("DELETE FROM app WHERE id=:app_id"),
                    {"app_id": app_id},
                )
        await engine.dispose()
