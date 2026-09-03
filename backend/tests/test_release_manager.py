from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))

import release_manager as release_manager_module  # noqa: E402
from release_manager import (  # noqa: E402
    ReconciliationDecision,
    ReleaseManager,
    ReleaseManagerError,
    ReleaseState,
    ReleaseStore,
    RuntimeObservation,
    reconcile_release,
)
from release_manifest import OFFLINE_EXPAND_MIGRATION, load_manifest  # noqa: E402

COMMIT = "c" * 40
OFFLINE_REPORT_EXPAND_MIGRATION = (
    "0081_sign_adoption_contract",
    "0082_outbox_realtime_report_queue",
)
OFFLINE_AUTH_EXPAND_MIGRATION = (
    "0082_outbox_realtime_report_queue",
    "0084_auth_security_and_ad_freshness",
)
DEFAULT_PRODUCTION_ENVIRONMENT_FILE = (
    release_manager_module._PRODUCTION_ENVIRONMENT_FILE
)
IMAGE_NAMES = ("api", "web", "postgres", "redis")
CONTROL_SMOKE_IMAGES = {
    "api": {
        "ref": "sms-platform-api:amd64-ffcecbe",
        "id": "sha256:07f1deaea83a50ac7d44d872f0748be523bc9edfa641d97565979d5031980c39",
    },
    "web": {
        "ref": "sms-platform-web:amd64-ffcecbe",
        "id": "sha256:804a1d8dc488b27535b45d15d7abee81b8e95ca3300b1d8939de23309af17d46",
    },
    "postgres": {
        "ref": "sms-platform-postgres:amd64-ffcecbe",
        "id": "sha256:4e9aa6c3ed14ac7d2f56a960066617bac46461a996bdd426b3c375b6fdfccb81",
    },
    "redis": {
        "ref": "sms-platform-redis:amd64-ffcecbe",
        "id": "sha256:ab4439eedeb2e9c742b5e3b087a269d95a98bb61eaaaaa25f75b93989fd2bf51",
    },
}
RUNTIME_SERVICES = (
    "api",
    "web",
    "postgres",
    "redis",
    "redis-auth",
    "redis-control",
    "worker-realtime",
    "worker-report",
    "worker-bulk",
    "worker-callback",
    "outbox-dispatcher",
    "beat",
)
WORKER_QUEUES = {
    "worker-realtime": "realtime",
    "worker-report": "realtime-report",
    "worker-bulk": "bulk",
    "worker-callback": "callback",
}
WORKER_SERVICES = tuple(WORKER_QUEUES)
RECOVERY_RUNTIME_TARGET = "generations/generation-" + "a" * 32


@pytest.fixture(autouse=True)
def production_environment_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        release_manager_module,
        "_PRODUCTION_ENVIRONMENT_FILE",
        tmp_path / "etc" / "sms-platform" / "platform.env",
    )
    monkeypatch.setattr(
        release_manager_module,
        "_PRODUCTION_ENVIRONMENT_UID",
        os.geteuid(),
    )
    monkeypatch.setattr(
        release_manager_module,
        "_PRODUCTION_ENVIRONMENT_GID",
        os.getegid(),
    )


def _image_id(name: str) -> str:
    return "sha256:" + dict(api="a", web="b", postgres="d", redis="e")[name] * 64


def _service_image_name(service: str) -> str:
    if service in {"redis-auth", "redis-control"}:
        return "redis"
    return service if service in IMAGE_NAMES else "api"


def _write_private_json(path: Path, value: object) -> None:
    if (
        path.name == "manifest.json"
        and type(value) is dict
        and type(value.get("evidence")) is dict
    ):
        report_name = value["evidence"].get("release_gate")
        report_path = path.parent / str(report_name)
        if report_path.is_file():
            report = json.loads(report_path.read_text(encoding="utf-8"))
            source = report.get("source")
            if type(source) is dict:
                source["schema_revision"] = value["migration"]["target"]
                promotion = report.get("promotion_source")
                if type(promotion) is dict and type(promotion.get("source")) is dict:
                    promotion["source"]["schema_revision"] = value["migration"]["target"]
                report_path.write_text(json.dumps(report), encoding="utf-8")
                report_path.chmod(0o600)
            value["evidence"]["release_gate_sha256"] = hashlib.sha256(
                report_path.read_bytes()
            ).hexdigest()
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


def _write_bound_release_report(
    path: Path,
    manifest: dict[str, Any],
    report: dict[str, Any],
) -> None:
    _write_private_json(path, report)
    manifest["evidence"]["release_gate_sha256"] = hashlib.sha256(
        path.read_bytes()
    ).hexdigest()


def _release_report(manifest: dict[str, Any]) -> dict[str, Any]:
    production = manifest["mode"] == "production"
    offline = manifest.get("image_source") == "production-offline-docker-archive-v1"
    commit = manifest["commit"]
    source = {
        "app_version": "1.6.0",
        "git_sha": commit,
        "schema_revision": manifest["migration"]["target"],
        "openapi_sha256": "9" * 64,
        "workflow_repository": (
            "example/enterprise-sms-platform" if production else "local"
        ),
        "workflow_run_id": 123 if production else 0,
        "workflow_run_attempt": 1 if production else 0,
        "sbom_sha256": {name: "8" * 64 for name in IMAGE_NAMES},
    }
    return {
        "schema_version": 1,
        "gate_type": "release",
        "candidate_commit": commit,
        "source": source,
        "generated_at": "2026-07-14T07:00:00Z",
        "trivy_image": "aquasec/trivy:0.70.0@sha256:" + "f" * 64,
        "images": {
            name: {
                "ref": (
                    f"sms-platform-release-{name}:{commit}"
                    if offline
                    else manifest["images"][name]["ref"]
                ),
                "image_id": manifest["images"][name]["id"],
                "repo_digests": (
                    [manifest["images"][name]["ref"]]
                    if production and not offline
                    else []
                ),
                "scan_report_sha256": "f" * 64,
                "scan_passed": True,
            }
            for name in IMAGE_NAMES
        },
        "promotion_source": (
            {
                "report_sha256": "a" * 64,
                "candidate_commit": commit,
                "source": source,
                "images": {
                    name: {
                        "ref": f"sms-platform-release-{name}:{commit}",
                        "image_id": manifest["images"][name]["id"],
                        "scan_report_sha256": "b" * 64,
                    }
                    for name in IMAGE_NAMES
                },
            }
            if production and not offline
            else None
        ),
        "passed": True,
    }


def _control_smoke_report(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "gate_type": "release_control_smoke",
        "candidate_commit": COMMIT,
        "generated_at": "2026-07-15T07:00:00Z",
        "purpose": "release_control_failure_injection",
        "scan_performed": False,
        "authorized_for_control_smoke": True,
        "images": {
            name: {
                "ref": manifest["images"][name]["ref"],
                "image_id": manifest["images"][name]["id"],
                "platform": "linux/amd64",
            }
            for name in IMAGE_NAMES
        },
    }


def _data_report(manifest: dict[str, Any], *, postgres_major: int = 16) -> dict[str, Any]:
    offline = manifest.get("image_source") == "production-offline-docker-archive-v1"
    return {
        "schema_version": 1,
        "gate_type": "data_images",
        "candidate_commit": manifest["commit"],
        "generated_at": "2026-07-14T07:05:00Z",
        "images": {
            "postgres": {
                "ref": (
                    f"sms-platform-release-postgres:{manifest['commit']}"
                    if offline
                    else manifest["images"]["postgres"]["ref"]
                ),
                "image_id": manifest["images"]["postgres"]["id"],
                "platform": "linux/amd64",
                "version": f"{postgres_major}.8",
                "major": postgres_major,
            },
            "redis": {
                "ref": (
                    f"sms-platform-release-redis:{manifest['commit']}"
                    if offline
                    else manifest["images"]["redis"]["ref"]
                ),
                "image_id": manifest["images"]["redis"]["id"],
                "platform": "linux/amd64",
                "version": "7.4.2",
                "major": 7,
            },
        },
        "checks": {
            "postgres_role_constraints": True,
            "postgres_restart_persistence": True,
            "redis_aof_restart_persistence": True,
        },
        "passed": True,
    }


RESTORE_CRYPTO_COVERAGE_FIELDS = (
    "app.callback_secret_enc",
    "blacklist.phone_enc",
    "callback_task.callback_secret_enc",
    "import_phone.phone_enc",
    "raw_vendor_log.payload_enc",
    "reply_event.content_enc",
    "reply_event.phone_enc",
    "report_event.phone_enc",
    "sensitive_metadata_archive.value_enc",
    "sms_batch.display_content_enc",
    "sms_batch.send_content_enc",
    "sms_message.phone_enc",
    "sms_reply.phone_enc",
    "sms_template.content_enc",
    "sms_template.name_enc",
    "unmatched_report.phone_enc",
    "vendor_test_recipient.phone_enc",
)


def _restore_crypto_probe_receipt() -> dict[str, Any]:
    coverage = {
        label: {"rows": 0, "key_versions_verified": 0}
        for label in RESTORE_CRYPTO_COVERAGE_FIELDS
    }
    coverage["raw_vendor_log.payload_enc"] = {
        "rows": 30,
        "key_versions_verified": 1,
    }
    coverage["sms_batch.display_content_enc"] = {
        "rows": 10,
        "key_versions_verified": 1,
    }
    coverage["sms_batch.send_content_enc"] = {
        "rows": 10,
        "key_versions_verified": 1,
    }
    return {
        "schema_version": 2,
        "status": "performed",
        "counts": {
            "audit_context_keys": 4,
            "encrypted_columns": len(RESTORE_CRYPTO_COVERAGE_FIELDS),
            "encrypted_rows": 50,
            "ciphertext_samples_verified": 3,
            "key_version_columns": 8,
            "referenced_key_versions": 1,
            "sms_message_rows": 0,
        },
        "coverage": coverage,
    }


def _restore_report() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "status": "success",
        "metric_scope": "database_restore",
        "business_rto_evidence": False,
        "snapshot_id": "snapshot-20260714",
        "recovery_crypto_generation_id": "recovery-generation-01",
        "backup_passphrase_generation_id": "backup-generation-01",
        "git_commit": COMMIT,
        "database": "sms_drill_release_20260714",
        "started_at": "2026-07-14T07:10:00+00:00",
        "finished_at": "2026-07-14T07:20:00+00:00",
        "restore_seconds": 600.0,
        "restore_budget_seconds": 43200.0,
        "within_restore_budget": True,
        "checks": {
            "alembic_version": "0012",
            "role_flags": "7|true",
            "audit_privileges": "true",
            "crypto_generation_binding": "matched_host_generation_ids",
            "historical_ciphertext_validation": "performed",
            "pre_migration_crypto_validation": "performed",
            "post_migration_crypto_validation": "performed",
        },
        "crypto_probe_receipts": {
            "pre_migration": _restore_crypto_probe_receipt(),
            "post_migration": _restore_crypto_probe_receipt(),
        },
        "table_counts": {
            "sms_batch": 10,
            "audit_log": 20,
            "raw_vendor_log": 30,
            "sms_message": 0,
        },
    }


