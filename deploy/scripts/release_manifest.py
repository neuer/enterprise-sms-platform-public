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
    evidence: Mapping[str, str | Mapping[str, str] | None]


_IMAGE_NAMES = ("api", "web", "postgres", "redis")
_TOP_LEVEL_FIELDS = frozenset(
    {"schema_version", "release_id", "commit", "mode", "images", "migration", "evidence"}
)
_IMAGE_FIELDS = frozenset({"ref", "id", "archive_file", "archive_sha256", "changed"})
_MIGRATION_FIELDS = frozenset({"from", "target", "compatibility"})
_EVIDENCE_FIELDS = frozenset(
    {
        "release_gate_kind",
        "release_gate",
        "release_gate_sha256",
        "data_images",
        "backup_restore_change",
    }
)
_BACKUP_RESTORE_FIELDS = frozenset({"record", "restore_report"})
_RELEASE_GATE_KINDS = frozenset({"release", "release_control_smoke"})
_RELEASE_ID_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_-]{0,62}[A-Za-z0-9])?")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
_REPOSITORY_COMPONENT_RE = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")
_REGISTRY_COMPONENT_RE = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[1-9][0-9]{0,4})?")
_TAG_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
_SAFE_NAME_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?")


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


def _parse_image(value: object, *, mode: str, name: str) -> ImageSpec:
    image = _require_object(value, f"images.{name}")
    _require_exact_fields(image, _IMAGE_FIELDS, f"images.{name}")
    if mode == "production":
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
    if mode == "production":
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
    _require_exact_fields(document, _TOP_LEVEL_FIELDS, "manifest")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ReleaseManifestError("schema_version must be exactly 1")
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

    images_value = _require_object(document["images"], "images")
    _require_exact_fields(images_value, frozenset(_IMAGE_NAMES), "images")
    images = {name: _parse_image(images_value[name], mode=mode, name=name) for name in _IMAGE_NAMES}

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

    evidence_value = _require_object(document["evidence"], "evidence")
    _require_exact_fields(evidence_value, _EVIDENCE_FIELDS, "evidence")
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
    data_images = (
        None
        if data_candidate is None
        else _validate_safe_name(data_candidate, "evidence.data_images")
    )
    data_changed = images["postgres"].changed or images["redis"].changed
    if data_changed is (data_images is None):
        raise ReleaseManifestError("evidence.data_images does not match changed data images")

    backup_candidate = evidence_value["backup_restore_change"]
    backup_required = mode == "production" and (
        images["postgres"].changed or migration_changes_schema
    )
    backup_evidence: Mapping[str, str] | None
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
    if backup_required is (backup_evidence is None):
        raise ReleaseManifestError(
            "evidence.backup_restore_change does not match production "
            "PostgreSQL or migration change"
        )

    bundle_names = ["manifest.json", release_gate]
    bundle_names.extend(
        spec.archive_file for spec in images.values() if spec.archive_file is not None
    )
    if data_images is not None:
        bundle_names.append(data_images)
    if backup_evidence is not None:
        bundle_names.extend(backup_evidence.values())
    if len(bundle_names) != len(set(bundle_names)):
        raise ReleaseManifestError("bundle basenames must be globally unique")

    optional_evidence: dict[str, str | Mapping[str, str] | None] = {
        "release_gate_kind": release_gate_kind,
        "release_gate": release_gate,
        "release_gate_sha256": release_gate_sha256,
        "data_images": data_images,
        "backup_restore_change": backup_evidence,
    }

    return ReleaseManifest(
        schema_version=1,
        release_id=release_id,
        commit=commit,
        mode=mode,
        images=MappingProxyType(images),
        migration_from=migration_from,
        migration_target=migration_target,
        migration_compatibility=compatibility,
        evidence=MappingProxyType(optional_evidence),
    )


def load_manifest(path: Path) -> ReleaseManifest:
    """读取并严格校验发布清单，不修正或容忍畸形输入。"""

    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ReleaseManifestError("manifest is not readable strict JSON") from exc
    return load_manifest_bytes(payload)


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
