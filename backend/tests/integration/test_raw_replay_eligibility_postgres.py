"""0075 回填与自动扫描：在已升级 recovery 目录上做行级覆盖，不另建库。"""

from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.services.raw_parse import persist_column_values
from app.services.raw_spill import (
    CAPTURE_COMPLETE,
    CAPTURE_PROTOCOL_INVALID,
    CAPTURE_TRUNCATED,
)

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)

BACKEND = Path(__file__).resolve().parents[2]
REVISION_0075 = BACKEND / "migrations/versions/0075_raw_parse_eligibility.py"


def _0075_backfill_sql() -> str:
    """取出 0075 回填 UPDATE，不调用 Alembic，不改共享库 schema。"""

    spec = importlib.util.spec_from_file_location(
        "rev_0075_raw_parse_eligibility", REVISION_0075
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    captured: list[str] = []

    def _capture(statement: object, *_args: object, **_kwargs: object) -> None:
        captured.append(str(statement))

    module.op.execute = _capture
    module.upgrade()
    updates = [
        sql
        for sql in captured
        if "UPDATE raw_vendor_log SET" in sql and "replay_eligibility=" in sql
    ]
    assert len(updates) == 1
    assert "ALTER TABLE" not in updates[0]
    assert "CREATE INDEX" not in updates[0]
    assert "GRANT" not in updates[0]
    return updates[0]


async def _insert(
    connection: AsyncConnection,
    *,
    name: str,
    capture_state: str = CAPTURE_COMPLETE,
    http_status: int = 200,
    content_encoding: str = "identity",
    error: str | None = None,
    processed: bool = False,
    parse_state: str | None = None,
    replay_eligibility: str | None = None,
) -> int:
    payload = f"{name}:{os.urandom(8).hex()}".encode()
    columns = persist_column_values(
        capture_state=capture_state,
        http_status=http_status,
        content_encoding=content_encoding,
    )
    result = await connection.execute(
        text(
            """
            INSERT INTO raw_vendor_log(
              source,payload_enc,payload_sha256,http_status,content_encoding,
              capture_state,error,processed,parse_state,replay_eligibility
            ) VALUES (
              'report',:payload_enc,:sha,:http_status,:content_encoding,
              :capture_state,:error,:processed,
              COALESCE(:parse_state,:persist_parse),
              COALESCE(:replay_eligibility,:persist_eligibility)
            )
            RETURNING id
            """
        ),
        {
            "payload_enc": payload,
            "sha": hashlib.sha256(payload).hexdigest(),
            "http_status": http_status,
            "content_encoding": content_encoding,
            "capture_state": capture_state,
            "error": error,
            "processed": processed,
            "parse_state": parse_state,
            "replay_eligibility": replay_eligibility,
            "persist_parse": columns["parse_state"],
            "persist_eligibility": columns["replay_eligibility"],
        },
    )
    return int(result.scalar_one())


async def _load(connection: AsyncConnection, raw_id: int) -> dict[str, object]:
    row = (
        await connection.execute(
            text(
                """
                SELECT capture_state,parse_state,replay_eligibility,processed,error
                FROM raw_vendor_log WHERE id=:raw_id
                """
            ),
            {"raw_id": raw_id},
        )
    ).mappings().one()
    return dict(row)


@pytest.mark.asyncio
async def test_http_encoding_json_and_transient_matrix_on_postgres() -> None:
    """在已升级库上执行 0075 UPDATE 后回滚：资格矩阵不得另建 catalog。"""

    assert os.environ.get("ENVIRONMENT") == "test"
    engine = create_async_engine(make_url(os.environ["OUTBOX_POSTGRES_DSN"]))
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                http_id = await _insert(
                    connection, name="http500", http_status=500
                )
                encoding_id = await _insert(
                    connection,
                    name="gzip",
                    content_encoding="unsupported",
                )
                json_id = await _insert(
                    connection,
                    name="json",
                    error="VendorProtocolError: vendor response is not JSON",
                    parse_state="unattempted",
                    replay_eligibility="manual",
                )
                envelope_id = await _insert(
                    connection,
                    name="envelope",
                    error="VendorApiError: vendor response parsing failed",
                    parse_state="protocol_invalid",
                    replay_eligibility="never",
                )
                transient_id = await _insert(
                    connection,
                    name="pg",
                    error="OperationalError: server closed the connection",
                    parse_state="transient_failure",
                    replay_eligibility="automatic",
                )
                crash_id = await _insert(connection, name="crash")
                historical_id = await _insert(
                    connection,
                    name="legacy",
                    error="unrecognized historical residue",
                    parse_state="unattempted",
                    replay_eligibility="manual",
                )
                truncated_id = await _insert(
                    connection,
                    name="truncated",
                    capture_state=CAPTURE_TRUNCATED,
                    error="report truncated vendor response beyond recovery limit",
                )
                protocol_id = await _insert(
                    connection,
                    name="protocol",
                    capture_state=CAPTURE_PROTOCOL_INVALID,
                    error="report protocol-invalid vendor response",
                )
                processed_id = await _insert(
                    connection, name="done", processed=True
                )

                http = await _load(connection, http_id)
                encoding = await _load(connection, encoding_id)
                crash = await _load(connection, crash_id)
                assert http["replay_eligibility"] == "never"
                assert encoding["replay_eligibility"] == "never"
                assert crash["replay_eligibility"] == "automatic"
                assert crash["parse_state"] == "unattempted"

                await connection.execute(text(_0075_backfill_sql()))

                json_row = await _load(connection, json_id)
                envelope = await _load(connection, envelope_id)
                transient = await _load(connection, transient_id)
                historical = await _load(connection, historical_id)
                truncated = await _load(connection, truncated_id)
                protocol = await _load(connection, protocol_id)
                processed = await _load(connection, processed_id)
                assert json_row["replay_eligibility"] == "manual"
                assert json_row["parse_state"] == "protocol_invalid"
                assert envelope["replay_eligibility"] == "never"
                assert transient["replay_eligibility"] == "automatic"
                assert transient["parse_state"] == "transient_failure"
                assert historical["replay_eligibility"] == "manual"
                assert truncated["replay_eligibility"] == "never"
                assert protocol["replay_eligibility"] == "never"
                assert processed["replay_eligibility"] == "never"
                assert processed["parse_state"] == "processed"
                assert (await _load(connection, http_id))["replay_eligibility"] == "never"
                assert (await _load(connection, crash_id))["replay_eligibility"] == "automatic"

                stale = [
                    int(value)
                    for value in (
                        await connection.execute(
                            text(
                                """
                                SELECT id FROM raw_vendor_log
                                WHERE processed=false
                                  AND capture_state='complete'
                                  AND replay_eligibility='automatic'
                                  AND replay_attempts<10
                                  AND (
                                    processing_started_at IS NULL
                                    OR processing_started_at<=now()-interval '15 minutes'
                                  )
                                  AND id = ANY(CAST(:ids AS bigint[]))
                                ORDER BY id
                                """
                            ),
                            {
                                "ids": [
                                    http_id,
                                    encoding_id,
                                    json_id,
                                    envelope_id,
                                    transient_id,
                                    crash_id,
                                    historical_id,
                                    truncated_id,
                                    protocol_id,
                                    processed_id,
                                ]
                            },
                        )
                    ).scalars()
                ]
                assert stale == [transient_id, crash_id]
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
