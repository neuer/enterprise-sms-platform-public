#!/usr/bin/env python3
"""把冷备快照恢复到隔离数据库并生成无 PII 的 RTO 演练报告。"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from failover_common import (
    CommandRunner,
    atomic_write_json,
    sha256_file,
    validate_drill_database,
    validate_passphrase_file,
)

SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class Runner(Protocol):
    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        input_bytes: bytes | None = None,
    ) -> bytes: ...

    def pipeline_from_file(
        self,
        producer: Sequence[str],
        consumer: Sequence[str],
        input_path: Path,
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
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
    max_restore_seconds: float = 1800


@dataclass(frozen=True, slots=True)
class RestoreResult:
    database: str
    restore_seconds: float
    within_rto: bool


def utc_now() -> datetime:
    return datetime.now(UTC)


def _regular_file(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular non-symlink file")
    return path.resolve(strict=True)


def _manifest(config: RestoreConfig) -> dict[str, Any]:
    manifest_path = _regular_file(config.manifest_file, "manifest")
    backup_path = _regular_file(config.backup_file, "backup")
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError("invalid snapshot manifest") from error
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("unsupported snapshot manifest")
    if value.get("secrets_included") is not False:
        raise ValueError("snapshot manifest must confirm secrets are excluded")
    for key in ("snapshot_id", "git_commit", "alembic_version"):
        item = value.get(key)
        if not isinstance(item, str) or SAFE_ID.fullmatch(item) is None:
            raise ValueError(f"invalid manifest field: {key}")
    files = value.get("files")
    database = files.get("database") if isinstance(files, dict) else None
    if not isinstance(database, dict):
        raise ValueError("database backup missing from manifest")
    if database.get("name") != backup_path.name:
        raise ValueError("backup filename does not match manifest")
    if database.get("size") != backup_path.stat().st_size:
        raise ValueError("backup size does not match manifest")
    expected_hash = database.get("sha256")
    if not isinstance(expected_hash, str) or sha256_file(backup_path) != expected_hash:
        raise ValueError("backup SHA-256 does not match manifest")
    return value


def _decode_single(output: bytes, label: str) -> str:
    value = output.decode("utf-8", errors="strict").strip()
    if not value or "\n" in value:
        raise ValueError(f"invalid {label} check result")
    return value


def _parse_counts(output: bytes) -> dict[str, int]:
    allowed = {"sms_batch", "audit_log", "raw_vendor_log"}
    result: dict[str, int] = {}
    for line in output.decode("utf-8", errors="strict").splitlines():
        key, separator, raw_count = line.partition("=")
        if not separator or key not in allowed or not raw_count.isdecimal():
            raise ValueError("invalid table-count check result")
        result[key] = int(raw_count)
    if result.keys() != allowed:
        raise ValueError("incomplete table-count check result")
    return result


class RestoreService:
    """只恢复到 sms_drill_*，校验后默认立即销毁演练库。"""

    def __init__(
        self,
        runner: Runner,
        *,
        clock: Callable[[], datetime] = utc_now,
        timer: Callable[[], float] = time.monotonic,
        suffix: Callable[[], str] = lambda: secrets.token_hex(2),
    ) -> None:
        self.runner = runner
        self.clock = clock
        self.timer = timer
        self.suffix = suffix

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

    def _validate_config(self, config: RestoreConfig) -> tuple[Path, dict[str, Any]]:
        root = config.repository_root.resolve(strict=True)
        if not config.compose_file.is_file():
            raise ValueError("compose file unavailable")
        if config.keep and not config.drill_environment:
            raise ValueError("--keep requires DRILL_ENV=1")
        if config.max_restore_seconds <= 0:
            raise ValueError("max restore seconds must be positive")
        passphrase = validate_passphrase_file(config.passphrase_file, root)
        return passphrase, _manifest(config)

    def run(self, config: RestoreConfig) -> RestoreResult:
        passphrase, manifest = self._validate_config(config)
        moment = self.clock()
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("drill clock must be timezone-aware")
        database = validate_drill_database(
            f"sms_drill_{moment.astimezone(UTC):%Y%m%d%H%M%S}_{self.suffix()}"
        )
        compose = self._compose(config)
        created = False
        started_at = moment.astimezone(UTC)
        started = self.timer()
        try:
            self.runner.run(
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
                cwd=config.repository_root,
            )
            created = True
            self.runner.pipeline_from_file(
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
                config.backup_file,
                cwd=config.repository_root,
            )
            before_migration = _decode_single(
                self.runner.run(
                    self._query(
                        compose,
                        database,
                        "SELECT version_num FROM alembic_version",
                    ),
                    cwd=config.repository_root,
                ),
                "restored Alembic version",
            )
            if before_migration != manifest["alembic_version"]:
                raise ValueError("restored Alembic version does not match manifest")
            self.runner.run(
                compose + ["run", "--rm", "-e", f"DB_NAME={database}", "migrate"],
                cwd=config.repository_root,
            )
            alembic_version = _decode_single(
                self.runner.run(
                    self._query(
                        compose,
                        database,
                        "SELECT version_num FROM alembic_version",
                    ),
                    cwd=config.repository_root,
                ),
                "migrated Alembic version",
            )
            counts = _parse_counts(
                self.runner.run(
                    self._query(
                        compose,
                        database,
                        "SELECT 'sms_batch=' || count(*) FROM sms_batch UNION ALL "
                        "SELECT 'audit_log=' || count(*) FROM audit_log UNION ALL "
                        "SELECT 'raw_vendor_log=' || count(*) FROM raw_vendor_log",
                    ),
                    cwd=config.repository_root,
                )
            )
            structure = _decode_single(
                self.runner.run(
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
                    cwd=config.repository_root,
                ),
                "restored table structure",
            )
            if structure != "true|true|true|6":
                raise ValueError("restored business table structure is incomplete")
            privilege_lines = self.runner.run(
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
                cwd=config.repository_root,
            ).decode("utf-8", errors="strict").splitlines()
            if privilege_lines != ["7|true", "true", "true|true"]:
                raise ValueError("runtime role or audit_log privileges are unsafe")
            runtime_read = _decode_single(
                self.runner.run(
                    self._query(
                        compose,
                        database,
                        "SET ROLE sms_accept; "
                        "SELECT CASE WHEN current_user='sms_accept' AND count(*) >= 0 "
                        "THEN 'ok' ELSE 'failed' END FROM sms_batch; "
                        "RESET ROLE",
                    ),
                    cwd=config.repository_root,
                ),
                "runtime business read",
            )
            if runtime_read != "ok":
                raise ValueError("runtime role cannot perform a basic business read")
            elapsed = round(self.timer() - started, 3)
            within_rto = elapsed <= config.max_restore_seconds
            finished_at = self.clock().astimezone(UTC)
            report: dict[str, Any] = {
                "schema_version": 1,
                "status": "success" if within_rto else "rto_failed",
                "snapshot_id": manifest["snapshot_id"],
                "git_commit": manifest["git_commit"],
                "database": database,
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "restore_seconds": elapsed,
                "rto_limit_seconds": config.max_restore_seconds,
                "within_rto": within_rto,
                "checks": {
                    "alembic_version": alembic_version,
                    "role_flags": privilege_lines[0],
                    "audit_privileges": privilege_lines[1],
                },
                "table_counts": counts,
            }
            atomic_write_json(config.report_file, report)
            return RestoreResult(database, elapsed, within_rto)
        finally:
            if created and not config.keep:
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
                        database,
                    ],
                    cwd=config.repository_root,
                )


def _config_from_args(args: argparse.Namespace) -> RestoreConfig:
    root = Path(__file__).resolve().parents[2]
    passphrase_value = os.environ.get("BACKUP_PASSPHRASE_FILE", "")
    if not passphrase_value:
        raise ValueError("BACKUP_PASSPHRASE_FILE path is required")
    return RestoreConfig(
        repository_root=root,
        compose_file=root / "deploy/docker-compose.yml",
        backup_file=Path(args.backup),
        manifest_file=Path(args.manifest),
        passphrase_file=Path(passphrase_value),
        report_file=Path(args.report),
        keep=args.keep,
        drill_environment=os.environ.get("DRILL_ENV") == "1",
        max_restore_seconds=args.max_restore_seconds,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--report", type=Path, default=Path("drill-report.json"))
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--max-restore-seconds", type=float, default=1800)
    args = parser.parse_args()
    result = RestoreService(CommandRunner()).run(_config_from_args(args))
    print(
        json.dumps(
            {
                "database": result.database,
                "restore_seconds": result.restore_seconds,
                "within_rto": result.within_rto,
            },
            ensure_ascii=False,
        )
    )
    return 0 if result.within_rto else 2


if __name__ == "__main__":
    raise SystemExit(main())
