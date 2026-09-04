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
    UnknownDigestAlgorithmError,
    UnknownPepperVersionError,
    _digest_matches,
    derive_legacy_data_hmac_pepper,
    hash_api_key,
    issue_api_key_digest,
    issue_api_key_record,
    load_api_key_pepper_keyring,
    parse_unclassified_algorithms,
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
    assert _digest_matches(
        key, digest, version, algorithm="api_pepper", settings=upgraded
    )
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
    assert _digest_matches(
        key, digest, 1, algorithm="api_pepper", settings=rotated
    )
    new_digest, new_version = issue_api_key_digest(key, settings=rotated)
    assert new_version == 2
    assert new_digest != digest
    assert not _digest_matches(
        key, digest, 2, algorithm="api_pepper", settings=rotated
    )


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
                    current_hash_algorithm="api_pepper",
                    previous_hash_algorithm="api_pepper",
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
        _digest_matches(key, digest, 9, algorithm="api_pepper", settings=settings)
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
                    current_hash_algorithm="api_pepper",
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
                    current_hash_algorithm="api_pepper",
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


def test_null_algorithm_is_not_silently_treated_as_sha256(tmp_path: Path) -> None:
    settings = settings_with_secrets(tmp_path, api_key_pepper_key=b64(b"p"))
    key = "current_key_with_enough_entropy_123"
    legacy = hashlib.sha256(key.encode()).hexdigest()
    assert not _digest_matches(key, legacy, None, settings=settings)
    assert _digest_matches(
        key, legacy, None, algorithm="legacy_sha256", settings=settings
    )
    modern, version = issue_api_key_digest(key, settings=settings)
    assert version == 1
    assert not _digest_matches(key, modern, None, settings=settings)
    assert _digest_matches(
        key, modern, version, algorithm="api_pepper", settings=settings
    )


def test_unclassified_candidates_are_limited_to_deploy_inventory(tmp_path: Path) -> None:
    settings = settings_with_secrets(tmp_path, api_key_pepper_key=b64(b"p"))
    key = "current_key_with_enough_entropy_123"
    legacy = hashlib.sha256(key.encode()).hexdigest()
    assert parse_unclassified_algorithms("") == ()
    assert parse_unclassified_algorithms("legacy_sha256") == ("legacy_sha256",)
    with pytest.raises(UnknownDigestAlgorithmError):
        parse_unclassified_algorithms("api_pepper")
    assert _digest_matches(
        key,
        legacy,
        None,
        unclassified_candidates=("legacy_sha256",),
        settings=settings,
    )
    assert not _digest_matches(
        key,
        legacy,
        None,
        unclassified_candidates=("legacy_data_hmac_pepper_v1",),
        settings=settings,
    )


def test_legacy_data_hmac_pepper_uses_independent_credential(tmp_path: Path) -> None:
    old_hmac = b64(b"h")
    settings = settings_with_secrets(
        tmp_path,
        api_key_pepper_key=b64(b"p"),
        api_key_legacy_hmac_pepper=old_hmac,
        data_hmac_key=keyring(2, {1: b64(b"h"), 2: b64(b"H")}),
    )
    key = "current_key_with_enough_entropy_123"
    digest = hmac.new(
        derive_legacy_data_hmac_pepper(old_hmac),
        key.encode(),
        hashlib.sha256,
    ).hexdigest()
    assert _digest_matches(
        key,
        digest,
        None,
        algorithm="legacy_data_hmac_pepper_v1",
        settings=settings,
    )
    current_json = settings.credential("data_hmac_key")
    wrong = hmac.new(
        derive_legacy_data_hmac_pepper(current_json),
        key.encode(),
        hashlib.sha256,
    ).hexdigest()
    assert digest != wrong
    issued = issue_api_key_record(key, settings=settings)
    assert issued.algorithm == "api_pepper"
    assert issued.pepper_version == 1


def test_migration_classifies_versioned_rows_without_guessing_sha256() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "migrations/versions/0091_api_key_digest_algorithms.py"
    ).read_text(encoding="utf-8")
    assert "SET api_key_hash_algorithm='api_pepper'" in source
    assert "api_key_hash_version IS NOT NULL" in source
    assert "api_key_hash_algorithm IS NULL" in source
    assert "legacy_sha256" in source
    assert "不得" in source or "禁止" in source


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


@pytest.mark.asyncio
async def test_on_auth_rehash_uses_cas_and_keeps_auth_on_migrate_failure(
    tmp_path: Path,
) -> None:
    settings = settings_with_secrets(tmp_path, api_key_pepper_key=b64(b"p"))
    key = "current_key_with_enough_entropy_123"
    repo = _FakeRepo(
        [
            ApiKeyCandidate(
                app_id=11,
                name="app-legacy",
                dept="平台部",
                allowed_categories="notice",
                current_hash=hashlib.sha256(key.encode()).hexdigest(),
                previous_hash=None,
                previous_expires_at=None,
                current_hash_algorithm="legacy_sha256",
            )
        ]
    )
    auth = ApiKeyAuthenticator(repo, settings=settings)
    assert (await auth.authenticate(key)).app_id == 11
    assert repo.migrations
    issued = issue_api_key_record(key, settings=settings)
    assert repo.migrations[0]["issued"].algorithm == "api_pepper"
    assert repo.migrations[0]["issued"].digest == issued.digest

    failing = _FakeRepo(
        [
            ApiKeyCandidate(
                app_id=12,
                name="app-fail",
                dept="平台部",
                allowed_categories="notice",
                current_hash=hashlib.sha256(key.encode()).hexdigest(),
                previous_hash=None,
                previous_expires_at=None,
                current_hash_algorithm="legacy_sha256",
            )
        ],
        migrate_error=RuntimeError("cas lost"),
    )
    assert (
        await ApiKeyAuthenticator(failing, settings=settings).authenticate(key)
    ).app_id == 12


class _FakeRepo:
    def __init__(
        self,
        candidates: list[ApiKeyCandidate],
        *,
        migrate_error: Exception | None = None,
        unclassified: tuple[str, ...] = (),
    ) -> None:
        self.candidates = candidates
        self.migrations: list[dict[str, object]] = []
        self.migrate_error = migrate_error
        self.unclassified = unclassified

    async def find_candidates(self, prefix: str) -> list[ApiKeyCandidate]:
        return self.candidates

    async def unclassified_algorithms(self) -> tuple[str, ...]:
        return self.unclassified

    async def migrate_digest(self, **payload: object) -> None:
        if self.migrate_error is not None:
            raise self.migrate_error
        self.migrations.append(payload)
