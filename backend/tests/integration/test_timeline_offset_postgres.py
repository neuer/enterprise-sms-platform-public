"""号码时间线把 PostgreSQL 的绝对时刻序列化为上海时区，不二次平移。"""

from __future__ import annotations

import asyncio
import base64
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.api import web_messages as web_messages_module
from app.core.auth.jwt import JwtClaims
from app.core.auth.runtime import get_auth_facade
from app.core.correlation import correlation_scope
from app.core.errors import ApiError, api_error_handler
from app.core.runtime_resources import close_runtime_resources
from app.services.batch_query import BatchAccessScope
from app.services.crypto import CryptoService, EncryptionContext
from app.services.operations_query import (
    OperationsQueryService,
    SqlOperationsQueryRepository,
)

pytestmark = pytest.mark.skipif(
    "SECURITY_SESSION_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


def _crypto() -> CryptoService:
    secret = base64.b64encode(b"t" * 32).decode()
    ring = '{"active_version":1,"keys":{"1":"' + secret + '"}}'
    return CryptoService.from_secret_values(ring, ring)


class _Facade:
    async def verify(self, _token: str) -> JwtClaims:
        return JwtClaims(11, 101, "local", "tester", "测试用户", "平台部", "admin", 1, "jti", "sid")


class _Auditor:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def record(self, **values: object) -> None:
        self.calls.append(values)


@pytest.fixture
async def accept_runtime() -> AsyncIterator[tuple[AsyncEngine, URL]]:
    owner_url = make_url(os.environ["SECURITY_SESSION_POSTGRES_DSN"])
    password = uuid4().hex
    owner = create_async_engine(owner_url)
    database = owner_url.database
    async with owner.begin() as connection:
        await connection.execute(text(f"ALTER ROLE sms_accept WITH LOGIN PASSWORD '{password}'"))
        if database:
            await connection.execute(text(f'GRANT CONNECT ON DATABASE "{database}" TO sms_accept'))
    try:
        yield owner, owner_url.set(username="sms_accept", password=password)
    finally:
        await close_runtime_resources()
        await owner.dispose()


def _packed(
    crypto: CryptoService,
    *,
    domain: str,
    table: str,
    column: str,
    object_id: str,
    plain: str,
) -> bytes:
    return crypto.encrypt_bound_packed_text(
        plain,
        EncryptionContext(domain=domain, table=table, column=column, object_id=object_id),
    )


@pytest.mark.asyncio
async def test_timeline_http_serializes_postgres_instants_as_shanghai(
    accept_runtime: tuple[AsyncEngine, URL],
) -> None:
    owner, accept_url = accept_runtime
    crypto = _crypto()
    phone = "13800138000"
    protected = crypto.protect_phone(phone)
    notice_no = uuid4().hex
    verify_no = uuid4().hex
    event_key = uuid4().hex + uuid4().hex
    moments = {
        "notice": datetime(2026, 7, 12, 15, 30, tzinfo=UTC),
        "verify": datetime(2026, 7, 12, 16, 0, tzinfo=UTC),
        "reply": datetime(2026, 7, 12, 0, 30, tzinfo=timezone(timedelta(hours=9))),
    }
    async with owner.begin() as connection:
        for batch_no, category, plain, moment in (
            (notice_no, "notice", "通知正文", moments["notice"]),
            (verify_no, "verify", "验证正文", moments["verify"]),
        ):
            batch_id = int(
                (
                    await connection.execute(
                        text(
                            """
                            INSERT INTO sms_batch(
                              batch_no,category,channel,dept,content,
                              display_content_enc,send_content_enc,status
                            ) VALUES(
                              :batch_no,:category,'api','平台部','[encrypted]',
                              :display,:send,'completed'
                            ) RETURNING id
                            """
                        ),
                        {
                            "batch_no": batch_no,
                            "category": category,
                            "display": _packed(
                                crypto,
                                domain="sms-display-content",
                                table="sms_batch",
                                column="display_content_enc",
                                object_id=batch_no,
                                plain=plain,
                            ),
                            "send": _packed(
                                crypto,
                                domain="sms-content",
                                table="sms_batch",
                                column="send_content_enc",
                                object_id=batch_no,
                                plain=plain,
                            ),
                        },
                    )
                ).scalar_one()
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO sms_message(
                      batch_id,phone_enc,phone_hmac,phone_mask,key_version,status,created_at
                    ) VALUES(
                      :batch_id,:phone_enc,:phone_hmac,:phone_mask,:key_version,'delivered',
                      CAST(:created_at AS timestamptz)
                    )
                    """
                ),
                {
                    "batch_id": batch_id,
                    "phone_enc": protected.phone_enc,
                    "phone_hmac": protected.phone_hmac,
                    "phone_mask": protected.phone_mask,
                    "key_version": protected.key_version,
                    "created_at": moment,
                },
            )
        reply_batch = int(
            (
                await connection.execute(
                    text("SELECT id FROM sms_batch WHERE batch_no=:batch_no"),
                    {"batch_no": notice_no},
                )
            ).scalar_one()
        )
        reply_phone = crypto.protect_phone(phone, table="reply_event")
        await connection.execute(
            text(
                """
                INSERT INTO reply_event(
                  event_key,event_key_version,vendor_task_id,phone_enc,phone_hmac,
                  phone_mask,key_version,content,content_enc,reply_time
                ) VALUES(
                  CAST(:event_key AS char(64)),1,CAST(:vendor_task_id AS varchar(64)),
                  :phone_enc,:phone_hmac,:phone_mask,:key_version,
                  '[encrypted]',:content_enc,CAST(:reply_time AS timestamptz)
                )
                """
            ),
            {
                "event_key": event_key,
                "vendor_task_id": event_key,
                "phone_enc": reply_phone.phone_enc,
                "phone_hmac": reply_phone.phone_hmac,
                "phone_mask": reply_phone.phone_mask,
                "key_version": reply_phone.key_version,
                "reply_time": moments["reply"],
                "content_enc": _packed(
                    crypto,
                    domain="reply-content",
                    table="reply_event",
                    column="content_enc",
                    object_id=event_key,
                    plain="回复正文",
                ),
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO sms_reply(
                  event_key,batch_id,phone_enc,phone_hmac,phone_mask,key_version,
                  content,reply_time,created_at
                ) VALUES(
                  :event_key,:batch_id,:phone_enc,:phone_hmac,:phone_mask,:key_version,
                  '[encrypted]',CAST(:reply_time AS timestamptz),
                  timestamptz '2026-07-15 08:00:00+08'
                )
                """
            ),
            {
                "event_key": event_key,
                "batch_id": reply_batch,
                "phone_enc": reply_phone.phone_enc,
                "phone_hmac": reply_phone.phone_hmac,
                "phone_mask": reply_phone.phone_mask,
                "key_version": reply_phone.key_version,
                "reply_time": moments["reply"],
            },
        )
        epochs = {
            str(row["label"]): float(row["epoch"])
            for row in (
                await connection.execute(
                    text(
                        """
                        SELECT 'notice' AS label, extract(epoch FROM m.created_at) AS epoch
                        FROM sms_message m JOIN sms_batch b ON b.id=m.batch_id
                        WHERE b.batch_no=:notice
                        UNION ALL
                        SELECT 'verify', extract(epoch FROM m.created_at)
                        FROM sms_message m JOIN sms_batch b ON b.id=m.batch_id
                        WHERE b.batch_no=:verify
                        UNION ALL
                        SELECT 'reply', extract(epoch FROM reply_time)
                        FROM sms_reply WHERE event_key=:event_key
                        """
                    ),
                    {"notice": notice_no, "verify": verify_no, "event_key": event_key},
                )
            ).mappings()
        }
    service = OperationsQueryService(
        SqlOperationsQueryRepository(
            cast(Any, SimpleNamespace(database_url=accept_url)),
            crypto,
        ),
        crypto,
    )
    try:
        with correlation_scope():
            loaded = await service.timeline(
                phone=phone,
                start=None,
                end=None,
                scope=BatchAccessScope(all_departments=True),
            )

        class _Operations:
            async def timeline(self, **_values: object) -> object:
                return loaded

        auditor = _Auditor()

        def post_timeline() -> object:
            app = FastAPI()
            app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
            app.dependency_overrides[get_auth_facade] = lambda: _Facade()
            app.dependency_overrides[web_messages_module.get_operations_query_service] = (
                lambda: _Operations()
            )
            app.dependency_overrides[web_messages_module.get_sensitive_read_auditor] = (
                lambda: auditor
            )
            app.include_router(web_messages_module.router)
            return TestClient(app).post(
                "/api/v1/web/messages/timeline",
                headers={"Authorization": "Bearer jwt"},
                json={"phone": phone},
            )

        response = await asyncio.to_thread(post_timeline)
        assert response.status_code == 200
        events = response.json()["events"]
        by_content = {item["content"]: item for item in events}
        assert set(by_content) == {"通知正文", "验证正文", "回复正文"}
        assert by_content["通知正文"]["category"] == "notice"
        assert by_content["验证正文"]["category"] == "verify"
        assert by_content["回复正文"]["direction"] == "in"
        expected = {
            "通知正文": epochs["notice"],
            "验证正文": epochs["verify"],
            "回复正文": epochs["reply"],
        }
        for content, epoch in expected.items():
            stamp = by_content[content]["ts"]
            assert stamp.endswith("+08:00")
            assert "+00:00" not in stamp
            assert abs(datetime.fromisoformat(stamp).timestamp() - epoch) < 0.001
        ordered = [datetime.fromisoformat(item["ts"]).timestamp() for item in events]
        assert ordered == sorted(ordered, reverse=True)
        assert auditor.calls[0]["action"] == "message_content_read"
        assert auditor.calls[0]["count"] == 3
        assert response.json()["truncated"] is False
    finally:
        async with owner.begin() as connection:
            await connection.execute(
                text("DELETE FROM sms_reply WHERE event_key=:event_key"),
                {"event_key": event_key},
            )
            await connection.execute(
                text("DELETE FROM reply_event WHERE event_key=:event_key"),
                {"event_key": event_key},
            )
            await connection.execute(
                text("DELETE FROM sms_message WHERE phone_hmac=:hmac"),
                {"hmac": protected.phone_hmac},
            )
            await connection.execute(
                text("DELETE FROM sms_batch WHERE batch_no IN (:notice,:verify)"),
                {"notice": notice_no, "verify": verify_no},
            )
