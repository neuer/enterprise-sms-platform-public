from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import render_release_evidence as evidence_module  # noqa: E402
from release_metadata import ReleaseMetadata, collect_release_metadata  # noqa: E402
from render_release_evidence import (  # noqa: E402
    ReleaseEvidenceError,
    write_data_image_evidence,
    write_promoted_release_evidence,
    write_release_evidence,
)

COMMIT = "c" * 40
TRIVY_IMAGE = "aquasec/trivy:0.70.0@sha256:" + "d" * 64


def _metadata(tmp_path: Path) -> ReleaseMetadata:
    sboms: dict[str, Path] = {}
    for name in ("api", "web", "postgres", "redis"):
        path = tmp_path / f"{name}.cdx.json"
        path.write_text(json.dumps({"bomFormat": "CycloneDX", "name": name}), encoding="utf-8")
        sboms[name] = path
    return collect_release_metadata(
        ROOT,
        commit=COMMIT,
        workflow_repository="local",
        workflow_run_id=0,
        workflow_run_attempt=0,
        sboms=sboms,
    )


def _release_images() -> dict[str, dict[str, str]]:
    return {
        name: {
            "ref": f"sms-platform-release-{name}:{COMMIT}",
            "image_id": "sha256:" + character * 64,
            "repo_digests": f"registry.example.com/sms/{name}@sha256:" + character * 64,
        }
        for name, character in zip(
            ("api", "web", "postgres", "redis"),
            ("a", "b", "e", "f"),
            strict=True,
        )
    }


