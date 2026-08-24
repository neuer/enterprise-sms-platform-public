from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "deploy" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import lifecycle_manager as lifecycle_manager_module  # noqa: E402
from failover_common import (  # noqa: E402
    BACKUP_PASSPHRASE_GENERATION_ID_FILE,
    RECOVERY_CRYPTO_GENERATION_ID_FILE,
    DeadlineExceeded,
    atomic_write_json,
    sha256_file,
)
from lifecycle_manager import (  # noqa: E402
    PRODUCTION_BACKUP_ROOT,
    LifecycleConfig,
    LifecycleService,
    LifecycleStatus,
    _alert,
    _runtime_config,
    _serialize_lifecycle_cli_result,
    load_config,
)
from restore_drill import (  # noqa: E402
    CRYPTO_PROBE_COVERAGE_FIELDS,
    RestoreConfig,
    RestoreResult,
)
from sync_standby import SyncConfig, SyncResult  # noqa: E402

SNAPSHOT_ONE = "20260501T050000Z_aaaaaaaaaaaa"
SNAPSHOT_TWO = "20260502T050000Z_bbbbbbbbbbbb"
SNAPSHOT_THREE = "20260712T050000Z_cccccccccccc"
RECOVERY_GENERATION_ID = "recovery-v1"
BACKUP_GENERATION_ID = "backup-passphrase-v1"


class ManualTimer:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


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
                "git_commit": (snapshot_id.rsplit("_", 1)[1] * 4)[:40],
                "alembic_version": "0008_audit_payload_guard",
                "database": "sms",
                "secrets_included": False,
                "recovery_crypto_generation_id": RECOVERY_GENERATION_ID,
                "backup_passphrase_generation_id": BACKUP_GENERATION_ID,
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
    def __init__(self, *, within_restore_budget: bool = True) -> None:
        self.within_restore_budget = within_restore_budget
        self.calls: list[RestoreConfig] = []

    def run(self, config: RestoreConfig) -> RestoreResult:
        self.calls.append(config)
        manifest = json.loads(config.manifest_file.read_text(encoding="utf-8"))
        empty_crypto_receipt = {
            "schema_version": 2,
            "status": "not_applicable_empty",
            "counts": {
                "audit_context_keys": 4,
                "encrypted_columns": len(CRYPTO_PROBE_COVERAGE_FIELDS),
                "encrypted_rows": 0,
                "ciphertext_samples_verified": 0,
                "key_version_columns": 8,
                "referenced_key_versions": 0,
                "sms_message_rows": 0,
            },
            "coverage": {
                label: {"rows": 0, "key_versions_verified": 0}
                for label in CRYPTO_PROBE_COVERAGE_FIELDS
            },
        }
        atomic_write_json(
            config.report_file,
            {
                "schema_version": 2,
                "status": (
                    "success" if self.within_restore_budget else "budget_failed"
                ),
                "metric_scope": "database_restore",
                "business_rto_evidence": False,
                "snapshot_id": config.manifest_file.parent.name,
                "git_commit": manifest["git_commit"],
                "database": "sms_drill_20260712060000_ab12",
                "started_at": "2026-07-12T05:59:00+00:00",
                "finished_at": "2026-07-12T06:00:00+00:00",
                "restore_seconds": 25.5,
                "restore_budget_seconds": config.max_restore_seconds,
                "within_restore_budget": self.within_restore_budget,
                "recovery_crypto_generation_id": manifest[
                    "recovery_crypto_generation_id"
                ],
                "backup_passphrase_generation_id": manifest[
                    "backup_passphrase_generation_id"
                ],
                "checks": {
                    "alembic_version": manifest["alembic_version"],
                    "role_flags": "7|true",
                    "audit_privileges": "true",
                    "crypto_generation_binding": "matched_host_generation_ids",
                    "historical_ciphertext_validation": "not_applicable_empty",
                    "pre_migration_crypto_validation": "not_applicable_empty",
                    "post_migration_crypto_validation": "not_applicable_empty",
                },
                "crypto_probe_receipts": {
                    "pre_migration": empty_crypto_receipt,
                    "post_migration": empty_crypto_receipt,
                },
                "table_counts": {
                    "sms_batch": 0,
                    "sms_message": 0,
                    "audit_log": 0,
                    "raw_vendor_log": 0,
                },
            },
        )
        return RestoreResult(
            "sms_drill_20260712060000_ab12",
            25.5,
            self.within_restore_budget,
        )


