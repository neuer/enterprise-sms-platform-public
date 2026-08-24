#!/usr/bin/env python3
"""从最终 release-gate 证据生成并自校验 production manifest。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))

from release_manifest import load_manifest_bytes  # noqa: E402

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


class ManifestCreationError(ValueError):
    """最终证据不足以自动生成安全的生产发布清单。"""


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestCreationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_private_json_with_hash(
    path: Path,
    context: str,
) -> tuple[dict[str, Any], str]:
    if not path.is_absolute():
        raise ManifestCreationError(f"{context} must use an absolute path")
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise ManifestCreationError(f"{context} is not a private regular file")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > _MAX_EVIDENCE_BYTES:
                raise ManifestCreationError(f"{context} is too large")
            chunks.append(chunk)
        payload = b"".join(chunks)
        value = json.loads(
            payload,
            object_pairs_hook=_reject_duplicates,
        )
    except ManifestCreationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManifestCreationError(f"{context} is unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if type(value) is not dict:
        raise ManifestCreationError(f"{context} is invalid")
    return value, hashlib.sha256(payload).hexdigest()


def _read_private_json(path: Path, context: str) -> dict[str, Any]:
    return _read_private_json_with_hash(path, context)[0]


def _evidence_name(path: Path | None, parent: Path, context: str) -> str | None:
    if path is None:
        return None
    _read_private_json(path, context)
    if path.parent != parent or path.name == "manifest.json":
        raise ManifestCreationError(f"{context} must be a sibling evidence file")
    return path.name


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
) -> None:
    """读取最终证据，原子生成通过现有 exact-field 契约的清单。"""

    if not changed.issubset(_IMAGES) or (not changed and not baseline):
        raise ManifestCreationError("changed images are invalid")
    if baseline and (changed or migration_from != migration_target):
        raise ManifestCreationError("baseline manifest must have no image or migration delta")
    if not output.is_absolute() or output.name != "manifest.json":
        raise ManifestCreationError("output must be an absolute manifest.json path")
    if output.parent != release_report.parent:
        raise ManifestCreationError("manifest and release report must share a directory")
    report, report_sha256 = _read_private_json_with_hash(
        release_report,
        "release report",
    )
    if (
        set(report) != _REPORT_FIELDS
        or report.get("schema_version") != 1
        or report.get("gate_type") != "release"
        or report.get("passed") is not True
        or type(report.get("promotion_source")) is not dict
        or type(report.get("candidate_commit")) is not str
        or _COMMIT_RE.fullmatch(report["candidate_commit"]) is None
    ):
        raise ManifestCreationError("release report is not final promotion evidence")
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

    images: dict[str, dict[str, object]] = {}
    for name in _IMAGES:
        image = report_images[name]
        if (
            type(image) is not dict
            or set(image) != _IMAGE_FIELDS
            or type(image.get("ref")) is not str
            or _DIGEST_REF_RE.fullmatch(image["ref"]) is None
            or type(image.get("image_id")) is not str
            or _IMAGE_ID_RE.fullmatch(image["image_id"]) is None
            or type(image.get("repo_digests")) is not list
            or image["ref"] not in image["repo_digests"]
            or image.get("scan_passed") is not True
        ):
            raise ManifestCreationError(f"release report image {name} is invalid")
        images[name] = {
            "ref": image["ref"],
            "id": image["image_id"],
            "archive_file": None,
            "archive_sha256": None,
            "changed": name in changed,
        }

    data_name = _evidence_name(data_images, output.parent, "data image evidence")
    data_changed = bool(changed & {"postgres", "redis"})
    if data_changed is (data_name is None):
        raise ManifestCreationError("data image evidence does not match changed images")
    backup_pair = (backup_record is not None, restore_report is not None)
    if backup_pair not in {(False, False), (True, True)}:
        raise ManifestCreationError("backup evidence must be provided as a pair")
    backup_required = "postgres" in changed or migration_from != migration_target
    if backup_required is not all(backup_pair):
        raise ManifestCreationError(
            "backup evidence does not match PostgreSQL or migration change"
        )
    backup_evidence = (
        None
        if not all(backup_pair)
        else {
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
    )
    manifest = {
        "schema_version": 1,
        "release_id": release_id,
        "commit": report["candidate_commit"],
        "mode": "production",
        "images": images,
        "migration": {
            "from": migration_from,
            "target": migration_target,
            "compatibility": (
                "none" if migration_from == migration_target else "expand"
            ),
        },
        "evidence": {
            "release_gate_kind": "release",
            "release_gate": release_report.name,
            "release_gate_sha256": report_sha256,
            "data_images": data_name,
            "backup_restore_change": backup_evidence,
        },
    }
    payload = (
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode()
    load_manifest_bytes(payload)
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = output.with_name(f".manifest.{uuid.uuid4().hex}.tmp")
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
        os.replace(temporary, output)
        output.chmod(0o600)
    finally:
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
        )
    except ManifestCreationError as exc:
        print(f"release-manifest: {exc}", file=sys.stderr)
        return 1
    print(f"release-manifest: created {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
