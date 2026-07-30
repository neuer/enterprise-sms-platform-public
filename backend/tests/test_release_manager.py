from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))

from release_manager import (  # noqa: E402
    ReconciliationDecision,
    ReleaseManager,
    ReleaseManagerError,
    ReleaseState,
    RuntimeObservation,
    reconcile_release,
)
from release_manifest import load_manifest  # noqa: E402

COMMIT = "c" * 40
IMAGE_NAMES = ("api", "web", "postgres", "redis")
CONTROL_SMOKE_IMAGES = {
    "api": {
        "ref": "sms-platform-api:amd64-ffcecbe",
        "id": "sha256:07f1deaea83a50ac7d44d872f0748be523bc9edfa641d97565979d5031980c39",
    },
    "web": {
        "ref": "sms-platform-web:amd64-ffcecbe",
        "id": "sha256:804a1d8dc488b27535b45d15d7abee81b8e95ca3300b1d8939de23309af17d46",
    },
    "postgres": {
        "ref": "sms-platform-postgres:amd64-ffcecbe",
        "id": "sha256:4e9aa6c3ed14ac7d2f56a960066617bac46461a996bdd426b3c375b6fdfccb81",
    },
    "redis": {
        "ref": "sms-platform-redis:amd64-ffcecbe",
        "id": "sha256:ab4439eedeb2e9c742b5e3b087a269d95a98bb61eaaaaa25f75b93989fd2bf51",
    },
}
RUNTIME_SERVICES = (
    "api",
    "web",
    "postgres",
    "redis",
    "redis-auth",
    "redis-control",
    "worker-realtime",
    "worker-bulk",
    "worker-callback",
    "outbox-dispatcher",
    "beat",
)
WORKER_SERVICES = ("worker-realtime", "worker-bulk", "worker-callback")


def _image_id(name: str) -> str:
    return "sha256:" + dict(api="a", web="b", postgres="d", redis="e")[name] * 64


def _service_image_name(service: str) -> str:
    if service in {"redis-auth", "redis-control"}:
        return "redis"
    return service if service in IMAGE_NAMES else "api"


def _write_private_json(path: Path, value: object) -> None:
    if (
        path.name == "manifest.json"
        and type(value) is dict
        and type(value.get("evidence")) is dict
    ):
        report_name = value["evidence"].get("release_gate")
        report_path = path.parent / str(report_name)
        if report_path.is_file():
            report = json.loads(report_path.read_text(encoding="utf-8"))
            source = report.get("source")
            if type(source) is dict:
                source["schema_revision"] = value["migration"]["target"]
                promotion = report.get("promotion_source")
                if type(promotion) is dict and type(promotion.get("source")) is dict:
                    promotion["source"]["schema_revision"] = value["migration"]["target"]
                report_path.write_text(json.dumps(report), encoding="utf-8")
                report_path.chmod(0o600)
            value["evidence"]["release_gate_sha256"] = hashlib.sha256(
                report_path.read_bytes()
            ).hexdigest()
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


def _write_bound_release_report(
    path: Path,
    manifest: dict[str, Any],
    report: dict[str, Any],
) -> None:
    _write_private_json(path, report)
    manifest["evidence"]["release_gate_sha256"] = hashlib.sha256(
        path.read_bytes()
    ).hexdigest()


def _release_report(manifest: dict[str, Any]) -> dict[str, Any]:
    production = manifest["mode"] == "production"
    source = {
        "app_version": "1.6.0",
        "git_sha": COMMIT,
        "schema_revision": manifest["migration"]["target"],
        "openapi_sha256": "9" * 64,
        "workflow_repository": (
            "example/enterprise-sms-platform" if production else "local"
        ),
        "workflow_run_id": 123 if production else 0,
        "workflow_run_attempt": 1 if production else 0,
        "sbom_sha256": {name: "8" * 64 for name in IMAGE_NAMES},
    }
    return {
        "schema_version": 1,
        "gate_type": "release",
        "candidate_commit": COMMIT,
        "source": source,
        "generated_at": "2026-07-14T07:00:00Z",
        "trivy_image": "aquasec/trivy:0.70.0@sha256:" + "f" * 64,
        "images": {
            name: {
                "ref": manifest["images"][name]["ref"],
                "image_id": manifest["images"][name]["id"],
                "repo_digests": ([manifest["images"][name]["ref"]] if production else []),
                "scan_report_sha256": "f" * 64,
                "scan_passed": True,
            }
            for name in IMAGE_NAMES
        },
        "promotion_source": (
            {
                "report_sha256": "a" * 64,
                "candidate_commit": COMMIT,
                "source": source,
                "images": {
                    name: {
                        "ref": f"sms-platform-release-{name}:{COMMIT}",
                        "image_id": manifest["images"][name]["id"],
                        "scan_report_sha256": "b" * 64,
                    }
                    for name in IMAGE_NAMES
                },
            }
            if production
            else None
        ),
        "passed": True,
    }


def _control_smoke_report(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "gate_type": "release_control_smoke",
        "candidate_commit": COMMIT,
        "generated_at": "2026-07-15T07:00:00Z",
        "purpose": "release_control_failure_injection",
        "scan_performed": False,
        "authorized_for_control_smoke": True,
        "images": {
            name: {
                "ref": manifest["images"][name]["ref"],
                "image_id": manifest["images"][name]["id"],
                "platform": "linux/amd64",
            }
            for name in IMAGE_NAMES
        },
    }


def _data_report(manifest: dict[str, Any], *, postgres_major: int = 16) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "gate_type": "data_images",
        "candidate_commit": COMMIT,
        "generated_at": "2026-07-14T07:05:00Z",
        "images": {
            "postgres": {
                "ref": manifest["images"]["postgres"]["ref"],
                "image_id": manifest["images"]["postgres"]["id"],
                "platform": "linux/amd64",
                "version": f"{postgres_major}.8",
                "major": postgres_major,
            },
            "redis": {
                "ref": manifest["images"]["redis"]["ref"],
                "image_id": manifest["images"]["redis"]["id"],
                "platform": "linux/amd64",
                "version": "7.4.2",
                "major": 7,
            },
        },
        "checks": {
            "postgres_role_constraints": True,
            "postgres_restart_persistence": True,
            "redis_aof_restart_persistence": True,
        },
        "passed": True,
    }


def _restore_report() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "success",
        "snapshot_id": "snapshot-20260714",
        "git_commit": COMMIT,
        "database": "sms_drill_release_20260714",
        "started_at": "2026-07-14T07:10:00+00:00",
        "finished_at": "2026-07-14T07:20:00+00:00",
        "restore_seconds": 600.0,
        "rto_limit_seconds": 1800.0,
        "within_rto": True,
        "checks": {
            "alembic_version": "0011",
            "role_flags": "false|false|false",
            "audit_privileges": "true|false|false",
        },
        "table_counts": {"sms_batch": 10, "audit_log": 20, "raw_vendor_log": 30},
    }


