#!/usr/bin/env python3
"""为临时生产离线包生成可 attestation 的固定摘要索引。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))

from offline_image_archive import (  # noqa: E402
    OfflineImageArchiveError,
    candidate_image_ref,
    load_offline_image_index_bytes,
    validate_offline_image_archive,
)


class OfflineImageIndexError(ValueError):
    """Release Gate 输出不能形成可信的固定离线索引。"""


@dataclass(frozen=True)
class Artifact:
    path: Path
    relative: str
    sha256: str
    size: int
    payload: bytes | None = None


IMAGES = ("api", "web", "postgres", "redis")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_SCHEMA_RE = re.compile(r"[0-9]{4}_[a-z0-9_]+")
_REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_MAX_JSON_BYTES = 16 * 1024 * 1024
_MAX_SCAN_OR_SBOM_BYTES = 64 * 1024 * 1024
_MAX_ARCHIVE_BYTES = 8 * 1024 * 1024 * 1024
_RELEASE_FIELDS = {
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
_IMAGE_FIELDS = {
    "ref",
    "image_id",
    "repo_digests",
    "scan_report_sha256",
    "scan_passed",
}


def _identity(metadata: os.stat_result) -> tuple[int, ...]:
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


def _require_directory(path: Path, context: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise OfflineImageIndexError(f"{context} is unavailable") from exc
    if (
        not path.is_absolute()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise OfflineImageIndexError(f"{context} must be an owned 0700 directory")


def _require_closed(path: Path, expected: set[str], context: str) -> None:
    try:
        names = {entry.name for entry in os.scandir(path)}
    except OSError as exc:
        raise OfflineImageIndexError(f"{context} is unavailable") from exc
    if names != expected:
        raise OfflineImageIndexError(f"{context} is not a closed file set")


def _read_artifact(
    path: Path,
    *,
    relative: str,
    maximum_size: int,
    context: str,
    keep_payload: bool = False,
) -> Artifact:
    descriptor = -1
    try:
        before_path = path.lstat()
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        before = os.fstat(descriptor)
        if (
            _identity(before_path) != _identity(before)
            or not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or not 1 <= before.st_size <= maximum_size
        ):
            raise OfflineImageIndexError(f"{context} is not a private single-link regular file")
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        size = 0
        while block := os.read(descriptor, 1024 * 1024):
            size += len(block)
            if size > maximum_size:
                raise OfflineImageIndexError(f"{context} is too large")
            digest.update(block)
            if keep_payload:
                chunks.append(block)
        after = os.fstat(descriptor)
        after_path = path.lstat()
        if (
            size != before.st_size
            or _identity(before) != _identity(after)
            or _identity(before) != _identity(after_path)
        ):
            raise OfflineImageIndexError(f"{context} changed while reading")
        return Artifact(
            path=path,
            relative=relative,
            sha256=digest.hexdigest(),
            size=size,
            payload=b"".join(chunks) if keep_payload else None,
        )
    except OfflineImageIndexError:
        raise
    except OSError as exc:
        raise OfflineImageIndexError(f"{context} is unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise OfflineImageIndexError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _decode_json(artifact: Artifact, context: str) -> dict[str, Any]:
    assert artifact.payload is not None
    try:
        value = json.loads(
            artifact.payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except OfflineImageIndexError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise OfflineImageIndexError(f"{context} is not strict JSON") from exc
    if type(value) is not dict:
        raise OfflineImageIndexError(f"{context} must be an object")
    return cast(dict[str, Any], value)


def _sha(value: object, context: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise OfflineImageIndexError(f"{context} is invalid")
    return value


def _validate_release_gate(
    document: dict[str, Any],
    *,
    commit: str,
) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    if (
        set(document) != _RELEASE_FIELDS
        or document.get("schema_version") != 1
        or document.get("gate_type") != "release"
        or document.get("candidate_commit") != commit
        or document.get("passed") is not True
        or document.get("promotion_source") is not None
    ):
        raise OfflineImageIndexError("release gate is not an offline candidate result")
    source = document.get("source")
    if type(source) is not dict or set(source) != _SOURCE_FIELDS:
        raise OfflineImageIndexError("release gate source is invalid")
    app_version = source.get("app_version")
    schema_revision = source.get("schema_revision")
    repository = source.get("workflow_repository")
    sboms = source.get("sbom_sha256")
    if (
        source.get("git_sha") != commit
        or type(app_version) is not str
        or _VERSION_RE.fullmatch(app_version) is None
        or type(schema_revision) is not str
        or _SCHEMA_RE.fullmatch(schema_revision) is None
        or type(repository) is not str
        or _REPOSITORY_RE.fullmatch(repository) is None
        or type(source.get("workflow_run_id")) is not int
        or source["workflow_run_id"] < 1
        or type(source.get("workflow_run_attempt")) is not int
        or source["workflow_run_attempt"] < 1
        or type(sboms) is not dict
        or set(sboms) != set(IMAGES)
    ):
        raise OfflineImageIndexError("release gate source is not GitHub-bound")
    sbom_hashes = {name: _sha(sboms[name], f"candidate SBOM hash for {name}") for name in IMAGES}
    images = document.get("images")
    if type(images) is not dict or set(images) != set(IMAGES):
        raise OfflineImageIndexError("release gate image set is invalid")
    rendered: dict[str, dict[str, str]] = {}
    for name in IMAGES:
        image = images[name]
        image_id = image.get("image_id") if type(image) is dict else None
        if (
            type(image) is not dict
            or set(image) != _IMAGE_FIELDS
            or image.get("ref") != candidate_image_ref(name, commit)
            or type(image_id) is not str
            or _IMAGE_ID_RE.fullmatch(image_id) is None
            or image.get("repo_digests") != []
            or image.get("scan_passed") is not True
        ):
            raise OfflineImageIndexError(f"release gate image {name} is invalid")
        rendered[name] = {
            "image_id": image_id,
            "scan_sha256": _sha(image.get("scan_report_sha256"), f"scan hash for {name}"),
        }
    return rendered, sbom_hashes


def _record(artifact: Artifact) -> dict[str, object]:
    return {"file": artifact.relative, "sha256": artifact.sha256, "size": artifact.size}


def _write_index(output: Path, document: dict[str, object]) -> None:
    if os.path.lexists(output):
        raise OfflineImageIndexError("offline image index output already exists")
    payload = (
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    load_offline_image_index_bytes(payload)
    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OfflineImageIndexError("offline image index write made no progress")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, output)
    except OfflineImageIndexError:
        temporary.unlink(missing_ok=True)
        raise
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise OfflineImageIndexError("offline image index cannot be written") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def create_index(
    *,
    commit: str,
    release_gate_path: Path,
    archive_dir: Path,
    scan_dir: Path,
    sbom_dir: Path,
    output: Path,
) -> None:
    """绑定 Release Gate 的固定四镜像归档及审计证据。"""

    if type(commit) is not str or _COMMIT_RE.fullmatch(commit) is None:
        raise OfflineImageIndexError("candidate commit is invalid")
    parent = output.parent
    expected_paths = {
        release_gate_path: parent / "release-gate.json",
        archive_dir: parent / "images",
        scan_dir: parent / "scans",
        sbom_dir: parent / "sboms",
        output: parent / "offline-image-index.json",
    }
    if not output.is_absolute() or any(
        path != expected for path, expected in expected_paths.items()
    ):
        raise OfflineImageIndexError("offline evidence paths do not match the fixed layout")
    for directory, context in (
        (parent, "release evidence directory"),
        (archive_dir, "image archive directory"),
        (scan_dir, "Trivy scan directory"),
        (sbom_dir, "SBOM directory"),
    ):
        _require_directory(directory, context)
    parent_files = {"release-gate.json", "images", "scans", "sboms"}
    _require_closed(parent, parent_files, "release evidence directory")
    _require_closed(archive_dir, {f"{name}.tar" for name in IMAGES}, "image archive directory")
    _require_closed(scan_dir, {f"{name}.json" for name in IMAGES}, "Trivy scan directory")
    _require_closed(
        sbom_dir,
        {f"{name}.cdx.json" for name in IMAGES},
        "SBOM directory",
    )

    release_gate = _read_artifact(
        release_gate_path,
        relative="release-gate.json",
        maximum_size=_MAX_JSON_BYTES,
        context="release gate",
        keep_payload=True,
    )
    release_images, candidate_sboms = _validate_release_gate(
        _decode_json(release_gate, "release gate"),
        commit=commit,
    )
    rendered_images: dict[str, object] = {}
    for name in IMAGES:
        scan = _read_artifact(
            scan_dir / f"{name}.json",
            relative=f"scans/{name}.json",
            maximum_size=_MAX_SCAN_OR_SBOM_BYTES,
            context=f"Trivy scan for {name}",
        )
        candidate = _read_artifact(
            sbom_dir / f"{name}.cdx.json",
            relative=f"sboms/{name}.cdx.json",
            maximum_size=_MAX_SCAN_OR_SBOM_BYTES,
            context=f"candidate SBOM for {name}",
        )
        if scan.sha256 != release_images[name]["scan_sha256"]:
            raise OfflineImageIndexError(f"Trivy scan hash for {name} does not match")
        if candidate.sha256 != candidate_sboms[name]:
            raise OfflineImageIndexError(f"SBOM hash for {name} does not match")
        try:
            archive = validate_offline_image_archive(
                (archive_dir / f"{name}.tar").absolute(),
                name=name,
            )
        except OfflineImageArchiveError as exc:
            raise OfflineImageIndexError(f"image archive for {name} is invalid") from exc
        rendered_images[name] = {
            "image_id": release_images[name]["image_id"],
            "archive": {
                "file": f"images/{name}.tar",
                "sha256": archive.sha256,
                "size": archive.size,
            },
            "scan": _record(scan),
            "sbom": {"candidate": _record(candidate)},
        }

    _require_closed(parent, parent_files, "release evidence directory")
    document: dict[str, object] = {
        "schema_version": 2,
        "kind": "production_offline_image_index",
        "candidate_commit": commit,
        "release_gate": _record(release_gate),
        "images": rendered_images,
    }
    document["verification"] = {
        "mode": "single_build_temporary_exception",
        "reproducibility_proven": False,
    }
    _write_index(output, document)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--release-gate", required=True, type=Path)
    parser.add_argument("--archive-dir", required=True, type=Path)
    parser.add_argument("--scan-dir", required=True, type=Path)
    parser.add_argument("--sbom-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        create_index(
            commit=arguments.commit,
            release_gate_path=arguments.release_gate,
            archive_dir=arguments.archive_dir,
            scan_dir=arguments.scan_dir,
            sbom_dir=arguments.sbom_dir,
            output=arguments.output,
        )
    except OfflineImageIndexError as exc:
        print(f"offline image index failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