def _production_change_record(manifest: dict[str, Any], report_path: Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "record_type": "postgres_backup_restore_change",
        "change_id": "CHG-20260714-001",
        "release_id": manifest["release_id"],
        "target_commit": manifest["commit"],
        "target_postgres_image_id": manifest["images"]["postgres"]["id"],
        "approval": {
            "status": "approved",
            "approved_by": "dba01",
            "approved_at": "2026-07-14T08:00:00+00:00",
        },
        "restore": {
            "snapshot_id": "snapshot-20260714",
            "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        },
    }


def _bundle(
    tmp_path: Path,
    *,
    mode: str = "development",
    postgres_changed: bool = False,
) -> tuple[Path, dict[str, Any], dict[str, str]]:
    runtime_root = tmp_path / "staging"
    runtime_root.mkdir(mode=0o700)
    runtime_root.chmod(0o700)
    bundle = runtime_root / "release-20260714"
    bundle.mkdir(mode=0o700)
    bundle.chmod(0o700)
    if mode == "production":
        refs = {
            name: f"registry.example.com/sms/{name}@sha256:"
            + dict(api="1", web="2", postgres="3", redis="4")[name] * 64
            for name in IMAGE_NAMES
        }
    else:
        refs = {name: f"sms-platform-{name}:candidate" for name in IMAGE_NAMES}
    changed = {name: name == "web" for name in IMAGE_NAMES}
    if postgres_changed:
        changed["web"] = False
        changed["postgres"] = True
    images: dict[str, dict[str, Any]] = {}
    for name in IMAGE_NAMES:
        archive_file = f"{name}.tar" if mode == "development" and changed[name] else None
        archive_sha: str | None = None
        if archive_file is not None:
            archive_path = bundle / archive_file
            archive_path.write_bytes(f"verified-{name}-archive".encode())
            archive_path.chmod(0o600)
            archive_sha = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        images[name] = {
            "ref": refs[name],
            "id": _image_id(name),
            "archive_file": archive_file,
            "archive_sha256": archive_sha,
            "changed": changed[name],
        }
    data_name = "data-images.json" if postgres_changed else None
    backup = (
        {"record": "backup-change.json", "restore_report": "restore-report.json"}
        if mode == "production"
        else None
    )
    manifest = {
        "schema_version": 1,
        "release_id": "release-20260714",
        "commit": COMMIT,
        "mode": mode,
        "images": images,
        "migration": {"from": "0011", "target": "0012", "compatibility": "expand"},
        "evidence": {
            "release_gate_kind": "release",
            "release_gate": "release-gate.json",
            "release_gate_sha256": "0" * 64,
            "data_images": data_name,
            "backup_restore_change": backup,
        },
    }
    _write_bound_release_report(
        bundle / "release-gate.json",
        manifest,
        _release_report(manifest),
    )
    if data_name is not None:
        _write_private_json(bundle / data_name, _data_report(manifest))
    if backup is not None:
        report_path = bundle / backup["restore_report"]
        _write_private_json(report_path, _restore_report())
        _write_private_json(
            bundle / backup["record"],
            _production_change_record(manifest, report_path),
        )
    manifest_path = bundle / "manifest.json"
    _write_private_json(manifest_path, manifest)
    current_refs = refs.copy()
    for name in IMAGE_NAMES:
        if changed[name]:
            current_refs[name] = (
                f"registry.example.com/sms/{name}@sha256:" + "9" * 64
                if mode == "production"
                else f"sms-platform-{name}:previous"
            )
    return manifest_path, manifest, current_refs


def _write_test_docker_archive(
    path: Path,
    *,
    name: str,
    app_version: str,
    commit: str,
    schema_revision: str,
    large: bool = False,
) -> tuple[str, str, int]:
    identity = json.dumps(
        {
            "name": name,
            "version": app_version,
            "commit": commit,
            "schema": schema_revision,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    image_id = "sha256:" + hashlib.sha256(identity).hexdigest()
    payload = b"opaque-docker-save-test-fixture\n" + identity
    if large:
        payload += b"x" * (1024 * 1024 + 1)
    path.write_bytes(payload)
    path.chmod(0o600)
    return image_id, hashlib.sha256(payload).hexdigest(), len(payload)


def _offline_bundle(
    tmp_path: Path,
    *,
    changed: set[str] | None = None,
    large_archive: bool = False,
    migration_changed: bool = False,
    migration_pair: tuple[str, str] | None = None,
    include_conditional_evidence: bool = True,
) -> tuple[Path, dict[str, Any], dict[str, str]]:
    changed = set(IMAGE_NAMES) if changed is None else changed
    runtime_root = tmp_path / "offline-staging"
    runtime_root.mkdir(mode=0o700)
    runtime_root.chmod(0o700)
    bundle = runtime_root / "offline-release-20260826"
    bundle.mkdir(mode=0o700)
    bundle.chmod(0o700)
    app_version = "1.6.0"
    if migration_changed or migration_pair is not None:
        migration_from, schema_revision = (
            migration_pair or OFFLINE_EXPAND_MIGRATION
        )
    else:
        migration_from, schema_revision = ("0012_baseline", "0012_baseline")
    migration_changed = migration_from != schema_revision
    images: dict[str, dict[str, Any]] = {}
    for name in IMAGE_NAMES:
        archive_path = bundle / f"{name}.tar"
        image_id, archive_sha256, archive_size = _write_test_docker_archive(
            archive_path,
            name=name,
            app_version=app_version,
            commit=COMMIT,
            schema_revision=schema_revision,
            large=large_archive and name == "api",
        )
        images[name] = {
            "ref": image_id,
            "id": image_id,
            "archive_file": f"{name}.tar",
            "archive_sha256": archive_sha256,
            "archive_size": archive_size,
            "changed": name in changed,
        }
    manifest: dict[str, Any] = {
        "schema_version": 2,
        "release_id": "offline-release-20260826",
        "commit": COMMIT,
        "mode": "production",
        "image_source": "production-offline-docker-archive-v1",
        "signing": {
            "algorithm": "ed25519",
            "key_id": "sms-prod-2026",
            "file": "manifest.sig",
        },
        "images": images,
        "migration": {
            "from": migration_from,
            "target": schema_revision,
            "compatibility": "expand" if migration_changed else "none",
        },
        "evidence": {
            "release_gate_kind": "release",
            "release_gate": "release-gate.json",
            "release_gate_sha256": "0" * 64,
            "data_images": None,
            "backup_restore_change": None,
            "offline_image_index": {
                "file": "offline-image-index.json",
                "sha256": "0" * 64,
            },
        },
    }
    release_gate_path = bundle / "release-gate.json"
    _write_bound_release_report(
        release_gate_path,
        manifest,
        _release_report(manifest),
    )
    if include_conditional_evidence and changed & {"postgres", "redis"}:
        data_path = bundle / "data-images.json"
        _write_private_json(data_path, _data_report(manifest))
        manifest["evidence"]["data_images"] = {
            "file": data_path.name,
            "sha256": hashlib.sha256(data_path.read_bytes()).hexdigest(),
            "size": data_path.stat().st_size,
        }
    if include_conditional_evidence and ("postgres" in changed or migration_changed):
        restore_path = bundle / "restore-report.json"
        restore = _restore_report()
        restore["git_commit"] = manifest["commit"]
        restore["checks"]["alembic_version"] = manifest["migration"]["target"]
        _write_private_json(restore_path, restore)
        record_path = bundle / "backup-change.json"
        _write_private_json(
            record_path,
            _production_change_record(manifest, restore_path),
        )
        manifest["evidence"]["backup_restore_change"] = {
            "record": {
                "file": record_path.name,
                "sha256": hashlib.sha256(record_path.read_bytes()).hexdigest(),
                "size": record_path.stat().st_size,
            },
            "restore_report": {
                "file": restore_path.name,
                "sha256": hashlib.sha256(restore_path.read_bytes()).hexdigest(),
                "size": restore_path.stat().st_size,
            },
        }
    index = {
        "schema_version": 1,
        "kind": "production_offline_image_index",
        "candidate_commit": COMMIT,
        "release_gate": {
            "file": "release-gate.json",
            "sha256": manifest["evidence"]["release_gate_sha256"],
            "size": release_gate_path.stat().st_size,
        },
        "reproducibility": {
            "file": "reproducibility.json",
            "sha256": "7" * 64,
            "size": 1,
        },
        "images": {
            name: {
                "image_id": images[name]["id"],
                "archive": {
                    "file": f"images/{name}.tar",
                    "sha256": images[name]["archive_sha256"],
                    "size": images[name]["archive_size"],
                },
                "scan": {
                    "file": f"scans/{name}.json",
                    "sha256": "6" * 64,
                    "size": 1,
                },
                "sbom": {
                    "candidate": {
                        "file": f"sboms/{name}.cdx.json",
                        "sha256": "5" * 64,
                        "size": 1,
                    },
                    "rebuild": {
                        "file": f"sboms/{name}.rebuild.cdx.json",
                        "sha256": "4" * 64,
                        "size": 1,
                    },
                },
            }
            for name in IMAGE_NAMES
        },
    }
    index_path = bundle / "offline-image-index.json"
    _write_private_json(index_path, index)
    manifest["evidence"]["offline_image_index"]["sha256"] = hashlib.sha256(
        index_path.read_bytes()
    ).hexdigest()
    manifest_path = bundle / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_path.chmod(0o600)
    signature_path = bundle / "manifest.sig"
    signature_path.write_bytes(b"s" * 64)
    signature_path.chmod(0o600)
    current_refs = {
        name: (
            "sha256:" + dict(api="1", web="2", postgres="3", redis="4")[name] * 64
            if name in changed
            else images[name]["id"]
        )
        for name in IMAGE_NAMES
    }
    return manifest_path, manifest, current_refs


def _configure_offline_trust(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path]:
    trust = tmp_path / "offline-trust"
    trust.mkdir(mode=0o700)
    public_key = trust / "offline-release-signing-public.pem"
    key_id = trust / "offline-release-signing-key-id"
    public_key.write_text("test-ed25519-public-key\n", encoding="ascii")
    key_id.write_text("sms-prod-2026\n", encoding="ascii")
    public_key.chmod(0o644)
    key_id.chmod(0o644)
    monkeypatch.setattr(
        release_manager_module,
        "_OFFLINE_SIGNING_PUBLIC_KEY",
        public_key,
    )
    monkeypatch.setattr(
        release_manager_module,
        "_OFFLINE_SIGNING_KEY_ID",
        key_id,
    )
    monkeypatch.setattr(
        release_manager_module,
        "_OFFLINE_SIGNING_TRUST_UID",
        os.geteuid(),
    )
    monkeypatch.setattr(
        release_manager_module,
        "_OFFLINE_SIGNING_TRUST_GID",
        os.getegid(),
    )
    return public_key, key_id


def _rewrite_offline_release_evidence(
    manifest_path: Path,
    manifest: dict[str, Any],
    report: dict[str, Any],
) -> None:
    release_gate = manifest_path.parent / "release-gate.json"
    _write_private_json(release_gate, report)
    manifest["evidence"]["release_gate_sha256"] = hashlib.sha256(
        release_gate.read_bytes()
    ).hexdigest()
    index_path = manifest_path.parent / "offline-image-index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["release_gate"] = {
        "file": "release-gate.json",
        "sha256": manifest["evidence"]["release_gate_sha256"],
        "size": release_gate.stat().st_size,
    }
    _write_private_json(index_path, index)
    manifest["evidence"]["offline_image_index"]["sha256"] = hashlib.sha256(
        index_path.read_bytes()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_path.chmod(0o600)


def _platform(
    tmp_path: Path,
    current_refs: dict[str, str],
    *,
    migration_from: str,
    migration_target: str,
) -> tuple[Path, Path, Path, Path]:
    root = tmp_path / "platform"
    control_root = tmp_path / "production-control" / "versions" / COMMIT
    for source_root in (root, control_root):
        (source_root / "deploy").mkdir(parents=True)
        (source_root / "deploy" / "docker-compose.yml").write_text(
            "services: {}\n", encoding="utf-8"
        )
        for name in (
            "docker-compose.production-storage.yml",
            "docker-compose.production-restart.yml",
            "docker-compose.redis-tls.yml",
        ):
            (source_root / "deploy" / name).write_text(
                f"# {name}\nservices: {{}}\n", encoding="utf-8"
            )
        versions = source_root / "backend" / "migrations" / "versions"
        versions.mkdir(parents=True)
        (versions / "0001_from.py").write_text(
            f'revision = "{migration_from}"\ndown_revision = None\n',
            encoding="utf-8",
        )
        if migration_target != migration_from:
            (versions / "0002_target.py").write_text(
                f'revision = "{migration_target}"\n'
                f'down_revision = "{migration_from}"\n',
                encoding="utf-8",
            )
    keys = {
        "api": "SMS_API_IMAGE",
        "web": "SMS_WEB_IMAGE",
        "postgres": "SMS_POSTGRES_IMAGE",
        "redis": "SMS_REDIS_IMAGE",
    }
    environment = (
        "\n".join(f"{keys[name]}={current_refs[name]}" for name in IMAGE_NAMES)
        + "\nREDIS_HA_MODE=isolated-standalone\n"
    )
    development_environment_file = root / ".env"
    development_environment_file.write_text(environment, encoding="utf-8")
    development_environment_file.chmod(0o600)
    production_environment_file = release_manager_module._PRODUCTION_ENVIRONMENT_FILE
    environment_parent = production_environment_file.parent
    environment_parent.mkdir(parents=True, exist_ok=True)
    environment_parent.chmod(0o755)
    production_environment_file.write_text(environment, encoding="utf-8")
    production_environment_file.chmod(0o600)
    release_root = tmp_path / "release-store"
    release_root.mkdir(mode=0o700)
    release_root.chmod(0o700)
    return root, control_root, production_environment_file, release_root


class FakeRunner:
    def __init__(
        self,
        manifest: dict[str, Any],
        current_refs: dict[str, str],
    ) -> None:
        self.manifest = manifest
        self.current_refs = current_refs
        self.calls: list[list[str]] = []
        self.call_options: list[dict[str, object]] = []
        self.git_commit = COMMIT
        self.postgres_version = "postgres (PostgreSQL) 16.8\n"
        self.redis_version = (
            "Redis server v=7.4.2 sha=00000000:0 malloc=jemalloc-5.3.0 bits=64 build=abcdef12\n"
        )
        self.target_id_override: str | None = None
        self.target_repo_digests_override: list[str] | None = None
        self.signature_valid = True
        self.loaded_offline_image_ids: set[str] = set()
        self.offline_identity_override: dict[str, str] = {}
        self.fail_offline_load_number: int | None = None
        self.offline_load_count = 0
        self.fail_action: str | None = None
        self.fail_action_number = 1
        self.fail_after_effect = False
        self.fail_compensation = False
        self.migration_head = manifest["migration"]["from"]
        self.migration_head_after_failed_run: str | None = None
        self.migration_head_after_successful_run: str | None = None
        self.failed_once = False
        self.action_counts: dict[str, int] = {}
        self.runtime_refs = current_refs.copy()
        self.service_running = {service: True for service in RUNTIME_SERVICES}
        self.service_container_ids = {
            service: hashlib.sha256(f"original:{service}".encode()).hexdigest()
            for service in RUNTIME_SERVICES
        }
        self.service_hostnames = {service: f"host-{service}" for service in RUNTIME_SERVICES}
        beat_project = "sms-platform"
        if (
            manifest["mode"] == "development"
            and manifest["evidence"]["release_gate_kind"] == "release_control_smoke"
        ):
            beat_project = os.environ.get("COMPOSE_PROJECT_NAME", "sms-platform")
        beat_volume = f"{beat_project}_beatdata"
        self.beat_schedule_mount = (
            f"volume {beat_volume} /var/lib/sms/beat true "
            f"/var/lib/docker/volumes/{beat_volume}/_data\n"
        )
        self.fail_beat_schedule_mount_inspection = False
        self.recreate_count = 0
        self.unhealthy_service: str | None = None
        self.missing_worker_ping: str | None = None
        self.worker_queue_overrides: dict[str, str] = {}
        self.after_action: Any = None
        self.after_ping: Any = None
        self.volume_inventory = ""
        self.recovery_watermark: dict[str, object] = {
            "batch_queued": 2,
            "batch_sending": 1,
            "chunk_pending": 3,
            "submitting": 0,
            "retrying": 0,
            "submitted": 4,
            "uncertain": 1,
            "max_chunk_id": 9,
            "outbox_pending": 2,
            "outbox_leased": 0,
            "outbox_processing": 0,
            "max_outbox_created_at": "2026-08-24 00:00:00+00",
        }
        self.recovery_database_fingerprint: dict[str, object] = {
            "database": "sms",
            "database_oid": "16384",
            "migration_head": self.manifest["migration"]["target"],
            "batch_rows": 10,
            "chunk_rows": 9,
            "outbox_rows": 4,
            "max_batch_id": 10,
            "max_chunk_id": 9,
        }
        self.recovery_crypto_probe = _restore_crypto_probe_receipt()
        self.recovery_crypto_probe_calls = 0

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        user: int | None = None,
        group: int | None = None,
        extra_groups: Sequence[int] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [str(value) for value in argv]
        self.calls.append(command)
        self.call_options.append(
            {
                "cwd": cwd,
                "env": env,
                "user": user,
                "group": group,
                "extra_groups": (
                    None if extra_groups is None else tuple(extra_groups)
                ),
            }
        )
        if command[:4] == ["git", "-C", str(cwd or command[2]), "status"]:
            return self._result(command)
        if command[0] == "git" and "status" in command:
            return self._result(command)
        if command[0] == "git" and "rev-parse" in command:
            return self._result(command, self.git_commit + "\n")
        if command[:3] == [
            str(release_manager_module._OPENSSL),
            "pkeyutl",
            "-verify",
        ]:
            if self.signature_valid:
                return self._result(command)
            return subprocess.CompletedProcess(command, 1, "", "bad signature")
        if command == ["sleep", "31"]:
            return self._result(command)
        if command[:2] == ["docker", "load"]:
            return self._result(command)
        if command[:3] == ["docker", "image", "load"]:
            self.offline_load_count += 1
            if self.fail_offline_load_number == self.offline_load_count:
                return subprocess.CompletedProcess(command, 1, "", "injected")
            archive_name = Path(command[-1]).stem
            self.loaded_offline_image_ids.add(
                self.manifest["images"][archive_name]["id"]
            )
            return self._result(command)
        if command[:3] == ["docker", "image", "inspect"]:
            ref = command[-1]
            if command[-2] == release_manager_module._OFFLINE_IMAGE_INSPECT_FORMAT:
                name = next(
                    name
                    for name in IMAGE_NAMES
                    if self.manifest["images"][name]["id"] == ref
                )
                if ref not in self.loaded_offline_image_ids:
                    return subprocess.CompletedProcess(command, 1, "", "No such image")
                value = self.offline_identity_override.get(
                    name,
                    f"{ref}|linux/amd64|1.6.0|{self.manifest['commit']}|"
                    f"{self.manifest['migration']['target']}",
                )
                return self._result(command, value + "\n")
            name = next(name for name in IMAGE_NAMES if self.manifest["images"][name]["ref"] == ref)
            image_id = self.target_id_override or self.manifest["images"][name]["id"]
            digests = (
                self.target_repo_digests_override
                if self.target_repo_digests_override is not None
                else ([ref] if self.manifest["mode"] == "production" else [])
            )
            return self._result(command, f"{image_id} linux/amd64 {json.dumps(digests)}\n")
        if command[:3] == ["docker", "volume", "ls"]:
            return self._result(command, self.volume_inventory)
        if command[:2] == ["docker", "inspect"]:
            if "range .Mounts" in command[-2]:
                if self.fail_beat_schedule_mount_inspection:
                    return subprocess.CompletedProcess(command, 1, "", "injected")
                return self._result(command, self.beat_schedule_mount)
            container = command[-1]
            service = next(
                (
                    name
                    for name, container_id in self.service_container_ids.items()
                    if container_id == container
                ),
                container.removeprefix("container-"),
            )
            name = _service_image_name(service)
            current_id = (
                self.manifest["images"][name]["id"]
                if self.runtime_refs[name] == self.manifest["images"][name]["ref"]
                else "sha256:" + dict(api="5", web="6", postgres="7", redis="8")[name] * 64
            )
            status = "exited" if not self.service_running[service] else "healthy"
            if service == self.unhealthy_service:
                status = "unhealthy"
            if (
                service in {*WORKER_SERVICES, "beat", "outbox-dispatcher"}
                and service != self.unhealthy_service
            ):
                status = "running" if self.service_running[service] else "exited"
            if "Config.Hostname" in command[-2]:
                return self._result(
                    command,
                    f"{self.service_container_ids[service]} {current_id} "
                    f"{self.runtime_refs[name]} {status} {self.service_hostnames[service]}\n",
                )
            if "Config.Image" in command[-2] and 'index .State "Health"' in command[-2]:
                return self._result(
                    command,
                    f"{current_id} {self.runtime_refs[name]} {status}\n",
                )
            if 'index .State "Health"' in command[-2]:
                identifier = (
                    self.service_container_ids[service] if "{{.Id}}" in command[-2] else current_id
                )
                return self._result(command, f"{identifier} {status}\n")
            return self._result(
                command,
                f"{self.service_container_ids[service]} {current_id} {self.runtime_refs[name]}\n",
            )
        if "compose" in command:
            if command[-2:] == ["config", "--quiet"]:
                return self._result(command)
            if command[-4:-1] == ["ps", "--all", "-q"]:
                service = command[-1]
                return self._result(command, f"{self.service_container_ids[service]}\n")
            if command[-3:] == ["ps", "--all", "-q"]:
                identifiers = [
                    self.service_container_ids[service]
                    for service in RUNTIME_SERVICES
                    if self.service_running[service]
                ]
                return self._result(
                    command,
                    "" if not identifiers else "\n".join(identifiers) + "\n",
                )
            if command[-3:-1] == ["ps", "-q"]:
                service = command[-1]
                value = (
                    f"{self.service_container_ids[service]}\n"
                    if self.service_running[service]
                    else ""
                )
                return self._result(command, value)
            if command[-4:] == ["exec", "-T", "postgres", "postgres"]:
                raise AssertionError("version command must include --version")
            if command[-5:] == ["exec", "-T", "postgres", "postgres", "--version"]:
                return self._result(command, self.postgres_version)
            if command[-5:] == ["exec", "-T", "redis", "redis-server", "--version"]:
                return self._result(command, self.redis_version)
            if (
                command[-6:-3] == ["exec", "-T", "postgres"]
                and "SELECT version_num FROM alembic_version" in command[-1]
                and "json_build_object" not in command[-1]
            ):
                return self._result(command, f"{self.migration_head}\n")
            if (
                command[-6:-3] == ["exec", "-T", "postgres"]
                and "'batch_queued'" in command[-1]
                and "'outbox_processing'" in command[-1]
            ):
                return self._result(command, json.dumps(self.recovery_watermark) + "\n")
            if (
                command[-6:-3] == ["exec", "-T", "postgres"]
                and "'database_oid'" in command[-1]
                and "'outbox_rows'" in command[-1]
            ):
                value = dict(self.recovery_database_fingerprint)
                value["migration_head"] = self.migration_head
                return self._result(command, json.dumps(value) + "\n")
            if command[-3:] == [
                "python",
                "-m",
                "scripts_support.recovery_crypto_probe",
            ]:
                self.recovery_crypto_probe_calls += 1
                return self._result(command, json.dumps(self.recovery_crypto_probe) + "\n")
            if command[-8:] == [
                "exec",
                "-T",
                "worker-realtime",
                "celery",
                "-A",
                "app.tasks",
                "inspect",
                "ping",
            ]:
                raise AssertionError("Celery ping must use a fixed timeout and JSON output")
            if command[-11:] == [
                "exec",
                "-T",
                "worker-realtime",
                "celery",
                "-A",
                "app.tasks",
                "inspect",
                "ping",
                "--timeout",
                "10",
                "--json",
            ]:
                if self.after_ping is not None:
                    self.after_ping()
                replies = {
                    f"celery@{self.service_hostnames[service]}": {"ok": "pong"}
                    for service in WORKER_SERVICES
                    if service != self.missing_worker_ping
                }
                return self._result(command, json.dumps(replies) + "\n")
            if command[-11:] == [
                "exec",
                "-T",
                "worker-realtime",
                "celery",
                "-A",
                "app.tasks",
                "inspect",
                "active_queues",
                "--timeout",
                "10",
                "--json",
            ]:
                replies = {}
                for service, configured_queue in WORKER_QUEUES.items():
                    queue = self.worker_queue_overrides.get(service, configured_queue)
                    replies[f"celery@{self.service_hostnames[service]}"] = [
                        {
                            "name": queue,
                            "routing_key": queue,
                            "exchange": {"name": queue},
                        }
                    ]
                return self._result(command, json.dumps(replies) + "\n")
            action = next(
                (value for value in ("stop", "up", "run") if value in command),
                None,
            )
            if action is not None:
                self.action_counts[action] = self.action_counts.get(action, 0) + 1
                should_fail = (
                    self.fail_action == action
                    and not self.failed_once
                    and self.action_counts[action] == self.fail_action_number
                )
                if action == "up" and (not should_fail or self.fail_after_effect):
                    environment_file = Path(command[command.index("--env-file") + 1])
                    env = {
                        line.split("=", 1)[0]: line.split("=", 1)[1]
                        for line in environment_file.read_text(encoding="utf-8").splitlines()
                        if "=" in line
                    }
                    keys = {
                        "api": "SMS_API_IMAGE",
                        "web": "SMS_WEB_IMAGE",
                        "postgres": "SMS_POSTGRES_IMAGE",
                        "redis": "SMS_REDIS_IMAGE",
                    }
                    services = command[command.index("120") + 1 :]
                    for service in services:
                        image_name = _service_image_name(service)
                        self.runtime_refs[image_name] = env[keys[image_name]]
                        self.service_running[service] = True
                        if (
                            self.unhealthy_service == service
                            and env[keys[image_name]]
                            != self.manifest["images"][image_name]["ref"]
                        ):
                            self.unhealthy_service = None
                        self.recreate_count += 1
                        self.service_container_ids[service] = hashlib.sha256(
                            f"recreated:{service}:{self.recreate_count}".encode()
                        ).hexdigest()
                if action == "stop" and not should_fail:
                    services = command[command.index("stop") + 1 :]
                    for service in services:
                        self.service_running[service] = False
                if should_fail:
                    if action == "run" and self.migration_head_after_failed_run is not None:
                        self.migration_head = self.migration_head_after_failed_run
                    self.failed_once = True
                    return subprocess.CompletedProcess(command, 1, "", "injected")
                if action == "run":
                    self.migration_head = (
                        self.migration_head_after_successful_run
                        or self.manifest["migration"]["target"]
                    )
                if self.fail_compensation and self.failed_once and action == "up":
                    return subprocess.CompletedProcess(command, 1, "", "compensation")
                if self.after_action is not None:
                    self.after_action(action, self.action_counts[action])
                return self._result(command)
        raise AssertionError(f"unexpected command: {command}")

    @staticmethod
    def _result(command: list[str], stdout: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout, "")


def _manager(
    tmp_path: Path,
    manifest: dict[str, Any],
    current_refs: dict[str, str],
) -> tuple[ReleaseManager, FakeRunner, Path, Path]:
    root, control_root, environment_file, release_root = _platform(
        tmp_path,
        current_refs,
        migration_from=manifest["migration"]["from"],
        migration_target=manifest["migration"]["target"],
    )
    runner = FakeRunner(manifest, current_refs)
    if manifest["mode"] == "production":
        manager = ReleaseManager(
            platform_root=root,
            control_root=control_root,
            environment_file=environment_file,
            release_root=release_root,
            mode="production",
            runner=runner,
            expected_staging_uid=os.geteuid(),
        )
    else:
        manager = ReleaseManager(
            root=root,
            release_root=release_root,
            mode="development",
            runner=runner,
            expected_staging_uid=os.geteuid(),
        )
    return manager, runner, root, release_root


def _restart_production_manager(
    manager: ReleaseManager,
    runner: FakeRunner,
) -> ReleaseManager:
    return ReleaseManager(
        platform_root=manager.platform_root,
        control_root=manager.control_root,
        environment_file=manager.environment_file,
        release_root=manager.release_root,
        mode="production",
        runner=runner,
        expected_staging_uid=os.geteuid(),
    )


def _switch_production_control_snapshot(
    manager: ReleaseManager,
    runner: FakeRunner,
    commit: str,
) -> ReleaseManager:
    control_root = manager.control_root.parent / commit
    shutil.copytree(manager.control_root, control_root)
    return ReleaseManager(
        platform_root=manager.platform_root,
        control_root=control_root,
        environment_file=manager.environment_file,
        release_root=manager.release_root,
        mode="production",
        runner=runner,
        expected_staging_uid=os.geteuid(),
    )


def _bundle_for_changes(
    tmp_path: Path,
    changed: set[str],
) -> tuple[Path, dict[str, Any], dict[str, str]]:
    manifest_path, manifest, _ = _bundle(tmp_path)
    for archive in manifest_path.parent.glob("*.tar"):
        archive.unlink()
    for name in IMAGE_NAMES:
        image = manifest["images"][name]
        image["changed"] = name in changed
        if name in changed:
            archive = manifest_path.parent / f"{name}.tar"
            archive.write_bytes(f"verified-{name}-archive".encode())
            archive.chmod(0o600)
            image["archive_file"] = archive.name
            image["archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
        else:
            image["archive_file"] = None
            image["archive_sha256"] = None
    data_changed = bool({"postgres", "redis"} & changed)
    manifest["evidence"]["data_images"] = "data-images.json" if data_changed else None
    data_path = manifest_path.parent / "data-images.json"
    if data_changed:
        _write_private_json(data_path, _data_report(manifest))
    elif data_path.exists():
        data_path.unlink()
    _write_bound_release_report(
        manifest_path.parent / "release-gate.json",
        manifest,
        _release_report(manifest),
    )
    _write_private_json(manifest_path, manifest)
    current_refs = {
        name: (
            f"sms-platform-{name}:previous" if name in changed else manifest["images"][name]["ref"]
        )
        for name in IMAGE_NAMES
    }
    return manifest_path, manifest, current_refs


def _production_baseline_bundle(
    tmp_path: Path,
) -> tuple[Path, dict[str, Any], dict[str, str]]:
    manifest_path, manifest, _ = _bundle(tmp_path, mode="production")
    for image in manifest["images"].values():
        image["changed"] = False
    manifest["migration"] = {"from": "0012", "target": "0012", "compatibility": "none"}
    manifest["evidence"]["backup_restore_change"] = None
    for name in ("backup-change.json", "restore-report.json"):
        (manifest_path.parent / name).unlink()
    _write_bound_release_report(
        manifest_path.parent / "release-gate.json",
        manifest,
        _release_report(manifest),
    )
    _write_private_json(manifest_path, manifest)
    refs = {name: manifest["images"][name]["ref"] for name in IMAGE_NAMES}
    return manifest_path, manifest, refs


def _recovery_evidence(
    tmp_path: Path,
    environment_file: Path,
    manifest: dict[str, Any],
) -> tuple[Path, str, Path, str, Path, str]:
    snapshot_dir = tmp_path / "verified-snapshot"
    snapshot_dir.mkdir(mode=0o700)
    snapshot_dir.chmod(0o700)
    contents = {
        "database": ("sms_snapshot.dump.enc", b"encrypted-production-database"),
        "repository_archive": ("repository_snapshot.tar.gz", b"tracked-repository"),
        "environment": ("production.env", environment_file.read_bytes()),
    }
    files: dict[str, dict[str, object]] = {}
    for label, (name, payload) in contents.items():
        path = snapshot_dir / name
        path.write_bytes(payload)
        path.chmod(0o600)
        files[label] = {
            "name": name,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }
    now = datetime.now(UTC)
    created_at = (now - timedelta(hours=2)).isoformat()
    window_ended_at = (now - timedelta(hours=1)).isoformat()
    approved_at = (now - timedelta(minutes=30)).isoformat()
    snapshot_manifest = snapshot_dir / "manifest.json"
    _write_private_json(
        snapshot_manifest,
        {
            "schema_version": 1,
            "snapshot_id": "20260714T060000Z_cccccccccccc",
            "created_at": created_at,
            "git_commit": manifest["commit"],
            "alembic_version": manifest["migration"]["target"],
            "database": "sms",
            "secrets_included": False,
            "recovery_crypto_generation_id": "recovery-generation-01",
            "backup_passphrase_generation_id": "backup-generation-01",
            "files": files,
        },
    )
    snapshot_sha256 = hashlib.sha256(snapshot_manifest.read_bytes()).hexdigest()
    checksum_lines = [
        f"{item['sha256']}  {item['name']}" for item in files.values()
    ] + [f"{snapshot_sha256}  manifest.json"]
    checksums = snapshot_dir / "SHA256SUMS"
    checksums.write_text("\n".join(checksum_lines) + "\n", encoding="ascii")
    checksums.chmod(0o600)

    live_database = {
        "database": "sms",
        "database_oid": "16384",
        "migration_head": manifest["migration"]["target"],
        "batch_rows": 10,
        "chunk_rows": 9,
        "outbox_rows": 4,
        "max_batch_id": 10,
        "max_chunk_id": 9,
    }
    live_digest = hashlib.sha256(
        json.dumps(live_database, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    crypto_probe = _restore_crypto_probe_receipt()
    crypto_probe_digest = hashlib.sha256(
        json.dumps(crypto_probe, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    receipt_dir = tmp_path / "approved-restore-receipt"
    receipt_dir.mkdir(mode=0o700)
    receipt_dir.chmod(0o700)
    receipt = receipt_dir / "restore-receipt.json"
    restored_at = (now - timedelta(minutes=45)).isoformat()
    receipt_approved_at = (now - timedelta(minutes=20)).isoformat()
    _write_private_json(
        receipt,
        {
            "schema_version": 1,
            "record_type": "production_recovery_restore_receipt",
            "status": "approved",
            "snapshot_id": "20260714T060000Z_cccccccccccc",
            "snapshot_manifest_sha256": snapshot_sha256,
            "snapshot_database_sha256": files["database"]["sha256"],
            "git_commit": manifest["commit"],
            "migration_head": manifest["migration"]["target"],
            "database": "sms",
            "recovery_crypto_generation_id": "recovery-generation-01",
            "backup_passphrase_generation_id": "backup-generation-01",
            "live_database_fingerprint_sha256": live_digest,
            "crypto_probe_status": "performed",
            "crypto_probe_sha256": crypto_probe_digest,
            "restored_at": restored_at,
            "approved_by": ["operator01", "reviewer02"],
            "approved_at": receipt_approved_at,
        },
    )
    receipt_sha256 = hashlib.sha256(receipt.read_bytes()).hexdigest()

    fence_dir = tmp_path / "approved-gap-fence"
    fence_dir.mkdir(mode=0o700)
    fence_dir.chmod(0o700)
    fence = fence_dir / "gap-fence.json"
    _write_private_json(
        fence,
        {
            "schema_version": 1,
            "record_type": "production_recovery_gap_fence",
            "status": "approved",
            "snapshot_id": "20260714T060000Z_cccccccccccc",
            "snapshot_manifest_sha256": snapshot_sha256,
            "git_commit": manifest["commit"],
            "migration_head": manifest["migration"]["target"],
            "window_started_at": created_at,
            "window_ended_at": window_ended_at,
            "upstream_request_count": 7,
            "vendor_accepted_or_sent_count": 3,
            "vendor_not_accepted_count": 2,
            "vendor_unknown_count": 2,
            "old_primary_isolated": True,
            "upstream_retries_frozen": True,
            "unknown_results_blocked": True,
            "automatic_resend_forbidden": True,
            "approved_by": ["operator01", "reviewer02"],
            "approved_at": approved_at,
        },
    )
    fence_sha256 = hashlib.sha256(fence.read_bytes()).hexdigest()
    return (
        snapshot_manifest,
        snapshot_sha256,
        receipt,
        receipt_sha256,
        fence,
        fence_sha256,
    )


def _start_and_adopt_recovery(
    manager: ReleaseManager,
    runner: FakeRunner,
    manifest_path: Path,
    manifest: dict[str, Any],
    evidence: tuple[Path, str, Path, str, Path, str],
) -> dict[str, object]:
    snapshot_path, snapshot_sha, _, _, _, _ = evidence
    started = manager.start_recovery(
        manifest_path,
        snapshot_manifest_path=snapshot_path,
        snapshot_manifest_sha256=snapshot_sha,
        runtime_secrets_target=RECOVERY_RUNTIME_TARGET,
        confirmed_recovered_host=True,
    )
    assert started["phase"] == "data_started"
    evidence = _approve_gap_after_recovery_start(evidence)
    _, _, _, _, fence_path, fence_sha = evidence
    runner.migration_head = manifest["migration"]["target"]
    receipt_path, receipt_sha = _observe_and_approve_recovery(
        manager,
        manifest_path,
        evidence,
    )
    adopted = manager.adopt_recovery(
        manifest_path,
        snapshot_manifest_path=snapshot_path,
        snapshot_manifest_sha256=snapshot_sha,
        restore_receipt_path=receipt_path,
        restore_receipt_sha256=receipt_sha,
        gap_fence_path=fence_path,
        gap_fence_sha256=fence_sha,
        runtime_secrets_target=RECOVERY_RUNTIME_TARGET,
        confirmed_recovered_host=True,
    )
    assert adopted["phase"] == "adopted"
    return adopted


def _approve_gap_after_recovery_start(
    evidence: tuple[Path, str, Path, str, Path, str],
) -> tuple[Path, str, Path, str, Path, str]:
    snapshot_path, snapshot_sha, receipt_path, receipt_sha, fence_path, _ = evidence
    fence = json.loads(fence_path.read_text(encoding="utf-8"))
    fence["approved_at"] = datetime.now(UTC).isoformat()
    _write_private_json(fence_path, fence)
    return (
        snapshot_path,
        snapshot_sha,
        receipt_path,
        receipt_sha,
        fence_path,
        hashlib.sha256(fence_path.read_bytes()).hexdigest(),
    )


def _observe_and_approve_recovery(
    manager: ReleaseManager,
    manifest_path: Path,
    evidence: tuple[Path, str, Path, str, Path, str],
) -> tuple[Path, str]:
    snapshot_path, snapshot_sha, receipt_seed, _, _, _ = evidence
    output = receipt_seed.parent / "observed-restore-receipt.json"
    template = manager.observe_recovery(
        manifest_path,
        snapshot_manifest_path=snapshot_path,
        snapshot_manifest_sha256=snapshot_sha,
        output_path=output,
        runtime_secrets_target=RECOVERY_RUNTIME_TARGET,
        confirmed_recovered_host=True,
    )
    approved = dict(template)
    approved["status"] = "approved"
    approved["approved_by"] = ["operator01", "reviewer02"]
    approved["approved_at"] = datetime.now(UTC).isoformat()
    _write_private_json(output, approved)
    return output, hashlib.sha256(output.read_bytes()).hexdigest()


def _finish_recovery(manager: ReleaseManager) -> dict[str, object]:
    result: dict[str, object] = {}
    for stage in ("api", "callback", "workers", "outbox", "beat", "web"):
        result = manager.resume_recovery(
            stage=stage,  # type: ignore[arg-type]
            runtime_secrets_target=RECOVERY_RUNTIME_TARGET,
            confirmed_recovered_host=True,
        )
    return result


def _forward_candidate_bundle(
    tmp_path: Path,
    source: dict[str, Any],
    *,
    direct_previous_ref: str | None = None,
    direct_previous_id: str | None = None,
    downgrade: bool = False,
) -> tuple[Path, dict[str, Any]]:
    manifest_path, manifest, _ = _bundle(tmp_path, mode="production")
    manifest["release_id"] = "release-forward-rollback"
    manifest["commit"] = "d" * 40
    for name in IMAGE_NAMES:
        manifest["images"][name].update(source["images"][name])
        manifest["images"][name]["archive_file"] = None
        manifest["images"][name]["archive_sha256"] = None
        manifest["images"][name]["changed"] = False
    web = manifest["images"]["web"]
    web["ref"] = direct_previous_ref or (
        "registry.example.com/sms/web@sha256:" + "5" * 64
    )
    web["id"] = direct_previous_id or ("sha256:" + "f" * 64)
    web["changed"] = True
    if downgrade:
        manifest["migration"] = {"from": "0012", "target": "0011", "compatibility": "expand"}
    else:
        manifest["migration"] = {"from": "0012", "target": "0012", "compatibility": "none"}
        manifest["evidence"]["backup_restore_change"] = None
        for name in ("backup-change.json", "restore-report.json"):
            (manifest_path.parent / name).unlink()
    _write_bound_release_report(
        manifest_path.parent / "release-gate.json",
        manifest,
        _release_report(manifest),
    )
    if downgrade:
        report_path = manifest_path.parent / "restore-report.json"
        report = _restore_report()
        report["git_commit"] = manifest["commit"]
        report["checks"]["alembic_version"] = "0012"
        _write_private_json(report_path, report)
        _write_private_json(
            manifest_path.parent / "backup-change.json",
            _production_change_record(manifest, report_path),
        )
    _write_private_json(manifest_path, manifest)
    return manifest_path, manifest


def _control_smoke_bundle(
    tmp_path: Path,
) -> tuple[Path, dict[str, Any], dict[str, str]]:
    manifest_path, manifest, _ = _bundle(tmp_path)
    for name in IMAGE_NAMES:
        manifest["images"][name]["ref"] = CONTROL_SMOKE_IMAGES[name]["ref"]
        manifest["images"][name]["id"] = CONTROL_SMOKE_IMAGES[name]["id"]
        if name != "web":
            manifest["images"][name]["changed"] = False
            manifest["images"][name]["archive_file"] = None
            manifest["images"][name]["archive_sha256"] = None
    manifest["images"]["web"]["changed"] = True
    manifest["evidence"] = {
        "release_gate_kind": "release_control_smoke",
        "release_gate": "release-gate.json",
        "release_gate_sha256": "0" * 64,
        "data_images": None,
        "backup_restore_change": None,
    }
    _write_bound_release_report(
        manifest_path.parent / "release-gate.json",
        manifest,
        _control_smoke_report(manifest),
    )
    _write_private_json(manifest_path, manifest)
    current_refs = {
        name: (
            "sms-platform-web:amd64-previous"
            if name == "web"
            else CONTROL_SMOKE_IMAGES[name]["ref"]
        )
        for name in IMAGE_NAMES
    }
    return manifest_path, manifest, current_refs


def test_prepare_copies_closed_bundle_and_records_safe_prepared_snapshot(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path)
    manager, runner, _, release_root = _manager(tmp_path, manifest, current_refs)

    manager.prepare(manifest_path)

    release_dir = release_root / manifest["release_id"]
    state = manager.status(manifest["release_id"])
    assert state["state"] == "prepared"
    assert stat.S_IMODE((release_dir / "manifest.json").stat().st_mode) == 0o600
    assert (release_dir / "artifacts" / "web.tar").read_bytes() == (
        manifest_path.parent / "web.tar"
    ).read_bytes()
    snapshot = json.loads((release_dir / "current-snapshot.json").read_text(encoding="utf-8"))
    assert set(snapshot) == {
        "current_commit",
        "current_refs",
        "container_ids",
        "image_ids",
        "migration_head",
        "service_container_ids",
        "target_commit",
        "target_image_ids",
    }
    assert snapshot["service_container_ids"] == runner.service_container_ids
    events = [
        json.loads(line)
        for line in (release_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert events
    assert {event["kind"] for event in events} == {"intent", "observation"}
    assert all(event["step"] == "external_command" for event in events)
    assert not any(
        token in command
        for command in runner.calls
        for token in ("up", "down", "stop", "rm", "pull")
    )
    assert any(
        "postgres" in command and "SELECT version_num FROM alembic_version" in command[-1]
        for command in runner.calls
    )
    assert not any(
        command[-5:] == ["exec", "-T", "api", "alembic", "current"]
        for command in runner.calls
    )


def test_migration_target_must_be_a_static_forward_descendant(tmp_path: Path) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path)
    manager, _, _, _ = _manager(tmp_path, manifest, current_refs)
    manifest["migration"] = {"from": "0012", "target": "0011", "compatibility": "expand"}
    _write_private_json(manifest_path, manifest)
    reversed_manifest = load_manifest(manifest_path)

    with pytest.raises(ReleaseManagerError, match="forward descendant"):
        manager._validate_migration_direction(reversed_manifest)

    manifest["migration"] = {
        "from": "missing_revision",
        "target": "missing_revision",
        "compatibility": "none",
    }
    _write_private_json(manifest_path, manifest)
    missing_manifest = load_manifest(manifest_path)
    with pytest.raises(ReleaseManagerError, match="endpoints are absent"):
        manager._validate_migration_direction(missing_manifest)


def test_production_reads_compose_and_migration_graph_only_from_control_root(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path, mode="production")
    manager, _, root, _ = _manager(tmp_path, manifest, current_refs)
    loaded = load_manifest(manifest_path)

    compose = manager._compose()
    assert compose[compose.index("--env-file") + 1] == str(manager.environment_file)
    compose_files = [
        Path(compose[index + 1])
        for index, value in enumerate(compose)
        if value == "-f"
    ]
    assert compose_files
    assert all(path.is_relative_to(manager.control_root) for path in compose_files)
    assert all(not path.is_relative_to(root) for path in compose_files)
    assert manager.environment_file != root / ".env"
    (root / ".env").write_text("checkout controlled\n", encoding="utf-8")
    assert manager._root_env_refs() == current_refs

    platform_versions = root / "backend" / "migrations" / "versions"
    for path in platform_versions.glob("*.py"):
        path.write_text("this is not valid Python", encoding="utf-8")
    manager._validate_migration_direction(loaded)

    control_revision = next(
        path
        for path in (manager.control_root / "backend" / "migrations" / "versions").glob(
            "*.py"
        )
        if loaded.migration_target in path.read_text(encoding="utf-8")
    )
    control_revision.write_text("this is not valid Python", encoding="utf-8")
    with pytest.raises(ReleaseManagerError, match="migration graph is invalid"):
        manager._validate_migration_direction(loaded)


def test_release_git_checks_disable_optional_index_refresh_and_repo_helpers(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path)
    manager, runner, root, _ = _manager(tmp_path, manifest, current_refs)

    manager.prepare(manifest_path)

    status = next(command for command in runner.calls if "status" in command)
    assert status[:3] == ["git", "--no-optional-locks", "--no-replace-objects"]
    assert status[3:5] == ["-c", "core.fsmonitor=false"]
    assert status[5:7] == ["-c", f"core.hooksPath={os.devnull}"]
    assert status[7:9] == ["-c", "core.untrackedCache=false"]
    assert status[9:11] == ["-c", f"safe.directory={root}"]
    assert status[11:] == [
        "-C",
        str(root),
        "status",
        "--porcelain",
        "--untracked-files=normal",
    ]
    options = runner.call_options[runner.calls.index(status)]
    assert options["env"] == manager._git_environment()
    assert options["user"] is None
    assert options["group"] is None
    assert options["extra_groups"] is None


def test_root_production_git_checks_run_as_fixed_operator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, manifest, current_refs = _bundle(tmp_path, mode="production")
    manager, runner, root, _ = _manager(tmp_path, manifest, current_refs)
    monkeypatch.setattr(release_manager_module.os, "geteuid", lambda: 0)

    assert manager._current_git_commit() == COMMIT

    assert len(runner.calls) == 2
    for command, options in zip(runner.calls, runner.call_options, strict=True):
        assert command[command.index("-C") + 1] == str(root)
        assert command[3:5] == ["-c", "core.fsmonitor=false"]
        assert command[5:7] == ["-c", f"core.hooksPath={os.devnull}"]
        assert options == {
            "cwd": root,
            "env": manager._git_environment(),
            "user": 1000,
            "group": 1000,
            "extra_groups": (),
        }


def test_production_release_binds_control_snapshot_leaf_to_manifest_commit(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path, mode="production")
    manager, runner, _, release_root = _manager(tmp_path, manifest, current_refs)
    wrong_control_root = manager.control_root.parent / ("d" * 40)
    shutil.copytree(manager.control_root, wrong_control_root)
    manager = ReleaseManager(
        platform_root=manager.platform_root,
        control_root=wrong_control_root,
        environment_file=manager.environment_file,
        release_root=release_root,
        mode="production",
        runner=runner,
        expected_staging_uid=os.geteuid(),
    )

    with pytest.raises(ReleaseManagerError, match="prepare failed") as error:
        manager.prepare(manifest_path)
    assert isinstance(error.value.__cause__, ReleaseManagerError)
    assert "control snapshot" in str(error.value.__cause__)
    assert not any(command[0] == "git" for command in runner.calls)


@pytest.mark.parametrize("action", ["activate", "resume", "rollback"])
def test_production_mutation_rejects_wrong_control_snapshot_before_runtime(
    tmp_path: Path,
    action: str,
) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path, mode="production")
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    wrong_control_root = manager.control_root.parent / ("d" * 40)
    shutil.copytree(manager.control_root, wrong_control_root)
    manager = ReleaseManager(
        platform_root=manager.platform_root,
        control_root=wrong_control_root,
        environment_file=manager.environment_file,
        release_root=manager.release_root,
        mode="production",
        runner=runner,
        expected_staging_uid=os.geteuid(),
    )
    runner.calls.clear()

    with pytest.raises(ReleaseManagerError, match="control snapshot"):
        getattr(manager, action)(manifest["release_id"])

    assert manager.status(manifest["release_id"])["state"] == "prepared"
    assert not runner.calls


def test_prepare_never_rereads_staging_manifest_after_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import release_manager as release_manager_module

    manifest_path, manifest, current_refs = _bundle(tmp_path)
    original_bytes = manifest_path.read_bytes()
    replacement = json.loads(original_bytes)
    replacement["commit"] = "d" * 40
    real_load_manifest = release_manager_module.load_manifest

    def swap_manifest_after_parse(path: Path) -> Any:
        parsed = real_load_manifest(path)
        _write_private_json(path, replacement)
        return parsed

    monkeypatch.setattr(release_manager_module, "load_manifest", swap_manifest_after_parse)
    manager, _, _, release_root = _manager(tmp_path, manifest, current_refs)

    manager.prepare(manifest_path)

    stored = release_root / manifest["release_id"] / "manifest.json"
    assert stored.read_bytes() == original_bytes


def test_identical_repeated_prepare_is_idempotent(tmp_path: Path) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path)
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)

    manager.prepare(manifest_path)
    call_count = len(runner.calls)
    manager.prepare(manifest_path)

    assert len(runner.calls) == call_count


def test_duplicate_release_id_rejects_a_different_manifest(tmp_path: Path) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path)
    manager, _, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    changed_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    changed_manifest["migration"]["target"] = "0013"
    _write_private_json(manifest_path, changed_manifest)

    with pytest.raises(ReleaseManagerError, match="prepare failed"):
        manager.prepare(manifest_path)

    assert manager.status(manifest["release_id"])["state"] == "prepared"


@pytest.mark.parametrize("unsafe", ["extra", "directory", "symlink", "mode", "owner"])
def test_prepare_rejects_open_or_unsafe_staging_bundle(
    tmp_path: Path,
    unsafe: str,
) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path)
    if unsafe == "extra":
        extra = manifest_path.parent / "extra.json"
        extra.write_text("{}", encoding="utf-8")
        extra.chmod(0o600)
    elif unsafe == "directory":
        (manifest_path.parent / "release-gate.json").unlink()
        (manifest_path.parent / "release-gate.json").mkdir()
    elif unsafe == "symlink":
        (manifest_path.parent / "release-gate.json").unlink()
        (manifest_path.parent / "release-gate.json").symlink_to("manifest.json")
    elif unsafe == "mode":
        manifest_path.chmod(0o644)
    manager, _, root, release_root = _manager(tmp_path, manifest, current_refs)
    if unsafe == "owner":
        manager = ReleaseManager(
            root=root,
            release_root=release_root,
            mode=manifest["mode"],
            runner=FakeRunner(manifest, current_refs),
            expected_staging_uid=os.geteuid() + 1,
        )

    with pytest.raises(ReleaseManagerError, match="staging"):
        manager.prepare(manifest_path)


def test_prepare_failure_marks_failed_without_env_or_container_mutation(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path)
    manager, runner, root, release_root = _manager(tmp_path, manifest, current_refs)
    original_env = manager.environment_file.read_bytes()
    runner.git_commit = "b" * 40

    with pytest.raises(ReleaseManagerError, match="prepare failed"):
        manager.prepare(manifest_path)

    assert manager.status(manifest["release_id"])["state"] == "failed"
    assert manager.environment_file.read_bytes() == original_env
    assert not any(
        token in command
        for command in runner.calls
        for token in ("up", "down", "stop", "rm", "pull")
    )
    assert (release_root / manifest["release_id"] / "artifacts").is_dir()


def test_development_loads_only_changed_archive_and_verifies_target_id(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path)
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)

    manager.prepare(manifest_path)

    loads = [command for command in runner.calls if command[:2] == ["docker", "load"]]
    assert len(loads) == 1
    assert loads[0][-1].endswith("/artifacts/web.tar")


def test_production_offline_import_uses_frozen_archives_and_fixed_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, manifest, current_refs = _offline_bundle(tmp_path)
    public_key, _ = _configure_offline_trust(tmp_path, monkeypatch)
    manager, runner, _, release_root = _manager(tmp_path, manifest, current_refs)

    manager.prepare(manifest_path)

    artifacts = release_root / manifest["release_id"] / "artifacts"
    loads = [
        command
        for command in runner.calls
        if command[:3] == ["docker", "image", "load"]
    ]
    assert loads == [
        [
            "docker",
            "image",
            "load",
            "--quiet",
            "--platform",
            "linux/amd64",
            "--input",
            str(artifacts / f"{name}.tar"),
        ]
        for name in IMAGE_NAMES
    ]
    assert all(str(manifest_path.parent) not in command[-1] for command in loads)
    assert {
        command[-1]
        for command in runner.calls
        if command[:3] == ["docker", "image", "inspect"]
        and command[-2] == release_manager_module._OFFLINE_IMAGE_INSPECT_FORMAT
    } == {manifest["images"][name]["id"] for name in IMAGE_NAMES}
    signature_commands = [
        command
        for command in runner.calls
        if command[:3]
        == [str(release_manager_module._OPENSSL), "pkeyutl", "-verify"]
    ]
    assert signature_commands == [
        [
            str(release_manager_module._OPENSSL),
            "pkeyutl",
            "-verify",
            "-pubin",
            "-inkey",
            str(public_key),
            "-sigfile",
            str(artifacts / "manifest.sig"),
            "-rawin",
            "-in",
            str(release_root / manifest["release_id"] / "manifest.json"),
        ]
    ]
    events = [
        json.loads(line)
        for line in (release_root / manifest["release_id"] / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert sum(
        event["kind"] == "intent"
        and event["step"] == "external_command"
        and event["details"].get("check") == "production offline image load"
        for event in events
    ) == 4
    assert sum(
        event["kind"] == "observation"
        and event["step"] == "external_command"
        and event["details"].get("check") == "production offline image load"
        and event["details"].get("passed") is True
        for event in events
    ) == 4
    assert manager.status(manifest["release_id"])["state"] == "prepared"


def test_production_offline_validates_all_four_archives_before_any_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, manifest, current_refs = _offline_bundle(tmp_path)
    _configure_offline_trust(tmp_path, monkeypatch)
    archive = manifest_path.parent / "redis.tar"
    payload = archive.read_bytes()
    archive.write_bytes(payload[:-1] + bytes([payload[-1] ^ 1]))
    archive.chmod(0o600)
    manager, runner, root, _ = _manager(tmp_path, manifest, current_refs)
    original_env = manager.environment_file.read_bytes()

    with pytest.raises(ReleaseManagerError, match="prepare failed"):
        manager.prepare(manifest_path)

    assert not any(
        command[:3] == ["docker", "image", "load"] for command in runner.calls
    )
    assert manager.environment_file.read_bytes() == original_env


def test_production_offline_import_is_idempotent_for_verified_raw_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, manifest, current_refs = _offline_bundle(tmp_path)
    _configure_offline_trust(tmp_path, monkeypatch)
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    runner.loaded_offline_image_ids = {
        manifest["images"][name]["id"] for name in IMAGE_NAMES
    }

    manager.prepare(manifest_path)

    assert not any(
        command[:3] == ["docker", "image", "load"] for command in runner.calls
    )
    inspections = [
        command
        for command in runner.calls
        if command[:3] == ["docker", "image", "inspect"]
        and command[-2] == release_manager_module._OFFLINE_IMAGE_INSPECT_FORMAT
    ]
    assert len(inspections) == 4


def test_production_offline_partial_load_failure_never_mutates_runtime_or_prunes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, manifest, current_refs = _offline_bundle(tmp_path)
    _configure_offline_trust(tmp_path, monkeypatch)
    manager, runner, root, _ = _manager(tmp_path, manifest, current_refs)
    runner.fail_offline_load_number = 2
    original_env = manager.environment_file.read_bytes()

    with pytest.raises(ReleaseManagerError, match="prepare failed"):
        manager.prepare(manifest_path)

    loads = [
        command
        for command in runner.calls
        if command[:3] == ["docker", "image", "load"]
    ]
    assert len(loads) == 2
    assert manager.environment_file.read_bytes() == original_env
    assert not any(
        token in {"up", "down", "stop", "rm", "prune"}
        for command in runner.calls
        for token in command
    )
    assert manager.status(manifest["release_id"])["state"] == "failed"


def test_production_offline_full_no_migration_update_can_compensate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, manifest, current_refs = _offline_bundle(
        tmp_path,
        include_conditional_evidence=False,
    )
    _configure_offline_trust(tmp_path, monkeypatch)
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    runner.fail_action = "up"

    with pytest.raises(ReleaseManagerError, match="rolled_back"):
        manager.activate(manifest["release_id"])

    assert manager.status(manifest["release_id"])["state"] == "rolled_back"


def test_production_offline_full_expand_update_can_activate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, manifest, current_refs = _offline_bundle(
        tmp_path,
        migration_changed=True,
    )
    _configure_offline_trust(tmp_path, monkeypatch)
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)

    manager.prepare(manifest_path)
    assert manager.status(manifest["release_id"])["state"] == "prepared"

    manager.activate(manifest["release_id"])

    state = manager.status(manifest["release_id"])
    assert state["state"] == "succeeded"
    assert state["verified_migration_head"] == manifest["migration"]["target"]
    assert runner.migration_head == manifest["migration"]["target"]
    with pytest.raises(ReleaseManagerError, match="requires a forward rollback candidate"):
        manager.rollback(manifest["release_id"])
    assert manager.status(manifest["release_id"])["state"] == "succeeded"


def test_production_offline_report_queue_expand_update_can_activate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, manifest, current_refs = _offline_bundle(
        tmp_path,
        migration_pair=OFFLINE_REPORT_EXPAND_MIGRATION,
    )
    _configure_offline_trust(tmp_path, monkeypatch)
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)

    manager.prepare(manifest_path)
    manager.activate(manifest["release_id"])

    state = manager.status(manifest["release_id"])
    assert state["state"] == "succeeded"
    assert state["verified_migration_head"] == OFFLINE_REPORT_EXPAND_MIGRATION[1]
    assert runner.migration_head == OFFLINE_REPORT_EXPAND_MIGRATION[1]


def test_production_offline_auth_security_expand_update_can_activate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, manifest, current_refs = _offline_bundle(
        tmp_path,
        migration_pair=OFFLINE_AUTH_EXPAND_MIGRATION,
    )
    _configure_offline_trust(tmp_path, monkeypatch)
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)

    manager.prepare(manifest_path)
    manager.activate(manifest["release_id"])

    state = manager.status(manifest["release_id"])
    assert state["state"] == "succeeded"
    assert state["verified_migration_head"] == OFFLINE_AUTH_EXPAND_MIGRATION[1]
    assert runner.migration_head == OFFLINE_AUTH_EXPAND_MIGRATION[1]


def test_succeeded_offline_expand_source_still_rejects_forward_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, manifest, current_refs = _offline_bundle(
        tmp_path,
        migration_changed=True,
    )
    _configure_offline_trust(tmp_path, monkeypatch)
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    manager.activate(manifest["release_id"])
    candidate_root = tmp_path / "registry-forward-candidate"
    candidate_root.mkdir()
    candidate_path, _, _ = _bundle(candidate_root, mode="production")
    runner.calls.clear()

    with pytest.raises(ReleaseManagerError, match="do not support forward rollback"):
        manager.prepare_forward_rollback(manifest["release_id"], candidate_path)

    assert not runner.calls
    assert manager.status(manifest["release_id"])["state"] == "succeeded"


def test_production_offline_full_expand_staged_resume_revalidates_and_activates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, manifest, current_refs = _offline_bundle(
        tmp_path,
        migration_changed=True,
    )
    _configure_offline_trust(tmp_path, monkeypatch)
    manager, _, _, _ = _manager(tmp_path, manifest, current_refs)
    validate_git = manager._validate_git
    interrupted = False

    def interrupt_after_bundle_copy(parsed: Any) -> str:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return validate_git(parsed)

    monkeypatch.setattr(manager, "_validate_git", interrupt_after_bundle_copy)
    with pytest.raises(KeyboardInterrupt):
        manager.prepare(manifest_path)

    assert manager.status(manifest["release_id"])["state"] == "staged"
    monkeypatch.setattr(manager, "_validate_git", validate_git)

    manager.resume(manifest["release_id"])

    assert manager.status(manifest["release_id"])["state"] == "succeeded"


@pytest.mark.parametrize(
    ("observed_head", "expected_state", "residual"),
    [
        (OFFLINE_EXPAND_MIGRATION[0], "rolled_back", []),
        (
            OFFLINE_EXPAND_MIGRATION[1],
            "rolled_back",
            [
                "image:postgres",
                "image:redis",
                f"migration:{OFFLINE_EXPAND_MIGRATION[1]}",
            ],
        ),
        ("0080_partial", "recovery_required", None),
    ],
)
def test_production_offline_expand_migrate_failure_uses_observed_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    observed_head: str,
    expected_state: str,
    residual: list[str] | None,
) -> None:
    manifest_path, manifest, current_refs = _offline_bundle(
        tmp_path,
        migration_changed=True,
    )
    _configure_offline_trust(tmp_path, monkeypatch)
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    runner.fail_action = "run"
    runner.migration_head_after_failed_run = observed_head

    with pytest.raises(ReleaseManagerError, match=expected_state):
        manager.activate(manifest["release_id"])

    state = manager.status(manifest["release_id"])
    assert state["state"] == expected_state
    if residual is None:
        assert "residual_changes" not in state
        return
    assert state["residual_changes"] == residual
    expected_refs = current_refs.copy()
    if observed_head == manifest["migration"]["target"]:
        expected_refs.update(
            {
                name: manifest["images"][name]["ref"]
                for name in ("postgres", "redis")
            }
        )
    assert manager._root_env_refs() == expected_refs
    assert runner.runtime_refs == expected_refs


def test_production_offline_expand_backend_failure_retains_schema_and_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, manifest, current_refs = _offline_bundle(
        tmp_path,
        migration_changed=True,
    )
    _configure_offline_trust(tmp_path, monkeypatch)
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    runner.fail_action = "up"
    runner.fail_action_number = 3
    runner.fail_after_effect = True

    with pytest.raises(ReleaseManagerError, match="rolled_back"):
        manager.activate(manifest["release_id"])

    expected_refs = current_refs.copy()
    expected_refs.update(
        {
            name: manifest["images"][name]["ref"]
            for name in ("postgres", "redis")
        }
    )
    state = manager.status(manifest["release_id"])
    assert state["state"] == "rolled_back"
    assert state["residual_changes"] == [
        "image:postgres",
        "image:redis",
        f"migration:{OFFLINE_EXPAND_MIGRATION[1]}",
    ]
    assert runner.migration_head == manifest["migration"]["target"]
    assert manager._root_env_refs() == expected_refs
    assert runner.runtime_refs == expected_refs


def test_production_offline_expand_explicit_rollback_retains_schema_and_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, manifest, current_refs = _offline_bundle(
        tmp_path,
        migration_changed=True,
    )
    _configure_offline_trust(tmp_path, monkeypatch)
    manager, runner, root, release_root = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)

    def interrupt_after_migrate(action: str, _count: int) -> None:
        if action == "run":
            manager.request_stop(signal.SIGTERM, None)

    runner.after_action = interrupt_after_migrate
    with pytest.raises(ReleaseManagerError, match="interrupted"):
        manager.activate(manifest["release_id"])

    assert manager.status(manifest["release_id"])["state"] == "activating"
    assert runner.migration_head == manifest["migration"]["target"]
    runner.after_action = None
    resumed = _restart_production_manager(manager, runner)

    with pytest.raises(ReleaseManagerError, match="rolled_back"):
        resumed.rollback(manifest["release_id"])

    expected_refs = current_refs.copy()
    expected_refs.update(
        {
            name: manifest["images"][name]["ref"]
            for name in ("postgres", "redis")
        }
    )
    state = resumed.status(manifest["release_id"])
    assert state["state"] == "rolled_back"
    assert state["residual_changes"] == [
        "image:postgres",
        "image:redis",
        f"migration:{OFFLINE_EXPAND_MIGRATION[1]}",
    ]
    assert resumed._root_env_refs() == expected_refs
    assert runner.runtime_refs == expected_refs


