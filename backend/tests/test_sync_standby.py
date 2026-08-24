from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "deploy" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import sync_standby as sync_module  # noqa: E402
from failover_common import (  # noqa: E402
    BACKUP_PASSPHRASE_GENERATION_ID_FILE,
    RECOVERY_CRYPTO_GENERATION_ID_FILE,
    CommandFailure,
    DeadlineExceeded,
    read_root_generation_id_file,
)
from sync_standby import (  # noqa: E402
    BackupCapacityExceeded,
    StandbyTarget,
    SyncConfig,
    SyncService,
    validate_environment_file,
)


class ManualTimer:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class FakeRunner:
    def __init__(
        self,
        *,
        dirty: bool = False,
        pipeline_error: bool = False,
        drift_commit_after_dump: bool = False,
        database_size: int = 100,
        timer: ManualTimer | None = None,
        expire_pipeline_at: float | None = None,
        remote_fail_at: str | None = None,
    ) -> None:
        self.dirty = dirty
        self.pipeline_error = pipeline_error
        self.drift_commit_after_dump = drift_commit_after_dump
        self.database_size = database_size
        self.timer = timer
        self.expire_pipeline_at = expire_pipeline_at
        self.remote_fail_at = remote_fail_at
        self.rev_parse_calls = 0
        self.calls: list[list[str]] = []
        self.pipeline_calls: list[tuple[list[str], list[str], Path]] = []
        self.timeouts: list[tuple[str, float | None]] = []

    def run(
        self,
        command: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        input_bytes: bytes | None = None,
        timeout: float | None = None,
    ) -> bytes:
        argv = list(command)
        self.calls.append(argv)
        self.timeouts.append((" ".join(argv), timeout))
        if "SELECT pg_database_size(current_database())" in " ".join(argv):
            return f"{self.database_size}\n".encode()
        if argv[:2] == ["git", "status"]:
            return b" M tracked-file\n" if self.dirty else b""
        if argv[:2] == ["git", "rev-parse"]:
            self.rev_parse_calls += 1
            marker = (
                b"b"
                if self.drift_commit_after_dump and self.rev_parse_calls > 1
                else b"a"
            )
            return (marker * 40) + b"\n"
        if argv[0] == "ssh" and "df -Pk" in " ".join(argv):
            if getattr(self, "remote_fail_at", None) == "capacity":
                return b"/dev/sda1 100 99 0 100% /\n"
            return b"/dev/sda1 100000000 1 99999999 1% /\n"
        if argv[0] == "rsync" and getattr(self, "remote_fail_at", None) == "rsync":
            raise CommandFailure("rsync", 255, "Connection reset by peer")
        if argv[0] == "rsync" and getattr(self, "remote_fail_at", None) == "enospc":
            raise CommandFailure("rsync", 12, "No space left on device")
        if argv[0] == "rsync" and getattr(self, "remote_fail_at", None) == "slow_net":
            raise CommandFailure("rsync", 30, "Connection timed out")
        fail_at = getattr(self, "remote_fail_at", None)
        remote = argv[-1] if argv else ""
        if argv[0] == "ssh" and fail_at == "checksum" and "shasum" in remote:
            raise CommandFailure("ssh", 1, "SHA256 mismatch")
        if (
            argv[0] == "ssh"
            and fail_at == "move"
            and "mv " in remote
            and "current.next" not in remote
        ):
            raise CommandFailure("ssh", 1, "cannot move snapshot")
        if argv[0] == "ssh" and fail_at == "remote_current" and "ln -sfn" in remote:
            raise CommandFailure("ssh", 1, "cannot switch current")
        if argv[0] == "ssh" and fail_at == "ssh_drop" and "shasum" in remote:
            raise CommandFailure("ssh", 255, "Connection reset")
        if argv[0] == "ssh" and fail_at == "digest_mismatch" and "cmp -s" in remote:
            raise CommandFailure("ssh", 1, "snapshot-digest-mismatch")
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
        timeout: float | None = None,
    ) -> None:
        self.pipeline_calls.append((list(producer), list(consumer), output_path))
        self.timeouts.append((" ".join([*producer, *consumer]), timeout))
        if self.pipeline_error:
            raise CommandFailure("pg_dump", 1, "dump failed")
        output_path.write_bytes(b"encrypted-custom-dump")
        output_path.chmod(0o600)
        if self.timer is not None and self.expire_pipeline_at is not None:
            self.timer.value = self.expire_pipeline_at


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
        max_backup_seconds=14400,
        target=target,
    )


