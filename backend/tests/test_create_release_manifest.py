from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import create_release_manifest as manifest_module  # noqa: E402
from create_release_manifest import (  # noqa: E402
    ManifestCreationError,
    create_manifest,
)

OFFLINE_EXPAND_FROM = "0080_security_daily_delivery_generation"
OFFLINE_EXPAND_TARGET = "0081_sign_adoption_contract"
OFFLINE_REPORT_EXPAND_FROM = "0081_sign_adoption_contract"
OFFLINE_REPORT_EXPAND_TARGET = "0082_outbox_realtime_report_queue"


def _private_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return path


def _release_report(
    path: Path,
    *,
    schema_revision: str = "0032_async_import_runtime",
) -> Path:
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
                "schema_revision": schema_revision,
                "openapi_sha256": "9" * 64,
                "workflow_repository": "example/enterprise-sms-platform",
                "workflow_run_id": 123,
                "workflow_run_attempt": 1,
                "sbom_sha256": {name: "8" * 64 for name in ("api", "web", "postgres", "redis")},
            },
            "generated_at": "2026-07-28T00:00:00Z",
            "trivy_image": "aquasec/trivy@sha256:" + "d" * 64,
            "images": {
                name: {
                    "ref": f"registry.example.com/sms/{name}@sha256:" + token * 64,
                    "image_id": "sha256:" + token * 64,
                    "repo_digests": [f"registry.example.com/sms/{name}@sha256:" + token * 64],
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


def _offline_archive(
    path: Path,
    *,
    name: str,
    schema_revision: str = "0032_async_import_runtime",
) -> tuple[str, str, int]:
    config = json.dumps(
        {
            "os": "linux",
            "architecture": "amd64",
            "config": {
                "Labels": {
                    "org.opencontainers.image.version": "1.6.0",
                    "org.opencontainers.image.revision": "c" * 40,
                    "com.sms-platform.schema-revision": schema_revision,
                }
            },
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    config_digest = hashlib.sha256(config).hexdigest()
    image_id = "sha256:" + config_digest
    config_name = f"blobs/sha256/{config_digest}"
    layer = f"layer-{name}".encode()
    layer_digest = hashlib.sha256(layer).hexdigest()
    layer_name = f"blobs/sha256/{layer_digest}"
    oci_manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": image_id,
                "size": len(config),
            },
            "layers": [
                {
                    "mediaType": "application/vnd.oci.image.layer.v1.tar",
                    "digest": f"sha256:{layer_digest}",
                    "size": len(layer),
                }
            ],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    oci_manifest_digest = hashlib.sha256(oci_manifest).hexdigest()
    manifest = json.dumps(
        [
            {
                "Config": config_name,
                "RepoTags": [f"sms-platform-release-{name}:" + "c" * 40],
                "Layers": [layer_name],
                "LayerSources": {
                    f"sha256:{layer_digest}": {
                        "mediaType": "application/vnd.oci.image.layer.v1.tar",
                        "size": len(layer),
                        "digest": f"sha256:{layer_digest}",
                    }
                },
            }
        ],
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    index = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": f"sha256:{oci_manifest_digest}",
                    "size": len(oci_manifest),
                }
            ],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    with tarfile.open(path, mode="w:") as archive:
        for filename, payload in (
            (config_name, config),
            (layer_name, layer),
            (f"blobs/sha256/{oci_manifest_digest}", oci_manifest),
            ("index.json", index),
            ("oci-layout", b'{"imageLayoutVersion":"1.0.0"}'),
            ("manifest.json", manifest),
        ):
            member = tarfile.TarInfo(filename)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    path.chmod(0o600)
    return image_id, hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size


def _artifact(path: Path, relative: str) -> dict[str, object]:
    return {
        "file": relative,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size": path.stat().st_size,
    }


def _offline_inputs(
    tmp_path: Path,
    *,
    schema_revision: str = "0032_async_import_runtime",
) -> dict[str, Any]:
    source = tmp_path / "source"
    output_dir = tmp_path / "bundle"
    for directory in (
        source,
        source / "images",
        source / "scans",
        source / "sboms",
        output_dir,
    ):
        directory.mkdir(mode=0o700)
    release_report = _release_report(
        source / "release-gate.json",
        schema_revision=schema_revision,
    )
    report = json.loads(release_report.read_text(encoding="utf-8"))
    report["promotion_source"] = None

    index_images: dict[str, object] = {}
    for name in ("api", "web", "postgres", "redis"):
        archive = source / "images" / f"{name}.tar"
        image_id, archive_hash, archive_size = _offline_archive(
            archive,
            name=name,
            schema_revision=schema_revision,
        )
        scan = _private_json(
            source / "scans" / f"{name}.json",
            {
                "SchemaVersion": 2,
                "ArtifactName": f"sms-platform-release-{name}:" + "c" * 40,
                "ArtifactType": "container_image",
                "Metadata": {"ImageID": image_id},
                "Results": [
                    {
                        "Class": "os-pkgs",
                        "Target": name,
                        "Vulnerabilities": None,
                    }
                ],
            },
        )
        candidate_sbom = _private_json(
            source / "sboms" / f"{name}.cdx.json",
            {"bomFormat": "CycloneDX", "name": name},
        )
        report["images"][name] = {
            "ref": f"sms-platform-release-{name}:" + "c" * 40,
            "image_id": image_id,
            "repo_digests": [],
            "scan_report_sha256": hashlib.sha256(scan.read_bytes()).hexdigest(),
            "scan_passed": True,
        }
        report["source"]["sbom_sha256"][name] = hashlib.sha256(
            candidate_sbom.read_bytes()
        ).hexdigest()
        index_images[name] = {
            "image_id": image_id,
            "archive": {
                "file": f"images/{name}.tar",
                "sha256": archive_hash,
                "size": archive_size,
            },
            "scan": _artifact(scan, f"scans/{name}.json"),
            "sbom": {
                "candidate": _artifact(candidate_sbom, f"sboms/{name}.cdx.json"),
            },
        }
    _private_json(release_report, report)
    offline_index = _private_json(
        source / "offline-image-index.json",
        {
            "schema_version": 2,
            "kind": "production_offline_image_index",
            "candidate_commit": "c" * 40,
            "release_gate": _artifact(release_report, "release-gate.json"),
            "verification": {
                "mode": "single_build_temporary_exception",
                "reproducibility_proven": False,
            },
            "images": index_images,
        },
    )
    private_key = tmp_path / "manifest-ed25519.pem"
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(private_key)],
        check=True,
        capture_output=True,
    )
    private_key.chmod(0o600)
    return {
        "release_report": release_report,
        "output": output_dir / "manifest.json",
        "release_id": "release-offline-20260728",
        "migration_from": schema_revision,
        "migration_target": schema_revision,
        "changed": frozenset(),
        "baseline": True,
        "offline_archive_dir": source / "images",
        "offline_index": offline_index,
        "signing_private_key": private_key,
        "signing_key_id": "production-2026",
    }


