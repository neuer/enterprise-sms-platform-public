from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]


def load_script(name: str) -> ModuleType:
    path = ROOT / "scripts" / f"{name}.py"
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_public_policy_excludes_internal_evidence_but_keeps_source_contracts() -> None:
    module = load_script("check_public_readiness")
    policy = module.load_policy(ROOT / "public-repository.json")

    for path in (
        "docs/plans/example.md",
        "docs/reports/result.json",
        "docs/test-evidence/page.txt",
        "docs/TEST-REPORT-2026-07-14.md",
        "docs/UAT-report.md",
    ):
        assert module.is_excluded(path, policy)
    for path in (
        "backend/app/main.py",
        "deploy/docker-compose.yml",
        "docs/vendor-api.md",
        "openapi.yaml",
    ):
        assert not module.is_excluded(path, policy)


def test_public_readiness_gate_passes_current_publishable_tree() -> None:
    module = load_script("check_public_readiness")
    policy = module.load_policy(ROOT / "public-repository.json")

    assert module.check_repository(ROOT, policy=policy) == []


def test_public_readiness_gate_blocks_sensitive_publication_documents(
    tmp_path: Path,
) -> None:
    module = load_script("check_public_readiness")
    policy = module.load_policy(ROOT / "public-repository.json")
    for required in policy.required_public_files:
        target = tmp_path / required
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("safe\n", encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    unsafe = docs / "manual.md"
    unsafe.write_text(
        "host: " + "8.8." + "8.8\n"
        "phone: 13912345678\n"
        "password: " + "Dev@" + "00000\n"
        "owner: person" + "@real-company" + ".com\n",
        encoding="utf-8",
    )

    findings = module.check_repository(tmp_path, policy=policy)

    assert any("globally routable IPv4" in finding for finding in findings)
    assert any("full mobile number" in finding for finding in findings)
    assert any("legacy mock password" in finding for finding in findings)
    assert any("non-example email address" in finding for finding in findings)
    assert all("13912345678" not in finding for finding in findings)


def test_public_readiness_gate_blocks_unapproved_operational_url_hosts(
    tmp_path: Path,
) -> None:
    module = load_script("check_public_readiness")
    policy = module.load_policy(ROOT / "public-repository.json")
    for required in policy.required_public_files:
        target = tmp_path / required
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("safe\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "service: https://" + "gateway.real-company" + ".com\n",
        encoding="utf-8",
    )

    findings = module.check_repository(tmp_path, policy=policy)

    assert any("URL host is not approved" in finding for finding in findings)


def test_sensitive_local_paths_are_never_publishable_git_paths() -> None:
    module = load_script("check_public_readiness")

    for path in (
        ".env",
        ".env.production",
        "deploy/secrets/token",
        "deploy/security-report/config/recipients.txt",
        "certificates/client.key",
    ):
        assert module._is_forbidden_tracked_path(path)
    assert not module._is_forbidden_tracked_path("deploy/.env.example")


def test_snapshot_mode_requires_provenance_and_absence_of_private_paths(
    tmp_path: Path,
) -> None:
    module = load_script("check_public_readiness")
    policy = module.load_policy(ROOT / "public-repository.json")
    for required in policy.required_public_files:
        target = tmp_path / required
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("safe\n", encoding="utf-8")
    internal = tmp_path / "docs/plans/private.md"
    internal.parent.mkdir(parents=True)
    internal.write_text("internal\n", encoding="utf-8")

    findings = module.check_repository(tmp_path, policy=policy, snapshot=True)

    assert any("snapshot provenance is missing" in finding for finding in findings)
    assert any("private-only path is present" in finding for finding in findings)

    internal.unlink()
    internal.parent.rmdir()
    (tmp_path / "PUBLIC-SNAPSHOT.json").write_text(
        json.dumps({"history_included": False}),
        encoding="utf-8",
    )
    assert module.check_repository(tmp_path, policy=policy, snapshot=True) == []


def test_exporter_requires_full_commit_shape_without_accepting_abbreviations() -> None:
    module = load_script("export_public_snapshot")

    assert module.re_full_sha("a" * 40)
    assert not module.re_full_sha("a" * 39)
    assert not module.re_full_sha("g" * 40)


def test_private_only_document_tests_skip_when_public_artifacts_are_absent() -> None:
    for relative in (
        "backend/tests/test_deployment_docs.py",
        "backend/tests/test_test_update_workflow_docs.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "private-only" in source
        assert "pytest.skip(" in source
        assert ".is_file()" in source
