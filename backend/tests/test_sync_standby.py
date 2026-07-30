from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "deploy" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from failover_common import CommandFailure  # noqa: E402
from sync_standby import (  # noqa: E402
    StandbyTarget,
    SyncConfig,
    SyncService,
    validate_environment_file,
)


class FakeRunner:
    def __init__(self, *, dirty: bool = False, pipeline_error: bool = False) -> None:
        self.dirty = dirty
        self.pipeline_error = pipeline_error
        self.calls: list[list[str]] = []
        self.pipeline_calls: list[tuple[list[str], list[str], Path]] = []

    def run(
        self,
        command: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        input_bytes: bytes | None = None,
    ) -> bytes:
        argv = list(command)
        self.calls.append(argv)
        if argv[:2] == ["git", "status"]:
            return b" M tracked-file\n" if self.dirty else b""
        if argv[:2] == ["git", "rev-parse"]:
            return (b"a" * 40) + b"\n"
        if argv[0] == "git" and argv[1] == "archive":
            output_value = next(
                item.split("=", 1)[1] for item in argv if item.startswith("--output=")
            )
            output = Path(output_value)
            output.write_bytes(b"tracked-config-archive")
            output.chmod(0o600)
            return b""
        if "SELECT version_num FROM alembic_version" in " ".join(argv):
            return b"0008_audit_payload_guard\n"
        return b""

    def pipeline_to_file(
        self,
        producer: list[str],
        consumer: list[str],
        output_path: Path,
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.pipeline_calls.append((list(producer), list(consumer), output_path))
        if self.pipeline_error:
            raise CommandFailure("pg_dump", 1, "dump failed")
        output_path.write_bytes(b"encrypted-custom-dump")
        output_path.chmod(0o600)


def make_config(tmp_path: Path, *, build_only: bool = True) -> SyncConfig:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "deploy").mkdir()
    (root / "deploy/docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    env_file = tmp_path / "production.env"
    env_file.write_text(
        "ENVIRONMENT=production\nDEBUG=0\nAUTH_MOCK=0\nVENDOR_MOCK=0\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    passphrase = tmp_path / "backup-passphrase"
    passphrase.write_text("not-a-production-secret", encoding="utf-8")
    passphrase.chmod(0o600)
    target = None
    if not build_only:
        target = StandbyTarget("backup01.internal", "smsdr", "/srv/sms-standby", 22)
    return SyncConfig(
        repository_root=root,
        compose_file=root / "deploy/docker-compose.yml",
        environment_file=env_file,
        output_dir=root / "var/backups/standby-sync",
        passphrase_file=passphrase,
        database="sms",
        build_only=build_only,
        target=target,
    )


def fixed_clock() -> datetime:
    return datetime(2026, 7, 12, 5, 0, tzinfo=UTC)


def test_environment_file_rejects_secret_values_and_mock_production(tmp_path: Path) -> None:
    unsafe = tmp_path / ".env"
    unsafe.write_text(
        "ENVIRONMENT=production\nDEBUG=0\nAUTH_MOCK=0\nVENDOR_MOCK=0\nJWT_SECRET=leak\n",
        encoding="utf-8",
    )
    unsafe.chmod(0o600)
    with pytest.raises(ValueError, match="secret-like"):
        validate_environment_file(unsafe)

    unsafe.write_text(
        "ENVIRONMENT=production\nDEBUG=0\nAUTH_MOCK=1\nVENDOR_MOCK=0\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="AUTH_MOCK=0"):
        validate_environment_file(unsafe)


def test_sync_rejects_dirty_tracked_worktree_before_backup(tmp_path: Path) -> None:
    runner = FakeRunner(dirty=True)
    with pytest.raises(ValueError, match="工作树"):
        SyncService(runner, clock=fixed_clock).run(make_config(tmp_path))
    assert runner.pipeline_calls == []


def test_build_only_creates_encrypted_atomic_snapshot_without_remote_calls(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    config = make_config(tmp_path)

    result = SyncService(runner, clock=fixed_clock).run(config)

    assert result.snapshot_id == "20260712T050000Z_aaaaaaaaaaaa"
    assert result.snapshot_dir.name == result.snapshot_id
    assert config.output_dir.joinpath("current").resolve() == result.snapshot_dir.resolve()
    manifest = json.loads(result.snapshot_dir.joinpath("manifest.json").read_text())
    assert manifest["git_commit"] == "a" * 40
    assert manifest["alembic_version"] == "0008_audit_payload_guard"
    assert manifest["secrets_included"] is False
    assert set(manifest["files"]) == {
        "database",
        "repository_archive",
        "environment",
    }
    assert "passphrase" not in json.dumps(manifest).casefold()

    producer, consumer, output = runner.pipeline_calls[0]
    assert "pg_dump" in producer and "--format=custom" in producer
    assert "openssl" in consumer and "-aes-256-cbc" in consumer
    assert output.name.endswith(".dump.enc")
    archive_call = next(call for call in runner.calls if call[:2] == ["git", "archive"])
    assert "HEAD" in archive_call
    assert all("secrets" not in item for item in archive_call)
    assert not any(call[0] in {"ssh", "rsync"} for call in runner.calls)


def test_remote_publish_uses_incoming_hash_check_then_atomic_current(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    config = make_config(tmp_path, build_only=False)

    SyncService(runner, clock=fixed_clock).run(config)

    rsync = next(call for call in runner.calls if call[0] == "rsync")
    ssh = [call for call in runner.calls if call[0] == "ssh"][-1]
    assert ".incoming/20260712T050000Z_aaaaaaaaaaaa/" in rsync[-1]
    remote = ssh[-1]
    assert "shasum -a 256 -c SHA256SUMS" in remote
    assert ".incoming" in remote and "snapshots" in remote
    assert remote.index("shasum") < remote.index("mv") < remote.index("current")
    assert "secrets" not in " ".join(rsync + ssh)


def test_backup_failure_never_publishes_current(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    with pytest.raises(CommandFailure):
        SyncService(FakeRunner(pipeline_error=True), clock=fixed_clock).run(config)
    assert not config.output_dir.joinpath("current").exists()