def _production_change_record(manifest: dict[str, Any], report_path: Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "record_type": "postgres_backup_restore_change",
        "change_id": "CHG-20260714-001",
        "release_id": manifest["release_id"],
        "target_commit": COMMIT,
        "target_postgres_image_id": manifest["images"]["postgres"]["id"],
        "approval": {
            "status": "approved",
            "approved_by": "dba01",
            "approved_at": "2026-07-14T08:00:00+00:00",
        },
        "restore": {
            "snapshot_id": "snapshot-20260714",
            "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        },
    }


def _bundle(
    tmp_path: Path,
    *,
    mode: str = "development",
    postgres_changed: bool = False,
) -> tuple[Path, dict[str, Any], dict[str, str]]:
    runtime_root = tmp_path / "staging"
    runtime_root.mkdir(mode=0o700)
    runtime_root.chmod(0o700)
    bundle = runtime_root / "release-20260714"
    bundle.mkdir(mode=0o700)
    bundle.chmod(0o700)
    if mode == "production":
        refs = {
            name: f"registry.example.com/sms/{name}@sha256:"
            + dict(api="1", web="2", postgres="3", redis="4")[name] * 64
            for name in IMAGE_NAMES
        }
    else:
        refs = {name: f"sms-platform-{name}:candidate" for name in IMAGE_NAMES}
    changed = {name: name == "web" for name in IMAGE_NAMES}
    if postgres_changed:
        changed["web"] = False
        changed["postgres"] = True
    images: dict[str, dict[str, Any]] = {}
    for name in IMAGE_NAMES:
        archive_file = f"{name}.tar" if mode == "development" and changed[name] else None
        archive_sha: str | None = None
        if archive_file is not None:
            archive_path = bundle / archive_file
            archive_path.write_bytes(f"verified-{name}-archive".encode())
            archive_path.chmod(0o600)
            archive_sha = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        images[name] = {
            "ref": refs[name],
            "id": _image_id(name),
            "archive_file": archive_file,
            "archive_sha256": archive_sha,
            "changed": changed[name],
        }
    data_name = "data-images.json" if postgres_changed else None
    backup = (
        {"record": "backup-change.json", "restore_report": "restore-report.json"}
        if mode == "production" and postgres_changed
        else None
    )
    manifest = {
        "schema_version": 1,
        "release_id": "release-20260714",
        "commit": COMMIT,
        "mode": mode,
        "images": images,
        "migration": {"from": "0011", "target": "0012", "compatibility": "expand"},
        "evidence": {
            "release_gate_kind": "release",
            "release_gate": "release-gate.json",
            "release_gate_sha256": "0" * 64,
            "data_images": data_name,
            "backup_restore_change": backup,
        },
    }
    _write_bound_release_report(
        bundle / "release-gate.json",
        manifest,
        _release_report(manifest),
    )
    if data_name is not None:
        _write_private_json(bundle / data_name, _data_report(manifest))
    if backup is not None:
        report_path = bundle / backup["restore_report"]
        _write_private_json(report_path, _restore_report())
        _write_private_json(
            bundle / backup["record"],
            _production_change_record(manifest, report_path),
        )
    manifest_path = bundle / "manifest.json"
    _write_private_json(manifest_path, manifest)
    current_refs = refs.copy()
    for name in IMAGE_NAMES:
        if changed[name]:
            current_refs[name] = (
                f"registry.example.com/sms/{name}@sha256:" + "9" * 64
                if mode == "production"
                else f"sms-platform-{name}:previous"
            )
    return manifest_path, manifest, current_refs


def _platform(tmp_path: Path, current_refs: dict[str, str]) -> tuple[Path, Path]:
    root = tmp_path / "platform"
    (root / "deploy").mkdir(parents=True)
    (root / "deploy" / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    keys = {
        "api": "SMS_API_IMAGE",
        "web": "SMS_WEB_IMAGE",
        "postgres": "SMS_POSTGRES_IMAGE",
        "redis": "SMS_REDIS_IMAGE",
    }
    (root / ".env").write_text(
        "\n".join(f"{keys[name]}={current_refs[name]}" for name in IMAGE_NAMES) + "\n",
        encoding="utf-8",
    )
    release_root = tmp_path / "release-store"
    release_root.mkdir(mode=0o700)
    release_root.chmod(0o700)
    return root, release_root


class FakeRunner:
    def __init__(
        self,
        manifest: dict[str, Any],
        current_refs: dict[str, str],
    ) -> None:
        self.manifest = manifest
        self.current_refs = current_refs
        self.calls: list[list[str]] = []
        self.git_commit = COMMIT
        self.postgres_version = "postgres (PostgreSQL) 16.8\n"
        self.redis_version = (
            "Redis server v=7.4.2 sha=00000000:0 malloc=jemalloc-5.3.0 bits=64 build=abcdef12\n"
        )
        self.target_id_override: str | None = None
        self.target_repo_digests_override: list[str] | None = None
        self.fail_action: str | None = None
        self.fail_action_number = 1
        self.fail_after_effect = False
        self.fail_compensation = False
        self.migration_head = "0011"
        self.migration_head_after_failed_run: str | None = None
        self.migration_head_after_successful_run: str | None = None
        self.failed_once = False
        self.action_counts: dict[str, int] = {}
        self.runtime_refs = current_refs.copy()
        self.service_running = {service: True for service in RUNTIME_SERVICES}
        self.service_container_ids = {
            service: hashlib.sha256(f"original:{service}".encode()).hexdigest()
            for service in RUNTIME_SERVICES
        }
        self.service_hostnames = {service: f"host-{service}" for service in RUNTIME_SERVICES}
        self.recreate_count = 0
        self.unhealthy_service: str | None = None
        self.missing_worker_ping: str | None = None
        self.after_action: Any = None
        self.after_ping: Any = None

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [str(value) for value in argv]
        self.calls.append(command)
        if command[:4] == ["git", "-C", str(cwd or command[2]), "status"]:
            return self._result(command)
        if command[0] == "git" and "status" in command:
            return self._result(command)
        if command[0] == "git" and "rev-parse" in command:
            return self._result(command, self.git_commit + "\n")
        if command == ["sleep", "31"]:
            return self._result(command)
        if command[:2] == ["docker", "load"]:
            return self._result(command)
        if command[:3] == ["docker", "image", "inspect"]:
            ref = command[-1]
            name = next(name for name in IMAGE_NAMES if self.manifest["images"][name]["ref"] == ref)
            image_id = self.target_id_override or self.manifest["images"][name]["id"]
            digests = (
                self.target_repo_digests_override
                if self.target_repo_digests_override is not None
                else ([ref] if self.manifest["mode"] == "production" else [])
            )
            return self._result(command, f"{image_id} linux/amd64 {json.dumps(digests)}\n")
        if command[:2] == ["docker", "inspect"]:
            container = command[-1]
            service = next(
                (
                    name
                    for name, container_id in self.service_container_ids.items()
                    if container_id == container
                ),
                container.removeprefix("container-"),
            )
            name = _service_image_name(service)
            current_id = (
                self.manifest["images"][name]["id"]
                if self.runtime_refs[name] == self.manifest["images"][name]["ref"]
                else "sha256:" + dict(api="5", web="6", postgres="7", redis="8")[name] * 64
            )
            status = "exited" if not self.service_running[service] else "healthy"
            if service == self.unhealthy_service:
                status = "unhealthy"
            if (
                service in {*WORKER_SERVICES, "beat", "outbox-dispatcher"}
                and service != self.unhealthy_service
            ):
                status = "running" if self.service_running[service] else "exited"
            if "Config.Hostname" in command[-2]:
                return self._result(
                    command,
                    f"{self.service_container_ids[service]} {current_id} "
                    f"{self.runtime_refs[name]} {status} {self.service_hostnames[service]}\n",
                )
            if "Config.Image" in command[-2] and 'index .State "Health"' in command[-2]:
                return self._result(
                    command,
                    f"{current_id} {self.runtime_refs[name]} {status}\n",
                )
            if 'index .State "Health"' in command[-2]:
                identifier = (
                    self.service_container_ids[service] if "{{.Id}}" in command[-2] else current_id
                )
                return self._result(command, f"{identifier} {status}\n")
            return self._result(
                command,
                f"{self.service_container_ids[service]} {current_id} {self.runtime_refs[name]}\n",
            )
        if "compose" in command:
            if command[-2:] == ["config", "--quiet"]:
                return self._result(command)
            if command[-4:-1] == ["ps", "--all", "-q"]:
                service = command[-1]
                return self._result(command, f"{self.service_container_ids[service]}\n")
            if command[-3:-1] == ["ps", "-q"]:
                service = command[-1]
                value = (
                    f"{self.service_container_ids[service]}\n"
                    if self.service_running[service]
                    else ""
                )
                return self._result(command, value)
            if command[-4:] == ["exec", "-T", "postgres", "postgres"]:
                raise AssertionError("version command must include --version")
            if command[-5:] == ["exec", "-T", "postgres", "postgres", "--version"]:
                return self._result(command, self.postgres_version)
            if command[-5:] == ["exec", "-T", "redis", "redis-server", "--version"]:
                return self._result(command, self.redis_version)
            if (
                command[-6:-3] == ["exec", "-T", "postgres"]
                and "SELECT version_num FROM alembic_version" in command[-1]
            ):
                return self._result(command, f"{self.migration_head}\n")
            if command[-8:] == [
                "exec",
                "-T",
                "worker-realtime",
                "celery",
                "-A",
                "app.tasks",
                "inspect",
                "ping",
            ]:
                raise AssertionError("Celery ping must use a fixed timeout and JSON output")
            if command[-11:] == [
                "exec",
                "-T",
                "worker-realtime",
                "celery",
                "-A",
                "app.tasks",
                "inspect",
                "ping",
                "--timeout",
                "10",
                "--json",
            ]:
                if self.after_ping is not None:
                    self.after_ping()
                replies = {
                    f"celery@{self.service_hostnames[service]}": {"ok": "pong"}
                    for service in WORKER_SERVICES
                    if service != self.missing_worker_ping
                }
                return self._result(command, json.dumps(replies) + "\n")
            action = next(
                (value for value in ("stop", "up", "run") if value in command),
                None,
            )
            if action is not None:
                self.action_counts[action] = self.action_counts.get(action, 0) + 1
                should_fail = (
                    self.fail_action == action
                    and not self.failed_once
                    and self.action_counts[action] == self.fail_action_number
                )
                if action == "up" and (not should_fail or self.fail_after_effect):
                    assert cwd is not None
                    env = {
                        line.split("=", 1)[0]: line.split("=", 1)[1]
                        for line in (cwd / ".env").read_text(encoding="utf-8").splitlines()
                        if "=" in line
                    }
                    keys = {
                        "api": "SMS_API_IMAGE",
                        "web": "SMS_WEB_IMAGE",
                        "postgres": "SMS_POSTGRES_IMAGE",
                        "redis": "SMS_REDIS_IMAGE",
                    }
                    services = command[command.index("120") + 1 :]
                    for service in services:
                        image_name = _service_image_name(service)
                        self.runtime_refs[image_name] = env[keys[image_name]]
                        self.service_running[service] = True
                        self.recreate_count += 1
                        self.service_container_ids[service] = hashlib.sha256(
                            f"recreated:{service}:{self.recreate_count}".encode()
                        ).hexdigest()
                if action == "stop" and not should_fail:
                    services = command[command.index("stop") + 1 :]
                    for service in services:
                        self.service_running[service] = False
                if should_fail:
                    if action == "run" and self.migration_head_after_failed_run is not None:
                        self.migration_head = self.migration_head_after_failed_run
                    self.failed_once = True
                    return subprocess.CompletedProcess(command, 1, "", "injected")
                if action == "run":
                    self.migration_head = (
                        self.migration_head_after_successful_run
                        or self.manifest["migration"]["target"]
                    )
                if self.fail_compensation and self.failed_once and action == "up":
                    return subprocess.CompletedProcess(command, 1, "", "compensation")
                if self.after_action is not None:
                    self.after_action(action, self.action_counts[action])
                return self._result(command)
        raise AssertionError(f"unexpected command: {command}")

    @staticmethod
    def _result(command: list[str], stdout: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout, "")


def _manager(
    tmp_path: Path,
    manifest: dict[str, Any],
    current_refs: dict[str, str],
) -> tuple[ReleaseManager, FakeRunner, Path, Path]:
    root, release_root = _platform(tmp_path, current_refs)
    runner = FakeRunner(manifest, current_refs)
    manager = ReleaseManager(
        root=root,
        release_root=release_root,
        mode=manifest["mode"],
        runner=runner,
        expected_staging_uid=os.geteuid(),
    )
    return manager, runner, root, release_root


def _bundle_for_changes(
    tmp_path: Path,
    changed: set[str],
) -> tuple[Path, dict[str, Any], dict[str, str]]:
    manifest_path, manifest, _ = _bundle(tmp_path)
    for archive in manifest_path.parent.glob("*.tar"):
        archive.unlink()
    for name in IMAGE_NAMES:
        image = manifest["images"][name]
        image["changed"] = name in changed
        if name in changed:
            archive = manifest_path.parent / f"{name}.tar"
            archive.write_bytes(f"verified-{name}-archive".encode())
            archive.chmod(0o600)
            image["archive_file"] = archive.name
            image["archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
        else:
            image["archive_file"] = None
            image["archive_sha256"] = None
    data_changed = bool({"postgres", "redis"} & changed)
    manifest["evidence"]["data_images"] = "data-images.json" if data_changed else None
    data_path = manifest_path.parent / "data-images.json"
    if data_changed:
        _write_private_json(data_path, _data_report(manifest))
    elif data_path.exists():
        data_path.unlink()
    _write_bound_release_report(
        manifest_path.parent / "release-gate.json",
        manifest,
        _release_report(manifest),
    )
    _write_private_json(manifest_path, manifest)
    current_refs = {
        name: (
            f"sms-platform-{name}:previous" if name in changed else manifest["images"][name]["ref"]
        )
        for name in IMAGE_NAMES
    }
    return manifest_path, manifest, current_refs


def _control_smoke_bundle(
    tmp_path: Path,
) -> tuple[Path, dict[str, Any], dict[str, str]]:
    manifest_path, manifest, _ = _bundle(tmp_path)
    for name in IMAGE_NAMES:
        manifest["images"][name]["ref"] = CONTROL_SMOKE_IMAGES[name]["ref"]
        manifest["images"][name]["id"] = CONTROL_SMOKE_IMAGES[name]["id"]
        if name != "web":
            manifest["images"][name]["changed"] = False
            manifest["images"][name]["archive_file"] = None
            manifest["images"][name]["archive_sha256"] = None
    manifest["images"]["web"]["changed"] = True
    manifest["evidence"] = {
        "release_gate_kind": "release_control_smoke",
        "release_gate": "release-gate.json",
        "release_gate_sha256": "0" * 64,
        "data_images": None,
        "backup_restore_change": None,
    }
    _write_bound_release_report(
        manifest_path.parent / "release-gate.json",
        manifest,
        _control_smoke_report(manifest),
    )
    _write_private_json(manifest_path, manifest)
    current_refs = {
        name: (
            "sms-platform-web:amd64-previous"
            if name == "web"
            else CONTROL_SMOKE_IMAGES[name]["ref"]
        )
        for name in IMAGE_NAMES
    }
    return manifest_path, manifest, current_refs


def test_prepare_copies_closed_bundle_and_records_safe_prepared_snapshot(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path)
    manager, runner, _, release_root = _manager(tmp_path, manifest, current_refs)

    manager.prepare(manifest_path)

    release_dir = release_root / manifest["release_id"]
    state = manager.status(manifest["release_id"])
    assert state["state"] == "prepared"
    assert stat.S_IMODE((release_dir / "manifest.json").stat().st_mode) == 0o600
    assert (release_dir / "artifacts" / "web.tar").read_bytes() == (
        manifest_path.parent / "web.tar"
    ).read_bytes()
    snapshot = json.loads((release_dir / "current-snapshot.json").read_text(encoding="utf-8"))
    assert set(snapshot) == {
        "current_commit",
        "current_refs",
        "container_ids",
        "image_ids",
        "migration_head",
        "service_container_ids",
        "target_commit",
        "target_image_ids",
    }
    assert snapshot["service_container_ids"] == runner.service_container_ids
    events = [
        json.loads(line)
        for line in (release_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert events
    assert {event["kind"] for event in events} == {"intent", "observation"}
    assert all(event["step"] == "external_command" for event in events)
    assert not any(
        token in command
        for command in runner.calls
        for token in ("up", "down", "stop", "rm", "pull")
    )
    assert any(
        "postgres" in command and "SELECT version_num FROM alembic_version" in command[-1]
        for command in runner.calls
    )
    assert not any(
        command[-5:] == ["exec", "-T", "api", "alembic", "current"]
        for command in runner.calls
    )


def test_release_git_checks_disable_optional_index_refresh(tmp_path: Path) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path)
    manager, runner, root, _ = _manager(tmp_path, manifest, current_refs)

    manager.prepare(manifest_path)

    assert [
        "git",
        "--no-optional-locks",
        "-C",
        str(root),
        "status",
        "--porcelain",
        "--untracked-files=normal",
    ] in runner.calls


def test_prepare_never_rereads_staging_manifest_after_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import release_manager as release_manager_module

    manifest_path, manifest, current_refs = _bundle(tmp_path)
    original_bytes = manifest_path.read_bytes()
    replacement = json.loads(original_bytes)
    replacement["commit"] = "d" * 40
    real_load_manifest = release_manager_module.load_manifest

    def swap_manifest_after_parse(path: Path) -> Any:
        parsed = real_load_manifest(path)
        _write_private_json(path, replacement)
        return parsed

    monkeypatch.setattr(release_manager_module, "load_manifest", swap_manifest_after_parse)
    manager, _, _, release_root = _manager(tmp_path, manifest, current_refs)

    manager.prepare(manifest_path)

    stored = release_root / manifest["release_id"] / "manifest.json"
    assert stored.read_bytes() == original_bytes


def test_identical_repeated_prepare_is_idempotent(tmp_path: Path) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path)
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)

    manager.prepare(manifest_path)
    call_count = len(runner.calls)
    manager.prepare(manifest_path)

    assert len(runner.calls) == call_count


def test_duplicate_release_id_rejects_a_different_manifest(tmp_path: Path) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path)
    manager, _, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    changed_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    changed_manifest["migration"]["target"] = "0013"
    _write_private_json(manifest_path, changed_manifest)

    with pytest.raises(ReleaseManagerError, match="prepare failed"):
        manager.prepare(manifest_path)

    assert manager.status(manifest["release_id"])["state"] == "prepared"


@pytest.mark.parametrize("unsafe", ["extra", "directory", "symlink", "mode", "owner"])
def test_prepare_rejects_open_or_unsafe_staging_bundle(
    tmp_path: Path,
    unsafe: str,
) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path)
    if unsafe == "extra":
        extra = manifest_path.parent / "extra.json"
        extra.write_text("{}", encoding="utf-8")
        extra.chmod(0o600)
    elif unsafe == "directory":
        (manifest_path.parent / "release-gate.json").unlink()
        (manifest_path.parent / "release-gate.json").mkdir()
    elif unsafe == "symlink":
        (manifest_path.parent / "release-gate.json").unlink()
        (manifest_path.parent / "release-gate.json").symlink_to("manifest.json")
    elif unsafe == "mode":
        manifest_path.chmod(0o644)
    manager, _, root, release_root = _manager(tmp_path, manifest, current_refs)
    if unsafe == "owner":
        manager = ReleaseManager(
            root=root,
            release_root=release_root,
            mode=manifest["mode"],
            runner=FakeRunner(manifest, current_refs),
            expected_staging_uid=os.geteuid() + 1,
        )

    with pytest.raises(ReleaseManagerError, match="staging"):
        manager.prepare(manifest_path)


