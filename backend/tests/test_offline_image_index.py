from __future__ import annotations

import hashlib
import json
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from create_offline_image_index import (  # noqa: E402
    IMAGES,
    OfflineImageIndexError,
    create_index,
)

COMMIT = "c" * 40
APP_VERSION = "1.6.0"
SCHEMA_REVISION = "0032_async_import_runtime"


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_private(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)
    path.chmod(0o600)


def _write_archive(path: Path, *, name: str) -> str:
    identity = _json_bytes(
        {
            "name": name,
            "version": APP_VERSION,
            "commit": COMMIT,
            "schema": SCHEMA_REVISION,
        }
    )
    image_id = "sha256:" + hashlib.sha256(identity).hexdigest()
    path.write_bytes(b"opaque-docker-save-test-fixture\n" + identity)
    path.chmod(0o600)
    return image_id


def _prepare_evidence(root: Path) -> tuple[Path, dict[str, str]]:
    images_dir = root / "images"
    scans_dir = root / "scans"
    sboms_dir = root / "sboms"
    for directory in (root, images_dir, scans_dir, sboms_dir):
        directory.mkdir()
        directory.chmod(0o700)

    image_ids = {name: _write_archive(images_dir / f"{name}.tar", name=name) for name in IMAGES}
    sbom_hashes: dict[str, str] = {}
    scan_hashes: dict[str, str] = {}
    for name in IMAGES:
        sbom = _json_bytes(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "components": [{"name": name, "type": "container"}],
            }
        )
        candidate_sbom = sboms_dir / f"{name}.cdx.json"
        rebuilt_sbom = sboms_dir / f"{name}.rebuild.cdx.json"
        _write_private(candidate_sbom, sbom)
        _write_private(rebuilt_sbom, sbom)
        sbom_hashes[name] = _sha256(candidate_sbom)

        scan = _json_bytes(
            {
                "SchemaVersion": 2,
                "ArtifactName": f"/scan/{name}.tar",
                "ArtifactType": "container_image",
                "Metadata": {"ImageID": image_ids[name]},
                "Results": [
                    {
                        "Target": f"sms-platform-release-{name}",
                        "Class": "os-pkgs",
                        "Vulnerabilities": [],
                    }
                ],
            }
        )
        scan_path = scans_dir / f"{name}.json"
        _write_private(scan_path, scan)
        scan_hashes[name] = _sha256(scan_path)

    release_gate = root / "release-gate.json"
    release_document: dict[str, Any] = {
        "schema_version": 1,
        "gate_type": "release",
        "candidate_commit": COMMIT,
        "source": {
            "app_version": APP_VERSION,
            "git_sha": COMMIT,
            "schema_revision": SCHEMA_REVISION,
            "openapi_sha256": "9" * 64,
            "workflow_repository": "owner/repository",
            "workflow_run_id": 123,
            "workflow_run_attempt": 1,
            "sbom_sha256": sbom_hashes,
        },
        "generated_at": "2026-08-26T00:00:00Z",
        "trivy_image": "aquasec/trivy:0.70.0@sha256:" + "d" * 64,
        "images": {
            name: {
                "ref": f"sms-platform-release-{name}:{COMMIT}",
                "image_id": image_ids[name],
                "repo_digests": [],
                "scan_report_sha256": scan_hashes[name],
                "scan_passed": True,
            }
            for name in IMAGES
        },
        "promotion_source": None,
        "passed": True,
    }
    _write_private(release_gate, _json_bytes(release_document))
    reproducibility = root / "reproducibility.json"
    _write_private(
        reproducibility,
        _json_bytes(
            {
                "schema_version": 1,
                "gate_type": "release_reproducibility",
                "candidate_commit": COMMIT,
                "passed": True,
                "source": {
                    "sbom_sha256": sbom_hashes,
                    "baseline_report_sha256": _sha256(release_gate),
                },
                "images": {name: {"image_id": image_ids[name]} for name in IMAGES},
            }
        ),
    )
    return root / "offline-image-index.json", image_ids


def _create(output: Path) -> None:
    create_index(
        commit=COMMIT,
        release_gate_path=output.parent / "release-gate.json",
        reproducibility_path=output.parent / "reproducibility.json",
        archive_dir=output.parent / "images",
        scan_dir=output.parent / "scans",
        sbom_dir=output.parent / "sboms",
        output=output,
    )


def test_offline_image_index_binds_the_exact_private_evidence_set(tmp_path: Path) -> None:
    output, image_ids = _prepare_evidence(tmp_path / "release-evidence")

    _create(output)

    document = json.loads(output.read_text(encoding="utf-8"))
    assert set(document) == {
        "schema_version",
        "kind",
        "candidate_commit",
        "release_gate",
        "reproducibility",
        "images",
    }
    assert document["schema_version"] == 1
    assert document["kind"] == "production_offline_image_index"
    assert document["candidate_commit"] == COMMIT
    assert set(document["images"]) == set(IMAGES)
    for name in IMAGES:
        image = document["images"][name]
        assert image["image_id"] == image_ids[name]
        assert image["archive"]["file"] == f"images/{name}.tar"
        assert image["archive"]["sha256"] == _sha256(output.parent / "images" / f"{name}.tar")
        assert image["archive"]["size"] > 0
        assert image["scan"]["file"] == f"scans/{name}.json"
        assert image["sbom"]["candidate"]["file"] == f"sboms/{name}.cdx.json"
        assert image["sbom"]["rebuild"]["file"] == (f"sboms/{name}.rebuild.cdx.json")
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert output.read_bytes().endswith(b"\n")


def test_offline_image_index_rejects_registry_digest_metadata(tmp_path: Path) -> None:
    output, _ = _prepare_evidence(tmp_path / "release-evidence")
    release_gate = output.parent / "release-gate.json"
    document = json.loads(release_gate.read_text(encoding="utf-8"))
    document["images"]["api"]["repo_digests"] = [
        "registry.example.com/sms/api@sha256:" + "a" * 64
    ]
    _write_private(release_gate, _json_bytes(document))

    with pytest.raises(OfflineImageIndexError, match="release gate image api"):
        _create(output)


def test_offline_image_index_rejects_scan_tampering(tmp_path: Path) -> None:
    output, _ = _prepare_evidence(tmp_path / "release-evidence")
    scan = output.parent / "scans" / "api.json"
    scan.write_bytes(scan.read_bytes() + b" ")

    with pytest.raises(OfflineImageIndexError, match="scan hash"):
        _create(output)

    assert not output.exists()


def test_offline_image_index_rejects_extra_files(tmp_path: Path) -> None:
    output, _ = _prepare_evidence(tmp_path / "release-evidence")
    extra = output.parent / "images" / "unexpected.tar"
    _write_private(extra, b"unexpected")

    with pytest.raises(OfflineImageIndexError, match="closed file set"):
        _create(output)

    assert not output.exists()


def test_offline_image_index_rejects_linked_evidence(tmp_path: Path) -> None:
    output, _ = _prepare_evidence(tmp_path / "release-evidence")
    scan = output.parent / "scans" / "api.json"
    hardlink = tmp_path / "scan-hardlink.json"
    hardlink.hardlink_to(scan)

    with pytest.raises(OfflineImageIndexError, match="single-link regular file"):
        _create(output)

    assert not output.exists()
