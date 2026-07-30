from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from canonicalize_sbom import (  # noqa: E402
    SbomCanonicalizationError,
    canonicalize,
    write_canonical,
)


def test_canonical_sbom_removes_volatile_fields_and_sorts_arrays(
    tmp_path: Path,
) -> None:
    first = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": "urn:uuid:first",
        "metadata": {"timestamp": "2026-07-28T01:00:00Z", "component": {"name": "api"}},
        "components": [
            {"name": "z", "version": "1"},
            {"name": "a", "version": "2"},
        ],
    }
    second = {
        **first,
        "serialNumber": "urn:uuid:second",
        "metadata": {"timestamp": "2026-07-28T02:00:00Z", "component": {"name": "api"}},
        "components": list(reversed(first["components"])),
    }
    sources = (tmp_path / "first.json", tmp_path / "second.json")
    outputs = (tmp_path / "first.cdx.json", tmp_path / "second.cdx.json")
    for path, document in zip(sources, (first, second), strict=True):
        path.write_text(json.dumps(document), encoding="utf-8")
    for source, output in zip(sources, outputs, strict=True):
        write_canonical(source.resolve(), output.resolve())

    assert outputs[0].read_bytes() == outputs[1].read_bytes()
    normalized = json.loads(outputs[0].read_text(encoding="utf-8"))
    assert "serialNumber" not in normalized
    assert "timestamp" not in normalized["metadata"]
    assert [item["name"] for item in normalized["components"]] == ["a", "z"]


def test_canonical_sbom_rejects_non_cyclonedx() -> None:
    with pytest.raises(SbomCanonicalizationError, match="CycloneDX"):
        canonicalize({"bomFormat": "SPDX", "specVersion": "2.3"})
