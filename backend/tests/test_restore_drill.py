from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "deploy" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from failover_common import CommandFailure, sha256_file  # noqa: E402
from restore_drill import RestoreConfig, RestoreService  # noqa: E402


class FakeRunner:
    def __init__(self, *, pipeline_error: bool = False) -> None:
        self.pipeline_error = pipeline_error
        self.calls: list[list[str]] = []
        self.pipeline_calls: list[tuple[list[str], list[str], Path]] = []

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        input_bytes: bytes | None = None,
    ) -> bytes:
        argv = list(command)
        self.calls.append(argv)
        command_text = " ".join(argv)
        if "SELECT version_num FROM alembic_version" in command_text:
            return b"0008_audit_payload_guard\n"
        if "sms_batch=" in command_text:
            return b"sms_batch=3\naudit_log=4\nraw_vendor_log=5\n"
        if "to_regclass('public.sms_batch')" in command_text:
            return b"true|true|true|6\n"
        if "has_table_privilege" in command_text:
            return b"7|true\ntrue\ntrue|true\n"
        if "SET ROLE sms_accept" in command_text:
            return b"ok\n"
        return b""

    def pipeline_from_file(
        self,
        producer: Sequence[str],
        consumer: Sequence[str],
        input_path: Path,
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> bytes:
        self.pipeline_calls.append((list(producer), list(consumer), input_path))
        if self.pipeline_error:
            raise CommandFailure("pg_restore", 1, "restore failed")
        return b""


def make_config(
    tmp_path: Path,
    *,
    keep: bool = False,
    drill_environment: bool = False,
    corrupt_hash: bool = False,
) -> RestoreConfig:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "deploy").mkdir()
    compose = root / "deploy/docker-compose.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    backup = tmp_path / "sms_snapshot.dump.enc"
    backup.write_bytes(b"encrypted-backup")
    passphrase = tmp_path / "backup-passphrase"
    passphrase.write_text("not-a-production-secret", encoding="utf-8")
    passphrase.chmod(0o600)
    manifest = tmp_path / "manifest.json"
    digest = "0" * 64 if corrupt_hash else sha256_file(backup)
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "snapshot_id": "20260712T050000Z_aaaaaaaaaaaa",
                "git_commit": "a" * 40,
                "alembic_version": "0008_audit_payload_guard",
                "secrets_included": False,
                "files": {
                    "database": {
                        "name": backup.name,
                        "sha256": digest,
                        "size": backup.stat().st_size,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    manifest.chmod(0o600)
    return RestoreConfig(
        repository_root=root,
        compose_file=compose,
        backup_file=backup,
        manifest_file=manifest,
        passphrase_file=passphrase,
        report_file=tmp_path / "drill-report.json",
        keep=keep,
        drill_environment=drill_environment,
        max_restore_seconds=1800,
    )


def fixed_clock() -> datetime:
    return datetime(2026, 7, 12, 5, 0, tzinfo=UTC)


def test_hash_mismatch_fails_before_database_creation(tmp_path: Path) -> None:
    runner = FakeRunner()
    with pytest.raises(ValueError, match="SHA-256"):
        RestoreService(runner, clock=fixed_clock, suffix=lambda: "ab12").run(
            make_config(tmp_path, corrupt_hash=True)
        )
    assert not any("createdb" in call for call in runner.calls)


def test_restore_uses_safe_drill_database_checks_permissions_and_cleans_up(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    config = make_config(tmp_path)
    result = RestoreService(
        runner,
        clock=fixed_clock,
        timer=iter((100.0, 125.5)).__next__,
        suffix=lambda: "ab12",
    ).run(config)

    assert result.database == "sms_drill_20260712050000_ab12"
    assert result.restore_seconds == 25.5 and result.within_rto is True
    producer, consumer, source = runner.pipeline_calls[0]
    assert producer[:4] == ["openssl", "enc", "-d", "-aes-256-cbc"]
    assert "pg_restore" in consumer and result.database in consumer
    assert source == config.backup_file
    commands = [" ".join(call) for call in runner.calls]
    assert any("createdb" in call and result.database in call for call in commands)
    assert any("run --rm -e" in call and f"DB_NAME={result.database}" in call for call in commands)
    assert any("to_regclass('public.sms_batch')" in call for call in commands)
    assert any("has_table_privilege" in call for call in commands)
    assert any("SET ROLE sms_accept" in call for call in commands)
    assert commands[-1].find("dropdb") >= 0 and result.database in commands[-1]
    report = json.loads(config.report_file.read_text(encoding="utf-8"))
    assert report["checks"]["audit_privileges"] == "true"
    assert report["table_counts"] == {
        "sms_batch": 3,
        "audit_log": 4,
        "raw_vendor_log": 5,
    }
    assert "phone" not in json.dumps(report).casefold()


def test_pipeline_failure_still_drops_created_drill_database(tmp_path: Path) -> None:
    runner = FakeRunner(pipeline_error=True)
    with pytest.raises(CommandFailure):
        RestoreService(runner, clock=fixed_clock, suffix=lambda: "ab12").run(make_config(tmp_path))
    assert "dropdb" in " ".join(runner.calls[-1])
    assert "sms_drill_" in " ".join(runner.calls[-1])


def test_keep_requires_explicit_drill_environment(tmp_path: Path) -> None:
    runner = FakeRunner()
    with pytest.raises(ValueError, match="DRILL_ENV"):
        RestoreService(runner, clock=fixed_clock, suffix=lambda: "ab12").run(
            make_config(tmp_path, keep=True, drill_environment=False)
        )
    assert runner.calls == []


def test_restore_over_1800_seconds_is_reported_as_rto_failure(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    result = RestoreService(
        FakeRunner(),
        clock=fixed_clock,
        timer=iter((0.0, 1800.01)).__next__,
        suffix=lambda: "ab12",
    ).run(config)
    assert result.within_rto is False
    assert json.loads(config.report_file.read_text())["within_rto"] is False
