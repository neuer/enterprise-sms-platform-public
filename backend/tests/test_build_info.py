from __future__ import annotations

from pathlib import Path

import pytest

from app.build_info import APP_VERSION, current_build_info

ROOT = Path(__file__).resolve().parents[2]


def test_build_info_uses_repository_version_and_safe_development_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("APP_VERSION", "GIT_SHA", "SCHEMA_REVISION", "IMAGE_DIGEST"):
        monkeypatch.delenv(name, raising=False)

    info = current_build_info()

    assert (ROOT / "VERSION").read_text(encoding="ascii").strip() == APP_VERSION
    assert info.app_version == APP_VERSION
    assert info.git_sha == "development"
    assert info.schema_revision == "development"
    assert info.image_digest == "development"


def test_build_info_accepts_exact_release_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_VERSION", APP_VERSION)
    monkeypatch.setenv("GIT_SHA", "c" * 40)
    monkeypatch.setenv("SCHEMA_REVISION", "0032_async_import_runtime")
    monkeypatch.setenv(
        "IMAGE_DIGEST",
        "registry.example.com/sms/api@sha256:" + "d" * 64,
    )

    info = current_build_info()

    assert info.git_sha == "c" * 40
    assert info.schema_revision == "0032_async_import_runtime"
    assert info.image_digest.endswith("d" * 64)


def test_build_info_accepts_local_tag_only_for_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE_DIGEST", "sms-platform-api:local")

    assert current_build_info().image_digest == "sms-platform-api:local"

    monkeypatch.setenv("GIT_SHA", "c" * 40)
    monkeypatch.setenv("SCHEMA_REVISION", "0032_async_import_runtime")
    with pytest.raises(RuntimeError, match="IMAGE_DIGEST"):
        current_build_info()


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("APP_VERSION", "1.5.0", "APP_VERSION"),
        ("GIT_SHA", "latest", "GIT_SHA"),
        ("SCHEMA_REVISION", "head", "SCHEMA_REVISION"),
        ("IMAGE_DIGEST", "not a safe image identity", "IMAGE_DIGEST"),
    ],
)
def test_build_info_rejects_unbound_or_mismatched_identity(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
    message: str,
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(RuntimeError, match=message):
        current_build_info()
