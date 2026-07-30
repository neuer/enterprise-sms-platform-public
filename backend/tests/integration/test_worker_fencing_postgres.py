from __future__ import annotations

import base64
import os
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.services.callback_repository import SqlCallbackRepository
from app.services.callback_worker import CallbackLeaseLost
from app.services.crypto import CryptoService
from app.services.export_file import ExportFileCodec
from app.services.export_repository import ExportLeaseLost, SqlExportRepository

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


async def _rows(value: str) -> AsyncIterator[tuple[str]]:
    yield (value,)


def _crypto() -> CryptoService:
    key = base64.b64encode(b"f" * 32).decode()
    return CryptoService.from_secret_values(key, key)


@pytest.mark.asyncio
async def test_callback_and_export_takeover_fence_old_worker_and_file(
    tmp_path: Path,
) -> None:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    settings = cast(
        Any,
        SimpleNamespace(
            database_url=database_url,
            database_url_for=lambda _role: database_url,
        ),
    )
    engine = create_async_engine(database_url)
    callbacks = SqlCallbackRepository(settings)
    exports = SqlExportRepository(settings)
    nonce = uuid4().hex
    app_id: int | None = None
    callback_id: int | None = None
    export_id: int | None = None

    async def cleanup() -> None:
        async with engine.begin() as connection:
            if callback_id is not None:
                await connection.execute(
                    text(
                        """
                        DELETE FROM worker_lease_event
                        WHERE task_kind='callback' AND task_id=:task_id
                        """
                    ),
                    {"task_id": callback_id},
                )
                await connection.execute(
                    text("DELETE FROM callback_task WHERE id=:task_id"),
                    {"task_id": callback_id},
                )
            if export_id is not None:
                await connection.execute(
                    text(
                        """
                        DELETE FROM worker_lease_event
                        WHERE task_kind='export' AND task_id=:task_id
                        """
                    ),
                    {"task_id": export_id},
                )
                await connection.execute(
                    text("DELETE FROM export_task WHERE id=:task_id"),
                    {"task_id": export_id},
                )
            if app_id is not None:
                await connection.execute(
                    text("DELETE FROM app WHERE id=:app_id"),
                    {"app_id": app_id},
                )

    try:
        async with engine.begin() as connection:
            app_id = int(
                (
                    await connection.execute(
                        text(
                            """
                            INSERT INTO app(
                              name,dept,api_key_hash,api_key_prefix,created_by
                            ) VALUES(
                              :name,'平台部',:api_key_hash,:api_key_prefix,'test'
                            ) RETURNING id
                            """
                        ),
                        {
                            "name": f"fencing-{nonce}",
                            "api_key_hash": "a" * 64,
                            "api_key_prefix": nonce[:8],
                        },
                    )
                ).scalar_one()
            )
            callback_id = int(
                (
                    await connection.execute(
                        text(
                            """
                            INSERT INTO callback_task(
                              app_id,event,url,callback_secret_enc,
                              callback_secret_key_version,signature_version
                            ) VALUES(
                              :app_id,'batch.finished','https://callback.invalid',
                              :callback_secret_enc,1,1
                            )
                            RETURNING id
                            """
                        ),
                        {
                            "app_id": app_id,
                            "callback_secret_enc": b"\x00\x01" + b"x" * 30,
                        },
                    )
                ).scalar_one()
            )
            export_id = int(
                (
                    await connection.execute(
                        text(
                            """
                            INSERT INTO export_task(
                              creator,scope_resolved,filters,decrypted
                            ) VALUES(
                              :creator,true,
                              '{"dataset":"message","phone_hmacs":[]}'::jsonb,
                              false
                            ) RETURNING id
                            """
                        ),
                        {"creator": f"fencing-{nonce}"},
                    )
                ).scalar_one()
            )

        callback_a = await callbacks.claim(callback_id, lease_seconds=15)
        assert callback_a is not None
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE callback_task
                    SET lease_expires_at=now()-interval '1 second'
                    WHERE id=:task_id
                    """
                ),
                {"task_id": callback_id},
            )
        callback_b = await callbacks.claim(callback_id, lease_seconds=15)
        assert callback_b is not None and callback_b.lease_id != callback_a.lease_id
        with pytest.raises(CallbackLeaseLost):
            await callbacks.mark_done(callback_id, callback_a.lease_id, 200)
        await callbacks.mark_done(callback_id, callback_b.lease_id, 204)

        export_a = await exports.claim(export_id, lease_seconds=15)
        assert export_a is not None
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE export_task
                    SET lease_expires_at=now()-interval '1 second'
                    WHERE id=:task_id
                    """
                ),
                {"task_id": export_id},
            )
        export_b = await exports.claim(export_id, lease_seconds=15)
        assert export_b is not None and export_b.lease_id != export_a.lease_id

        codec = ExportFileCodec(_crypto(), tmp_path)
        old_path = await codec.write_csv(
            export_id,
            export_a.lease_id,
            ("phone",),
            _rows("138****8000"),
        )
        current_path = await codec.write_csv(
            export_id,
            export_b.lease_id,
            ("phone",),
            _rows("139****9000"),
        )
        with pytest.raises(ExportLeaseLost):
            await exports.mark_done(
                export_id,
                lease_id=export_a.lease_id,
                file_path=str(old_path),
                row_count=1,
            )
        codec.remove(old_path)
        await exports.mark_done(
            export_id,
            lease_id=export_b.lease_id,
            file_path=str(current_path),
            row_count=1,
        )

        async with engine.connect() as connection:
            export_row = (
                await connection.execute(
                    text(
                        """
                        SELECT status,file_path,takeover_count
                        FROM export_task WHERE id=:task_id
                        """
                    ),
                    {"task_id": export_id},
                )
            ).mappings().one()
            callback_events = (
                await connection.execute(
                    text(
                        """
                        SELECT event_type FROM worker_lease_event
                        WHERE task_kind='callback' AND task_id=:task_id
                        ORDER BY id
                        """
                    ),
                    {"task_id": callback_id},
                )
            ).scalars().all()
            export_events = (
                await connection.execute(
                    text(
                        """
                        SELECT event_type FROM worker_lease_event
                        WHERE task_kind='export' AND task_id=:task_id
                        ORDER BY id
                        """
                    ),
                    {"task_id": export_id},
                )
            ).scalars().all()

        assert export_row["status"] == "done"
        assert export_row["file_path"] == str(current_path)
        assert int(export_row["takeover_count"]) == 1
        assert not old_path.exists() and current_path.exists()
        assert callback_events == ["acquired", "takeover", "fencing_miss"]
        assert export_events == ["acquired", "takeover", "fencing_miss"]
    finally:
        await cleanup()
        await engine.dispose()