def test_production_offline_signature_failure_precedes_every_docker_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, manifest, current_refs = _offline_bundle(tmp_path)
    _configure_offline_trust(tmp_path, monkeypatch)
    manager, runner, root, release_root = _manager(tmp_path, manifest, current_refs)
    runner.signature_valid = False
    original_env = manager.environment_file.read_bytes()

    with pytest.raises(ReleaseManagerError, match="prepare failed"):
        manager.prepare(manifest_path)

    assert not any(command[0] == "docker" for command in runner.calls)
    assert manager.environment_file.read_bytes() == original_env
    release_dir = release_root / manifest["release_id"]
    assert {
        path.name for path in (release_dir / "artifacts").iterdir()
    } == {"manifest.sig"}
    assert (release_dir / "manifest.json").read_bytes() == manifest_path.read_bytes()
    assert manager.status(manifest["release_id"])["state"] == "failed"


@pytest.mark.parametrize("oversized_name", ["release-gate.json", "offline-image-index.json"])
def test_production_offline_caps_signed_json_before_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    oversized_name: str,
) -> None:
    manifest_path, manifest, current_refs = _offline_bundle(tmp_path)
    _configure_offline_trust(tmp_path, monkeypatch)
    oversized = manifest_path.parent / oversized_name
    oversized.write_bytes(b"x" * (release_manager_module._MAX_JSON_BYTES + 1))
    oversized.chmod(0o600)
    manager, runner, _, release_root = _manager(tmp_path, manifest, current_refs)

    with pytest.raises(ReleaseManagerError, match="prepare failed"):
        manager.prepare(manifest_path)

    artifacts = release_root / manifest["release_id"] / "artifacts"
    assert not (artifacts / oversized_name).exists()
    assert not any(command[0] == "docker" for command in runner.calls)


@pytest.mark.parametrize(
    "binding_failure",
    ["data_hash", "data_size", "backup_record_hash", "backup_report_size"],
)
def test_production_offline_v2_condition_evidence_checks_hash_and_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    binding_failure: str,
) -> None:
    manifest_path, manifest, current_refs = _offline_bundle(tmp_path)
    _configure_offline_trust(tmp_path, monkeypatch)
    if binding_failure == "data_hash":
        manifest["evidence"]["data_images"]["sha256"] = "0" * 64
    elif binding_failure == "data_size":
        manifest["evidence"]["data_images"]["size"] += 1
    elif binding_failure == "backup_record_hash":
        manifest["evidence"]["backup_restore_change"]["record"]["sha256"] = (
            "0" * 64
        )
    else:
        manifest["evidence"]["backup_restore_change"]["restore_report"]["size"] += 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_path.chmod(0o600)
    manager, runner, root, _ = _manager(tmp_path, manifest, current_refs)
    original_env = manager.environment_file.read_bytes()

    with pytest.raises(ReleaseManagerError, match="prepare failed"):
        manager.prepare(manifest_path)

    assert not any(
        command[:3] == ["docker", "image", "load"] for command in runner.calls
    )
    assert manager.environment_file.read_bytes() == original_env


