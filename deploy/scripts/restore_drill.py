#!/usr/bin/env python3
"""把冷备快照恢复到隔离数据库并生成无 PII 的工程预算报告。"""

from __future__ import annotations

import argparse
import json
import math
import os
import secrets
import stat
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from failover_common import (
    BACKUP_PASSPHRASE_GENERATION_ID_FILE,
    RECOVERY_CRYPTO_GENERATION_ID_FILE,
    CommandFailure,
    CommandRunner,
    DeadlineExceeded,
    atomic_write_json,
    read_root_generation_id_file,
    validate_drill_database,
    validate_generation_id,
    validate_passphrase_file,
    validate_snapshot_bundle,
)

CLEANUP_TIMEOUT_SECONDS = 60.0
PREPRODUCTION_MARKER = Path("/etc/sms-platform/preproduction-restore-host")
PREPRODUCTION_MARKER_CONTENT = b"preproduction-restore-host-v1\n"
CRYPTO_PROBE_FIELDS = frozenset(
    {"schema_version", "status", "counts", "coverage"}
)
CRYPTO_PROBE_COUNT_FIELDS = frozenset(
    {
        "audit_context_keys",
        "encrypted_columns",
        "encrypted_rows",
        "ciphertext_samples_verified",
        "key_version_columns",
        "referenced_key_versions",
        "sms_message_rows",
    }
)
CRYPTO_PROBE_COVERAGE_FIELDS = frozenset(
    {
        "app.callback_secret_enc",
        "blacklist.phone_enc",
        "callback_task.callback_secret_enc",
        "import_phone.phone_enc",
        "raw_vendor_log.payload_enc",
        "reply_event.content_enc",
        "reply_event.phone_enc",
        "report_event.phone_enc",
        "sensitive_metadata_archive.value_enc",
        "sms_batch.display_content_enc",
        "sms_batch.send_content_enc",
        "sms_message.phone_enc",
        "sms_reply.phone_enc",
        "sms_template.content_enc",
        "sms_template.name_enc",
        "unmatched_report.phone_enc",
        "vendor_test_recipient.phone_enc",
    }
)
CRYPTO_PROBE_COVERAGE_VALUE_FIELDS = frozenset(
    {"rows", "key_versions_verified"}
)


class Runner(Protocol):
    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        input_bytes: bytes | None = None,
        timeout: float | None = None,
    ) -> bytes: ...

    def pipeline_from_file(
        self,
        producer: Sequence[str],
        consumer: Sequence[str],
        input_path: Path,
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> bytes: ...


@dataclass(frozen=True, slots=True)
class RestoreConfig:
    repository_root: Path
    compose_file: Path
    backup_file: Path
    manifest_file: Path
    passphrase_file: Path
    report_file: Path
    keep: bool
    drill_environment: bool
    recovery_crypto_generation_id_file: Path = RECOVERY_CRYPTO_GENERATION_ID_FILE
    backup_passphrase_generation_id_file: Path = BACKUP_PASSPHRASE_GENERATION_ID_FILE
    max_restore_seconds: float = 43200


@dataclass(frozen=True, slots=True)
class RestoreResult:
    database: str
    restore_seconds: float
    within_restore_budget: bool


class RestoreCleanupFailure(RuntimeError):
    """演练库未能在独立清理预算内确认删除。"""

    def __init__(self) -> None:
        super().__init__("drill database cleanup failed")


class RestoreCommandFailure(RuntimeError):
    """高敏恢复命令失败；绝不携带命令输出。"""

    def __init__(self) -> None:
        super().__init__("restore command failed")


def utc_now() -> datetime:
    return datetime.now(UTC)


def _validate_preproduction_marker(
    path: Path = PREPRODUCTION_MARKER,
    *,
    expected_uid: int = 0,
    expected_gid: int = 0,
) -> None:
    """显式 CLI 恢复只允许在当前 root 管理的隔离恢复主机执行。"""

    if path.is_symlink():
        raise ValueError("preproduction restore marker violates host contract")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise ValueError("preproduction restore marker is unavailable") from error
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or (metadata.st_uid, metadata.st_gid) != (expected_uid, expected_gid)
        ):
            raise ValueError("preproduction restore marker violates host contract")
        value = os.read(fd, len(PREPRODUCTION_MARKER_CONTENT) + 1)
    finally:
        os.close(fd)
    if value != PREPRODUCTION_MARKER_CONTENT:
        raise ValueError("preproduction restore marker violates host contract")


