#!/usr/bin/env python3
"""把 CycloneDX SBOM 规范化为可重复哈希的 JSON。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


class SbomCanonicalizationError(ValueError):
    pass


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, list):
        normalized = [_normalize(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    return value


def canonicalize(document: object) -> dict[str, Any]:
    if not isinstance(document, dict) or document.get("bomFormat") != "CycloneDX":
        raise SbomCanonicalizationError("input is not a CycloneDX document")
    if not isinstance(document.get("specVersion"), str):
        raise SbomCanonicalizationError("CycloneDX specVersion is missing")
    normalized = dict(document)
    normalized.pop("serialNumber", None)
    metadata = normalized.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, dict):
            raise SbomCanonicalizationError("CycloneDX metadata is invalid")
        metadata = dict(metadata)
        metadata.pop("timestamp", None)
        normalized["metadata"] = metadata
    result = _normalize(normalized)
    if not isinstance(result, dict):
        raise AssertionError("canonical CycloneDX root must remain an object")
    return result


def write_canonical(source: Path, destination: Path) -> None:
    if not source.is_absolute() or not destination.is_absolute():
        raise SbomCanonicalizationError("SBOM paths must be absolute")
    if source.is_symlink() or destination.is_symlink():
        raise SbomCanonicalizationError("SBOM paths must not be symlinks")
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SbomCanonicalizationError("SBOM input is unavailable or invalid") from error
    payload = json.dumps(
        canonicalize(document),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    destination.write_text(payload + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    arguments = parser.parse_args()
    try:
        write_canonical(arguments.source, arguments.destination)
    except SbomCanonicalizationError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