def _scan_reports(
    tmp_path: Path,
    images: dict[str, dict[str, str]],
    *,
    vulnerable: str | None = None,
) -> dict[str, Path]:
    reports: dict[str, Path] = {}
    for name, image in images.items():
        vulnerabilities: list[dict[str, str]] = []
        if name == vulnerable:
            vulnerabilities = [{"VulnerabilityID": "CVE-TEST", "Severity": "HIGH"}]
        path = tmp_path / f"trivy-{name}.json"
        path.write_text(
            json.dumps(
                {
                    "SchemaVersion": 2,
                    "ArtifactName": image["ref"],
                    "ArtifactType": "container_image",
                    "Metadata": {"ImageID": image["image_id"]},
                    "Results": [
                        {
                            "Target": image["ref"],
                            "Class": "os-pkgs",
                            "Type": "alpine",
                            "Vulnerabilities": vulnerabilities,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        reports[name] = path
    return reports


def _data_images() -> dict[str, dict[str, str]]:
    return {
        "postgres": {
            "ref": f"sms-platform-release-postgres:{COMMIT}",
            "image_id": "sha256:" + "e" * 64,
            "platform": "linux/amd64",
            "version_output": "postgres (PostgreSQL) 16.8",
        },
        "redis": {
            "ref": f"sms-platform-release-redis:{COMMIT}",
            "image_id": "sha256:" + "f" * 64,
            "platform": "linux/amd64",
            "version_output": (
                "Redis server v=7.4.2 sha=00000000:0 malloc=jemalloc-5.3.0 bits=64 build=abcdef12"
            ),
        },
    }


def _control_smoke_images() -> dict[str, dict[str, str]]:
    return {
        "api": {
            "ref": "sms-platform-api:amd64-ffcecbe",
            "image_id": "sha256:07f1deaea83a50ac7d44d872f0748be523bc9edfa641d97565979d5031980c39",
            "platform": "linux/amd64",
        },
        "web": {
            "ref": "sms-platform-web:amd64-ffcecbe",
            "image_id": "sha256:804a1d8dc488b27535b45d15d7abee81b8e95ca3300b1d8939de23309af17d46",
            "platform": "linux/amd64",
        },
        "postgres": {
            "ref": "sms-platform-postgres:amd64-ffcecbe",
            "image_id": "sha256:4e9aa6c3ed14ac7d2f56a960066617bac46461a996bdd426b3c375b6fdfccb81",
            "platform": "linux/amd64",
        },
        "redis": {
            "ref": "sms-platform-redis:amd64-ffcecbe",
            "image_id": "sha256:ab4439eedeb2e9c742b5e3b087a269d95a98bb61eaaaaa25f75b93989fd2bf51",
            "platform": "linux/amd64",
        },
    }


def test_release_report_is_complete_atomic_and_private(tmp_path: Path) -> None:
    output = tmp_path / "release-gate.json"
    images = _release_images()

    write_release_evidence(
        output,
        commit=COMMIT,
        trivy_image=TRIVY_IMAGE,
        images=images,
        scan_reports=_scan_reports(tmp_path, images),
        metadata=_metadata(tmp_path),
    )

    info = output.lstat()
    assert stat.S_ISREG(info.st_mode)
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert info.st_uid == os.geteuid()
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["schema_version"] == 1
    assert report["gate_type"] == "release"
    assert report["candidate_commit"] == COMMIT
    assert report["source"]["git_sha"] == COMMIT
    assert report["source"]["schema_revision"] == "0052_idempotency_request_hash"
    assert len(report["source"]["openapi_sha256"]) == 64
    assert set(report["source"]["sbom_sha256"]) == {
        "api",
        "web",
        "postgres",
        "redis",
    }
    assert report["generated_at"].endswith("Z")
    assert report["trivy_image"] == TRIVY_IMAGE
    assert report["passed"] is True
    assert report["promotion_source"] is None
    assert set(report["images"]) == {"api", "web", "postgres", "redis"}
    assert all(image["scan_passed"] is True for image in report["images"].values())
    assert all("scan_report_sha256" in image for image in report["images"].values())
    assert all(image["repo_digests"] for image in report["images"].values())
    assert not list(tmp_path.glob(".release-gate.json.*.tmp"))


def test_data_report_records_ids_and_successful_role_persistence_checks(
    tmp_path: Path,
) -> None:
    output = tmp_path / "data-images.json"

    write_data_image_evidence(output, commit=COMMIT, images=_data_images())

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["schema_version"] == 1
    assert report["gate_type"] == "data_images"
    assert report["candidate_commit"] == COMMIT
    assert report["passed"] is True
    assert set(report["images"]) == {"postgres", "redis"}
    assert report["images"]["postgres"]["image_id"].startswith("sha256:")
    assert report["images"]["redis"]["image_id"].startswith("sha256:")
    assert report["images"]["postgres"]["version"] == "16.8"
    assert report["images"]["postgres"]["major"] == 16
    assert report["images"]["redis"]["version"] == "7.4.2"
    assert report["images"]["redis"]["major"] == 7
    assert report["checks"] == {
        "postgres_role_constraints": True,
        "postgres_restart_persistence": True,
        "redis_aof_restart_persistence": True,
    }


def test_report_has_no_secret_fields_or_secret_derived_metadata(tmp_path: Path) -> None:
    output = tmp_path / "release-gate.json"
    images = _release_images()
    write_release_evidence(
        output,
        commit=COMMIT,
        trivy_image=TRIVY_IMAGE,
        images=images,
        scan_reports=_scan_reports(tmp_path, images),
        metadata=_metadata(tmp_path),
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    forbidden = {"secret", "password", "token", "credential", "length", "key_hash"}

    def collect_keys(value: object) -> set[str]:
        if isinstance(value, dict):
            return set(value) | {nested for item in value.values() for nested in collect_keys(item)}
        if isinstance(value, list):
            return {nested for item in value for nested in collect_keys(item)}
        return set()

    assert not ({key.casefold() for key in collect_keys(report)} & forbidden)


@pytest.mark.fault_injection
def test_atomic_report_failure_preserves_previous_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "release-gate.json"
    previous = b'{"passed":true,"previous":true}\n'
    output.write_bytes(previous)
    output.chmod(0o600)
    images = _release_images()

    def fail_replace(source: str | Path, target: str | Path) -> None:
        raise OSError("injected evidence replace failure")

    monkeypatch.setattr(evidence_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="injected"):
        write_release_evidence(
            output,
            commit=COMMIT,
            trivy_image=TRIVY_IMAGE,
            images=images,
            scan_reports=_scan_reports(tmp_path, images),
            metadata=_metadata(tmp_path),
        )

    assert output.read_bytes() == previous
    assert not list(tmp_path.glob(".release-gate.json.*.tmp"))


def test_report_path_must_be_absolute(tmp_path: Path) -> None:
    with pytest.raises(ReleaseEvidenceError, match="absolute"):
        write_data_image_evidence(
            Path("data-images.json"),
            commit=COMMIT,
            images=_data_images(),
        )


@pytest.mark.parametrize(
    ("parser_name", "official_output", "expected"),
    [
        ("parse_postgres_version_output", "postgres (PostgreSQL) 16.8\n", ("16.8", 16)),
        (
            "parse_redis_version_output",
            "Redis server v=7.4.2 sha=00000000:0 malloc=jemalloc-5.3.0 bits=64 build=abcdef12\n",
            ("7.4.2", 7),
        ),
    ],
)
def test_official_data_image_versions_are_strictly_parsed(
    parser_name: str,
    official_output: str,
    expected: tuple[str, int],
) -> None:
    parser = getattr(evidence_module, parser_name)

    assert parser(official_output) == expected


@pytest.mark.parametrize(
    ("parser_name", "invalid_output"),
    [
        ("parse_postgres_version_output", ""),
        ("parse_postgres_version_output", "psql (PostgreSQL) 16.8"),
        (
            "parse_postgres_version_output",
            "postgres (PostgreSQL) 16.8\npostgres (PostgreSQL) 16.8",
        ),
        ("parse_redis_version_output", "redis-server 7.4.2"),
        (
            "parse_redis_version_output",
            "Redis server v=7.4.2 sha=bad malloc=jemalloc bits=64 build=abcdef12",
        ),
        (
            "parse_redis_version_output",
            "Redis server v=7.4.2 sha=00000000:0 malloc=jemalloc-5.3.0 "
            "bits=64 build=abcdef12\nRedis server v=7.4.2 sha=00000000:0 "
            "malloc=jemalloc-5.3.0 bits=64 build=abcdef12",
        ),
    ],
)
def test_data_image_versions_reject_missing_ambiguous_or_nonofficial_output(
    parser_name: str,
    invalid_output: str,
) -> None:
    parser = getattr(evidence_module, parser_name)

    with pytest.raises(ReleaseEvidenceError, match="version output"):
        parser(invalid_output)


def test_release_report_requires_digest_pinned_trivy_image(tmp_path: Path) -> None:
    images = _release_images()
    with pytest.raises(ReleaseEvidenceError, match="digest pinned"):
        write_release_evidence(
            tmp_path / "release-gate.json",
            commit=COMMIT,
            trivy_image="aquasec/trivy:0.70.0@sha256:short",
            images=images,
            scan_reports=_scan_reports(tmp_path, images),
            metadata=_metadata(tmp_path),
        )


def test_release_report_requires_matching_zero_finding_trivy_proof(tmp_path: Path) -> None:
    images = _release_images()

    with pytest.raises(ReleaseEvidenceError, match="Trivy scan"):
        write_release_evidence(
            tmp_path / "release-gate.json",
            commit=COMMIT,
            trivy_image=TRIVY_IMAGE,
            images=images,
            scan_reports=_scan_reports(tmp_path, images, vulnerable="api"),
            metadata=_metadata(tmp_path),
        )


def test_release_report_accepts_only_the_expected_archive_scan_identity(
    tmp_path: Path,
) -> None:
    images = _release_images()
    reports = _scan_reports(tmp_path, images)
    for name, path in reports.items():
        report = json.loads(path.read_text(encoding="utf-8"))
        report["ArtifactName"] = f"/scan/{name}.tar"
        path.write_text(json.dumps(report), encoding="utf-8")

    write_release_evidence(
        tmp_path / "archive-release-gate.json",
        commit=COMMIT,
        trivy_image=TRIVY_IMAGE,
        images=images,
        scan_reports=reports,
        metadata=_metadata(tmp_path),
    )

    report = json.loads(reports["api"].read_text(encoding="utf-8"))
    report["ArtifactName"] = "/scan/other.tar"
    reports["api"].write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ReleaseEvidenceError, match="not bound"):
        write_release_evidence(
            tmp_path / "wrong-archive-release-gate.json",
            commit=COMMIT,
            trivy_image=TRIVY_IMAGE,
            images=images,
            scan_reports=reports,
            metadata=_metadata(tmp_path),
        )


def test_release_report_rejects_trivy_proof_without_vulnerability_targets(
    tmp_path: Path,
) -> None:
    images = _release_images()
    reports = _scan_reports(tmp_path, images)
    empty = json.loads(reports["api"].read_text(encoding="utf-8"))
    empty["Results"] = []
    reports["api"].write_text(json.dumps(empty), encoding="utf-8")

    with pytest.raises(ReleaseEvidenceError, match="Trivy scan"):
        write_release_evidence(
            tmp_path / "release-gate.json",
            commit=COMMIT,
            trivy_image=TRIVY_IMAGE,
            images=images,
            scan_reports=reports,
            metadata=_metadata(tmp_path),
        )


def test_promoted_release_requires_same_image_ids_as_candidate_build_report(
    tmp_path: Path,
) -> None:
    candidate_images = _release_images()
    source = tmp_path / "candidate-build-gate.json"
    write_release_evidence(
        source,
        commit=COMMIT,
        trivy_image=TRIVY_IMAGE,
        images=candidate_images,
        scan_reports=_scan_reports(tmp_path, candidate_images),
        metadata=_metadata(tmp_path),
    )
    promoted_images = {
        name: {
            **image,
            "ref": f"registry.example.com/sms/{name}@sha256:" + character * 64,
            "repo_digests": f"registry.example.com/sms/{name}@sha256:" + character * 64,
        }
        for (name, image), character in zip(
            candidate_images.items(),
            ("1", "2", "3", "4"),
            strict=True,
        )
    }

    output = tmp_path / "promoted-release-gate.json"
    write_promoted_release_evidence(
        output,
        commit=COMMIT,
        images=promoted_images,
        promotion_source=source,
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["promotion_source"]["candidate_commit"] == COMMIT
    assert all(
        report["promotion_source"]["images"][name]["image_id"] == report["images"][name]["image_id"]
        for name in promoted_images
    )

    wrong_digest_binding = {name: dict(image) for name, image in promoted_images.items()}
    wrong_digest_binding["web"]["repo_digests"] = "registry.example.com/sms/web@sha256:" + "8" * 64
    with pytest.raises(ReleaseEvidenceError, match="RepoDigest"):
        write_promoted_release_evidence(
            tmp_path / "unbound-digest-release-gate.json",
            commit=COMMIT,
            images=wrong_digest_binding,
            promotion_source=source,
        )

    promoted_images["api"]["image_id"] = "sha256:" + "9" * 64
    with pytest.raises(ReleaseEvidenceError, match="promotion source"):
        write_promoted_release_evidence(
            tmp_path / "mismatched-release-gate.json",
            commit=COMMIT,
            images=promoted_images,
            promotion_source=source,
        )


def test_control_smoke_report_is_atomic_private_and_not_a_release_scan(
    tmp_path: Path,
) -> None:
    output = tmp_path / "release-control-smoke.json"

    evidence_module.write_release_control_smoke_evidence(
        output,
        commit=COMMIT,
        images=_control_smoke_images(),
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["schema_version"] == 1
    assert report["gate_type"] == "release_control_smoke"
    assert report["candidate_commit"] == COMMIT
    assert report["generated_at"].endswith("Z")
    assert report["purpose"] == "release_control_failure_injection"
    assert report["scan_performed"] is False
    assert report["authorized_for_control_smoke"] is True
    assert set(report["images"]) == {"api", "web", "postgres", "redis"}
    assert all(image["platform"] == "linux/amd64" for image in report["images"].values())
    assert "passed" not in report
    assert "trivy_image" not in report
    assert not any("scan_passed" in image for image in report["images"].values())


def test_control_smoke_report_rejects_non_amd64_or_unexpected_refs(
    tmp_path: Path,
) -> None:
    images = _control_smoke_images()
    images["api"]["platform"] = "linux/arm64"

    with pytest.raises(ReleaseEvidenceError, match="control smoke"):
        evidence_module.write_release_control_smoke_evidence(
            tmp_path / "release-control-smoke.json",
            commit=COMMIT,
            images=images,
        )