def test_prepare_failure_marks_failed_without_env_or_container_mutation(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path)
    manager, runner, root, release_root = _manager(tmp_path, manifest, current_refs)
    original_env = (root / ".env").read_bytes()
    runner.git_commit = "b" * 40

    with pytest.raises(ReleaseManagerError, match="prepare failed"):
        manager.prepare(manifest_path)

    assert manager.status(manifest["release_id"])["state"] == "failed"
    assert (root / ".env").read_bytes() == original_env
    assert not any(
        token in command
        for command in runner.calls
        for token in ("up", "down", "stop", "rm", "pull")
    )
    assert (release_root / manifest["release_id"] / "artifacts").is_dir()


def test_development_loads_only_changed_archive_and_verifies_target_id(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path)
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)

    manager.prepare(manifest_path)

    loads = [command for command in runner.calls if command[:2] == ["docker", "load"]]
    assert len(loads) == 1
    assert loads[0][-1].endswith("/artifacts/web.tar")


def test_target_image_mismatch_fails_without_lifecycle_mutation(tmp_path: Path) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path)
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    runner.target_id_override = "sha256:" + "9" * 64

    with pytest.raises(ReleaseManagerError, match="prepare failed"):
        manager.prepare(manifest_path)

    assert not any("pull" in command or "up" in command for command in runner.calls)


def test_data_image_major_mismatch_fails_closed_after_fixed_observation(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path, postgres_changed=True)
    report_path = manifest_path.parent / "data-images.json"
    _write_private_json(report_path, _data_report(manifest, postgres_major=17))
    manager, runner, root, _ = _manager(tmp_path, manifest, current_refs)
    original_env = (root / ".env").read_bytes()

    with pytest.raises(ReleaseManagerError, match="prepare failed"):
        manager.prepare(manifest_path)

    compose_prefix = [
        "docker",
        "compose",
        "--env-file",
        str(root / ".env"),
        "-f",
        str(root / "deploy" / "docker-compose.yml"),
    ]
    assert compose_prefix + ["exec", "-T", "postgres", "postgres", "--version"] in runner.calls
    assert compose_prefix + ["exec", "-T", "redis", "redis-server", "--version"] in runner.calls
    assert (root / ".env").read_bytes() == original_env


def test_data_evidence_image_binding_mismatch_is_rejected(tmp_path: Path) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path, postgres_changed=True)
    report_path = manifest_path.parent / "data-images.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["images"]["postgres"]["image_id"] = "sha256:" + "9" * 64
    _write_private_json(report_path, report)
    manager, _, _, _ = _manager(tmp_path, manifest, current_refs)

    with pytest.raises(ReleaseManagerError, match="prepare failed"):
        manager.prepare(manifest_path)


@pytest.mark.parametrize(
    ("name", "version"),
    [("postgres", "16.not-official"), ("redis", "7.4")],
)
def test_data_evidence_rejects_noncanonical_normalized_versions(
    tmp_path: Path,
    name: str,
    version: str,
) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path, postgres_changed=True)
    report_path = manifest_path.parent / "data-images.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["images"][name]["version"] = version
    _write_private_json(report_path, report)
    manager, _, _, _ = _manager(tmp_path, manifest, current_refs)

    with pytest.raises(ReleaseManagerError, match="prepare failed"):
        manager.prepare(manifest_path)


def test_production_postgres_change_accepts_bound_approval_and_restore_report(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle(
        tmp_path,
        mode="production",
        postgres_changed=True,
    )
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)

    manager.prepare(manifest_path)

    assert manager.status(manifest["release_id"])["state"] == "prepared"
    assert not any(command[:2] == ["docker", "load"] for command in runner.calls)
    assert not any("pull" in command for command in runner.calls)


def test_release_gate_bytes_must_match_manifest_hash(tmp_path: Path) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path)
    report_path = manifest_path.parent / "release-gate.json"
    report_path.write_bytes(report_path.read_bytes() + b" ")
    report_path.chmod(0o600)
    manager, _, _, _ = _manager(tmp_path, manifest, current_refs)

    with pytest.raises(ReleaseManagerError, match="prepare failed") as error:
        manager.prepare(manifest_path)

    assert error.value.__cause__ is not None
    assert "hash does not match manifest" in str(error.value.__cause__)