class TamperingRestore(FakeRestore):
    def __init__(self, path: tuple[str, ...], replacement: object) -> None:
        super().__init__()
        self.path = path
        self.replacement = replacement

    def run(self, config: RestoreConfig) -> RestoreResult:
        result = super().run(config)
        report: dict[str, Any] = json.loads(
            config.report_file.read_text(encoding="utf-8")
        )
        target = report
        for key in self.path[:-1]:
            nested = target[key]
            assert isinstance(nested, dict)
            target = nested
        target[self.path[-1]] = self.replacement
        atomic_write_json(config.report_file, report)
        return result


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
        max_backup_seconds=14400,
        max_backup_age_hours=24,
        max_restore_age_hours=168,
        max_restore_seconds=43200,
    )


def make_service(
    tmp_path: Path,
    sync: FakeSync,
    restore: FakeRestore | None = None,
    *,
    timer: ManualTimer | None = None,
    generation_reader=None,
    marker_validator=lambda: None,
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
        timer=timer or ManualTimer(),
        generation_reader=generation_reader or stable_generation_reader,
        marker_validator=marker_validator,
    )


def stable_generation_reader(path: Path) -> str:
    if path == RECOVERY_CRYPTO_GENERATION_ID_FILE:
        return RECOVERY_GENERATION_ID
    if path == BACKUP_PASSPHRASE_GENERATION_ID_FILE:
        return BACKUP_GENERATION_ID
    raise AssertionError("unexpected generation id path")


def read_state(config: LifecycleConfig) -> dict[str, object]:
    return json.loads(
        config.output_root.joinpath("lifecycle-state.json").read_text(encoding="utf-8")
    )


def valid_drill_cli_result() -> dict[str, object]:
    return {
        "snapshot_id": SNAPSHOT_THREE,
        "finished_at": "2026-07-12T06:00:00+00:00",
        "restore_seconds": 25.5,
        "drill_budget_seconds": 43200.0,
        "restore_step_budget_seconds": 43199.5,
        "within_drill_budget": True,
        "data_gap_seconds": 3600,
        "report_sha256": "a" * 64,
        "recovery_crypto_generation_id": "RECOVERY-SECRET-VALUE-MARKER",
        "backup_passphrase_generation_id": "BACKUP-PASSPHRASE-VALUE-MARKER",
    }


def run_main_with_drill_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: Mapping[str, object],
) -> int:
    class StubLifecycleService:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def drill(self, _config: LifecycleConfig) -> Mapping[str, object]:
            return result

    monkeypatch.setattr(
        lifecycle_manager_module,
        "_runtime_config",
        lambda: (
            make_config(tmp_path),
            Path("/run/backup-secrets/CONFIG-PASSPHRASE-PATH-MARKER"),
        ),
    )
    monkeypatch.setattr(
        lifecycle_manager_module, "LifecycleService", StubLifecycleService
    )
    monkeypatch.setattr(sys, "argv", ["lifecycle_manager.py", "drill"])
    return lifecycle_manager_module.main()


