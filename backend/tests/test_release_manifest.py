from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))

from release_manifest import (  # noqa: E402
    MigrationCompatibility,
    ReleaseManifestError,
    load_manifest,
    validate_changed_images,
)


def _manifest(
    *,
    mode: str = "development",
    release_gate_kind: str = "release",
) -> dict[str, Any]:
    digest = "a" * 64
    archive = "b" * 64
    if mode == "production":
        refs = {
            name: f"registry.example.com/sms/{name}@sha256:{digest}"
            for name in ("api", "web", "postgres", "redis")
        }
    else:
        refs = {
            name: f"sms-platform-{name}:amd64-b9eabc0"
            for name in ("api", "web", "postgres", "redis")
        }
    return {
        "schema_version": 1,
        "release_id": "20260714-b9eabc0",
        "commit": "b9eabc0e64cff67d7512a5e3f86db7d93d50b3a0",
        "mode": mode,
        "images": {
            name: {
                "ref": ref,
                "id": f"sha256:{digest}",
                "archive_file": "web.tar" if mode == "development" and name == "web" else None,
                "archive_sha256": archive if mode == "development" and name == "web" else None,
                "changed": name == "web",
            }
            for name, ref in refs.items()
        },
        "migration": {"from": "0011", "target": "0012", "compatibility": "expand"},
        "evidence": {
            "release_gate_kind": release_gate_kind,
            "release_gate": "release-gate.json",
            "release_gate_sha256": "c" * 64,
            "data_images": None,
            "backup_restore_change": None,
        },
    }


def _write_manifest(tmp_path: Path, payload: dict[str, Any]) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_manifest_accepts_exact_four_image_schema(tmp_path: Path) -> None:
    manifest = load_manifest(_write_manifest(tmp_path, _manifest()))

    assert manifest.schema_version == 1
    assert manifest.release_id == "20260714-b9eabc0"
    assert manifest.migration_compatibility is MigrationCompatibility.EXPAND
    assert tuple(manifest.images) == ("api", "web", "postgres", "redis")
    assert manifest.images["web"].changed is True


def test_manifest_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(
        '{"schema_version":1,"schema_version":1}',
        encoding="utf-8",
    )

    with pytest.raises(ReleaseManifestError, match="duplicate"):
        load_manifest(path)


@pytest.mark.parametrize(
    ("location", "field"),
    [
        ((), "unexpected"),
        (("images", "api"), "platform"),
        (("migration",), "downgrade"),
        (("evidence",), "notes"),
    ],
)
def test_manifest_rejects_unknown_fields(
    tmp_path: Path,
    location: tuple[str, ...],
    field: str,
) -> None:
    payload = _manifest()
    target: dict[str, Any] = payload
    for part in location:
        target = target[part]
    target[field] = "not-allowed"

    with pytest.raises(ReleaseManifestError, match="unknown"):
        load_manifest(_write_manifest(tmp_path, payload))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("release_id",), "release;touch-pwned"),
        (("release_id",), "../release"),
        (("images", "api", "ref"), "sms-api:tag$(id)"),
        (("images", "api", "ref"), "../sms-api:tag"),
        (("evidence", "release_gate"), "../../release-gate.json"),
        (("migration", "target"), "0012;shutdown"),
    ],
)
def test_manifest_rejects_shell_metacharacters_and_traversal(
    tmp_path: Path,
    path: tuple[str, ...],
    value: str,
) -> None:
    payload = deepcopy(_manifest())
    target: dict[str, Any] = payload
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value

    with pytest.raises(ReleaseManifestError):
        load_manifest(_write_manifest(tmp_path, payload))


def test_production_requires_repo_digest_references(tmp_path: Path) -> None:
    payload = _manifest(mode="production")
    payload["images"]["api"]["ref"] = "registry.example.com/sms/api:latest"

    with pytest.raises(ReleaseManifestError, match="RepoDigest"):
        load_manifest(_write_manifest(tmp_path, payload))