def test_production_requires_preloaded_target_repo_digests(tmp_path: Path) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path, mode="production")
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    runner.target_repo_digests_override = []

    with pytest.raises(ReleaseManagerError, match="prepare failed"):
        manager.prepare(manifest_path)

    assert not any("pull" in command for command in runner.calls)


@pytest.mark.parametrize("source_state", ["missing", "mismatched"])
def test_prepare_rejects_production_release_without_bound_candidate_source(
    tmp_path: Path,
    source_state: str,
) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path, mode="production")
    report_path = manifest_path.parent / "release-gate.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if source_state == "missing":
        report["promotion_source"] = None
    else:
        report["promotion_source"]["images"]["api"]["image_id"] = "sha256:" + "9" * 64
    _write_private_json(report_path, report)
    manager, _, _, _ = _manager(tmp_path, manifest, current_refs)

    with pytest.raises(ReleaseManagerError, match="prepare failed"):
        manager.prepare(manifest_path)


def test_prepare_rejects_control_smoke_without_fully_gated_smoke_context(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _control_smoke_bundle(tmp_path)
    manager, _, _, _ = _manager(tmp_path, manifest, current_refs)

    with pytest.raises(ReleaseManagerError, match="prepare failed"):
        manager.prepare(manifest_path)


def test_prepare_rejects_control_smoke_with_unbound_bundle_report(
    tmp_path: Path,
    smoke_release_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, manifest, current_refs = _control_smoke_bundle(tmp_path)
    report_path = manifest_path.parent / "release-gate.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["images"]["redis"]["platform"] = "linux/arm64"
    _write_private_json(report_path, report)
    runtime_root = smoke_release_root.parent / "runtime-secrets"
    runtime_root.mkdir(mode=0o700)
    runtime_root.chmod(0o700)
    monkeypatch.setenv("SMS_RELEASE_SMOKE", "1")
    monkeypatch.setenv("SMS_RELEASE_ROOT", str(smoke_release_root))
    monkeypatch.setenv("SMS_RUNTIME_ROOT", str(runtime_root))
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", smoke_release_root.parent.name)
    manager, runner, root, _ = _manager(tmp_path, manifest, current_refs)
    manager = ReleaseManager(
        root=root,
        release_root=smoke_release_root,
        mode=manifest["mode"],
        runner=runner,
        expected_staging_uid=os.geteuid(),
    )

    with pytest.raises(ReleaseManagerError, match="prepare failed"):
        manager.prepare(manifest_path)


def test_prepare_accepts_control_smoke_only_with_isolated_smoke_context(
    tmp_path: Path,
    smoke_release_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, manifest, current_refs = _control_smoke_bundle(tmp_path)
    runtime_root = smoke_release_root.parent / "runtime-secrets"
    runtime_root.mkdir(mode=0o700)
    runtime_root.chmod(0o700)
    monkeypatch.setenv("SMS_RELEASE_SMOKE", "1")
    monkeypatch.setenv("SMS_RELEASE_ROOT", str(smoke_release_root))
    monkeypatch.setenv("SMS_RUNTIME_ROOT", str(runtime_root))
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", smoke_release_root.parent.name)
    manager, runner, root, _ = _manager(tmp_path, manifest, current_refs)
    manager = ReleaseManager(
        root=root,
        release_root=smoke_release_root,
        mode=manifest["mode"],
        runner=runner,
        expected_staging_uid=os.geteuid(),
    )

    manager.prepare(manifest_path)

    state = manager.status(manifest["release_id"])
    assert state["state"] == "prepared"
    assert state["release_gate_kind"] == "release_control_smoke"
    assert state["control_smoke_only"] is True
    assert state["release_scan_performed"] is False


@pytest.mark.parametrize(
    "mutation",
    [
        "change_unknown",
        "change_binding",
        "report_hash",
        "snapshot_binding",
        "time_order",
        "report_checks",
        "report_tables",
    ],
)
def test_production_backup_contract_rejects_any_unbound_or_unsafe_evidence(
    tmp_path: Path,
    mutation: str,
) -> None:
    manifest_path, manifest, current_refs = _bundle(
        tmp_path,
        mode="production",
        postgres_changed=True,
    )
    change_path = manifest_path.parent / "backup-change.json"
    report_path = manifest_path.parent / "restore-report.json"
    change = json.loads(change_path.read_text(encoding="utf-8"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if mutation == "change_unknown":
        change["unknown"] = True
    elif mutation == "change_binding":
        change["release_id"] = "different-release"
    elif mutation == "report_hash":
        change["restore"]["report_sha256"] = "0" * 64
    elif mutation == "snapshot_binding":
        change["restore"]["snapshot_id"] = "different-snapshot"
    elif mutation == "time_order":
        change["approval"]["approved_at"] = "2026-07-14T07:15:00+00:00"
    elif mutation == "report_checks":
        report["checks"]["role_flags"] = "true|false|false"
    else:
        report["table_counts"]["extra"] = 0
    _write_private_json(report_path, report)
    if mutation not in {
        "report_hash",
        "change_unknown",
        "change_binding",
        "snapshot_binding",
        "time_order",
    }:
        change["restore"]["report_sha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()
    _write_private_json(change_path, change)
    manager, _, _, _ = _manager(tmp_path, manifest, current_refs)

    with pytest.raises(ReleaseManagerError) as error:
        manager.prepare(manifest_path)

    assert "0" * 64 not in str(error.value)


def test_prepare_rejects_duplicate_env_image_keys(tmp_path: Path) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path)
    manager, _, root, _ = _manager(tmp_path, manifest, current_refs)
    with (root / ".env").open("a", encoding="utf-8") as stream:
        stream.write(f"SMS_WEB_IMAGE={current_refs['web']}\n")

    with pytest.raises(ReleaseManagerError, match="prepare failed"):
        manager.prepare(manifest_path)


def _planned_manifest(
    tmp_path: Path,
    changed: set[str],
    *,
    migration_changed: bool,
) -> Any:
    manifest_path, manifest, _ = _bundle(tmp_path)
    for name in IMAGE_NAMES:
        image = manifest["images"][name]
        image["changed"] = name in changed
        if name in changed:
            archive = manifest_path.parent / f"{name}.tar"
            archive.write_bytes(f"verified-{name}-archive".encode())
            archive.chmod(0o600)
            image["archive_file"] = archive.name
            image["archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
        else:
            image["archive_file"] = None
            image["archive_sha256"] = None
    if not migration_changed:
        manifest["migration"]["target"] = manifest["migration"]["from"]
        manifest["migration"]["compatibility"] = "none"
    manifest["evidence"]["data_images"] = (
        "data-images.json" if {"postgres", "redis"} & changed else None
    )
    _write_private_json(manifest_path, manifest)
    return load_manifest(manifest_path)


@pytest.mark.parametrize(
    ("changed", "migration_changed", "expected"),
    [
        (set(), False, [("verify", ())]),
        ({"web"}, False, [("recreate_web", ("web",)), ("verify", ())]),
        (
            {"api"},
            False,
            [
                (
                    "quiesce_backend",
                    (
                        "beat",
                        "outbox-dispatcher",
                        "worker-realtime",
                        "worker-bulk",
                        "worker-callback",
                        "api",
                    ),
                ),
                ("wait_beat_lease", ()),
                (
                    "recreate_backend",
                    (
                        "api",
                        "worker-realtime",
                        "worker-bulk",
                        "worker-callback",
                        "outbox-dispatcher",
                        "beat",
                    ),
                ),
                ("verify", ()),
            ],
        ),
        (
            {"api"},
            True,
            [
                (
                    "quiesce_backend",
                    (
                        "beat",
                        "outbox-dispatcher",
                        "worker-realtime",
                        "worker-bulk",
                        "worker-callback",
                        "api",
                    ),
                ),
                ("wait_beat_lease", ()),
                ("run_migrate", ("migrate",)),
                (
                    "recreate_backend",
                    (
                        "api",
                        "worker-realtime",
                        "worker-bulk",
                        "worker-callback",
                        "outbox-dispatcher",
                        "beat",
                    ),
                ),
                ("verify", ()),
            ],
        ),
        (
            {"redis"},
            False,
            [
                (
                    "quiesce_backend",
                    (
                        "beat",
                        "outbox-dispatcher",
                        "worker-realtime",
                        "worker-bulk",
                        "worker-callback",
                        "api",
                    ),
                ),
                ("wait_beat_lease", ()),
                ("recreate_redis", ("redis", "redis-auth", "redis-control")),
                (
                    "recreate_backend",
                    (
                        "api",
                        "worker-realtime",
                        "worker-bulk",
                        "worker-callback",
                        "outbox-dispatcher",
                        "beat",
                    ),
                ),
                ("verify", ()),
            ],
        ),
        (
            {"postgres"},
            True,
            [
                (
                    "quiesce_backend",
                    (
                        "beat",
                        "outbox-dispatcher",
                        "worker-realtime",
                        "worker-bulk",
                        "worker-callback",
                        "api",
                    ),
                ),
                ("wait_beat_lease", ()),
                ("recreate_postgres", ("postgres",)),
                ("run_migrate", ("migrate",)),
                (
                    "recreate_backend",
                    (
                        "api",
                        "worker-realtime",
                        "worker-bulk",
                        "worker-callback",
                        "outbox-dispatcher",
                        "beat",
                    ),
                ),
                ("verify", ()),
            ],
        ),
        (
            set(IMAGE_NAMES),
            True,
            [
                (
                    "quiesce_backend",
                    (
                        "beat",
                        "outbox-dispatcher",
                        "worker-realtime",
                        "worker-bulk",
                        "worker-callback",
                        "api",
                    ),
                ),
                ("wait_beat_lease", ()),
                ("recreate_postgres", ("postgres",)),
                ("recreate_redis", ("redis", "redis-auth", "redis-control")),
                ("run_migrate", ("migrate",)),
                (
                    "recreate_backend",
                    (
                        "api",
                        "worker-realtime",
                        "worker-bulk",
                        "worker-callback",
                        "outbox-dispatcher",
                        "beat",
                    ),
                ),
                ("recreate_web", ("web",)),
                ("verify", ()),
            ],
        ),
    ],
)
def test_activation_plan_is_pure_and_orders_exact_service_groups(
    tmp_path: Path,
    changed: set[str],
    migration_changed: bool,
    expected: list[tuple[str, tuple[str, ...]]],
) -> None:
    import release_manager as release_manager_module

    manifest = _planned_manifest(tmp_path, changed, migration_changed=migration_changed)

    first = release_manager_module.build_activation_plan(manifest)
    second = release_manager_module.build_activation_plan(manifest)

    assert first == second
    assert [(step.kind.value, step.services) for step in first] == expected


def test_activation_commands_are_exact_argv_arrays(tmp_path: Path) -> None:
    import release_manager as release_manager_module

    manifest = _planned_manifest(tmp_path, {"web"}, migration_changed=False)
    root = tmp_path / "platform"
    compose = [
        "docker",
        "compose",
        "--env-file",
        str(root / ".env"),
        "-f",
        str(root / "deploy" / "docker-compose.yml"),
    ]

    commands = release_manager_module.activation_commands(
        root,
        release_manager_module.build_activation_plan(manifest),
    )

    assert commands == [
        compose
        + [
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            "120",
            "web",
        ],
        compose + ["config", "--quiet"],
    ]
    assert all(type(command) is list for command in commands)


def test_configure_activation_atomically_updates_all_refs_and_preserves_env_metadata(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path, postgres_changed=True)
    manager, runner, root, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    env_path = root / ".env"
    original = env_path.read_bytes()
    decorated = b"# retained-comment\nUNRELATED=opaque\n" + original + b"TAIL=no-newline"
    env_path.write_bytes(decorated)
    env_path.chmod(0o640)
    before = env_path.stat()
    runner.calls.clear()

    plan = manager.configure_activation(manifest["release_id"])

    after = env_path.stat()
    assert env_path.read_bytes() == decorated.replace(
        f"SMS_POSTGRES_IMAGE={current_refs['postgres']}".encode(),
        f"SMS_POSTGRES_IMAGE={manifest['images']['postgres']['ref']}".encode(),
    )
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode)
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)
    assert plan[0].kind.value == "quiesce_backend"
    assert runner.calls == [manager._compose() + ["config", "--quiet"]]


@pytest.mark.parametrize("failure", ["write", "fsync", "replace"])
def test_configure_activation_atomic_failure_restores_original_without_container_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    import release_store as release_store_module

    manifest_path, manifest, current_refs = _bundle(tmp_path)
    manager, runner, root, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    env_path = root / ".env"
    original = env_path.read_bytes()
    release_store_module.ReleaseStore(manager.release_root, manifest["release_id"]).snapshot_env(
        env_path
    )
    real = getattr(release_store_module.os, failure)
    injected = False

    def fail_once(*args: Any, **kwargs: Any) -> Any:
        nonlocal injected
        if not injected:
            injected = True
            raise OSError(f"injected {failure} failure")
        return real(*args, **kwargs)

    monkeypatch.setattr(release_store_module.os, failure, fail_once)
    runner.calls.clear()

    with pytest.raises(ReleaseManagerError, match="configuration"):
        manager.configure_activation(manifest["release_id"])

    assert env_path.read_bytes() == original
    assert not any(
        token in command
        for command in runner.calls
        for token in ("up", "stop", "run", "restart", "rm", "pull")
    )


def test_config_failure_restores_original_env_and_never_changes_containers(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle(tmp_path)
    manager, runner, root, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    env_path = root / ".env"
    original = env_path.read_bytes()
    runner.calls.clear()
    real_run = runner.run

    def fail_config(
        argv: Sequence[str], *, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        command = [str(value) for value in argv]
        if command[-2:] == ["config", "--quiet"]:
            runner.calls.append(command)
            return subprocess.CompletedProcess(command, 1, "", "invalid")
        return real_run(argv, cwd=cwd)

    runner.run = fail_config  # type: ignore[method-assign]

    with pytest.raises(ReleaseManagerError, match="configuration"):
        manager.configure_activation(manifest["release_id"])

    assert env_path.read_bytes() == original
    assert runner.calls == [manager._compose() + ["config", "--quiet"]]


def _compose_actions(runner: FakeRunner) -> list[list[str]]:
    return [
        command
        for command in runner.calls
        if "compose" in command
        and any(action in command for action in ("config", "stop", "up", "run"))
    ]


def test_activate_orders_data_migrate_backend_and_web_with_exact_groups(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, set(IMAGE_NAMES))
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    runner.calls.clear()

    manager.activate(manifest["release_id"])

    compose = manager._compose()
    assert _compose_actions(runner) == [
        compose + ["config", "--quiet"],
        compose + ["stop", *_QUIESCE_TEST_SERVICES],
        compose
        + [
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            "120",
            "postgres",
        ],
        compose
        + [
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            "120",
            "redis",
            "redis-auth",
            "redis-control",
        ],
        compose + ["run", "--rm", "migrate"],
        compose
        + [
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            "120",
            *_BACKEND_TEST_SERVICES,
        ],
        compose
        + ["up", "-d", "--no-deps", "--force-recreate", "--wait", "--wait-timeout", "120", "web"],
    ]
    assert manager.status(manifest["release_id"])["state"] == "succeeded"


def test_successful_migrate_command_must_reach_declared_target(tmp_path: Path) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"api"})
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    runner.migration_head_after_successful_run = manifest["migration"]["from"]

    with pytest.raises(ReleaseManagerError, match="recovery_required"):
        manager.activate(manifest["release_id"])

    assert manager.status(manifest["release_id"])["state"] == "recovery_required"


def test_final_verification_rechecks_declared_migration_target(tmp_path: Path) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"api"})
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)

    def drift_migration_after_worker_probe() -> None:
        runner.migration_head = manifest["migration"]["from"]

    runner.after_ping = drift_migration_after_worker_probe

    with pytest.raises(ReleaseManagerError, match="recovery_required"):
        manager.activate(manifest["release_id"])

    assert manager.status(manifest["release_id"])["state"] == "recovery_required"


def test_final_runtime_verification_binds_images_containers_health_and_workers(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"web"})
    manifest["migration"]["target"] = manifest["migration"]["from"]
    manifest["migration"]["compatibility"] = "none"
    _write_private_json(manifest_path, manifest)
    manager, runner, _, release_root = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    original_ids = runner.service_container_ids.copy()
    runner.calls.clear()

    manager.activate(manifest["release_id"])

    assert runner.service_container_ids["web"] != original_ids["web"]
    assert all(
        runner.service_container_ids[service] == original_ids[service]
        for service in RUNTIME_SERVICES
        if service != "web"
    )
    compose = manager._compose()
    for service in RUNTIME_SERVICES:
        assert compose + ["ps", "-q", service] in runner.calls
        assert any(
            command[:3] == ["docker", "inspect", "--format"]
            and "Config.Hostname" in command[-2]
            and command[-1] == runner.service_container_ids[service]
            for command in runner.calls
        )
    assert (
        compose
        + [
            "exec",
            "-T",
            "worker-realtime",
            "celery",
            "-A",
            "app.tasks",
            "inspect",
            "ping",
            "--timeout",
            "10",
            "--json",
        ]
        in runner.calls
    )
    events = [
        json.loads(line)
        for line in (release_root / manifest["release_id"] / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    final = [
        event
        for event in events
        if event["kind"] == "observation" and event["step"] == "final_runtime"
    ]
    assert final[-1]["details"] == {
        "completed": True,
        "services": list(RUNTIME_SERVICES),
        "tracked_job_heartbeat": "post_release_operational_check",
    }


def test_final_web_health_failure_uses_existing_stateless_compensation(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"web"})
    manifest["migration"]["target"] = manifest["migration"]["from"]
    manifest["migration"]["compatibility"] = "none"
    _write_private_json(manifest_path, manifest)
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    runner.unhealthy_service = "web"
    runner.calls.clear()

    with pytest.raises(ReleaseManagerError, match="rolled_back"):
        manager.activate(manifest["release_id"])

    assert manager.status(manifest["release_id"])["state"] == "rolled_back"
    assert (
        sum("up" in command and command[-1] == "web" for command in _compose_actions(runner)) == 2
    )


def test_missing_worker_ping_uses_existing_stateless_compensation(tmp_path: Path) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"web"})
    manifest["migration"]["target"] = manifest["migration"]["from"]
    manifest["migration"]["compatibility"] = "none"
    _write_private_json(manifest_path, manifest)
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    runner.missing_worker_ping = "worker-callback"
    runner.calls.clear()

    with pytest.raises(ReleaseManagerError, match="rolled_back"):
        manager.activate(manifest["release_id"])

    assert manager.status(manifest["release_id"])["state"] == "rolled_back"
    events = (manager.release_root / manifest["release_id"] / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert '"reason":"worker_ping_membership"' in events


def test_service_failure_during_worker_ping_is_detected_before_success(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"web"})
    manifest["migration"]["target"] = manifest["migration"]["from"]
    manifest["migration"]["compatibility"] = "none"
    _write_private_json(manifest_path, manifest)
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)

    def stop_web_after_ping() -> None:
        runner.unhealthy_service = "web"

    runner.after_ping = stop_web_after_ping
    with pytest.raises(ReleaseManagerError, match="rolled_back"):
        manager.activate(manifest["release_id"])

    assert manager.status(manifest["release_id"])["state"] == "rolled_back"
    events = (manager.release_root / manifest["release_id"] / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert '"reason":"post_ping_service_health"' in events


def test_unselected_container_identity_drift_requires_manual_recovery(tmp_path: Path) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"web"})
    manifest["migration"]["target"] = manifest["migration"]["from"]
    manifest["migration"]["compatibility"] = "none"
    _write_private_json(manifest_path, manifest)
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)

    def drift_unselected(action: str, _: int) -> None:
        if action == "up":
            runner.service_container_ids["api"] = hashlib.sha256(b"external-api").hexdigest()

    runner.after_action = drift_unselected
    runner.calls.clear()

    with pytest.raises(ReleaseManagerError, match="recovery_required"):
        manager.activate(manifest["release_id"])

    assert manager.status(manifest["release_id"])["state"] == "recovery_required"