def _manifest(
    config: RestoreConfig,
    *,
    now: datetime,
    deadline: float,
    timer: Callable[[], float],
) -> dict[str, Any]:
    if config.manifest_file.is_symlink() or config.backup_file.is_symlink():
        raise ValueError("snapshot input path must not be a symlink")
    bundle = validate_snapshot_bundle(
        config.manifest_file.parent,
        now=now,
        deadline=deadline,
        timer=timer,
    )
    if (
        config.manifest_file.resolve(strict=True)
        != config.manifest_file.parent.joinpath("manifest.json").resolve(strict=True)
        or config.backup_file.resolve(strict=True)
        != bundle.files["database"].resolve(strict=True)
    ):
        raise ValueError("restore input does not match snapshot bundle")
    return bundle.manifest


def _decode_single(output: bytes, label: str) -> str:
    value = output.decode("utf-8", errors="strict").strip()
    if not value or "\n" in value:
        raise ValueError(f"invalid {label} check result")
    return value


def _parse_counts(output: bytes) -> dict[str, int]:
    allowed = {"sms_batch", "sms_message", "audit_log", "raw_vendor_log"}
    result: dict[str, int] = {}
    for line in output.decode("utf-8", errors="strict").splitlines():
        key, separator, raw_count = line.partition("=")
        if not separator or key not in allowed or not raw_count.isdecimal():
            raise ValueError("invalid table-count check result")
        result[key] = int(raw_count)
    if result.keys() != allowed:
        raise ValueError("incomplete table-count check result")
    return result