@pytest.mark.parametrize(
    ("changed", "migration_changed", "compatibility"),
    [
        ({"web"}, False, "none"),
        (set(IMAGE_NAMES), True, "manual"),
    ],
)
def test_production_offline_rejects_selective_or_unsupported_migration_updates(
    tmp_path: Path,
    changed: set[str],
    migration_changed: bool,
    compatibility: str,
) -> None:
    manifest_path, manifest, current_refs = _offline_bundle(
        tmp_path,
        changed=changed,
        migration_changed=migration_changed,
    )
    manifest["migration"]["compatibility"] = compatibility
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_path.chmod(0o600)
    manager, runner, root, _ = _manager(tmp_path, manifest, current_refs)
    original_env = manager.environment_file.read_bytes()

    with pytest.raises(ReleaseManagerError):
        manager.prepare(manifest_path)

    assert not runner.calls
    assert manager.environment_file.read_bytes() == original_env


@pytest.mark.parametrize("trust_failure", ["mode", "key_id"])
def test_production_offline_rejects_untrusted_fixed_signing_material(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trust_failure: str,
) -> None:
    manifest_path, manifest, current_refs = _offline_bundle(tmp_path)
    public_key, key_id = _configure_offline_trust(tmp_path, monkeypatch)
    if trust_failure == "mode":
        public_key.chmod(0o600)
    else:
        key_id.write_text("another-key\n", encoding="ascii")
        key_id.chmod(0o644)
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)

    with pytest.raises(ReleaseManagerError, match="prepare failed"):
        manager.prepare(manifest_path)

    assert not any(command[0] == "docker" for command in runner.calls)
    assert not any(command[0] == str(release_manager_module._OPENSSL) for command in runner.calls)


@pytest.mark.parametrize("invalid_evidence", ["local", "promotion", "ref"])
def test_production_offline_requires_bound_nonlocal_candidate_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_evidence: str,
) -> None:
    manifest_path, manifest, current_refs = _offline_bundle(tmp_path)
    _configure_offline_trust(tmp_path, monkeypatch)
    report_path = manifest_path.parent / "release-gate.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if invalid_evidence == "local":
        report["source"]["workflow_repository"] = "local"
        report["source"]["workflow_run_id"] = 0
        report["source"]["workflow_run_attempt"] = 0
    elif invalid_evidence == "promotion":
        report["promotion_source"] = {}
    else:
        report["images"]["api"]["ref"] = manifest["images"]["api"]["ref"]
    _rewrite_offline_release_evidence(manifest_path, manifest, report)
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)

    with pytest.raises(ReleaseManagerError, match="prepare failed"):
        manager.prepare(manifest_path)

    assert not any(
        command[:3] == ["docker", "image", "load"] for command in runner.calls
    )


def test_production_offline_index_must_cross_bind_every_manifest_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, manifest, current_refs = _offline_bundle(tmp_path)
    _configure_offline_trust(tmp_path, monkeypatch)
    index_path = manifest_path.parent / "offline-image-index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["images"]["redis"]["archive"]["size"] += 1
    _write_private_json(index_path, index)
    manifest["evidence"]["offline_image_index"]["sha256"] = hashlib.sha256(
        index_path.read_bytes()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_path.chmod(0o600)
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)

    with pytest.raises(ReleaseManagerError, match="prepare failed"):
        manager.prepare(manifest_path)

    assert not any(
        command[:3] == ["docker", "image", "load"] for command in runner.calls
    )


def test_production_bootstrap_freezes_archive_larger_than_json_limit_streaming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, manifest, current_refs = _offline_bundle(
        tmp_path,
        changed=set(),
        large_archive=True,
    )
    _configure_offline_trust(tmp_path, monkeypatch)
    manager, _, _, release_root = _manager(tmp_path, manifest, current_refs)
    parsed, manifest_bytes, files = release_manager_module._validate_staging_bundle(
        manifest_path,
        os.geteuid(),
    )
    assert parsed.release_id == manifest["release_id"]
    assert (manifest_path.parent / "api.tar").stat().st_size > 1024 * 1024
    store = ReleaseStore(release_root, manifest["release_id"])

    manager._freeze_bootstrap_bundle(
        manifest_path,
        manifest_bytes,
        files,
        store,
    )

    frozen = store.release_dir / "artifacts" / "api.tar"
    assert frozen.stat().st_size == (manifest_path.parent / "api.tar").stat().st_size
    assert hashlib.sha256(frozen.read_bytes()).hexdigest() == manifest["images"]["api"][
        "archive_sha256"
    ]


def test_target_image_mismatch_fails_without_lifecycle_mutation(tmp_path: Path) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path)
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    runner.target_id_override = "sha256:" + "9" * 64

    with pytest.raises(ReleaseManagerError, match="prepare failed"):
        manager.prepare(manifest_path)

    assert not any("pull" in command or "up" in command for command in runner.calls)


def test_data_image_major_mismatch_fails_closed_after_fixed_observation(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path, postgres_changed=True)
    report_path = manifest_path.parent / "data-images.json"
    _write_private_json(report_path, _data_report(manifest, postgres_major=17))
    manager, runner, root, _ = _manager(tmp_path, manifest, current_refs)
    original_env = manager.environment_file.read_bytes()

    with pytest.raises(ReleaseManagerError, match="prepare failed"):
        manager.prepare(manifest_path)

    compose_prefix = [
        "docker",
        "compose",
        "--env-file",
        str(manager.environment_file),
        "-f",
        str(root / "deploy" / "docker-compose.yml"),
    ]
    assert compose_prefix + ["exec", "-T", "postgres", "postgres", "--version"] in runner.calls
    assert compose_prefix + ["exec", "-T", "redis", "redis-server", "--version"] in runner.calls
    assert manager.environment_file.read_bytes() == original_env


def test_data_evidence_image_binding_mismatch_is_rejected(tmp_path: Path) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path, postgres_changed=True)
    report_path = manifest_path.parent / "data-images.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["images"]["postgres"]["image_id"] = "sha256:" + "9" * 64
    _write_private_json(report_path, report)
    manager, _, _, _ = _manager(tmp_path, manifest, current_refs)

    with pytest.raises(ReleaseManagerError, match="prepare failed"):
        manager.prepare(manifest_path)


@pytest.mark.parametrize(
    ("name", "version"),
    [("postgres", "16.not-official"), ("redis", "7.4")],
)
def test_data_evidence_rejects_noncanonical_normalized_versions(
    tmp_path: Path,
    name: str,
    version: str,
) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path, postgres_changed=True)
    report_path = manifest_path.parent / "data-images.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["images"][name]["version"] = version
    _write_private_json(report_path, report)
    manager, _, _, _ = _manager(tmp_path, manifest, current_refs)

    with pytest.raises(ReleaseManagerError, match="prepare failed"):
        manager.prepare(manifest_path)


def test_production_postgres_change_accepts_bound_approval_and_restore_report(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle(
        tmp_path,
        mode="production",
        postgres_changed=True,
    )
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)

    assert manifest["migration"]["from"] != manifest["migration"]["target"]

    manager.prepare(manifest_path)

    assert manager.status(manifest["release_id"])["state"] == "prepared"
    assert not any(command[:2] == ["docker", "load"] for command in runner.calls)
    assert not any(
        command[:3] == ["docker", "image", "load"] for command in runner.calls
    )
    assert not any(command[0] == str(release_manager_module._OPENSSL) for command in runner.calls)
    assert not any("pull" in command for command in runner.calls)


def test_release_gate_bytes_must_match_manifest_hash(tmp_path: Path) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path)
    report_path = manifest_path.parent / "release-gate.json"
    report_path.write_bytes(report_path.read_bytes() + b" ")
    report_path.chmod(0o600)
    manager, _, _, _ = _manager(tmp_path, manifest, current_refs)

    with pytest.raises(ReleaseManagerError, match="prepare failed") as error:
        manager.prepare(manifest_path)

    assert error.value.__cause__ is not None
    assert "hash does not match manifest" in str(error.value.__cause__)


def test_production_requires_preloaded_target_repo_digests(tmp_path: Path) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path, mode="production")
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    runner.target_repo_digests_override = []

    with pytest.raises(ReleaseManagerError, match="prepare failed"):
        manager.prepare(manifest_path)

    assert not any("pull" in command for command in runner.calls)


@pytest.mark.parametrize("source_state", ["missing", "mismatched"])
def test_prepare_rejects_production_release_without_bound_candidate_source(
    tmp_path: Path,
    source_state: str,
) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path, mode="production")
    report_path = manifest_path.parent / "release-gate.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if source_state == "missing":
        report["promotion_source"] = None
    else:
        report["promotion_source"]["images"]["api"]["image_id"] = "sha256:" + "9" * 64
    _write_private_json(report_path, report)
    manager, _, _, _ = _manager(tmp_path, manifest, current_refs)

    with pytest.raises(ReleaseManagerError, match="prepare failed"):
        manager.prepare(manifest_path)


def test_prepare_rejects_control_smoke_without_fully_gated_smoke_context(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _control_smoke_bundle(tmp_path)
    manager, _, _, _ = _manager(tmp_path, manifest, current_refs)

    with pytest.raises(ReleaseManagerError, match="prepare failed"):
        manager.prepare(manifest_path)


def test_prepare_rejects_control_smoke_with_unbound_bundle_report(
    tmp_path: Path,
    smoke_release_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, manifest, current_refs = _control_smoke_bundle(tmp_path)
    report_path = manifest_path.parent / "release-gate.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["images"]["redis"]["platform"] = "linux/arm64"
    _write_private_json(report_path, report)
    runtime_root = smoke_release_root.parent / "runtime-secrets"
    runtime_root.mkdir(mode=0o700)
    runtime_root.chmod(0o700)
    monkeypatch.setenv("SMS_RELEASE_SMOKE", "1")
    monkeypatch.setenv("SMS_RELEASE_ROOT", str(smoke_release_root))
    monkeypatch.setenv("SMS_RUNTIME_ROOT", str(runtime_root))
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", smoke_release_root.parent.name)
    manager, runner, root, _ = _manager(tmp_path, manifest, current_refs)
    manager = ReleaseManager(
        root=root,
        release_root=smoke_release_root,
        mode=manifest["mode"],
        runner=runner,
        expected_staging_uid=os.geteuid(),
    )

    with pytest.raises(ReleaseManagerError, match="prepare failed"):
        manager.prepare(manifest_path)


def test_prepare_accepts_control_smoke_only_with_isolated_smoke_context(
    tmp_path: Path,
    smoke_release_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, manifest, current_refs = _control_smoke_bundle(tmp_path)
    runtime_root = smoke_release_root.parent / "runtime-secrets"
    runtime_root.mkdir(mode=0o700)
    runtime_root.chmod(0o700)
    monkeypatch.setenv("SMS_RELEASE_SMOKE", "1")
    monkeypatch.setenv("SMS_RELEASE_ROOT", str(smoke_release_root))
    monkeypatch.setenv("SMS_RUNTIME_ROOT", str(runtime_root))
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", smoke_release_root.parent.name)
    manager, runner, root, _ = _manager(tmp_path, manifest, current_refs)
    manager = ReleaseManager(
        root=root,
        release_root=smoke_release_root,
        mode=manifest["mode"],
        runner=runner,
        expected_staging_uid=os.geteuid(),
    )

    manager.prepare(manifest_path)

    state = manager.status(manifest["release_id"])
    assert state["state"] == "prepared"
    assert state["release_gate_kind"] == "release_control_smoke"
    assert state["control_smoke_only"] is True
    assert state["release_scan_performed"] is False


@pytest.mark.parametrize(
    "mutation",
    [
        "change_unknown",
        "change_binding",
        "report_hash",
        "snapshot_binding",
        "time_order",
        "report_checks",
        "crypto_receipt",
        "report_tables",
    ],
)
def test_production_backup_contract_rejects_any_unbound_or_unsafe_evidence(
    tmp_path: Path,
    mutation: str,
) -> None:
    manifest_path, manifest, current_refs = _bundle(
        tmp_path,
        mode="production",
        postgres_changed=True,
    )
    change_path = manifest_path.parent / "backup-change.json"
    report_path = manifest_path.parent / "restore-report.json"
    change = json.loads(change_path.read_text(encoding="utf-8"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if mutation == "change_unknown":
        change["unknown"] = True
    elif mutation == "change_binding":
        change["release_id"] = "different-release"
    elif mutation == "report_hash":
        change["restore"]["report_sha256"] = "0" * 64
    elif mutation == "snapshot_binding":
        change["restore"]["snapshot_id"] = "different-snapshot"
    elif mutation == "time_order":
        change["approval"]["approved_at"] = "2026-07-14T07:15:00+00:00"
    elif mutation == "report_checks":
        report["checks"]["role_flags"] = "6|true"
    elif mutation == "crypto_receipt":
        report["crypto_probe_receipts"]["pre_migration"]["counts"][
            "encrypted_rows"
        ] += 1
    else:
        report["table_counts"]["extra"] = 0
    _write_private_json(report_path, report)
    if mutation not in {
        "report_hash",
        "change_unknown",
        "change_binding",
        "snapshot_binding",
        "time_order",
    }:
        change["restore"]["report_sha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()
    _write_private_json(change_path, change)
    manager, _, _, _ = _manager(tmp_path, manifest, current_refs)

    with pytest.raises(ReleaseManagerError) as error:
        manager.prepare(manifest_path)

    assert "0" * 64 not in str(error.value)


def test_prepare_rejects_duplicate_env_image_keys(tmp_path: Path) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path)
    manager, _, root, _ = _manager(tmp_path, manifest, current_refs)
    with manager.environment_file.open("a", encoding="utf-8") as stream:
        stream.write(f"SMS_WEB_IMAGE={current_refs['web']}\n")

    with pytest.raises(ReleaseManagerError, match="prepare failed"):
        manager.prepare(manifest_path)


def _planned_manifest(
    tmp_path: Path,
    changed: set[str],
    *,
    migration_changed: bool,
) -> Any:
    manifest_path, manifest, _ = _bundle(tmp_path)
    for name in IMAGE_NAMES:
        image = manifest["images"][name]
        image["changed"] = name in changed
        if name in changed:
            archive = manifest_path.parent / f"{name}.tar"
            archive.write_bytes(f"verified-{name}-archive".encode())
            archive.chmod(0o600)
            image["archive_file"] = archive.name
            image["archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
        else:
            image["archive_file"] = None
            image["archive_sha256"] = None
    if not migration_changed:
        manifest["migration"]["target"] = manifest["migration"]["from"]
        manifest["migration"]["compatibility"] = "none"
    manifest["evidence"]["data_images"] = (
        "data-images.json" if {"postgres", "redis"} & changed else None
    )
    _write_private_json(manifest_path, manifest)
    return load_manifest(manifest_path)


@pytest.mark.parametrize(
    ("changed", "migration_changed", "expected"),
    [
        (set(), False, [("verify", ())]),
        ({"web"}, False, [("recreate_web", ("web",)), ("verify", ())]),
        (
            {"api"},
            False,
            [
                (
                    "quiesce_backend",
                    (
                        "beat",
                        "outbox-dispatcher",
                        "worker-realtime",
                        "worker-report",
                        "worker-bulk",
                        "worker-callback",
                        "api",
                    ),
                ),
                ("wait_beat_lease", ()),
                (
                    "recreate_backend",
                    (
                        "api",
                        "worker-realtime",
                        "worker-report",
                        "worker-bulk",
                        "worker-callback",
                        "outbox-dispatcher",
                        "beat",
                    ),
                ),
                ("verify", ()),
            ],
        ),
        (
            {"api"},
            True,
            [
                (
                    "quiesce_backend",
                    (
                        "beat",
                        "outbox-dispatcher",
                        "worker-realtime",
                        "worker-report",
                        "worker-bulk",
                        "worker-callback",
                        "api",
                    ),
                ),
                ("wait_beat_lease", ()),
                ("run_migrate", ("migrate",)),
                (
                    "recreate_backend",
                    (
                        "api",
                        "worker-realtime",
                        "worker-report",
                        "worker-bulk",
                        "worker-callback",
                        "outbox-dispatcher",
                        "beat",
                    ),
                ),
                ("verify", ()),
            ],
        ),
        (
            {"redis"},
            False,
            [
                (
                    "quiesce_backend",
                    (
                        "beat",
                        "outbox-dispatcher",
                        "worker-realtime",
                        "worker-report",
                        "worker-bulk",
                        "worker-callback",
                        "api",
                    ),
                ),
                ("wait_beat_lease", ()),
                ("recreate_redis", ("redis", "redis-auth", "redis-control")),
                (
                    "recreate_backend",
                    (
                        "api",
                        "worker-realtime",
                        "worker-report",
                        "worker-bulk",
                        "worker-callback",
                        "outbox-dispatcher",
                        "beat",
                    ),
                ),
                ("verify", ()),
            ],
        ),
        (
            {"postgres"},
            True,
            [
                (
                    "quiesce_backend",
                    (
                        "beat",
                        "outbox-dispatcher",
                        "worker-realtime",
                        "worker-report",
                        "worker-bulk",
                        "worker-callback",
                        "api",
                    ),
                ),
                ("wait_beat_lease", ()),
                ("recreate_postgres", ("postgres",)),
                ("run_migrate", ("migrate",)),
                (
                    "recreate_backend",
                    (
                        "api",
                        "worker-realtime",
                        "worker-report",
                        "worker-bulk",
                        "worker-callback",
                        "outbox-dispatcher",
                        "beat",
                    ),
                ),
                ("verify", ()),
            ],
        ),
        (
            set(IMAGE_NAMES),
            True,
            [
                (
                    "quiesce_backend",
                    (
                        "beat",
                        "outbox-dispatcher",
                        "worker-realtime",
                        "worker-report",
                        "worker-bulk",
                        "worker-callback",
                        "api",
                    ),
                ),
                ("wait_beat_lease", ()),
                ("recreate_postgres", ("postgres",)),
                ("recreate_redis", ("redis", "redis-auth", "redis-control")),
                ("run_migrate", ("migrate",)),
                (
                    "recreate_backend",
                    (
                        "api",
                        "worker-realtime",
                        "worker-report",
                        "worker-bulk",
                        "worker-callback",
                        "outbox-dispatcher",
                        "beat",
                    ),
                ),
                ("recreate_web", ("web",)),
                ("verify", ()),
            ],
        ),
    ],
)
def test_activation_plan_is_pure_and_orders_exact_service_groups(
    tmp_path: Path,
    changed: set[str],
    migration_changed: bool,
    expected: list[tuple[str, tuple[str, ...]]],
) -> None:
    import release_manager as release_manager_module

    manifest = _planned_manifest(tmp_path, changed, migration_changed=migration_changed)

    first = release_manager_module.build_activation_plan(manifest)
    second = release_manager_module.build_activation_plan(manifest)

    assert first == second
    assert [(step.kind.value, step.services) for step in first] == expected


def test_activation_commands_are_exact_argv_arrays(tmp_path: Path) -> None:
    import release_manager as release_manager_module

    manifest = _planned_manifest(tmp_path, {"web"}, migration_changed=False)
    root = tmp_path / "platform"
    compose = [
        "docker",
        "compose",
        "--env-file",
        str(root / ".env"),
        "-f",
        str(root / "deploy" / "docker-compose.yml"),
    ]

    commands = release_manager_module.activation_commands(
        root,
        release_manager_module.build_activation_plan(manifest),
    )

    assert commands == [
        compose
        + [
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            "120",
            "web",
        ],
        compose + ["config", "--quiet"],
    ]
    assert all(type(command) is list for command in commands)


def test_production_activation_recreates_never_build_from_control_snapshot(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, refs = _bundle(tmp_path, mode="production")
    manager, runner, _, _ = _manager(tmp_path, manifest, refs)
    manager.prepare(manifest_path)
    runner.calls.clear()

    manager.activate(manifest["release_id"])

    up_commands = [command for command in runner.calls if "up" in command]
    assert up_commands
    assert all(
        command[command.index("up") + 1] == "--no-build"
        for command in up_commands
    )


def test_configure_activation_atomically_updates_all_refs_and_preserves_env_metadata(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path, postgres_changed=True)
    manager, runner, root, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    env_path = manager.environment_file
    original = env_path.read_bytes()
    decorated = b"# retained-comment\nUNRELATED=opaque\n" + original + b"TAIL=no-newline"
    env_path.write_bytes(decorated)
    env_path.chmod(0o640)
    before = env_path.stat()
    runner.calls.clear()

    plan = manager.configure_activation(manifest["release_id"])

    after = env_path.stat()
    assert env_path.read_bytes() == decorated.replace(
        f"SMS_POSTGRES_IMAGE={current_refs['postgres']}".encode(),
        f"SMS_POSTGRES_IMAGE={manifest['images']['postgres']['ref']}".encode(),
    )
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode)
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)
    assert plan[0].kind.value == "quiesce_backend"
    assert runner.calls == [manager._compose() + ["config", "--quiet"]]


@pytest.mark.parametrize("failure", ["write", "fsync", "replace"])
def test_configure_activation_atomic_failure_restores_original_without_container_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    import release_store as release_store_module

    manifest_path, manifest, current_refs = _bundle(tmp_path)
    manager, runner, root, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    env_path = manager.environment_file
    original = env_path.read_bytes()
    release_store_module.ReleaseStore(manager.release_root, manifest["release_id"]).snapshot_env(
        env_path
    )
    real = getattr(release_store_module.os, failure)
    injected = False

    def fail_once(*args: Any, **kwargs: Any) -> Any:
        nonlocal injected
        if not injected:
            injected = True
            raise OSError(f"injected {failure} failure")
        return real(*args, **kwargs)

    monkeypatch.setattr(release_store_module.os, failure, fail_once)
    runner.calls.clear()

    with pytest.raises(ReleaseManagerError, match="configuration"):
        manager.configure_activation(manifest["release_id"])

    assert env_path.read_bytes() == original
    assert not any(
        token in command
        for command in runner.calls
        for token in ("up", "stop", "run", "restart", "rm", "pull")
    )


def test_config_failure_restores_original_env_and_never_changes_containers(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path)
    manager, runner, root, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    env_path = manager.environment_file
    original = env_path.read_bytes()
    runner.calls.clear()
    real_run = runner.run

    def fail_config(
        argv: Sequence[str], *, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        command = [str(value) for value in argv]
        if command[-2:] == ["config", "--quiet"]:
            runner.calls.append(command)
            return subprocess.CompletedProcess(command, 1, "", "invalid")
        return real_run(argv, cwd=cwd)

    runner.run = fail_config  # type: ignore[method-assign]

    with pytest.raises(ReleaseManagerError, match="configuration"):
        manager.configure_activation(manifest["release_id"])

    assert env_path.read_bytes() == original
    assert runner.calls == [manager._compose() + ["config", "--quiet"]]


def _compose_actions(runner: FakeRunner) -> list[list[str]]:
    return [
        command
        for command in runner.calls
        if "compose" in command
        and any(action in command for action in ("config", "stop", "up", "run"))
    ]


def test_activate_orders_data_migrate_backend_and_web_with_exact_groups(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, set(IMAGE_NAMES))
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    runner.calls.clear()

    manager.activate(manifest["release_id"])

    compose = manager._compose()
    assert _compose_actions(runner) == [
        compose + ["config", "--quiet"],
        compose + ["stop", *_QUIESCE_TEST_SERVICES],
        compose
        + [
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            "120",
            "postgres",
        ],
        compose
        + [
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            "120",
            "redis",
            "redis-auth",
            "redis-control",
        ],
        compose + ["run", "--rm", "migrate"],
        compose
        + [
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            "120",
            *_BACKEND_TEST_SERVICES,
        ],
        compose
        + ["up", "-d", "--no-deps", "--force-recreate", "--wait", "--wait-timeout", "120", "web"],
    ]
    release_state = manager.status(manifest["release_id"])
    assert release_state["state"] == "succeeded"


def test_successful_migrate_command_must_reach_declared_target(tmp_path: Path) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"api"})
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    runner.migration_head_after_successful_run = manifest["migration"]["from"]

    with pytest.raises(ReleaseManagerError, match="recovery_required"):
        manager.activate(manifest["release_id"])

    assert manager.status(manifest["release_id"])["state"] == "recovery_required"


def test_final_verification_rechecks_declared_migration_target(tmp_path: Path) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"api"})
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)

    def drift_migration_after_worker_probe() -> None:
        runner.migration_head = manifest["migration"]["from"]

    runner.after_ping = drift_migration_after_worker_probe

    with pytest.raises(ReleaseManagerError, match="recovery_required"):
        manager.activate(manifest["release_id"])

    assert manager.status(manifest["release_id"])["state"] == "recovery_required"


def test_final_runtime_verification_binds_images_containers_health_and_workers(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"web"})
    manifest["migration"]["target"] = manifest["migration"]["from"]
    manifest["migration"]["compatibility"] = "none"
    _write_private_json(manifest_path, manifest)
    manager, runner, _, release_root = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    original_ids = runner.service_container_ids.copy()
    runner.calls.clear()

    manager.activate(manifest["release_id"])

    assert runner.service_container_ids["web"] != original_ids["web"]
    assert all(
        runner.service_container_ids[service] == original_ids[service]
        for service in RUNTIME_SERVICES
        if service != "web"
    )
    compose = manager._compose()
    for service in RUNTIME_SERVICES:
        assert compose + ["ps", "-q", service] in runner.calls
        assert any(
            command[:3] == ["docker", "inspect", "--format"]
            and "Config.Hostname" in command[-2]
            and command[-1] == runner.service_container_ids[service]
            for command in runner.calls
        )
    assert [
        command
        for command in runner.calls
        if command[:3] == ["docker", "inspect", "--format"]
        and "range .Mounts" in command[-2]
    ] == [
        [
            "docker",
            "inspect",
            "--format",
            '{{range .Mounts}}{{if eq .Destination "/var/lib/sms/beat"}}'
            "{{.Type}} {{.Name}} {{.Destination}} {{.RW}} {{.Source}}"
            "{{end}}{{end}}",
            runner.service_container_ids["beat"],
        ]
    ]
    assert (
        compose
        + [
            "exec",
            "-T",
            "worker-realtime",
            "celery",
            "-A",
            "app.tasks",
            "inspect",
            "ping",
            "--timeout",
            "10",
            "--json",
        ]
        in runner.calls
    )
    assert (
        compose
        + [
            "exec",
            "-T",
            "worker-realtime",
            "celery",
            "-A",
            "app.tasks",
            "inspect",
            "active_queues",
            "--timeout",
            "10",
            "--json",
        ]
        in runner.calls
    )
    events = [
        json.loads(line)
        for line in (release_root / manifest["release_id"] / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    final = [
        event
        for event in events
        if event["kind"] == "observation" and event["step"] == "final_runtime"
    ]
    assert final[-1]["details"] == {
        "completed": True,
        "services": list(RUNTIME_SERVICES),
        "tracked_job_heartbeat": "post_release_operational_check",
    }


def test_beat_schedule_mount_uses_isolated_compose_project(
    tmp_path: Path,
    smoke_release_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, manifest, current_refs = _control_smoke_bundle(tmp_path)
    project = smoke_release_root.parent.name
    runtime_root = smoke_release_root.parent / "runtime-secrets"
    runtime_root.mkdir(mode=0o700)
    runtime_root.chmod(0o700)
    monkeypatch.setenv("SMS_RELEASE_SMOKE", "1")
    monkeypatch.setenv("SMS_RELEASE_ROOT", str(smoke_release_root))
    monkeypatch.setenv("SMS_RUNTIME_ROOT", str(runtime_root))
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", project)
    manager, runner, root, _ = _manager(tmp_path, manifest, current_refs)
    manager = ReleaseManager(
        root=root,
        release_root=smoke_release_root,
        mode=manifest["mode"],
        runner=runner,
        expected_staging_uid=os.geteuid(),
    )
    manager.prepare(manifest_path)
    runner.calls.clear()

    manager.activate(manifest["release_id"])

    assert manager.status(manifest["release_id"])["state"] == "succeeded"
    assert runner.beat_schedule_mount == (
        f"volume {project}_beatdata /var/lib/sms/beat true "
        f"/var/lib/docker/volumes/{project}_beatdata/_data\n"
    )
    assert any(
        command[:3] == ["docker", "inspect", "--format"]
        and "range .Mounts" in command[-2]
        and command[-1] == runner.service_container_ids["beat"]
        for command in runner.calls
    )


@pytest.mark.parametrize(
    "action",
    ("activate", "resume", "rollback", "configure_activation"),
)
def test_control_smoke_project_drift_is_rejected_before_runtime_side_effects(
    tmp_path: Path,
    smoke_release_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    manifest_path, manifest, current_refs = _control_smoke_bundle(tmp_path)
    runtime_root = smoke_release_root.parent / "runtime-secrets"
    runtime_root.mkdir(mode=0o700)
    runtime_root.chmod(0o700)
    monkeypatch.setenv("SMS_RELEASE_SMOKE", "1")
    monkeypatch.setenv("SMS_RELEASE_ROOT", str(smoke_release_root))
    monkeypatch.setenv("SMS_RUNTIME_ROOT", str(runtime_root))
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", smoke_release_root.parent.name)
    manager, runner, root, _ = _manager(tmp_path, manifest, current_refs)
    manager = ReleaseManager(
        root=root,
        release_root=smoke_release_root,
        mode=manifest["mode"],
        runner=runner,
        expected_staging_uid=os.geteuid(),
    )
    manager.prepare(manifest_path)
    runner.calls.clear()
    monkeypatch.setenv(
        "COMPOSE_PROJECT_NAME",
        "sms-platform-release-control-deadbeef",
    )

    with pytest.raises(ReleaseManagerError):
        getattr(manager, action)(manifest["release_id"])

    assert manager.status(manifest["release_id"])["state"] == "prepared"
    assert runner.calls == []


def test_production_beat_volume_ignores_ambient_compose_project_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path, mode="production")
    monkeypatch.setenv(
        "COMPOSE_PROJECT_NAME",
        "sms-platform-release-control-deadbeef",
    )
    manager, _, _, _ = _manager(tmp_path, manifest, current_refs)

    assert manager._beat_schedule_volume_binding(load_manifest(manifest_path)) == (
        "sms-platform_beatdata",
        "/var/lib/docker/volumes/sms-platform_beatdata/_data",
    )


@pytest.mark.parametrize(
    ("mount_output", "inspection_failure", "reason"),
    [
        ("", False, "beat_schedule_mount_output"),
        (
            "volume wrong /var/lib/sms/beat true "
            "/var/lib/docker/volumes/sms-platform_beatdata/_data\n",
            False,
            "beat_schedule_mount_binding",
        ),
        (
            "volume sms-platform_beatdata /var/lib/sms/beat false "
            "/var/lib/docker/volumes/sms-platform_beatdata/_data\n",
            False,
            "beat_schedule_mount_binding",
        ),
        (
            "volume sms-platform_beatdata /var/lib/sms/beat true /wrong/_data\n",
            False,
            "beat_schedule_mount_binding",
        ),
        (
            "volume sms-platform_beatdata /var/lib/sms/beat true "
            "/var/lib/docker/volumes/sms-platform_beatdata/_data\n",
            True,
            "beat_schedule_mount_inspection",
        ),
    ],
)
def test_beat_schedule_mount_drift_requires_manual_recovery(
    tmp_path: Path,
    mount_output: str,
    inspection_failure: bool,
    reason: str,
) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"web"})
    manifest["migration"]["target"] = manifest["migration"]["from"]
    manifest["migration"]["compatibility"] = "none"
    _write_private_json(manifest_path, manifest)
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    runner.beat_schedule_mount = mount_output
    runner.fail_beat_schedule_mount_inspection = inspection_failure

    with pytest.raises(ReleaseManagerError, match="recovery_required"):
        manager.activate(manifest["release_id"])

    assert manager.status(manifest["release_id"])["state"] == "recovery_required"
    events = (manager.release_root / manifest["release_id"] / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert f'"reason":"{reason}"' in events


def test_final_web_health_failure_uses_existing_stateless_compensation(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"web"})
    manifest["migration"]["target"] = manifest["migration"]["from"]
    manifest["migration"]["compatibility"] = "none"
    _write_private_json(manifest_path, manifest)
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    runner.unhealthy_service = "web"
    runner.calls.clear()

    with pytest.raises(ReleaseManagerError, match="rolled_back"):
        manager.activate(manifest["release_id"])

    assert manager.status(manifest["release_id"])["state"] == "rolled_back"
    assert (
        sum("up" in command and command[-1] == "web" for command in _compose_actions(runner)) == 2
    )


def test_missing_worker_ping_uses_existing_stateless_compensation(tmp_path: Path) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"web"})
    manifest["migration"]["target"] = manifest["migration"]["from"]
    manifest["migration"]["compatibility"] = "none"
    _write_private_json(manifest_path, manifest)
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    runner.missing_worker_ping = "worker-callback"
    runner.calls.clear()

    with pytest.raises(ReleaseManagerError, match="rolled_back"):
        manager.activate(manifest["release_id"])

    assert manager.status(manifest["release_id"])["state"] == "rolled_back"
    events = (manager.release_root / manifest["release_id"] / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert '"reason":"worker_ping_membership"' in events


def test_wrong_worker_queue_binding_uses_existing_stateless_compensation(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"web"})
    manifest["migration"]["target"] = manifest["migration"]["from"]
    manifest["migration"]["compatibility"] = "none"
    _write_private_json(manifest_path, manifest)
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    runner.worker_queue_overrides["worker-report"] = "realtime"
    runner.calls.clear()

    with pytest.raises(ReleaseManagerError, match="rolled_back"):
        manager.activate(manifest["release_id"])

    assert manager.status(manifest["release_id"])["state"] == "rolled_back"
    events = (manager.release_root / manifest["release_id"] / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert '"reason":"worker_active_queues_binding"' in events


def test_service_failure_during_worker_ping_is_detected_before_success(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"web"})
    manifest["migration"]["target"] = manifest["migration"]["from"]
    manifest["migration"]["compatibility"] = "none"
    _write_private_json(manifest_path, manifest)
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)

    def stop_web_after_ping() -> None:
        runner.unhealthy_service = "web"

    runner.after_ping = stop_web_after_ping
    with pytest.raises(ReleaseManagerError, match="rolled_back"):
        manager.activate(manifest["release_id"])

    assert manager.status(manifest["release_id"])["state"] == "rolled_back"
    events = (manager.release_root / manifest["release_id"] / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert '"reason":"post_ping_service_health"' in events


def test_unselected_container_identity_drift_requires_manual_recovery(tmp_path: Path) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"web"})
    manifest["migration"]["target"] = manifest["migration"]["from"]
    manifest["migration"]["compatibility"] = "none"
    _write_private_json(manifest_path, manifest)
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)

    def drift_unselected(action: str, _: int) -> None:
        if action == "up":
            runner.service_container_ids["api"] = hashlib.sha256(b"external-api").hexdigest()

    runner.after_action = drift_unselected
    runner.calls.clear()

    with pytest.raises(ReleaseManagerError, match="recovery_required"):
        manager.activate(manifest["release_id"])

    assert manager.status(manifest["release_id"])["state"] == "recovery_required"