def _mock_attestation(
    monkeypatch: pytest.MonkeyPatch,
    *,
    passed: bool = True,
) -> list[tuple[str, ...]]:
    real_run = subprocess.run
    calls: list[tuple[str, ...]] = []

    def run(command: list[str] | tuple[str, ...], *args: object, **kwargs: object) -> Any:
        normalized = tuple(command)
        if normalized[:3] == ("gh", "attestation", "verify"):
            calls.append(normalized)
            payload = b'[{"verificationResult":{"statement":{"subject":[]}}}]' if passed else b"[]"
            return subprocess.CompletedProcess(
                command,
                0 if passed else 1,
                stdout=payload,
                stderr=b"",
            )
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(manifest_module.subprocess, "run", run)
    return calls


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
    assert (
        manifest["evidence"]["release_gate_sha256"]
        == hashlib.sha256(release_report.read_bytes()).hexdigest()
    )
    assert output.stat().st_mode & 0o777 == 0o600


def test_creates_signed_offline_production_manifest_and_closed_payloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = _offline_inputs(tmp_path)
    attestation_bundle = _private_json(tmp_path / "attestation.jsonl", {"bundle": True})
    arguments["attestation_bundle"] = attestation_bundle
    attestation_calls = _mock_attestation(monkeypatch)

    create_manifest(**arguments)

    output = arguments["output"]
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2
    assert manifest["image_source"] == "production-offline-docker-archive-v1"
    assert manifest["signing"] == {
        "algorithm": "ed25519",
        "key_id": "production-2026",
        "file": "manifest.sig",
    }
    assert manifest["images"]["api"]["ref"] == manifest["images"]["api"]["id"]
    assert manifest["images"]["api"]["archive_size"] > 0
    assert (
        manifest["evidence"]["offline_image_index"]["sha256"]
        == hashlib.sha256(arguments["offline_index"].read_bytes()).hexdigest()
    )
    expected_files = {
        "manifest.json",
        "manifest.sig",
        "offline-image-index.json",
        "release-gate.json",
        "api.tar",
        "web.tar",
        "postgres.tar",
        "redis.tar",
    }
    assert {path.name for path in output.parent.iterdir()} == expected_files
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in output.parent.iterdir())
    signature = output.parent / "manifest.sig"
    verified = subprocess.run(
        [
            "openssl",
            "pkeyutl",
            "-verify",
            "-rawin",
            "-inkey",
            str(arguments["signing_private_key"]),
            "-in",
            str(output),
            "-sigfile",
            str(signature),
        ],
        check=False,
        capture_output=True,
    )
    assert verified.returncode == 0
    assert len(attestation_calls) == 1
    command = attestation_calls[0]
    assert command == (
        "gh",
        "attestation",
        "verify",
        str(arguments["offline_index"]),
        "--repo",
        "example/enterprise-sms-platform",
        "--signer-workflow",
        "example/enterprise-sms-platform/.github/workflows/release-gate.yml",
        "--source-digest",
        "c" * 40,
        "--deny-self-hosted-runners",
        "--format",
        "json",
        "--bundle",
        str(attestation_bundle),
    )


