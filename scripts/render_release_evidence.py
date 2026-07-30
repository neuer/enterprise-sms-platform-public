"""为四镜像发布门禁生成严格、原子的机器可读成功证据。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from release_metadata import (
    ReleaseMetadata,
    ReleaseMetadataError,
    collect_release_metadata,
)


class ReleaseEvidenceError(ValueError):
    """发布门禁证据不满足安全契约。"""


_RELEASE_IMAGES = ("api", "web", "postgres", "redis")
_DATA_IMAGES = ("postgres", "redis")
_CONTROL_SMOKE_PURPOSE = "release_control_failure_injection"
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
_SAFE_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,510}[A-Za-z0-9]")
_REPO_DIGEST_RE = re.compile(r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}")
_PLATFORM_RE = re.compile(r"linux/[a-z0-9_]+")
_POSTGRES_VERSION_RE = re.compile(
    r"postgres \(PostgreSQL\) (?P<version>[1-9][0-9]*(?:\.[0-9]+){1,2})"
)
_REDIS_VERSION_RE = re.compile(
    r"Redis server v=(?P<version>[1-9][0-9]*\.[0-9]+\.[0-9]+) "
    r"sha=[0-9a-f]{8,64}:[0-9]+ "
    r"malloc=[A-Za-z0-9][A-Za-z0-9._+-]{0,127} "
    r"bits=(?:32|64) build=[0-9a-f]{8,64}"
)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_MAX_SCAN_REPORT_BYTES = 64 * 1024 * 1024
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_SCHEMA_RE = re.compile(r"[0-9]{4}_[a-z0-9_]+")
_REPOSITORY_RE = re.compile(r"(?:local|[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)")
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


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _require_string(value: object, pattern: re.Pattern[str], context: str) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise ReleaseEvidenceError(f"invalid {context}")
    return value


def _validate_ref(value: object, context: str) -> str:
    ref = _require_string(value, _SAFE_REF_RE, context)
    if ".." in ref or "//" in ref:
        raise ReleaseEvidenceError(f"invalid {context}")
    return ref


def _require_exact_images(
    images: Mapping[str, Mapping[str, str]],
    expected: Sequence[str],
) -> None:
    if set(images) != set(expected):
        raise ReleaseEvidenceError("evidence must contain the exact required images")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseEvidenceError("Trivy scan contains duplicate fields")
        result[key] = value
    return result


def _parse_version_output(
    value: object,
    pattern: re.Pattern[str],
    context: str,
) -> tuple[str, int]:
    if type(value) is not str:
        raise ReleaseEvidenceError(f"invalid {context} version output")
    output = value
    if output.endswith("\n"):
        output = output[:-1]
    if not output or "\n" in output or "\r" in output:
        raise ReleaseEvidenceError(f"invalid {context} version output")
    match = pattern.fullmatch(output)
    if match is None:
        raise ReleaseEvidenceError(f"invalid {context} version output")
    version = match.group("version")
    return version, int(version.split(".", 1)[0])


def parse_postgres_version_output(value: object) -> tuple[str, int]:
    """严格解析候选或运行中 PostgreSQL 官方二进制版本输出。"""

    return _parse_version_output(value, _POSTGRES_VERSION_RE, "PostgreSQL")


def parse_redis_version_output(value: object) -> tuple[str, int]:
    """严格解析候选或运行中 Redis 官方二进制版本输出。"""

    return _parse_version_output(value, _REDIS_VERSION_RE, "Redis")


def _validate_output_path(output: Path) -> None:
    if not output.is_absolute():
        raise ReleaseEvidenceError("evidence output path must be absolute")
    try:
        parent_info = output.parent.lstat()
    except FileNotFoundError as exc:
        raise ReleaseEvidenceError("evidence output parent is missing") from exc
    if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
        raise ReleaseEvidenceError("evidence output parent is unsafe")
    try:
        output_info = output.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(output_info.st_mode) or not stat.S_ISREG(output_info.st_mode):
        raise ReleaseEvidenceError("existing evidence output is unsafe")
    if output_info.st_uid != os.geteuid():
        raise ReleaseEvidenceError("existing evidence output owner is invalid")


def _write_json_atomic(output: Path, payload: Mapping[str, object]) -> None:
    _validate_output_path(output)
    rendered = (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()
    temporary = output.parent / f".{output.name}.{uuid.uuid4().hex}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        view = memoryview(rendered)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("atomic evidence write made no progress")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, output)
        info = output.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise ReleaseEvidenceError("evidence output mode is invalid")
        directory_descriptor = os.open(output.parent, os.O_RDONLY | _DIRECTORY | _NOFOLLOW)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with suppress(FileNotFoundError):
            temporary.unlink()


def _read_regular_bytes(path: Path, context: str) -> bytes:
    if not path.is_absolute():
        raise ReleaseEvidenceError(f"{context} path must be absolute")
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | _NOFOLLOW)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ReleaseEvidenceError(f"{context} is unsafe")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_SCAN_REPORT_BYTES:
                raise ReleaseEvidenceError(f"{context} is too large")
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError as exc:
        raise ReleaseEvidenceError(f"{context} is unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _validated_trivy_scan(
    path: Path,
    *,
    name: str,
    image_ref: str,
    image_id: str,
) -> str:
    payload = _read_regular_bytes(path, f"Trivy scan for {name}")
    try:
        report = json.loads(payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeError, json.JSONDecodeError, ReleaseEvidenceError) as exc:
        raise ReleaseEvidenceError(f"Trivy scan for {name} is invalid") from exc
    if type(report) is not dict:
        raise ReleaseEvidenceError(f"Trivy scan for {name} is invalid")
    metadata = report.get("Metadata")
    results = report.get("Results")
    artifact_name = report.get("ArtifactName")
    expected_archive = f"/scan/{name}.tar"
    if (
        report.get("SchemaVersion") != 2
        or artifact_name not in {image_ref, expected_archive}
        or report.get("ArtifactType") != "container_image"
        or type(metadata) is not dict
        or metadata.get("ImageID") != image_id
        or type(results) is not list
        or not results
    ):
        raise ReleaseEvidenceError(f"Trivy scan for {name} is not bound to the image")
    for result in results:
        if (
            type(result) is not dict
            or result.get("Class") not in {"os-pkgs", "lang-pkgs"}
            or type(result.get("Target")) is not str
            or not result["Target"]
        ):
            raise ReleaseEvidenceError(f"Trivy scan for {name} is invalid")
        vulnerabilities = result.get("Vulnerabilities")
        if vulnerabilities not in (None, []):
            raise ReleaseEvidenceError(f"Trivy scan for {name} contains findings")
    return hashlib.sha256(payload).hexdigest()


def _validated_promotion_source(
    path: Path,
    *,
    candidate: str,
    images: Mapping[str, Mapping[str, str]],
) -> tuple[dict[str, object], str, dict[str, object]]:
    payload = _read_regular_bytes(path, "promotion source report")
    try:
        report = json.loads(payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeError, json.JSONDecodeError, ReleaseEvidenceError) as exc:
        raise ReleaseEvidenceError("promotion source report is invalid") from exc
    expected_report_fields = {
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
    if (
        type(report) is not dict
        or set(report) != expected_report_fields
        or report.get("schema_version") != 1
        or report.get("gate_type") != "release"
        or report.get("candidate_commit") != candidate
        or report.get("promotion_source") is not None
        or report.get("passed") is not True
    ):
        raise ReleaseEvidenceError("promotion source report is not a bound candidate build")
    source_metadata = _validated_source(report.get("source"), candidate=candidate)
    source_images = report.get("images")
    if type(source_images) is not dict or set(source_images) != set(_RELEASE_IMAGES):
        raise ReleaseEvidenceError("promotion source report has invalid images")
    rendered_images: dict[str, dict[str, object]] = {}
    expected_image_fields = {
        "ref",
        "image_id",
        "repo_digests",
        "scan_report_sha256",
        "scan_passed",
    }
    for name in _RELEASE_IMAGES:
        source_image = source_images[name]
        expected_ref = f"sms-platform-release-{name}:{candidate}"
        if (
            type(source_image) is not dict
            or set(source_image) != expected_image_fields
            or source_image.get("ref") != expected_ref
            or source_image.get("image_id") != images[name]["image_id"]
            or source_image.get("scan_passed") is not True
            or type(source_image.get("scan_report_sha256")) is not str
            or re.fullmatch(r"[0-9a-f]{64}", source_image["scan_report_sha256"]) is None
        ):
            raise ReleaseEvidenceError("promotion source image ID does not match candidate build")
        rendered_images[name] = {
            "ref": expected_ref,
            "image_id": source_image["image_id"],
            "scan_report_sha256": source_image["scan_report_sha256"],
        }
    scanner = _validate_ref(report["trivy_image"], "Trivy image")
    if _REPO_DIGEST_RE.fullmatch(scanner) is None:
        raise ReleaseEvidenceError("promotion source Trivy image must be digest pinned")
    return (
        {
            "report_sha256": hashlib.sha256(payload).hexdigest(),
            "candidate_commit": candidate,
            "source": source_metadata,
            "images": rendered_images,
        },
        scanner,
        source_metadata,
    )


def _validated_source(value: object, *, candidate: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != _SOURCE_FIELDS:
        raise ReleaseEvidenceError("release source metadata is invalid")
    source = value
    sboms = source.get("sbom_sha256")
    if (
        source.get("git_sha") != candidate
        or type(source.get("app_version")) is not str
        or _VERSION_RE.fullmatch(source["app_version"]) is None
        or type(source.get("schema_revision")) is not str
        or _SCHEMA_RE.fullmatch(source["schema_revision"]) is None
        or type(source.get("openapi_sha256")) is not str
        or _SHA256_RE.fullmatch(source["openapi_sha256"]) is None
        or type(source.get("workflow_repository")) is not str
        or _REPOSITORY_RE.fullmatch(source["workflow_repository"]) is None
        or type(source.get("workflow_run_id")) is not int
        or type(source.get("workflow_run_attempt")) is not int
        or source["workflow_run_id"] < 0
        or source["workflow_run_attempt"] < 0
        or (
            source["workflow_repository"] != "local"
            and (
                source["workflow_run_id"] < 1
                or source["workflow_run_attempt"] < 1
            )
        )
        or type(sboms) is not dict
        or set(sboms) != set(_RELEASE_IMAGES)
        or any(
            type(digest) is not str or _SHA256_RE.fullmatch(digest) is None
            for digest in sboms.values()
        )
    ):
        raise ReleaseEvidenceError("release source metadata is invalid")
    return cast(dict[str, object], source)


def write_release_evidence(
    output: Path,
    *,
    commit: str,
    trivy_image: str,
    images: Mapping[str, Mapping[str, str]],
    scan_reports: Mapping[str, Path],
    metadata: ReleaseMetadata,
    promotion_source: Path | None = None,
) -> None:
    """仅为已通过的四镜像扫描写入成功证据。"""

    candidate = _require_string(commit, _COMMIT_RE, "candidate commit")
    source = _validated_source(metadata.as_json(), candidate=candidate)
    scanner = _validate_ref(trivy_image, "Trivy image")
    if _REPO_DIGEST_RE.fullmatch(scanner) is None:
        raise ReleaseEvidenceError("Trivy image must be digest pinned")
    _require_exact_images(images, _RELEASE_IMAGES)
    if set(scan_reports) != set(_RELEASE_IMAGES):
        raise ReleaseEvidenceError("release evidence requires exact Trivy scan reports")
    rendered_images: dict[str, dict[str, object]] = {}
    for name in _RELEASE_IMAGES:
        image = images[name]
        if set(image) != {"ref", "image_id", "repo_digests"}:
            raise ReleaseEvidenceError(f"invalid release image fields for {name}")
        digests_value = image["repo_digests"]
        if type(digests_value) is not str:
            raise ReleaseEvidenceError(f"invalid RepoDigests for {name}")
        repo_digests = [] if not digests_value else digests_value.split(",")
        if any(_REPO_DIGEST_RE.fullmatch(digest) is None for digest in repo_digests):
            raise ReleaseEvidenceError(f"invalid RepoDigests for {name}")
        image_ref = _validate_ref(image["ref"], f"{name} image ref")
        image_id = _require_string(
            image["image_id"],
            _IMAGE_ID_RE,
            f"{name} image ID",
        )
        rendered_images[name] = {
            "ref": image_ref,
            "image_id": image_id,
            "repo_digests": repo_digests,
            "scan_report_sha256": _validated_trivy_scan(
                scan_reports[name],
                name=name,
                image_ref=image_ref,
                image_id=image_id,
            ),
            "scan_passed": True,
        }
    rendered_promotion_source: dict[str, object] | None = None
    if promotion_source is not None:
        if any(_REPO_DIGEST_RE.fullmatch(images[name]["ref"]) is None for name in _RELEASE_IMAGES):
            raise ReleaseEvidenceError("promoted images must all use RepoDigest refs")
        if any(
            images[name]["ref"] not in images[name]["repo_digests"].split(",")
            for name in _RELEASE_IMAGES
        ):
            raise ReleaseEvidenceError("promoted image ref is absent from RepoDigests")
        rendered_promotion_source, _, promoted_source = _validated_promotion_source(
            promotion_source,
            candidate=candidate,
            images=images,
        )
        if promoted_source != source:
            raise ReleaseEvidenceError("promotion source metadata does not match candidate")
    _write_json_atomic(
        output,
        {
            "schema_version": 1,
            "gate_type": "release",
            "candidate_commit": candidate,
            "source": source,
            "generated_at": _utc_now(),
            "trivy_image": scanner,
            "images": rendered_images,
            "promotion_source": rendered_promotion_source,
            "passed": True,
        },
    )


def write_promoted_release_evidence(
    output: Path,
    *,
    commit: str,
    images: Mapping[str, Mapping[str, str]],
    promotion_source: Path,
) -> None:
    """按不可变 image ID 复用候选扫描，不对同一镜像重复执行 Trivy。"""

    candidate = _require_string(commit, _COMMIT_RE, "candidate commit")
    _require_exact_images(images, _RELEASE_IMAGES)
    rendered_images: dict[str, dict[str, object]] = {}
    for name in _RELEASE_IMAGES:
        image = images[name]
        if set(image) != {"ref", "image_id", "repo_digests"}:
            raise ReleaseEvidenceError(f"invalid release image fields for {name}")
        image_ref = _validate_ref(image["ref"], f"{name} image ref")
        image_id = _require_string(
            image["image_id"],
            _IMAGE_ID_RE,
            f"{name} image ID",
        )
        repo_digests = image["repo_digests"].split(",")
        if (
            _REPO_DIGEST_RE.fullmatch(image_ref) is None
            or any(_REPO_DIGEST_RE.fullmatch(digest) is None for digest in repo_digests)
            or image_ref not in repo_digests
        ):
            raise ReleaseEvidenceError("promoted image ref is absent from RepoDigests")
        rendered_images[name] = {
            "ref": image_ref,
            "image_id": image_id,
            "repo_digests": repo_digests,
        }

    promotion, scanner, source = _validated_promotion_source(
        promotion_source,
        candidate=candidate,
        images=images,
    )
    source_images = cast(dict[str, dict[str, str]], promotion["images"])
    for name in _RELEASE_IMAGES:
        rendered_images[name]["scan_report_sha256"] = source_images[name][
            "scan_report_sha256"
        ]
        rendered_images[name]["scan_passed"] = True

    _write_json_atomic(
        output,
        {
            "schema_version": 1,
            "gate_type": "release",
            "candidate_commit": candidate,
            "source": source,
            "generated_at": _utc_now(),
            "trivy_image": scanner,
            "images": rendered_images,
            "promotion_source": promotion,
            "passed": True,
        },
    )


def write_data_image_evidence(
    output: Path,
    *,
    commit: str,
    images: Mapping[str, Mapping[str, str]],
) -> None:
    """仅为已通过角色与重启持久化检查的数据镜像写入成功证据。"""

    candidate = _require_string(commit, _COMMIT_RE, "candidate commit")
    _require_exact_images(images, _DATA_IMAGES)
    rendered_images: dict[str, object] = {}
    for name in _DATA_IMAGES:
        image = images[name]
        if set(image) != {"ref", "image_id", "platform", "version_output"}:
            raise ReleaseEvidenceError(f"invalid data image fields for {name}")
        if name == "postgres":
            version, major = parse_postgres_version_output(image["version_output"])
        else:
            version, major = parse_redis_version_output(image["version_output"])
        rendered_images[name] = {
            "ref": _validate_ref(image["ref"], f"{name} image ref"),
            "image_id": _require_string(
                image["image_id"],
                _IMAGE_ID_RE,
                f"{name} image ID",
            ),
            "platform": _require_string(
                image["platform"],
                _PLATFORM_RE,
                f"{name} platform",
            ),
            "version": version,
            "major": major,
        }
    _write_json_atomic(
        output,
        {
            "schema_version": 1,
            "gate_type": "data_images",
            "candidate_commit": candidate,
            "generated_at": _utc_now(),
            "images": rendered_images,
            "checks": {
                "postgres_role_constraints": True,
                "postgres_restart_persistence": True,
                "redis_aof_restart_persistence": True,
            },
            "passed": True,
        },
    )


def write_release_control_smoke_evidence(
    output: Path,
    *,
    commit: str,
    images: Mapping[str, Mapping[str, str]],
) -> None:
    """仅为 development control smoke 写入控制面证据，不伪装发布扫描。"""

    candidate = _require_string(commit, _COMMIT_RE, "candidate commit")
    _require_exact_images(images, _RELEASE_IMAGES)
    rendered_images: dict[str, object] = {}
    for name in _RELEASE_IMAGES:
        image = images[name]
        if set(image) != {"ref", "image_id", "platform"}:
            raise ReleaseEvidenceError(f"invalid control smoke image fields for {name}")
        platform = _require_string(
            image["platform"],
            _PLATFORM_RE,
            f"{name} control smoke platform",
        )
        if platform != "linux/amd64":
            raise ReleaseEvidenceError("control smoke images must all be linux/amd64")
        rendered_images[name] = {
            "ref": _validate_ref(image["ref"], f"{name} control smoke ref"),
            "image_id": _require_string(
                image["image_id"],
                _IMAGE_ID_RE,
                f"{name} control smoke image ID",
            ),
            "platform": platform,
        }
    _write_json_atomic(
        output,
        {
            "schema_version": 1,
            "gate_type": "release_control_smoke",
            "candidate_commit": candidate,
            "generated_at": _utc_now(),
            "purpose": _CONTROL_SMOKE_PURPOSE,
            "scan_performed": False,
            "authorized_for_control_smoke": True,
            "images": rendered_images,
        },
    )


def _image_rows(rows: list[list[str]], *, data_images: bool) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for row in rows:
        name = row[0]
        if name in result:
            raise ReleaseEvidenceError(f"duplicate image argument: {name}")
        if data_images:
            _, ref, image_id, platform, version_output = row
            result[name] = {
                "ref": ref,
                "image_id": image_id,
                "platform": platform,
                "version_output": version_output,
            }
        else:
            _, ref, image_id, repo_digests, *_ = row
            result[name] = {
                "ref": ref,
                "image_id": image_id,
                "repo_digests": repo_digests,
            }
    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成发布门禁成功证据")
    subparsers = parser.add_subparsers(dest="gate", required=True)
    release = subparsers.add_parser("release")
    release.add_argument("--output", required=True, type=Path)
    release.add_argument("--commit", required=True)
    release.add_argument("--trivy-image", required=True)
    release.add_argument("--root", required=True, type=Path)
    release.add_argument("--workflow-repository", default="local")
    release.add_argument("--workflow-run-id", default=0, type=int)
    release.add_argument("--workflow-run-attempt", default=0, type=int)
    release.add_argument("--sbom", action="append", nargs=2, required=True)
    release.add_argument("--promotion-source", type=Path)
    release.add_argument("--image", action="append", nargs=5, required=True)
    promote = subparsers.add_parser("promote")
    promote.add_argument("--output", required=True, type=Path)
    promote.add_argument("--commit", required=True)
    promote.add_argument("--promotion-source", required=True, type=Path)
    promote.add_argument("--image", action="append", nargs=4, required=True)
    control_smoke = subparsers.add_parser("release-control-smoke")
    control_smoke.add_argument("--output", required=True, type=Path)
    control_smoke.add_argument("--commit", required=True)
    control_smoke.add_argument("--image", action="append", nargs=4, required=True)
    data = subparsers.add_parser("data-images")
    data.add_argument("--output", required=True, type=Path)
    data.add_argument("--commit", required=True)
    data.add_argument("--image", action="append", nargs=5, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.gate == "release":
            if (
                len(arguments.sbom) != len(_RELEASE_IMAGES)
                or {row[0] for row in arguments.sbom} != set(_RELEASE_IMAGES)
            ):
                raise ReleaseMetadataError(
                    "exactly one SBOM for each release image is required"
                )
            metadata = collect_release_metadata(
                arguments.root,
                commit=arguments.commit,
                workflow_repository=arguments.workflow_repository,
                workflow_run_id=arguments.workflow_run_id,
                workflow_run_attempt=arguments.workflow_run_attempt,
                sboms={row[0]: Path(row[1]) for row in arguments.sbom},
            )
            write_release_evidence(
                arguments.output,
                commit=arguments.commit,
                trivy_image=arguments.trivy_image,
                images=_image_rows(arguments.image, data_images=False),
                scan_reports={row[0]: Path(row[4]) for row in arguments.image},
                metadata=metadata,
                promotion_source=arguments.promotion_source,
            )
        elif arguments.gate == "promote":
            write_promoted_release_evidence(
                arguments.output,
                commit=arguments.commit,
                images={
                    row[0]: {
                        "ref": row[1],
                        "image_id": row[2],
                        "repo_digests": row[3],
                    }
                    for row in arguments.image
                },
                promotion_source=arguments.promotion_source,
            )
        elif arguments.gate == "release-control-smoke":
            write_release_control_smoke_evidence(
                arguments.output,
                commit=arguments.commit,
                images={
                    row[0]: {"ref": row[1], "image_id": row[2], "platform": row[3]}
                    for row in arguments.image
                },
            )
        else:
            write_data_image_evidence(
                arguments.output,
                commit=arguments.commit,
                images=_image_rows(arguments.image, data_images=True),
            )
    except (ReleaseEvidenceError, ReleaseMetadataError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
