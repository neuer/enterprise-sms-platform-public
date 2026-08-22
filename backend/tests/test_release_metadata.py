from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from release_metadata import (  # noqa: E402
    ReleaseMetadataError,
    collect_release_metadata,
    schema_head,
    source_version,
)


def test_repository_has_one_version_source_and_one_migration_head() -> None:
    assert source_version(ROOT) == "1.6.0"
    assert schema_head(ROOT) == "0073_raw_legacy_capture"


def test_release_metadata_binds_exact_commit_contract_workflow_and_sboms(
    tmp_path: Path,
) -> None:
    sboms: dict[str, Path] = {}
    for name in ("api", "web", "postgres", "redis"):
        path = tmp_path / f"{name}.cdx.json"
        path.write_text(json.dumps({"bomFormat": "CycloneDX", "name": name}), encoding="utf-8")
        sboms[name] = path

    metadata = collect_release_metadata(
        ROOT,
        commit="c" * 40,
        workflow_repository="example/enterprise-sms-platform",
        workflow_run_id=123,
        workflow_run_attempt=2,
        sboms=sboms,
    )

    assert metadata.app_version == "1.6.0"
    assert metadata.git_sha == "c" * 40
    assert metadata.schema_revision == "0073_raw_legacy_capture"
    assert len(metadata.openapi_sha256) == 64
    assert metadata.workflow_run_id == 123
    assert set(metadata.sbom_sha256) == {"api", "web", "postgres", "redis"}


def test_release_metadata_fails_closed_on_missing_or_old_candidate_evidence(
    tmp_path: Path,
) -> None:
    sboms = {name: tmp_path / f"{name}.json" for name in ("api", "web", "postgres")}
    for path in sboms.values():
        path.write_text("{}", encoding="utf-8")

    with pytest.raises(ReleaseMetadataError, match="four image"):
        collect_release_metadata(
            ROOT,
            commit="b" * 40,
            workflow_repository="local",
            workflow_run_id=0,
            workflow_run_attempt=0,
            sboms=sboms,
        )

    shared = tmp_path / "shared.cdx.json"
    shared.write_text("{}", encoding="utf-8")
    with pytest.raises(ReleaseMetadataError, match="distinct"):
        collect_release_metadata(
            ROOT,
            commit="b" * 40,
            workflow_repository="local",
            workflow_run_id=0,
            workflow_run_attempt=0,
            sboms={
                name: shared for name in ("api", "web", "postgres", "redis")
            },
        )
