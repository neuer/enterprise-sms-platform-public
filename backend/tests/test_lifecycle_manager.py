from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "deploy" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from failover_common import atomic_write_json, sha256_file  # noqa: E402
from lifecycle_manager import (  # noqa: E402
    LifecycleConfig,
    LifecycleService,
    _alert,
    load_config,
)
from restore_drill import RestoreConfig, RestoreResult  # noqa: E402
from sync_standby import SyncConfig, SyncResult  # noqa: E402

SNAPSHOT_ONE = "20260501T050000Z_aaaaaaaaaaaa"
SNAPSHOT_TWO = "20260502T050000Z_bbbbbbbbbbbb"
SNAPSHOT_THREE = "20260712T050000Z_cccccccccccc"


def fixed_clock() -> datetime:
    return datetime(2026, 7, 12, 6, 0, tzinfo=UTC)


class FakeSync:
    def __init__(
        self,
        snapshots: Sequence[tuple[str, datetime]],
        *,
        corrupt: bool = False,
    ) -> None:
        self.snapshots = iter(snapshots)
        self.corrupt = corrupt
        self.calls: list[SyncConfig] = []

    def run(self, config: SyncConfig) -> SyncResult:
        self.calls.append(config)
        snapshot_id, created_at = next(self.snapshots)
        snapshot = config.output_dir / "snapshots" / snapshot_id
        snapshot.mkdir(parents=True, mode=0o700)
        contents = {
            "database": (f"sms_{snapshot_id}.dump.enc", b"encrypted-database"),
            "repository_archive": (f"repository_{snapshot_id}.tar.gz", b"repository"),
            "environment": ("production.env", b"DEBUG=0\n"),
        }
        files: dict[str, dict[str, object]] = {}
        for label, (name, body) in contents.items():
            path = snapshot / name
            path.write_bytes(body)
            path.chmod(0o600)
            files[label] = {
                "name": name,
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
            }
        manifest = snapshot / "manifest.json"
        atomic_write_json(
            manifest,
            {
                "schema_version": 1,
                "snapshot_id": snapshot_id,
                "created_at": created_at.isoformat(),
                "git_commit": snapshot_id.rsplit("_", 1)[1] * 4,
                "alembic_version": "0008_audit_payload_guard",
                "database": "sms",
                "secrets_included": False,
                "files": files,
            },
        )
        checksum_lines = [
            f"{item['sha256']}  {item['name']}" for item in files.values()
        ]
        checksum_lines.append(f"{sha256_file(manifest)}  manifest.json")
        checksums = snapshot / "SHA256SUMS"
        checksums.write_text("\n".join(checksum_lines) + "\n", encoding="ascii")
        checksums.chmod(0o600)
        if self.corrupt:
            database = snapshot / str(files["database"]["name"])
            database.write_bytes(b"tampered")
            database.chmod(0o600)
        next_link = config.output_dir / ".current.next"
        next_link.unlink(missing_ok=True)
        next_link.symlink_to(Path("snapshots") / snapshot_id)
        next_link.replace(config.output_dir / "current")
        return SyncResult(snapshot_id, snapshot, False)


class FakeRestore:
    def __init__(self, *, within_rto: bool = True) -> None:
        self.within_rto = within_rto
        self.calls: list[RestoreConfig] = []

    def run(self, config: RestoreConfig) -> RestoreResult:
        self.calls.append(config)
        atomic_write_json(
            config.report_file,
            {
                "schema_version": 1,
                "status": "success" if self.within_rto else "rto_failed",
                "snapshot_id": config.manifest_file.parent.name,
            },
        )
        return RestoreResult(
            "sms_drill_20260712060000_ab12",
            25.5,
            self.within_rto,
        )


def make_config(tmp_path: Path) -> LifecycleConfig:
    environment = tmp_path / "production.env"
    environment.write_text("DEBUG=0\nAUTH_MOCK=0\nVENDOR_MOCK=0\n", encoding="utf-8")
    environment.chmod(0o600)
    return LifecycleConfig(
        environment_file=environment,
        output_root=tmp_path / "backups",
        database="sms",
        retention_days=30,
        minimum_snapshots=2,
        max_backup_age_hours=24,
        max_restore_age_hours=168,
        max_restore_seconds=1800,
    )


