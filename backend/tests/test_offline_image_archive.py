from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))

from offline_image_archive import (  # noqa: E402
    OfflineImageArchiveError,
    load_offline_image_index_bytes,
    validate_offline_image_archive,
)

COMMIT = "c" * 40
IMAGE_ID = "sha256:" + "a" * 64


def _archive(path: Path, payload: bytes = b"docker-save-archive") -> tuple[str, int]:
    path.write_bytes(payload)
    path.chmod(0o600)
    return hashlib.sha256(payload).hexdigest(), len(payload)


def test_validates_private_archive_hash_and_size_without_reimplementing_docker(
    tmp_path: Path,
) -> None:
    path = tmp_path / "api.tar"
    digest, size = _archive(path)

    result = validate_offline_image_archive(
        path,
        name="api",
        expected_sha256=digest,
        expected_size=size,
    )

    assert result.path == path
    assert result.sha256 == digest
    assert result.size == size


@pytest.mark.parametrize("mutation", ["mode", "hardlink", "hash", "size", "name"])
def test_rejects_unsafe_or_unbound_archive_file(tmp_path: Path, mutation: str) -> None:
    path = tmp_path / ("wrong.tar" if mutation == "name" else "api.tar")
    digest, size = _archive(path)
    if mutation == "mode":
        path.chmod(0o666)
    elif mutation == "hardlink":
        (tmp_path / "linked.tar").hardlink_to(path)
    elif mutation == "hash":
        digest = "f" * 64
    elif mutation == "size":
        size += 1

    with pytest.raises(OfflineImageArchiveError):
        validate_offline_image_archive(
            path,
            name="api",
            expected_sha256=digest,
            expected_size=size,
        )


def _artifact(file: str) -> dict[str, object]:
    return {"file": file, "sha256": "b" * 64, "size": 123}


def _index() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "production_offline_image_index",
        "candidate_commit": COMMIT,
        "release_gate": _artifact("release-gate.json"),
        "reproducibility": _artifact("reproducibility.json"),
        "images": {
            name: {
                "image_id": IMAGE_ID,
                "archive": _artifact(f"images/{name}.tar"),
                "scan": _artifact(f"scans/{name}.json"),
                "sbom": {
                    "candidate": _artifact(f"sboms/{name}.cdx.json"),
                    "rebuild": _artifact(f"sboms/{name}.rebuild.cdx.json"),
                },
            }
            for name in ("api", "web", "postgres", "redis")
        },
    }


def _json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


def test_offline_image_index_parser_is_exact_and_typed() -> None:
    index = load_offline_image_index_bytes(_json(_index()))

    assert index.candidate_commit == COMMIT
    assert index.release_gate.file == "release-gate.json"
    assert index.images["api"].archive.file == "images/api.tar"

    invalid = _index()
    invalid["unknown"] = True
    with pytest.raises(OfflineImageArchiveError, match="header"):
        load_offline_image_index_bytes(_json(invalid))
