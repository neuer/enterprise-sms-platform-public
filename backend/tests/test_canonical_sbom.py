from __future__ import annotations

import json
import sys
from copy import deepcopy
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


def _graph(reference: str) -> dict[str, object]:
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "metadata": {"component": {"name": "image", "bom-ref": reference}},
        "components": [
            {
                "name": "package",
                "version": "1",
                "bom-ref": "pkg:apk/test@1",
                "hashes": [{"alg": "SHA-256", "content": "a" * 64}],
            }
        ],
        "dependencies": [{"ref": reference, "dependsOn": ["pkg:apk/test@1"]}],
    }


def test_uuid_renaming_preserves_dependency_graph_and_is_idempotent() -> None:
    first = canonicalize(_graph("11111111-1111-4111-8111-111111111111"))
    second = canonicalize(_graph("22222222-2222-4222-8222-222222222222"))
    assert first == second
    assert canonicalize(first) == first
    root = first["metadata"]["component"]["bom-ref"]
    assert first["dependencies"] == [{"ref": root, "dependsOn": ["pkg:apk/test@1"]}]


@pytest.mark.parametrize("change", ["version", "hash", "edge"])
def test_canonicalization_never_hides_package_or_dependency_changes(change: str) -> None:
    baseline = _graph("11111111-1111-4111-8111-111111111111")
    altered = deepcopy(baseline)
    if change == "version":
        altered["components"][0]["version"] = "2"  # type: ignore[index]
    elif change == "hash":
        altered["components"][0]["hashes"][0]["content"] = "b" * 64  # type: ignore[index]
    else:
        altered["dependencies"][0]["dependsOn"] = []  # type: ignore[index]
    assert canonicalize(baseline) != canonicalize(altered)


def test_undefined_uuid_reference_is_rejected() -> None:
    document = _graph("11111111-1111-4111-8111-111111111111")
    document["dependencies"][0]["ref"] = "22222222-2222-4222-8222-222222222222"  # type: ignore[index]
    with pytest.raises(SbomCanonicalizationError, match="undefined"):
        canonicalize(document)