def test_runtime_host_and_public_probes_are_not_added_to_release_manager() -> None:
    source = (ROOT / "deploy/scripts/release_manager.py").read_text(encoding="utf-8")

    assert "systemctl" not in source
    assert "public-url" not in source


def test_activating_release_with_original_runtime_resumes_after_preflight_stop() -> None:
    decision = reconcile_release(
        {"state": ReleaseState.ACTIVATING.value},
        RuntimeObservation(
            env_state="original",
            service_state="original",
            migration_state="original",
            healthy=True,
            migration_required=True,
        ),
    )

    assert decision is ReconciliationDecision.RESUME


def test_backend_release_drains_legacy_beat_lease_before_recreate(
    tmp_path: Path,
) -> None:
    manifest_path, _, _ = _bundle_for_changes(tmp_path, {"api"})
    parsed = load_manifest(manifest_path)
    import release_manager as release_manager_module

    steps = release_manager_module.build_activation_plan(parsed)
    commands = release_manager_module.activation_commands(tmp_path, steps)
    kinds = [step.kind for step in steps]

    assert kinds[:2] == [
        release_manager_module.ReleaseStepKind.QUIESCE_BACKEND,
        release_manager_module.ReleaseStepKind.WAIT_BEAT_LEASE,
    ]
    assert commands[1] == ["sleep", "31"]