def test_offline_manifest_rejects_registry_digest_metadata(tmp_path: Path) -> None:
    arguments = _offline_inputs(tmp_path)
    report_path = arguments["release_report"]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["images"]["api"]["repo_digests"] = [
        "registry.example.com/sms/api@sha256:" + "a" * 64
    ]
    _private_json(report_path, report)

    with pytest.raises(ManifestCreationError, match="release report image api"):
        create_manifest(**arguments)


def test_offline_manifest_accepts_standard_downloaded_artifact_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = _offline_inputs(tmp_path)
    source_root = arguments["offline_index"].parent
    for path in source_root.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    source_root.chmod(0o755)
    _mock_attestation(monkeypatch)

    create_manifest(**arguments)

    output = arguments["output"]
    assert output.stat().st_mode & 0o777 == 0o600
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in output.parent.iterdir())


def test_offline_manifest_rejects_partial_options_and_tampered_index_closure(
    tmp_path: Path,
) -> None:
    arguments = _offline_inputs(tmp_path)
    partial = dict(arguments)
    partial["signing_key_id"] = None

    with pytest.raises(ManifestCreationError, match="provided together"):
        create_manifest(**partial)

    scan = arguments["offline_index"].parent / "scans" / "api.json"
    _private_json(scan, {"passed": False})
    with pytest.raises(ManifestCreationError, match="does not match offline image index"):
        create_manifest(**arguments)
    assert not arguments["output"].exists()
    assert not (arguments["output"].parent / "manifest.sig").exists()


def test_offline_manifest_fails_closed_when_attestation_verification_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = _offline_inputs(tmp_path)
    calls = _mock_attestation(monkeypatch, passed=False)

    with pytest.raises(ManifestCreationError, match="attestation verification failed"):
        create_manifest(**arguments)

    assert len(calls) == 1
    assert not arguments["output"].exists()


def test_offline_manifest_hash_binds_and_copies_conditional_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = _offline_inputs(tmp_path)
    source = arguments["offline_index"].parent
    data = _private_json(source / "data-images.json", {"passed": True})
    backup = _private_json(source / "backup-change.json", {"passed": True})
    restore = _private_json(source / "restore-report.json", {"passed": True})
    arguments.update(
        {
            "changed": frozenset({"api", "web", "postgres", "redis"}),
            "baseline": False,
            "data_images": data,
            "backup_record": backup,
            "restore_report": restore,
        }
    )
    _mock_attestation(monkeypatch)

    create_manifest(**arguments)

    manifest = json.loads(arguments["output"].read_text(encoding="utf-8"))
    data_binding = manifest["evidence"]["data_images"]
    assert data_binding == {
        "file": "data-images.json",
        "sha256": hashlib.sha256(data.read_bytes()).hexdigest(),
        "size": data.stat().st_size,
    }
    backup_binding = manifest["evidence"]["backup_restore_change"]
    assert backup_binding["record"]["sha256"] == hashlib.sha256(backup.read_bytes()).hexdigest()
    assert backup_binding["restore_report"]["size"] == restore.stat().st_size
    for source_path in (data, backup, restore):
        copied = arguments["output"].parent / source_path.name
        assert copied.read_bytes() == source_path.read_bytes()
        assert copied.stat().st_mode & 0o777 == 0o600


def test_offline_full_no_migration_manifest_allows_missing_conditional_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = _offline_inputs(tmp_path)
    arguments.update(
        {
            "changed": frozenset({"api", "web", "postgres", "redis"}),
            "baseline": False,
            "allow_offline_no_conditional_evidence": True,
        }
    )
    _mock_attestation(monkeypatch)

    create_manifest(**arguments)

    output = arguments["output"]
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["evidence"]["data_images"] is None
    assert manifest["evidence"]["backup_restore_change"] is None
    assert {path.name for path in output.parent.iterdir()} == {
        "api.tar",
        "web.tar",
        "postgres.tar",
        "redis.tar",
        "release-gate.json",
        "offline-image-index.json",
        "manifest.json",
        "manifest.sig",
    }


