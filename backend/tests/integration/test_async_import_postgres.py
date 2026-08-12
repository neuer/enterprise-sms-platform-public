from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.auth.accounts import SecurityPrincipal
from app.services.import_repository import SqlImportRepository
from app.services.imports import ImportPhone

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


@pytest.mark.asyncio
async def test_async_import_lease_takeover_fences_old_worker_and_exhausts_attempts(
    tmp_path: Path,
) -> None:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    settings = cast(
        Any,
        SimpleNamespace(database_url=database_url, import_storage_dir=tmp_path),
    )
    engine = create_async_engine(database_url)
    repository = SqlImportRepository(settings)
    nonce = uuid4().hex
    login = f"async-import-{nonce[:16]}"
    account_id: int | None = None
    identity_id: int | None = None
    import_ids: list[str] = []

    async def cleanup() -> None:
        async with engine.begin() as connection:
            if import_ids:
                await connection.execute(
                    text(
                        """
                        DELETE FROM import_phone WHERE import_task_id IN (
                          SELECT id FROM import_task
                          WHERE import_id=ANY(CAST(:import_ids AS uuid[]))
                        )
                        """
                    ),
                    {"import_ids": import_ids},
                )
                await connection.execute(
                    text(
                        """
                        DELETE FROM import_task
                        WHERE import_id=ANY(CAST(:import_ids AS uuid[]))
                        """
                    ),
                    {"import_ids": import_ids},
                )
            await connection.execute(
                text("DELETE FROM audit_log WHERE actor_account_id=:account_id"),
                {"account_id": account_id},
            )
            await connection.execute(
                text("DELETE FROM auth_identity WHERE id=:identity_id"),
                {"identity_id": identity_id},
            )
            await connection.execute(
                text("DELETE FROM user_account WHERE id=:account_id"),
                {"account_id": account_id},
            )

    try:
        async with engine.begin() as connection:
            account_id = int(
                (
                    await connection.execute(
                        text(
                            """
                            INSERT INTO user_account(display_name,dept,role)
                            VALUES(:login,'平台部','operator') RETURNING id
                            """
                        ),
                        {"login": login},
                    )
                ).scalar_one()
            )
            provider_id = int(
                (
                    await connection.execute(
                        text("SELECT id FROM auth_provider WHERE code='local'")
                    )
                ).scalar_one()
            )
            identity_id = int(
                (
                    await connection.execute(
                        text(
                            """
                            INSERT INTO auth_identity(
                              account_id,provider_id,login_name,
                              normalized_login_name,external_subject
                            ) VALUES(
                              :account_id,:provider_id,:login,:login,:subject
                            ) RETURNING id
                            """
                        ),
                        {
                            "account_id": account_id,
                            "provider_id": provider_id,
                            "login": login,
                            "subject": f"local:{login}",
                        },
                    )
                ).scalar_one()
            )
        principal = SecurityPrincipal(
            account_id,
            identity_id,
            login,
            "平台部",
            "operator",
        )
        stored = await repository.register(
            principal=principal,
            filename="phones.csv",
            source_size=12,
            expire_hours=6,
            ip="10.0.0.8",
        )
        import_ids.append(stored.import_id)
        assert stored.status == "staging" and stored.source_file is not None
        await repository.attach_source(
            UUID(stored.import_id),
            stored.source_file,
        )

        first = await repository.claim_parse(stored.import_id)
        assert first is not None
        assert await repository.append_parse_batch(
            first,
            (ImportPhone(b"first", "a" * 64, "138****8000", 1, 2),),
        )
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE import_task
                    SET parse_lease_expires_at=now()-interval '1 second'
                    WHERE import_id=CAST(:import_id AS uuid)
                    """
                ),
                {"import_id": stored.import_id},
            )
        second = await repository.claim_parse(stored.import_id)
        assert second is not None and second.lease_id != first.lease_id
        assert await repository.append_parse_batch(
            second,
            (ImportPhone(b"second", "b" * 64, "139****9000", 1, 3),),
        )
        assert not await repository.finish_parse(
            first,
            valid=1,
            invalid=0,
            duplicate=0,
            blacklisted=0,
            invalid_file=None,
        )
        assert await repository.finish_parse(
            second,
            valid=1,
            invalid=0,
            duplicate=0,
            blacklisted=0,
            invalid_file=None,
        )
        ready = await repository.get_status(stored.import_id, principal=principal)
        assert ready is not None and ready.status == "ready" and ready.valid == 1
        async with engine.connect() as connection:
            payloads = list(
                (
                    await connection.execute(
                        text(
                            """
                            SELECT phone_enc FROM import_phone
                            WHERE import_task_id=(
                              SELECT id FROM import_task
                              WHERE import_id=CAST(:import_id AS uuid)
                            )
                            """
                        ),
                        {"import_id": stored.import_id},
                    )
                ).scalars()
            )
        assert payloads == [b"second"]

        exhausted = await repository.register(
            principal=principal,
            filename="exhausted.csv",
            source_size=12,
            expire_hours=6,
            ip="10.0.0.8",
        )
        import_ids.append(exhausted.import_id)
        assert exhausted.source_file is not None
        await repository.attach_source(
            UUID(exhausted.import_id),
            exhausted.source_file,
        )
        claim = await repository.claim_parse(exhausted.import_id)
        assert claim is not None
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE import_task SET parse_attempts=3,
                      parse_lease_expires_at=now()-interval '1 second'
                    WHERE import_id=CAST(:import_id AS uuid)
                    """
                ),
                {"import_id": exhausted.import_id},
            )
        assert exhausted.import_id not in await repository.pending_parse_ids()
        failed = await repository.get_status(exhausted.import_id, principal=principal)
        assert failed is not None
        assert failed.status == "failed"
        assert failed.error == "IMPORT_PARSE_FAILED"
    finally:
        await cleanup()
        await engine.dispose()
