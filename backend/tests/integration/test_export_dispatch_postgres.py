"""导出必须使用正式角色读取剔除原因，不能由 owner 测试掩盖缺失授权。"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import create_async_engine

from app.services.export import ExportFilterSet
from app.services.export_repository import ExportClaim, SqlExportRepository
from tests.integration.test_dispatch_blacklist_postgres import _insert_app, _prepare, _seed

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


class SharedConnection:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    @asynccontextmanager
    async def connect(self):
        yield self.connection

    async def dispose(self) -> None:
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("decrypted", [False, True])
async def test_export_role_reads_exclusion_without_write_or_extra_column_grants(
    decrypted: bool,
) -> None:
    engine = create_async_engine(make_url(os.environ["OUTBOX_POSTGRES_DSN"]))
    try:
        await _prepare(engine)
        async with engine.connect() as c:
            tx = await c.begin()
            try:
                app = await _insert_app(c, uuid4().hex, blacklist_check=True)
                batch, _chunk, batch_no = await _seed(
                    c,
                    app_id=app,
                    category="notice",
                    status="pending",
                    phones=["138" + "0013" + "8000"],
                    reservation=False,
                )
                await c.execute(
                    text("""
                    INSERT INTO sms_dispatch_exclusion(message_id,message_created_at,
                        batch_id,reason,decision_generation)
                    SELECT id,created_at,batch_id,'BLACKLISTED_BEFORE_SEND',1
                    FROM sms_message WHERE batch_id=:batch
                """),
                    {"batch": batch},
                )
                await c.execute(
                    text("UPDATE sms_message SET status='other' WHERE batch_id=:batch"),
                    {"batch": batch},
                )
                await c.execute(text("SET LOCAL ROLE sms_export"))
                repo = SqlExportRepository()
                bound = SharedConnection(c)
                repo._engine = lambda: bound  # type: ignore[method-assign]
                claim = ExportClaim(
                    1,
                    ExportFilterSet(None, None, None, None, None, batch_no, (), "平台部"),
                    decrypted,
                    uuid4(),
                    datetime.now(UTC),
                )
                rows = [row async for row in repo.rows(claim)]
                assert len(rows) == 1 and rows[0]["exclusion_reason"] == "BLACKLISTED_BEFORE_SEND"
                assert rows[0]["display_status"] == "other"
                assert ("phone_enc" in rows[0]) is decrypted
                assert ("phone_mask" in rows[0]) is not decrypted
                for operation in [
                    "SELECT decision_generation FROM sms_dispatch_exclusion LIMIT 1",
                    "UPDATE sms_dispatch_exclusion SET reason=reason WHERE false",
                    "DELETE FROM sms_dispatch_exclusion WHERE false",
                    "INSERT INTO sms_dispatch_exclusion "
                    "SELECT * FROM sms_dispatch_exclusion WHERE false",
                    "TRUNCATE sms_dispatch_exclusion",
                ]:
                    with pytest.raises(ProgrammingError):
                        async with c.begin_nested():
                            await c.execute(text(operation))
            finally:
                await tx.rollback()
    finally:
        await engine.dispose()
