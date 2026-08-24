from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from create_release_manifest import (  # noqa: E402
    ManifestCreationError,
    create_manifest,
)


def _private_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return path


def _release_report(path: Path) -> Path:
    commit = "c" * 40
    return _private_json(
        path,
        {
            "schema_version": 1,
            "gate_type": "release",
            "candidate_commit": commit,
            "source": {
                "app_version": "1.6.0",
                "git_sha": commit,
                "schema_revision": "0032_async_import_runtime",
                "openapi_sha256": "9" * 64,
                "workflow_repository": "example/enterprise-sms-platform",
                "workflow_run_id": 123,
                "workflow_run_attempt": 1,
                "sbom_sha256": {
                    name: "8" * 64
                    for name in ("api", "web", "postgres", "redis")
                },
            },
            "generated_at": "2026-07-28T00:00:00Z",
            "trivy_image": "aquasec/trivy@sha256:" + "d" * 64,
            "images": {
                name: {
                    "ref": f"registry.example.com/sms/{name}@sha256:" + token * 64,
                    "image_id": "sha256:" + token * 64,
                    "repo_digests": [
                        f"registry.example.com/sms/{name}@sha256:" + token * 64
                    ],
                    "scan_report_sha256": "e" * 64,
                    "scan_passed": True,
                }
                for name, token in zip(
                    ("api", "web", "postgres", "redis"),
                    ("1", "2", "3", "4"),
                    strict=True,
                )
            },
            "promotion_source": {"report_sha256": "f" * 64},
            "passed": True,
        },
    )


def test_creates_self_validated_production_manifest(tmp_path: Path) -> None:
    bundle = tmp_path / "release"
    bundle.mkdir(mode=0o700)
    release_report = _release_report(bundle / "release-gate.json")
    output = bundle / "manifest.json"

    create_manifest(
        release_report=release_report,
        output=output,
        release_id="release-20260728",
        migration_from="0032_async_import_runtime",
        migration_target="0032_async_import_runtime",
        changed=frozenset({"api", "web"}),
    )

    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["mode"] == "production"
    assert manifest["commit"] == "c" * 40
    assert manifest["images"]["api"]["changed"] is True
    assert manifest["images"]["postgres"]["changed"] is False
    assert manifest["evidence"]["release_gate"] == "release-gate.json"
    assert manifest["evidence"]["release_gate_sha256"] == hashlib.sha256(
        release_report.read_bytes()
    ).hexdigest()
    assert output.stat().st_mode & 0o777 == 0o600


