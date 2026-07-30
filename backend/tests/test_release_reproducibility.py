from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from verify_reproducible_release import (  # noqa: E402
    ReproducibilityError,
    _write_evidence,
    observation,
    verify,
)


def report() -> dict[str, object]:
    return {
        "candidate_commit": "a" * 40,
        "passed": True,
        "source": {
            "sbom_sha256": {
                name: character * 64
                for name, character in zip(
                    ("api", "web", "postgres", "redis"),
                    "1234",
                    strict=True,
                )
            }
        },
        "images": {
            name: {"image_id": "sha256:" + character * 64}
            for name, character in zip(
                ("api", "web", "postgres", "redis"),
                "abcd",
                strict=True,
            )
        },
    }


def test_independent_reports_with_same_inputs_are_reproducible() -> None:
    verify(report(), deepcopy(report()))


def test_image_or_sbom_drift_fails_reproducibility() -> None:
    first = report()
    second = deepcopy(first)
    second["images"]["api"]["image_id"] = "sha256:" + "f" * 64  # type: ignore[index]
    with pytest.raises(ReproducibilityError, match="image IDs"):
        verify(first, second)  # type: ignore[arg-type]

    second = deepcopy(first)
    second["source"]["sbom_sha256"]["web"] = "f" * 64  # type: ignore[index]
    with pytest.raises(ReproducibilityError, match="SBOM"):
        verify(first, second)  # type: ignore[arg-type]


def test_rebuild_observation_requires_canonical_distinct_sboms(
    tmp_path: Path,
) -> None:
    sboms: dict[str, Path] = {}
    image_ids: dict[str, str] = {}
    for name, character in zip(
        ("api", "web", "postgres", "redis"),
        "1234",
        strict=True,
    ):
        path = (tmp_path / f"{name}.cdx.json").resolve()
        path.write_text(
            json.dumps(
                {
                    "bomFormat": "CycloneDX",
                    "specVersion": "1.6",
                    "components": [{"name": name}],
                }
            ),
            encoding="utf-8",
        )
        sboms[name] = path
        image_ids[name] = "sha256:" + character * 64

    result = observation(commit="a" * 40, sboms=sboms, image_ids=image_ids)

    assert result["gate_type"] == "release_reproducibility"
    assert set(result["source"]["sbom_sha256"]) == set(sboms)
    sboms["web"].write_text(
        '{"bomFormat":"CycloneDX","specVersion":"1.6",'
        '"serialNumber":"volatile"}',
        encoding="utf-8",
    )
    with pytest.raises(ReproducibilityError, match="canonical"):
        observation(commit="a" * 40, sboms=sboms, image_ids=image_ids)


def test_reproducibility_evidence_is_private_and_atomic(tmp_path: Path) -> None:
    output = (tmp_path / "reproducibility.json").resolve()

    _write_evidence(output, report())

    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(output.read_text(encoding="utf-8"))["passed"] is True
    assert not (tmp_path / ".reproducibility.json.part").exists()