def test_development_accepts_validated_local_tags(tmp_path: Path) -> None:
    manifest = load_manifest(_write_manifest(tmp_path, _manifest(mode="development")))

    assert manifest.mode == "development"
    assert manifest.images["api"].ref == "sms-platform-api:amd64-b9eabc0"
    assert manifest.images["web"].archive_file == "web.tar"
    assert manifest.images["web"].archive_sha256 == "b" * 64


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("archive_file", None),
        ("archive_sha256", None),
    ],
)
def test_development_changed_image_requires_both_archive_fields(
    tmp_path: Path,
    field: str,
    value: None,
) -> None:
    payload = _manifest()
    payload["images"]["web"][field] = value

    with pytest.raises(ReleaseManifestError, match="archive"):
        load_manifest(_write_manifest(tmp_path, payload))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("archive_file", "api.tar"),
        ("archive_sha256", "b" * 64),
    ],
)
def test_development_unchanged_image_rejects_archive_fields(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    payload = _manifest()
    payload["images"]["api"][field] = value

    with pytest.raises(ReleaseManifestError, match="archive"):
        load_manifest(_write_manifest(tmp_path, payload))


def test_production_rejects_all_archive_fields(tmp_path: Path) -> None:
    payload = _manifest(mode="production")
    payload["images"]["web"]["archive_file"] = "web.tar"

    with pytest.raises(ReleaseManifestError, match="production.*archive"):
        load_manifest(_write_manifest(tmp_path, payload))


def test_data_evidence_is_required_only_for_changed_data_images(tmp_path: Path) -> None:
    payload = _manifest()
    payload["images"]["postgres"]["changed"] = True
    payload["images"]["postgres"]["archive_file"] = "postgres.tar"
    payload["images"]["postgres"]["archive_sha256"] = "c" * 64

    with pytest.raises(ReleaseManifestError, match="data_images"):
        load_manifest(_write_manifest(tmp_path, payload))

    payload["evidence"]["data_images"] = "data-images.json"
    load_manifest(_write_manifest(tmp_path, payload))

    unchanged = _manifest()
    unchanged["evidence"]["data_images"] = "data-images.json"
    with pytest.raises(ReleaseManifestError, match="data_images"):
        load_manifest(_write_manifest(tmp_path, unchanged))


def test_production_postgres_change_requires_exact_backup_evidence(
    tmp_path: Path,
) -> None:
    payload = _manifest(mode="production")
    payload["images"]["postgres"]["changed"] = True
    payload["evidence"]["data_images"] = "data-images.json"

    with pytest.raises(ReleaseManifestError, match="backup_restore_change"):
        load_manifest(_write_manifest(tmp_path, payload))

    payload["evidence"]["backup_restore_change"] = {
        "record": "backup-change.json",
        "restore_report": "restore-report.json",
    }
    manifest = load_manifest(_write_manifest(tmp_path, payload))
    assert manifest.evidence["backup_restore_change"] == {
        "record": "backup-change.json",
        "restore_report": "restore-report.json",
    }

    missing = deepcopy(payload)
    del missing["evidence"]["backup_restore_change"]["restore_report"]
    with pytest.raises(ReleaseManifestError, match="missing"):
        load_manifest(_write_manifest(tmp_path, missing))

    payload["evidence"]["backup_restore_change"]["unknown"] = "extra.json"
    with pytest.raises(ReleaseManifestError, match="unknown"):
        load_manifest(_write_manifest(tmp_path, payload))


def test_backup_evidence_is_forbidden_outside_production_postgres_change(
    tmp_path: Path,
) -> None:
    payload = _manifest()
    payload["evidence"]["backup_restore_change"] = {
        "record": "backup-change.json",
        "restore_report": "restore-report.json",
    }

    with pytest.raises(ReleaseManifestError, match="backup_restore_change"):
        load_manifest(_write_manifest(tmp_path, payload))


def test_release_gate_kind_is_explicit_and_bound_in_manifest(tmp_path: Path) -> None:
    manifest = load_manifest(
        _write_manifest(
            tmp_path,
            _manifest(release_gate_kind="release"),
        )
    )

    assert manifest.evidence["release_gate_kind"] == "release"

    control_smoke = load_manifest(
        _write_manifest(
            tmp_path,
            _manifest(release_gate_kind="release_control_smoke"),
        )
    )
    assert control_smoke.evidence["release_gate_kind"] == "release_control_smoke"


def test_release_control_smoke_is_rejected_outside_development(tmp_path: Path) -> None:
    payload = _manifest(mode="production", release_gate_kind="release_control_smoke")

    with pytest.raises(ReleaseManifestError, match="release_gate_kind"):
        load_manifest(_write_manifest(tmp_path, payload))


@pytest.mark.parametrize(
    "unsafe_name",
    ["../web.tar", ".web.tar", "web..tar", "web.tar.part", "web\n.tar"],
)
def test_bundle_basenames_reject_unsafe_values(
    tmp_path: Path,
    unsafe_name: str,
) -> None:
    payload = _manifest()
    payload["images"]["web"]["archive_file"] = unsafe_name

    with pytest.raises(ReleaseManifestError):
        load_manifest(_write_manifest(tmp_path, payload))


@pytest.mark.parametrize("duplicate_name", ["manifest.json", "release-gate.json"])
def test_bundle_basenames_are_globally_unique(
    tmp_path: Path,
    duplicate_name: str,
) -> None:
    payload = _manifest()
    payload["images"]["web"]["archive_file"] = duplicate_name

    with pytest.raises(ReleaseManifestError, match="unique"):
        load_manifest(_write_manifest(tmp_path, payload))


def test_evidence_basenames_are_globally_unique(tmp_path: Path) -> None:
    payload = _manifest(mode="production")
    payload["images"]["postgres"]["changed"] = True
    payload["evidence"]["data_images"] = "release-gate.json"
    payload["evidence"]["backup_restore_change"] = {
        "record": "backup-change.json",
        "restore_report": "restore-report.json",
    }

    with pytest.raises(ReleaseManifestError, match="unique"):
        load_manifest(_write_manifest(tmp_path, payload))

    payload["evidence"]["data_images"] = "data-images.json"
    payload["evidence"]["backup_restore_change"]["restore_report"] = "backup-change.json"
    with pytest.raises(ReleaseManifestError, match="unique"):
        load_manifest(_write_manifest(tmp_path, payload))


def test_manual_migration_is_not_automatable(tmp_path: Path) -> None:
    payload = _manifest()
    payload["migration"]["compatibility"] = "manual"

    with pytest.raises(ReleaseManifestError, match="manual"):
        load_manifest(_write_manifest(tmp_path, payload))


@pytest.mark.parametrize(
    ("migration_from", "migration_target", "compatibility"),
    [
        ("0011", "0012", "none"),
        ("0011", "0011", "expand"),
    ],
)
def test_migration_compatibility_must_exactly_match_schema_change(
    tmp_path: Path,
    migration_from: str,
    migration_target: str,
    compatibility: str,
) -> None:
    payload = _manifest()
    payload["migration"] = {
        "from": migration_from,
        "target": migration_target,
        "compatibility": compatibility,
    }

    with pytest.raises(ReleaseManifestError, match="compatibility"):
        load_manifest(_write_manifest(tmp_path, payload))


def test_changed_flags_must_match_observed_current_refs(tmp_path: Path) -> None:
    manifest = load_manifest(_write_manifest(tmp_path, _manifest()))
    current_refs = {name: spec.ref for name, spec in manifest.images.items()}
    current_refs["web"] = "sms-platform-web:amd64-previous"

    validate_changed_images(manifest, current_refs)

    current_refs["api"] = "sms-platform-api:amd64-previous"
    with pytest.raises(ReleaseManifestError, match="changed"):
        validate_changed_images(manifest, current_refs)