def _parse_crypto_probe(output: bytes) -> dict[str, Any]:
    """只接受探针固定 JSON；错误不得回显容器输出。"""

    if len(output) > 8192:
        raise ValueError("invalid recovery crypto probe result")
    try:
        value = json.loads(output.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid recovery crypto probe result") from error
    if (
        not isinstance(value, dict)
        or set(value) != CRYPTO_PROBE_FIELDS
        or value.get("schema_version") != 2
        or value.get("status") not in {"performed", "not_applicable_empty"}
    ):
        raise ValueError("invalid recovery crypto probe result")
    raw_counts = value.get("counts")
    if not isinstance(raw_counts, dict) or set(raw_counts) != CRYPTO_PROBE_COUNT_FIELDS:
        raise ValueError("invalid recovery crypto probe result")
    if any(
        isinstance(item, bool) or not isinstance(item, int) or item < 0
        for item in raw_counts.values()
    ):
        raise ValueError("invalid recovery crypto probe result")
    counts = {key: int(raw_counts[key]) for key in CRYPTO_PROBE_COUNT_FIELDS}
    status = str(value["status"])
    raw_coverage = value.get("coverage")
    if (
        not isinstance(raw_coverage, dict)
        or set(raw_coverage) != CRYPTO_PROBE_COVERAGE_FIELDS
    ):
        raise ValueError("invalid recovery crypto probe result")
    coverage: dict[str, dict[str, int]] = {}
    for label in CRYPTO_PROBE_COVERAGE_FIELDS:
        item = raw_coverage[label]
        if (
            not isinstance(item, dict)
            or set(item) != CRYPTO_PROBE_COVERAGE_VALUE_FIELDS
            or any(
                isinstance(number, bool)
                or not isinstance(number, int)
                or number < 0
                for number in item.values()
            )
            or (item["rows"] == 0) != (item["key_versions_verified"] == 0)
            or item["key_versions_verified"] > item["rows"]
        ):
            raise ValueError("invalid recovery crypto probe result")
        coverage[label] = {
            "rows": int(item["rows"]),
            "key_versions_verified": int(item["key_versions_verified"]),
        }
    encrypted_rows = sum(item["rows"] for item in coverage.values())
    samples = sum(
        item["key_versions_verified"] for item in coverage.values()
    )
    if (
        counts["audit_context_keys"] != 4
        or counts["encrypted_columns"] != len(CRYPTO_PROBE_COVERAGE_FIELDS)
        or counts["key_version_columns"] < 1
        or counts["encrypted_rows"] != encrypted_rows
        or counts["ciphertext_samples_verified"] != samples
        or counts["sms_message_rows"]
        != coverage["sms_message.phone_enc"]["rows"]
        or (
            status == "performed"
            and (
                counts["encrypted_rows"] < 1
                or counts["ciphertext_samples_verified"] < 1
                or counts["referenced_key_versions"] < 1
            )
        )
        or (
            status == "not_applicable_empty"
            and (
                counts["encrypted_rows"] != 0
                or counts["ciphertext_samples_verified"] != 0
            )
        )
    ):
        raise ValueError("invalid recovery crypto probe result")
    return {
        "schema_version": 2,
        "status": status,
        "counts": counts,
        "coverage": coverage,
    }


class RestoreService:
    """只恢复到 sms_drill_*，校验后默认立即销毁演练库。"""

    def __init__(
        self,
        runner: Runner,
        *,
        clock: Callable[[], datetime] = utc_now,
        timer: Callable[[], float] = time.monotonic,
        suffix: Callable[[], str] = lambda: secrets.token_hex(2),
        generation_reader: Callable[[Path], str] = read_root_generation_id_file,
    ) -> None:
        self.runner = runner
        self.clock = clock
        self.timer = timer
        self.suffix = suffix
        self.generation_reader = generation_reader

    @staticmethod
    def _compose(config: RestoreConfig) -> list[str]:
        return ["docker", "compose", "-f", str(config.compose_file)]

    @staticmethod
    def _query(compose: list[str], database: str, sql: str) -> list[str]:
        return compose + [
            "exec",
            "-T",
            "postgres",
            "psql",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "sms_owner",
            "-d",
            database,
            "-Atc",
            sql,
        ]

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self.timer()
        if not math.isfinite(remaining) or remaining <= 0:
            raise DeadlineExceeded
        return remaining

    def _run_command(
        self,
        command: Sequence[str],
        config: RestoreConfig,
        deadline: float,
    ) -> bytes:
        try:
            output = self.runner.run(
                command,
                cwd=config.repository_root,
                timeout=self._remaining(deadline),
            )
        except CommandFailure:
            self._remaining(deadline)
            raise RestoreCommandFailure from None
        except BaseException:
            self._remaining(deadline)
            raise
        self._remaining(deadline)
        return output

    def _restore_pipeline(
        self,
        producer: Sequence[str],
        consumer: Sequence[str],
        config: RestoreConfig,
        deadline: float,
    ) -> bytes:
        try:
            output = self.runner.pipeline_from_file(
                producer,
                consumer,
                config.backup_file,
                cwd=config.repository_root,
                timeout=self._remaining(deadline),
            )
        except CommandFailure:
            self._remaining(deadline)
            raise RestoreCommandFailure from None
        except BaseException:
            self._remaining(deadline)
            raise
        self._remaining(deadline)
        return output

    def _validate_config(
        self,
        config: RestoreConfig,
        now: datetime,
        deadline: float,
    ) -> tuple[Path, dict[str, Any]]:
        self._remaining(deadline)
        try:
            root = config.repository_root.resolve(strict=True)
            compose_available = config.compose_file.is_file()
        except BaseException:
            self._remaining(deadline)
            raise
        self._remaining(deadline)
        if not compose_available:
            raise ValueError("compose file unavailable")
        if config.keep and not config.drill_environment:
            raise ValueError("--keep requires DRILL_ENV=1")
        try:
            passphrase = validate_passphrase_file(config.passphrase_file, root)
        except BaseException:
            self._remaining(deadline)
            raise
        self._remaining(deadline)
        manifest = _manifest(
            config,
            now=now,
            deadline=deadline,
            timer=self.timer,
        )
        if (
            config.recovery_crypto_generation_id_file
            != RECOVERY_CRYPTO_GENERATION_ID_FILE
            or config.backup_passphrase_generation_id_file
            != BACKUP_PASSPHRASE_GENERATION_ID_FILE
        ):
            raise ValueError("generation id file paths are fixed")
        try:
            recovery_crypto_generation_id = validate_generation_id(
                self.generation_reader(config.recovery_crypto_generation_id_file)
            )
            backup_passphrase_generation_id = validate_generation_id(
                self.generation_reader(config.backup_passphrase_generation_id_file)
            )
        except BaseException:
            self._remaining(deadline)
            raise
        if (
            not secrets.compare_digest(
                recovery_crypto_generation_id,
                str(manifest["recovery_crypto_generation_id"]),
            )
            or not secrets.compare_digest(
                backup_passphrase_generation_id,
                str(manifest["backup_passphrase_generation_id"]),
            )
        ):
            raise ValueError("restore host generation binding does not match snapshot")
        self._remaining(deadline)
        return passphrase, manifest

    def run(self, config: RestoreConfig) -> RestoreResult:
        started = self.timer()
        if not math.isfinite(started):
            raise ValueError("invalid monotonic clock")
        if not math.isfinite(config.max_restore_seconds) or config.max_restore_seconds <= 0:
            raise ValueError("max restore seconds must be a positive finite value")
        deadline = started + config.max_restore_seconds
        try:
            moment = self.clock()
        except BaseException:
            self._remaining(deadline)
            raise
        self._remaining(deadline)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("drill clock must be timezone-aware")
        passphrase, manifest = self._validate_config(config, moment, deadline)
        try:
            suffix = self.suffix()
        except BaseException:
            self._remaining(deadline)
            raise
        self._remaining(deadline)
        database = validate_drill_database(
            f"sms_drill_{moment.astimezone(UTC):%Y%m%d%H%M%S}_{suffix}"
        )
        self._remaining(deadline)
        compose = self._compose(config)
        stale_count = _decode_single(
            self._run_command(
                self._query(
                    compose,
                    "postgres",
                    "SELECT count(*) FROM pg_database "
                    "WHERE datname ~ '^sms_drill_'",
                ),
                config,
                deadline,
            ),
            "stale drill database count",
        )
        if not stale_count.isdecimal():
            raise ValueError("invalid stale drill database check result")
        if int(stale_count) != 0:
            raise ValueError("stale drill database exists; manual cleanup required")

        creation_attempted = False
        failure: BaseException | None = None
        started_at = moment.astimezone(UTC)
        try:
            creation_attempted = True
            self._run_command(
                compose
                + [
                    "exec",
                    "-T",
                    "postgres",
                    "createdb",
                    "-U",
                    "sms_owner",
                    "-T",
                    "template0",
                    database,
                ],
                config,
                deadline,
            )
            self._restore_pipeline(
                [
                    "openssl",
                    "enc",
                    "-d",
                    "-aes-256-cbc",
                    "-pbkdf2",
                    "-iter",
                    "600000",
                    "-pass",
                    f"file:{passphrase}",
                ],
                compose
                + [
                    "exec",
                    "-T",
                    "postgres",
                    "pg_restore",
                    "-U",
                    "sms_owner",
                    "-d",
                    database,
                    "--no-owner",
                    "--exit-on-error",
                ],
                config,
                deadline,
            )
            before_migration = _decode_single(
                self._run_command(
                    self._query(
                        compose,
                        database,
                        "SELECT version_num FROM alembic_version",
                    ),
                    config,
                    deadline,
                ),
                "restored Alembic version",
            )
            if before_migration != manifest["alembic_version"]:
                raise ValueError("restored Alembic version does not match manifest")
            pre_migration_crypto_receipt = _parse_crypto_probe(
                self._run_command(
                    compose
                    + [
                        "run",
                        "--rm",
                        "--no-deps",
                        "-e",
                        f"DB_NAME={database}",
                        "migrate",
                        "python",
                        "-m",
                        "scripts_support.recovery_crypto_probe",
                    ],
                    config,
                    deadline,
                )
            )
            self._run_command(
                compose + ["run", "--rm", "-e", f"DB_NAME={database}", "migrate"],
                config,
                deadline,
            )
            post_migration_crypto_receipt = _parse_crypto_probe(
                self._run_command(
                    compose
                    + [
                        "run",
                        "--rm",
                        "--no-deps",
                        "-e",
                        f"DB_NAME={database}",
                        "migrate",
                        "python",
                        "-m",
                        "scripts_support.recovery_crypto_probe",
                    ],
                    config,
                    deadline,
                )
            )
            alembic_version = _decode_single(
                self._run_command(
                    self._query(
                        compose,
                        database,
                        "SELECT version_num FROM alembic_version",
                    ),
                    config,
                    deadline,
                ),
                "migrated Alembic version",
            )
            counts = _parse_counts(
                self._run_command(
                    self._query(
                        compose,
                        database,
                        "SELECT 'sms_batch=' || count(*) FROM sms_batch UNION ALL "
                        "SELECT 'sms_message=' || count(*) FROM sms_message UNION ALL "
                        "SELECT 'audit_log=' || count(*) FROM audit_log UNION ALL "
                        "SELECT 'raw_vendor_log=' || count(*) FROM raw_vendor_log",
                    ),
                    config,
                    deadline,
                )
            )
            structure = _decode_single(
                self._run_command(
                    self._query(
                        compose,
                        database,
                        "SELECT "
                        "(to_regclass('public.sms_batch') IS NOT NULL)::text || '|' || "
                        "(to_regclass('public.sms_message') IS NOT NULL)::text || '|' || "
                        "(to_regclass('public.audit_log') IS NOT NULL)::text || '|' || "
                        "(SELECT count(*)::text FROM information_schema.columns "
                        "WHERE table_schema='public' AND "
                        "(table_name,column_name) IN "
                        "(('sms_batch','batch_no'),('sms_batch','status'),"
                        "('sms_message','phone_enc'),('sms_message','phone_hmac'),"
                        "('sms_message','phone_mask'),('sms_message','key_version')))",
                    ),
                    config,
                    deadline,
                ),
                "restored table structure",
            )
            if structure != "true|true|true|6":
                raise ValueError("restored business table structure is incomplete")
            privilege_lines = self._run_command(
                self._query(
                    compose,
                    database,
                    "SELECT count(*)::text || '|' || "
                    "bool_and(NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole "
                    "AND NOT rolreplication AND NOT rolinherit)::text "
                    "FROM pg_roles WHERE rolname IN "
                    "('sms_auth','sms_accept','sms_send','sms_callback',"
                    "'sms_export','sms_scheduler','sms_metrics'); "
                    "SELECT bool_and(has_table_privilege(rolname,'audit_log','INSERT') "
                    "AND NOT has_table_privilege(rolname,'audit_log','UPDATE') "
                    "AND NOT has_table_privilege(rolname,'audit_log','DELETE') "
                    "AND NOT has_table_privilege(rolname,'audit_log','TRUNCATE'))::text "
                    "FROM pg_roles WHERE rolname IN "
                    "('sms_auth','sms_accept','sms_send','sms_callback',"
                    "'sms_export','sms_scheduler'); "
                    "SELECT (NOT rolcanlogin)::text || '|' || "
                    "(NOT EXISTS (SELECT 1 FROM information_schema.role_table_grants "
                    "WHERE grantee='sms_app'))::text "
                    "FROM pg_roles WHERE rolname='sms_app'",
                ),
                config,
                deadline,
            ).decode("utf-8", errors="strict").splitlines()
            if privilege_lines != ["7|true", "true", "true|true"]:
                raise ValueError("runtime role or audit_log privileges are unsafe")
            runtime_read = _decode_single(
                self._run_command(
                    self._query(
                        compose,
                        database,
                        "SET ROLE sms_accept; "
                        "SELECT CASE WHEN current_user='sms_accept' AND count(*) >= 0 "
                        "THEN 'ok' ELSE 'failed' END FROM sms_batch; "
                        "RESET ROLE",
                    ),
                    config,
                    deadline,
                ),
                "runtime business read",
            )
            if runtime_read != "ok":
                raise ValueError("runtime role cannot perform a basic business read")
            if (
                pre_migration_crypto_receipt["counts"]["sms_message_rows"]
                != counts["sms_message"]
                or post_migration_crypto_receipt["counts"]["sms_message_rows"]
                != counts["sms_message"]
            ):
                raise ValueError("recovery crypto probe count does not match restore")
        except BaseException as error:
            failure = error
            raise
        finally:
            if creation_attempted and not config.keep:
                try:
                    self.runner.run(
                        compose
                        + [
                            "exec",
                            "-T",
                            "postgres",
                            "dropdb",
                            "-U",
                            "sms_owner",
                            "--if-exists",
                            "--force",
                            database,
                        ],
                        cwd=config.repository_root,
                        timeout=CLEANUP_TIMEOUT_SECONDS,
                    )
                except BaseException as cleanup_error:
                    cleanup_failure = RestoreCleanupFailure()
                    if failure is not None:
                        cleanup_failure.add_note(
                            "restore failed before bounded cleanup also failed"
                        )
                    raise cleanup_failure from cleanup_error

        remaining = self._remaining(deadline)
        elapsed = round(config.max_restore_seconds - remaining, 3)
        try:
            finished_at = self.clock()
        except BaseException:
            self._remaining(deadline)
            raise
        self._remaining(deadline)
        if finished_at.tzinfo is None or finished_at.utcoffset() is None:
            raise ValueError("drill clock must be timezone-aware")
        report: dict[str, Any] = {
            "schema_version": 2,
            "status": "success",
            "metric_scope": "database_restore",
            "business_rto_evidence": False,
            "snapshot_id": manifest["snapshot_id"],
            "git_commit": manifest["git_commit"],
            "database": database,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.astimezone(UTC).isoformat(),
            "restore_seconds": elapsed,
            "restore_budget_seconds": config.max_restore_seconds,
            "within_restore_budget": True,
            "recovery_crypto_generation_id": manifest[
                "recovery_crypto_generation_id"
            ],
            "backup_passphrase_generation_id": manifest[
                "backup_passphrase_generation_id"
            ],
            "checks": {
                "alembic_version": alembic_version,
                "role_flags": privilege_lines[0],
                "audit_privileges": privilege_lines[1],
                "crypto_generation_binding": "matched_host_generation_ids",
                "historical_ciphertext_validation": pre_migration_crypto_receipt[
                    "status"
                ],
                "pre_migration_crypto_validation": pre_migration_crypto_receipt[
                    "status"
                ],
                "post_migration_crypto_validation": post_migration_crypto_receipt[
                    "status"
                ],
            },
            "crypto_probe_receipts": {
                "pre_migration": pre_migration_crypto_receipt,
                "post_migration": post_migration_crypto_receipt,
            },
            "table_counts": counts,
        }
        self._remaining(deadline)
        try:
            atomic_write_json(config.report_file, report)
        except BaseException:
            try:
                self._remaining(deadline)
            except DeadlineExceeded:
                config.report_file.unlink(missing_ok=True)
                raise
            raise
        try:
            self._remaining(deadline)
        except DeadlineExceeded:
            config.report_file.unlink(missing_ok=True)
            raise
        return RestoreResult(database, elapsed, True)


def _config_from_args(args: argparse.Namespace) -> RestoreConfig:
    root = Path(__file__).resolve().parents[2]
    passphrase_value = os.environ.get("BACKUP_PASSPHRASE_FILE", "")
    if not passphrase_value:
        raise ValueError("BACKUP_PASSPHRASE_FILE path is required")
    drill_environment = os.environ.get("DRILL_ENV") == "1"
    if not drill_environment:
        raise ValueError("direct restore requires DRILL_ENV=1")
    _validate_preproduction_marker()
    return RestoreConfig(
        repository_root=root,
        compose_file=root / "deploy/docker-compose.yml",
        backup_file=Path(args.backup),
        manifest_file=Path(args.manifest),
        passphrase_file=Path(passphrase_value),
        report_file=Path(args.report),
        keep=args.keep,
        drill_environment=drill_environment,
        max_restore_seconds=args.max_restore_seconds,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--report", type=Path, default=Path("drill-report.json"))
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--max-restore-seconds", type=float, default=43200)
    args = parser.parse_args()
    try:
        result = RestoreService(CommandRunner()).run(_config_from_args(args))
    except BaseException as error:
        if isinstance(error, DeadlineExceeded):
            error_type = "deadline_exceeded"
        elif isinstance(error, RestoreCleanupFailure):
            error_type = "cleanup_failed"
        else:
            error_type = "restore_failed"
        print(
            json.dumps(
                {"status": "failed", "error_type": error_type},
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "database": result.database,
                "restore_seconds": result.restore_seconds,
                "within_restore_budget": result.within_restore_budget,
            },
            ensure_ascii=False,
        )
    )
    return 0 if result.within_restore_budget else 2


if __name__ == "__main__":
    raise SystemExit(main())
