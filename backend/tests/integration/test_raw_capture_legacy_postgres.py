"""0074 不改写 protocol_invalid：真实 PostgreSQL 行级覆盖。"""

from __future__ import annotations

import hashlib
import importlib.util
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.services.raw_capture_legacy import (
    BOUND_ENVELOPE_MAGIC,
    BOUND_ENVELOPE_OVERHEAD_BYTES,
)
from app.services.raw_spill import (
    CAPTURE_COMPLETE,
    CAPTURE_COMPLETE_TOO_LARGE,
    CAPTURE_PROTOCOL_INVALID,
    CAPTURE_UNKNOWN_LEGACY,
)

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)

BACKEND = Path(__file__).resolve().parents[2]
REVISION_0074 = BACKEND / "migrations/versions/0074_raw_legacy_capture.py"
OVERSIZED_ERROR = "report oversized payload persisted after consume gap"
TRUNCATED_ERROR = "report truncated vendor response beyond recovery limit"


@dataclass(frozen=True, slots=True)
class RawRowSnapshot:
    """行级对照：只含无 PII 的分类证据，不含密文正文。"""

    name: str
    raw_id: int
    capture_state: str
    error: str | None
    processed: bool
    enc_len: int
    payload_sha256: str


def _sme2_ciphertext(plaintext_len: int) -> bytes:
    """构造仅含 SME2 头的合成密文；不加密、不含手机号。"""

    tail_len = BOUND_ENVELOPE_OVERHEAD_BYTES - len(BOUND_ENVELOPE_MAGIC) + plaintext_len
    return BOUND_ENVELOPE_MAGIC + b"\x00" * tail_len


def _0074_complete_repair_sql() -> str:
    """取出 0074 只改 complete 行的 UPDATE，不调用 Alembic，不改共享库 schema。"""

    spec = importlib.util.spec_from_file_location("rev_0074_raw_legacy_capture", REVISION_0074)
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
        if "UPDATE raw_vendor_log" in sql and "WHERE capture_state='complete'" in sql
    ]
    assert len(updates) == 1
    assert "WHERE capture_state='protocol_invalid'" not in updates[0]
    return updates[0]


async def _insert_raw(
    connection: AsyncConnection,
    *,
    name: str,
    capture_state: str,
    payload_enc: bytes,
    error: str | None,
    processed: bool = False,
) -> RawRowSnapshot:
    digest = hashlib.sha256(payload_enc).hexdigest()
    raw_id = int(
        (
            await connection.execute(
                text(
                    """
                    INSERT INTO raw_vendor_log(
                      source,payload_enc,payload_sha256,capture_state,error,processed
                    ) VALUES (
                      'report',:payload_enc,:sha,:capture_state,:error,:processed
                    )
                    RETURNING id
                    """
                ),
                {
                    "payload_enc": payload_enc,
                    "sha": digest,
                    "capture_state": capture_state,
                    "error": error,
                    "processed": processed,
                },
            )
        ).scalar_one()
    )
    return await _load_one(connection, name=name, raw_id=raw_id)


async def _load_one(connection: AsyncConnection, *, name: str, raw_id: int) -> RawRowSnapshot:
    row = (
        (
            await connection.execute(
                text(
                    """
                SELECT capture_state,error,processed,
                       octet_length(payload_enc) AS enc_len,
                       payload_sha256
                FROM raw_vendor_log
                WHERE id=:raw_id
                """
                ),
                {"raw_id": raw_id},
            )
        )
        .mappings()
        .one()
    )
    return RawRowSnapshot(
        name=name,
        raw_id=raw_id,
        capture_state=str(row["capture_state"]),
        error=str(row["error"]) if row["error"] is not None else None,
        processed=bool(row["processed"]),
        enc_len=int(row["enc_len"]),
        payload_sha256=str(row["payload_sha256"]),
    )


