"""四镜像发布清单的严格安全契约。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, cast


class ReleaseManifestError(ValueError):
    """发布清单不满足严格安全契约。"""


class MigrationCompatibility(StrEnum):
    NONE = "none"
    EXPAND = "expand"
    MANUAL = "manual"


@dataclass(frozen=True)
class ImageSpec:
    ref: str
    image_id: str
    archive_file: str | None
    archive_sha256: str | None
    changed: bool
    archive_size: int | None = None


@dataclass(frozen=True)
class ReleaseManifest:
    schema_version: int
    release_id: str
    commit: str
    mode: Literal["development", "production"]
    images: Mapping[str, ImageSpec]
    migration_from: str
    migration_target: str
    migration_compatibility: MigrationCompatibility
    evidence: Mapping[str, object]
    image_source: str | None = None
    signing: Mapping[str, str] | None = None


_IMAGE_NAMES = ("api", "web", "postgres", "redis")
OFFLINE_IMAGE_SOURCE = "production-offline-docker-archive-v1"
_TOP_LEVEL_FIELDS_V1 = frozenset(
    {"schema_version", "release_id", "commit", "mode", "images", "migration", "evidence"}
)
_TOP_LEVEL_FIELDS_V2 = _TOP_LEVEL_FIELDS_V1 | frozenset({"image_source", "signing"})
_IMAGE_FIELDS_V1 = frozenset({"ref", "id", "archive_file", "archive_sha256", "changed"})
_IMAGE_FIELDS_V2 = _IMAGE_FIELDS_V1 | frozenset({"archive_size"})
_MIGRATION_FIELDS = frozenset({"from", "target", "compatibility"})
_SIGNING_FIELDS = frozenset({"algorithm", "key_id", "file"})
_EVIDENCE_FIELDS_V1 = frozenset(
    {
        "release_gate_kind",
        "release_gate",
        "release_gate_sha256",
        "data_images",
        "backup_restore_change",
    }
)
_EVIDENCE_FIELDS_V2 = _EVIDENCE_FIELDS_V1 | frozenset({"offline_image_index"})
_BACKUP_RESTORE_FIELDS = frozenset({"record", "restore_report"})
_OFFLINE_INDEX_FIELDS = frozenset({"file", "sha256"})
_BOUND_EVIDENCE_FIELDS = frozenset({"file", "sha256", "size"})
_RELEASE_GATE_KINDS = frozenset({"release", "release_control_smoke"})
_RELEASE_ID_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_-]{0,62}[A-Za-z0-9])?")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
_REPOSITORY_COMPONENT_RE = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")
_REGISTRY_COMPONENT_RE = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[1-9][0-9]{0,4})?")
_TAG_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
_SAFE_NAME_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?")
_MIN_ARCHIVE_BYTES = 1
_MAX_ARCHIVE_BYTES = 8 * 1024 * 1024 * 1024
_MAX_EVIDENCE_BYTES = 16 * 1024 * 1024


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseManifestError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _require_object(value: object, context: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ReleaseManifestError(f"{context} must be an object")
    return cast(dict[str, Any], value)


def _require_exact_fields(
    value: dict[str, Any],
    expected: frozenset[str],
    context: str,
) -> None:
    actual = set(value)
    unknown = actual - expected
    missing = expected - actual
    if unknown:
        raise ReleaseManifestError(f"{context} has unknown fields: {sorted(unknown)}")
    if missing:
        raise ReleaseManifestError(f"{context} has missing fields: {sorted(missing)}")


def _require_matching_string(value: object, pattern: re.Pattern[str], context: str) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise ReleaseManifestError(f"{context} has an invalid value")
    return value


def _validate_repository(value: str, *, require_registry: bool) -> None:
    components = value.split("/")
    if any(not component for component in components):
        raise ReleaseManifestError("image repository has an invalid value")
    if require_registry:
        if len(components) < 2 or _REGISTRY_COMPONENT_RE.fullmatch(components[0]) is None:
            raise ReleaseManifestError("production ref must use image@sha256:RepoDigest")
        repository_components = components[1:]
    else:
        if len(components) > 1 and _REGISTRY_COMPONENT_RE.fullmatch(components[0]) is None:
            raise ReleaseManifestError("development ref has an invalid repository")
        repository_components = components[1:] if len(components) > 1 else components
    if any(_REPOSITORY_COMPONENT_RE.fullmatch(part) is None for part in repository_components):
        raise ReleaseManifestError("image repository has an invalid value")


def _validate_production_ref(value: object) -> str:
    if type(value) is not str:
        raise ReleaseManifestError("production ref must use image@sha256:RepoDigest")
    ref = value
    marker = "@sha256:"
    if ref.count(marker) != 1:
        raise ReleaseManifestError("production ref must use image@sha256:RepoDigest")
    repository, digest = ref.split(marker, 1)
    _validate_repository(repository, require_registry=True)
    if _SHA256_RE.fullmatch(digest) is None:
        raise ReleaseManifestError("production ref must use image@sha256:RepoDigest")
    return ref


def _validate_development_ref(value: object) -> str:
    if type(value) is not str:
        raise ReleaseManifestError("development ref must use a validated local tag")
    ref = value
    repository, separator, tag = ref.rpartition(":")
    if not separator or _TAG_RE.fullmatch(tag) is None or ".." in tag:
        raise ReleaseManifestError("development ref must use a validated local tag")
    _validate_repository(repository, require_registry=False)
    return ref


def _validate_safe_name(value: object, context: str) -> str:
    name = _require_matching_string(value, _SAFE_NAME_RE, context)
    if ".." in name or name.endswith(".part"):
        raise ReleaseManifestError(f"{context} has an invalid value")
    return name


def _validate_json_name(value: object, context: str) -> str:
    name = _validate_safe_name(value, context)
    if not name.endswith(".json"):
        raise ReleaseManifestError(f"{context} must be a safe JSON basename")
    return name


def _parse_bound_evidence(
    value: object,
    context: str,
) -> Mapping[str, str | int]:
    artifact = _require_object(value, context)
    _require_exact_fields(artifact, _BOUND_EVIDENCE_FIELDS, context)
    size = artifact["size"]
    if type(size) is not int or not 1 <= size <= _MAX_EVIDENCE_BYTES:
        raise ReleaseManifestError(f"{context}.size has an invalid value")
    return MappingProxyType(
        {
            "file": _validate_json_name(artifact["file"], f"{context}.file"),
            "sha256": _require_matching_string(
                artifact["sha256"],
                _SHA256_RE,
                f"{context}.sha256",
            ),
            "size": size,
        }
    )


def _parse_image(
    value: object,
    *,
    schema_version: int,
    mode: str,
    name: str,
) -> ImageSpec:
    image = _require_object(value, f"images.{name}")
    _require_exact_fields(
        image,
        _IMAGE_FIELDS_V1 if schema_version == 1 else _IMAGE_FIELDS_V2,
        f"images.{name}",
    )
    if schema_version == 2:
        ref = _require_matching_string(image["ref"], _IMAGE_ID_RE, f"images.{name}.ref")
    elif mode == "production":
        ref = _validate_production_ref(image["ref"])
    else:
        ref = _validate_development_ref(image["ref"])
    image_id = _require_matching_string(image["id"], _IMAGE_ID_RE, f"images.{name}.id")
    if type(image["changed"]) is not bool:
        raise ReleaseManifestError(f"images.{name}.changed must be a boolean")
    changed = image["changed"]
    archive_file_value = image["archive_file"]
    archive_file = (
        None
        if archive_file_value is None
        else _validate_safe_name(archive_file_value, f"images.{name}.archive_file")
    )
    archive_value = image["archive_sha256"]
    archive_sha256 = (
        None
        if archive_value is None
        else _require_matching_string(
            archive_value,
            _SHA256_RE,
            f"images.{name}.archive_sha256",
        )
    )
    archive_size: int | None = None
    if schema_version == 2:
        archive_size_value = image["archive_size"]
        if (
            type(archive_size_value) is not int
            or not _MIN_ARCHIVE_BYTES <= archive_size_value <= _MAX_ARCHIVE_BYTES
        ):
            raise ReleaseManifestError(
                f"images.{name}.archive_size must be between 1 byte and 8 GiB"
            )
        archive_size = archive_size_value
        expected_archive = f"{name}.tar"
        if archive_file != expected_archive or archive_sha256 is None:
            raise ReleaseManifestError(f"images.{name} must declare the fixed production archive")
        if ref != image_id:
            raise ReleaseManifestError(f"images.{name}.ref must equal images.{name}.id")
    elif mode == "production":
        if archive_file is not None or archive_sha256 is not None:
            raise ReleaseManifestError("production images must not declare archive fields")
    elif changed and (archive_file is None or archive_sha256 is None):
        raise ReleaseManifestError("changed development images require both archive fields")
    elif not changed and (archive_file is not None or archive_sha256 is not None):
        raise ReleaseManifestError("unchanged development images must not declare archive fields")
    return ImageSpec(
        ref=ref,
        image_id=image_id,
        archive_file=archive_file,
        archive_sha256=archive_sha256,
        archive_size=archive_size,
        changed=changed,
    )


def load_manifest_bytes(payload: bytes) -> ReleaseManifest:
    """从一次读取取得的确切字节严格校验发布清单。"""

    try:
        raw = json.loads(payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except ReleaseManifestError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseManifestError("manifest is not readable strict JSON") from exc

    document = _require_object(raw, "manifest")
    schema_version_value = document.get("schema_version")
    if type(schema_version_value) is not int or schema_version_value not in {1, 2}:
        raise ReleaseManifestError("schema_version must be exactly 1 or 2")
    schema_version = schema_version_value
    _require_exact_fields(
        document,
        _TOP_LEVEL_FIELDS_V1 if schema_version == 1 else _TOP_LEVEL_FIELDS_V2,
        "manifest",
    )
    release_id = _require_matching_string(
        document["release_id"],
        _RELEASE_ID_RE,
        "release_id",
    )
    commit = _require_matching_string(document["commit"], _COMMIT_RE, "commit")
    mode_value = document["mode"]
    if type(mode_value) is not str or mode_value not in {"development", "production"}:
        raise ReleaseManifestError("mode must be development or production")
    mode = cast(Literal["development", "production"], mode_value)
    if schema_version == 2 and mode != "production":
        raise ReleaseManifestError("schema_version 2 is only valid for production")

    image_source: str | None = None
    signing: Mapping[str, str] | None = None
    if schema_version == 2:
        if document["image_source"] != OFFLINE_IMAGE_SOURCE:
            raise ReleaseManifestError("image_source has an invalid value")
        image_source = OFFLINE_IMAGE_SOURCE
        signing_value = _require_object(document["signing"], "signing")
        _require_exact_fields(signing_value, _SIGNING_FIELDS, "signing")
        if signing_value["algorithm"] != "ed25519":
            raise ReleaseManifestError("signing.algorithm must be ed25519")
        if signing_value["file"] != "manifest.sig":
            raise ReleaseManifestError("signing.file must be manifest.sig")
        signing = MappingProxyType(
            {
                "algorithm": "ed25519",
                "key_id": _validate_safe_name(signing_value["key_id"], "signing.key_id"),
                "file": "manifest.sig",
            }
        )

    images_value = _require_object(document["images"], "images")
    _require_exact_fields(images_value, frozenset(_IMAGE_NAMES), "images")
    images = {
        name: _parse_image(
            images_value[name],
            schema_version=schema_version,
            mode=mode,
            name=name,
        )
        for name in _IMAGE_NAMES
    }

    migration = _require_object(document["migration"], "migration")
    _require_exact_fields(migration, _MIGRATION_FIELDS, "migration")
    migration_from = _validate_safe_name(migration["from"], "migration.from")
    migration_target = _validate_safe_name(migration["target"], "migration.target")
    try:
        compatibility = MigrationCompatibility(migration["compatibility"])
    except (TypeError, ValueError) as exc:
        raise ReleaseManifestError("migration.compatibility has an invalid value") from exc
    if compatibility is MigrationCompatibility.MANUAL:
        raise ReleaseManifestError("manual migration is not automatable")
    migration_changes_schema = migration_from != migration_target
    if migration_changes_schema is not (compatibility is MigrationCompatibility.EXPAND):
        raise ReleaseManifestError(
            "migration.compatibility must be expand exactly when migration changes schema"
        )
    if schema_version == 2:
        changed_count = sum(spec.changed for spec in images.values())
        if migration_changes_schema:
            raise ReleaseManifestError("offline production release cannot include a migration")
        if changed_count not in {0, len(_IMAGE_NAMES)}:
            raise ReleaseManifestError(
                "offline production release must change either zero or all four images"
            )

    evidence_value = _require_object(document["evidence"], "evidence")
    _require_exact_fields(
        evidence_value,
        _EVIDENCE_FIELDS_V1 if schema_version == 1 else _EVIDENCE_FIELDS_V2,
        "evidence",
    )
    release_gate_kind = evidence_value["release_gate_kind"]
    if type(release_gate_kind) is not str or release_gate_kind not in _RELEASE_GATE_KINDS:
        raise ReleaseManifestError("evidence.release_gate_kind has an invalid value")
    if release_gate_kind == "release_control_smoke" and mode != "development":
        raise ReleaseManifestError(
            "evidence.release_gate_kind=release_control_smoke is only valid for development"
        )
    release_gate = _validate_safe_name(evidence_value["release_gate"], "evidence.release_gate")
    release_gate_sha256 = _require_matching_string(
        evidence_value["release_gate_sha256"],
        _SHA256_RE,
        "evidence.release_gate_sha256",
    )
    data_candidate = evidence_value["data_images"]
    data_images: str | Mapping[str, str | int] | None
    if data_candidate is None:
        data_images = None
    elif schema_version == 1:
        data_images = _validate_safe_name(data_candidate, "evidence.data_images")
    else:
        data_images = _parse_bound_evidence(data_candidate, "evidence.data_images")
    data_changed = images["postgres"].changed or images["redis"].changed
    if data_changed is (data_images is None):
        raise ReleaseManifestError("evidence.data_images does not match changed data images")

    backup_candidate = evidence_value["backup_restore_change"]
    backup_required = mode == "production" and (
        images["postgres"].changed or migration_changes_schema
    )
    backup_evidence: Mapping[str, object] | None
    if backup_candidate is None:
        backup_evidence = None
    else:
        backup_object = _require_object(
            backup_candidate,
            "evidence.backup_restore_change",
        )
        _require_exact_fields(
            backup_object,
            _BACKUP_RESTORE_FIELDS,
            "evidence.backup_restore_change",
        )
        if schema_version == 1:
            backup_evidence = MappingProxyType(
                {
                    "record": _validate_json_name(
                        backup_object["record"],
                        "evidence.backup_restore_change.record",
                    ),
                    "restore_report": _validate_json_name(
                        backup_object["restore_report"],
                        "evidence.backup_restore_change.restore_report",
                    ),
                }
            )
        else:
            backup_evidence = MappingProxyType(
                {
                    "record": _parse_bound_evidence(
                        backup_object["record"],
                        "evidence.backup_restore_change.record",
                    ),
                    "restore_report": _parse_bound_evidence(
                        backup_object["restore_report"],
                        "evidence.backup_restore_change.restore_report",
                    ),
                }
            )
    if backup_required is (backup_evidence is None):
        raise ReleaseManifestError(
            "evidence.backup_restore_change does not match production "
            "PostgreSQL or migration change"
        )

    offline_index: Mapping[str, str] | None = None
    if schema_version == 2:
        offline_index_value = _require_object(
            evidence_value["offline_image_index"],
            "evidence.offline_image_index",
        )
        _require_exact_fields(
            offline_index_value,
            _OFFLINE_INDEX_FIELDS,
            "evidence.offline_image_index",
        )
        if offline_index_value["file"] != "offline-image-index.json":
            raise ReleaseManifestError(
                "evidence.offline_image_index.file must be offline-image-index.json"
            )
        offline_index = MappingProxyType(
            {
                "file": "offline-image-index.json",
                "sha256": _require_matching_string(
                    offline_index_value["sha256"],
                    _SHA256_RE,
                    "evidence.offline_image_index.sha256",
                ),
            }
        )

    bundle_names = ["manifest.json", release_gate]
    if signing is not None:
        bundle_names.append(signing["file"])
    if offline_index is not None:
        bundle_names.append(offline_index["file"])
    bundle_names.extend(
        spec.archive_file for spec in images.values() if spec.archive_file is not None
    )
    if data_images is not None:
        bundle_names.append(
            data_images if isinstance(data_images, str) else str(data_images["file"])
        )
    if backup_evidence is not None:
        if schema_version == 1:
            bundle_names.extend(str(value) for value in backup_evidence.values())
        else:
            bundle_names.extend(
                str(cast(Mapping[str, object], value)["file"]) for value in backup_evidence.values()
            )
    if len(bundle_names) != len(set(bundle_names)):
        raise ReleaseManifestError("bundle basenames must be globally unique")

    optional_evidence: dict[str, object] = {
        "release_gate_kind": release_gate_kind,
        "release_gate": release_gate,
        "release_gate_sha256": release_gate_sha256,
        "data_images": data_images,
        "backup_restore_change": backup_evidence,
    }
    if offline_index is not None:
        optional_evidence["offline_image_index"] = offline_index

    return ReleaseManifest(
        schema_version=schema_version,
        release_id=release_id,
        commit=commit,
        mode=mode,
        images=MappingProxyType(images),
        migration_from=migration_from,
        migration_target=migration_target,
        migration_compatibility=compatibility,
        evidence=MappingProxyType(optional_evidence),
        image_source=image_source,
        signing=signing,
    )


def load_manifest(path: Path) -> ReleaseManifest:
    """读取并严格校验发布清单，不修正或容忍畸形输入。"""

    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ReleaseManifestError("manifest is not readable strict JSON") from exc
    return load_manifest_bytes(payload)


def release_evidence_ref(manifest: ReleaseManifest, name: str) -> str:
    """返回 release-gate 中应与指定镜像绑定的引用。"""

    if name not in _IMAGE_NAMES:
        raise ReleaseManifestError("image name must identify one of the four release images")
    if manifest.image_source == OFFLINE_IMAGE_SOURCE:
        return f"sms-platform-release-{name}:{manifest.commit}"
    return manifest.images[name].ref


def validate_changed_images(
    manifest: ReleaseManifest,
    current_refs: Mapping[str, str],
) -> None:
    """核对 changed 标记与当前四镜像引用的实际差异完全一致。"""

    if set(current_refs) != set(_IMAGE_NAMES):
        raise ReleaseManifestError("current refs must contain exactly four images")
    for name in _IMAGE_NAMES:
        current_ref = current_refs[name]
        if type(current_ref) is not str:
            raise ReleaseManifestError(f"current ref for {name} must be a string")
        observed_changed = current_ref != manifest.images[name].ref
        if manifest.images[name].changed is not observed_changed:
            raise ReleaseManifestError(f"changed flag does not match observed ref for {name}")