def test_runtime_inspection_handles_services_without_healthchecks() -> None:
    source = (ROOT / "deploy/scripts/release_manager.py").read_text(encoding="utf-8")

    assert 'index .State "Health"' in source
    assert "{{if .State.Health}}" not in source


_QUIESCE_TEST_SERVICES = (
    "beat",
    "outbox-dispatcher",
    "worker-realtime",
    "worker-bulk",
    "worker-callback",
    "api",
)
_BACKEND_TEST_SERVICES = (
    "api",
    "worker-realtime",
    "worker-bulk",
    "worker-callback",
    "outbox-dispatcher",
    "beat",
)


def test_web_only_failure_never_touches_backend_or_data_and_rolls_back(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"web"})
    manifest["migration"]["target"] = manifest["migration"]["from"]
    manifest["migration"]["compatibility"] = "none"
    _write_private_json(manifest_path, manifest)
    manager, runner, root, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    original = (root / ".env").read_bytes()
    runner.calls.clear()
    runner.fail_action = "up"
    runner.fail_after_effect = True

    with pytest.raises(ReleaseManagerError, match="rolled_back"):
        manager.activate(manifest["release_id"])

    actions = _compose_actions(runner)
    assert sum("up" in command and command[-1] == "web" for command in actions) == 2
    assert not any(set(_BACKEND_TEST_SERVICES) & set(command) for command in actions)
    assert not any("postgres" in command or "redis" in command for command in actions)
    assert (root / ".env").read_bytes() == original
    assert manager.status(manifest["release_id"])["state"] == "rolled_back"


def test_data_health_failure_restores_old_image_and_resumes_backend(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"postgres"})
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    runner.calls.clear()
    runner.fail_action = "up"
    runner.fail_after_effect = True

    with pytest.raises(ReleaseManagerError, match="rolled_back"):
        manager.activate(manifest["release_id"])

    actions = _compose_actions(runner)
    assert sum("up" in command and command[-1] == "postgres" for command in actions) == 2
    assert any(
        command[-len(_BACKEND_TEST_SERVICES) :] == list(_BACKEND_TEST_SERVICES)
        for command in actions
    )
    state = manager.status(manifest["release_id"])
    assert state["state"] == "rolled_back"
    assert state["residual_changes"] == []


@pytest.mark.parametrize(
    ("observed_head", "expected_state", "residual"),
    [
        ("0011", "rolled_back", []),
        ("0012", "rolled_back", ["migration:0012"]),
        ("0011_partial", "recovery_required", None),
    ],
)
def test_migrate_failure_uses_observed_head_without_downgrade(
    tmp_path: Path,
    observed_head: str,
    expected_state: str,
    residual: list[str] | None,
) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"api"})
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    runner.calls.clear()
    runner.fail_action = "run"
    runner.migration_head_after_failed_run = observed_head

    with pytest.raises(ReleaseManagerError, match=expected_state):
        manager.activate(manifest["release_id"])

    state = manager.status(manifest["release_id"])
    assert state["state"] == expected_state
    if residual is not None:
        assert state["residual_changes"] == residual
    assert sum("run" in command for command in _compose_actions(runner)) == 1


def test_later_backend_failure_keeps_healthy_data_and_records_residual(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"api", "postgres"})
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    runner.calls.clear()
    runner.fail_action = "up"
    runner.fail_action_number = 2
    runner.fail_after_effect = True

    with pytest.raises(ReleaseManagerError, match="rolled_back"):
        manager.activate(manifest["release_id"])

    actions = _compose_actions(runner)
    assert sum("up" in command and command[-1] == "postgres" for command in actions) == 1
    assert (
        sum(
            command[-len(_BACKEND_TEST_SERVICES) :] == list(_BACKEND_TEST_SERVICES)
            for command in actions
        )
        == 2
    )
    assert manager.status(manifest["release_id"])["residual_changes"] == [
        "image:postgres",
        "migration:0012",
    ]


def test_compensation_failure_enters_recovery_required_and_preserves_evidence(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"web"})
    manifest["migration"]["target"] = manifest["migration"]["from"]
    manifest["migration"]["compatibility"] = "none"
    _write_private_json(manifest_path, manifest)
    manager, runner, _, release_root = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    runner.calls.clear()
    runner.fail_action = "up"
    runner.fail_after_effect = True
    runner.fail_compensation = True

    with pytest.raises(ReleaseManagerError, match="recovery_required"):
        manager.activate(manifest["release_id"])

    release_dir = release_root / manifest["release_id"]
    assert manager.status(manifest["release_id"])["state"] == "recovery_required"
    assert (release_dir / "original.env").is_file()
    assert (release_dir / "artifacts" / "web.tar").is_file()


@pytest.mark.parametrize(
    "step_name",
    [
        "quiesce_backend",
        "recreate_postgres",
        "recreate_redis",
        "run_migrate",
        "recreate_backend",
        "recreate_web",
        "verify",
    ],
)
def test_failure_before_each_action_never_executes_that_intended_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    step_name: str,
) -> None:
    import release_manager as release_manager_module

    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, set(IMAGE_NAMES))
    manager, runner, _, release_root = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    parsed = load_manifest(manifest_path)
    plan = release_manager_module.build_activation_plan(parsed)
    step = next(item for item in plan if item.kind.value == step_name)
    intended = release_manager_module.activation_commands(manager.root, [step])[0]
    real_record = release_manager_module.ReleaseStore.record_intent

    def fail_intent(self: Any, event_step: str, details: dict[str, object]) -> None:
        if event_step == step_name:
            raise OSError("injected intent failure")
        real_record(self, event_step, details)

    monkeypatch.setattr(release_manager_module.ReleaseStore, "record_intent", fail_intent)
    runner.calls.clear()

    with pytest.raises(ReleaseManagerError, match="rolled_back"):
        manager.activate(manifest["release_id"])

    expected_count = {"recreate_backend": 1, "verify": 2}.get(step_name, 0)
    assert runner.calls.count(intended) == expected_count
    events = [
        json.loads(line)
        for line in (release_root / manifest["release_id"] / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert not any(
        event["kind"] == "observation"
        and event["step"] == step_name
        and event["details"].get("completed") is True
        for event in events
    )
    assert manager.status(manifest["release_id"])["state"] == "rolled_back"


@pytest.mark.parametrize(
    "step_name",
    [
        "quiesce_backend",
        "recreate_postgres",
        "recreate_redis",
        "run_migrate",
        "recreate_backend",
        "recreate_web",
        "verify",
    ],
)
def test_missing_observation_after_each_action_fails_closed_to_recovery_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    step_name: str,
) -> None:
    import release_manager as release_manager_module

    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, set(IMAGE_NAMES))
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    real_record = release_manager_module.ReleaseStore.record_observation

    def fail_observation(self: Any, event_step: str, details: dict[str, object]) -> None:
        if event_step == step_name:
            raise OSError("injected observation failure")
        real_record(self, event_step, details)

    monkeypatch.setattr(
        release_manager_module.ReleaseStore,
        "record_observation",
        fail_observation,
    )
    runner.calls.clear()

    with pytest.raises(ReleaseManagerError, match="recovery_required"):
        manager.activate(manifest["release_id"])

    assert manager.status(manifest["release_id"])["state"] == "recovery_required"


def test_activation_rejects_runtime_drift_before_env_or_lifecycle_mutation(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"web"})
    manifest["migration"]["target"] = manifest["migration"]["from"]
    manifest["migration"]["compatibility"] = "none"
    _write_private_json(manifest_path, manifest)
    manager, runner, root, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    original = (root / ".env").read_bytes()
    runner.runtime_refs["web"] = "sms-platform-web:external-drift"
    runner.calls.clear()

    with pytest.raises(ReleaseManagerError, match="recovery_required"):
        manager.activate(manifest["release_id"])

    assert (root / ".env").read_bytes() == original
    assert not any(
        action in command
        for command in runner.calls
        for action in ("stop", "up", "run", "rm", "pull")
    )
    assert manager.status(manifest["release_id"])["state"] == "recovery_required"


@pytest.mark.parametrize(
    ("stored_state", "env_state", "service_state", "migration_state", "healthy", "expected"),
    [
        ("staged", "original", "original", "original", True, "resume"),
        ("prepared", "original", "original", "original", True, "resume"),
        ("prepared", "target", "target", "original", True, "finalize"),
        ("activating", "target", "prefix", "original", True, "resume"),
        ("activating", "original", "target", "original", True, "rollback"),
        ("rolling_back", "original", "prefix", "target", True, "rollback"),
        ("succeeded", "target", "target", "target", True, "finalize"),
        ("rolled_back", "original", "original", "target", True, "finalize"),
        ("failed", "original", "original", "original", True, "recovery_required"),
        (
            "recovery_required",
            "target",
            "prefix",
            "ambiguous",
            False,
            "recovery_required",
        ),
        ("unknown", "unknown", "ambiguous", "ambiguous", False, "recovery_required"),
        ("activating", "target", "ambiguous", "original", True, "recovery_required"),
        ("activating", "target", "target", "ambiguous", True, "recovery_required"),
        ("activating", "target", "target", "target", False, "recovery_required"),
    ],
)
def test_reconcile_release_is_exhaustive_and_deterministic(
    stored_state: str,
    env_state: str,
    service_state: str,
    migration_state: str,
    healthy: bool,
    expected: str,
) -> None:
    import release_manager as release_manager_module

    observation = release_manager_module.RuntimeObservation(
        env_state=env_state,
        service_state=service_state,
        migration_state=migration_state,
        healthy=healthy,
    )

    first = release_manager_module.reconcile_release({"state": stored_state}, observation)
    second = release_manager_module.reconcile_release({"state": stored_state}, observation)

    assert first == second
    assert first.value == expected


