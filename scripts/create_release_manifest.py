#!/usr/bin/env python3
"""从最终 release-gate 证据生成并自校验 production manifest。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))

from offline_image_archive import (  # noqa: E402
    EvidenceArtifact,
    OfflineImageArchiveError,
    OfflineImageIndex,
    candidate_image_ref,
    load_offline_image_index_bytes,
    validate_offline_image_archive,
)
from release_manifest import OFFLINE_IMAGE_SOURCE, load_manifest_bytes  # noqa: E402

_IMAGES = ("api", "web", "postgres", "redis")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
_DIGEST_REF_RE = re.compile(r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}")
_REPORT_FIELDS = {
    "schema_version",
    "gate_type",
    "candidate_commit",
    "source",
    "generated_at",
    "trivy_image",
    "images",
    "promotion_source",
    "passed",
}
_IMAGE_FIELDS = {
    "ref",
    "image_id",
    "repo_digests",
    "scan_report_sha256",
    "scan_passed",
}
_SOURCE_FIELDS = {
    "app_version",
    "git_sha",
    "schema_revision",
    "openapi_sha256",
    "workflow_repository",
    "workflow_run_id",
    "workflow_run_attempt",
    "sbom_sha256",
}
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_SCHEMA_RE = re.compile(r"[0-9]{4}_[a-z0-9_]+")
_REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_MAX_EVIDENCE_BYTES = 16 * 1024 * 1024
_MAX_SCAN_OR_SBOM_BYTES = 64 * 1024 * 1024
_MAX_ARCHIVE_BYTES = 8 * 1024 * 1024 * 1024
_SIGNING_KEY_ID_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?")
_OPENSSL = "openssl"
_GH = "gh"


class ManifestCreationError(ValueError):
    """最终证据不足以自动生成安全的生产发布清单。"""


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestCreationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _metadata_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_private_bytes_with_hash(
    path: Path,
    context: str,
    *,
    maximum_size: int = _MAX_EVIDENCE_BYTES,
) -> tuple[bytes, str, int]:
    if not path.is_absolute():
        raise ManifestCreationError(f"{context} must use an absolute path")
    descriptor = -1
    try:
        before_path = path.lstat()
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        before = os.fstat(descriptor)
        if (
            _metadata_identity(before_path) != _metadata_identity(before)
            or not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) not in {0o600, 0o644}
            or not 1 <= before.st_size <= maximum_size
        ):
            raise ManifestCreationError(f"{context} is not an owned single-link regular file")
        chunks: list[bytes] = []
        size = 0
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > maximum_size:
                raise ManifestCreationError(f"{context} is too large")
            chunks.append(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
        after_path = path.lstat()
        if (
            size != before.st_size
            or _metadata_identity(before) != _metadata_identity(after)
            or _metadata_identity(before) != _metadata_identity(after_path)
        ):
            raise ManifestCreationError(f"{context} changed while reading")
        return b"".join(chunks), digest.hexdigest(), size
    except ManifestCreationError:
        raise
    except OSError as exc:
        raise ManifestCreationError(f"{context} is unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_private_json_with_hash_and_size(
    path: Path,
    context: str,
) -> tuple[dict[str, Any], str, int]:
    payload, digest, size = _read_private_bytes_with_hash(path, context)
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_reject_duplicates,
        )
    except ManifestCreationError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ManifestCreationError(f"{context} is unavailable") from exc
    if type(value) is not dict:
        raise ManifestCreationError(f"{context} is invalid")
    return value, digest, size


def _read_private_json_with_hash(
    path: Path,
    context: str,
) -> tuple[dict[str, Any], str]:
    value, digest, _ = _read_private_json_with_hash_and_size(path, context)
    return value, digest


def _read_private_json(path: Path, context: str) -> dict[str, Any]:
    return _read_private_json_with_hash(path, context)[0]


def _evidence_name(path: Path | None, parent: Path, context: str) -> str | None:
    if path is None:
        return None
    _read_private_json(path, context)
    if path.parent != parent or path.name == "manifest.json":
        raise ManifestCreationError(f"{context} must be a sibling evidence file")
    return path.name


def _bound_evidence(
    path: Path | None,
    context: str,
) -> tuple[dict[str, object] | None, tuple[Path, str, int] | None]:
    if path is None:
        return None, None
    _, digest, size = _read_private_json_with_hash_and_size(path, context)
    binding: dict[str, object] = {
        "file": path.name,
        "sha256": digest,
        "size": size,
    }
    return binding, (path, digest, size)


def _validate_index_artifact(
    root: Path,
    artifact: EvidenceArtifact,
    context: str,
    *,
    maximum_size: int = _MAX_EVIDENCE_BYTES,
) -> None:
    _, digest, size = _read_private_bytes_with_hash(
        root / artifact.file,
        context,
        maximum_size=maximum_size,
    )
    if digest != artifact.sha256 or size != artifact.size:
        raise ManifestCreationError(f"{context} does not match offline image index")


def _validate_offline_index_closure(
    *,
    offline_index_path: Path,
    offline_archive_dir: Path,
    release_report: Path,
    report: dict[str, Any],
    report_sha256: str,
    report_size: int,
    source: dict[str, Any],
) -> tuple[OfflineImageIndex, str]:
    if (
        not offline_index_path.is_absolute()
        or offline_index_path.name != "offline-image-index.json"
        or not offline_archive_dir.is_absolute()
        or offline_archive_dir != offline_index_path.parent / "images"
        or release_report != offline_index_path.parent / "release-gate.json"
    ):
        raise ManifestCreationError("offline evidence paths do not match the fixed layout")
    index_payload, index_sha256, _ = _read_private_bytes_with_hash(
        offline_index_path,
        "offline image index",
    )
    try:
        index = load_offline_image_index_bytes(index_payload)
    except OfflineImageArchiveError as exc:
        raise ManifestCreationError("offline image index is invalid") from exc
    if (
        index.candidate_commit != report["candidate_commit"]
        or index.release_gate.sha256 != report_sha256
        or index.release_gate.size != report_size
    ):
        raise ManifestCreationError("offline image index is not bound to the release gate")

    source_root = offline_index_path.parent
    if index.reproducibility is not None:
        _validate_index_artifact(
            source_root,
            index.reproducibility,
            "reproducibility evidence",
        )
    report_images = report["images"]
    source_sboms = source["sbom_sha256"]
    for name in _IMAGES:
        index_image = index.images[name]
        report_image = report_images[name]
        if (
            index_image.image_id != report_image["image_id"]
            or index_image.scan.sha256 != report_image["scan_report_sha256"]
            or index_image.sbom_candidate.sha256 != source_sboms[name]
        ):
            raise ManifestCreationError(
                f"offline image index image {name} is not bound to release evidence"
            )
        _validate_index_artifact(
            source_root,
            index_image.scan,
            f"Trivy scan for {name}",
            maximum_size=_MAX_SCAN_OR_SBOM_BYTES,
        )
        _validate_index_artifact(
            source_root,
            index_image.sbom_candidate,
            f"candidate SBOM for {name}",
            maximum_size=_MAX_SCAN_OR_SBOM_BYTES,
        )
        if index_image.sbom_rebuild is not None:
            _validate_index_artifact(
                source_root,
                index_image.sbom_rebuild,
                f"rebuild SBOM for {name}",
                maximum_size=_MAX_SCAN_OR_SBOM_BYTES,
            )
    return index, index_sha256


def _verify_github_attestation(
    *,
    offline_index: Path,
    workflow_repository: str,
    candidate_commit: str,
    attestation_bundle: Path | None,
) -> None:
    if attestation_bundle is not None:
        _read_private_bytes_with_hash(
            attestation_bundle,
            "GitHub attestation bundle",
        )
    command = [
        _GH,
        "attestation",
        "verify",
        str(offline_index),
        "--repo",
        workflow_repository,
        "--signer-workflow",
        f"{workflow_repository}/.github/workflows/release-gate.yml",
        "--source-digest",
        candidate_commit,
        "--deny-self-hosted-runners",
        "--format",
        "json",
    ]
    if attestation_bundle is not None:
        command.extend(("--bundle", str(attestation_bundle)))
    try:
        completed = subprocess.run(
            tuple(command),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ManifestCreationError("GitHub attestation verification is unavailable") from exc
    if (
        completed.returncode != 0
        or not completed.stdout
        or len(completed.stdout) > _MAX_EVIDENCE_BYTES
    ):
        raise ManifestCreationError("GitHub attestation verification failed")
    try:
        verification = json.loads(
            completed.stdout.decode("utf-8"),
            object_pairs_hook=_reject_duplicates,
        )
    except ManifestCreationError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ManifestCreationError(
            "GitHub attestation verification returned invalid JSON"
        ) from exc
    if (
        type(verification) is not list
        or not verification
        or any(
            type(result) is not dict
            or type(result.get("verificationResult")) is not dict
            or not result["verificationResult"]
            for result in verification
        )
    ):
        raise ManifestCreationError("GitHub attestation verification returned no verified result")


def _stage_private_copy(
    source: Path,
    destination: Path,
    *,
    expected_sha256: str,
    expected_size: int,
    context: str,
) -> Path | None:
    if source == destination:
        _, observed_hash, size = _read_private_bytes_with_hash(
            source,
            context,
            maximum_size=_MAX_ARCHIVE_BYTES,
        )
        if observed_hash != expected_sha256 or size != expected_size:
            raise ManifestCreationError(f"{context} changed before materialization")
        return None

    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    source_descriptor = -1
    destination_descriptor = -1
    try:
        before_path = source.lstat()
        source_descriptor = os.open(
            source,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(source_descriptor)
        if (
            _metadata_identity(before_path) != _metadata_identity(before)
            or not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) not in {0o600, 0o644}
            or before.st_size != expected_size
            or not 1 <= before.st_size <= _MAX_ARCHIVE_BYTES
        ):
            raise ManifestCreationError(f"{context} is unsafe during materialization")
        destination_descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        copy_digest = hashlib.sha256()
        copied = 0
        while block := os.read(source_descriptor, 1024 * 1024):
            copied += len(block)
            if copied > expected_size:
                raise ManifestCreationError(f"{context} changed during materialization")
            copy_digest.update(block)
            view = memoryview(block)
            while view:
                written = os.write(destination_descriptor, view)
                if written <= 0:
                    raise ManifestCreationError(f"{context} copy made no forward progress")
                view = view[written:]
        os.fsync(destination_descriptor)
        after = os.fstat(source_descriptor)
        after_path = source.lstat()
        if (
            copied != expected_size
            or copy_digest.hexdigest() != expected_sha256
            or _metadata_identity(before) != _metadata_identity(after)
            or _metadata_identity(before) != _metadata_identity(after_path)
        ):
            raise ManifestCreationError(f"{context} changed during materialization")
    except ManifestCreationError:
        temporary.unlink(missing_ok=True)
        raise
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise ManifestCreationError(f"{context} cannot be materialized") from exc
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
    return temporary


def _write_private_temporary(destination: Path, payload: bytes) -> Path:
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _validate_signing_key(path: Path) -> tuple[int, ...]:
    if not path.is_absolute():
        raise ManifestCreationError("signing private key must use an absolute path")
    descriptor = -1
    try:
        before_path = path.lstat()
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        metadata = os.fstat(descriptor)
        if (
            _metadata_identity(before_path) != _metadata_identity(metadata)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or not 1 <= metadata.st_size <= 64 * 1024
        ):
            raise ManifestCreationError(
                "signing private key is not a private single-link regular file"
            )
        return _metadata_identity(metadata)
    except ManifestCreationError:
        raise
    except OSError as exc:
        raise ManifestCreationError("signing private key is unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _stage_manifest_signature(
    manifest_temporary: Path,
    signature_destination: Path,
    private_key: Path,
) -> Path:
    key_identity = _validate_signing_key(private_key)
    signature_temporary = signature_destination.with_name(
        f".{signature_destination.name}.{uuid.uuid4().hex}.tmp"
    )
    descriptor = os.open(
        signature_temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    os.close(descriptor)
    try:
        sign = subprocess.run(
            (
                _OPENSSL,
                "pkeyutl",
                "-sign",
                "-rawin",
                "-inkey",
                str(private_key),
                "-in",
                str(manifest_temporary),
                "-out",
                str(signature_temporary),
            ),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        if sign.returncode != 0:
            raise ManifestCreationError("OpenSSL could not sign the manifest")
        signature_temporary.chmod(0o600)
        signature = signature_temporary.lstat()
        if (
            not stat.S_ISREG(signature.st_mode)
            or signature.st_uid != os.geteuid()
            or signature.st_nlink != 1
            or stat.S_IMODE(signature.st_mode) != 0o600
            or signature.st_size != 64
        ):
            raise ManifestCreationError("OpenSSL produced an invalid Ed25519 signature")
        verify = subprocess.run(
            (
                _OPENSSL,
                "pkeyutl",
                "-verify",
                "-rawin",
                "-inkey",
                str(private_key),
                "-in",
                str(manifest_temporary),
                "-sigfile",
                str(signature_temporary),
            ),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        if verify.returncode != 0 or _validate_signing_key(private_key) != key_identity:
            raise ManifestCreationError("Ed25519 manifest signature verification failed")
        return signature_temporary
    except ManifestCreationError:
        signature_temporary.unlink(missing_ok=True)
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        signature_temporary.unlink(missing_ok=True)
        raise ManifestCreationError("OpenSSL could not sign the manifest") from exc


def create_manifest(
    *,
    release_report: Path,
    output: Path,
    release_id: str,
    migration_from: str,
    migration_target: str,
    changed: frozenset[str],
    baseline: bool = False,
    data_images: Path | None = None,
    backup_record: Path | None = None,
    restore_report: Path | None = None,
    offline_archive_dir: Path | None = None,
    offline_index: Path | None = None,
    signing_private_key: Path | None = None,
    signing_key_id: str | None = None,
    attestation_bundle: Path | None = None,
    allow_offline_no_conditional_evidence: bool = False,
) -> None:
    """读取最终证据，原子生成 registry v1 或离线归档 v2 清单。"""

    if not changed.issubset(_IMAGES) or (not changed and not baseline):
        raise ManifestCreationError("changed images are invalid")
    if baseline and (changed or migration_from != migration_target):
        raise ManifestCreationError("baseline manifest must have no image or migration delta")
    if not output.is_absolute() or output.name != "manifest.json":
        raise ManifestCreationError("output must be an absolute manifest.json path")
    offline_options = (
        offline_archive_dir,
        offline_index,
        signing_private_key,
        signing_key_id,
    )
    if any(value is not None for value in offline_options) and not all(
        value is not None for value in offline_options
    ):
        raise ManifestCreationError("offline archive options must be provided together")
    offline = all(value is not None for value in offline_options)
    if attestation_bundle is not None and not offline:
        raise ManifestCreationError(
            "attestation bundle is only valid for an offline archive release"
        )
    if offline and (
        migration_from != migration_target or changed not in {frozenset(), frozenset(_IMAGES)}
    ):
        raise ManifestCreationError(
            "offline release must be a no-migration baseline or all-four-image update"
        )
    offline_full_no_migration_update = (
        offline
        and changed == frozenset(_IMAGES)
        and migration_from == migration_target
    )
    backup_pair = (backup_record is not None, restore_report is not None)
    if backup_pair not in {(False, False), (True, True)}:
        raise ManifestCreationError("backup evidence must be provided as a pair")
    conditional_evidence_missing = data_images is None or not all(backup_pair)
    if allow_offline_no_conditional_evidence and (
        not offline_full_no_migration_update or not conditional_evidence_missing
    ):
        raise ManifestCreationError(
            "offline conditional evidence risk acceptance is not applicable"
        )
    if (
        offline_full_no_migration_update
        and conditional_evidence_missing
        and not allow_offline_no_conditional_evidence
    ):
        raise ManifestCreationError(
            "missing offline conditional evidence requires explicit risk acceptance"
        )
    if not offline and output.parent != release_report.parent:
        raise ManifestCreationError("manifest and release report must share a directory")
    if offline and (
        type(signing_key_id) is not str
        or _SIGNING_KEY_ID_RE.fullmatch(signing_key_id) is None
        or ".." in signing_key_id
        or signing_key_id.endswith(".part")
    ):
        raise ManifestCreationError("signing key ID is invalid")

    report, report_sha256, report_size = _read_private_json_with_hash_and_size(
        release_report,
        "release report",
    )
    if (
        set(report) != _REPORT_FIELDS
        or type(report.get("schema_version")) is not int
        or report["schema_version"] != 1
        or report.get("gate_type") != "release"
        or report.get("passed") is not True
        or type(report.get("generated_at")) is not str
        or not report["generated_at"]
        or type(report.get("candidate_commit")) is not str
        or _COMMIT_RE.fullmatch(report["candidate_commit"]) is None
        or type(report.get("trivy_image")) is not str
        or _DIGEST_REF_RE.fullmatch(report["trivy_image"]) is None
        or (
            report.get("promotion_source") is not None
            if offline
            else type(report.get("promotion_source")) is not dict
        )
    ):
        raise ManifestCreationError(
            "release report is not bound candidate evidence"
            if offline
            else "release report is not final promotion evidence"
        )
    source = report.get("source")
    if type(source) is not dict or set(source) != _SOURCE_FIELDS:
        raise ManifestCreationError("release source metadata is invalid")
    sboms = source.get("sbom_sha256")
    if (
        source.get("git_sha") != report["candidate_commit"]
        or type(source.get("app_version")) is not str
        or _VERSION_RE.fullmatch(source["app_version"]) is None
        or type(source.get("schema_revision")) is not str
        or _SCHEMA_RE.fullmatch(source["schema_revision"]) is None
        or source["schema_revision"] != migration_target
        or type(source.get("openapi_sha256")) is not str
        or _SHA256_RE.fullmatch(source["openapi_sha256"]) is None
        or type(source.get("workflow_repository")) is not str
        or _REPOSITORY_RE.fullmatch(source["workflow_repository"]) is None
        or source["workflow_repository"].casefold().startswith("local/")
        or type(source.get("workflow_run_id")) is not int
        or source["workflow_run_id"] < 1
        or type(source.get("workflow_run_attempt")) is not int
        or source["workflow_run_attempt"] < 1
        or type(sboms) is not dict
        or set(sboms) != set(_IMAGES)
        or any(
            type(digest) is not str or _SHA256_RE.fullmatch(digest) is None
            for digest in sboms.values()
        )
    ):
        raise ManifestCreationError("release source metadata is invalid")
    report_images = report.get("images")
    if type(report_images) is not dict or set(report_images) != set(_IMAGES):
        raise ManifestCreationError("release report images are invalid")

    for name in _IMAGES:
        image = report_images[name]
        if (
            type(image) is not dict
            or set(image) != _IMAGE_FIELDS
            or type(image.get("ref")) is not str
            or type(image.get("image_id")) is not str
            or _IMAGE_ID_RE.fullmatch(image["image_id"]) is None
            or type(image.get("repo_digests")) is not list
            or any(
                type(value) is not str or _DIGEST_REF_RE.fullmatch(value) is None
                for value in image["repo_digests"]
            )
            or (offline and image["repo_digests"] != [])
            or type(image.get("scan_report_sha256")) is not str
            or _SHA256_RE.fullmatch(image["scan_report_sha256"]) is None
            or image.get("scan_passed") is not True
            or (
                (image["ref"] != candidate_image_ref(name, report["candidate_commit"]))
                if offline
                else (
                    _DIGEST_REF_RE.fullmatch(image["ref"]) is None
                    or image["ref"] not in image["repo_digests"]
                )
            )
        ):
            raise ManifestCreationError(f"release report image {name} is invalid")

    parsed_index: OfflineImageIndex | None = None
    index_sha256: str | None = None
    if offline:
        assert offline_index is not None
        assert offline_archive_dir is not None
        parsed_index, index_sha256 = _validate_offline_index_closure(
            offline_index_path=offline_index,
            offline_archive_dir=offline_archive_dir,
            release_report=release_report,
            report=report,
            report_sha256=report_sha256,
            report_size=report_size,
            source=source,
        )
        _verify_github_attestation(
            offline_index=offline_index,
            workflow_repository=source["workflow_repository"],
            candidate_commit=report["candidate_commit"],
            attestation_bundle=attestation_bundle,
        )

    images: dict[str, dict[str, object]] = {}
    verified_archives: dict[str, tuple[Path, str, int]] = {}
    for name in _IMAGES:
        image = report_images[name]
        if parsed_index is None:
            images[name] = {
                "ref": image["ref"],
                "id": image["image_id"],
                "archive_file": None,
                "archive_sha256": None,
                "changed": name in changed,
            }
            continue
        assert offline_archive_dir is not None
        index_image = parsed_index.images[name]
        archive_path = offline_archive_dir / f"{name}.tar"
        try:
            archive = validate_offline_image_archive(
                archive_path,
                name=name,
                expected_sha256=index_image.archive.sha256,
                expected_size=index_image.archive.size,
            )
        except OfflineImageArchiveError as exc:
            raise ManifestCreationError(f"offline Docker archive for {name} is invalid") from exc
        images[name] = {
            "ref": image["image_id"],
            "id": image["image_id"],
            "archive_file": f"{name}.tar",
            "archive_sha256": archive.sha256,
            "archive_size": archive.size,
            "changed": name in changed,
        }
        verified_archives[name] = (archive_path, archive.sha256, archive.size)

    conditional_copies: list[tuple[Path, str, int]] = []
    data_evidence: str | dict[str, object] | None
    if offline:
        data_evidence, data_copy = _bound_evidence(
            data_images,
            "data image evidence",
        )
        if data_copy is not None:
            conditional_copies.append(data_copy)
    else:
        data_evidence = _evidence_name(
            data_images,
            output.parent,
            "data image evidence",
        )
    data_changed = bool(changed & {"postgres", "redis"})
    data_evidence_required = data_changed and not offline_full_no_migration_update
    if (data_evidence_required and data_evidence is None) or (
        not data_changed and data_evidence is not None
    ):
        raise ManifestCreationError("data image evidence does not match changed images")
    backup_allowed = "postgres" in changed or migration_from != migration_target
    backup_required = backup_allowed and not offline_full_no_migration_update
    if (backup_required and not all(backup_pair)) or (
        not backup_allowed and all(backup_pair)
    ):
        raise ManifestCreationError("backup evidence does not match PostgreSQL or migration change")
    backup_evidence: dict[str, object] | None
    if not all(backup_pair):
        backup_evidence = None
    elif offline:
        backup_record_binding, backup_copy = _bound_evidence(
            backup_record,
            "backup change record",
        )
        restore_binding, restore_copy = _bound_evidence(
            restore_report,
            "restore report",
        )
        assert backup_record_binding is not None
        assert restore_binding is not None
        assert backup_copy is not None
        assert restore_copy is not None
        conditional_copies.extend((backup_copy, restore_copy))
        backup_evidence = {
            "record": backup_record_binding,
            "restore_report": restore_binding,
        }
    else:
        backup_evidence = {
            "record": _evidence_name(
                backup_record,
                output.parent,
                "backup change record",
            ),
            "restore_report": _evidence_name(
                restore_report,
                output.parent,
                "restore report",
            ),
        }
    evidence: dict[str, object] = {
        "release_gate_kind": "release",
        "release_gate": release_report.name,
        "release_gate_sha256": report_sha256,
        "data_images": data_evidence,
        "backup_restore_change": backup_evidence,
    }
    if parsed_index is not None:
        assert index_sha256 is not None
        evidence["offline_image_index"] = {
            "file": "offline-image-index.json",
            "sha256": index_sha256,
        }
    manifest: dict[str, object] = {
        "schema_version": 2 if offline else 1,
        "release_id": release_id,
        "commit": report["candidate_commit"],
        "mode": "production",
        "images": images,
        "migration": {
            "from": migration_from,
            "target": migration_target,
            "compatibility": ("none" if migration_from == migration_target else "expand"),
        },
        "evidence": evidence,
    }
    if offline:
        manifest["image_source"] = OFFLINE_IMAGE_SOURCE
        manifest["signing"] = {
            "algorithm": "ed25519",
            "key_id": signing_key_id,
            "file": "manifest.sig",
        }
    payload = (
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()
    try:
        load_manifest_bytes(payload)
    except ValueError as exc:
        raise ManifestCreationError("generated manifest failed strict validation") from exc
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not offline:
        temporary = _write_private_temporary(output, payload)
        try:
            os.replace(temporary, output)
            output.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)
        return

    output_directory = output.parent.lstat()
    if (
        not stat.S_ISDIR(output_directory.st_mode)
        or output_directory.st_uid != os.geteuid()
        or stat.S_IMODE(output_directory.st_mode) != 0o700
    ):
        raise ManifestCreationError("offline output directory must be a private directory")
    assert offline_index is not None
    assert signing_private_key is not None
    assert parsed_index is not None
    staged: list[tuple[Path, Path]] = []
    try:
        copy_specs = [
            (
                offline_index,
                output.parent / "offline-image-index.json",
                index_sha256,
                offline_index.lstat().st_size,
                "offline image index",
            ),
            (
                release_report,
                output.parent / "release-gate.json",
                report_sha256,
                report_size,
                "release report",
            ),
        ]
        copy_specs.extend(
            (
                verified_archives[name][0],
                output.parent / f"{name}.tar",
                verified_archives[name][1],
                verified_archives[name][2],
                f"offline Docker archive for {name}",
            )
            for name in _IMAGES
        )
        copy_specs.extend(
            (
                source_path,
                output.parent / source_path.name,
                expected_hash,
                expected_size,
                f"conditional evidence {source_path.name}",
            )
            for source_path, expected_hash, expected_size in conditional_copies
        )
        for source_path, destination, expected_hash, expected_size, context in copy_specs:
            assert expected_hash is not None
            copy_temporary = _stage_private_copy(
                source_path,
                destination,
                expected_sha256=expected_hash,
                expected_size=expected_size,
                context=context,
            )
            if copy_temporary is not None:
                staged.append((copy_temporary, destination))
        manifest_temporary = _write_private_temporary(output, payload)
        staged.append((manifest_temporary, output))
        signature_temporary = _stage_manifest_signature(
            manifest_temporary,
            output.parent / "manifest.sig",
            signing_private_key,
        )
        staged.append((signature_temporary, output.parent / "manifest.sig"))
        for temporary, destination in staged:
            os.replace(temporary, destination)
            destination.chmod(0o600)
        directory_descriptor = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-report", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--migration-from", required=True)
    parser.add_argument("--migration-target", required=True)
    release_kind = parser.add_mutually_exclusive_group(required=True)
    release_kind.add_argument("--changed", action="append", choices=_IMAGES)
    release_kind.add_argument("--baseline", action="store_true")
    parser.add_argument("--data-images", type=Path)
    parser.add_argument("--backup-record", type=Path)
    parser.add_argument("--restore-report", type=Path)
    parser.add_argument("--offline-archive-dir", type=Path)
    parser.add_argument("--offline-index", type=Path)
    parser.add_argument("--signing-private-key", type=Path)
    parser.add_argument("--signing-key-id")
    parser.add_argument("--attestation-bundle", type=Path)
    parser.add_argument(
        "--allow-offline-no-conditional-evidence",
        action="store_true",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        create_manifest(
            release_report=args.release_report,
            output=args.output,
            release_id=args.release_id,
            migration_from=args.migration_from,
            migration_target=args.migration_target,
            changed=frozenset(args.changed or ()),
            baseline=args.baseline,
            data_images=args.data_images,
            backup_record=args.backup_record,
            restore_report=args.restore_report,
            offline_archive_dir=args.offline_archive_dir,
            offline_index=args.offline_index,
            signing_private_key=args.signing_private_key,
            signing_key_id=args.signing_key_id,
            attestation_bundle=args.attestation_bundle,
            allow_offline_no_conditional_evidence=(
                args.allow_offline_no_conditional_evidence
            ),
        )
    except ManifestCreationError as exc:
        print(f"release-manifest: {exc}", file=sys.stderr)
        return 1
    print(f"release-manifest: created {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