def test_load_config_requires_exact_0600_path_only_contract(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle.json"
    value = {
        "schema_version": 1,
        "environment_file": "/etc/sms-platform/production.env",
        "output_root": "/var/lib/sms-platform/runtime/backups",
        "recovery_crypto_generation_id_file": str(
            RECOVERY_CRYPTO_GENERATION_ID_FILE
        ),
        "backup_passphrase_generation_id_file": str(
            BACKUP_PASSPHRASE_GENERATION_ID_FILE
        ),
        "database": "sms",
        "retention_days": 35,
        "minimum_snapshots": 2,
        "max_backup_seconds": 14400,
        "max_backup_age_hours": 24,
        "max_restore_age_hours": 168,
        "max_restore_seconds": 43200,
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)

    config = load_config(path)

    assert config.retention_days == 35
    assert config.max_backup_seconds == 14400
    path.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        load_config(path)
    path.chmod(0o600)
    value["extra"] = "forbidden"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="fields"):
        load_config(path)


def test_runtime_config_fixes_production_backup_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "lifecycle.json"
    value = {
        "schema_version": 1,
        "environment_file": "/etc/sms-platform/production.env",
        "output_root": str(PRODUCTION_BACKUP_ROOT),
        "recovery_crypto_generation_id_file": str(
            RECOVERY_CRYPTO_GENERATION_ID_FILE
        ),
        "backup_passphrase_generation_id_file": str(
            BACKUP_PASSPHRASE_GENERATION_ID_FILE
        ),
        "database": "sms",
        "retention_days": 35,
        "minimum_snapshots": 2,
        "max_backup_seconds": 14400,
        "max_backup_age_hours": 24,
        "max_restore_age_hours": 168,
        "max_restore_seconds": 43200,
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setenv("SMS_LIFECYCLE_CONFIG", str(path))
    monkeypatch.setenv("BACKUP_PASSPHRASE_FILE", "/run/backup-secrets/passphrase")

    config, _ = _runtime_config()

    assert config.output_root == PRODUCTION_BACKUP_ROOT
    value["output_root"] = "/var/lib/sms-platform/backups"
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(ValueError, match="output root is fixed"):
        _runtime_config()


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
    assert sync.calls[0].max_backup_seconds == config.max_backup_seconds
    assert stat_mode(config.output_root / "lifecycle-state.json") == 0o600


def test_load_config_rejects_backup_deadline_without_outer_cleanup_margin(
    tmp_path: Path,
) -> None:
    path = tmp_path / "lifecycle.json"
    value = {
        "schema_version": 1,
        "environment_file": "/etc/sms-platform/production.env",
        "output_root": "/var/lib/sms-platform/runtime/backups",
        "recovery_crypto_generation_id_file": str(
            RECOVERY_CRYPTO_GENERATION_ID_FILE
        ),
        "backup_passphrase_generation_id_file": str(
            BACKUP_PASSPHRASE_GENERATION_ID_FILE
        ),
        "database": "sms",
        "retention_days": 35,
        "minimum_snapshots": 2,
        "max_backup_seconds": 14401,
        "max_backup_age_hours": 24,
        "max_restore_age_hours": 168,
        "max_restore_seconds": 43200,
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(ValueError, match="max backup seconds"):
        load_config(path)


def test_backup_status_checks_latest_backup_rpo_without_counting_restore(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    sync = FakeSync([(SNAPSHOT_THREE, datetime(2026, 7, 12, 5, 0, tzinfo=UTC))])
    service = make_service(tmp_path, sync)
    service.backup(config)

    before_drill = service.backup_status(config)
    service.drill(config)
    after_drill = service.backup_status(config)

    assert before_drill.healthy is True
    assert after_drill.healthy is True
    assert after_drill.evidence == before_drill.evidence
    assert after_drill.evidence["evidence_scope"] == "production_latest_backup"
    assert after_drill.evidence["snapshot_id"] == SNAPSHOT_THREE
    assert after_drill.evidence["snapshot_age_seconds"] == 3600
    assert after_drill.evidence["integrity_verified"] is True
    assert after_drill.evidence["restore_evidence_counted"] is False
    assert "last_successful_restore" not in after_drill.evidence


def test_backup_status_fails_closed_without_backup_or_after_ciphertext_change(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    sync = FakeSync([(SNAPSHOT_THREE, datetime(2026, 7, 12, 5, 0, tzinfo=UTC))])
    service = make_service(tmp_path, sync)

    missing = service.backup_status(config)

    assert missing.healthy is False
    assert missing.evidence["failure_type"] == "EvidenceStale"
    service.backup(config)
    database = next(
        config.output_root.joinpath("snapshots", SNAPSHOT_THREE).glob("*.dump.enc")
    )
    database.write_bytes(b"changed-after-backup")
    database.chmod(0o600)

    corrupt = service.backup_status(config)

    assert corrupt.healthy is False
    assert corrupt.evidence["failure_type"] == "SnapshotIntegrityFailed"
    state = read_state(config)
    item = state["snapshots"][SNAPSHOT_THREE]  # type: ignore[index]
    assert item["integrity_verified"] is False
    assert item["available"] is False
    assert state["last_failure"]["operation"] == "backup-status"  # type: ignore[index]


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


def test_latest_restore_makes_same_snapshot_available_with_engineering_evidence(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    sync = FakeSync([(SNAPSHOT_THREE, datetime(2026, 7, 12, 5, 0, tzinfo=UTC))])
    restore = FakeRestore()
    service = make_service(
        tmp_path,
        sync,
        restore,
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
    assert status.evidence["snapshot_age_seconds"] == 3600
    assert status.evidence["restore_age_seconds"] == 0
    assert status.evidence["usable_snapshot_count"] == 1
    assert status.evidence["recovery_point_snapshot_id"] == SNAPSHOT_THREE
    assert status.evidence["recovery_point_age_seconds"] == 3600
    assert status.evidence["evidence_scope"] == "preproduction_restore"


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        pytest.param(("git_commit",), "d" * 40, id="git-commit"),
        pytest.param(
            ("database",),
            "sms_drill_20260712060000_cd34",
            id="result-database-binding",
        ),
        pytest.param(("database",), "sms", id="drill-database-name"),
        pytest.param(
            ("checks", "alembic_version"),
            "0009_other_head",
            id="alembic-version",
        ),
        pytest.param(
            ("checks", "role_flags"),
            "6|true",
            id="role-flags",
        ),
        pytest.param(
            ("checks", "audit_privileges"),
            "false",
            id="audit-privileges",
        ),
        pytest.param(
            ("started_at",),
            "2026-07-12T06:01:00+00:00",
            id="timestamp-order",
        ),
        pytest.param(
            ("finished_at",),
            "2026-07-12T06:00:00Z",
            id="strict-utc-timestamp",
        ),
        pytest.param(
            ("restore_budget_seconds",),
            43199.0,
            id="restore-budget-binding",
        ),
    ],
)
def test_drill_rejects_tampered_restore_report_contract_fields(
    tmp_path: Path,
    path: tuple[str, ...],
    replacement: object,
) -> None:
    config = make_config(tmp_path)
    sync = FakeSync([(SNAPSHOT_THREE, datetime(2026, 7, 12, 5, 0, tzinfo=UTC))])
    service = make_service(tmp_path, sync, TamperingRestore(path, replacement))
    service.backup(config)

    with pytest.raises(ValueError, match="generation binding"):
        service.drill(config)

    state = read_state(config)
    snapshot = state["snapshots"][SNAPSHOT_THREE]  # type: ignore[index]
    assert snapshot["available"] is False
    assert snapshot["restore_verified"] is False
    assert state["last_successful_restore"] is None
    reports = config.output_root / "reports"
    assert not reports.exists() or list(reports.iterdir()) == []


def test_lifecycle_drill_requires_valid_preproduction_marker(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    sync = FakeSync([(SNAPSHOT_THREE, datetime(2026, 7, 12, 5, 0, tzinfo=UTC))])
    restore = FakeRestore()

    def reject_marker() -> None:
        raise ValueError("preproduction restore marker violates host contract")

    service = make_service(
        tmp_path,
        sync,
        restore,
        marker_validator=reject_marker,
    )
    service.backup(config)

    with pytest.raises(ValueError, match="marker"):
        service.drill(config)

    assert restore.calls == []
    state = read_state(config)
    snapshot = state["snapshots"][SNAPSHOT_THREE]  # type: ignore[index]
    assert snapshot["available"] is False
    assert state["last_successful_restore"] is None


def test_outer_drill_deadline_removes_report_and_never_marks_available(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    timer = ManualTimer()
    sync = FakeSync([(SNAPSHOT_THREE, datetime(2026, 7, 12, 5, 0, tzinfo=UTC))])

    class ExpiringRestore(FakeRestore):
        def run(self, restore_config: RestoreConfig) -> RestoreResult:
            result = super().run(restore_config)
            timer.value = config.max_restore_seconds + 0.01
            return result

    service = make_service(
        tmp_path,
        sync,
        ExpiringRestore(),
        timer=timer,
    )
    service.backup(config)

    with pytest.raises(DeadlineExceeded):
        service.drill(config)

    state = read_state(config)
    snapshot = state["snapshots"][SNAPSHOT_THREE]  # type: ignore[index]
    assert snapshot["available"] is False
    assert snapshot["restore_verified"] is False
    assert state["last_successful_restore"] is None
    reports = config.output_root / "reports"
    assert not reports.exists() or list(reports.iterdir()) == []


def test_generation_rotation_during_drill_invalidates_restore_evidence(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    sync = FakeSync([(SNAPSHOT_THREE, datetime(2026, 7, 12, 5, 0, tzinfo=UTC))])
    calls: dict[Path, int] = {}

    def rotating_reader(path: Path) -> str:
        calls[path] = calls.get(path, 0) + 1
        if path == RECOVERY_CRYPTO_GENERATION_ID_FILE:
            return RECOVERY_GENERATION_ID if calls[path] == 1 else "recovery-v2"
        if path == BACKUP_PASSPHRASE_GENERATION_ID_FILE:
            return BACKUP_GENERATION_ID if calls[path] == 1 else "backup-v2"
        raise AssertionError("unexpected generation id path")

    service = make_service(
        tmp_path,
        sync,
        generation_reader=rotating_reader,
    )
    service.backup(config)

    with pytest.raises(ValueError, match="evidence changed"):
        service.drill(config)

    state = read_state(config)
    snapshot = state["snapshots"][SNAPSHOT_THREE]  # type: ignore[index]
    assert snapshot["available"] is False
    assert snapshot["restore_verified"] is False
    assert state["last_successful_restore"] is None


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
    service = make_service(
        tmp_path,
        sync,
        FakeRestore(within_restore_budget=False),
    )
    service.backup(config)

    with pytest.raises(RuntimeError, match="engineering budget"):
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
    assert status.evidence["snapshot_age_seconds"] is None
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


def test_status_uses_one_recent_restored_snapshot_even_if_newer_backup_is_pending(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    sync = FakeSync(
        [
            (SNAPSHOT_TWO, datetime(2026, 7, 12, 4, 0, tzinfo=UTC)),
            (SNAPSHOT_THREE, datetime(2026, 7, 12, 5, 0, tzinfo=UTC)),
        ]
    )
    service = make_service(tmp_path, sync)
    service.backup(config)
    service.drill(config)
    service.backup(config)

    status = service.status(config)

    assert status.healthy is True
    assert status.evidence["last_successful_backup"]["snapshot_id"] == SNAPSHOT_THREE
    assert status.evidence["last_successful_restore"]["snapshot_id"] == SNAPSHOT_TWO
    assert status.evidence["usable_snapshot_count"] == 1
    assert status.evidence["recovery_point_snapshot_id"] == SNAPSHOT_TWO


def test_status_rejects_restored_snapshot_older_than_rpo_limit(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    sync = FakeSync(
        [
            (SNAPSHOT_ONE, datetime(2026, 7, 10, 5, 0, tzinfo=UTC)),
            (SNAPSHOT_THREE, datetime(2026, 7, 12, 5, 0, tzinfo=UTC)),
        ]
    )
    service = make_service(tmp_path, sync)
    service.backup(config)
    service.drill(config)
    service.backup(config)

    status = service.status(config)

    assert status.healthy is False
    assert status.evidence["usable_snapshot_count"] == 0
    assert status.evidence["recovery_point_snapshot_id"] is None


def test_restore_runs_outside_lifecycle_lock(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    sync = FakeSync([(SNAPSHOT_THREE, datetime(2026, 7, 12, 5, 0, tzinfo=UTC))])
    observed: list[bool] = []
    holder: dict[str, LifecycleService] = {}

    class InspectingRestore(FakeRestore):
        def run(self, restore_config: RestoreConfig) -> RestoreResult:
            observed.append(holder["service"].status(config).healthy)
            return super().run(restore_config)

    service = make_service(tmp_path, sync, InspectingRestore())
    holder["service"] = service
    service.backup(config)

    service.drill(config)

    assert observed == [False]


def test_main_emits_only_validated_machine_readable_drill_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = valid_drill_cli_result()

    assert run_main_with_drill_result(tmp_path, monkeypatch, result) == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert payload == {
        "snapshot_id": SNAPSHOT_THREE,
        "finished_at": "2026-07-12T06:00:00+00:00",
        "restore_seconds": 25.5,
        "drill_budget_seconds": 43200.0,
        "restore_step_budget_seconds": 43199.5,
        "within_drill_budget": True,
        "data_gap_seconds": 3600,
        "report_sha256": "a" * 64,
    }
    for forbidden in (
        "CONFIG-PASSPHRASE-PATH-MARKER",
        "RECOVERY-SECRET-VALUE-MARKER",
        "BACKUP-PASSPHRASE-VALUE-MARKER",
        "passphrase",
        "secret",
        "password",
    ):
        assert forbidden.casefold() not in captured.out.casefold()


@pytest.mark.parametrize(
    ("field", "marker"),
    [
        pytest.param("password", "PASSWORD-VALUE-MARKER", id="password-field"),
        pytest.param(
            "passphrase", "PASSPHRASE-VALUE-MARKER", id="passphrase-field"
        ),
        pytest.param("secret", "SECRET-VALUE-MARKER", id="secret-field"),
    ],
)
def test_main_rejects_unapproved_sensitive_fields_without_disclosure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    field: str,
    marker: str,
) -> None:
    result = valid_drill_cli_result()
    result[field] = marker

    assert run_main_with_drill_result(tmp_path, monkeypatch, result) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert '"error_type": "ValueError"' in captured.err
    for forbidden in (field, marker, "CONFIG-PASSPHRASE-PATH-MARKER"):
        assert forbidden.casefold() not in captured.err.casefold()


@pytest.mark.parametrize(
    ("field", "marker"),
    [
        pytest.param("snapshot_id", "PASSWORD-VALUE-MARKER", id="snapshot-id"),
        pytest.param("finished_at", "PASSPHRASE-VALUE-MARKER", id="timestamp"),
        pytest.param("report_sha256", "SECRET-VALUE-MARKER", id="digest"),
    ],
)
def test_main_rejects_sensitive_values_smuggled_through_public_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    field: str,
    marker: str,
) -> None:
    result = valid_drill_cli_result()
    result[field] = marker

    assert run_main_with_drill_result(tmp_path, monkeypatch, result) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert '"error_type": "ValueError"' in captured.err
    assert marker not in captured.err
    assert "CONFIG-PASSPHRASE-PATH-MARKER" not in captured.err


def test_main_rejects_invalid_omitted_generation_id_without_disclosure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = valid_drill_cli_result()
    result["backup_passphrase_generation_id"] = "INVALID PASSPHRASE VALUE MARKER"

    assert run_main_with_drill_result(tmp_path, monkeypatch, result) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert '"error_type": "ValueError"' in captured.err
    assert "INVALID PASSPHRASE VALUE MARKER" not in captured.err


def test_main_preserves_stale_but_intact_backup_status_machine_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = make_config(tmp_path)
    sync = FakeSync([(SNAPSHOT_THREE, datetime(2026, 7, 12, 5, 0, tzinfo=UTC))])
    service = make_service(tmp_path, sync)
    service.backup(config)
    evidence = dict(service.backup_status(config).evidence)
    evidence.update(
        {
            "status": "stale",
            "backup_age_seconds": 7200,
            "snapshot_age_seconds": 7200,
            "max_backup_age_seconds": 3600,
            "failure_type": "EvidenceStale",
        }
    )

    class StubLifecycleService:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def backup_status(self, _config: LifecycleConfig) -> LifecycleStatus:
            return LifecycleStatus(evidence, False)

    monkeypatch.setattr(
        lifecycle_manager_module,
        "_runtime_config",
        lambda: (config, Path("/run/backup-secrets/PASSPHRASE-PATH-MARKER")),
    )
    monkeypatch.setattr(
        lifecycle_manager_module, "LifecycleService", StubLifecycleService
    )
    monkeypatch.setattr(sys, "argv", ["lifecycle_manager.py", "backup-status"])

    assert lifecycle_manager_module.main() == 2

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert payload["status"] == "stale"
    assert payload["integrity_verified"] is True
    assert payload["event"] == "lifecycle_alert"
    assert payload["error_type"] == "EvidenceStale"
    assert "passphrase" not in captured.out.casefold()


def test_status_cli_projection_rejects_nested_secret_and_omits_generation_ids(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    sync = FakeSync([(SNAPSHOT_THREE, datetime(2026, 7, 12, 5, 0, tzinfo=UTC))])
    service = make_service(tmp_path, sync)
    service.backup(config)
    service.drill(config)
    status = service.status(config)

    rendered = _serialize_lifecycle_cli_result(
        "status", status.evidence, healthy=status.healthy
    )

    payload = json.loads(rendered)
    assert payload["status"] == "healthy"
    assert payload["last_successful_backup"]["snapshot_id"] == SNAPSHOT_THREE
    assert payload["last_successful_restore"]["snapshot_id"] == SNAPSHOT_THREE
    assert "generation_id" not in rendered
    hostile = dict(status.evidence)
    backup = dict(hostile["last_successful_backup"])
    backup["secret"] = "NESTED-SECRET-VALUE-MARKER"
    hostile["last_successful_backup"] = backup
    with pytest.raises(ValueError, match="fields"):
        _serialize_lifecycle_cli_result("status", hostile, healthy=True)


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


def test_successful_backup_ledger_matches_current_after_remount(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    sync = FakeSync([(SNAPSHOT_THREE, datetime(2026, 7, 12, 5, 0, tzinfo=UTC))])
    service = make_service(tmp_path, sync)
    evidence = service.backup(config)
    remount = json.loads(
        config.output_root.joinpath("lifecycle-state.json").read_text(encoding="utf-8")
    )
    current = (config.output_root / "current").readlink().name
    assert current == evidence.snapshot_id
    assert remount["last_successful_backup"]["snapshot_id"] == evidence.snapshot_id
    assert remount["snapshots"][evidence.snapshot_id]["commit_phase"] == "ledger_committed"


def test_unregistered_snapshot_is_isolated_and_old_orphans_are_pruned(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    service = make_service(
        tmp_path,
        FakeSync([(SNAPSHOT_THREE, datetime(2026, 7, 12, 5, 0, tzinfo=UTC))]),
    )
    service.backup(config)
    extra = config.output_root / "snapshots" / SNAPSHOT_ONE
    extra.mkdir(mode=0o700)
    (extra / "junk").write_text("x", encoding="utf-8")
    stale = config.output_root / "orphans" / SNAPSHOT_TWO
    stale.mkdir(parents=True, mode=0o700)
    os.utime(stale, (0, 0))
    service.backup_status(config)
    assert not extra.exists()
    assert (config.output_root / "orphans" / SNAPSHOT_ONE).is_dir()
    assert not stale.exists()


def test_ledger_write_failure_adopts_published_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config(tmp_path)
    service = make_service(
        tmp_path,
        FakeSync([(SNAPSHOT_THREE, datetime(2026, 7, 12, 5, 0, tzinfo=UTC))]),
    )
    real_save = lifecycle_manager_module.LifecycleService._save_state
    calls = {"n": 0}

    def fail_first(
        self: lifecycle_manager_module.LifecycleService,
        cfg: LifecycleConfig,
        state: Mapping[str, object],
    ) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("state replace failed")
        real_save(self, cfg, state)

    monkeypatch.setattr(
        lifecycle_manager_module.LifecycleService, "_save_state", fail_first
    )
    with pytest.raises(OSError, match="state replace"):
        service.backup(config)
    state = read_state(config)
    current = config.output_root / "current"
    assert SNAPSHOT_THREE in state["snapshots"]
    assert current.is_symlink()
    assert current.readlink().name == SNAPSHOT_THREE


def test_state_replace_barrier_is_reached_on_ledger_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def record(name: str, path: Path | None = None) -> None:
        seen.append(name)

    monkeypatch.setattr(lifecycle_manager_module, "atomic_write_json", atomic_write_json)
    import failover_common as failover_module

    monkeypatch.setattr(failover_module, "durability_barrier", record)
    config = make_config(tmp_path)
    service = make_service(
        tmp_path,
        FakeSync([(SNAPSHOT_THREE, datetime(2026, 7, 12, 5, 0, tzinfo=UTC))]),
    )
    service.backup(config)
    assert "state_replace" in seen


class SimulatedLedgerPowerLoss(RuntimeError):
    """State Replace 屏障之后的生产等价掉电。"""


def test_state_replace_power_loss_does_not_report_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hits = {"n": 0}

    def inject(name: str, path: Path | None = None) -> None:
        if (
            name == "state_replace"
            and path is not None
            and path.name == "lifecycle-state.json"
        ):
            hits["n"] += 1
            if hits["n"] == 1:
                raise SimulatedLedgerPowerLoss(name)

    monkeypatch.setattr(lifecycle_manager_module, "atomic_write_json", atomic_write_json)
    import failover_common as failover_module

    monkeypatch.setattr(failover_module, "durability_barrier", inject)
    config = make_config(tmp_path)
    service = make_service(
        tmp_path,
        FakeSync([(SNAPSHOT_THREE, datetime(2026, 7, 12, 5, 0, tzinfo=UTC))]),
    )
    with pytest.raises(SimulatedLedgerPowerLoss, match="state_replace"):
        service.backup(config)
    current = config.output_root / "current"
    assert current.is_symlink()
    assert current.readlink().name == SNAPSHOT_THREE
    remount = read_state(config)
    assert SNAPSHOT_THREE in remount["snapshots"]


def test_stale_incoming_is_pruned_on_status(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    service = make_service(
        tmp_path,
        FakeSync([(SNAPSHOT_THREE, datetime(2026, 7, 12, 5, 0, tzinfo=UTC))]),
    )
    service.backup(config)
    incoming = config.output_root / ".incoming" / SNAPSHOT_ONE
    incoming.mkdir(parents=True, mode=0o700)
    (incoming / "partial.dump.enc").write_bytes(b"x")
    os.utime(incoming, (0, 0))
    service.backup_status(config)
    assert not incoming.exists()
