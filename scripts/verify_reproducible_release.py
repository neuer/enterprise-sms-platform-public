#!/usr/bin/env python3
"""比较两个独立候选构建的镜像 ID 与规范化 SBOM 摘要。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

IMAGES = ("api", "web", "postgres", "redis")
SHA256 = re.compile(r"[0-9a-f]{64}")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")


class ReproducibilityError(ValueError):
    pass


def _load(path: Path) -> dict[str, Any]:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ReproducibilityError("release report path is unavailable or unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReproducibilityError("release report is invalid") from error
    if not isinstance(value, dict) or value.get("passed") is not True:
        raise ReproducibilityError("release report did not pass")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def observation(
    *,
    commit: str,
    sboms: Mapping[str, Path],
    image_ids: Mapping[str, str],
) -> dict[str, Any]:
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ReproducibilityError("candidate commit is invalid")
    if set(sboms) != set(IMAGES) or len(set(sboms.values())) != len(IMAGES):
        raise ReproducibilityError("exactly four distinct SBOMs are required")
    if set(image_ids) != set(IMAGES):
        raise ReproducibilityError("exactly four image IDs are required")

    hashes: dict[str, str] = {}
    for name in IMAGES:
        path = sboms[name]
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise ReproducibilityError("rebuild SBOM path is unavailable or unsafe")
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ReproducibilityError("rebuild SBOM is invalid") from error
        metadata = document.get("metadata") if isinstance(document, dict) else None
        if (
            not isinstance(document, dict)
            or document.get("bomFormat") != "CycloneDX"
            or "serialNumber" in document
            or (isinstance(metadata, dict) and "timestamp" in metadata)
        ):
            raise ReproducibilityError("rebuild SBOM is not canonical")
        hashes[name] = _sha256(path)

        image_id = image_ids[name]
        if IMAGE_ID.fullmatch(image_id) is None:
            raise ReproducibilityError("rebuild image ID is invalid")

    return {
        "schema_version": 1,
        "gate_type": "release_reproducibility",
        "candidate_commit": commit,
        "passed": True,
        "source": {"sbom_sha256": hashes},
        "images": {
            name: {"image_id": image_ids[name]}
            for name in IMAGES
        },
    }


def identity(document: dict[str, Any]) -> tuple[str, dict[str, str], dict[str, str]]:
    candidate = document.get("candidate_commit")
    source = document.get("source")
    images = document.get("images")
    if (
        not isinstance(candidate, str)
        or re.fullmatch(r"[0-9a-f]{40}", candidate) is None
        or not isinstance(source, dict)
        or not isinstance(images, dict)
    ):
        raise ReproducibilityError("release report identity is invalid")
    sboms = source.get("sbom_sha256")
    if (
        not isinstance(sboms, dict)
        or set(sboms) != set(IMAGES)
        or any(
            not isinstance(value, str) or SHA256.fullmatch(value) is None
            for value in sboms.values()
        )
        or set(images) != set(IMAGES)
    ):
        raise ReproducibilityError("release SBOM or image set is invalid")
    image_ids: dict[str, str] = {}
    for name in IMAGES:
        image = images[name]
        if not isinstance(image, dict):
            raise ReproducibilityError("release image identity is invalid")
        image_id = image.get("image_id")
        if not isinstance(image_id, str) or IMAGE_ID.fullmatch(image_id) is None:
            raise ReproducibilityError("release image ID is invalid")
        image_ids[name] = image_id
    return candidate, dict(sboms), image_ids


def verify(first: dict[str, Any], second: dict[str, Any]) -> None:
    first_identity = identity(first)
    second_identity = identity(second)
    if first_identity[0] != second_identity[0]:
        raise ReproducibilityError("release reports use different commits")
    if first_identity[1] != second_identity[1]:
        raise ReproducibilityError("canonical SBOM digests are not reproducible")
    if first_identity[2] != second_identity[2]:
        raise ReproducibilityError("image IDs are not reproducible")


def _pairs(
    values: list[list[str]],
    *,
    label: str,
) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in values:
        if name not in IMAGES or name in result:
            raise ReproducibilityError(f"{label} set is invalid")
        result[name] = value
    if set(result) != set(IMAGES):
        raise ReproducibilityError(f"{label} set is incomplete")
    return result


def _write_evidence(path: Path, document: dict[str, Any]) -> None:
    if (
        not path.is_absolute()
        or path == Path("/")
        or path.is_symlink()
        or not path.parent.is_dir()
        or path.parent.is_symlink()
    ):
        raise ReproducibilityError("reproducibility report path is unsafe")
    temporary = path.with_name(f".{path.name}.part")
    if temporary.is_symlink():
        raise ReproducibilityError("reproducibility report temporary path is unsafe")
    payload = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    try:
        temporary.write_text(payload + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, path)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise ReproducibilityError("reproducibility report cannot be written") from error


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--sbom", action="append", nargs=2, required=True)
    parser.add_argument("--image", action="append", nargs=2, required=True)
    arguments = parser.parse_args()
    try:
        sbom_values = _pairs(arguments.sbom, label="SBOM")
        image_values = _pairs(arguments.image, label="image")
        rebuilt = observation(
            commit=arguments.commit,
            sboms={
                name: Path(value)
                for name, value in sbom_values.items()
            },
            image_ids=image_values,
        )
        baseline = _load(arguments.baseline)
        verify(baseline, rebuilt)
        rebuilt["source"]["baseline_report_sha256"] = _sha256(arguments.baseline)
        _write_evidence(arguments.output, rebuilt)
    except ReproducibilityError as error:
        parser.error(str(error))
    print("release reproducibility verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
