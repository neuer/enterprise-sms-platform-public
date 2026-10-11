#!/usr/bin/env python3
"""把 CycloneDX SBOM 规范化为可重复哈希的 JSON。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


class SbomCanonicalizationError(ValueError):
    pass


UUID_REF = re.compile(r"(?:urn:uuid:)?[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z", re.I)


def _stable_references(document: dict[str, Any]) -> dict[str, Any]:
    """只重命名随机 UUID 引用，保留组件事实、哈希和依赖边；重复定义失败关闭。"""
    references: dict[str, str] = {}
    identities: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            ref = value.get("bom-ref")
            if isinstance(ref, str):
                if ref in identities:
                    raise SbomCanonicalizationError("duplicate CycloneDX bom-ref")
                identities.add(ref)
                if UUID_REF.fullmatch(ref):
                    # Trivy 的根镜像/操作系统组件使用 UUID；包的 purl 不重写。
                    identity = {key: item for key, item in value.items() if key != "bom-ref"}
                    encoded = json.dumps(
                        _normalize(identity), sort_keys=True, separators=(",", ":")
                    )
                    references[ref] = "urn:sms-sbom:" + hashlib.sha256(encoded.encode()).hexdigest()
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    def rewrite(value: Any, key: str = "") -> Any:
        if isinstance(value, dict):
            return {name: rewrite(item, name) for name, item in value.items()}
        if isinstance(value, list):
            return [rewrite(item, key) for item in value]
        if isinstance(value, str) and key in {"bom-ref", "ref", "dependsOn", "provides"}:
            if UUID_REF.fullmatch(value) and value not in references:
                raise SbomCanonicalizationError("undefined CycloneDX UUID reference")
            return references.get(value, value)
        return value

    visit(document)
    if len(set(references.values())) != len(references):
        raise SbomCanonicalizationError("ambiguous CycloneDX component identity")
    rewritten = rewrite(document)
    if not isinstance(rewritten, dict):
        raise AssertionError("rewritten CycloneDX root must remain an object")
    return rewritten


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
    result = _normalize(_stable_references(normalized))
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