async def _load_snapshots(
    connection: AsyncConnection,
    expected: Mapping[str, RawRowSnapshot],
) -> dict[str, RawRowSnapshot]:
    ids = {item.raw_id: item.name for item in expected.values()}
    rows = (
        await connection.execute(
            text(
                """
                SELECT id,capture_state,error,processed,
                       octet_length(payload_enc) AS enc_len,
                       payload_sha256
                FROM raw_vendor_log
                WHERE id = ANY(CAST(:ids AS bigint[]))
                ORDER BY id
                """
            ),
            {"ids": list(ids)},
        )
    ).mappings()
    loaded = {
        ids[int(row["id"])]: RawRowSnapshot(
            name=ids[int(row["id"])],
            raw_id=int(row["id"]),
            capture_state=str(row["capture_state"]),
            error=str(row["error"]) if row["error"] is not None else None,
            processed=bool(row["processed"]),
            enc_len=int(row["enc_len"]),
            payload_sha256=str(row["payload_sha256"]),
        )
        for row in rows
    }
    assert loaded.keys() == expected.keys()
    return loaded


@pytest.mark.asyncio
async def test_0074_does_not_rewrite_protocol_invalid_rows() -> None:
    """在已升级库上执行 0074 UPDATE 后回滚：protocol_invalid 行保持不动。"""

    assert os.environ.get("ENVIRONMENT") == "test"
    repair_sql = _0074_complete_repair_sql()
    engine = create_async_engine(make_url(os.environ["OUTBOX_POSTGRES_DSN"]))
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                before = {
                    item.name: item
                    for item in (
                        await _insert_raw(
                            connection,
                            name="protocol_invalid_oversized",
                            capture_state=CAPTURE_PROTOCOL_INVALID,
                            payload_enc=_sme2_ciphertext(800),
                            error=OVERSIZED_ERROR,
                        ),
                        await _insert_raw(
                            connection,
                            name="protocol_invalid_truncated",
                            capture_state=CAPTURE_PROTOCOL_INVALID,
                            payload_enc=_sme2_ciphertext(800),
                            error=TRUNCATED_ERROR,
                        ),
                        await _insert_raw(
                            connection,
                            name="protocol_invalid_short_envelope",
                            capture_state=CAPTURE_PROTOCOL_INVALID,
                            payload_enc=b"xxshort!",
                            error=None,
                        ),
                        await _insert_raw(
                            connection,
                            name="complete_oversized",
                            capture_state=CAPTURE_COMPLETE,
                            payload_enc=_sme2_ciphertext(800),
                            error=OVERSIZED_ERROR,
                        ),
                        await _insert_raw(
                            connection,
                            name="complete_ordinary",
                            capture_state=CAPTURE_COMPLETE,
                            payload_enc=_sme2_ciphertext(800),
                            error=None,
                        ),
                        await _insert_raw(
                            connection,
                            name="complete_short_envelope",
                            capture_state=CAPTURE_COMPLETE,
                            payload_enc=b"xxshort!",
                            error=None,
                        ),
                    )
                }
                await connection.execute(text(repair_sql))
                after = await _load_snapshots(connection, before)
                expected_states = {
                    "protocol_invalid_oversized": CAPTURE_PROTOCOL_INVALID,
                    "protocol_invalid_truncated": CAPTURE_PROTOCOL_INVALID,
                    "protocol_invalid_short_envelope": CAPTURE_PROTOCOL_INVALID,
                    "complete_oversized": CAPTURE_COMPLETE_TOO_LARGE,
                    "complete_ordinary": CAPTURE_COMPLETE,
                    "complete_short_envelope": CAPTURE_UNKNOWN_LEGACY,
                }
                for name, expected_state in expected_states.items():
                    assert after[name].capture_state == expected_state, name
                for name in (
                    "protocol_invalid_oversized",
                    "protocol_invalid_truncated",
                    "protocol_invalid_short_envelope",
                ):
                    assert after[name] == before[name]
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