def make_service(
    tmp_path: Path,
    sync: FakeSync,
    restore: FakeRestore | None = None,
    *,
    chooser=lambda items: items[0],
) -> LifecycleService:
    repository = tmp_path / "repo"
    repository.mkdir(exist_ok=True)
    (repository / "deploy").mkdir(exist_ok=True)
    (repository / "deploy/docker-compose.yml").write_text(
        "services: {}\n", encoding="utf-8"
    )
    passphrase = tmp_path / "backup-passphrase"
    passphrase.write_text("outside-repository", encoding="utf-8")
    passphrase.chmod(0o600)
    return LifecycleService(
        repository,
        passphrase,
        sync_service=sync,
        restore_service=restore or FakeRestore(),
        clock=fixed_clock,
        chooser=chooser,
    )


def read_state(config: LifecycleConfig) -> dict[str, object]:
    return json.loads(
        config.output_root.joinpath("lifecycle-state.json").read_text(encoding="utf-8")
    )


def test_load_config_requires_exact_0600_path_only_contract(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle.json"
    value = {
        "schema_version": 1,
        "environment_file": "/etc/sms-platform/production.env",
        "output_root": "/var/lib/sms-platform/backups",
        "database": "sms",
        "retention_days": 35,
        "minimum_snapshots": 2,
        "max_backup_age_hours": 24,
        "max_restore_age_hours": 168,
        "max_restore_seconds": 1800,
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)

    config = load_config(path)

    assert config.retention_days == 35
    path.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        load_config(path)
    path.chmod(0o600)
    value["extra"] = "forbidden"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="fields"):
        load_config(path)


