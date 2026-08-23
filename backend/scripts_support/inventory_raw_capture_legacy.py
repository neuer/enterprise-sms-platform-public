"""只读盘点 raw_vendor_log 历史完整性，不解密正文、不输出号码或密钥。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, cast

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import URL, text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.services.raw_capture_legacy import (  # noqa: E402
    RawInventoryInput,
    build_inventory,
    inventory_leak_reasons,
)

INVENTORY_SQL_WITH_STATE = """
SELECT id,source,processed,octet_length(payload_enc) AS enc_len,
  substring(payload_enc from 1 for 4) AS payload_prefix,
  error,fetched_at,replay_attempts,capture_state
FROM raw_vendor_log
ORDER BY id
"""
INVENTORY_SQL_WITHOUT_STATE = """
SELECT id,source,processed,octet_length(payload_enc) AS enc_len,
  substring(payload_enc from 1 for 4) AS payload_prefix,
  error,fetched_at,replay_attempts,NULL::varchar AS capture_state
FROM raw_vendor_log
ORDER BY id
"""


def _column_exists_sql() -> str:
    return """
    SELECT 1 FROM information_schema.columns
    WHERE table_schema='public' AND table_name='raw_vendor_log'
      AND column_name='capture_state'
    """


def row_to_input(row: Mapping[str, Any]) -> RawInventoryInput:
    prefix = row.get("payload_prefix") or b""
    if isinstance(prefix, memoryview):
        prefix = prefix.tobytes()
    fetched_at = row.get("fetched_at")
    return RawInventoryInput(
        source=str(row["source"]),
        processed=bool(row["processed"]),
        enc_len=int(row["enc_len"] or 0),
        payload_prefix=bytes(prefix)[:4],
        error=str(row["error"]) if row.get("error") is not None else None,
        fetched_at=cast(datetime | None, fetched_at),
        capture_state=(
            str(row["capture_state"]) if row.get("capture_state") is not None else None
        ),
        replay_attempts=int(row.get("replay_attempts") or 0),
        raw_id=int(row["id"]) if row.get("id") is not None else None,
    )


async def load_inventory_rows(database_url: str | URL) -> list[RawInventoryInput]:
    engine = create_async_engine(database_url, hide_parameters=True)
    try:
        async with engine.connect() as connection:
            exists = await connection.scalar(text(_column_exists_sql()))
            result = await connection.execute(
                text(
                    INVENTORY_SQL_WITH_STATE
                    if exists
                    else INVENTORY_SQL_WITHOUT_STATE
                )
            )
            return [row_to_input(dict(row)) for row in result.mappings()]
    finally:
        await engine.dispose()


def render_inventory(rows: Sequence[RawInventoryInput]) -> dict[str, Any]:
    document = build_inventory(rows)
    leaks = inventory_leak_reasons(document)
    if leaks:
        raise RuntimeError("inventory output rejected: " + ",".join(leaks))
    return document


async def run(database_url: str | URL) -> dict[str, Any]:
    rows = await load_inventory_rows(database_url)
    return render_inventory(rows)


def _database_url(explicit: str | None) -> str | URL:
    if explicit:
        return explicit
    from app.settings import get_settings

    return get_settings().database_owner_url


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="只读盘点 raw_vendor_log 完整性状态，不解密正文"
    )
    parser.add_argument(
        "--database-url",
        help="可选 SQLAlchemy URL；默认读取 owner DSN，且只执行 SELECT",
    )
    args = parser.parse_args(argv)
    document = asyncio.run(run(_database_url(args.database_url)))
    json.dump(document, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
