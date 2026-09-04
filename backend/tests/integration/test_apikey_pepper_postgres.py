"""独立 API Key pepper 在真实 PostgreSQL 上的创建、轮换、legacy 与就绪检查。"""

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

from app.core.apikey import (
    ApiKeyAuthenticator,
    InvalidApiKey,
    SqlApiKeyRepository,
    issue_api_key_digest,
)
from app.core.health import ApiKeyPepperReferenceCheck
from app.services.app_repository import SqlAppRepository
from tests.test_apikey_pepper import b64, keyring, settings_with_secrets

pytestmark = pytest.mark.skipif(
    "OUTBOX_POSTGRES_DSN" not in os.environ,
    reason="requires isolated migrated PostgreSQL",
)


class _ReferenceSettings:
    def __init__(self, inner: Any, database_url: Any) -> None:
        self._inner = inner
        self._database_url = database_url

    def credential(self, name: str) -> str:
        return self._inner.credential(name)

    def database_url_for(self, _role: str) -> Any:
        return self._database_url


async def _insert_app(
    repository: SqlAppRepository,
    *,
    key: str,
    digest: str,
    version: int | None,
    name: str,
) -> int:
    return await repository.create(
        name=name,
        dept="测试部",
        api_key_hash=digest,
        api_key_prefix=key[:8],
        api_key_hash_version=version,
        allowed_categories="notice",
        default_sign=None,
        daily_quota=0,
        rate_limit_per_min=60,
        blacklist_check=True,
        freq_override=None,
        allowed_ips=("10.0.0.0/8",),
        callback_url=None,
        callback_secret_enc=None,
        callback_report_enabled=False,
        actor="integration",
        ip="127.0.0.1",
    )


@pytest.mark.asyncio
async def test_pepper_bound_key_survives_data_hmac_rotation_and_legacy_sha256(
    tmp_path,
) -> None:
    database_url = make_url(os.environ["OUTBOX_POSTGRES_DSN"])
    pepper_v1 = settings_with_secrets(tmp_path / "v1", api_key_pepper_key=b64(b"p"))
    hmac_rotated = settings_with_secrets(
        tmp_path / "hmac",
        data_hmac_key=keyring(2, {1: b64(b"h"), 2: b64(b"H")}),
        api_key_pepper_key=b64(b"p"),
    )
    pepper_rotated = settings_with_secrets(
        tmp_path / "pepper",
        api_key_pepper_key=keyring(2, {1: b64(b"p"), 2: b64(b"q")}),
    )
    missing_history = settings_with_secrets(
        tmp_path / "missing",
        api_key_pepper_key=b64(b"q"),
    )
    db_settings = cast(
        Any,
        SimpleNamespace(
            database_url=database_url,
            database_url_for=lambda _role: database_url,
        ),
    )
    engine = create_async_engine(database_url)
    repository = SqlAppRepository(db_settings)
    key_repository = SqlApiKeyRepository(db_settings)
    nonce = uuid4().hex
    modern_key = f"modern-{nonce}-with-enough-entropy"
    legacy_key = f"legacy-{nonce}-with-enough-entropy"
    rotated_key = f"rotated-{nonce}-with-enough-entropy"
    digest_v1, version_v1 = issue_api_key_digest(modern_key, settings=pepper_v1)
    digest_v2, version_v2 = issue_api_key_digest(rotated_key, settings=pepper_rotated)
    assert version_v1 == 1
    assert version_v2 == 2
    app_ids: list[int] = []
    now = datetime.now(UTC).replace(second=0, microsecond=0)

    try:
        app_id = await _insert_app(
            repository,
            key=modern_key,
            digest=digest_v1,
            version=version_v1,
            name=f"pepper-{nonce}",
        )
        app_ids.append(app_id)
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT api_key_hash_version, api_key_prev_hash_version
                        FROM app WHERE id=:app_id
                        """
                    ),
                    {"app_id": app_id},
                )
            ).mappings().one()
        assert row["api_key_hash_version"] == 1
        assert row["api_key_prev_hash_version"] is None

        hmac_auth = ApiKeyAuthenticator(key_repository, settings=hmac_rotated)
        assert (await hmac_auth.authenticate(modern_key)).app_id == app_id

        await repository.rotate_key(
            app_id,
            api_key_hash=digest_v2,
            api_key_prefix=rotated_key[:8],
            api_key_hash_version=version_v2,
            old_key_expires_at=now + timedelta(hours=72),
            actor="integration",
            ip="127.0.0.1",
        )
        pepper_auth = ApiKeyAuthenticator(key_repository, settings=pepper_rotated)
        assert (await pepper_auth.authenticate(modern_key)).app_id == app_id
        assert (await pepper_auth.authenticate(rotated_key)).app_id == app_id

        await repository.revoke_old_key(app_id, actor="integration", ip="127.0.0.1")
        with pytest.raises(InvalidApiKey):
            await pepper_auth.authenticate(modern_key)

        legacy_id = await _insert_app(
            repository,
            key=legacy_key,
            digest=hashlib.sha256(legacy_key.encode()).hexdigest(),
            version=None,
            name=f"legacy-{nonce}",
        )
        app_ids.append(legacy_id)
        assert (await pepper_auth.authenticate(legacy_key)).app_id == legacy_id

        await ApiKeyPepperReferenceCheck(
            _ReferenceSettings(pepper_rotated, database_url)
        )()
        with pytest.raises(RuntimeError, match="unavailable"):
            await ApiKeyPepperReferenceCheck(
                _ReferenceSettings(missing_history, database_url)
            )()
    finally:
        async with engine.begin() as connection:
            if app_ids:
                await connection.execute(
                    text("DELETE FROM app WHERE name LIKE :prefix"),
                    {"prefix": f"%{nonce}%"},
                )
        await engine.dispose()