def test_reconcile_never_finalizes_target_services_with_required_old_migration() -> None:
    import release_manager as release_manager_module

    inconsistent = release_manager_module.RuntimeObservation(
        env_state="target",
        service_state="target",
        migration_state="original",
        healthy=True,
        migration_required=True,
    )
    safe_prefix = release_manager_module.RuntimeObservation(
        env_state="target",
        service_state="prefix",
        migration_state="original",
        healthy=True,
        migration_required=True,
    )

    assert (
        release_manager_module.reconcile_release({"state": "activating"}, inconsistent).value
        == "recovery_required"
    )
    assert (
        release_manager_module.reconcile_release({"state": "activating"}, safe_prefix).value
        == "resume"
    )


def test_runtime_probe_uses_image_ref_when_original_and_target_ids_match(
    tmp_path: Path,
) -> None:
    import release_manager as release_manager_module

    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"api"})
    manifest["images"]["api"]["id"] = "sha256:" + "5" * 64
    _write_bound_release_report(
        manifest_path.parent / "release-gate.json",
        manifest,
        _release_report(manifest),
    )
    _write_private_json(manifest_path, manifest)
    manager, runner, _, release_root = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    store = release_manager_module.ReleaseStore(release_root, manifest["release_id"])
    backend_step = next(
        step
        for step in release_manager_module.build_activation_plan(
            release_manager_module.load_manifest(manifest_path)
        )
        if step.kind is release_manager_module.ReleaseStepKind.RECREATE_BACKEND
    )

    stored_manifest = manager._stored_manifest(store)
    original, _ = manager._observe_step_runtime_status(store, stored_manifest, backend_step)
    runner.runtime_refs["api"] = manifest["images"]["api"]["ref"]
    target, _ = manager._observe_step_runtime_status(store, stored_manifest, backend_step)

    assert original == "original"
    assert target == "target"


def test_runtime_probe_binds_a_stopped_target_container_before_compensation(
    tmp_path: Path,
) -> None:
    import release_manager as release_manager_module

    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"web"})
    manifest["migration"]["target"] = manifest["migration"]["from"]
    manifest["migration"]["compatibility"] = "none"
    _write_private_json(manifest_path, manifest)
    manager, runner, _, release_root = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    store = release_manager_module.ReleaseStore(release_root, manifest["release_id"])
    web_step = next(
        step
        for step in release_manager_module.build_activation_plan(
            release_manager_module.load_manifest(manifest_path)
        )
        if step.kind is release_manager_module.ReleaseStepKind.RECREATE_WEB
    )
    runner.runtime_refs["web"] = manifest["images"]["web"]["ref"]
    runner.service_running["web"] = False
    runner.calls.clear()

    state, healthy = manager._observe_step_runtime_status(
        store,
        manager._stored_manifest(store),
        web_step,
    )

    assert state == "target"
    assert healthy is False
    assert any(command[-4:] == ["ps", "--all", "-q", "web"] for command in runner.calls)


def test_migration_probe_does_not_require_a_running_api(tmp_path: Path) -> None:
    import release_manager as release_manager_module

    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"api"})
    manager, runner, _, release_root = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    store = release_manager_module.ReleaseStore(release_root, manifest["release_id"])
    runner.service_running["api"] = False
    runner.calls.clear()

    observed = manager._observe_migration_state(store, manager._stored_manifest(store))

    assert observed == "original"
    assert any(
        "postgres" in command and "SELECT version_num FROM alembic_version" in command[-1]
        for command in runner.calls
    )
    assert not any(
        command[-5:] == ["exec", "-T", "api", "alembic", "current"] for command in runner.calls
    )


def test_resume_prepared_release_and_terminal_repeat_are_idempotent(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"web"})
    manifest["migration"]["target"] = manifest["migration"]["from"]
    manifest["migration"]["compatibility"] = "none"
    _write_private_json(manifest_path, manifest)
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    runner.calls.clear()

    manager.resume(manifest["release_id"])
    completed_calls = list(runner.calls)
    manager.resume(manifest["release_id"])

    assert manager.status(manifest["release_id"])["state"] == "succeeded"
    assert runner.calls == completed_calls


def test_resume_finalizes_sigkill_style_missing_success_observation_from_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import release_manager as release_manager_module

    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"web"})
    manifest["migration"]["target"] = manifest["migration"]["from"]
    manifest["migration"]["compatibility"] = "none"
    _write_private_json(manifest_path, manifest)
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    real_transition = release_manager_module.ReleaseStore.transition

    def lose_success_observation(
        self: Any,
        expected: Any,
        target: Any,
        **fields: object,
    ) -> None:
        if target.value == "succeeded":
            raise OSError("simulated SIGKILL before terminal persistence")
        real_transition(self, expected, target, **fields)

    with monkeypatch.context() as scoped:
        scoped.setattr(
            release_manager_module.ReleaseStore,
            "transition",
            lose_success_observation,
        )
        with pytest.raises(OSError, match="SIGKILL"):
            manager.activate(manifest["release_id"])

    runner.calls.clear()
    manager.resume(manifest["release_id"])

    assert manager.status(manifest["release_id"])["state"] == "succeeded"
    assert (
        manager._compose()
        + [
            "exec",
            "-T",
            "worker-realtime",
            "celery",
            "-A",
            "app.tasks",
            "inspect",
            "ping",
            "--timeout",
            "10",
            "--json",
        ]
        in runner.calls
    )
    assert not any(
        action in command for command in runner.calls for action in ("stop", "up", "run")
    )


def test_resume_refuses_partial_migration_and_marks_recovery_required(
    tmp_path: Path,
) -> None:
    import release_manager as release_manager_module

    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"api"})
    manager, runner, _, release_root = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    store = release_manager_module.ReleaseStore(release_root, manifest["release_id"])
    store.transition(
        release_manager_module.ReleaseState.PREPARED,
        release_manager_module.ReleaseState.ACTIVATING,
    )
    manager.configure_activation(manifest["release_id"])
    store.record_intent("run_migrate", {"services": ["migrate"]})
    runner.migration_head = "0011_partial"
    runner.calls.clear()

    with pytest.raises(ReleaseManagerError, match="recovery_required"):
        manager.resume(manifest["release_id"])

    assert manager.status(manifest["release_id"])["state"] == "recovery_required"
    assert not any("run" in command for command in _compose_actions(runner))


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_signal_before_resume_persists_and_starts_no_new_step(
    tmp_path: Path,
    signum: int,
) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"web"})
    manifest["migration"]["target"] = manifest["migration"]["from"]
    manifest["migration"]["compatibility"] = "none"
    _write_private_json(manifest_path, manifest)
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    runner.calls.clear()
    manager.request_stop(signum, None)

    with pytest.raises(ReleaseManagerError, match="interrupted"):
        manager.resume(manifest["release_id"])

    state = manager.status(manifest["release_id"])
    assert state["interrupted_signal"] == signal.Signals(signum).name
    assert not runner.calls


def test_term_after_data_step_stops_before_redis_and_persists_resume_point(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, set(IMAGE_NAMES))
    manager, runner, _, _ = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)

    def interrupt_after_postgres(action: str, count: int) -> None:
        if action == "up" and count == 1:
            manager.request_stop(signal.SIGTERM, None)

    runner.after_action = interrupt_after_postgres
    runner.calls.clear()

    with pytest.raises(ReleaseManagerError, match="interrupted"):
        manager.activate(manifest["release_id"])

    state = manager.status(manifest["release_id"])
    assert state["state"] == "activating"
    assert state["interrupted_signal"] == "SIGTERM"
    assert not any("up" in command and command[-1] == "redis" for command in runner.calls)


def test_resume_after_env_replacement_before_config_observation(
    tmp_path: Path,
) -> None:
    import release_manager as release_manager_module

    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"web"})
    manifest["migration"]["target"] = manifest["migration"]["from"]
    manifest["migration"]["compatibility"] = "none"
    _write_private_json(manifest_path, manifest)
    manager, runner, root, release_root = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    store = release_manager_module.ReleaseStore(release_root, manifest["release_id"])
    store.transition(
        release_manager_module.ReleaseState.PREPARED,
        release_manager_module.ReleaseState.ACTIVATING,
    )
    env_path = root / ".env"
    original = env_path.read_bytes()
    store.snapshot_env(env_path)
    store.record_intent("env_replace", {"source": "manifest"})
    release_manager_module.ReleaseStore._atomic_write(
        env_path,
        manager._render_env_refs(
            original,
            {name: manifest["images"][name]["ref"] for name in IMAGE_NAMES},
        ),
        mode=stat.S_IMODE(env_path.stat().st_mode),
        private_parent=False,
    )
    runner.calls.clear()

    manager.resume(manifest["release_id"])

    assert manager.status(manifest["release_id"])["state"] == "succeeded"
    assert any(command[-2:] == ["config", "--quiet"] for command in runner.calls)
    assert sum("up" in command and command[-1] == "web" for command in runner.calls) == 1


def test_sigkill_after_data_action_reconciles_stopped_backend_and_target_data(
    tmp_path: Path,
) -> None:
    import release_manager as release_manager_module

    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, set(IMAGE_NAMES))
    manager, runner, root, release_root = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    store = release_manager_module.ReleaseStore(release_root, manifest["release_id"])
    store.transition(
        release_manager_module.ReleaseState.PREPARED,
        release_manager_module.ReleaseState.ACTIVATING,
    )
    plan = manager.configure_activation(manifest["release_id"])
    commands = release_manager_module.activation_commands(root, plan)
    quiesce_index = next(
        index for index, step in enumerate(plan) if step.kind.value == "quiesce_backend"
    )
    postgres_index = next(
        index for index, step in enumerate(plan) if step.kind.value == "recreate_postgres"
    )
    store.record_intent("quiesce_backend", {"services": list(_QUIESCE_TEST_SERVICES)})
    assert runner.run(commands[quiesce_index], cwd=root).returncode == 0
    store.record_intent("recreate_postgres", {"services": ["postgres"]})
    assert runner.run(commands[postgres_index], cwd=root).returncode == 0
    runner.calls.clear()
    resumed = ReleaseManager(
        root=root,
        release_root=release_root,
        mode=manifest["mode"],
        runner=runner,
        expected_staging_uid=os.geteuid(),
    )

    resumed.resume(manifest["release_id"])

    assert not any("up" in command and command[-1] == "postgres" for command in runner.calls)
    assert any(
        "up" in command
        and command[-3:] == ["redis", "redis-auth", "redis-control"]
        for command in runner.calls
    )
    assert resumed.status(manifest["release_id"])["state"] == "succeeded"


