"""把主体/系统审计 key 装入仅 sms_owner 可读的验证表。"""

from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.audit_context import decode_audit_context_key
from app.settings import get_settings


async def provision() -> None:
    settings = get_settings()
    keys = {
        "principal": decode_audit_context_key(
            settings.credential("audit_context_key")
        ),
        "system:api": decode_audit_context_key(
            settings.credential("audit_system_api_context_key")
        ),
        "system:realtime": decode_audit_context_key(
            settings.credential("audit_system_realtime_context_key")
        ),
        "system:bulk": decode_audit_context_key(
            settings.credential("audit_system_bulk_context_key")
        ),
    }
    engine = create_async_engine(settings.database_owner_url, hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO audit_context_signing_key(
                      key_kind,key_material,updated_at
                    ) VALUES(:key_kind,:key,now())
                    ON CONFLICT(key_kind) DO UPDATE
                    SET key_material=EXCLUDED.key_material,updated_at=now()
                    """
                ),
                [
                    {"key_kind": key_kind, "key": key}
                    for key_kind, key in keys.items()
                ],
            )
    finally:
        await engine.dispose()


def main() -> int:
    asyncio.run(provision())
    print("audit context signing keys provisioned")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