def test_creates_no_delta_bootstrap_baseline_only_when_explicit(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "release"
    bundle.mkdir(mode=0o700)
    release_report = _release_report(bundle / "release-gate.json")
    output = bundle / "manifest.json"

    with pytest.raises(ManifestCreationError, match="changed images"):
        create_manifest(
            release_report=release_report,
            output=output,
            release_id="release-bootstrap",
            migration_from="0032_async_import_runtime",
            migration_target="0032_async_import_runtime",
            changed=frozenset(),
        )

    create_manifest(
        release_report=release_report,
        output=output,
        release_id="release-bootstrap",
        migration_from="0032_async_import_runtime",
        migration_target="0032_async_import_runtime",
        changed=frozenset(),
        baseline=True,
    )
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert not any(image["changed"] for image in manifest["images"].values())
    assert manifest["migration"]["compatibility"] == "none"

    with pytest.raises(ManifestCreationError, match="no image or migration delta"):
        create_manifest(
            release_report=release_report,
            output=output,
            release_id="release-invalid-bootstrap",
            migration_from="0031_previous",
            migration_target="0032_async_import_runtime",
            changed=frozenset(),
            baseline=True,
        )


def test_requires_data_for_changed_data_images_and_backup_for_postgres_change(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "release"
    bundle.mkdir(mode=0o700)
    release_report = _release_report(bundle / "release-gate.json")

    with pytest.raises(ManifestCreationError, match="data image"):
        create_manifest(
            release_report=release_report,
            output=bundle / "manifest.json",
            release_id="release-20260728",
            migration_from="0032_async_import_runtime",
            migration_target="0032_async_import_runtime",
            changed=frozenset({"postgres"}),
        )

    data = _private_json(bundle / "data-images.json", {"passed": True})
    backup = _private_json(bundle / "backup-change.json", {"passed": True})
    restore = _private_json(bundle / "restore-report.json", {"passed": True})
    create_manifest(
        release_report=release_report,
        output=bundle / "manifest.json",
        release_id="release-20260728",
        migration_from="0032_async_import_runtime",
        migration_target="0032_async_import_runtime",
        changed=frozenset({"postgres"}),
        data_images=data,
        backup_record=backup,
        restore_report=restore,
    )

    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["evidence"]["data_images"] == "data-images.json"
    assert manifest["evidence"]["backup_restore_change"] == {
        "record": "backup-change.json",
        "restore_report": "restore-report.json",
    }


def test_migration_only_requires_backup_pair_and_no_migration_forbids_it(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "release"
    bundle.mkdir(mode=0o700)
    release_report = _release_report(bundle / "release-gate.json")
    backup = _private_json(bundle / "backup-change.json", {"passed": True})
    restore = _private_json(bundle / "restore-report.json", {"passed": True})

    with pytest.raises(ManifestCreationError, match="PostgreSQL or migration"):
        create_manifest(
            release_report=release_report,
            output=bundle / "manifest.json",
            release_id="release-20260728",
            migration_from="0031_previous",
            migration_target="0032_async_import_runtime",
            changed=frozenset({"api"}),
        )

    create_manifest(
        release_report=release_report,
        output=bundle / "manifest.json",
        release_id="release-20260728",
        migration_from="0031_previous",
        migration_target="0032_async_import_runtime",
        changed=frozenset({"api"}),
        backup_record=backup,
        restore_report=restore,
    )
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["images"]["postgres"]["changed"] is False
    assert manifest["evidence"]["backup_restore_change"] == {
        "record": backup.name,
        "restore_report": restore.name,
    }

    with pytest.raises(ManifestCreationError, match="PostgreSQL or migration"):
        create_manifest(
            release_report=release_report,
            output=bundle / "manifest.json",
            release_id="release-20260729",
            migration_from="0032_async_import_runtime",
            migration_target="0032_async_import_runtime",
            changed=frozenset({"api"}),
            backup_record=backup,
            restore_report=restore,
        )


def test_rejects_candidate_report_without_final_promotion_binding(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "release"
    bundle.mkdir(mode=0o700)
    report = _release_report(bundle / "release-gate.json")
    value = json.loads(report.read_text(encoding="utf-8"))
    value["promotion_source"] = None
    _private_json(report, value)

    with pytest.raises(ManifestCreationError, match="final promotion"):
        create_manifest(
            release_report=report,
            output=bundle / "manifest.json",
            release_id="release-20260728",
            migration_from="0032_async_import_runtime",
            migration_target="0032_async_import_runtime",
            changed=frozenset({"api"}),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("git_sha", "d" * 40),
        ("schema_revision", "0031_other_head"),
        ("openapi_sha256", "short"),
        ("workflow_repository", "local"),
    ],
)
def test_rejects_release_source_not_bound_to_final_candidate(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    bundle = tmp_path / "release"
    bundle.mkdir(mode=0o700)
    report = _release_report(bundle / "release-gate.json")
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload["source"][field] = value
    _private_json(report, payload)

    with pytest.raises(ManifestCreationError, match="source metadata"):
        create_manifest(
            release_report=report,
            output=bundle / "manifest.json",
            release_id="release-20260728",
            migration_from="0032_async_import_runtime",
            migration_target="0032_async_import_runtime",
            changed=frozenset({"api"}),
        )
