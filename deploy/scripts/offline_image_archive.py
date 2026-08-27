"""临时生产离线镜像包的索引与文件摘要契约。

镜像归档来自固定 Release Gate workflow，并由 GitHub attestation、离线索引、
Ed25519 manifest 和 SHA-256 共同绑定。Docker 是 archive 格式的权威解析器；本模块
不重复实现 Docker/OCI tar 解析，只在导入前校验文件边界与摘要。导入后由 release
manager 逐镜像读回 image ID、平台和 OCI labels。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast


class OfflineImageArchiveError(ValueError):
    """离线镜像文件或索引不满足生产交付契约。"""


@dataclass(frozen=True)
class OfflineImageArchive:
    path: Path
    size: int
    sha256: str


@dataclass(frozen=True)
class EvidenceArtifact:
    file: str
    sha256: str
    size: int


@dataclass(frozen=True)
class OfflineIndexImage:
    image_id: str
    archive: EvidenceArtifact
    scan: EvidenceArtifact
    sbom_candidate: EvidenceArtifact
    sbom_rebuild: EvidenceArtifact | None


@dataclass(frozen=True)
class OfflineImageIndex:
    schema_version: int
    candidate_commit: str
    verification_mode: str
    release_gate: EvidenceArtifact
    reproducibility: EvidenceArtifact | None
    images: Mapping[str, OfflineIndexImage]


_IMAGE_NAMES = frozenset({"api", "web", "postgres", "redis"})
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_MAX_ARCHIVE_BYTES = 8 * 1024 * 1024 * 1024
_MAX_METADATA_BYTES = 16 * 1024 * 1024
_MAX_SCAN_OR_SBOM_BYTES = 64 * 1024 * 1024
_INDEX_V1_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "candidate_commit",
        "release_gate",
        "reproducibility",
        "images",
    }
)
_INDEX_V2_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "candidate_commit",
        "verification",
        "release_gate",
        "images",
    }
)
_INDEX_IMAGE_FIELDS = frozenset({"image_id", "archive", "scan", "sbom"})
_INDEX_ARTIFACT_FIELDS = frozenset({"file", "sha256", "size"})
_INDEX_V1_SBOM_FIELDS = frozenset({"candidate", "rebuild"})
_INDEX_V2_SBOM_FIELDS = frozenset({"candidate"})
_INDEX_VERIFICATION_FIELDS = frozenset({"mode", "reproducibility_proven"})
_SINGLE_BUILD_MODE = "single_build_temporary_exception"


def candidate_image_ref(name: str, commit: str) -> str:
    """构造 Release Gate 绑定的固定候选镜像标签。"""

    if name not in _IMAGE_NAMES:
        raise OfflineImageArchiveError("image name is invalid")
    if type(commit) is not str or _COMMIT_RE.fullmatch(commit) is None:
        raise OfflineImageArchiveError("image commit is invalid")
    return f"sms-platform-release-{name}:{commit}"


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise OfflineImageArchiveError(f"duplicate JSON key in offline index: {key}")
        value[key] = item
    return value


def _decode_json(payload: bytes, context: str) -> object:
    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except OfflineImageArchiveError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise OfflineImageArchiveError(f"{context} is invalid JSON") from exc


def _require_mapping(value: object, context: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise OfflineImageArchiveError(f"{context} must be an object")
    return cast(dict[str, Any], value)


def _parse_index_artifact(
    value: object,
    *,
    expected_file: str,
    context: str,
    maximum_size: int,
) -> EvidenceArtifact:
    artifact = _require_mapping(value, context)
    if set(artifact) != _INDEX_ARTIFACT_FIELDS:
        raise OfflineImageArchiveError(f"{context} fields are invalid")
    sha256 = artifact.get("sha256")
    size = artifact.get("size")
    if (
        artifact.get("file") != expected_file
        or type(sha256) is not str
        or _SHA256_RE.fullmatch(sha256) is None
        or type(size) is not int
        or not 1 <= size <= maximum_size
    ):
        raise OfflineImageArchiveError(f"{context} is invalid")
    return EvidenceArtifact(file=expected_file, sha256=sha256, size=size)


def load_offline_image_index_bytes(payload: bytes) -> OfflineImageIndex:
    """严格解析 CI 生成并 attestation 的离线镜像索引。"""

    document = _require_mapping(
        _decode_json(payload, "offline image index"),
        "offline image index",
    )
    schema_version = document.get("schema_version")
    if (
        type(schema_version) is not int
        or schema_version not in {1, 2}
        or document.get("kind") != "production_offline_image_index"
    ):
        raise OfflineImageArchiveError("offline image index header is invalid")
    if schema_version == 1:
        if set(document) != _INDEX_V1_FIELDS:
            raise OfflineImageArchiveError("offline image index header is invalid")
        verification_mode = "independent_rebuild"
        reproducibility = _parse_index_artifact(
            document.get("reproducibility"),
            expected_file="reproducibility.json",
            context="offline image index reproducibility evidence",
            maximum_size=_MAX_METADATA_BYTES,
        )
        expected_sbom_fields = _INDEX_V1_SBOM_FIELDS
    else:
        if set(document) != _INDEX_V2_FIELDS:
            raise OfflineImageArchiveError("offline image index header is invalid")
        verification = _require_mapping(
            document.get("verification"),
            "offline image index verification",
        )
        if (
            set(verification) != _INDEX_VERIFICATION_FIELDS
            or verification.get("mode") != _SINGLE_BUILD_MODE
            or verification.get("reproducibility_proven") is not False
        ):
            raise OfflineImageArchiveError("offline image index verification is invalid")
        verification_mode = _SINGLE_BUILD_MODE
        reproducibility = None
        expected_sbom_fields = _INDEX_V2_SBOM_FIELDS
    commit = document.get("candidate_commit")
    if type(commit) is not str or _COMMIT_RE.fullmatch(commit) is None:
        raise OfflineImageArchiveError("offline image index commit is invalid")
    release_gate = _parse_index_artifact(
        document.get("release_gate"),
        expected_file="release-gate.json",
        context="offline image index release gate",
        maximum_size=_MAX_METADATA_BYTES,
    )
    images_value = _require_mapping(document.get("images"), "offline image index images")
    if set(images_value) != _IMAGE_NAMES:
        raise OfflineImageArchiveError("offline image index must contain exactly four images")
    images: dict[str, OfflineIndexImage] = {}
    for name in ("api", "web", "postgres", "redis"):
        image = _require_mapping(images_value[name], f"offline image index images.{name}")
        image_id = image.get("image_id")
        if (
            set(image) != _INDEX_IMAGE_FIELDS
            or type(image_id) is not str
            or _IMAGE_ID_RE.fullmatch(image_id) is None
        ):
            raise OfflineImageArchiveError(f"offline image index images.{name} is invalid")
        sbom = _require_mapping(image.get("sbom"), f"offline image index images.{name}.sbom")
        if set(sbom) != expected_sbom_fields:
            raise OfflineImageArchiveError(
                f"offline image index images.{name}.sbom fields are invalid"
            )
        images[name] = OfflineIndexImage(
            image_id=image_id,
            archive=_parse_index_artifact(
                image["archive"],
                expected_file=f"images/{name}.tar",
                context=f"offline image index images.{name}.archive",
                maximum_size=_MAX_ARCHIVE_BYTES,
            ),
            scan=_parse_index_artifact(
                image["scan"],
                expected_file=f"scans/{name}.json",
                context=f"offline image index images.{name}.scan",
                maximum_size=_MAX_SCAN_OR_SBOM_BYTES,
            ),
            sbom_candidate=_parse_index_artifact(
                sbom["candidate"],
                expected_file=f"sboms/{name}.cdx.json",
                context=f"offline image index images.{name}.sbom.candidate",
                maximum_size=_MAX_SCAN_OR_SBOM_BYTES,
            ),
            sbom_rebuild=(
                _parse_index_artifact(
                    sbom["rebuild"],
                    expected_file=f"sboms/{name}.rebuild.cdx.json",
                    context=f"offline image index images.{name}.sbom.rebuild",
                    maximum_size=_MAX_SCAN_OR_SBOM_BYTES,
                )
                if schema_version == 1
                else None
            ),
        )
    return OfflineImageIndex(
        schema_version=schema_version,
        candidate_commit=commit,
        verification_mode=verification_mode,
        release_gate=release_gate,
        reproducibility=reproducibility,
        images=MappingProxyType(images),
    )


def _archive_sha256(descriptor: int, expected_size: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while offset < expected_size:
        block = os.pread(descriptor, min(1024 * 1024, expected_size - offset), offset)
        if not block:
            break
        offset += len(block)
        digest.update(block)
    if offset != expected_size or os.pread(descriptor, 1, offset):
        raise OfflineImageArchiveError("Docker archive changed while hashing")
    return digest.hexdigest()


def _same_file(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_uid,
        before.st_nlink,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) == (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_uid,
        after.st_nlink,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )


def validate_offline_image_archive(
    path: Path,
    *,
    name: str,
    expected_sha256: str | None = None,
    expected_size: int | None = None,
) -> OfflineImageArchive:
    """校验固定归档的文件边界与摘要；内部格式交由 Docker 解析。"""

    if name not in _IMAGE_NAMES:
        raise OfflineImageArchiveError("image name is invalid")
    if not path.is_absolute() or path.name != f"{name}.tar":
        raise OfflineImageArchiveError("Docker archive must use its fixed absolute path")
    if expected_sha256 is not None and (
        type(expected_sha256) is not str or _SHA256_RE.fullmatch(expected_sha256) is None
    ):
        raise OfflineImageArchiveError("declared archive hash is invalid")
    if expected_size is not None and (
        type(expected_size) is not int or not 1 <= expected_size <= _MAX_ARCHIVE_BYTES
    ):
        raise OfflineImageArchiveError("declared archive size is invalid")

    descriptor = -1
    try:
        before_path = path.lstat()
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        before = os.fstat(descriptor)
        if (
            not _same_file(before_path, before)
            or not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) not in {0o600, 0o644}
            or not 1 <= before.st_size <= _MAX_ARCHIVE_BYTES
        ):
            raise OfflineImageArchiveError(
                "Docker archive must be an owned single-link regular file"
            )
        if expected_size is not None and before.st_size != expected_size:
            raise OfflineImageArchiveError("Docker archive size does not match its declaration")
        digest = _archive_sha256(descriptor, before.st_size)
        if expected_sha256 is not None and not hmac.compare_digest(digest, expected_sha256):
            raise OfflineImageArchiveError("Docker archive hash does not match its declaration")
        after = os.fstat(descriptor)
        after_path = path.lstat()
        if not _same_file(before, after) or not _same_file(before, after_path):
            raise OfflineImageArchiveError("Docker archive changed during validation")
    except OfflineImageArchiveError:
        raise
    except OSError as exc:
        raise OfflineImageArchiveError("Docker archive is unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    return OfflineImageArchive(
        path=path,
        size=before.st_size,
        sha256=digest,
    )
