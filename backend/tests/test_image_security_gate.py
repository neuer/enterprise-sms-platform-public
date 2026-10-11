from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import verify_image_security as gate  # noqa: E402

SHA = "a" * 40
IMAGE = "sha256:" + "b" * 64


@pytest.mark.parametrize(
    "defect", ["none", "high", "missing", "identity", "stale", "future", "exit"]
)
def test_scan_gate_rejects_incomplete_or_mismatched_evidence(defect: str) -> None:
    raw = {
        "SchemaVersion": 2,
        "Metadata": {"ImageID": IMAGE},
        "Results": [{"Type": "alpine", "Vulnerabilities": []}],
    }
    database = {"updated_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat()}
    if defect == "high":
        raw["Results"][0]["Vulnerabilities"] = [  # type: ignore[index]
            {"Severity": "HIGH", "VulnerabilityID": "CVE-2026-12345"}
        ]
    if defect == "identity":
        raw["Metadata"] = {"ImageID": "sha256:" + "c" * 64}
    if defect in {"stale", "future"}:
        delta = timedelta(days=2 if defect == "stale" else -1)
        database["updated_at"] = (datetime.now(UTC) - delta).isoformat()
    result = gate.scan_summary(
        None if defect == "missing" else raw, 2 if defect == "exit" else 0, IMAGE, database
    )
    assert (result["outcome"] == "passed") is (defect == "none")


@pytest.mark.parametrize("label,platform", [("development", "amd64"), (SHA, "arm64")])
def test_image_source_and_platform_are_enforced(
    monkeypatch: pytest.MonkeyPatch, label: str, platform: str
) -> None:
    inspected = [
        {
            "Id": IMAGE,
            "Os": "linux",
            "Architecture": platform,
            "Config": {"Labels": {"org.opencontainers.image.revision": label}},
        }
    ]
    monkeypatch.setattr(gate, "command", lambda *args: json.dumps(inspected))
    with pytest.raises(ValueError):
        gate.inspect_image("candidate", SHA)


def test_dirty_source_cannot_produce_sha_bound_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "command", lambda *args: " M backend/Dockerfile")
    with pytest.raises(ValueError, match="unclean"):
        gate.source_sha()