def test_runtime_host_and_public_probes_are_not_added_to_release_manager() -> None:
    source = (ROOT / "deploy/scripts/release_manager.py").read_text(encoding="utf-8")

    assert "systemctl" not in source
    assert "public-url" not in source


def test_activating_release_with_original_runtime_resumes_after_preflight_stop() -> None:
    decision = reconcile_release(
        {"state": ReleaseState.ACTIVATING.value},
        RuntimeObservation(
            env_state="original",
            service_state="original",
            migration_state="original",
            healthy=True,
            migration_required=True,
        ),
    )

    assert decision is ReconciliationDecision.RESUME


def test_backend_release_drains_legacy_beat_lease_before_recreate(
    tmp_path: Path,
) -> None:
    manifest_path, _, _ = _bundle_for_changes(tmp_path, {"api"})
    parsed = load_manifest(manifest_path)
    import release_manager as release_manager_module

    steps = release_manager_module.build_activation_plan(parsed)
    commands = release_manager_module.activation_commands(tmp_path, steps)
    kinds = [step.kind for step in steps]

    assert kinds[:2] == [
        release_manager_module.ReleaseStepKind.QUIESCE_BACKEND,
        release_manager_module.ReleaseStepKind.WAIT_BEAT_LEASE,
    ]
    assert commands[1] == ["sleep", "31"]


def test_runtime_inspection_handles_services_without_healthchecks() -> None:
    source = (ROOT / "deploy/scripts/release_manager.py").read_text(encoding="utf-8")

    assert 'index .State "Health"' in source
    assert "{{if .State.Health}}" not in source


_QUIESCE_TEST_SERVICES = (
    "beat",
    "outbox-dispatcher",
    "worker-realtime",
    "worker-report",
    "worker-bulk",
    "worker-callback",
    "api",
)
_BACKEND_TEST_SERVICES = (
    "api",
    "worker-realtime",
    "worker-report",
    "worker-bulk",
    "worker-callback",
    "outbox-dispatcher",
    "beat",
)


def test_web_only_failure_never_touches_backend_or_data_and_rolls_back(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"web"})
    manifest["migration"]["target"] = manifest["migration"]["from"]
    manifest["migration"]["compatibility"] = "none"
    _write_private_json(manifest_path, manifest)
    manager, runner, root, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    original = manager.environment_file.read_bytes()
    runner.calls.clear()
    runner.fail_action = "up"
    runner.fail_after_effect = True

    with pytest.raises(ReleaseManagerError, match="rolled_back"):
        manager.activate(manifest["release_id"])

    actions = _compose_actions(runner)
    assert sum("up" in command and command[-1] == "web" for command in actions) == 2
    assert not any(set(_BACKEND_TEST_SERVICES) & set(command) for command in actions)
    assert not any("postgres" in command or "redis" in command for command in actions)
    assert manager.environment_file.read_bytes() == original
    assert manager.status(manifest["release_id"])["state"] == "rolled_back"


def test_data_health_failure_restores_old_image_and_resumes_backend(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"postgres"})
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    runner.calls.clear()
    runner.fail_action = "up"
    runner.fail_after_effect = True

    with pytest.raises(ReleaseManagerError, match="rolled_back"):
        manager.activate(manifest["release_id"])

    actions = _compose_actions(runner)
    assert sum("up" in command and command[-1] == "postgres" for command in actions) == 2
    assert any(
        command[-len(_BACKEND_TEST_SERVICES) :] == list(_BACKEND_TEST_SERVICES)
        for command in actions
    )
    state = manager.status(manifest["release_id"])
    assert state["state"] == "rolled_back"
    assert state["residual_changes"] == []


@pytest.mark.parametrize(
    ("observed_head", "expected_state", "residual"),
    [
        ("0011", "rolled_back", []),
        ("0012", "rolled_back", ["migration:0012"]),
        ("0011_partial", "recovery_required", None),
    ],
)
def test_migrate_failure_uses_observed_head_without_downgrade(
    tmp_path: Path,
    observed_head: str,
    expected_state: str,
    residual: list[str] | None,
) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"api"})
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    runner.calls.clear()
    runner.fail_action = "run"
    runner.migration_head_after_failed_run = observed_head

    with pytest.raises(ReleaseManagerError, match=expected_state):
        manager.activate(manifest["release_id"])

    state = manager.status(manifest["release_id"])
    assert state["state"] == expected_state
    if residual is not None:
        assert state["residual_changes"] == residual
    assert sum("run" in command for command in _compose_actions(runner)) == 1


def test_later_backend_failure_keeps_healthy_data_and_records_residual(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"api", "postgres"})
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    runner.calls.clear()
    runner.fail_action = "up"
    runner.fail_action_number = 2
    runner.fail_after_effect = True

    with pytest.raises(ReleaseManagerError, match="rolled_back"):
        manager.activate(manifest["release_id"])

    actions = _compose_actions(runner)
    assert sum("up" in command and command[-1] == "postgres" for command in actions) == 1
    assert (
        sum(
            command[-len(_BACKEND_TEST_SERVICES) :] == list(_BACKEND_TEST_SERVICES)
            for command in actions
        )
        == 2
    )
    assert manager.status(manifest["release_id"])["residual_changes"] == [
        "image:postgres",
        "migration:0012",
    ]


def test_compensation_failure_enters_recovery_required_and_preserves_evidence(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"web"})
    manifest["migration"]["target"] = manifest["migration"]["from"]
    manifest["migration"]["compatibility"] = "none"
    _write_private_json(manifest_path, manifest)
    manager, runner, _, release_root = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    runner.calls.clear()
    runner.fail_action = "up"
    runner.fail_after_effect = True
    runner.fail_compensation = True

    with pytest.raises(ReleaseManagerError, match="recovery_required"):
        manager.activate(manifest["release_id"])

    release_dir = release_root / manifest["release_id"]
    assert manager.status(manifest["release_id"])["state"] == "recovery_required"
    assert (release_dir / "original.env").is_file()
    assert (release_dir / "artifacts" / "web.tar").is_file()


@pytest.mark.parametrize(
    "step_name",
    [
        "quiesce_backend",
        "recreate_postgres",
        "recreate_redis",
        "run_migrate",
        "recreate_backend",
        "recreate_web",
        "verify",
    ],
)
def test_failure_before_each_action_never_executes_that_intended_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    step_name: str,
) -> None:
    import release_manager as release_manager_module

    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, set(IMAGE_NAMES))
    manager, runner, _, release_root = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    parsed = load_manifest(manifest_path)
    plan = release_manager_module.build_activation_plan(parsed)
    step = next(item for item in plan if item.kind.value == step_name)
    intended = release_manager_module.activation_commands(manager.root, [step])[0]
    real_record = release_manager_module.ReleaseStore.record_intent

    def fail_intent(self: Any, event_step: str, details: dict[str, object]) -> None:
        if event_step == step_name:
            raise OSError("injected intent failure")
        real_record(self, event_step, details)

    monkeypatch.setattr(release_manager_module.ReleaseStore, "record_intent", fail_intent)
    runner.calls.clear()

    with pytest.raises(ReleaseManagerError, match="rolled_back"):
        manager.activate(manifest["release_id"])

    expected_count = {"recreate_backend": 1, "verify": 2}.get(step_name, 0)
    assert runner.calls.count(intended) == expected_count
    events = [
        json.loads(line)
        for line in (release_root / manifest["release_id"] / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert not any(
        event["kind"] == "observation"
        and event["step"] == step_name
        and event["details"].get("completed") is True
        for event in events
    )
    assert manager.status(manifest["release_id"])["state"] == "rolled_back"


@pytest.mark.parametrize(
    "step_name",
    [
        "quiesce_backend",
        "recreate_postgres",
        "recreate_redis",
        "run_migrate",
        "recreate_backend",
        "recreate_web",
        "verify",
    ],
)
def test_missing_observation_after_each_action_fails_closed_to_recovery_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    step_name: str,
) -> None:
    import release_manager as release_manager_module

    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, set(IMAGE_NAMES))
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    real_record = release_manager_module.ReleaseStore.record_observation

    def fail_observation(self: Any, event_step: str, details: dict[str, object]) -> None:
        if event_step == step_name:
            raise OSError("injected observation failure")
        real_record(self, event_step, details)

    monkeypatch.setattr(
        release_manager_module.ReleaseStore,
        "record_observation",
        fail_observation,
    )
    runner.calls.clear()

    with pytest.raises(ReleaseManagerError, match="recovery_required"):
        manager.activate(manifest["release_id"])

    assert manager.status(manifest["release_id"])["state"] == "recovery_required"


def test_activation_rejects_runtime_drift_before_env_or_lifecycle_mutation(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"web"})
    manifest["migration"]["target"] = manifest["migration"]["from"]
    manifest["migration"]["compatibility"] = "none"
    _write_private_json(manifest_path, manifest)
    manager, runner, root, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    original = manager.environment_file.read_bytes()
    runner.runtime_refs["web"] = "sms-platform-web:external-drift"
    runner.calls.clear()

    with pytest.raises(ReleaseManagerError, match="recovery_required"):
        manager.activate(manifest["release_id"])

    assert manager.environment_file.read_bytes() == original
    assert not any(
        action in command
        for command in runner.calls
        for action in ("stop", "up", "run", "rm", "pull")
    )
    assert manager.status(manifest["release_id"])["state"] == "recovery_required"


@pytest.mark.parametrize(
    ("stored_state", "env_state", "service_state", "migration_state", "healthy", "expected"),
    [
        ("staged", "original", "original", "original", True, "resume"),
        ("prepared", "original", "original", "original", True, "resume"),
        ("prepared", "target", "target", "original", True, "finalize"),
        ("activating", "target", "prefix", "original", True, "resume"),
        ("activating", "original", "target", "original", True, "rollback"),
        ("rolling_back", "original", "prefix", "target", True, "rollback"),
        ("succeeded", "target", "target", "target", True, "finalize"),
        ("rolled_back", "original", "original", "target", True, "finalize"),
        ("failed", "original", "original", "original", True, "recovery_required"),
        (
            "recovery_required",
            "target",
            "prefix",
            "ambiguous",
            False,
            "recovery_required",
        ),
        ("unknown", "unknown", "ambiguous", "ambiguous", False, "recovery_required"),
        ("activating", "target", "ambiguous", "original", True, "recovery_required"),
        ("activating", "target", "target", "ambiguous", True, "recovery_required"),
        ("activating", "target", "target", "target", False, "recovery_required"),
    ],
)
def test_reconcile_release_is_exhaustive_and_deterministic(
    stored_state: str,
    env_state: str,
    service_state: str,
    migration_state: str,
    healthy: bool,
    expected: str,
) -> None:
    import release_manager as release_manager_module

    observation = release_manager_module.RuntimeObservation(
        env_state=env_state,
        service_state=service_state,
        migration_state=migration_state,
        healthy=healthy,
    )

    first = release_manager_module.reconcile_release({"state": stored_state}, observation)
    second = release_manager_module.reconcile_release({"state": stored_state}, observation)

    assert first == second
    assert first.value == expected


def test_reconcile_never_finalizes_target_services_with_required_old_migration() -> None:
    import release_manager as release_manager_module

    inconsistent = release_manager_module.RuntimeObservation(
        env_state="target",
        service_state="target",
        migration_state="original",
        healthy=True,
        migration_required=True,
    )
    safe_prefix = release_manager_module.RuntimeObservation(
        env_state="target",
        service_state="prefix",
        migration_state="original",
        healthy=True,
        migration_required=True,
    )

    assert (
        release_manager_module.reconcile_release({"state": "activating"}, inconsistent).value
        == "recovery_required"
    )
    assert (
        release_manager_module.reconcile_release({"state": "activating"}, safe_prefix).value
        == "resume"
    )


def test_runtime_probe_uses_image_ref_when_original_and_target_ids_match(
    tmp_path: Path,
) -> None:
    import release_manager as release_manager_module

    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"api"})
    manifest["images"]["api"]["id"] = "sha256:" + "5" * 64
    _write_bound_release_report(
        manifest_path.parent / "release-gate.json",
        manifest,
        _release_report(manifest),
    )
    _write_private_json(manifest_path, manifest)
    manager, runner, _, release_root = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    store = release_manager_module.ReleaseStore(release_root, manifest["release_id"])
    backend_step = next(
        step
        for step in release_manager_module.build_activation_plan(
            release_manager_module.load_manifest(manifest_path)
        )
        if step.kind is release_manager_module.ReleaseStepKind.RECREATE_BACKEND
    )

    stored_manifest = manager._stored_manifest(store)
    original, _ = manager._observe_step_runtime_status(store, stored_manifest, backend_step)
    runner.runtime_refs["api"] = manifest["images"]["api"]["ref"]
    target, _ = manager._observe_step_runtime_status(store, stored_manifest, backend_step)

    assert original == "original"
    assert target == "target"


def test_runtime_probe_binds_a_stopped_target_container_before_compensation(
    tmp_path: Path,
) -> None:
    import release_manager as release_manager_module

    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"web"})
    manifest["migration"]["target"] = manifest["migration"]["from"]
    manifest["migration"]["compatibility"] = "none"
    _write_private_json(manifest_path, manifest)
    manager, runner, _, release_root = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    store = release_manager_module.ReleaseStore(release_root, manifest["release_id"])
    web_step = next(
        step
        for step in release_manager_module.build_activation_plan(
            release_manager_module.load_manifest(manifest_path)
        )
        if step.kind is release_manager_module.ReleaseStepKind.RECREATE_WEB
    )
    runner.runtime_refs["web"] = manifest["images"]["web"]["ref"]
    runner.service_running["web"] = False
    runner.calls.clear()

    state, healthy = manager._observe_step_runtime_status(
        store,
        manager._stored_manifest(store),
        web_step,
    )

    assert state == "target"
    assert healthy is False
    assert any(command[-4:] == ["ps", "--all", "-q", "web"] for command in runner.calls)


def test_migration_probe_does_not_require_a_running_api(tmp_path: Path) -> None:
    import release_manager as release_manager_module

    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"api"})
    manager, runner, _, release_root = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    store = release_manager_module.ReleaseStore(release_root, manifest["release_id"])
    runner.service_running["api"] = False
    runner.calls.clear()

    observed = manager._observe_migration_state(store, manager._stored_manifest(store))

    assert observed == "original"
    assert any(
        "postgres" in command and "SELECT version_num FROM alembic_version" in command[-1]
        for command in runner.calls
    )
    assert not any(
        command[-5:] == ["exec", "-T", "api", "alembic", "current"] for command in runner.calls
    )


def test_resume_prepared_release_and_terminal_repeat_are_idempotent(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"web"})
    manifest["migration"]["target"] = manifest["migration"]["from"]
    manifest["migration"]["compatibility"] = "none"
    _write_private_json(manifest_path, manifest)
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    runner.calls.clear()

    manager.resume(manifest["release_id"])
    completed_calls = list(runner.calls)
    manager.resume(manifest["release_id"])

    assert manager.status(manifest["release_id"])["state"] == "succeeded"
    assert runner.calls == completed_calls


def test_resume_finalizes_sigkill_style_missing_success_observation_from_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import release_manager as release_manager_module

    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"web"})
    manifest["migration"]["target"] = manifest["migration"]["from"]
    manifest["migration"]["compatibility"] = "none"
    _write_private_json(manifest_path, manifest)
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    real_transition = release_manager_module.ReleaseStore.transition

    def lose_success_observation(
        self: Any,
        expected: Any,
        target: Any,
        **fields: object,
    ) -> None:
        if target.value == "succeeded":
            raise OSError("simulated SIGKILL before terminal persistence")
        real_transition(self, expected, target, **fields)

    with monkeypatch.context() as scoped:
        scoped.setattr(
            release_manager_module.ReleaseStore,
            "transition",
            lose_success_observation,
        )
        with pytest.raises(OSError, match="SIGKILL"):
            manager.activate(manifest["release_id"])

    runner.calls.clear()
    manager.resume(manifest["release_id"])

    assert manager.status(manifest["release_id"])["state"] == "succeeded"
    assert (
        manager._compose()
        + [
            "exec",
            "-T",
            "worker-realtime",
            "celery",
            "-A",
            "app.tasks",
            "inspect",
            "ping",
            "--timeout",
            "10",
            "--json",
        ]
        in runner.calls
    )
    assert not any(
        action in command for command in runner.calls for action in ("stop", "up", "run")
    )


def test_resume_refuses_partial_migration_and_marks_recovery_required(
    tmp_path: Path,
) -> None:
    import release_manager as release_manager_module

    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"api"})
    manager, runner, _, release_root = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    store = release_manager_module.ReleaseStore(release_root, manifest["release_id"])
    store.transition(
        release_manager_module.ReleaseState.PREPARED,
        release_manager_module.ReleaseState.ACTIVATING,
    )
    manager.configure_activation(manifest["release_id"])
    store.record_intent("run_migrate", {"services": ["migrate"]})
    runner.migration_head = "0011_partial"
    runner.calls.clear()

    with pytest.raises(ReleaseManagerError, match="recovery_required"):
        manager.resume(manifest["release_id"])

    assert manager.status(manifest["release_id"])["state"] == "recovery_required"
    assert not any("run" in command for command in _compose_actions(runner))


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_signal_before_resume_persists_and_starts_no_new_step(
    tmp_path: Path,
    signum: int,
) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"web"})
    manifest["migration"]["target"] = manifest["migration"]["from"]
    manifest["migration"]["compatibility"] = "none"
    _write_private_json(manifest_path, manifest)
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    runner.calls.clear()
    manager.request_stop(signum, None)

    with pytest.raises(ReleaseManagerError, match="interrupted"):
        manager.resume(manifest["release_id"])

    state = manager.status(manifest["release_id"])
    assert state["interrupted_signal"] == signal.Signals(signum).name
    assert not runner.calls


