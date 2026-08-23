"""0074 不改写 protocol_invalid：真实 PostgreSQL 行级覆盖。"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

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
PRE_0074_REVISION = "0073_raw_protocol_invalid"
REVISION_0074 = "0074_raw_legacy_capture"
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


def _source_url() -> URL:
    return make_url(os.environ["OUTBOX_POSTGRES_DSN"])


def _redact(text_value: str) -> str:
    password = _source_url().password
    if password:
        return text_value.replace(password, "***")
    return text_value


def _sme2_ciphertext(plaintext_len: int) -> bytes:
    """构造仅含 SME2 头的合成密文；不加密、不含手机号。"""

    tail_len = BOUND_ENVELOPE_OVERHEAD_BYTES - len(BOUND_ENVELOPE_MAGIC) + plaintext_len
    return BOUND_ENVELOPE_MAGIC + b"\x00" * tail_len


def _alembic_env(db_name: str) -> dict[str, str]:
    source = _source_url()
    env = os.environ.copy()
    env["ENVIRONMENT"] = "test"
    env["DEBUG"] = "1"
    env["AUTH_MOCK"] = "1"
    env["VENDOR_MOCK"] = "1"
    env["DB_NAME"] = db_name
    if source.host:
        env["DB_HOST"] = source.host
    if source.port is not None:
        env["DB_PORT"] = str(source.port)
    return env


def _run_alembic(revision: str, *, db_name: str) -> None:
    """对隔离库执行指定 Alembic revision；失败时不回显明文口令。"""

    alembic = shutil.which("alembic")
    if alembic is None:
        raise RuntimeError("alembic executable not found; run via uv run")
    completed = subprocess.run(
        [alembic, "-c", str(BACKEND / "alembic.ini"), "upgrade", revision],
        cwd=BACKEND,
        env=_alembic_env(db_name),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = _redact((completed.stderr or completed.stdout or "").strip())
        raise RuntimeError(
            f"alembic upgrade {revision} failed with code {completed.returncode}: {detail[-2000:]}"
        )


async def _execute_autocommit(admin_engine: AsyncEngine, statement: str) -> None:
    async with admin_engine.connect() as connection:
        await connection.execute(text(statement))


async def _insert_raw(
    engine: AsyncEngine,
    *,
    name: str,
    capture_state: str,
    payload_enc: bytes,
    error: str | None,
    processed: bool = False,
) -> RawRowSnapshot:
    digest = hashlib.sha256(payload_enc).hexdigest()
    async with engine.begin() as connection:
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
    engine: AsyncEngine,
    expected: Mapping[str, RawRowSnapshot],
) -> dict[str, RawRowSnapshot]:
    ids = {item.raw_id: item.name for item in expected.values()}
    async with engine.connect() as connection:
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


async def _alembic_version(engine: AsyncEngine) -> str:
    async with engine.connect() as connection:
        version = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    assert version is not None
    return str(version)


@pytest.mark.asyncio
async def test_0074_does_not_rewrite_protocol_invalid_rows() -> None:
    """0073 插入后跑 0074：protocol_invalid 保持原行，complete 证据行仍重分类。"""

    assert os.environ.get("ENVIRONMENT") == "test"
    isolated_name = f"sms_0074_{uuid4().hex}"
    assert isolated_name.isidentifier()
    source = _source_url()
    admin_engine = create_async_engine(
        source.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
    )
    isolated_engine: AsyncEngine | None = None
    try:
        await _execute_autocommit(admin_engine, f'CREATE DATABASE "{isolated_name}"')
        _run_alembic(PRE_0074_REVISION, db_name=isolated_name)
        isolated_engine = create_async_engine(source.set(database=isolated_name))
        assert await _alembic_version(isolated_engine) == PRE_0074_REVISION

        with pytest.raises(IntegrityError):
            async with isolated_engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        INSERT INTO raw_vendor_log(
                          source,payload_enc,payload_sha256,capture_state
                        ) VALUES (
                          'report',:payload_enc,:sha,'unknown_legacy'
                        )
                        """
                    ),
                    {
                        "payload_enc": _sme2_ciphertext(32),
                        "sha": "b" * 64,
                    },
                )

        before = {
            item.name: item
            for item in (
                await _insert_raw(
                    isolated_engine,
                    name="protocol_invalid_oversized",
                    capture_state=CAPTURE_PROTOCOL_INVALID,
                    payload_enc=_sme2_ciphertext(800),
                    error=OVERSIZED_ERROR,
                ),
                await _insert_raw(
                    isolated_engine,
                    name="protocol_invalid_truncated",
                    capture_state=CAPTURE_PROTOCOL_INVALID,
                    payload_enc=_sme2_ciphertext(800),
                    error=TRUNCATED_ERROR,
                ),
                await _insert_raw(
                    isolated_engine,
                    name="protocol_invalid_short_envelope",
                    capture_state=CAPTURE_PROTOCOL_INVALID,
                    payload_enc=b"xxshort!",
                    error=None,
                ),
                await _insert_raw(
                    isolated_engine,
                    name="complete_oversized",
                    capture_state=CAPTURE_COMPLETE,
                    payload_enc=_sme2_ciphertext(800),
                    error=OVERSIZED_ERROR,
                ),
                await _insert_raw(
                    isolated_engine,
                    name="complete_ordinary",
                    capture_state=CAPTURE_COMPLETE,
                    payload_enc=_sme2_ciphertext(800),
                    error=None,
                ),
                await _insert_raw(
                    isolated_engine,
                    name="complete_short_envelope",
                    capture_state=CAPTURE_COMPLETE,
                    payload_enc=b"xxshort!",
                    error=None,
                ),
            )
        }

        await isolated_engine.dispose()
        isolated_engine = None
        _run_alembic(REVISION_0074, db_name=isolated_name)
        isolated_engine = create_async_engine(source.set(database=isolated_name))
        assert await _alembic_version(isolated_engine) == REVISION_0074

        after = await _load_snapshots(isolated_engine, before)
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
        if isolated_engine is not None:
            await isolated_engine.dispose()
        await _execute_autocommit(
            admin_engine,
            f'DROP DATABASE IF EXISTS "{isolated_name}" WITH (FORCE)',
        )
        await admin_engine.dispose()
