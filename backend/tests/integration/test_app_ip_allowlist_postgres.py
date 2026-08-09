"""应用 IP 白名单在真实 PostgreSQL 上的持久化、轮换与认证强制。"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.apikey import ApiKeyAuthenticator, InvalidApiKey, SqlApiKeyRepository
from app.services.app_repository import SqlAppRepository

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@pytest.mark.asyncio
async def test_allowed_ips_persist_and_rotate_grace_revoke_disable_are_enforced() -> None:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    settings = cast(
        Any,
        SimpleNamespace(
            database_url=database_url,
            database_url_for=lambda _role: database_url,
        ),
    )
    engine = create_async_engine(database_url)
    repository = SqlAppRepository(settings)
    key_repository = SqlApiKeyRepository(settings)
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    nonce = uuid4().hex
    key1 = f"key-one-{nonce}-with-enough-entropy"
    key2 = f"key-two-{nonce}-with-enough-entropy"
    app_id: int | None = None

    try:
        app_id = await repository.create(
            name=f"ip-allow-{nonce}",
            dept="测试部",
            api_key_hash=digest(key1),
            api_key_prefix=key1[:8],
            allowed_categories="notice",
            default_sign=None,
            daily_quota=0,
            rate_limit_per_min=60,
            blacklist_check=True,
            freq_override=None,
            allowed_ips=("10.0.0.0/8", "2001:db8::/32"),
            callback_url=None,
            callback_secret_enc=None,
            callback_report_enabled=False,
            actor="integration",
            ip="127.0.0.1",
        )

        row = await repository.get(app_id)
        assert row is not None
        assert row["allowed_ips"] == ["10.0.0.0/8", "2001:db8::/32"]
        assert "api_key_hash" not in row
        assert "callback_secret_enc" not in row

        authenticator = ApiKeyAuthenticator(key_repository)
        context = await authenticator.authenticate(key1)
        assert context.app_id == app_id
        assert context.allowed_ips == ("10.0.0.0/8", "2001:db8::/32")

        await repository.rotate_key(
            app_id,
            api_key_hash=digest(key2),
            api_key_prefix=key2[:8],
            old_key_expires_at=now + timedelta(hours=72),
            actor="integration",
            ip="127.0.0.1",
        )
        assert (await authenticator.authenticate(key1)).app_id == app_id
        assert (await authenticator.authenticate(key2)).app_id == app_id

        expired = ApiKeyAuthenticator(
            key_repository,
            clock=lambda: now + timedelta(hours=72),
        )
        with pytest.raises(InvalidApiKey):
            await expired.authenticate(key1)

        await repository.revoke_old_key(app_id, actor="integration", ip="127.0.0.1")
        with pytest.raises(InvalidApiKey):
            await authenticator.authenticate(key1)
        assert (await authenticator.authenticate(key2)).app_id == app_id

        await repository.disable(app_id, actor="integration", ip="127.0.0.1")
        with pytest.raises(InvalidApiKey):
            await authenticator.authenticate(key2)

        await repository.update(
            app_id,
            dept="测试部",
            allowed_categories="notice",
            default_sign=None,
            daily_quota=0,
            rate_limit_per_min=60,
            blacklist_check=True,
            freq_override=None,
            allowed_ips=("203.0.113.0/24",),
            callback_url=None,
            callback_report_enabled=False,
            status=1,
            actor="integration",
            ip="127.0.0.1",
        )
        assert (await authenticator.authenticate(key2)).app_id == app_id
    finally:
        if app_id is not None:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "DELETE FROM audit_log WHERE object_type='app' "
                        "AND object_id=CAST(:app_id AS text)"
                    ),
                    {"app_id": str(app_id)},
                )
                await connection.execute(
                    text("DELETE FROM app WHERE id=:app_id"),
                    {"app_id": app_id},
                )
        await engine.dispose()


@pytest.mark.asyncio
async def test_callback_url_change_quarantines_queued_task_in_same_transaction() -> None:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    settings = cast(
        Any,
        SimpleNamespace(
            database_url=database_url,
            database_url_for=lambda _role: database_url,
        ),
    )
    engine = create_async_engine(database_url)
    repository = SqlAppRepository(settings)
    nonce = uuid4().hex
    key = f"callback-{nonce}-with-enough-entropy"
    secret = b"\x00\x01encrypted-callback-secret"
    app_id: int | None = None
    task_id: int | None = None

    try:
        app_id = await repository.create(
            name=f"callback-revoke-{nonce}",
            dept="测试部",
            api_key_hash=digest(key),
            api_key_prefix=key[:8],
            allowed_categories="notice",
            default_sign=None,
            daily_quota=0,
            rate_limit_per_min=60,
            blacklist_check=True,
            freq_override=None,
            allowed_ips=(),
            callback_url="https://callback-old.internal/hook",
            callback_secret_enc=secret,
            callback_report_enabled=True,
            actor="integration",
            ip="127.0.0.1",
        )
        async with engine.begin() as connection:
            task_id = int(
                (
                    await connection.execute(
                        text(
                            """
                            INSERT INTO callback_task(
                              app_id,event,url,callback_secret_enc,
                              callback_secret_key_version
                            ) VALUES (
                              :app_id,'message.report',:url,:secret,1
                            ) RETURNING id
                            """
                        ),
                        {
                            "app_id": app_id,
                            "url": "https://callback-old.internal/hook",
                            "secret": secret,
                        },
                    )
                ).scalar_one()
            )

        await repository.update(
            app_id,
            dept="测试部",
            allowed_categories="notice",
            default_sign=None,
            daily_quota=0,
            rate_limit_per_min=60,
            blacklist_check=True,
            freq_override=None,
            allowed_ips=(),
            callback_url="https://callback-new.internal/hook",
            callback_report_enabled=True,
            status=1,
            actor="integration",
            ip="127.0.0.1",
        )

        async with engine.connect() as connection:
            task = (
                await connection.execute(
                    text(
                        """
                        SELECT status,last_error,lease_id,lease_expires_at
                        FROM callback_task WHERE id=:task_id
                        """
                    ),
                    {"task_id": task_id},
                )
            ).mappings().one()
        assert dict(task) == {
            "status": "dead",
            "last_error": "CallbackConfigRevoked",
            "lease_id": None,
            "lease_expires_at": None,
        }
    finally:
        if app_id is not None:
            async with engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM callback_task WHERE app_id=:app_id"),
                    {"app_id": app_id},
                )
                await connection.execute(
                    text(
                        "DELETE FROM audit_log WHERE object_type='app' "
                        "AND object_id=CAST(:app_id AS text)"
                    ),
                    {"app_id": str(app_id)},
                )
                await connection.execute(
                    text("DELETE FROM app WHERE id=:app_id"),
                    {"app_id": app_id},
                )
        await engine.dispose()
