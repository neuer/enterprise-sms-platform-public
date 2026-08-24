from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "deploy" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import failover_common as failover_module  # noqa: E402
import restore_drill as restore_module  # noqa: E402
from failover_common import (  # noqa: E402
    BACKUP_PASSPHRASE_GENERATION_ID_FILE,
    RECOVERY_CRYPTO_GENERATION_ID_FILE,
    CommandFailure,
    CommandRunner,
    CommandTimeout,
    DeadlineExceeded,
    sha256_file,
)
from restore_drill import (  # noqa: E402
    CLEANUP_TIMEOUT_SECONDS,
    PREPRODUCTION_MARKER_CONTENT,
    RestoreCleanupFailure,
    RestoreCommandFailure,
    RestoreConfig,
    RestoreService,
    _validate_preproduction_marker,
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
        pipeline_error: bool = False,
        stale_count: int = 0,
        cleanup_error: bool = False,
        createdb_error: bool = False,
        timer: ManualTimer | None = None,
        expire_on: str | None = None,
        expire_at: float = 0.0,
        sms_batch_count: int = 3,
        sms_message_count: int = 0,
        raw_vendor_log_count: int = 5,
        crypto_probe_status: str | None = None,
    ) -> None:
        self.pipeline_error = pipeline_error
        self.stale_count = stale_count
        self.cleanup_error = cleanup_error
        self.createdb_error = createdb_error
        self.timer = timer
        self.expire_on = expire_on
        self.expire_at = expire_at
        self.sms_batch_count = sms_batch_count
        self.sms_message_count = sms_message_count
        self.raw_vendor_log_count = raw_vendor_log_count
        self.crypto_probe_status = crypto_probe_status
        self.crypto_probe_calls = 0
        self.calls: list[list[str]] = []
        self.pipeline_calls: list[tuple[list[str], list[str], Path]] = []
        self.timeouts: list[tuple[str, float | None]] = []

    def _maybe_expire(self, command_text: str) -> None:
        if (
            self.timer is not None
            and self.expire_on is not None
            and self.expire_on in command_text
        ):
            self.timer.value = self.expire_at

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        input_bytes: bytes | None = None,
        timeout: float | None = None,
    ) -> bytes:
        argv = list(command)
        self.calls.append(argv)
        command_text = " ".join(argv)
        self.timeouts.append((command_text, timeout))
        if "dropdb" in argv and self.cleanup_error:
            raise CommandTimeout("dropdb")
        if "createdb" in argv and self.createdb_error:
            raise CommandTimeout("createdb")
        self._maybe_expire(command_text)
        if "SELECT count(*) FROM pg_database" in command_text:
            return f"{self.stale_count}\n".encode()
        if "SELECT version_num FROM alembic_version" in command_text:
            return b"0008_audit_payload_guard\n"
        if "scripts_support.recovery_crypto_probe" in command_text:
            self.crypto_probe_calls += 1
            coverage = {
                label: {"rows": 0, "key_versions_verified": 0}
                for label in restore_module.CRYPTO_PROBE_COVERAGE_FIELDS
            }
            coverage["sms_batch.display_content_enc"] = {
                "rows": self.sms_batch_count,
                "key_versions_verified": 1 if self.sms_batch_count else 0,
            }
            coverage["sms_batch.send_content_enc"] = {
                "rows": self.sms_batch_count,
                "key_versions_verified": 1 if self.sms_batch_count else 0,
            }
            coverage["sms_message.phone_enc"] = {
                "rows": self.sms_message_count,
                "key_versions_verified": 1 if self.sms_message_count else 0,
            }
            coverage["raw_vendor_log.payload_enc"] = {
                "rows": self.raw_vendor_log_count,
                "key_versions_verified": 1 if self.raw_vendor_log_count else 0,
            }
            encrypted_rows = sum(item["rows"] for item in coverage.values())
            verified = sum(
                item["key_versions_verified"] for item in coverage.values()
            )
            status = self.crypto_probe_status or (
                "performed" if encrypted_rows else "not_applicable_empty"
            )
            return json.dumps(
                {
                    "schema_version": 2,
                    "status": status,
                    "counts": {
                        "audit_context_keys": 4,
                        "encrypted_columns": len(
                            restore_module.CRYPTO_PROBE_COVERAGE_FIELDS
                        ),
                        "encrypted_rows": encrypted_rows,
                        "ciphertext_samples_verified": verified,
                        "key_version_columns": 8,
                        "referenced_key_versions": 1 if encrypted_rows else 0,
                        "sms_message_rows": self.sms_message_count,
                    },
                    "coverage": coverage,
                }
            ).encode()
        if "sms_batch=" in command_text:
            return (
                f"sms_batch={self.sms_batch_count}\n"
                f"sms_message={self.sms_message_count}\n"
                "audit_log=4\n"
                f"raw_vendor_log={self.raw_vendor_log_count}\n"
            ).encode()
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
        timeout: float | None = None,
    ) -> bytes:
        self.pipeline_calls.append((list(producer), list(consumer), input_path))
        command_text = " ".join([*producer, *consumer])
        self.timeouts.append((command_text, timeout))
        self._maybe_expire(command_text)
        if self.pipeline_error:
            raise CommandFailure(
                "pg_restore",
                1,
                "COPY failed 13800138000 secret=leaked " + "a" * 64,
            )
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
    snapshot_id = "20260712T050000Z_aaaaaaaaaaaa"
    snapshot = tmp_path / snapshot_id
    snapshot.mkdir(mode=0o700)
    backup = snapshot / "sms_snapshot.dump.enc"
    backup.write_bytes(b"encrypted-backup")
    backup.chmod(0o600)
    repository_archive = snapshot / "repository_snapshot.tar.gz"
    repository_archive.write_bytes(b"repository")
    repository_archive.chmod(0o600)
    environment = snapshot / "production.env"
    environment.write_bytes(b"DEBUG=0\n")
    environment.chmod(0o600)
    passphrase = tmp_path / "backup-passphrase"
    passphrase.write_text("not-a-production-secret", encoding="utf-8")
    passphrase.chmod(0o600)
    manifest = snapshot / "manifest.json"
    digest = "0" * 64 if corrupt_hash else sha256_file(backup)
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "snapshot_id": snapshot_id,
                "created_at": "2026-07-12T05:00:00+00:00",
                "git_commit": "a" * 40,
                "alembic_version": "0008_audit_payload_guard",
                "database": "sms",
                "secrets_included": False,
                "recovery_crypto_generation_id": "recovery-v1",
                "backup_passphrase_generation_id": "backup-passphrase-v1",
                "files": {
                    "database": {
                        "name": backup.name,
                        "sha256": digest,
                        "size": backup.stat().st_size,
                    },
                    "repository_archive": {
                        "name": repository_archive.name,
                        "sha256": sha256_file(repository_archive),
                        "size": repository_archive.stat().st_size,
                    },
                    "environment": {
                        "name": environment.name,
                        "sha256": sha256_file(environment),
                        "size": environment.stat().st_size,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    manifest.chmod(0o600)
    checksums = snapshot / "SHA256SUMS"
    checksums.write_text(
        "\n".join(
            (
                f"{digest}  {backup.name}",
                f"{sha256_file(repository_archive)}  {repository_archive.name}",
                f"{sha256_file(environment)}  {environment.name}",
                f"{sha256_file(manifest)}  {manifest.name}",
            )
        )
        + "\n",
        encoding="ascii",
    )
    checksums.chmod(0o600)
    return RestoreConfig(
        repository_root=root,
        compose_file=compose,
        backup_file=backup,
        manifest_file=manifest,
        passphrase_file=passphrase,
        report_file=tmp_path / "drill-report.json",
        keep=keep,
        drill_environment=drill_environment,
        max_restore_seconds=43200,
    )


def fixed_clock() -> datetime:
    return datetime(2026, 7, 12, 5, 0, tzinfo=UTC)


def generation_reader(path: Path) -> str:
    if path == RECOVERY_CRYPTO_GENERATION_ID_FILE:
        return "recovery-v1"
    if path == BACKUP_PASSPHRASE_GENERATION_ID_FILE:
        return "backup-passphrase-v1"
    raise AssertionError("unexpected generation id path")


def make_restore_service(
    runner: FakeRunner,
    **kwargs: object,
) -> RestoreService:
    kwargs.setdefault("clock", fixed_clock)
    kwargs.setdefault("suffix", lambda: "ab12")
    kwargs.setdefault("generation_reader", generation_reader)
    return RestoreService(runner, **kwargs)  # type: ignore[arg-type]


def test_hash_mismatch_fails_before_database_creation(tmp_path: Path) -> None:
    runner = FakeRunner()
    with pytest.raises(ValueError, match="integrity"):
        make_restore_service(runner).run(
            make_config(tmp_path, corrupt_hash=True)
        )
    assert not any("createdb" in call for call in runner.calls)


@pytest.mark.parametrize(
    "mutation",
    ("extra_file", "missing_payload", "tampered_payload", "duplicate_checksum"),
)
def test_snapshot_bundle_requires_exact_five_file_integrity_closed_set(
    tmp_path: Path,
    mutation: str,
) -> None:
    config = make_config(tmp_path)
    snapshot = config.manifest_file.parent
    manifest = json.loads(config.manifest_file.read_text(encoding="utf-8"))
    repository = snapshot / manifest["files"]["repository_archive"]["name"]
    if mutation == "extra_file":
        extra = snapshot / "unexpected.txt"
        extra.write_text("unexpected", encoding="utf-8")
        extra.chmod(0o600)
    elif mutation == "missing_payload":
        repository.unlink()
    elif mutation == "tampered_payload":
        repository.write_bytes(b"tampered")
    else:
        checksums = snapshot / "SHA256SUMS"
        first = checksums.read_text(encoding="ascii").splitlines()[0]
        checksums.write_text(
            checksums.read_text(encoding="ascii") + first + "\n",
            encoding="ascii",
        )

    runner = FakeRunner()
    with pytest.raises((FileNotFoundError, ValueError)):
        make_restore_service(runner).run(config)
    assert not any("createdb" in call for call in runner.calls)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("database", "other"),
        ("git_commit", "a" * 39),
        ("created_at", "2027-01-01T00:00:00+00:00"),
    ),
)
def test_snapshot_manifest_identity_fields_are_strict(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    config = make_config(tmp_path)
    manifest = json.loads(config.manifest_file.read_text(encoding="utf-8"))
    manifest[field] = value
    config.manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
    config.manifest_file.chmod(0o600)
    checksums = config.manifest_file.parent / "SHA256SUMS"
    lines = checksums.read_text(encoding="ascii").splitlines()
    lines = [
        (
            f"{sha256_file(config.manifest_file)}  manifest.json"
            if line.endswith("  manifest.json")
            else line
        )
        for line in lines
    ]
    checksums.write_text("\n".join(lines) + "\n", encoding="ascii")

    runner = FakeRunner()
    with pytest.raises(ValueError):
        make_restore_service(runner).run(config)
    assert not any("createdb" in call for call in runner.calls)


def test_restore_uses_safe_drill_database_checks_permissions_and_cleans_up(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    config = make_config(tmp_path)
    result = make_restore_service(runner).run(config)

    assert result.database == "sms_drill_20260712050000_ab12"
    assert 0 <= result.restore_seconds < config.max_restore_seconds
    assert result.within_restore_budget is True
    producer, consumer, source = runner.pipeline_calls[0]
    assert producer[:4] == ["openssl", "enc", "-d", "-aes-256-cbc"]
    assert "pg_restore" in consumer and result.database in consumer
    assert source == config.backup_file
    commands = [" ".join(call) for call in runner.calls]
    assert any("createdb" in call and result.database in call for call in commands)
    assert any("run --rm -e" in call and f"DB_NAME={result.database}" in call for call in commands)
    crypto_probe_indices = [
        index
        for index, call in enumerate(commands)
        if "scripts_support.recovery_crypto_probe" in call
    ]
    assert len(crypto_probe_indices) == 2
    assert all(
        "run --rm --no-deps -e" in commands[index]
        for index in crypto_probe_indices
    )
    migrate_index = next(
        index
        for index, call in enumerate(commands)
        if "run --rm -e" in call and call.endswith(" migrate")
    )
    assert crypto_probe_indices[0] < migrate_index < crypto_probe_indices[1]
    assert any("to_regclass('public.sms_batch')" in call for call in commands)
    assert any("has_table_privilege" in call for call in commands)
    assert any("SET ROLE sms_accept" in call for call in commands)
    assert commands[-1].find("dropdb") >= 0 and result.database in commands[-1]
    assert "--force" in commands[-1]
    assert runner.timeouts[-1][1] == CLEANUP_TIMEOUT_SECONDS
    assert all(
        timeout is not None and 0 < timeout <= config.max_restore_seconds
        for command, timeout in runner.timeouts
        if "dropdb" not in command
    )
    report = json.loads(config.report_file.read_text(encoding="utf-8"))
    assert report["schema_version"] == 2
    assert report["metric_scope"] == "database_restore"
    assert report["business_rto_evidence"] is False
    assert report["restore_budget_seconds"] == config.max_restore_seconds
    assert report["within_restore_budget"] is True
    assert report["recovery_crypto_generation_id"] == "recovery-v1"
    assert report["backup_passphrase_generation_id"] == "backup-passphrase-v1"
    assert report["checks"]["audit_privileges"] == "true"
    assert report["checks"]["crypto_generation_binding"] == (
        "matched_host_generation_ids"
    )
    assert report["checks"]["historical_ciphertext_validation"] == (
        "performed"
    )
    assert report["checks"]["pre_migration_crypto_validation"] == (
        "performed"
    )
    assert report["checks"]["post_migration_crypto_validation"] == (
        "performed"
    )
    assert set(report["crypto_probe_receipts"]) == {
        "pre_migration",
        "post_migration",
    }
    assert all(
        receipt["counts"]["encrypted_rows"] == 11
        for receipt in report["crypto_probe_receipts"].values()
    )
    assert report["table_counts"] == {
        "sms_batch": 3,
        "sms_message": 0,
        "audit_log": 4,
        "raw_vendor_log": 5,
    }
    serialized = json.dumps(report).casefold()
    assert "13800138000" not in serialized
    assert "secret=" not in serialized


def test_all_ciphertext_tables_empty_is_explicitly_not_applicable(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(sms_batch_count=0, raw_vendor_log_count=0)
    config = make_config(tmp_path)

    make_restore_service(runner).run(config)

    report = json.loads(config.report_file.read_text(encoding="utf-8"))
    assert report["checks"]["historical_ciphertext_validation"] == (
        "not_applicable_empty"
    )
    assert all(
        receipt["counts"]["encrypted_rows"] == 0
        for receipt in report["crypto_probe_receipts"].values()
    )


def test_nonempty_restore_performs_historical_ciphertext_probe(tmp_path: Path) -> None:
    runner = FakeRunner(sms_message_count=12)
    config = make_config(tmp_path)

    make_restore_service(runner).run(config)

    report = json.loads(config.report_file.read_text(encoding="utf-8"))
    assert report["table_counts"]["sms_message"] == 12
    assert report["checks"]["historical_ciphertext_validation"] == "performed"
    assert report["checks"]["pre_migration_crypto_validation"] == "performed"
    assert report["checks"]["post_migration_crypto_validation"] == "performed"


def test_restore_rejects_generation_id_mismatch_before_database_creation(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()

    def mismatched_reader(path: Path) -> str:
        if path == RECOVERY_CRYPTO_GENERATION_ID_FILE:
            return "rotated-recovery-v2"
        return generation_reader(path)

    with pytest.raises(ValueError, match="generation binding"):
        make_restore_service(runner, generation_reader=mismatched_reader).run(
            make_config(tmp_path)
        )

    assert runner.calls == []


def test_invalid_crypto_probe_result_fails_and_cleans_database(tmp_path: Path) -> None:
    runner = FakeRunner(crypto_probe_status="failed")

    with pytest.raises(ValueError, match="crypto probe"):
        make_restore_service(runner).run(make_config(tmp_path))

    assert runner.crypto_probe_calls == 1
    assert not any(
        "run --rm -e" in " ".join(call)
        and "scripts_support.recovery_crypto_probe" not in " ".join(call)
        for call in runner.calls
    )
    assert "dropdb" in " ".join(runner.calls[-1])


def test_pipeline_failure_still_drops_created_drill_database(tmp_path: Path) -> None:
    runner = FakeRunner(pipeline_error=True)
    with pytest.raises(RestoreCommandFailure) as caught:
        make_restore_service(runner).run(make_config(tmp_path))
    assert "13800138000" not in str(caught.value)
    assert "leaked" not in str(caught.value)
    assert "a" * 64 not in str(caught.value)
    assert "dropdb" in " ".join(runner.calls[-1])
    assert "sms_drill_" in " ".join(runner.calls[-1])


def test_direct_cli_failure_is_fixed_json_without_traceback_or_stderr_data(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["restore_drill.py", "backup.enc", "manifest.json"],
    )

    def fail_config(_args: object) -> RestoreConfig:
        raise RuntimeError(
            "COPY 13800138000 secret=leaked " + "b" * 64
        )

    monkeypatch.setattr(restore_module, "_config_from_args", fail_config)

    assert restore_module.main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err) == {
        "status": "failed",
        "error_type": "restore_failed",
    }
    assert "13800138000" not in output.err
    assert "leaked" not in output.err
    assert "traceback" not in output.err.casefold()


def test_createdb_timeout_still_attempts_bounded_force_cleanup(tmp_path: Path) -> None:
    runner = FakeRunner(createdb_error=True)
    with pytest.raises(CommandTimeout):
        make_restore_service(runner).run(
            make_config(tmp_path)
        )
    cleanup = " ".join(runner.calls[-1])
    assert "dropdb" in cleanup and "--force" in cleanup and "--if-exists" in cleanup
    assert runner.timeouts[-1][1] == CLEANUP_TIMEOUT_SECONDS


def test_keep_requires_explicit_drill_environment(tmp_path: Path) -> None:
    runner = FakeRunner()
    with pytest.raises(ValueError, match="DRILL_ENV"):
        make_restore_service(runner).run(
            make_config(tmp_path, keep=True, drill_environment=False)
        )
    assert runner.calls == []


def test_direct_restore_marker_requires_exact_regular_0600_file(tmp_path: Path) -> None:
    marker = tmp_path / "preproduction-restore-host"
    marker.write_bytes(PREPRODUCTION_MARKER_CONTENT)
    marker.chmod(0o600)

    marker_owner = {"expected_uid": os.getuid(), "expected_gid": os.getgid()}
    _validate_preproduction_marker(marker, **marker_owner)

    marker.chmod(0o644)
    with pytest.raises(ValueError, match="host contract"):
        _validate_preproduction_marker(marker, **marker_owner)
    marker.chmod(0o600)
    marker.write_text("wrong-marker\n", encoding="ascii")
    with pytest.raises(ValueError, match="host contract"):
        _validate_preproduction_marker(marker, **marker_owner)
    marker.unlink()
    marker.symlink_to(tmp_path / "target")
    with pytest.raises(ValueError, match="host contract"):
        _validate_preproduction_marker(marker, **marker_owner)


def test_restore_deadline_aborts_and_uses_independent_cleanup_budget(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    timer = ManualTimer()
    runner = FakeRunner(
        timer=timer,
        expire_on="pg_restore",
        expire_at=config.max_restore_seconds + 0.01,
    )
    with pytest.raises(DeadlineExceeded, match="deadline"):
        make_restore_service(
            runner,
            timer=timer,
        ).run(config)
    assert not config.report_file.exists()
    assert "dropdb" in " ".join(runner.calls[-1])
    assert runner.timeouts[-1][1] == CLEANUP_TIMEOUT_SECONDS


def test_stale_drill_database_blocks_before_createdb(tmp_path: Path) -> None:
    runner = FakeRunner(stale_count=1)
    with pytest.raises(ValueError, match="stale drill database"):
        make_restore_service(runner).run(
            make_config(tmp_path)
        )
    commands = [" ".join(call) for call in runner.calls]
    assert len(commands) == 1
    assert "pg_database" in commands[0]
    assert not any("createdb" in command for command in commands)


def test_deadline_covers_snapshot_hash_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timer = ManualTimer()
    config = make_config(tmp_path)
    original_sha256 = failover_module.sha256_file

    def slow_sha256(
        path: Path,
        *,
        deadline: float | None = None,
        timer: object = None,
    ) -> str:
        del timer
        assert deadline == config.max_restore_seconds
        timer_clock.value = config.max_restore_seconds + 0.01
        return original_sha256(path)

    timer_clock = timer
    monkeypatch.setattr(failover_module, "sha256_file", slow_sha256)
    runner = FakeRunner()
    with pytest.raises(DeadlineExceeded):
        make_restore_service(
            runner,
            timer=timer,
        ).run(config)
    assert runner.calls == []


def test_deadline_covers_final_report_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timer = ManualTimer()
    config = make_config(tmp_path)
    original_write = restore_module.atomic_write_json

    def slow_write(path: Path, value: Mapping[str, object]) -> None:
        original_write(path, value)
        timer.value = config.max_restore_seconds + 0.01

    monkeypatch.setattr(restore_module, "atomic_write_json", slow_write)
    with pytest.raises(DeadlineExceeded):
        make_restore_service(
            FakeRunner(),
            timer=timer,
        ).run(config)
    assert not config.report_file.exists()


def test_cleanup_failure_is_generic_and_fail_closed(tmp_path: Path) -> None:
    runner = FakeRunner(cleanup_error=True)
    with pytest.raises(RestoreCleanupFailure) as captured:
        make_restore_service(runner).run(
            make_config(tmp_path)
        )
    assert "sms_drill_" not in str(captured.value)
    assert runner.timeouts[-1][1] == CLEANUP_TIMEOUT_SECONDS


def test_command_runner_timeout_does_not_expose_argv_and_reaps_process() -> None:
    marker = "password=do-not-expose"
    with pytest.raises(CommandTimeout) as captured:
        CommandRunner().run(
            [
                sys.executable,
                "-c",
                "import signal,time; "
                "signal.signal(signal.SIGTERM, lambda *_: time.sleep(5)); "
                "time.sleep(5)",
                marker,
            ],
            timeout=0.05,
        )
    assert marker not in str(captured.value)


def test_command_runner_pipeline_timeout_is_bounded_and_generic(tmp_path: Path) -> None:
    source = tmp_path / "encrypted.dump"
    source.write_bytes(b"payload")
    marker = "token=do-not-expose"
    with pytest.raises(CommandTimeout) as captured:
        CommandRunner().pipeline_from_file(
            [
                sys.executable,
                "-c",
                "import signal,time; "
                "signal.signal(signal.SIGTERM, lambda *_: time.sleep(5)); "
                "time.sleep(5)",
                marker,
            ],
            [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
            source,
            timeout=0.05,
        )
    assert marker not in str(captured.value)