def test_offline_approved_all_four_expand_allows_explicit_evidence_waiver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = _offline_inputs(tmp_path, schema_revision=OFFLINE_EXPAND_TARGET)
    data = _private_json(
        arguments["offline_index"].parent / "data-images.json",
        {"passed": True},
    )
    arguments.update(
        {
            "migration_from": OFFLINE_EXPAND_FROM,
            "changed": frozenset({"api", "web", "postgres", "redis"}),
            "baseline": False,
            "data_images": data,
            "allow_offline_no_conditional_evidence": True,
        }
    )
    _mock_attestation(monkeypatch)

    create_manifest(**arguments)

    manifest = json.loads(arguments["output"].read_text(encoding="utf-8"))
    assert manifest["migration"] == {
        "from": OFFLINE_EXPAND_FROM,
        "target": OFFLINE_EXPAND_TARGET,
        "compatibility": "expand",
    }
    assert all(image["changed"] for image in manifest["images"].values())
    assert manifest["evidence"]["data_images"] == {
        "file": "data-images.json",
        "sha256": hashlib.sha256(data.read_bytes()).hexdigest(),
        "size": data.stat().st_size,
    }
    assert manifest["evidence"]["backup_restore_change"] is None


def test_offline_realtime_report_expand_is_an_approved_full_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = _offline_inputs(
        tmp_path,
        schema_revision=OFFLINE_REPORT_EXPAND_TARGET,
    )
    data = _private_json(
        arguments["offline_index"].parent / "data-images.json",
        {"passed": True},
    )
    arguments.update(
        {
            "migration_from": OFFLINE_REPORT_EXPAND_FROM,
            "changed": frozenset({"api", "web", "postgres", "redis"}),
            "baseline": False,
            "data_images": data,
            "allow_offline_no_conditional_evidence": True,
        }
    )
    _mock_attestation(monkeypatch)

    create_manifest(**arguments)

    manifest = json.loads(arguments["output"].read_text(encoding="utf-8"))
    assert manifest["migration"] == {
        "from": OFFLINE_REPORT_EXPAND_FROM,
        "target": OFFLINE_REPORT_EXPAND_TARGET,
        "compatibility": "expand",
    }


def test_offline_approved_expand_waiver_must_be_explicit(tmp_path: Path) -> None:
    arguments = _offline_inputs(tmp_path, schema_revision=OFFLINE_EXPAND_TARGET)
    data = _private_json(
        arguments["offline_index"].parent / "data-images.json",
        {"passed": True},
    )
    arguments.update(
        {
            "migration_from": OFFLINE_EXPAND_FROM,
            "changed": frozenset({"api", "web", "postgres", "redis"}),
            "baseline": False,
            "data_images": data,
        }
    )

    with pytest.raises(ManifestCreationError, match="explicit risk acceptance"):
        create_manifest(**arguments)


def test_offline_approved_expand_never_waives_data_image_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = _offline_inputs(tmp_path, schema_revision=OFFLINE_EXPAND_TARGET)
    arguments.update(
        {
            "migration_from": OFFLINE_EXPAND_FROM,
            "changed": frozenset({"api", "web", "postgres", "redis"}),
            "baseline": False,
            "allow_offline_no_conditional_evidence": True,
        }
    )
    _mock_attestation(monkeypatch)

    with pytest.raises(ManifestCreationError, match="data image evidence"):
        create_manifest(**arguments)


def test_offline_missing_conditional_evidence_requires_explicit_risk_acceptance(
    tmp_path: Path,
) -> None:
    arguments = _offline_inputs(tmp_path)
    arguments.update(
        {
            "changed": frozenset({"api", "web", "postgres", "redis"}),
            "baseline": False,
        }
    )

    with pytest.raises(ManifestCreationError, match="explicit risk acceptance"):
        create_manifest(**arguments)


@pytest.mark.parametrize(
    ("changed", "migration_from"),
    [
        (frozenset({"api"}), "0032_async_import_runtime"),
        (
            frozenset({"api", "web", "postgres", "redis"}),
            "0031_previous",
        ),
    ],
)
def test_offline_manifest_rejects_selective_and_unapproved_migrations(
    tmp_path: Path,
    changed: frozenset[str],
    migration_from: str,
) -> None:
    arguments = _offline_inputs(tmp_path)
    arguments.update(
        {
            "changed": changed,
            "baseline": False,
            "migration_from": migration_from,
        }
    )

    with pytest.raises(ManifestCreationError, match="approved all-four expand"):
        create_manifest(**arguments)


def test_offline_approved_expand_rejects_selective_images(tmp_path: Path) -> None:
    arguments = _offline_inputs(tmp_path, schema_revision=OFFLINE_EXPAND_TARGET)
    arguments.update(
        {
            "migration_from": OFFLINE_EXPAND_FROM,
            "changed": frozenset({"api"}),
            "baseline": False,
            "allow_offline_no_conditional_evidence": True,
        }
    )

    with pytest.raises(ManifestCreationError, match="approved all-four expand"):
        create_manifest(**arguments)


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