def fixed_clock() -> datetime:
    return datetime(2026, 7, 12, 5, 0, tzinfo=UTC)


def generation_reader(path: Path) -> str:
    if path == RECOVERY_CRYPTO_GENERATION_ID_FILE:
        return "recovery-v1"
    if path == BACKUP_PASSPHRASE_GENERATION_ID_FILE:
        return "backup-passphrase-v1"
    raise AssertionError("unexpected generation id path")


def make_sync_service(
    runner: FakeRunner,
    *,
    clock: Callable[[], datetime] = fixed_clock,
) -> SyncService:
    return SyncService(
        runner,
        clock=clock,
        capacity_reader=lambda _path: (10_000, 0),
        generation_reader=generation_reader,
    )


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
        make_sync_service(runner).run(make_config(tmp_path))
    assert runner.pipeline_calls == []


def test_build_only_creates_encrypted_atomic_snapshot_without_remote_calls(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    config = make_config(tmp_path)

    result = make_sync_service(runner).run(config)

    assert result.snapshot_id == "20260712T050000Z_aaaaaaaaaaaa"
    assert result.snapshot_dir.name == result.snapshot_id
    assert config.output_dir.joinpath("current").resolve() == result.snapshot_dir.resolve()
    manifest = json.loads(result.snapshot_dir.joinpath("manifest.json").read_text())
    assert manifest["git_commit"] == "a" * 40
    assert manifest["alembic_version"] == "0008_audit_payload_guard"
    assert manifest["created_at"] == "2026-07-12T05:00:00+00:00"
    assert manifest["secrets_included"] is False
    assert manifest["recovery_crypto_generation_id"] == "recovery-v1"
    assert manifest["backup_passphrase_generation_id"] == "backup-passphrase-v1"
    assert set(manifest["files"]) == {
        "database",
        "repository_archive",
        "environment",
    }
    assert "not-a-production-secret" not in json.dumps(manifest)

    producer, consumer, output = runner.pipeline_calls[0]
    assert "pg_dump" in producer and "--format=custom" in producer
    assert "openssl" in consumer and "-aes-256-cbc" in consumer
    assert output.name.endswith(".dump.enc")
    archive_call = next(call for call in runner.calls if call[:2] == ["git", "archive"])
    assert "HEAD" not in archive_call
    assert "a" * 40 in archive_call
    assert all("secrets" not in item for item in archive_call)
    assert not any(call[0] in {"ssh", "rsync"} for call in runner.calls)


def test_remote_publish_uses_incoming_hash_check_then_atomic_current(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    config = make_config(tmp_path, build_only=False)

    make_sync_service(runner).run(config)

    rsync = next(call for call in runner.calls if call[0] == "rsync")
    ssh_calls = [call for call in runner.calls if call[0] == "ssh"]
    stage = next(call for call in ssh_calls if "shasum" in call[-1])
    commit = next(call for call in ssh_calls if "ln -sfn" in call[-1])
    assert ".incoming/20260712T050000Z_aaaaaaaaaaaa/" in rsync[-1]
    assert "shasum -a 256 -c SHA256SUMS" in stage[-1]
    assert "cmp -s" in stage[-1]
    assert "ln -sfn" not in stage[-1]
    prepare = next(call for call in ssh_calls if "mkdir -p" in call[-1])
    assert f"-mmin +{sync_module.REMOTE_INCOMING_TTL_MINUTES}" in prepare[-1]
    assert sync_module.REMOTE_INCOMING_TTL_MINUTES == 24 * 60
    assert stage[-1].index("shasum") < stage[-1].index("mv")
    assert "current" in commit[-1]
    assert runner.calls.index(stage) < runner.calls.index(commit)
    assert "secrets" not in " ".join(rsync + stage + commit)


def test_backup_failure_never_publishes_current(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    with pytest.raises(CommandFailure):
        make_sync_service(FakeRunner(pipeline_error=True)).run(config)
    assert not config.output_dir.joinpath("current").exists()


def test_source_generation_drift_discards_snapshot_before_publication(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    runner = FakeRunner(drift_commit_after_dump=True)

    with pytest.raises(RuntimeError, match="source generation changed"):
        make_sync_service(runner).run(config)

    assert not config.output_dir.joinpath("current").exists()
    incoming = config.output_dir / ".incoming"
    assert not incoming.exists() or list(incoming.iterdir()) == []


def test_manifest_timestamp_is_pinned_before_long_backup_work(tmp_path: Path) -> None:
    moments = iter(
        (
            datetime(2026, 7, 12, 5, 0, tzinfo=UTC),
            datetime(2026, 7, 12, 11, 0, tzinfo=UTC),
        )
    )
    runner = FakeRunner()
    config = make_config(tmp_path)

    result = make_sync_service(runner, clock=lambda: next(moments)).run(config)

    manifest = json.loads(result.snapshot_dir.joinpath("manifest.json").read_text())
    assert result.snapshot_id.startswith("20260712T050000Z_")
    assert manifest["created_at"] == "2026-07-12T05:00:00+00:00"


@pytest.mark.parametrize(
    ("used_bytes", "allowed"),
    ((550, True), (551, False)),
)
def test_capacity_gate_uses_db_size_times_one_point_five_and_70_percent_boundary(
    tmp_path: Path,
    used_bytes: int,
    allowed: bool,
) -> None:
    config = make_config(tmp_path)
    runner = FakeRunner(database_size=100)
    service = SyncService(
        runner,
        clock=fixed_clock,
        capacity_reader=lambda _path: (1000, used_bytes),
        generation_reader=generation_reader,
    )

    if allowed:
        result = service.run(config)
        assert result.snapshot_dir.is_dir()
    else:
        with pytest.raises(BackupCapacityExceeded):
            service.run(config)
        assert not config.output_dir.exists()
        assert runner.pipeline_calls == []
        assert not any(call[:2] == ["git", "status"] for call in runner.calls)


def test_absolute_deadline_cleans_incoming_and_never_publishes_current(
    tmp_path: Path,
) -> None:
    timer = ManualTimer()
    config = make_config(tmp_path)
    runner = FakeRunner(
        timer=timer,
        expire_pipeline_at=config.max_backup_seconds + 0.01,
    )

    with pytest.raises(DeadlineExceeded):
        SyncService(
            runner,
            clock=fixed_clock,
            timer=timer,
            capacity_reader=lambda _path: (10_000, 0),
            generation_reader=generation_reader,
        ).run(config)

    incoming = config.output_dir / ".incoming"
    assert not incoming.exists() or list(incoming.iterdir()) == []
    assert not config.output_dir.joinpath("current").exists()
    assert all(
        timeout is not None and 0 < timeout <= config.max_backup_seconds
        for _command, timeout in runner.timeouts
    )


def test_deadline_during_local_publish_rolls_back_current_and_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timer = ManualTimer()
    config = make_config(tmp_path)
    old_snapshot = config.output_dir / "snapshots/20260711T050000Z_bbbbbbbbbbbb"
    old_snapshot.mkdir(parents=True)
    config.output_dir.joinpath("current").symlink_to(
        Path("snapshots") / old_snapshot.name
    )
    original_replace = sync_module.os.replace

    def expiring_replace(source: Path | str, destination: Path | str) -> None:
        original_replace(source, destination)
        if Path(source).parent.name == ".incoming":
            timer.value = config.max_backup_seconds + 0.01

    monkeypatch.setattr(sync_module.os, "replace", expiring_replace)
    with pytest.raises(DeadlineExceeded):
        SyncService(
            FakeRunner(),
            clock=fixed_clock,
            timer=timer,
            capacity_reader=lambda _path: (10_000, 0),
            generation_reader=generation_reader,
        ).run(config)

    assert config.output_dir.joinpath("current").resolve() == old_snapshot.resolve()
    snapshots = config.output_dir / "snapshots"
    assert list(snapshots.iterdir()) == [old_snapshot]
    incoming = config.output_dir / ".incoming"
    assert not incoming.exists() or list(incoming.iterdir()) == []


def test_generation_id_reader_requires_regular_owned_0600_single_line(
    tmp_path: Path,
) -> None:
    generation = tmp_path / "generation-id"
    generation.write_text("recovery-generation.v1\n", encoding="ascii")
    generation.chmod(0o600)
    owner = {"expected_uid": os.getuid(), "expected_gid": os.getgid()}

    assert read_root_generation_id_file(generation, **owner) == (
        "recovery-generation.v1"
    )

    generation.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        read_root_generation_id_file(generation, **owner)
    generation.unlink()
    generation.symlink_to(tmp_path / "missing-target")
    with pytest.raises(ValueError, match="symlink"):
        read_root_generation_id_file(generation, **owner)
    generation.unlink()
    with pytest.raises(ValueError, match="unavailable"):
        read_root_generation_id_file(generation, **owner)
    generation.write_text("invalid id with spaces\n", encoding="ascii")
    generation.chmod(0o600)
    with pytest.raises(ValueError, match="invalid generation"):
        read_root_generation_id_file(generation, **owner)


def test_generation_rotation_during_long_backup_discards_incoming(
    tmp_path: Path,
) -> None:
    calls: dict[Path, int] = {}

    def rotating_reader(path: Path) -> str:
        calls[path] = calls.get(path, 0) + 1
        prefix = "recovery" if path == RECOVERY_CRYPTO_GENERATION_ID_FILE else "backup"
        return f"{prefix}-v{calls[path]}"

    config = make_config(tmp_path)
    with pytest.raises(RuntimeError, match="source generation changed"):
        SyncService(
            FakeRunner(),
            clock=fixed_clock,
            capacity_reader=lambda _path: (10_000, 0),
            generation_reader=rotating_reader,
        ).run(config)

    assert calls == {
        RECOVERY_CRYPTO_GENERATION_ID_FILE: 2,
        BACKUP_PASSPHRASE_GENERATION_ID_FILE: 2,
    }
    assert not config.output_dir.joinpath("current").exists()
    incoming = config.output_dir / ".incoming"
    assert not incoming.exists() or list(incoming.iterdir()) == []


class SimulatedPowerLoss(RuntimeError):
    """生产等价文件系统上的掉电注入：fsync 之后进程消失。"""


@pytest.mark.parametrize(
    "barrier",
    ("dump_fsync", "archive_fsync", "payload_durable", "snapshot_rename", "current_switch"),
)
def test_power_loss_at_each_local_commit_barrier_does_not_report_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    barrier: str,
) -> None:
    config = make_config(tmp_path)

    def inject(name: str, path: Path | None = None) -> None:
        if name == barrier:
            raise SimulatedPowerLoss(name)

    monkeypatch.setattr(sync_module, "durability_barrier", inject)
    with pytest.raises(SimulatedPowerLoss, match=barrier):
        make_sync_service(FakeRunner()).run(config)
    current = config.output_dir / "current"
    if barrier in {"dump_fsync", "archive_fsync", "payload_durable", "snapshot_rename"}:
        assert not current.exists() or (
            current.is_symlink()
            and "20260712T050000Z_aaaaaaaaaaaa" not in str(current.readlink())
        )


def test_successful_backup_is_power_loss_durable_after_all_barriers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def record(name: str, path: Path | None = None) -> None:
        seen.append(name)

    monkeypatch.setattr(sync_module, "durability_barrier", record)
    config = make_config(tmp_path)
    result = make_sync_service(FakeRunner()).run(config)
    remount = config.output_dir / "snapshots" / result.snapshot_id
    assert remount.is_dir()
    assert (remount / "SHA256SUMS").is_file()
    assert (remount / "manifest.json").is_file()
    assert config.output_dir.joinpath("current").resolve() == remount.resolve()
    assert seen == [
        "dump_fsync",
        "archive_fsync",
        "payload_durable",
        "snapshot_rename",
        "current_switch",
    ]


@pytest.mark.parametrize(
    "fail_at",
    (
        "rsync",
        "checksum",
        "move",
        "remote_current",
        "ssh_drop",
        "enospc",
        "slow_net",
        "capacity",
    ),
)
def test_remote_publish_failures_converge_without_leading_current(
    tmp_path: Path,
    fail_at: str,
) -> None:
    runner = FakeRunner(remote_fail_at=fail_at)
    config = make_config(tmp_path, build_only=False)
    with pytest.raises((CommandFailure, BackupCapacityExceeded)):
        make_sync_service(runner).run(config)
    commit_calls = [
        call for call in runner.calls if call[0] == "ssh" and "ln -sfn" in call[-1]
    ]
    rollback_calls = [
        call
        for call in runner.calls
        if call[0] == "ssh" and "readlink current" in call[-1]
    ]
    cleanup_calls = [
        call
        for call in runner.calls
        if call[0] == "ssh" and "rm -rf" in call[-1] and ".incoming" in call[-1]
    ]
    assert commit_calls == [] or fail_at == "remote_current"
    if fail_at == "remote_current":
        assert rollback_calls or cleanup_calls
    else:
        assert cleanup_calls or rollback_calls
    incoming = config.output_dir / ".incoming"
    assert not incoming.exists() or list(incoming.iterdir()) == []


def test_remote_existing_target_with_different_digest_fails_closed(tmp_path: Path) -> None:
    runner = FakeRunner(remote_fail_at="digest_mismatch")
    config = make_config(tmp_path, build_only=False)
    with pytest.raises(CommandFailure, match="digest"):
        make_sync_service(runner).run(config)
    assert not any(
        call[0] == "ssh" and "ln -sfn" in call[-1] for call in runner.calls
    )


def test_local_failure_after_remote_stage_rolls_back_uncommitted_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = FakeRunner()
    config = make_config(tmp_path, build_only=False)

    def fail_local(self: SyncService, *args: object, **kwargs: object) -> Path:
        raise RuntimeError("local publish failed after remote stage")

    monkeypatch.setattr(sync_module.SyncService, "_publish_local", fail_local)
    with pytest.raises(RuntimeError, match="local publish"):
        make_sync_service(runner).run(config)
    assert any("readlink current" in call[-1] for call in runner.calls if call[0] == "ssh")
    assert not any(
        call[0] == "ssh" and "ln -sfn" in call[-1] for call in runner.calls
    )


def test_retry_same_snapshot_is_idempotent(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    first = make_sync_service(FakeRunner()).run(config)
    second = make_sync_service(FakeRunner()).run(config)
    assert first.snapshot_id == second.snapshot_id
    snapshots = list((config.output_dir / "snapshots").iterdir())
    assert [item.name for item in snapshots] == [first.snapshot_id]


def test_build_only_path_still_skips_remote(tmp_path: Path) -> None:
    runner = FakeRunner()
    make_sync_service(runner).run(make_config(tmp_path, build_only=True))
    assert not any(call[0] in {"ssh", "rsync"} for call in runner.calls)


def test_remote_enospc_does_not_pollute_next_backup(tmp_path: Path) -> None:
    config = make_config(tmp_path, build_only=False)
    with pytest.raises(CommandFailure, match="space"):
        make_sync_service(FakeRunner(remote_fail_at="enospc")).run(config)
    assert not config.output_dir.joinpath("current").exists()
    result = make_sync_service(FakeRunner()).run(config)
    assert config.output_dir.joinpath("current").resolve() == result.snapshot_dir.resolve()
    assert (result.snapshot_dir / "SHA256SUMS").is_file()
