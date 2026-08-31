from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from deploy_release_remote import (  # noqa: E402
    RemoteReleaseError,
    RemoteTarget,
    build_release_plan,
    deploy_release,
    main,
)

COMMIT = "c" * 40
IMAGE_ID = "sha256:" + "a" * 64


def _bundle(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    archive = bundle / "web.tar"
    archive.write_bytes(b"verified-development-archive")
    archive_sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    release_report = {
        "schema_version": 1,
        "gate_type": "release",
        "candidate_commit": COMMIT,
        "source": {
            "app_version": "1.6.0",
            "git_sha": COMMIT,
            "schema_revision": "0012",
            "openapi_sha256": "9" * 64,
            "workflow_repository": "local",
            "workflow_run_id": 0,
            "workflow_run_attempt": 0,
            "sbom_sha256": {
                name: "8" * 64 for name in ("api", "web", "postgres", "redis")
            },
        },
        "generated_at": "2026-07-14T08:00:00Z",
        "trivy_image": "aquasec/trivy:0.70.0@sha256:" + "d" * 64,
        "images": {
            name: {
                "ref": f"sms-platform-{name}:candidate",
                "image_id": IMAGE_ID,
                "repo_digests": [],
                "scan_report_sha256": "e" * 64,
                "scan_passed": True,
            }
            for name in ("api", "web", "postgres", "redis")
        },
        "promotion_source": None,
        "passed": True,
    }
    release_report_path = bundle / "release-gate.json"
    release_report_path.write_text(
        json.dumps(release_report),
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 1,
        "release_id": "release-20260714",
        "commit": COMMIT,
        "mode": "development",
        "images": {
            name: {
                "ref": f"sms-platform-{name}:candidate",
                "id": IMAGE_ID,
                "archive_file": "web.tar" if name == "web" else None,
                "archive_sha256": archive_sha if name == "web" else None,
                "changed": name == "web",
            }
            for name in ("api", "web", "postgres", "redis")
        },
        "migration": {"from": "0011", "target": "0012", "compatibility": "expand"},
        "evidence": {
            "release_gate_kind": "release",
            "release_gate": "release-gate.json",
            "release_gate_sha256": hashlib.sha256(
                release_report_path.read_bytes()
            ).hexdigest(),
            "data_images": None,
            "backup_restore_change": None,
        },
    }
    manifest_path = bundle / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path, manifest


def _offline_bundle(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    bundle = tmp_path / "offline-bundle"
    bundle.mkdir()
    archive_metadata: dict[str, tuple[str, int]] = {}
    for name in ("api", "web", "postgres", "redis"):
        archive = bundle / f"{name}.tar"
        archive.write_bytes(f"verified-production-{name}-archive".encode())
        archive_metadata[name] = (
            hashlib.sha256(archive.read_bytes()).hexdigest(),
            archive.stat().st_size,
        )
    (bundle / "manifest.sig").write_bytes(b"test-ed25519-signature")
    release_report = {
        "schema_version": 1,
        "gate_type": "release",
        "candidate_commit": COMMIT,
        "source": {
            "app_version": "1.6.0",
            "git_sha": COMMIT,
            "schema_revision": "0012",
            "openapi_sha256": "9" * 64,
            "workflow_repository": "example/enterprise-sms-platform",
            "workflow_run_id": 123,
            "workflow_run_attempt": 1,
            "sbom_sha256": {name: "8" * 64 for name in ("api", "web", "postgres", "redis")},
        },
        "generated_at": "2026-08-26T08:00:00Z",
        "trivy_image": "aquasec/trivy:0.70.0@sha256:" + "d" * 64,
        "images": {
            name: {
                "ref": f"sms-platform-release-{name}:{COMMIT}",
                "image_id": IMAGE_ID,
                "repo_digests": [],
                "scan_report_sha256": "e" * 64,
                "scan_passed": True,
            }
            for name in ("api", "web", "postgres", "redis")
        },
        "promotion_source": None,
        "passed": True,
    }
    release_report_path = bundle / "release-gate.json"
    release_report_path.write_text(json.dumps(release_report), encoding="utf-8")
    offline_index = {
        "schema_version": 1,
        "kind": "production_offline_image_index",
        "candidate_commit": COMMIT,
        "release_gate": {
            "file": "release-gate.json",
            "sha256": hashlib.sha256(release_report_path.read_bytes()).hexdigest(),
            "size": release_report_path.stat().st_size,
        },
        "reproducibility": {
            "file": "reproducibility.json",
            "sha256": "7" * 64,
            "size": 1,
        },
        "images": {
            name: {
                "image_id": IMAGE_ID,
                "archive": {
                    "file": f"images/{name}.tar",
                    "sha256": archive_metadata[name][0],
                    "size": archive_metadata[name][1],
                },
                "scan": {
                    "file": f"scans/{name}.json",
                    "sha256": "6" * 64,
                    "size": 1,
                },
                "sbom": {
                    "candidate": {
                        "file": f"sboms/{name}.cdx.json",
                        "sha256": "5" * 64,
                        "size": 1,
                    },
                    "rebuild": {
                        "file": f"sboms/{name}.rebuild.cdx.json",
                        "sha256": "4" * 64,
                        "size": 1,
                    },
                },
            }
            for name in ("api", "web", "postgres", "redis")
        },
    }
    offline_index_path = bundle / "offline-image-index.json"
    offline_index_path.write_text(json.dumps(offline_index), encoding="utf-8")
    manifest = {
        "schema_version": 2,
        "release_id": "release-20260826-offline",
        "commit": COMMIT,
        "mode": "production",
        "image_source": "production-offline-docker-archive-v1",
        "signing": {
            "algorithm": "ed25519",
            "key_id": "sms-prod-2026",
            "file": "manifest.sig",
        },
        "images": {
            name: {
                "ref": IMAGE_ID,
                "id": IMAGE_ID,
                "archive_file": f"{name}.tar",
                "archive_sha256": archive_metadata[name][0],
                "archive_size": archive_metadata[name][1],
                "changed": False,
            }
            for name in ("api", "web", "postgres", "redis")
        },
        "migration": {"from": "0012", "target": "0012", "compatibility": "none"},
        "evidence": {
            "release_gate_kind": "release",
            "release_gate": "release-gate.json",
            "release_gate_sha256": hashlib.sha256(release_report_path.read_bytes()).hexdigest(),
            "offline_image_index": {
                "file": "offline-image-index.json",
                "sha256": hashlib.sha256(offline_index_path.read_bytes()).hexdigest(),
            },
            "data_images": None,
            "backup_restore_change": None,
        },
    }
    manifest_path = bundle / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path, manifest


def _registry_bundle(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    manifest_path, manifest = _bundle(tmp_path)
    (manifest_path.parent / "web.tar").unlink()
    source = {
        "app_version": "1.6.0",
        "git_sha": COMMIT,
        "schema_revision": "0012",
        "openapi_sha256": "9" * 64,
        "workflow_repository": "example/enterprise-sms-platform",
        "workflow_run_id": 123,
        "workflow_run_attempt": 1,
        "sbom_sha256": {
            name: "8" * 64 for name in ("api", "web", "postgres", "redis")
        },
    }
    refs = {
        name: f"registry.example.com/sms/{name}@sha256:{digest * 64}"
        for name, digest in zip(
            ("api", "web", "postgres", "redis"),
            ("a", "b", "c", "d"),
            strict=True,
        )
    }
    report = {
        "schema_version": 1,
        "gate_type": "release",
        "candidate_commit": COMMIT,
        "source": source,
        "generated_at": "2026-08-26T08:00:00Z",
        "trivy_image": "aquasec/trivy:0.70.0@sha256:" + "d" * 64,
        "images": {
            name: {
                "ref": refs[name],
                "image_id": IMAGE_ID,
                "repo_digests": [refs[name]],
                "scan_report_sha256": "e" * 64,
                "scan_passed": True,
            }
            for name in ("api", "web", "postgres", "redis")
        },
        "promotion_source": {
            "report_sha256": "f" * 64,
            "candidate_commit": COMMIT,
            "source": source,
            "images": {
                name: {
                    "ref": f"sms-platform-release-{name}:{COMMIT}",
                    "image_id": IMAGE_ID,
                    "scan_report_sha256": "e" * 64,
                }
                for name in ("api", "web", "postgres", "redis")
            },
        },
        "passed": True,
    }
    report_path = manifest_path.parent / "release-gate.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    manifest["mode"] = "production"
    manifest["migration"] = {
        "from": "0012",
        "target": "0012",
        "compatibility": "none",
    }
    manifest["images"] = {
        name: {
            "ref": refs[name],
            "id": IMAGE_ID,
            "archive_file": None,
            "archive_sha256": None,
            "changed": False,
        }
        for name in ("api", "web", "postgres", "redis")
    }
    manifest["evidence"]["release_gate_sha256"] = hashlib.sha256(
        report_path.read_bytes()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path, manifest


def _arguments(manifest: Path) -> argparse.Namespace:
    return argparse.Namespace(
        manifest=manifest,
        target=RemoteTarget(
            host="release.example.com",
            port=22,
            user="smsdeploy",
            platform_root="/opt/sms-platform",
            secrets_mode="development",
            runtime_root="/home/smsdeploy/.cache/sms-platform/releases",
        ),
        remote_ref="origin/main",
        public_urls=("https://sms.example.com/readyz",),
        dry_run=False,
        stage_only=False,
    )


def _offline_arguments(manifest: Path) -> argparse.Namespace:
    arguments = _arguments(manifest)
    arguments.target = RemoteTarget(
        host="release.example.com",
        port=22,
        user="smsdeploy",
        platform_root="/opt/sms-platform",
        secrets_mode="production",
        runtime_root="/home/smsdeploy/.cache/sms-platform/releases",
    )
    arguments.public_urls = ()
    arguments.stage_only = True
    return arguments


def _registry_arguments(manifest: Path) -> argparse.Namespace:
    arguments = _arguments(manifest)
    arguments.target = RemoteTarget(
        host="release.example.com",
        port=22,
        user="smsdeploy",
        platform_root="/opt/sms-platform",
        secrets_mode="production",
        runtime_root="/home/smsdeploy/.cache/sms-platform/releases",
    )
    arguments.public_urls = ()
    return arguments


def _bind_evidence(path: Path) -> dict[str, object]:
    return {
        "file": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size": path.stat().st_size,
    }


class FakeRunner:
    def __init__(self, manifest: dict[str, Any]) -> None:
        self.manifest = manifest
        self.calls: list[list[str]] = []
        self.remote_hashes: dict[str, str] = {}
        self.remote_metadata: dict[str, tuple[str, str, str, str]] = {}
        self.fail_prepare = False
        self.host_control_failure: str | None = None
        self.host_control_overrides: dict[str, dict[str, Any]] = {}
        self.host_control_active_commit = "b" * 40
        self.rsync_failures_remaining = 0
        self.remote_head = "b" * 40
        self.remote_ref_commit = COMMIT
        self.rollback_ref: str | None = None
        self.vendor_agent_changed = True
        self.fail_vendor_agent_restart = False
        self.fail_vendor_agent_active_check = False
        self.public_probe_body = '{"status":"ready"}\n'

    def run(
        self,
        argv: Sequence[str],
        *,
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command = [str(item) for item in argv]
        self.calls.append(command)
        if command[:3] == ["git", "status", "--porcelain"]:
            return self._result(command)
        if command[:2] == ["git", "rev-parse"]:
            return self._result(command, stdout=COMMIT + "\n")
        if command[:2] == ["git", "merge-base"]:
            return self._result(command)
        if command[:3] == ["docker", "image", "inspect"]:
            return self._result(command, stdout=f"{IMAGE_ID} linux/amd64\n")
        if command[0] == "rsync":
            if self.rsync_failures_remaining > 0:
                self.rsync_failures_remaining -= 1
                return self._result(command, returncode=255)
            source = Path(command[-2])
            remote_part = command[-1].split(":", 1)[1]
            self.remote_hashes[remote_part] = hashlib.sha256(source.read_bytes()).hexdigest()
            return self._result(command)
        if command[0] == "curl":
            return self._result(command, stdout=self.public_probe_body)
        if command[0] != "ssh":
            raise AssertionError(f"unexpected command: {command[0]}")
        remote = command[8:]
        if remote[:2] == ["test", "-e"]:
            return self._result(command, returncode=0 if remote[2] in self.remote_hashes else 1)
        if remote[:2] == ["sha256sum", "--"]:
            path = remote[2]
            digest = self.remote_hashes[path]
            return self._result(command, stdout=f"{digest}  {path}\n")
        if remote and remote[0] == "find":
            bundle = remote[1]
            records = [("", "d", "smsdeploy", "700", "2")]
            for path in sorted(self.remote_hashes):
                item = Path(path)
                if str(item.parent) != bundle:
                    continue
                kind, owner, mode, links = self.remote_metadata.get(
                    path,
                    ("f", "smsdeploy", "600", "1"),
                )
                records.append((item.name, kind, owner, mode, links))
            output = "".join("\0".join(record) + "\0" for record in records)
            return self._result(command, stdout=output)
        if remote[:2] == ["mv", "--"]:
            source, destination = remote[2:]
            self.remote_hashes[destination] = self.remote_hashes.pop(source)
            return self._result(command)
        if remote[:2] == ["rm", "--"]:
            self.remote_hashes.pop(remote[2], None)
            return self._result(command)
        if remote[:2] == ["rmdir", "--"]:
            return self._result(command)
        if remote[:4] == ["git", "-C", "/opt/sms-platform", "status"]:
            return self._result(command)
        if remote[:4] == ["git", "-C", "/opt/sms-platform", "rev-parse"]:
            ref = remote[-1]
            if ref == "HEAD":
                value = self.remote_head
            elif ref == "origin/main":
                value = self.remote_ref_commit
            else:
                value = self.rollback_ref or ""
            return self._result(command, returncode=0 if value else 1, stdout=value + "\n")
        if remote[:4] == ["git", "-C", "/opt/sms-platform", "show-ref"]:
            return self._result(command, returncode=0 if self.rollback_ref else 1)
        if remote[:4] == ["git", "-C", "/opt/sms-platform", "branch"]:
            self.rollback_ref = self.remote_head
            return self._result(command)
        if remote[:4] == ["git", "-C", "/opt/sms-platform", "merge"]:
            self.remote_head = COMMIT
            return self._result(command)
        if remote[:4] == ["git", "-C", "/opt/sms-platform", "diff"]:
            return self._result(command, returncode=1 if self.vendor_agent_changed else 0)
        if remote == [
            "sudo",
            "--",
            "/usr/bin/systemctl",
            "restart",
            "vendor-control-agent.service",
        ]:
            return self._result(
                command,
                returncode=1 if self.fail_vendor_agent_restart else 0,
            )
        if remote[:3] == ["systemctl", "is-active", "--quiet"]:
            return self._result(
                command,
                returncode=(
                    1
                    if remote[3] == "vendor-control-agent.service"
                    and self.fail_vendor_agent_active_check
                    else 0
                ),
            )
        lifecycle_prefix = [
            "sudo",
            "--",
            "/usr/bin/env",
            "SMS_SECRETS_MODE=development",
            "/usr/local/sbin/sms-compose",
        ]
        if remote[:5] == lifecycle_prefix:
            if "prepare" in remote and self.fail_prepare:
                return self._result(command, returncode=1)
            if "status" in remote:
                return self._result(command, stdout='{"state":"succeeded"}\n')
            return self._result(command)
        production_prefix = [
            "sudo",
            "--",
            "/usr/bin/env",
            "SMS_SECRETS_MODE=production",
            "/usr/local/sbin/sms-compose",
        ]
        if remote[:5] == production_prefix:
            entrypoint = remote[5]
            operation = remote[6]
            if entrypoint == "host-control":
                if self.host_control_failure == operation:
                    return self._result(command, returncode=1)
                if operation == "activate":
                    self.host_control_active_commit = COMMIT
                response = self.host_control_overrides.get(operation)
                if response is None:
                    active = operation != "prepare"
                    response = {
                        "bytes": 4096,
                        "commit": (self.host_control_active_commit if active else COMMIT),
                        "current_target": (
                            f"versions/{self.host_control_active_commit}" if active else None
                        ),
                        "files": 12,
                        "status": "active" if active else "prepared",
                        "tree": "f" * 40,
                    }
                return self._result(command, stdout=json.dumps(response) + "\n")
            if entrypoint == "release":
                if operation == "prepare" and self.fail_prepare:
                    return self._result(command, returncode=1)
                if operation == "status":
                    return self._result(command, stdout='{"state":"succeeded"}\n')
                return self._result(command)
        return self._result(command)

    @staticmethod
    def _result(
        command: list[str],
        *,
        returncode: int = 0,
        stdout: str = "",
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, returncode, stdout, "")


def test_plan_uses_fixed_argv_and_single_remote_actions(tmp_path: Path) -> None:
    manifest_path, _ = _bundle(tmp_path)

    plan = build_release_plan(_arguments(manifest_path))

    assert ["git", "status", "--porcelain", "--untracked-files=normal"] in plan
    assert any(command[:3] == ["docker", "image", "inspect"] for command in plan)
    ssh_actions = [command[8:] for command in plan if command[0] == "ssh"]
    assert any(action[:5] == ["install", "-d", "-m", "0700", "--"] for action in ssh_actions)
    assert any(action[:2] == ["sha256sum", "--"] for action in ssh_actions)
    assert any(action[:2] == ["mv", "--"] for action in ssh_actions)
    assert all(";" not in token and "&&" not in token for action in ssh_actions for token in action)
    assert any("show-ref" in action for action in ssh_actions)
    assert not any("--force" in action for action in ssh_actions)
    rsync = next(command for command in plan if command[0] == "rsync")
    assert "--partial" in rsync
    assert "--chmod=Fu=rw,Fgo=" in rsync
    assert rsync[-1].endswith(".part")
    lifecycle_actions = [
        action for action in ssh_actions if "/usr/local/sbin/sms-compose" in action
    ]
    assert lifecycle_actions
    assert all(
        action[:5]
        == [
            "sudo",
            "--",
            "/usr/bin/env",
            "SMS_SECRETS_MODE=development",
            "/usr/local/sbin/sms-compose",
        ]
        for action in lifecycle_actions
    )


def test_offline_stage_only_uploads_closed_bundle_without_docker_or_lifecycle(
    tmp_path: Path,
) -> None:
    manifest_path, manifest = _offline_bundle(tmp_path)
    for image in manifest["images"].values():
        image["changed"] = True
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    arguments = _offline_arguments(manifest_path)
    runner = FakeRunner(manifest)

    plan = build_release_plan(arguments)
    deploy_release(arguments, runner)

    assert not any(command[:3] == ["docker", "image", "inspect"] for command in plan)
    assert not any(command[:3] == ["docker", "image", "inspect"] for command in runner.calls)
    planned_remote = [command[8:] for command in plan if command[0] == "ssh"]
    executed_remote = [command[8:] for command in runner.calls if command[0] == "ssh"]
    assert not any(action[:2] == ["git", "-C"] for action in planned_remote)
    assert not any("/usr/local/sbin/sms-compose" in action for action in planned_remote)
    assert not any(action[:2] == ["git", "-C"] for action in executed_remote)
    assert not any("/usr/local/sbin/sms-compose" in action for action in executed_remote)
    assert not any(action[:2] == ["rm", "--"] for action in executed_remote)
    uploaded_names = {
        Path(command[-1].split(":", 1)[1]).name.removesuffix(".part")
        for command in runner.calls
        if command[0] == "rsync"
    }
    assert uploaded_names == {
        "manifest.json",
        "manifest.sig",
        "release-gate.json",
        "offline-image-index.json",
        "api.tar",
        "web.tar",
        "postgres.tar",
        "redis.tar",
    }
    find_command = next(
        command[8:]
        for command in runner.calls
        if command[0] == "ssh" and command[8:9] == ["find"]
    )
    reparsed = shlex.split(" ".join(find_command))
    assert reparsed[-2:] == ["-printf", r"%P\0%y\0%u\0%m\0%n\0"]


def test_offline_full_update_uploads_and_verifies_bound_conditional_evidence(
    tmp_path: Path,
) -> None:
    manifest_path, manifest = _offline_bundle(tmp_path)
    bundle = manifest_path.parent
    data = bundle / "data-images.json"
    backup = bundle / "backup-change.json"
    restore = bundle / "restore-report.json"
    data.write_text('{"passed":true}', encoding="utf-8")
    backup.write_text('{"backup":true}', encoding="utf-8")
    restore.write_text('{"restored":true}', encoding="utf-8")
    for image in manifest["images"].values():
        image["changed"] = True
    manifest["evidence"]["data_images"] = _bind_evidence(data)
    manifest["evidence"]["backup_restore_change"] = {
        "record": _bind_evidence(backup),
        "restore_report": _bind_evidence(restore),
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    plan = build_release_plan(_offline_arguments(manifest_path))

    uploaded_names = {
        Path(command[-1].split(":", 1)[1]).name.removesuffix(".part")
        for command in plan
        if command[0] == "rsync"
    }
    assert {data.name, backup.name, restore.name} <= uploaded_names

    data.write_text('{"passed":false}', encoding="utf-8")
    with pytest.raises(RemoteReleaseError, match="data image evidence"):
        build_release_plan(_offline_arguments(manifest_path))


def test_stage_only_is_restricted_to_production_offline_bundle(tmp_path: Path) -> None:
    manifest_path, _ = _bundle(tmp_path)
    arguments = _arguments(manifest_path)
    arguments.stage_only = True
    arguments.public_urls = ()

    with pytest.raises(RemoteReleaseError, match="production offline"):
        build_release_plan(arguments)


def test_registry_production_keeps_promotion_and_local_docker_contract(
    tmp_path: Path,
) -> None:
    manifest_path, _ = _registry_bundle(tmp_path)
    arguments = _registry_arguments(manifest_path)

    plan = build_release_plan(arguments)

    assert sum(command[:3] == ["docker", "image", "inspect"] for command in plan) == 4
    remote_actions = [command[8:] for command in plan if command[0] == "ssh"]
    host_prefix = [
        "sudo",
        "--",
        "/usr/bin/env",
        "SMS_SECRETS_MODE=production",
        "/usr/local/sbin/sms-compose",
        "host-control",
    ]
    host_prepare = [*host_prefix, "prepare", "--expected-commit", COMMIT]
    host_activate = [*host_prefix, "activate", "--expected-commit", COMMIT]
    host_status = [*host_prefix, "status"]
    fast_forward = ["git", "-C", "/opt/sms-platform", "merge", "--ff-only", COMMIT]
    rollback_check = [
        "git",
        "-C",
        "/opt/sms-platform",
        "show-ref",
        "--verify",
        "--quiet",
        "refs/heads/release-rollback/release-20260714",
    ]
    release_prepare_index = next(
        index
        for index, action in enumerate(remote_actions)
        if action[:7]
        == [
            "sudo",
            "--",
            "/usr/bin/env",
            "SMS_SECRETS_MODE=production",
            "/usr/local/sbin/sms-compose",
            "release",
            "prepare",
        ]
    )
    assert (
        remote_actions.index(host_prepare)
        < remote_actions.index(rollback_check)
        < remote_actions.index(fast_forward)
        < remote_actions.index(host_activate)
        < remote_actions.index(host_status)
        < release_prepare_index
    )
    arguments.stage_only = True
    with pytest.raises(RemoteReleaseError, match="production offline"):
        build_release_plan(arguments)


def test_registry_deploy_attests_host_control_before_release_lifecycle(
    tmp_path: Path,
) -> None:
    manifest_path, manifest = _registry_bundle(tmp_path)
    runner = FakeRunner(manifest)

    deploy_release(_registry_arguments(manifest_path), runner)

    remote_actions = [command[8:] for command in runner.calls if command[0] == "ssh"]
    host_actions = [
        (index, action)
        for index, action in enumerate(remote_actions)
        if action[5:6] == ["host-control"]
    ]
    assert [action[6] for _, action in host_actions] == ["prepare", "activate", "status"]
    host_prepare_index, host_activate_index, host_status_index = (
        index for index, _ in host_actions
    )
    rollback_index = next(
        index
        for index, action in enumerate(remote_actions)
        if action[:4] == ["git", "-C", "/opt/sms-platform", "branch"]
    )
    fast_forward_index = remote_actions.index(
        ["git", "-C", "/opt/sms-platform", "merge", "--ff-only", COMMIT]
    )
    release_prepare_index = next(
        index
        for index, action in enumerate(remote_actions)
        if action[5:7] == ["release", "prepare"]
    )
    assert (
        host_prepare_index
        < rollback_index
        < fast_forward_index
        < host_activate_index
        < host_status_index
        < release_prepare_index
    )


@pytest.mark.parametrize(
    ("operation", "fast_forwarded"),
    [("prepare", False), ("activate", True), ("status", True)],
)
def test_registry_host_control_failure_retains_bundle_and_stops_release(
    tmp_path: Path,
    operation: str,
    fast_forwarded: bool,
) -> None:
    manifest_path, manifest = _registry_bundle(tmp_path)
    runner = FakeRunner(manifest)
    runner.host_control_failure = operation

    with pytest.raises(RemoteReleaseError, match=f"host-control {operation}"):
        deploy_release(_registry_arguments(manifest_path), runner)

    remote_actions = [command[8:] for command in runner.calls if command[0] == "ssh"]
    assert any(path.endswith("/manifest.json") for path in runner.remote_hashes)
    assert not any(action[:2] == ["rm", "--"] for action in remote_actions)
    assert not any(action[5:6] == ["release"] for action in remote_actions)
    assert (
        ["git", "-C", "/opt/sms-platform", "merge", "--ff-only", COMMIT] in remote_actions
    ) is fast_forwarded


def test_registry_host_control_identity_drift_stops_before_release(
    tmp_path: Path,
) -> None:
    manifest_path, manifest = _registry_bundle(tmp_path)
    runner = FakeRunner(manifest)
    runner.host_control_overrides["activate"] = {
        "bytes": 4096,
        "commit": COMMIT,
        "current_target": "versions/" + "d" * 40,
        "files": 12,
        "status": "active",
        "tree": "f" * 40,
    }

    with pytest.raises(RemoteReleaseError, match="identity"):
        deploy_release(_registry_arguments(manifest_path), runner)

    remote_actions = [command[8:] for command in runner.calls if command[0] == "ssh"]
    assert not any(action[5:6] == ["release"] for action in remote_actions)
    assert not any(action[:2] == ["rm", "--"] for action in remote_actions)


def test_registry_retry_reuses_bundle_and_rollback_ref_after_host_status_failure(
    tmp_path: Path,
) -> None:
    manifest_path, manifest = _registry_bundle(tmp_path)
    runner = FakeRunner(manifest)
    runner.host_control_failure = "status"

    with pytest.raises(RemoteReleaseError, match="host-control status"):
        deploy_release(_registry_arguments(manifest_path), runner)

    first_call_count = len(runner.calls)
    original_rollback = runner.rollback_ref
    runner.host_control_failure = None
    deploy_release(_registry_arguments(manifest_path), runner)

    retry_calls = runner.calls[first_call_count:]
    retry_remote = [command[8:] for command in retry_calls if command[0] == "ssh"]
    assert original_rollback == "b" * 40
    assert runner.rollback_ref == original_rollback
    assert not any(command[0] == "rsync" for command in retry_calls)
    assert not any(
        action[:4] == ["git", "-C", "/opt/sms-platform", "branch"] for action in retry_remote
    )
    assert [action[6] for action in retry_remote if action[5:6] == ["host-control"]] == [
        "prepare",
        "activate",
        "status",
    ]
    assert any(action[5:7] == ["release", "prepare"] for action in retry_remote)


def test_offline_archive_size_must_match_before_upload(tmp_path: Path) -> None:
    manifest_path, manifest = _offline_bundle(tmp_path)
    manifest["images"]["web"]["archive_size"] += 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RemoteReleaseError, match="archive does not match"):
        build_release_plan(_offline_arguments(manifest_path))


def test_offline_release_evidence_uses_non_registry_candidate_ref(
    tmp_path: Path,
) -> None:
    manifest_path, manifest = _offline_bundle(tmp_path)
    evidence_path = manifest_path.parent / "release-gate.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["images"]["api"]["ref"] = IMAGE_ID
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    manifest["evidence"]["release_gate_sha256"] = hashlib.sha256(
        evidence_path.read_bytes()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RemoteReleaseError, match="not bound"):
        build_release_plan(_offline_arguments(manifest_path))


def test_offline_image_index_hash_is_bound_by_manifest(tmp_path: Path) -> None:
    manifest_path, _ = _offline_bundle(tmp_path)
    index_path = manifest_path.parent / "offline-image-index.json"
    index_path.write_bytes(index_path.read_bytes() + b"\n")

    with pytest.raises(RemoteReleaseError, match="index hash"):
        build_release_plan(_offline_arguments(manifest_path))


def test_offline_image_index_archive_metadata_is_bound_to_manifest(
    tmp_path: Path,
) -> None:
    manifest_path, manifest = _offline_bundle(tmp_path)
    index_path = manifest_path.parent / "offline-image-index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["images"]["api"]["archive"]["size"] += 1
    index_path.write_text(json.dumps(index), encoding="utf-8")
    manifest["evidence"]["offline_image_index"]["sha256"] = hashlib.sha256(
        index_path.read_bytes()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RemoteReleaseError, match="image is not bound"):
        build_release_plan(_offline_arguments(manifest_path))


def test_development_dry_run_exposes_fixed_vendor_agent_reload_stage(
    tmp_path: Path,
) -> None:
    manifest_path, _ = _bundle(tmp_path)

    plan = build_release_plan(_arguments(manifest_path))

    remote_actions = [command[8:] for command in plan if command[0] == "ssh"]
    diff = next(
        action
        for action in remote_actions
        if action[:4] == ["git", "-C", "/opt/sms-platform", "diff"]
    )
    assert diff[4:6] == ["--name-only", "refs/heads/release-rollback/release-20260714"]
    assert diff[-6:] == [
        "deploy/scripts/vendor_control_agent.py",
        "deploy/scripts/vendor_control_journal.py",
        "deploy/scripts/vendor_control_protocol.py",
        "deploy/scripts/vendor_credential_store.py",
        "deploy/scripts/vendor_control_reload.py",
        "deploy/scripts/vendor_seal_sessions.py",
    ]
    status_index = next(i for i, action in enumerate(remote_actions) if "status" in action)
    restart_index = remote_actions.index(
        [
            "sudo",
            "--",
            "/usr/bin/systemctl",
            "restart",
            "vendor-control-agent.service",
        ]
    )
    active_index = remote_actions.index(
        ["systemctl", "is-active", "--quiet", "vendor-control-agent.service"]
    )
    assert status_index < restart_index < active_index


def test_rsync_upload_uses_macos_openrsync_compatible_argv(tmp_path: Path) -> None:
    manifest_path, _ = _bundle(tmp_path)

    plan = build_release_plan(_arguments(manifest_path))

    rsync = next(command for command in plan if command[0] == "rsync")
    assert "--protect-args" not in rsync
    assert "--chmod=F600" not in rsync
    assert "--inplace" in rsync
    assert "--chmod=Fu=rw,Fgo=" in rsync


def test_upload_retries_transient_rsync_failures_with_fixed_partial_file(
    tmp_path: Path,
) -> None:
    manifest_path, manifest = _bundle(tmp_path)
    runner = FakeRunner(manifest)
    runner.rsync_failures_remaining = 2

    deploy_release(_arguments(manifest_path), runner)

    rsync_calls = [command for command in runner.calls if command[0] == "rsync"]
    assert all("--inplace" in command for command in rsync_calls)
    retried_partial = rsync_calls[0][-1]
    retried_calls = [command for command in rsync_calls if command[-1] == retried_partial]
    assert len(retried_calls) == 3
    assert retried_partial.endswith(".part")


def test_upload_stops_after_bounded_rsync_failures(tmp_path: Path) -> None:
    manifest_path, manifest = _bundle(tmp_path)
    runner = FakeRunner(manifest)
    runner.rsync_failures_remaining = 3

    with pytest.raises(RemoteReleaseError, match="release bundle upload failed"):
        deploy_release(_arguments(manifest_path), runner)

    rsync_calls = [command for command in runner.calls if command[0] == "rsync"]
    assert len(rsync_calls) == 3
    assert len({command[-1] for command in rsync_calls}) == 1
    assert not any("sha256sum" in command[8:] for command in runner.calls if command[0] == "ssh")


def test_staging_root_is_secured_before_the_release_directory(tmp_path: Path) -> None:
    manifest_path, manifest = _bundle(tmp_path)

    plan = build_release_plan(_arguments(manifest_path))

    remote_actions = [command[8:] for command in plan if command[0] == "ssh"]
    root_install = [
        "install",
        "-d",
        "-m",
        "0700",
        "--",
        "/home/smsdeploy/.cache/sms-platform/releases",
    ]
    bundle_install = [
        "install",
        "-d",
        "-m",
        "0700",
        "--",
        "/home/smsdeploy/.cache/sms-platform/releases/release-20260714",
    ]
    assert root_install in remote_actions
    assert bundle_install in remote_actions
    assert remote_actions.index(root_install) < remote_actions.index(bundle_install)

    runner = FakeRunner(manifest)
    deploy_release(_arguments(manifest_path), runner)
    executed_actions = [command[8:] for command in runner.calls if command[0] == "ssh"]
    assert root_install in executed_actions
    assert bundle_install in executed_actions
    assert executed_actions.index(root_install) < executed_actions.index(bundle_install)


def test_deploy_runs_git_upload_release_entrypoint_probe_and_cleanup(
    tmp_path: Path,
) -> None:
    manifest_path, manifest = _bundle(tmp_path)
    runner = FakeRunner(manifest)

    deploy_release(_arguments(manifest_path), runner)

    calls = runner.calls
    assert ["git", "rev-parse", "origin/main"] in calls
    assert ["git", "merge-base", "--is-ancestor", COMMIT, "origin/main"] in calls
    remote_actions = [command[8:] for command in calls if command[0] == "ssh"]
    assert [
        "git",
        "-C",
        "/opt/sms-platform",
        "fetch",
        "--prune",
        "origin",
        "refs/heads/main:refs/remotes/origin/main",
    ] in remote_actions
    assert ["git", "-C", "/opt/sms-platform", "merge", "--ff-only", COMMIT] in remote_actions
    prepare_index = next(i for i, action in enumerate(remote_actions) if "prepare" in action)
    cleanup_index = next(i for i, action in enumerate(remote_actions) if action[:2] == ["rm", "--"])
    activate_index = next(i for i, action in enumerate(remote_actions) if "activate" in action)
    assert prepare_index < cleanup_index < activate_index
    assert any(
        action[:5]
        == [
            "sudo",
            "--",
            "/usr/bin/env",
            "SMS_SECRETS_MODE=development",
            "/usr/local/sbin/sms-compose",
        ]
        for action in remote_actions
    )
    assert ["systemctl", "is-active", "--quiet", "sms-platform.service"] in remote_actions
    assert remote_actions[-1] == [
        "git",
        "-C",
        "/opt/sms-platform",
        "status",
        "--porcelain",
        "--untracked-files=normal",
    ]
    assert any(command[0] == "curl" for command in calls)


def test_public_probe_rejects_spa_html_even_when_http_status_is_successful(
    tmp_path: Path,
) -> None:
    manifest_path, manifest = _bundle(tmp_path)
    runner = FakeRunner(manifest)
    runner.public_probe_body = "<!doctype html><html></html>\n"

    with pytest.raises(RemoteReleaseError, match="readiness response"):
        deploy_release(_arguments(manifest_path), runner)

    assert any(command[0] == "curl" for command in runner.calls)


def test_development_release_restarts_changed_vendor_agent_after_succeeded_status(
    tmp_path: Path,
) -> None:
    manifest_path, manifest = _bundle(tmp_path)
    runner = FakeRunner(manifest)

    deploy_release(_arguments(manifest_path), runner)

    remote_actions = [command[8:] for command in runner.calls if command[0] == "ssh"]
    status_index = next(i for i, action in enumerate(remote_actions) if "status" in action)
    restart = [
        "sudo",
        "--",
        "/usr/bin/systemctl",
        "restart",
        "vendor-control-agent.service",
    ]
    restart_index = remote_actions.index(restart)
    active_index = remote_actions.index(
        ["systemctl", "is-active", "--quiet", "vendor-control-agent.service"]
    )
    assert status_index < restart_index < active_index


def test_development_release_skips_vendor_agent_restart_when_runtime_is_unchanged(
    tmp_path: Path,
) -> None:
    manifest_path, manifest = _bundle(tmp_path)
    runner = FakeRunner(manifest)
    runner.vendor_agent_changed = False

    deploy_release(_arguments(manifest_path), runner)

    remote_actions = [command[8:] for command in runner.calls if command[0] == "ssh"]
    assert not any(
        action[-2:] == ["restart", "vendor-control-agent.service"] for action in remote_actions
    )
    assert ["systemctl", "is-active", "--quiet", "vendor-control-agent.service"] not in (
        remote_actions
    )


@pytest.mark.parametrize("failure", ["restart", "active"])
def test_vendor_agent_reload_failure_stops_before_public_probe(
    tmp_path: Path,
    failure: str,
) -> None:
    manifest_path, manifest = _bundle(tmp_path)
    runner = FakeRunner(manifest)
    runner.fail_vendor_agent_restart = failure == "restart"
    runner.fail_vendor_agent_active_check = failure == "active"

    with pytest.raises(RemoteReleaseError, match="vendor control agent"):
        deploy_release(_arguments(manifest_path), runner)

    assert not any(command[0] == "curl" for command in runner.calls)
    assert (
        sum(
            command[8:12] == ["git", "-C", "/opt/sms-platform", "status"]
            and command[8:][-1] == "--untracked-files=normal"
            for command in runner.calls
            if command[0] == "ssh"
        )
        == 1
    )


def test_retry_uses_persistent_rollback_ref_to_reload_agent_after_git_already_switched(
    tmp_path: Path,
) -> None:
    manifest_path, manifest = _bundle(tmp_path)
    runner = FakeRunner(manifest)
    runner.remote_head = COMMIT
    runner.rollback_ref = "b" * 40

    deploy_release(_arguments(manifest_path), runner)

    remote_actions = [command[8:] for command in runner.calls if command[0] == "ssh"]
    diff = next(
        action
        for action in remote_actions
        if action[:4] == ["git", "-C", "/opt/sms-platform", "diff"]
    )
    assert "b" * 40 in diff
    assert [
        "sudo",
        "--",
        "/usr/bin/systemctl",
        "restart",
        "vendor-control-agent.service",
    ] in remote_actions


def test_remote_fetches_exact_approved_ref_for_single_branch_clone(
    tmp_path: Path,
) -> None:
    manifest_path, _ = _bundle(tmp_path)
    arguments = _arguments(manifest_path)
    arguments.remote_ref = "origin/codex/release-candidate"

    plan = build_release_plan(arguments)

    remote_actions = [command[8:] for command in plan if command[0] == "ssh"]
    assert [
        "git",
        "-C",
        "/opt/sms-platform",
        "fetch",
        "--prune",
        "origin",
        "refs/heads/codex/release-candidate:refs/remotes/origin/codex/release-candidate",
    ] in remote_actions


def test_remote_ref_must_resolve_exactly_to_manifest_commit(tmp_path: Path) -> None:
    manifest_path, manifest = _bundle(tmp_path)
    runner = FakeRunner(manifest)
    runner.remote_ref_commit = "d" * 40

    with pytest.raises(RemoteReleaseError, match="remote-ref"):
        deploy_release(_arguments(manifest_path), runner)

    assert not any("merge" in command[8:] for command in runner.calls if command[0] == "ssh")


def test_remote_git_outputs_must_be_exact_commit_hashes(tmp_path: Path) -> None:
    manifest_path, manifest = _bundle(tmp_path)
    runner = FakeRunner(manifest)
    runner.remote_head = "head;touch-pwned"

    with pytest.raises(RemoteReleaseError, match="remote HEAD"):
        deploy_release(_arguments(manifest_path), runner)

    assert not any("head;touch-pwned" in token for command in runner.calls for token in command)


def test_retry_preserves_original_remote_rollback_ref(tmp_path: Path) -> None:
    manifest_path, manifest = _bundle(tmp_path)
    runner = FakeRunner(manifest)
    original = "b" * 40
    runner.remote_head = COMMIT
    runner.rollback_ref = original

    deploy_release(_arguments(manifest_path), runner)

    assert runner.rollback_ref == original
    remote_actions = [command[8:] for command in runner.calls if command[0] == "ssh"]
    assert not any(
        action[:4] == ["git", "-C", "/opt/sms-platform", "branch"] for action in remote_actions
    )


def test_existing_identical_remote_files_are_idempotently_skipped(tmp_path: Path) -> None:
    manifest_path, manifest = _bundle(tmp_path)
    runner = FakeRunner(manifest)
    remote_bundle = "/home/smsdeploy/.cache/sms-platform/releases/release-20260714"
    for path in manifest_path.parent.iterdir():
        runner.remote_hashes[f"{remote_bundle}/{path.name}"] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()

    deploy_release(_arguments(manifest_path), runner)

    assert not any(command[0] == "rsync" for command in runner.calls)


def test_stage_only_rejects_extra_remote_staging_file(tmp_path: Path) -> None:
    manifest_path, manifest = _offline_bundle(tmp_path)
    runner = FakeRunner(manifest)
    remote_bundle = (
        "/home/smsdeploy/.cache/sms-platform/releases/release-20260826-offline"
    )
    runner.remote_hashes[f"{remote_bundle}/stale.part"] = "0" * 64

    with pytest.raises(RemoteReleaseError, match="closed file set"):
        deploy_release(_offline_arguments(manifest_path), runner)


def test_stage_only_rejects_unsafe_remote_file_metadata(tmp_path: Path) -> None:
    manifest_path, manifest = _offline_bundle(tmp_path)
    runner = FakeRunner(manifest)
    remote_manifest = (
        "/home/smsdeploy/.cache/sms-platform/releases/"
        "release-20260826-offline/manifest.json"
    )
    runner.remote_metadata[remote_manifest] = ("f", "smsdeploy", "644", "1")

    with pytest.raises(RemoteReleaseError, match="metadata"):
        deploy_release(_offline_arguments(manifest_path), runner)


def test_existing_mismatched_remote_file_is_rejected_without_hash_disclosure(
    tmp_path: Path,
) -> None:
    manifest_path, manifest = _bundle(tmp_path)
    runner = FakeRunner(manifest)
    remote_manifest = "/home/smsdeploy/.cache/sms-platform/releases/release-20260714/manifest.json"
    runner.remote_hashes[remote_manifest] = "0" * 64

    with pytest.raises(RemoteReleaseError) as error:
        deploy_release(_arguments(manifest_path), runner)

    message = str(error.value)
    assert "0" * 64 not in message
    assert not any(
        command[0] == "rsync" and command[-1].endswith("manifest.json.part")
        for command in runner.calls
    )


def test_prepare_failure_retains_remote_staging_and_skips_activation(tmp_path: Path) -> None:
    manifest_path, manifest = _bundle(tmp_path)
    runner = FakeRunner(manifest)
    runner.fail_prepare = True

    with pytest.raises(RemoteReleaseError, match="prepare"):
        deploy_release(_arguments(manifest_path), runner)

    remote_actions = [command[8:] for command in runner.calls if command[0] == "ssh"]
    assert not any(action[:2] == ["rm", "--"] for action in remote_actions)
    assert not any("activate" in action for action in remote_actions)


def test_closed_bundle_rejects_extra_directory_and_symlink(tmp_path: Path) -> None:
    manifest_path, _ = _bundle(tmp_path)
    (manifest_path.parent / "extra.txt").write_text("extra", encoding="utf-8")
    with pytest.raises(RemoteReleaseError, match="closed"):
        build_release_plan(_arguments(manifest_path))

    (manifest_path.parent / "extra.txt").unlink()
    (manifest_path.parent / "link.json").symlink_to("release-gate.json")
    with pytest.raises(RemoteReleaseError, match="closed"):
        build_release_plan(_arguments(manifest_path))


def test_archive_must_match_manifest_without_disclosing_digest(tmp_path: Path) -> None:
    manifest_path, _ = _bundle(tmp_path)
    (manifest_path.parent / "web.tar").write_bytes(b"changed-after-manifest")

    with pytest.raises(RemoteReleaseError) as error:
        build_release_plan(_arguments(manifest_path))

    assert "sha256" not in str(error.value).casefold()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("host", "host;id"),
        ("user", "root $(id)"),
        ("platform_root", "/opt/../platform"),
        ("runtime_root", "relative/runtime"),
        ("runtime_root", "/opt/sms-platform/staging"),
        ("port", 0),
    ],
)
def test_remote_target_rejects_unsafe_values(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    manifest_path, _ = _bundle(tmp_path)
    arguments = _arguments(manifest_path)
    values = vars(arguments.target) | {field: value}
    arguments.target = RemoteTarget(**values)

    with pytest.raises(RemoteReleaseError):
        build_release_plan(arguments)


def test_remote_release_public_probe_requires_readyz(tmp_path: Path) -> None:
    manifest_path, _ = _bundle(tmp_path)
    arguments = _arguments(manifest_path)
    arguments.public_urls = ("https://sms.example.com/healthz",)

    with pytest.raises(RemoteReleaseError, match="readyz"):
        build_release_plan(arguments)


@pytest.mark.parametrize(
    "body",
    [
        '{"status":"ok"}\n',
        '{"status":"alive"}\n',
        '{"status":"not_ready"}\n',
        '{"status":"ready","detail":"x"}\n',
    ],
)
def test_public_readyz_probe_rejects_non_ready_bodies(
    tmp_path: Path,
    body: str,
) -> None:
    manifest_path, manifest = _bundle(tmp_path)
    runner = FakeRunner(manifest)
    runner.public_probe_body = body

    with pytest.raises(RemoteReleaseError, match="readiness"):
        deploy_release(_arguments(manifest_path), runner)


def test_public_readyz_probe_accepts_ready_body(tmp_path: Path) -> None:
    manifest_path, manifest = _bundle(tmp_path)
    runner = FakeRunner(manifest)

    deploy_release(_arguments(manifest_path), runner)

    assert any(
        command[0] == "curl" and command[-1] == "https://sms.example.com/readyz"
        for command in runner.calls
    )


def test_registry_dry_run_previews_redacted_host_control_sequence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest_path, _ = _registry_bundle(tmp_path)

    result = main(
        [
            "--manifest",
            str(manifest_path),
            "--host",
            "release.example.com",
            "--user",
            "smsdeploy",
            "--platform-root",
            "/opt/sms-platform",
            "--runtime-root",
            "/home/smsdeploy/.cache/sms-platform/releases",
            "--mode",
            "production",
            "--dry-run",
        ]
    )

    commands = [shlex.split(line) for line in capsys.readouterr().out.splitlines()]
    remote_actions = [command[8:] for command in commands if command[0] == "ssh"]
    assert result == 0
    assert COMMIT not in "\n".join(" ".join(command) for command in commands)
    assert [
        (action[6], action[7:]) for action in remote_actions if action[5:6] == ["host-control"]
    ] == [
        ("prepare", ["--expected-commit", "<redacted>"]),
        ("activate", ["--expected-commit", "<redacted>"]),
        ("status", []),
    ]


def test_dry_run_redacts_commit_image_and_transfer_hashes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest_path, manifest = _bundle(tmp_path)

    result = main(
        [
            "--manifest",
            str(manifest_path),
            "--host",
            "release.example.com",
            "--user",
            "smsdeploy",
            "--platform-root",
            "/opt/sms-platform",
            "--runtime-root",
            "/home/smsdeploy/.cache/sms-platform/releases",
            "--mode",
            "development",
            "--public-url",
            "https://sms.example.com/readyz",
            "--dry-run",
        ]
    )

    output = capsys.readouterr().out
    assert result == 0
    assert COMMIT not in output
    assert IMAGE_ID not in output
    assert manifest["images"]["web"]["archive_sha256"] not in output
