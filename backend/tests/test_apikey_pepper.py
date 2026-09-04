"""API Key pepper 必须独立于 data_hmac_key，并按记录版本验证。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core import health
from app.core.apikey import (
    ApiKeyAuthenticator,
    ApiKeyCandidate,
    InvalidApiKey,
    UnknownPepperVersionError,
    _digest_matches,
    hash_api_key,
    issue_api_key_digest,
    load_api_key_pepper_keyring,
)
from app.settings import Settings


def b64(byte: bytes) -> str:
    return base64.b64encode(byte * 32).decode("ascii")


def keyring(active: int, keys: dict[int, str]) -> str:
    return json.dumps(
        {"active_version": active, "keys": {str(version): key for version, key in keys.items()}}
    )


def settings_with_secrets(tmp_path: Path, **secrets: str) -> Settings:
    tmp_path.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for name, value in secrets.items():
        path = tmp_path / name
        path.write_text(value, encoding="utf-8")
        paths[name] = path
    kwargs: dict[str, object] = {
        "_env_file": None,
        "environment": "test",
        "debug": True,
        "auth_mock": True,
        "vendor_mock": True,
    }
    for name, path in paths.items():
        kwargs[f"{name}_file"] = path
    return Settings(**kwargs)


def test_data_hmac_keyring_upgrade_must_not_change_api_key_digest(tmp_path: Path) -> None:
    bare_hmac = b64(b"h")
    pepper = b64(b"p")
    aes = b64(b"a")
    key = "current_key_with_enough_entropy_123"
    first = settings_with_secrets(
        tmp_path / "v1",
        data_aes_key=aes,
        data_hmac_key=bare_hmac,
        api_key_pepper_key=pepper,
    )
    digest, version = issue_api_key_digest(key, settings=first)

    upgraded = settings_with_secrets(
        tmp_path / "v2",
        data_aes_key=keyring(2, {1: aes, 2: b64(b"A")}),
        data_hmac_key=keyring(2, {1: bare_hmac, 2: b64(b"H")}),
        api_key_pepper_key=pepper,
    )
    assert issue_api_key_digest(key, settings=upgraded) == (digest, version)
    assert _digest_matches(key, digest, version, settings=upgraded)
    assert hash_api_key(key, settings=upgraded) != hashlib.sha256(key.encode()).hexdigest()
    assert first.credential("data_hmac_key") != upgraded.credential("data_hmac_key")


def test_api_key_pepper_rotation_keeps_version_bound_digests(tmp_path: Path) -> None:
    key = "current_key_with_enough_entropy_123"
    v1 = settings_with_secrets(tmp_path / "p1", api_key_pepper_key=b64(b"p"))
    digest, version = issue_api_key_digest(key, settings=v1)
    assert version == 1

    rotated = settings_with_secrets(
        tmp_path / "p2",
        api_key_pepper_key=keyring(2, {1: b64(b"p"), 2: b64(b"q")}),
    )
    assert _digest_matches(key, digest, 1, settings=rotated)
    new_digest, new_version = issue_api_key_digest(key, settings=rotated)
    assert new_version == 2
    assert new_digest != digest
    assert not _digest_matches(key, digest, 2, settings=rotated)


@pytest.mark.asyncio
async def test_current_and_previous_keys_can_use_different_pepper_versions(
    tmp_path: Path,
) -> None:
    current = "current_key_with_enough_entropy_123"
    previous = "previous_key_with_enough_entropy"
    settings = settings_with_secrets(
        tmp_path,
        api_key_pepper_key=keyring(2, {1: b64(b"p"), 2: b64(b"q")}),
    )
    current_digest = hmac.new(b"q" * 32, current.encode(), hashlib.sha256).hexdigest()
    previous_digest = hmac.new(b"p" * 32, previous.encode(), hashlib.sha256).hexdigest()
    now = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
    auth = ApiKeyAuthenticator(
        _FakeRepo(
            [
                ApiKeyCandidate(
                    app_id=9,
                    name="app-pepper",
                    dept="平台部",
                    allowed_categories="notice",
                    current_hash=current_digest,
                    previous_hash=previous_digest,
                    previous_expires_at=now + timedelta(hours=1),
                    current_hash_version=2,
                    previous_hash_version=1,
                )
            ]
        ),
        clock=lambda: now,
        settings=settings,
    )

    assert (await auth.authenticate(current)).app_id == 9
    assert (await auth.authenticate(previous)).app_id == 9


def test_unknown_pepper_version_fail_closed_without_leaking_secret(tmp_path: Path) -> None:
    settings = settings_with_secrets(tmp_path, api_key_pepper_key=b64(b"p"))
    key = "current_key_with_enough_entropy_123"
    digest, _version = issue_api_key_digest(key, settings=settings)
    with pytest.raises(UnknownPepperVersionError) as error:
        _digest_matches(key, digest, 9, settings=settings)
    leaked = f"{error.value!r} {load_api_key_pepper_keyring(settings)!r}"
    assert b64(b"p") not in leaked
    assert digest not in leaked
    assert "version 9" not in leaked


@pytest.mark.asyncio
async def test_authenticate_keeps_working_after_data_hmac_keyring_upgrade(
    tmp_path: Path,
) -> None:
    key = "current_key_with_enough_entropy_123"
    created = settings_with_secrets(
        tmp_path / "create",
        data_hmac_key=b64(b"h"),
        api_key_pepper_key=b64(b"p"),
    )
    digest, version = issue_api_key_digest(key, settings=created)
    upgraded = settings_with_secrets(
        tmp_path / "upgrade",
        data_hmac_key=keyring(2, {1: b64(b"h"), 2: b64(b"H")}),
        api_key_pepper_key=b64(b"p"),
    )
    auth = ApiKeyAuthenticator(
        _FakeRepo(
            [
                ApiKeyCandidate(
                    app_id=3,
                    name="app-hmac",
                    dept="平台部",
                    allowed_categories="notice",
                    current_hash=digest,
                    previous_hash=None,
                    previous_expires_at=None,
                    current_hash_version=version,
                )
            ]
        ),
        settings=upgraded,
    )
    assert (await auth.authenticate(key)).app_id == 3


@pytest.mark.asyncio
async def test_unknown_pepper_version_authenticate_is_unauthorized_without_leak(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = settings_with_secrets(tmp_path, api_key_pepper_key=b64(b"p"))
    key = "current_key_with_enough_entropy_123"
    digest, _version = issue_api_key_digest(key, settings=settings)
    auth = ApiKeyAuthenticator(
        _FakeRepo(
            [
                ApiKeyCandidate(
                    app_id=4,
                    name="app-missing",
                    dept="平台部",
                    allowed_categories="notice",
                    current_hash=digest,
                    previous_hash=None,
                    previous_expires_at=None,
                    current_hash_version=9,
                )
            ]
        ),
        settings=settings,
    )
    with pytest.raises(InvalidApiKey) as error:
        await auth.authenticate(key)
    leaked = f"{error.value!r} {caplog.text}"
    assert b64(b"p") not in leaked
    assert digest not in leaked
    assert "app-missing" not in leaked


def test_legacy_sha256_digest_still_matches_when_version_is_null(tmp_path: Path) -> None:
    settings = settings_with_secrets(tmp_path, api_key_pepper_key=b64(b"p"))
    key = "current_key_with_enough_entropy_123"
    legacy = hashlib.sha256(key.encode()).hexdigest()
    assert _digest_matches(key, legacy, None, settings=settings)
    modern, version = issue_api_key_digest(key, settings=settings)
    assert version == 1
    assert not _digest_matches(key, modern, None, settings=settings)


def test_readiness_parses_pepper_keyring_without_exposing_value(tmp_path: Path) -> None:
    encoded = b64(b"k")
    system = b64(b"s")
    pepper = b64(b"p")
    values = {
        "db_accept_password": "db-password",
        "data_aes_key": encoded,
        "data_hmac_key": encoded,
        "api_key_pepper_key": pepper,
        "audit_context_key": encoded,
        "audit_system_api_context_key": system,
        "alert_credential_public_key": encoded,
        "jwt_secret": "jwt-key",
        "ldap_bind_password": "ldap-password",
    }
    settings = settings_with_secrets(tmp_path, **values)
    assert health._validate_runtime_secrets(settings) is None


class _FakeRepo:
    def __init__(self, candidates: list[ApiKeyCandidate]) -> None:
        self.candidates = candidates

    async def find_candidates(self, prefix: str) -> list[ApiKeyCandidate]:
        return self.candidates