def test_term_after_data_step_stops_before_redis_and_persists_resume_point(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, set(IMAGE_NAMES))
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)

    def interrupt_after_postgres(action: str, count: int) -> None:
        if action == "up" and count == 1:
            manager.request_stop(signal.SIGTERM, None)

    runner.after_action = interrupt_after_postgres
    runner.calls.clear()

    with pytest.raises(ReleaseManagerError, match="interrupted"):
        manager.activate(manifest["release_id"])

    state = manager.status(manifest["release_id"])
    assert state["state"] == "activating"
    assert state["interrupted_signal"] == "SIGTERM"
    assert not any("up" in command and command[-1] == "redis" for command in runner.calls)


def test_resume_after_env_replacement_before_config_observation(
    tmp_path: Path,
) -> None:
    import release_manager as release_manager_module

    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"web"})
    manifest["migration"]["target"] = manifest["migration"]["from"]
    manifest["migration"]["compatibility"] = "none"
    _write_private_json(manifest_path, manifest)
    manager, runner, root, release_root = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    store = release_manager_module.ReleaseStore(release_root, manifest["release_id"])
    store.transition(
        release_manager_module.ReleaseState.PREPARED,
        release_manager_module.ReleaseState.ACTIVATING,
    )
    env_path = manager.environment_file
    original = env_path.read_bytes()
    store.snapshot_env(env_path)
    store.record_intent("env_replace", {"source": "manifest"})
    release_manager_module.ReleaseStore._atomic_write(
        env_path,
        manager._render_env_refs(
            original,
            {name: manifest["images"][name]["ref"] for name in IMAGE_NAMES},
        ),
        mode=stat.S_IMODE(env_path.stat().st_mode),
        private_parent=False,
    )
    runner.calls.clear()

    manager.resume(manifest["release_id"])

    assert manager.status(manifest["release_id"])["state"] == "succeeded"
    assert any(command[-2:] == ["config", "--quiet"] for command in runner.calls)
    assert sum("up" in command and command[-1] == "web" for command in runner.calls) == 1


def test_sigkill_after_data_action_reconciles_stopped_backend_and_target_data(
    tmp_path: Path,
) -> None:
    import release_manager as release_manager_module

    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, set(IMAGE_NAMES))
    manager, runner, root, release_root = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    store = release_manager_module.ReleaseStore(release_root, manifest["release_id"])
    store.transition(
        release_manager_module.ReleaseState.PREPARED,
        release_manager_module.ReleaseState.ACTIVATING,
    )
    plan = manager.configure_activation(manifest["release_id"])
    commands = release_manager_module.activation_commands(root, plan)
    quiesce_index = next(
        index for index, step in enumerate(plan) if step.kind.value == "quiesce_backend"
    )
    postgres_index = next(
        index for index, step in enumerate(plan) if step.kind.value == "recreate_postgres"
    )
    store.record_intent("quiesce_backend", {"services": list(_QUIESCE_TEST_SERVICES)})
    assert runner.run(commands[quiesce_index], cwd=root).returncode == 0
    store.record_intent("recreate_postgres", {"services": ["postgres"]})
    assert runner.run(commands[postgres_index], cwd=root).returncode == 0
    runner.calls.clear()
    resumed = ReleaseManager(
        root=root,
        release_root=release_root,
        mode=manifest["mode"],
        runner=runner,
        expected_staging_uid=os.geteuid(),
    )

    resumed.resume(manifest["release_id"])

    assert not any("up" in command and command[-1] == "postgres" for command in runner.calls)
    assert any(
        "up" in command
        and command[-3:] == ["redis", "redis-auth", "redis-control"]
        for command in runner.calls
    )
    assert resumed.status(manifest["release_id"])["state"] == "succeeded"


def test_resume_after_backend_success_does_not_restart_backend_before_web(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"api", "web"})
    manager, runner, root, release_root = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)

    def interrupt_after_backend(action: str, count: int) -> None:
        if action == "up" and count == 1:
            manager.request_stop(signal.SIGTERM, None)

    runner.after_action = interrupt_after_backend
    with pytest.raises(ReleaseManagerError, match="interrupted"):
        manager.activate(manifest["release_id"])
    resumed_runner = runner
    resumed_runner.after_action = None
    resumed_runner.calls.clear()
    resumed = ReleaseManager(
        root=root,
        release_root=release_root,
        mode=manifest["mode"],
        runner=resumed_runner,
        expected_staging_uid=os.geteuid(),
    )

    resumed.resume(manifest["release_id"])

    actions = _compose_actions(resumed_runner)
    assert not any(command[-5:] == list(_BACKEND_TEST_SERVICES) for command in actions)
    assert sum("up" in command and command[-1] == "web" for command in actions) == 1
    assert resumed.status(manifest["release_id"])["state"] == "succeeded"


def test_resume_interrupted_compensation_uses_persisted_observations(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"web"})
    manifest["migration"]["target"] = manifest["migration"]["from"]
    manifest["migration"]["compatibility"] = "none"
    _write_private_json(manifest_path, manifest)
    manager, runner, root, release_root = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    runner.fail_action = "up"
    runner.fail_after_effect = True
    real_run = runner.run

    def interrupt_on_failed_action(
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        user: int | None = None,
        group: int | None = None,
        extra_groups: Sequence[int] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        result = real_run(
            argv,
            cwd=cwd,
            env=env,
            user=user,
            group=group,
            extra_groups=extra_groups,
        )
        if result.returncode != 0:
            manager.request_stop(signal.SIGTERM, None)
        return result

    runner.run = interrupt_on_failed_action  # type: ignore[method-assign]

    with pytest.raises(ReleaseManagerError, match="interrupted"):
        manager.activate(manifest["release_id"])

    assert manager.status(manifest["release_id"])["state"] == "rolling_back"
    runner.run = real_run  # type: ignore[method-assign]
    runner.fail_action = None
    runner.calls.clear()
    resumed = ReleaseManager(
        root=root,
        release_root=release_root,
        mode=manifest["mode"],
        runner=runner,
        expected_staging_uid=os.geteuid(),
    )

    with pytest.raises(ReleaseManagerError, match="rolled_back"):
        resumed.resume(manifest["release_id"])

    assert resumed.status(manifest["release_id"])["state"] == "rolled_back"
    assert sum("up" in command and command[-1] == "web" for command in runner.calls) == 1


def test_explicit_rollback_after_data_observation_uses_same_compensation_matrix(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"postgres"})
    manager, runner, root, release_root = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)

    def interrupt_after_postgres(action: str, count: int) -> None:
        if action == "up" and count == 1:
            manager.request_stop(signal.SIGTERM, None)

    runner.after_action = interrupt_after_postgres
    with pytest.raises(ReleaseManagerError, match="interrupted"):
        manager.activate(manifest["release_id"])
    runner.after_action = None
    runner.calls.clear()
    resumed = ReleaseManager(
        root=root,
        release_root=release_root,
        mode=manifest["mode"],
        runner=runner,
        expected_staging_uid=os.geteuid(),
    )

    with pytest.raises(ReleaseManagerError, match="rolled_back"):
        resumed.rollback(manifest["release_id"])

    assert sum("up" in command and command[-1] == "postgres" for command in runner.calls) == 1
    assert resumed.status(manifest["release_id"])["state"] == "rolled_back"


@pytest.mark.parametrize("post_compensation_drift", [False, True])
def test_explicit_rollback_recovers_effect_missing_its_completed_observation(
    tmp_path: Path,
    post_compensation_drift: bool,
) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"web"})
    manifest["migration"]["target"] = manifest["migration"]["from"]
    manifest["migration"]["compatibility"] = "none"
    _write_private_json(manifest_path, manifest)
    manager, runner, root, release_root = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)

    def interrupt_after_web_effect(action: str, count: int) -> None:
        if action == "up" and count == 1:
            raise KeyboardInterrupt
        if action == "up" and count == 2 and post_compensation_drift:
            runner.runtime_refs["web"] = manifest["images"]["web"]["ref"]

    runner.after_action = interrupt_after_web_effect
    with pytest.raises(KeyboardInterrupt):
        manager.activate(manifest["release_id"])
    assert manager.status(manifest["release_id"])["state"] == "activating"
    assert runner.runtime_refs["web"] == manifest["images"]["web"]["ref"]

    runner.calls.clear()
    resumed = ReleaseManager(
        root=root,
        release_root=release_root,
        mode=manifest["mode"],
        runner=runner,
        expected_staging_uid=os.geteuid(),
    )
    expected_error = "recovery_required" if post_compensation_drift else "rolled_back"
    with pytest.raises(ReleaseManagerError, match=expected_error):
        resumed.rollback(manifest["release_id"])

    expected_state = "recovery_required" if post_compensation_drift else "rolled_back"
    assert resumed.status(manifest["release_id"])["state"] == expected_state
    expected_ref = (
        manifest["images"]["web"]["ref"]
        if post_compensation_drift
        else current_refs["web"]
    )
    assert runner.runtime_refs["web"] == expected_ref
    assert any("up" in command and command[-1] == "web" for command in runner.calls)


def test_prepared_data_release_rollback_does_not_recreate_unchanged_backend(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"postgres"})
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    runner.calls.clear()

    with pytest.raises(ReleaseManagerError, match="rolled_back"):
        manager.rollback(manifest["release_id"])

    assert manager.status(manifest["release_id"])["state"] == "rolled_back"
    assert not any("up" in command for command in runner.calls)


def test_rolling_back_resume_closes_effective_compensation_without_repeating_it(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"web"})
    manifest["migration"]["target"] = manifest["migration"]["from"]
    manifest["migration"]["compatibility"] = "none"
    _write_private_json(manifest_path, manifest)
    manager, runner, root, release_root = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)

    def interrupt_after_each_web_effect(action: str, count: int) -> None:
        if action == "up" and count in {1, 2}:
            raise KeyboardInterrupt

    runner.after_action = interrupt_after_each_web_effect
    with pytest.raises(KeyboardInterrupt):
        manager.activate(manifest["release_id"])
    first_resume = ReleaseManager(
        root=root,
        release_root=release_root,
        mode=manifest["mode"],
        runner=runner,
        expected_staging_uid=os.geteuid(),
    )
    with pytest.raises(KeyboardInterrupt):
        first_resume.rollback(manifest["release_id"])
    assert first_resume.status(manifest["release_id"])["state"] == "rolling_back"
    assert runner.runtime_refs["web"] == current_refs["web"]

    runner.after_action = None
    runner.calls.clear()
    second_resume = ReleaseManager(
        root=root,
        release_root=release_root,
        mode=manifest["mode"],
        runner=runner,
        expected_staging_uid=os.geteuid(),
    )
    with pytest.raises(ReleaseManagerError, match="rolled_back"):
        second_resume.rollback(manifest["release_id"])

    assert second_resume.status(manifest["release_id"])["state"] == "rolled_back"
    assert not any("up" in command and command[-1] == "web" for command in runner.calls)


def test_resume_complete_staged_store_is_idempotent(tmp_path: Path) -> None:
    import release_manager as release_manager_module

    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"web"})
    manifest["migration"]["target"] = manifest["migration"]["from"]
    manifest["migration"]["compatibility"] = "none"
    _write_private_json(manifest_path, manifest)
    manager, runner, _, release_root = _manager(tmp_path, manifest, current_refs)
    parsed, manifest_bytes, files = release_manager_module._validate_staging_bundle(
        manifest_path,
        os.geteuid(),
    )
    store = release_manager_module.ReleaseStore(release_root, manifest["release_id"])
    store.create(manifest_bytes)
    manager._copy_bundle(store, manifest_path, files, parsed)
    assert parsed.release_id == manifest["release_id"]

    manager.resume(manifest["release_id"])
    completed_calls = list(runner.calls)
    manager.resume(manifest["release_id"])

    assert manager.status(manifest["release_id"])["state"] == "succeeded"
    assert runner.calls == completed_calls


def _succeeded_production_release(
    tmp_path: Path,
) -> tuple[ReleaseManager, FakeRunner, Path, dict[str, Any], Path]:
    source_bundle = tmp_path / "source-bundle"
    source_bundle.mkdir()
    manifest_path, manifest, current_refs = _bundle(source_bundle, mode="production")
    manager, runner, root, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    manager.activate(manifest["release_id"])
    assert manager.status(manifest["release_id"])["state"] == "succeeded"
    return manager, runner, root, manifest, manifest_path


@pytest.fixture
def production_storage_bind_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, ...]:
    import release_manager as release_manager_module

    base = tmp_path / "production-storage"
    paths = (
        base / "postgres" / "pgdata",
        base / "redis" / "broker",
        base / "redis" / "auth",
        base / "redis" / "control",
        base / "runtime" / "imports",
        base / "runtime" / "exports",
        base / "runtime" / "raw-spill",
        base / "runtime" / "backups",
    )
    for path in paths:
        path.mkdir(parents=True)
    monkeypatch.setattr(
        release_manager_module,
        "_PRODUCTION_STORAGE_BIND_PATHS",
        paths,
    )
    return paths


def test_succeeded_release_prepares_bound_forward_rollback_candidate(
    tmp_path: Path,
) -> None:
    manager, runner, _, source, _ = _succeeded_production_release(tmp_path)
    candidate_root = tmp_path / "candidate-bundle"
    candidate_root.mkdir()
    candidate_path, candidate = _forward_candidate_bundle(candidate_root, source)
    runner.manifest = candidate
    runner.git_commit = candidate["commit"]
    manager = _switch_production_control_snapshot(
        manager,
        runner,
        candidate["commit"],
    )
    runner.calls.clear()

    manager.prepare_forward_rollback(source["release_id"], candidate_path)
    prepared = manager.status(candidate["release_id"])

    assert prepared["state"] == "prepared"
    assert prepared["release_kind"] == "forward_rollback"
    assert prepared["forward_rollback_of"] == source["release_id"]
    assert prepared["schema_retained_at"] == source["migration"]["target"]
    first_calls = list(runner.calls)
    manager.prepare_forward_rollback(source["release_id"], candidate_path)
    assert runner.calls == first_calls


def test_forward_rollback_explicitly_rejects_offline_v2_candidate(
    tmp_path: Path,
) -> None:
    manager, runner, _, source, _ = _succeeded_production_release(tmp_path)
    candidate_root = tmp_path / "offline-forward-candidate"
    candidate_root.mkdir()
    candidate_path, candidate, _ = _offline_bundle(candidate_root)
    runner.manifest = candidate
    runner.git_commit = candidate["commit"]
    runner.calls.clear()

    with pytest.raises(ReleaseManagerError, match="do not support forward rollback"):
        manager.prepare_forward_rollback(source["release_id"], candidate_path)

    assert not runner.calls


def test_forward_rollback_rejects_source_topology_drift_before_candidate_work(
    tmp_path: Path,
) -> None:
    manager, runner, root, source, source_manifest_path = _succeeded_production_release(tmp_path)
    env_path = manager.environment_file
    env_path.write_text(
        env_path.read_text(encoding="utf-8").replace(
            "REDIS_HA_MODE=isolated-standalone",
            "REDIS_HA_MODE=managed",
        ),
        encoding="utf-8",
    )
    resumed = _restart_production_manager(manager, runner)
    runner.calls.clear()

    with pytest.raises(ReleaseManagerError, match="topology.*drifted"):
        resumed.prepare_forward_rollback(source["release_id"], source_manifest_path)

    assert not runner.calls


@pytest.mark.parametrize("unsafe", ["schema_downgrade", "direct_previous_image"])
def test_forward_rollback_rejects_schema_downgrade_and_direct_image_restore(
    tmp_path: Path,
    unsafe: str,
) -> None:
    manager, runner, _, source, _ = _succeeded_production_release(tmp_path)
    source_store = ReleaseStore(manager.release_root, source["release_id"])
    snapshot = manager._read_snapshot(source_store)
    candidate_root = tmp_path / "candidate-bundle"
    candidate_root.mkdir()
    candidate_path, candidate = _forward_candidate_bundle(
        candidate_root,
        source,
        downgrade=unsafe == "schema_downgrade",
        direct_previous_ref=(
            snapshot["current_refs"]["web"] if unsafe == "direct_previous_image" else None
        ),
        direct_previous_id=(
            snapshot["image_ids"]["web"] if unsafe == "direct_previous_image" else None
        ),
    )
    runner.manifest = candidate
    runner.git_commit = candidate["commit"]
    manager = _switch_production_control_snapshot(
        manager,
        runner,
        candidate["commit"],
    )
    runner.calls.clear()

    with pytest.raises(ReleaseManagerError, match="schema|previous image|historical image"):
        manager.prepare_forward_rollback(source["release_id"], candidate_path)

    assert not (manager.release_root / candidate["release_id"]).exists()
    assert not runner.calls


def test_standard_prepare_cannot_bypass_forward_rollback_with_historical_image(
    tmp_path: Path,
) -> None:
    manager, runner, _, source, _ = _succeeded_production_release(tmp_path)
    snapshot = manager._read_snapshot(
        ReleaseStore(manager.release_root, source["release_id"])
    )
    candidate_root = tmp_path / "candidate-bundle"
    candidate_root.mkdir()
    candidate_path, candidate = _forward_candidate_bundle(
        candidate_root,
        source,
        direct_previous_ref=snapshot["current_refs"]["web"],
        direct_previous_id=snapshot["image_ids"]["web"],
    )
    runner.manifest = candidate
    runner.git_commit = candidate["commit"]
    manager = _switch_production_control_snapshot(
        manager,
        runner,
        candidate["commit"],
    )
    runner.calls.clear()

    with pytest.raises(ReleaseManagerError, match="historical image"):
        manager.prepare(candidate_path)

    assert not (manager.release_root / candidate["release_id"]).exists()
    assert not runner.calls


def test_historical_image_check_accepts_snapshot_from_before_report_worker(
    tmp_path: Path,
) -> None:
    manager, runner, _, source, _ = _succeeded_production_release(tmp_path)
    source_store = ReleaseStore(manager.release_root, source["release_id"])
    snapshot_path = source_store.release_dir / "current-snapshot.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["service_container_ids"].pop("worker-report")
    _write_private_json(snapshot_path, snapshot)

    with pytest.raises(ReleaseManagerError, match="service mapping"):
        manager._read_snapshot(source_store)

    candidate_root = tmp_path / "candidate-bundle"
    candidate_root.mkdir()
    candidate_path, candidate = _forward_candidate_bundle(
        candidate_root,
        source,
        direct_previous_ref=snapshot["current_refs"]["web"],
        direct_previous_id=snapshot["image_ids"]["web"],
    )
    runner.manifest = candidate
    runner.git_commit = candidate["commit"]
    manager = _switch_production_control_snapshot(
        manager,
        runner,
        candidate["commit"],
    )
    runner.calls.clear()

    with pytest.raises(ReleaseManagerError, match="historical image"):
        manager.prepare(candidate_path)

    assert not (manager.release_root / candidate["release_id"]).exists()
    assert not runner.calls