def test_resume_after_backend_success_does_not_restart_backend_before_web(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"api", "web"})
    manager, runner, root, release_root = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)

    def interrupt_after_backend(action: str, count: int) -> None:
        if action == "up" and count == 1:
            manager.request_stop(signal.SIGTERM, None)

    runner.after_action = interrupt_after_backend
    with pytest.raises(ReleaseManagerError, match="interrupted"):
        manager.activate(manifest["release_id"])
    resumed_runner = runner
    resumed_runner.after_action = None
    resumed_runner.calls.clear()
    resumed = ReleaseManager(
        root=root,
        release_root=release_root,
        mode=manifest["mode"],
        runner=resumed_runner,
        expected_staging_uid=os.geteuid(),
    )

    resumed.resume(manifest["release_id"])

    actions = _compose_actions(resumed_runner)
    assert not any(command[-5:] == list(_BACKEND_TEST_SERVICES) for command in actions)
    assert sum("up" in command and command[-1] == "web" for command in actions) == 1
    assert resumed.status(manifest["release_id"])["state"] == "succeeded"


def test_resume_interrupted_compensation_uses_persisted_observations(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"web"})
    manifest["migration"]["target"] = manifest["migration"]["from"]
    manifest["migration"]["compatibility"] = "none"
    _write_private_json(manifest_path, manifest)
    manager, runner, root, release_root = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)
    runner.fail_action = "up"
    runner.fail_after_effect = True
    real_run = runner.run

    def interrupt_on_failed_action(
        argv: Sequence[str], *, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        result = real_run(argv, cwd=cwd)
        if result.returncode != 0:
            manager.request_stop(signal.SIGTERM, None)
        return result

    runner.run = interrupt_on_failed_action  # type: ignore[method-assign]

    with pytest.raises(ReleaseManagerError, match="interrupted"):
        manager.activate(manifest["release_id"])

    assert manager.status(manifest["release_id"])["state"] == "rolling_back"
    runner.run = real_run  # type: ignore[method-assign]
    runner.fail_action = None
    runner.calls.clear()
    resumed = ReleaseManager(
        root=root,
        release_root=release_root,
        mode=manifest["mode"],
        runner=runner,
        expected_staging_uid=os.geteuid(),
    )

    with pytest.raises(ReleaseManagerError, match="rolled_back"):
        resumed.resume(manifest["release_id"])

    assert resumed.status(manifest["release_id"])["state"] == "rolled_back"
    assert sum("up" in command and command[-1] == "web" for command in runner.calls) == 1


def test_explicit_rollback_after_data_observation_uses_same_compensation_matrix(
    tmp_path: Path,
) -> None:
    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"postgres"})
    manager, runner, root, release_root = _manager(tmp_path, manifest, current_refs)
    manager.prepare(manifest_path)

    def interrupt_after_postgres(action: str, count: int) -> None:
        if action == "up" and count == 1:
            manager.request_stop(signal.SIGTERM, None)

    runner.after_action = interrupt_after_postgres
    with pytest.raises(ReleaseManagerError, match="interrupted"):
        manager.activate(manifest["release_id"])
    runner.after_action = None
    runner.calls.clear()
    resumed = ReleaseManager(
        root=root,
        release_root=release_root,
        mode=manifest["mode"],
        runner=runner,
        expected_staging_uid=os.geteuid(),
    )

    with pytest.raises(ReleaseManagerError, match="rolled_back"):
        resumed.rollback(manifest["release_id"])

    assert sum("up" in command and command[-1] == "postgres" for command in runner.calls) == 1
    assert resumed.status(manifest["release_id"])["state"] == "rolled_back"


def test_resume_complete_staged_store_is_idempotent(tmp_path: Path) -> None:
    import release_manager as release_manager_module

    manifest_path, manifest, current_refs = _bundle_for_changes(tmp_path, {"web"})
    manifest["migration"]["target"] = manifest["migration"]["from"]
    manifest["migration"]["compatibility"] = "none"
    _write_private_json(manifest_path, manifest)
    manager, runner, _, release_root = _manager(tmp_path, manifest, current_refs)
    parsed, manifest_bytes, files = release_manager_module._validate_staging_bundle(
        manifest_path,
        os.geteuid(),
    )
    store = release_manager_module.ReleaseStore(release_root, manifest["release_id"])
    store.create(manifest_bytes)
    manager._copy_bundle(store, manifest_path, files)
    assert parsed.release_id == manifest["release_id"]

    manager.resume(manifest["release_id"])
    completed_calls = list(runner.calls)
    manager.resume(manifest["release_id"])

    assert manager.status(manifest["release_id"])["state"] == "succeeded"
    assert runner.calls == completed_calls


def test_main_registers_and_restores_term_int_hup_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import release_manager as release_manager_module

    registrations: list[tuple[int, object]] = []

    class StubManager:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def request_stop(self, _signum: int, _frame: object | None) -> None:
            pass

        def status(self, _release_id: str) -> dict[str, object]:
            return {"state": "prepared"}

    monkeypatch.setattr(release_manager_module, "ReleaseManager", StubManager)
    monkeypatch.setattr(release_manager_module.signal, "getsignal", lambda signum: f"old-{signum}")
    monkeypatch.setattr(
        release_manager_module.signal,
        "signal",
        lambda signum, handler: registrations.append((signum, handler)),
    )

    result = release_manager_module.main(
        [
            "--root",
            "/tmp/platform",
            "--release-root",
            "/var/lib/sms-platform/releases",
            "--mode",
            "development",
            "status",
            "--release-id",
            "release-1",
        ]
    )

    assert result == 0
    assert [item[0] for item in registrations[:3]] == [
        signal.SIGTERM,
        signal.SIGINT,
        signal.SIGHUP,
    ]
    assert registrations[-3:] == [
        (signal.SIGTERM, f"old-{signal.SIGTERM}"),
        (signal.SIGINT, f"old-{signal.SIGINT}"),
        (signal.SIGHUP, f"old-{signal.SIGHUP}"),
    ]


@pytest.fixture
def smoke_release_root() -> Iterator[Path]:
    parent = Path(tempfile.gettempdir()) / (f"sms-platform-release-control-{uuid.uuid4().hex[:8]}")
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    try:
        yield parent / "releases"
    finally:
        parent.chmod(0o700)
        shutil.rmtree(parent)


def test_cli_release_root_accepts_only_fixed_production_or_strict_development_smoke(
    tmp_path: Path,
    smoke_release_root: Path,
) -> None:
    import release_manager as release_manager_module

    validate = release_manager_module._validate_cli_release_root
    assert validate(
        Path("/var/lib/sms-platform/releases"),
        mode="production",
        platform_root=tmp_path,
    ) == Path("/var/lib/sms-platform/releases")
    assert validate(
        Path("/var/lib/sms-platform/releases"),
        mode="development",
        platform_root=tmp_path,
    ) == Path("/var/lib/sms-platform/releases")
    assert (
        validate(
            smoke_release_root,
            mode="development",
            platform_root=tmp_path,
        )
        == smoke_release_root
    )
    assert not smoke_release_root.exists()
    smoke_release_root.mkdir(mode=0o700)
    smoke_release_root.chmod(0o700)
    assert (
        validate(
            smoke_release_root,
            mode="development",
            platform_root=tmp_path,
        )
        == smoke_release_root
    )

    with pytest.raises(ReleaseManagerError, match="release root"):
        validate(
            smoke_release_root,
            mode="production",
            platform_root=tmp_path,
        )


@pytest.mark.parametrize(
    "candidate",
    [
        Path("relative/releases"),
        Path("/var/lib/sms-platform/other"),
        Path(tempfile.gettempdir()) / "sms-platform-release-control-Ab12345" / "releases",
        Path(tempfile.gettempdir()) / "sms-platform-release-control-Ab12_cd34" / "releases",
        Path(tempfile.gettempdir()) / "sms-platform-release-control-Ab12Cd34" / "other",
        Path(tempfile.gettempdir())
        / "sms-platform-release-control-Ab12Cd34"
        / "child"
        / ".."
        / "releases",
        Path(tempfile.gettempdir()) / "sms-platform-release-control-Ab12Cd34\n" / "releases",
    ],
)
def test_cli_release_root_rejects_invalid_shape(
    tmp_path: Path,
    candidate: Path,
) -> None:
    import release_manager as release_manager_module

    with pytest.raises(ReleaseManagerError, match="release root"):
        release_manager_module._validate_cli_release_root(
            candidate,
            mode="development",
            platform_root=tmp_path,
        )


@pytest.mark.parametrize("unsafe", ["parent_mode", "root_mode", "root_symlink", "owner", "git"])
def test_cli_release_root_rejects_unsafe_parent_or_existing_root(
    tmp_path: Path,
    smoke_release_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe: str,
) -> None:
    import release_manager as release_manager_module

    parent = smoke_release_root.parent
    if unsafe == "parent_mode":
        parent.chmod(0o755)
    elif unsafe == "root_mode":
        smoke_release_root.mkdir(mode=0o755)
    elif unsafe == "root_symlink":
        smoke_release_root.symlink_to(tmp_path, target_is_directory=True)
    elif unsafe == "owner":
        actual_uid = os.geteuid()
        monkeypatch.setattr(release_manager_module.os, "geteuid", lambda: actual_uid + 1)
    else:
        (parent / ".git").mkdir(mode=0o700)

    with pytest.raises(ReleaseManagerError, match="release root"):
        release_manager_module._validate_cli_release_root(
            smoke_release_root,
            mode="development",
            platform_root=tmp_path,
        )


def test_cli_rejects_invalid_release_root_before_constructing_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import release_manager as release_manager_module

    constructed = False

    class ForbiddenManager:
        def __init__(self, **_kwargs: object) -> None:
            nonlocal constructed
            constructed = True

    monkeypatch.setattr(release_manager_module, "ReleaseManager", ForbiddenManager)

    result = release_manager_module.main(
        [
            "--root",
            str(tmp_path),
            "--release-root",
            str(tmp_path / "unsafe" / "releases"),
            "--mode",
            "development",
            "status",
            "--release-id",
            "release-1",
        ]
    )

    assert result == 1
    assert constructed is False