def test_backup_records_integrity_but_is_unavailable_until_restore(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    sync = FakeSync([(SNAPSHOT_THREE, datetime(2026, 7, 12, 5, 0, tzinfo=UTC))])
    service = make_service(tmp_path, sync)

    evidence = service.backup(config)

    state = read_state(config)
    snapshot = state["snapshots"][SNAPSHOT_THREE]  # type: ignore[index]
    assert evidence.snapshot_id == SNAPSHOT_THREE
    assert snapshot["integrity_verified"] is True
    assert snapshot["restore_verified"] is False
    assert snapshot["available"] is False
    assert state["last_successful_backup"]["snapshot_id"] == SNAPSHOT_THREE  # type: ignore[index]
    assert stat_mode(config.output_root / "lifecycle-state.json") == 0o600


def test_backup_rejects_repository_output_and_symlink_children_before_sync(
    tmp_path: Path,
) -> None:
    sync = FakeSync([(SNAPSHOT_THREE, datetime(2026, 7, 12, 5, 0, tzinfo=UTC))])
    service = make_service(tmp_path, sync)
    config = replace(make_config(tmp_path), output_root=service.repository_root / "backup")
    with pytest.raises(ValueError, match="outside"):
        service.backup(config)
    assert sync.calls == []

    config = make_config(tmp_path)
    config.output_root.mkdir(mode=0o700)
    outside = tmp_path / "outside-snapshots"
    outside.mkdir()
    config.output_root.joinpath("snapshots").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        service.backup(config)
    assert sync.calls == []


def test_random_restore_makes_only_verified_snapshot_available_and_proves_rpo_rto(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    sync = FakeSync([(SNAPSHOT_THREE, datetime(2026, 7, 12, 5, 0, tzinfo=UTC))])
    restore = FakeRestore()
    service = make_service(
        tmp_path,
        sync,
        restore,
        chooser=lambda candidates: candidates[-1],
    )
    service.backup(config)

    evidence = service.drill(config)
    status = service.status(config)

    state = read_state(config)
    snapshot = state["snapshots"][SNAPSHOT_THREE]  # type: ignore[index]
    assert restore.calls[0].keep is False
    assert restore.calls[0].drill_environment is False
    assert snapshot["available"] is True
    assert snapshot["restore_verified"] is True
    assert evidence["restore_seconds"] == 25.5
    assert evidence["data_gap_seconds"] == 3600
    assert status.healthy is True
    assert status.evidence["backup_age_seconds"] == 0
    assert status.evidence["restore_age_seconds"] == 0
    assert status.evidence["usable_snapshot_count"] == 1


def test_corrupt_backup_remains_unavailable_and_failure_state_has_no_details(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    sync = FakeSync(
        [(SNAPSHOT_THREE, datetime(2026, 7, 12, 5, 0, tzinfo=UTC))],
        corrupt=True,
    )
    service = make_service(tmp_path, sync)

    with pytest.raises(ValueError, match="integrity"):
        service.backup(config)

    state = read_state(config)
    snapshot = state["snapshots"][SNAPSHOT_THREE]  # type: ignore[index]
    assert snapshot["integrity_verified"] is False
    assert snapshot["available"] is False
    assert state["last_successful_backup"] is None
    assert set(state["last_failure"]) == {"at", "operation", "error_type"}  # type: ignore[arg-type]
    serialized = json.dumps(state).casefold()
    assert "pii-marker" not in serialized
    assert "outside-repository" not in serialized


def test_retention_deletes_only_expired_snapshots_and_keeps_minimum(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    sync = FakeSync(
        [
            (SNAPSHOT_ONE, datetime(2026, 5, 1, 5, 0, tzinfo=UTC)),
            (SNAPSHOT_TWO, datetime(2026, 5, 2, 5, 0, tzinfo=UTC)),
            (SNAPSHOT_THREE, datetime(2026, 7, 12, 5, 0, tzinfo=UTC)),
        ]
    )
    service = make_service(tmp_path, sync)

    service.backup(config)
    service.backup(config)
    service.backup(config)

    state = read_state(config)
    assert set(state["snapshots"]) == {SNAPSHOT_TWO, SNAPSHOT_THREE}  # type: ignore[arg-type]
    assert not config.output_root.joinpath("snapshots", SNAPSHOT_ONE).exists()
    assert config.output_root.joinpath("snapshots", SNAPSHOT_TWO).is_dir()
    assert state["retention"]["removed_count"] == 1  # type: ignore[index]
    assert state["retention"]["retained_count"] == 2  # type: ignore[index]


def test_failed_restore_never_marks_snapshot_usable(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    sync = FakeSync([(SNAPSHOT_THREE, datetime(2026, 7, 12, 5, 0, tzinfo=UTC))])
    service = make_service(tmp_path, sync, FakeRestore(within_rto=False))
    service.backup(config)

    with pytest.raises(RuntimeError, match="RTO"):
        service.drill(config)

    state = read_state(config)
    snapshot = state["snapshots"][SNAPSHOT_THREE]  # type: ignore[index]
    assert snapshot["available"] is False
    assert snapshot["restore_verified"] is False
    assert state["last_successful_restore"] is None


def test_status_fails_closed_without_recent_backup_and_restore(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    service = make_service(tmp_path, FakeSync([]))

    status = service.status(config)

    assert status.healthy is False
    assert status.evidence["backup_age_seconds"] is None
    assert status.evidence["restore_age_seconds"] is None
    assert status.evidence["usable_snapshot_count"] == 0
    state = read_state(config)
    assert state["last_failure"]["error_type"] == "EvidenceStale"  # type: ignore[index]


def test_status_revokes_available_snapshot_when_ciphertext_changes(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    sync = FakeSync([(SNAPSHOT_THREE, datetime(2026, 7, 12, 5, 0, tzinfo=UTC))])
    service = make_service(tmp_path, sync)
    service.backup(config)
    service.drill(config)
    snapshot = config.output_root / "snapshots" / SNAPSHOT_THREE
    database = next(snapshot.glob("*.dump.enc"))
    database.write_bytes(b"changed-after-restore")
    database.chmod(0o600)

    status = service.status(config)

    assert status.healthy is False
    state = read_state(config)
    item = state["snapshots"][SNAPSHOT_THREE]  # type: ignore[index]
    assert item["available"] is False
    assert item["integrity_verified"] is False
    assert state["last_failure"]["error_type"] == "SnapshotIntegrityFailed"  # type: ignore[index]


def test_alert_output_never_includes_exception_message_or_sensitive_values(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _alert(
        "backup",
        ValueError("pii=PII-MARKER credential-marker=private-value /private/path"),
    )

    output = capsys.readouterr()
    assert output.out == ""
    assert '"event": "lifecycle_alert"' in output.err
    assert '"error_type": "ValueError"' in output.err
    for forbidden in ("PII-MARKER", "private-value", "/private/path"):
        assert forbidden not in output.err


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777