def test_staged_forward_resume_revalidates_bound_source_before_docker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, runner, root, source, _ = _succeeded_production_release(tmp_path)
    candidate_root = tmp_path / "candidate-bundle"
    candidate_root.mkdir()
    candidate_path, candidate = _forward_candidate_bundle(candidate_root, source)
    runner.manifest = candidate
    runner.git_commit = candidate["commit"]
    manager = _switch_production_control_snapshot(
        manager,
        runner,
        candidate["commit"],
    )

    def interrupt_copy(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(manager, "_copy_bundle", interrupt_copy)
    with pytest.raises(KeyboardInterrupt):
        manager.prepare_forward_rollback(source["release_id"], candidate_path)

    staged = manager.status(candidate["release_id"])
    assert staged["state"] == "staged"
    assert staged["release_kind"] == "forward_rollback"
    assert staged["forward_rollback_of"] == source["release_id"]
    env_path = manager.environment_file
    env_path.write_text(
        env_path.read_text(encoding="utf-8").replace(
            f"SMS_WEB_IMAGE={source['images']['web']['ref']}",
            "SMS_WEB_IMAGE=registry.example.com/sms/web@sha256:" + "8" * 64,
        ),
        encoding="utf-8",
    )
    resumed = _restart_production_manager(manager, runner)
    runner.calls.clear()

    with pytest.raises(ReleaseManagerError, match="current baseline"):
        resumed.resume(candidate["release_id"])

    assert resumed.status(candidate["release_id"])["state"] == "staged"
    assert runner.calls
    assert all(command[0] == "git" for command in runner.calls)


def test_production_bootstrap_is_empty_host_only_idempotent_and_seals_release(
    tmp_path: Path,
    production_storage_bind_paths: tuple[Path, ...],
) -> None:
    bundle_root = tmp_path / "baseline-bundle"
    bundle_root.mkdir()
    manifest_path, manifest, refs = _production_baseline_bundle(bundle_root)
    manager, runner, _, release_root = _manager(tmp_path, manifest, refs)
    runner.service_running = {service: False for service in RUNTIME_SERVICES}
    runner.migration_head = "uninitialized"

    state = manager.bootstrap(manifest_path, confirmed_empty_host=True)

    assert state["status"] == "succeeded"
    assert state["phase"] == "complete"
    release_state = manager.status(manifest["release_id"])
    assert release_state["state"] == "succeeded"
    assert release_state["release_kind"] == "bootstrap"
    assert release_state["release_gate_kind"] == "release"
    assert release_state["release_scan_performed"] is True
    assert release_state["control_smoke_only"] is False
    assert isinstance(release_state["prepared_at"], str)
    assert (release_root / "bootstrap-state.json").stat().st_mode & 0o777 == 0o600
    bootstrap_up_commands = [command for command in runner.calls if "up" in command]
    assert bootstrap_up_commands
    assert all(
        command[command.index("up") + 1] == "--no-build"
        for command in bootstrap_up_commands
    )
    assert runner.migration_head == manifest["migration"]["target"]
    first_calls = list(runner.calls)
    assert manager.bootstrap(manifest_path, confirmed_empty_host=True) == state
    assert runner.calls == first_calls
    assert state["production_topology"]["redis_ha_mode"] == "isolated-standalone"
    (manager.control_root / "deploy" / "docker-compose.redis-tls.yml").write_text(
        "services: {redis: {}}\n",
        encoding="utf-8",
    )
    restarted = _restart_production_manager(manager, runner)
    with pytest.raises(ReleaseManagerError, match="manual recovery"):
        restarted.bootstrap(manifest_path, confirmed_empty_host=True)


def test_production_start_gate_rechecks_offline_image_platform_and_labels(
    tmp_path: Path,
    production_storage_bind_paths: tuple[Path, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, manifest, refs = _offline_bundle(tmp_path, changed=set())
    _configure_offline_trust(tmp_path, monkeypatch)
    manager, runner, _, _ = _manager(tmp_path, manifest, refs)
    runner.service_running = {service: False for service in RUNTIME_SERVICES}
    runner.migration_head = "uninitialized"
    manager.bootstrap(manifest_path, confirmed_empty_host=True)
    runner.calls.clear()
    web_id = manifest["images"]["web"]["id"]
    runner.offline_identity_override["web"] = (
        f"{web_id}|linux/arm64|1.6.0|{COMMIT}|"
        f"{manifest['migration']['target']}"
    )

    with pytest.raises(ReleaseManagerError, match="identity is invalid"):
        manager.assert_production_start_allowed()

    assert any(
        command[:3] == ["docker", "image", "inspect"]
        and command[-1] == web_id
        and command[-2] == release_manager_module._OFFLINE_IMAGE_INSPECT_FORMAT
        for command in runner.calls
    )
    assert not any("compose" in command for command in runner.calls)


def test_offline_bootstrap_writes_target_ids_after_import_from_stage_only_env(
    tmp_path: Path,
    production_storage_bind_paths: tuple[Path, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, manifest, _ = _offline_bundle(tmp_path, changed=set())
    _configure_offline_trust(tmp_path, monkeypatch)
    staged_refs = {
        name: "sha256:" + marker * 64
        for name, marker in zip(IMAGE_NAMES, "1234", strict=True)
    }
    manager, runner, root, release_root = _manager(tmp_path, manifest, staged_refs)
    runner.service_running = {service: False for service in RUNTIME_SERVICES}
    runner.migration_head = "uninitialized"
    original_env = manager.environment_file.read_bytes()

    state = manager.bootstrap(manifest_path, confirmed_empty_host=True)

    assert state["status"] == "succeeded"
    assert manager._root_env_refs() == {
        name: manifest["images"][name]["id"] for name in IMAGE_NAMES
    }
    release_dir = release_root / manifest["release_id"]
    assert (release_dir / "original.env").read_bytes() == original_env
    events = [
        json.loads(line)
        for line in (release_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    load_observations = [
        index
        for index, event in enumerate(events)
        if event["kind"] == "observation"
        and event["step"] == "external_command"
        and event["details"].get("check") == "production offline image load"
    ]
    env_intent = next(
        index
        for index, event in enumerate(events)
        if event["kind"] == "intent" and event["step"] == "env_replace"
    )
    compose_config_intent = next(
        index
        for index, event in enumerate(events)
        if event["kind"] == "intent"
        and event["step"] == "external_command"
        and event["details"].get("check") == "production bootstrap compose_config"
    )
    assert len(load_observations) == 4
    assert max(load_observations) < env_intent < compose_config_intent


def test_production_bootstrap_seals_frozen_manifest_after_staging_replacement(
    tmp_path: Path,
    production_storage_bind_paths: tuple[Path, ...],
) -> None:
    bundle_root = tmp_path / "baseline-bundle"
    bundle_root.mkdir()
    manifest_path, manifest, refs = _production_baseline_bundle(bundle_root)
    manager, runner, _, release_root = _manager(tmp_path, manifest, refs)
    runner.service_running = {service: False for service in RUNTIME_SERVICES}
    runner.migration_head = "uninitialized"
    replacement_release_id = "replacement-bootstrap"

    def replace_staging_manifest(action: str, count: int) -> None:
        if action != "up" or count != 1:
            return
        replacement = json.loads(manifest_path.read_text(encoding="utf-8"))
        replacement["release_id"] = replacement_release_id
        _write_private_json(manifest_path, replacement)

    runner.after_action = replace_staging_manifest
    state = manager.bootstrap(manifest_path, confirmed_empty_host=True)

    assert state["release_id"] == manifest["release_id"]
    assert manager.status(manifest["release_id"])["state"] == "succeeded"
    assert not (release_root / replacement_release_id).exists()


def test_unfinished_bootstrap_blocks_generic_release_mutations(
    tmp_path: Path,
    production_storage_bind_paths: tuple[Path, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_root = tmp_path / "baseline-bundle"
    bundle_root.mkdir()
    manifest_path, manifest, refs = _production_baseline_bundle(bundle_root)
    manager, runner, root, release_root = _manager(tmp_path, manifest, refs)
    runner.service_running = {service: False for service in RUNTIME_SERVICES}
    runner.migration_head = "uninitialized"

    def interrupt_before_activation(_release_id: str) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(manager, "activate", interrupt_before_activation)
    with pytest.raises(KeyboardInterrupt):
        manager.bootstrap(manifest_path, confirmed_empty_host=True)

    assert manager.status(manifest["release_id"])["state"] == "prepared"
    bootstrap_state = json.loads(
        (release_root / "bootstrap-state.json").read_text(encoding="utf-8")
    )
    assert bootstrap_state["status"] == "failed"
    assert bootstrap_state["phase"] == "contained"
    assert all(
        runner.service_running[service] is False
        for service in ("web", *_QUIESCE_TEST_SERVICES)
    )
    restarted = _restart_production_manager(manager, runner)
    runner.calls.clear()

    for action in ("activate", "resume", "rollback"):
        with pytest.raises(ReleaseManagerError, match="bootstrap.*manual recovery"):
            getattr(restarted, action)(manifest["release_id"])

    assert restarted.status(manifest["release_id"])["state"] == "prepared"
    assert not runner.calls


def test_production_bootstrap_fails_closed_on_existing_volume_and_after_failure(
    tmp_path: Path,
    production_storage_bind_paths: tuple[Path, ...],
) -> None:
    bundle_root = tmp_path / "baseline-bundle"
    bundle_root.mkdir()
    manifest_path, manifest, refs = _production_baseline_bundle(bundle_root)
    manager, runner, _, release_root = _manager(tmp_path, manifest, refs)
    runner.service_running = {service: False for service in RUNTIME_SERVICES}
    runner.volume_inventory = "sms-platform_pgdata\n"

    with pytest.raises(ReleaseManagerError, match="explicit empty-host confirmation"):
        manager.bootstrap(manifest_path, confirmed_empty_host=False)
    assert not runner.calls

    with pytest.raises(ReleaseManagerError, match="not empty"):
        manager.bootstrap(manifest_path, confirmed_empty_host=True)

    assert not (release_root / "bootstrap-state.json").exists()
    runner.volume_inventory = ""
    runner.fail_action = "up"
    with pytest.raises(ReleaseManagerError, match="bootstrap failed"):
        manager.bootstrap(manifest_path, confirmed_empty_host=True)
    state = json.loads((release_root / "bootstrap-state.json").read_text(encoding="utf-8"))
    assert state["status"] == "failed"
    assert state["phase"] == "contained"
    assert any("stop" in command for command in runner.calls)
    events = [
        json.loads(line)
        for line in (
            release_root / manifest["release_id"] / "events.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        event["kind"] == "intent" and event["step"] == "env_replace"
        for event in events
    )
    assert any(
        event["kind"] == "observation"
        and event["step"] == "env_replace"
        and event["details"].get("completed") is True
        for event in events
    )
    calls = list(runner.calls)
    with pytest.raises(ReleaseManagerError, match="manual recovery"):
        manager.bootstrap(manifest_path, confirmed_empty_host=True)
    assert runner.calls == calls


def test_production_bootstrap_records_bundle_freeze_failure(
    tmp_path: Path,
    production_storage_bind_paths: tuple[Path, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_root = tmp_path / "baseline-bundle"
    bundle_root.mkdir()
    manifest_path, manifest, refs = _production_baseline_bundle(bundle_root)
    manager, runner, _, release_root = _manager(tmp_path, manifest, refs)
    runner.service_running = {service: False for service in RUNTIME_SERVICES}
    runner.migration_head = "uninitialized"

    def fail_copy(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected archive copy failure")

    monkeypatch.setattr(manager, "_copy_bundle", fail_copy)

    with pytest.raises(ReleaseManagerError, match="bootstrap failed"):
        manager.bootstrap(manifest_path, confirmed_empty_host=True)

    state = json.loads((release_root / "bootstrap-state.json").read_text(encoding="utf-8"))
    assert state["status"] == "failed"
    assert state["phase"] == "contained"
    assert manager.status(manifest["release_id"])["state"] == "staged"
    with pytest.raises(ReleaseManagerError, match="manual recovery"):
        manager.bootstrap(manifest_path, confirmed_empty_host=True)


@pytest.mark.parametrize("path_index", range(8))
def test_production_bootstrap_rejects_any_nonempty_bind_source_before_docker(
    tmp_path: Path,
    production_storage_bind_paths: tuple[Path, ...],
    path_index: int,
) -> None:
    bundle_root = tmp_path / "baseline-bundle"
    bundle_root.mkdir()
    manifest_path, manifest, refs = _production_baseline_bundle(bundle_root)
    manager, runner, _, release_root = _manager(tmp_path, manifest, refs)
    runner.service_running = {service: False for service in RUNTIME_SERVICES}
    (production_storage_bind_paths[path_index] / ".existing-data").write_text(
        "occupied\n",
        encoding="utf-8",
    )

    with pytest.raises(ReleaseManagerError, match="bind source is not empty"):
        manager.bootstrap(manifest_path, confirmed_empty_host=True)

    assert not (release_root / "bootstrap-state.json").exists()
    assert not runner.calls


@pytest.mark.parametrize("unsafe_kind", ["missing", "file", "symlink"])
def test_production_bootstrap_requires_real_bind_source_directories(
    tmp_path: Path,
    production_storage_bind_paths: tuple[Path, ...],
    unsafe_kind: str,
) -> None:
    bundle_root = tmp_path / "baseline-bundle"
    bundle_root.mkdir()
    manifest_path, manifest, refs = _production_baseline_bundle(bundle_root)
    manager, runner, _, release_root = _manager(tmp_path, manifest, refs)
    runner.service_running = {service: False for service in RUNTIME_SERVICES}
    unsafe = production_storage_bind_paths[0]
    unsafe.rmdir()
    if unsafe_kind == "file":
        unsafe.write_text("not-a-directory\n", encoding="utf-8")
    elif unsafe_kind == "symlink":
        unsafe.symlink_to(production_storage_bind_paths[1], target_is_directory=True)

    with pytest.raises(ReleaseManagerError, match="unavailable or unsafe"):
        manager.bootstrap(manifest_path, confirmed_empty_host=True)

    assert not (release_root / "bootstrap-state.json").exists()
    assert not runner.calls


def test_recovery_adoption_seals_nonempty_verified_baseline_and_survives_reboot(
    tmp_path: Path,
) -> None:
    bundle_root = tmp_path / "recovery-bundle"
    bundle_root.mkdir()
    manifest_path, manifest, refs = _production_baseline_bundle(bundle_root)
    manager, runner, root, release_root = _manager(tmp_path, manifest, refs)
    evidence = _recovery_evidence(
        tmp_path,
        manager.environment_file,
        manifest,
    )
    _, snapshot_sha, _, _, fence_path, _ = evidence
    _start_and_adopt_recovery(manager, runner, manifest_path, manifest, evidence)
    fence_sha = hashlib.sha256(fence_path.read_bytes()).hexdigest()
    with pytest.raises(ReleaseManagerError):
        manager.assert_production_start_allowed()
    baseline = _finish_recovery(manager)

    assert baseline["status"] == "succeeded"
    assert baseline["phase"] == "succeeded"
    release_state = manager.status(manifest["release_id"])
    assert release_state["state"] == "succeeded"
    assert release_state["release_kind"] == "recovery_baseline"
    assert release_state["snapshot_manifest_sha256"] == snapshot_sha
    assert release_state["gap_fence_sha256"] == fence_sha
    assert release_state["verified_migration_head"] == manifest["migration"]["target"]
    recovery_up_commands = [command for command in runner.calls if "up" in command]
    assert recovery_up_commands
    assert all(
        command[command.index("up") + 1] == "--no-build"
        for command in recovery_up_commands
    )
    watermark_path = (
        release_root
        / manifest["release_id"]
        / "artifacts"
        / "recovery-watermark.json"
    )
    assert watermark_path.is_file()

    restarted = _restart_production_manager(manager, runner)
    runner.calls.clear()
    runner.service_running = {service: False for service in RUNTIME_SERVICES}
    restarted.assert_production_start_allowed()

    historical = json.loads(manifest_path.read_text(encoding="utf-8"))
    historical["release_id"] = "release-historical"
    historical["commit"] = "d" * 40
    historical_store = ReleaseStore(release_root, historical["release_id"])
    historical_store.create(json.dumps(historical).encode())
    historical_store.checkpoint(
        ReleaseState.STAGED,
        production_topology=release_state["production_topology"],
        release_kind="standard",
    )
    historical_store.transition(ReleaseState.STAGED, ReleaseState.PREPARED)
    historical_store.transition(ReleaseState.PREPARED, ReleaseState.ACTIVATING)
    historical_store.transition(
        ReleaseState.ACTIVATING,
        ReleaseState.SUCCEEDED,
        verified_migration_head=historical["migration"]["target"],
    )

    restarted.assert_production_start_allowed()


def test_successful_recovery_state_does_not_block_next_prepare(tmp_path: Path) -> None:
    bundle_root = tmp_path / "recovery-bundle"
    bundle_root.mkdir()
    manifest_path, manifest, refs = _production_baseline_bundle(bundle_root)
    manager, runner, root, release_root = _manager(tmp_path, manifest, refs)
    evidence = _recovery_evidence(tmp_path, manager.environment_file, manifest)
    _start_and_adopt_recovery(manager, runner, manifest_path, manifest, evidence)
    _finish_recovery(manager)
    assert (release_root / "recovery-state.json").is_file()

    candidate_root = tmp_path / "post-recovery-candidate"
    candidate_root.mkdir()
    candidate_path, candidate = _forward_candidate_bundle(candidate_root, manifest)
    runner.manifest = candidate
    runner.git_commit = candidate["commit"]
    manager = _switch_production_control_snapshot(
        manager,
        runner,
        candidate["commit"],
    )
    runner.calls.clear()

    manager.prepare(candidate_path)

    assert manager.status(candidate["release_id"])["state"] == "prepared"


@pytest.mark.parametrize(
    "failure",
    [
        "confirmation",
        "empty_runtime",
        "snapshot_digest",
        "snapshot_commit",
        "gap_unverified",
        "gap_counts",
        "migration",
    ],
)
def test_recovery_adoption_rejects_unconfirmed_empty_unverified_or_drifted_host(
    tmp_path: Path,
    failure: str,
) -> None:
    bundle_root = tmp_path / "recovery-bundle"
    bundle_root.mkdir()
    manifest_path, manifest, refs = _production_baseline_bundle(bundle_root)
    manager, runner, root, release_root = _manager(tmp_path, manifest, refs)
    (
        snapshot_path,
        snapshot_sha,
        receipt_path,
        receipt_sha,
        fence_path,
        fence_sha,
    ) = _recovery_evidence(
        tmp_path,
        manager.environment_file,
        manifest,
    )
    confirmed = True
    if failure == "confirmation":
        confirmed = False
    elif failure == "snapshot_digest":
        snapshot_sha = "0" * 64
    elif failure == "snapshot_commit":
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        snapshot["git_commit"] = "d" * 40
        _write_private_json(snapshot_path, snapshot)
        snapshot_sha = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
        checksums = snapshot_path.parent / "SHA256SUMS"
        lines = [
            line
            for line in checksums.read_text(encoding="ascii").splitlines()
            if not line.endswith("  manifest.json")
        ]
        checksums.write_text(
            "\n".join([*lines, f"{snapshot_sha}  manifest.json"]) + "\n",
            encoding="ascii",
        )
        checksums.chmod(0o600)
    elif failure in {"gap_unverified", "gap_counts"}:
        fence = json.loads(fence_path.read_text(encoding="utf-8"))
        if failure == "gap_unverified":
            fence["unknown_results_blocked"] = False
        else:
            fence["upstream_request_count"] = 8
        _write_private_json(fence_path, fence)
        fence_sha = hashlib.sha256(fence_path.read_bytes()).hexdigest()
    if failure == "confirmation":
        with pytest.raises(ReleaseManagerError, match="explicit"):
            manager.start_recovery(
                manifest_path,
                snapshot_manifest_path=snapshot_path,
                snapshot_manifest_sha256=snapshot_sha,
                runtime_secrets_target=RECOVERY_RUNTIME_TARGET,
                confirmed_recovered_host=False,
            )
        assert not (release_root / "recovery-state.json").exists()
        return

    if failure in {"snapshot_digest", "snapshot_commit"}:
        with pytest.raises(ReleaseManagerError):
            manager.start_recovery(
                manifest_path,
                snapshot_manifest_path=snapshot_path,
                snapshot_manifest_sha256=snapshot_sha,
                runtime_secrets_target=RECOVERY_RUNTIME_TARGET,
                confirmed_recovered_host=True,
            )
        assert not (release_root / "bootstrap-state.json").exists()
        return

    manager.start_recovery(
        manifest_path,
        snapshot_manifest_path=snapshot_path,
        snapshot_manifest_sha256=snapshot_sha,
        runtime_secrets_target=RECOVERY_RUNTIME_TARGET,
        confirmed_recovered_host=True,
    )
    (
        snapshot_path,
        snapshot_sha,
        receipt_path,
        receipt_sha,
        fence_path,
        fence_sha,
    ) = _approve_gap_after_recovery_start(
        (
            snapshot_path,
            snapshot_sha,
            receipt_path,
            receipt_sha,
            fence_path,
            fence_sha,
        )
    )
    runner.migration_head = manifest["migration"]["target"]
    receipt_path, receipt_sha = _observe_and_approve_recovery(
        manager,
        manifest_path,
        (
            snapshot_path,
            snapshot_sha,
            receipt_path,
            receipt_sha,
            fence_path,
            fence_sha,
        ),
    )
    if failure == "empty_runtime":
        runner.service_running["postgres"] = False
    elif failure == "migration":
        runner.migration_head = "0011"

    with pytest.raises(ReleaseManagerError):
        manager.adopt_recovery(
            manifest_path,
            snapshot_manifest_path=snapshot_path,
            snapshot_manifest_sha256=snapshot_sha,
            restore_receipt_path=receipt_path,
            restore_receipt_sha256=receipt_sha,
            gap_fence_path=fence_path,
            gap_fence_sha256=fence_sha,
            runtime_secrets_target=RECOVERY_RUNTIME_TARGET,
            confirmed_recovered_host=confirmed,
        )

    assert not (release_root / "bootstrap-state.json").exists()
    assert not (release_root / manifest["release_id"]).exists()
    assert manager._read_recovery_state()["status"] in {"failed", "recovery_required"}


def test_recovery_observation_generates_bound_pending_receipt_before_adoption(
    tmp_path: Path,
) -> None:
    bundle_root = tmp_path / "recovery-bundle"
    bundle_root.mkdir()
    manifest_path, manifest, refs = _production_baseline_bundle(bundle_root)
    manager, runner, root, release_root = _manager(tmp_path, manifest, refs)
    (
        snapshot_path,
        snapshot_sha,
        receipt_path,
        receipt_sha,
        fence_path,
        fence_sha,
    ) = _recovery_evidence(
        tmp_path,
        manager.environment_file,
        manifest,
    )
    manager.start_recovery(
        manifest_path,
        snapshot_manifest_path=snapshot_path,
        snapshot_manifest_sha256=snapshot_sha,
        runtime_secrets_target=RECOVERY_RUNTIME_TARGET,
        confirmed_recovered_host=True,
    )
    refreshed = _approve_gap_after_recovery_start(
        (
            snapshot_path,
            snapshot_sha,
            receipt_path,
            receipt_sha,
            fence_path,
            fence_sha,
        )
    )
    fence_sha = refreshed[5]
    runner.migration_head = manifest["migration"]["target"]
    output_dir = tmp_path / "observed-receipt"
    output_dir.mkdir(mode=0o700)
    output_dir.chmod(0o700)
    output = output_dir / "restore-receipt.json"
    template = manager.observe_recovery(
        manifest_path,
        snapshot_manifest_path=snapshot_path,
        snapshot_manifest_sha256=snapshot_sha,
        output_path=output,
        runtime_secrets_target=RECOVERY_RUNTIME_TARGET,
        confirmed_recovered_host=True,
    )

    producer_probe = _restore_crypto_probe_receipt()
    producer_probe_digest = hashlib.sha256(
        json.dumps(producer_probe, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert template["status"] == "pending_approval"
    assert template["approved_by"] == []
    assert template["crypto_probe_status"] == producer_probe["status"]
    assert template["crypto_probe_sha256"] == producer_probe_digest
    assert runner.recovery_crypto_probe_calls == 1
    assert output.stat().st_mode & 0o777 == 0o600
    approved = dict(template)
    approved["status"] = "approved"
    approved["approved_by"] = ["operator01", "reviewer02"]
    approved["approved_at"] = datetime.now(UTC).isoformat()
    _write_private_json(output, approved)
    manager.adopt_recovery(
        manifest_path,
        snapshot_manifest_path=snapshot_path,
        snapshot_manifest_sha256=snapshot_sha,
        restore_receipt_path=output,
        restore_receipt_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
        gap_fence_path=fence_path,
        gap_fence_sha256=fence_sha,
        runtime_secrets_target=RECOVERY_RUNTIME_TARGET,
        confirmed_recovered_host=True,
    )

    assert manager.status(manifest["release_id"])["state"] == "prepared"
    assert runner.recovery_crypto_probe_calls == 3
    assert not (release_root / "bootstrap-state.json").exists()
    with pytest.raises(ReleaseManagerError):
        manager.assert_production_start_allowed()


def test_recovery_crypto_probe_rejects_legacy_v1_shape(tmp_path: Path) -> None:
    bundle_root = tmp_path / "recovery-bundle"
    bundle_root.mkdir()
    _, manifest, refs = _production_baseline_bundle(bundle_root)
    manager, runner, _, _ = _manager(tmp_path, manifest, refs)
    runner.recovery_crypto_probe = {
        "schema_version": 1,
        "status": "performed",
        "counts": {
            "audit_context_keys": 4,
            "encrypted_version_columns": 8,
            "referenced_key_versions": 2,
            "sms_message_rows": 10,
            "sms_message_key_versions_verified": 2,
        },
    }

    with pytest.raises(ReleaseManagerError, match="recovery crypto probe"):
        manager._recovery_crypto_probe()


def test_recovery_required_fence_remains_readable_and_blocks_gate_and_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_root = tmp_path / "recovery-bundle"
    bundle_root.mkdir()
    manifest_path, manifest, refs = _production_baseline_bundle(bundle_root)
    manager, _, root, _ = _manager(tmp_path, manifest, refs)
    snapshot_path, snapshot_sha, _, _, _, _ = _recovery_evidence(
        tmp_path,
        manager.environment_file,
        manifest,
    )

    def containment_failure() -> None:
        raise ReleaseManagerError("injected containment failure")

    monkeypatch.setattr(manager, "_contain_recovery_consumers", containment_failure)
    with pytest.raises(ReleaseManagerError, match="recovery_required"):
        manager.start_recovery(
            manifest_path,
            snapshot_manifest_path=snapshot_path,
            snapshot_manifest_sha256=snapshot_sha,
            runtime_secrets_target=RECOVERY_RUNTIME_TARGET,
            confirmed_recovered_host=True,
        )

    state = manager._read_recovery_state()
    assert state is not None
    assert state["status"] == "recovery_required"
    assert state["phase"] == "failed"
    with pytest.raises(ReleaseManagerError):
        manager.assert_production_start_allowed()
    with pytest.raises(ReleaseManagerError, match="manual recovery"):
        manager.resume_recovery(
            stage="api",
            runtime_secrets_target=RECOVERY_RUNTIME_TARGET,
            confirmed_recovered_host=True,
        )


@pytest.mark.parametrize("state", ["prepared", "failed", "recovery_required"])
def test_start_gate_rejects_non_succeeded_current_release(
    tmp_path: Path,
    state: str,
) -> None:
    bundle_root = tmp_path / "recovery-bundle"
    bundle_root.mkdir()
    manifest_path, manifest, refs = _production_baseline_bundle(bundle_root)
    manager, runner, root, _ = _manager(tmp_path, manifest, refs)
    evidence = _recovery_evidence(
        tmp_path,
        manager.environment_file,
        manifest,
    )
    _start_and_adopt_recovery(manager, runner, manifest_path, manifest, evidence)
    _finish_recovery(manager)
    state_path = manager.release_root / manifest["release_id"] / "state.json"
    current = json.loads(state_path.read_text(encoding="utf-8"))
    current["state"] = state
    _write_private_json(state_path, current)

    with pytest.raises(ReleaseManagerError):
        manager.assert_production_start_allowed()


@pytest.mark.parametrize("terminal_state", ["failed", "rolled_back"])
def test_start_gate_ignores_same_commit_terminal_noncurrent_records(
    tmp_path: Path,
    terminal_state: str,
) -> None:
    bundle_root = tmp_path / "recovery-bundle"
    bundle_root.mkdir()
    manifest_path, manifest, refs = _production_baseline_bundle(bundle_root)
    manager, runner, root, release_root = _manager(tmp_path, manifest, refs)
    evidence = _recovery_evidence(tmp_path, manager.environment_file, manifest)
    _start_and_adopt_recovery(manager, runner, manifest_path, manifest, evidence)
    _finish_recovery(manager)

    duplicate = json.loads(manifest_path.read_text(encoding="utf-8"))
    duplicate["release_id"] = f"terminal-{terminal_state}"
    duplicate_store = ReleaseStore(release_root, duplicate["release_id"])
    duplicate_store.create(json.dumps(duplicate).encode())
    if terminal_state == "failed":
        duplicate_store.transition(ReleaseState.STAGED, ReleaseState.FAILED)
    else:
        duplicate_store.transition(ReleaseState.STAGED, ReleaseState.PREPARED)
        duplicate_store.transition(ReleaseState.PREPARED, ReleaseState.ACTIVATING)
        duplicate_store.transition(
            ReleaseState.ACTIVATING,
            ReleaseState.ROLLING_BACK,
        )
        duplicate_store.transition(
            ReleaseState.ROLLING_BACK,
            ReleaseState.ROLLED_BACK,
        )

    manager.assert_production_start_allowed()


@pytest.mark.parametrize("drift", ["missing", "duplicate", "refs", "topology", "migration"])
def test_start_gate_binds_unique_current_commit_refs_topology_and_migration(
    tmp_path: Path,
    drift: str,
) -> None:
    bundle_root = tmp_path / "recovery-bundle"
    bundle_root.mkdir()
    manifest_path, manifest, refs = _production_baseline_bundle(bundle_root)
    manager, runner, root, release_root = _manager(tmp_path, manifest, refs)
    evidence = _recovery_evidence(
        tmp_path,
        manager.environment_file,
        manifest,
    )
    _start_and_adopt_recovery(manager, runner, manifest_path, manifest, evidence)
    _finish_recovery(manager)
    if drift == "missing":
        runner.git_commit = "d" * 40
    elif drift == "duplicate":
        duplicate = json.loads(manifest_path.read_text(encoding="utf-8"))
        duplicate["release_id"] = "release-current-duplicate"
        duplicate_store = ReleaseStore(release_root, duplicate["release_id"])
        duplicate_store.create(json.dumps(duplicate).encode())
        topology = manager.status(manifest["release_id"])["production_topology"]
        duplicate_store.checkpoint(
            ReleaseState.STAGED,
            production_topology=topology,
            release_kind="standard",
        )
        duplicate_store.transition(ReleaseState.STAGED, ReleaseState.PREPARED)
        duplicate_store.transition(ReleaseState.PREPARED, ReleaseState.ACTIVATING)
        duplicate_store.transition(
            ReleaseState.ACTIVATING,
            ReleaseState.SUCCEEDED,
            verified_migration_head=duplicate["migration"]["target"],
        )
    elif drift == "refs":
        env = manager.environment_file
        env.write_text(
            env.read_text(encoding="utf-8").replace(
                refs["web"],
                "registry.example.com/sms/web@sha256:" + "f" * 64,
            ),
            encoding="utf-8",
        )
    elif drift == "topology":
        (manager.control_root / "deploy" / "docker-compose.redis-tls.yml").write_text(
            "services: {redis: {image: changed}}\n",
            encoding="utf-8",
        )
    else:
        state_path = release_root / manifest["release_id"] / "state.json"
        current = json.loads(state_path.read_text(encoding="utf-8"))
        current["verified_migration_head"] = "0011"
        _write_private_json(state_path, current)

    with pytest.raises(ReleaseManagerError):
        manager.assert_production_start_allowed()


def test_start_gate_translates_release_store_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import release_manager as release_manager_module

    bundle_root = tmp_path / "recovery-bundle"
    bundle_root.mkdir()
    manifest_path, manifest, refs = _production_baseline_bundle(bundle_root)
    manager, runner, root, _ = _manager(tmp_path, manifest, refs)
    evidence = _recovery_evidence(
        tmp_path,
        manager.environment_file,
        manifest,
    )
    _start_and_adopt_recovery(manager, runner, manifest_path, manifest, evidence)
    _finish_recovery(manager)

    def unavailable(_store: ReleaseStore) -> dict[str, object]:
        raise release_manager_module.ReleaseStoreError("corrupt state")

    monkeypatch.setattr(release_manager_module.ReleaseStore, "read_state", unavailable)
    with pytest.raises(ReleaseManagerError, match="baseline is unavailable"):
        manager.assert_production_start_allowed()


def test_production_compose_overlays_are_strict_and_development_stays_base_only(
    tmp_path: Path,
) -> None:
    development_root = tmp_path / "development"
    development_root.mkdir()
    _, development, refs = _bundle(development_root)
    development_manager, _, _, _ = _manager(
        tmp_path / "development-manager",
        development,
        refs,
    )
    development_files = [
        Path(value).name
        for value in development_manager._compose()
        if value.endswith(".yml")
    ]
    assert development_files == ["docker-compose.yml"]

    production_root = tmp_path / "production"
    production_root.mkdir()
    _, production, production_refs = _bundle(production_root, mode="production")
    production_manager, production_runner, root, release_root = _manager(
        tmp_path / "production-manager",
        production,
        production_refs,
    )
    production_files = [
        Path(value).name
        for value in production_manager._compose()
        if value.endswith(".yml")
    ]
    assert production_files == [
        "docker-compose.yml",
        "docker-compose.production-storage.yml",
        "docker-compose.production-restart.yml",
        "docker-compose.redis-tls.yml",
    ]

    env_path = production_manager.environment_file
    env_path.write_text(
        env_path.read_text(encoding="utf-8").replace(
            "REDIS_HA_MODE=isolated-standalone",
            "REDIS_HA_MODE=managed",
        ),
        encoding="utf-8",
    )
    production_files = [
        Path(value).name
        for value in production_manager._compose()
        if value.endswith(".yml")
    ]
    assert production_files == [
        "docker-compose.yml",
        "docker-compose.production-storage.yml",
        "docker-compose.production-restart.yml",
        "docker-compose.redis-tls.yml",
    ]

    managed_manager = _restart_production_manager(
        production_manager,
        production_runner,
    )
    managed_files = [
        Path(value).name
        for value in managed_manager._compose()
        if value.endswith(".yml")
    ]
    assert managed_files == [
        "docker-compose.yml",
        "docker-compose.production-storage.yml",
        "docker-compose.production-restart.yml",
    ]

    for invalid in ("standalone", "managed\nREDIS_HA_MODE=managed", ""):
        lines = [
            line
            for line in env_path.read_text(encoding="utf-8").splitlines()
            if not line.startswith("REDIS_HA_MODE=")
        ]
        if invalid:
            lines.append(f"REDIS_HA_MODE={invalid}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        invalid_manager = _restart_production_manager(
            production_manager,
            production_runner,
        )
        with pytest.raises(ReleaseManagerError, match="REDIS_HA_MODE"):
            invalid_manager._compose()


@pytest.mark.parametrize("action", ["activate", "resume", "rollback"])
@pytest.mark.parametrize(
    "drift",
    ["mode", "compose_file", "non_image_env", "duplicate_image_ref"],
)
@pytest.mark.parametrize("restart_manager", [False, True])
def test_release_mutations_reject_persisted_production_topology_drift(
    tmp_path: Path,
    action: str,
    drift: str,
    restart_manager: bool,
) -> None:
    bundle_root = tmp_path / "release-bundle"
    bundle_root.mkdir()
    manifest_path, manifest, refs = _bundle(bundle_root, mode="production")
    manager, runner, root, release_root = _manager(tmp_path, manifest, refs)
    manager.prepare(manifest_path)
    if drift == "mode":
        env_path = manager.environment_file
        env_path.write_text(
            env_path.read_text(encoding="utf-8").replace(
                "REDIS_HA_MODE=isolated-standalone",
                "REDIS_HA_MODE=managed",
            ),
            encoding="utf-8",
        )
    elif drift == "compose_file":
        (
            manager.control_root / "deploy" / "docker-compose.production-restart.yml"
        ).write_text(
            "services: {api: {restart: unless-stopped}}\n",
            encoding="utf-8",
        )
    elif drift == "non_image_env":
        env_path = manager.environment_file
        env_path.write_text(
            env_path.read_text(encoding="utf-8") + "SAFE_CONFIG=changed\n",
            encoding="utf-8",
        )
    else:
        env_path = manager.environment_file
        env_path.write_text(
            env_path.read_text(encoding="utf-8")
            + f"SMS_WEB_IMAGE={refs['web']}\n",
            encoding="utf-8",
        )
    restarted = manager
    if restart_manager:
        restarted = _restart_production_manager(manager, runner)
    runner.calls.clear()

    with pytest.raises(ReleaseManagerError, match="topology.*drifted"):
        getattr(restarted, action)(manifest["release_id"])

    assert restarted.status(manifest["release_id"])["state"] == "prepared"
    assert all(command[0] == "git" for command in runner.calls)


def test_production_topology_ignores_only_release_image_ref_changes(
    tmp_path: Path,
) -> None:
    bundle_root = tmp_path / "release-bundle"
    bundle_root.mkdir()
    manifest_path, manifest, refs = _bundle(bundle_root, mode="production")
    manager, _, root, _ = _manager(tmp_path, manifest, refs)

    manager.prepare(manifest_path)
    expected = manager.status(manifest["release_id"])["production_topology"]
    env_path = manager.environment_file
    env_path.write_text(
        env_path.read_text(encoding="utf-8").replace(
            f"SMS_WEB_IMAGE={refs['web']}",
            f"SMS_WEB_IMAGE={manifest['images']['web']['ref']}",
        ),
        encoding="utf-8",
    )

    assert manager._production_topology() == expected


def _recovery_cli_state_result(
    *,
    runtime_target: str,
    backup_generation: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "status": "running",
        "phase": "adopted",
        "release_id": "release-recovery-cli",
        "commit": COMMIT,
        "manifest_sha256": "1" * 64,
        "production_topology": {
            "schema_version": 1,
            "redis_ha_mode": "isolated-standalone",
            "compose_files": [
                {"name": "deploy/docker-compose.yml", "sha256": "2" * 64},
                {
                    "name": "deploy/docker-compose.production-storage.yml",
                    "sha256": "c" * 64,
                },
                {
                    "name": "deploy/docker-compose.production-restart.yml",
                    "sha256": "e" * 64,
                },
                {
                    "name": "deploy/docker-compose.redis-tls.yml",
                    "sha256": "d" * 64,
                },
            ],
            "root_env_non_image_sha256": "3" * 64,
            "topology_id": "4" * 64,
        },
        "runtime_secrets_target": runtime_target,
        "migration_head": "0012",
        "snapshot_id": "snapshot-recovery-cli",
        "snapshot_manifest_sha256": "5" * 64,
        "snapshot_database_sha256": "6" * 64,
        "recovery_crypto_generation_id": "crypto-generation-01",
        "backup_passphrase_generation_id": backup_generation,
        "restore_receipt_sha256": "7" * 64,
        "live_database_fingerprint_sha256": "8" * 64,
        "crypto_probe_status": "performed",
        "crypto_probe_sha256": "9" * 64,
        "gap_fence_sha256": "a" * 64,
        "recovery_watermark_sha256": "b" * 64,
        "started_at": "2026-08-24T00:00:00Z",
        "updated_at": "2026-08-24T00:01:00Z",
        "failure_type": None,
    }


def _recovery_cli_receipt_result(*, backup_generation: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "record_type": "production_recovery_restore_receipt",
        "status": "pending_approval",
        "snapshot_id": "snapshot-recovery-cli",
        "snapshot_manifest_sha256": "5" * 64,
        "snapshot_database_sha256": "6" * 64,
        "git_commit": COMMIT,
        "migration_head": "0012",
        "database": "sms",
        "recovery_crypto_generation_id": "crypto-generation-01",
        "backup_passphrase_generation_id": backup_generation,
        "live_database_fingerprint_sha256": "8" * 64,
        "crypto_probe_status": "performed",
        "crypto_probe_sha256": "9" * 64,
        "restored_at": "2026-08-24T00:01:00Z",
        "approved_by": [],
        "approved_at": None,
    }


def _recovery_cli_arguments(action: str) -> list[str]:
    common = [
        "--platform-root",
        "/tmp/platform",
        "--control-root",
        f"/tmp/production-control/versions/{COMMIT}",
        "--environment-file",
        str(release_manager_module._PRODUCTION_ENVIRONMENT_FILE),
        "--release-root",
        "/var/lib/sms-platform/releases",
        "--mode",
        "production",
        action,
        "--manifest",
        "/tmp/staging/manifest.json",
        "--snapshot-manifest",
        "/tmp/snapshot/manifest.json",
        "--snapshot-manifest-sha256",
        "password-cli-value",
    ]
    if action == "observe-recovery":
        return [
            *common,
            "--output",
            "/tmp/passphrase-cli-value.json",
            "--runtime-secrets-target",
            "secret-runtime-cli-value",
            "--confirm-recovered-host",
        ]
    return [
        *common,
        "--restore-receipt",
        "/tmp/restore-receipt.json",
        "--restore-receipt-sha256",
        "passphrase-cli-value",
        "--gap-fence-evidence",
        "/tmp/gap-fence.json",
        "--gap-fence-sha256",
        "secret-cli-value",
        "--runtime-secrets-target",
        "secret-runtime-cli-value",
        "--confirm-recovered-host",
    ]


@pytest.mark.parametrize("action", ("observe-recovery", "adopt-recovery"))
def test_recovery_main_serializes_only_public_closed_set(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    action: str,
) -> None:
    import release_manager as release_manager_module

    seen: dict[str, object] = {}
    result = (
        _recovery_cli_receipt_result(backup_generation="passphrase-result-value")
        if action == "observe-recovery"
        else _recovery_cli_state_result(
            runtime_target="secret-runtime-cli-value",
            backup_generation="passphrase-result-value",
        )
    )

    class StubManager:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def request_stop(self, _signum: int, _frame: object | None) -> None:
            pass

        def observe_recovery(self, _manifest: Path, **kwargs: object) -> dict[str, object]:
            seen.update(kwargs)
            return result

        def adopt_recovery(self, _manifest: Path, **kwargs: object) -> dict[str, object]:
            seen.update(kwargs)
            return result

    monkeypatch.setattr(release_manager_module, "ReleaseManager", StubManager)

    return_code = release_manager_module.main(_recovery_cli_arguments(action))
    captured = capsys.readouterr()

    assert return_code == 0
    payload = json.loads(captured.out)
    assert payload["status"] == result["status"]
    assert payload["snapshot_id"] == result["snapshot_id"]
    serialized = captured.out.casefold()
    for marker in ("password", "passphrase", "secret"):
        assert marker not in serialized
    assert "runtime_secrets_target" not in payload
    assert "backup_passphrase_generation_id" not in payload
    assert captured.err == ""
    assert seen["snapshot_manifest_sha256"] == "password-cli-value"
    assert seen["runtime_secrets_target"] == "secret-runtime-cli-value"


@pytest.mark.parametrize("field", ("password", "passphrase", "secret"))
def test_recovery_main_rejects_unapproved_sensitive_result_fields_without_leak(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    field: str,
) -> None:
    import release_manager as release_manager_module

    result = _recovery_cli_state_result(
        runtime_target="secret-runtime-cli-value",
        backup_generation="passphrase-result-value",
    )
    result[field] = f"{field}-result-value"

    class StubManager:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def request_stop(self, _signum: int, _frame: object | None) -> None:
            pass

        def adopt_recovery(self, _manifest: Path, **_kwargs: object) -> dict[str, object]:
            return result

    monkeypatch.setattr(release_manager_module, "ReleaseManager", StubManager)

    return_code = release_manager_module.main(
        _recovery_cli_arguments("adopt-recovery")
    )
    captured = capsys.readouterr()

    assert return_code == 1
    assert captured.out == ""
    assert f"{field}-result-value" not in captured.err
    assert "invalid fields" in captured.err


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("status", "password-running"),
        ("release_id", "passphrase-result-value"),
        ("failure_type", "SecretFailure"),
    ),
)
def test_recovery_main_rejects_sensitive_values_in_public_fields_without_leak(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    field: str,
    value: str,
) -> None:
    import release_manager as release_manager_module

    result = _recovery_cli_state_result(
        runtime_target="secret-runtime-cli-value",
        backup_generation="passphrase-result-value",
    )
    result[field] = value

    class StubManager:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def request_stop(self, _signum: int, _frame: object | None) -> None:
            pass

        def adopt_recovery(self, _manifest: Path, **_kwargs: object) -> dict[str, object]:
            return result

    monkeypatch.setattr(release_manager_module, "ReleaseManager", StubManager)

    return_code = release_manager_module.main(
        _recovery_cli_arguments("adopt-recovery")
    )
    captured = capsys.readouterr()

    assert return_code == 1
    assert captured.out == ""
    assert value not in captured.err
    assert "invalid" in captured.err


def test_main_registers_and_restores_term_int_hup_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import release_manager as release_manager_module

    registrations: list[tuple[int, object]] = []

    class StubManager:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def request_stop(self, _signum: int, _frame: object | None) -> None:
            pass

        def status(self, _release_id: str) -> dict[str, object]:
            return {"state": "prepared"}

    monkeypatch.setattr(release_manager_module, "ReleaseManager", StubManager)
    monkeypatch.setattr(release_manager_module.signal, "getsignal", lambda signum: f"old-{signum}")
    monkeypatch.setattr(
        release_manager_module.signal,
        "signal",
        lambda signum, handler: registrations.append((signum, handler)),
    )

    result = release_manager_module.main(
        [
            "--root",
            "/tmp/platform",
            "--release-root",
            "/var/lib/sms-platform/releases",
            "--mode",
            "development",
            "status",
            "--release-id",
            "release-1",
        ]
    )

    assert result == 0
    assert [item[0] for item in registrations[:3]] == [
        signal.SIGTERM,
        signal.SIGINT,
        signal.SIGHUP,
    ]
    assert registrations[-3:] == [
        (signal.SIGTERM, f"old-{signal.SIGTERM}"),
        (signal.SIGINT, f"old-{signal.SIGINT}"),
        (signal.SIGHUP, f"old-{signal.SIGHUP}"),
    ]


def test_production_manager_requires_distinct_explicit_roots(tmp_path: Path) -> None:
    _, manifest, refs = _bundle(tmp_path, mode="production")
    root, control_root, environment_file, release_root = _platform(
        tmp_path / "manager",
        refs,
        migration_from=manifest["migration"]["from"],
        migration_target=manifest["migration"]["target"],
    )

    with pytest.raises(ReleaseManagerError, match="legacy root"):
        ReleaseManager(
            root=root,
            release_root=release_root,
            mode="production",
        )
    with pytest.raises(ReleaseManagerError, match="must be distinct"):
        ReleaseManager(
            platform_root=root,
            control_root=root,
            release_root=release_root,
            mode="production",
        )
    with pytest.raises(ReleaseManagerError, match="environment file is required"):
        ReleaseManager(
            platform_root=root,
            control_root=control_root,
            release_root=release_root,
            mode="production",
        )

    manager = ReleaseManager(
        platform_root=root,
        control_root=control_root,
        environment_file=environment_file,
        release_root=release_root,
        mode="production",
    )
    assert manager.platform_root == root
    assert manager.control_root == control_root
    assert manager.environment_file == environment_file


def test_production_environment_authority_default_is_fixed() -> None:
    assert Path("/etc/sms-platform/platform.env") == DEFAULT_PRODUCTION_ENVIRONMENT_FILE


@pytest.mark.parametrize(
    "unsafe",
    [
        "file_mode",
        "file_symlink",
        "file_directory",
        "parent_mode",
        "parent_other_mode",
        "parent_symlink",
        "owner",
        "group",
    ],
)
def test_production_environment_authority_rejects_unsafe_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe: str,
) -> None:
    _, manifest, refs = _bundle(tmp_path, mode="production")
    root, control_root, environment_file, release_root = _platform(
        tmp_path / "manager",
        refs,
        migration_from=manifest["migration"]["from"],
        migration_target=manifest["migration"]["target"],
    )
    if unsafe == "file_mode":
        environment_file.chmod(0o640)
    elif unsafe == "file_symlink":
        target = environment_file.with_name("real.env")
        environment_file.rename(target)
        environment_file.symlink_to(target)
    elif unsafe == "file_directory":
        environment_file.unlink()
        environment_file.mkdir()
    elif unsafe == "parent_mode":
        environment_file.parent.chmod(0o775)
    elif unsafe == "parent_other_mode":
        environment_file.parent.chmod(0o757)
    elif unsafe == "parent_symlink":
        parent = environment_file.parent
        target = parent.with_name("sms-platform-real")
        parent.rename(target)
        parent.symlink_to(target, target_is_directory=True)
    elif unsafe == "owner":
        monkeypatch.setattr(
            release_manager_module,
            "_PRODUCTION_ENVIRONMENT_UID",
            release_manager_module._PRODUCTION_ENVIRONMENT_UID + 1,
        )
    else:
        monkeypatch.setattr(
            release_manager_module,
            "_PRODUCTION_ENVIRONMENT_GID",
            release_manager_module._PRODUCTION_ENVIRONMENT_GID + 1,
        )

    with pytest.raises(ReleaseManagerError, match="environment file is invalid"):
        ReleaseManager(
            platform_root=root,
            control_root=control_root,
            environment_file=environment_file,
            release_root=release_root,
            mode="production",
        )


@pytest.mark.parametrize(
    "drift",
    ["file_mode", "file_symlink", "parent_mode", "parent_other_mode"],
)
def test_cached_production_compose_revalidates_environment_authority(
    tmp_path: Path,
    drift: str,
) -> None:
    _, manifest, refs = _bundle(tmp_path, mode="production")
    manager, _, _, _ = _manager(tmp_path, manifest, refs)
    manager._compose()
    environment_file = manager.environment_file
    if drift == "file_mode":
        environment_file.chmod(0o640)
    elif drift == "file_symlink":
        target = environment_file.with_name("real.env")
        environment_file.rename(target)
        environment_file.symlink_to(target)
    elif drift == "parent_mode":
        environment_file.parent.chmod(0o775)
    else:
        environment_file.parent.chmod(0o757)

    with pytest.raises(ReleaseManagerError, match="environment file is invalid"):
        manager._compose()


@pytest.mark.parametrize("operation", ["read", "hash", "configure"])
def test_production_environment_drift_blocks_every_active_access(
    tmp_path: Path,
    operation: str,
) -> None:
    manifest_path, manifest, refs = _bundle(tmp_path, mode="production")
    manager, runner, _, _ = _manager(tmp_path, manifest, refs)
    if operation == "configure":
        manager.prepare(manifest_path)
        runner.calls.clear()
    manager.environment_file.chmod(0o640)

    with pytest.raises(ReleaseManagerError, match="root env|environment file"):
        if operation == "read":
            manager._root_env_refs()
        elif operation == "hash":
            manager._root_env_non_image_sha256()
        else:
            manager.configure_activation(manifest["release_id"])

    assert not runner.calls


def test_production_environment_snapshot_replace_and_restore_ignore_checkout_env(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, refs = _bundle(tmp_path, mode="production")
    manager, _, root, _ = _manager(tmp_path, manifest, refs)
    manager.prepare(manifest_path)
    checkout_environment = root / ".env"
    checkout_environment.write_bytes(b"operator-controlled-decoy\n")
    original = manager.environment_file.read_bytes()

    manager.configure_activation(manifest["release_id"])

    store = ReleaseStore(manager.release_root, manifest["release_id"])
    assert (store.release_dir / "original.env").read_bytes() == original
    assert manager.environment_file.read_bytes() != original
    assert checkout_environment.read_bytes() == b"operator-controlled-decoy\n"
    manager._restore_environment(store)
    info = manager.environment_file.stat(follow_symlinks=False)
    assert manager.environment_file.read_bytes() == original
    assert checkout_environment.read_bytes() == b"operator-controlled-decoy\n"
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert info.st_uid == release_manager_module._PRODUCTION_ENVIRONMENT_UID
    assert info.st_gid == release_manager_module._PRODUCTION_ENVIRONMENT_GID


def test_production_cli_forwards_explicit_platform_and_control_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    class StubManager:
        def __init__(self, **kwargs: object) -> None:
            seen.update(kwargs)

        def request_stop(self, _signum: int, _frame: object | None) -> None:
            pass

        def status(self, _release_id: str) -> dict[str, object]:
            return {"state": "prepared"}

    monkeypatch.setattr(release_manager_module, "ReleaseManager", StubManager)
    monkeypatch.setattr(
        release_manager_module,
        "_validate_cli_release_root",
        lambda path, **_kwargs: path,
    )

    result = release_manager_module.main(
        [
            "--platform-root",
            "/srv/sms-platform",
            "--control-root",
            f"/opt/sms-control/versions/{COMMIT}",
            "--environment-file",
            str(release_manager_module._PRODUCTION_ENVIRONMENT_FILE),
            "--release-root",
            "/var/lib/sms-platform/releases",
            "--mode",
            "production",
            "status",
            "--release-id",
            "release-1",
        ]
    )

    assert result == 0
    assert seen["platform_root"] == Path("/srv/sms-platform")
    assert seen["control_root"] == Path(f"/opt/sms-control/versions/{COMMIT}")
    assert seen["environment_file"] == release_manager_module._PRODUCTION_ENVIRONMENT_FILE
    assert seen["mode"] == "production"


@pytest.mark.parametrize("environment", ["missing", "alternate"])
def test_production_cli_rejects_missing_or_alternate_environment_authority(
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
) -> None:
    constructed = False

    class ForbiddenManager:
        def __init__(self, **_kwargs: object) -> None:
            nonlocal constructed
            constructed = True

    monkeypatch.setattr(release_manager_module, "ReleaseManager", ForbiddenManager)
    environment_arguments = (
        []
        if environment == "missing"
        else [
            "--environment-file",
            str(release_manager_module._PRODUCTION_ENVIRONMENT_FILE.with_name("other.env")),
        ]
    )

    result = release_manager_module.main(
        [
            "--platform-root",
            "/srv/sms-platform",
            "--control-root",
            f"/opt/sms-control/versions/{COMMIT}",
            *environment_arguments,
            "--release-root",
            "/var/lib/sms-platform/releases",
            "--mode",
            "production",
            "status",
            "--release-id",
            "release-1",
        ]
    )

    assert result == 1
    assert constructed is False


@pytest.mark.parametrize(
    "root_arguments",
    [
        ["--root", "/srv/sms-platform"],
        ["--platform-root", "/srv/sms-platform"],
        ["--control-root", f"/opt/sms-control/versions/{COMMIT}"],
        [
            "--root",
            "/srv/sms-platform",
            "--platform-root",
            "/srv/sms-platform",
            "--control-root",
            f"/opt/sms-control/versions/{COMMIT}",
        ],
        [
            "--platform-root",
            "/srv/sms-platform",
            "--control-root",
            "/srv/sms-platform",
        ],
        [
            "--platform-root",
            "relative/platform",
            "--control-root",
            f"/opt/sms-control/versions/{COMMIT}",
        ],
        [
            "--platform-root",
            "/srv/sms-platform",
            "--control-root",
            f"relative/versions/{COMMIT}",
        ],
    ],
)
def test_production_cli_rejects_legacy_or_collapsed_roots_before_manager(
    monkeypatch: pytest.MonkeyPatch,
    root_arguments: list[str],
) -> None:
    constructed = False

    class ForbiddenManager:
        def __init__(self, **_kwargs: object) -> None:
            nonlocal constructed
            constructed = True

    monkeypatch.setattr(release_manager_module, "ReleaseManager", ForbiddenManager)

    result = release_manager_module.main(
        [
            *root_arguments,
            "--environment-file",
            str(release_manager_module._PRODUCTION_ENVIRONMENT_FILE),
            "--release-root",
            "/var/lib/sms-platform/releases",
            "--mode",
            "production",
            "status",
            "--release-id",
            "release-1",
        ]
    )

    assert result == 1
    assert constructed is False


@pytest.fixture
def smoke_release_root() -> Iterator[Path]:
    parent = Path(tempfile.gettempdir()) / (f"sms-platform-release-control-{uuid.uuid4().hex[:8]}")
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    try:
        yield parent / "releases"
    finally:
        parent.chmod(0o700)
        shutil.rmtree(parent)


def test_cli_release_root_accepts_only_fixed_production_or_strict_development_smoke(
    tmp_path: Path,
    smoke_release_root: Path,
) -> None:
    import release_manager as release_manager_module

    validate = release_manager_module._validate_cli_release_root
    assert validate(
        Path("/var/lib/sms-platform/releases"),
        mode="production",
        platform_root=tmp_path,
    ) == Path("/var/lib/sms-platform/releases")
    assert validate(
        Path("/var/lib/sms-platform/releases"),
        mode="development",
        platform_root=tmp_path,
    ) == Path("/var/lib/sms-platform/releases")
    assert (
        validate(
            smoke_release_root,
            mode="development",
            platform_root=tmp_path,
        )
        == smoke_release_root
    )
    assert not smoke_release_root.exists()
    smoke_release_root.mkdir(mode=0o700)
    smoke_release_root.chmod(0o700)
    assert (
        validate(
            smoke_release_root,
            mode="development",
            platform_root=tmp_path,
        )
        == smoke_release_root
    )

    with pytest.raises(ReleaseManagerError, match="release root"):
        validate(
            smoke_release_root,
            mode="production",
            platform_root=tmp_path,
        )


@pytest.mark.parametrize(
    "candidate",
    [
        Path("relative/releases"),
        Path("/var/lib/sms-platform/other"),
        Path(tempfile.gettempdir()) / "sms-platform-release-control-Ab12345" / "releases",
        Path(tempfile.gettempdir()) / "sms-platform-release-control-Ab12_cd34" / "releases",
        Path(tempfile.gettempdir()) / "sms-platform-release-control-Ab12Cd34" / "other",
        Path(tempfile.gettempdir())
        / "sms-platform-release-control-Ab12Cd34"
        / "child"
        / ".."
        / "releases",
        Path(tempfile.gettempdir()) / "sms-platform-release-control-Ab12Cd34\n" / "releases",
    ],
)
def test_cli_release_root_rejects_invalid_shape(
    tmp_path: Path,
    candidate: Path,
) -> None:
    import release_manager as release_manager_module

    with pytest.raises(ReleaseManagerError, match="release root"):
        release_manager_module._validate_cli_release_root(
            candidate,
            mode="development",
            platform_root=tmp_path,
        )


@pytest.mark.parametrize("unsafe", ["parent_mode", "root_mode", "root_symlink", "owner", "git"])
def test_cli_release_root_rejects_unsafe_parent_or_existing_root(
    tmp_path: Path,
    smoke_release_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe: str,
) -> None:
    import release_manager as release_manager_module

    parent = smoke_release_root.parent
    if unsafe == "parent_mode":
        parent.chmod(0o755)
    elif unsafe == "root_mode":
        smoke_release_root.mkdir(mode=0o755)
    elif unsafe == "root_symlink":
        smoke_release_root.symlink_to(tmp_path, target_is_directory=True)
    elif unsafe == "owner":
        actual_uid = os.geteuid()
        monkeypatch.setattr(release_manager_module.os, "geteuid", lambda: actual_uid + 1)
    else:
        (parent / ".git").mkdir(mode=0o700)

    with pytest.raises(ReleaseManagerError, match="release root"):
        release_manager_module._validate_cli_release_root(
            smoke_release_root,
            mode="development",
            platform_root=tmp_path,
        )


def test_cli_rejects_invalid_release_root_before_constructing_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import release_manager as release_manager_module

    constructed = False

    class ForbiddenManager:
        def __init__(self, **_kwargs: object) -> None:
            nonlocal constructed
            constructed = True

    monkeypatch.setattr(release_manager_module, "ReleaseManager", ForbiddenManager)

    result = release_manager_module.main(
        [
            "--root",
            str(tmp_path),
            "--release-root",
            str(tmp_path / "unsafe" / "releases"),
            "--mode",
            "development",
            "status",
            "--release-id",
            "release-1",
        ]
    )

    assert result == 1
    assert constructed is False
