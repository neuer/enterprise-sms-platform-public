#!/usr/bin/env python3
"""生成加密备份、恢复最新合格快照并维护无 PII 的恢复证据账本。"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import secrets
import shutil
import stat
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from failover_common import (
    BACKUP_PASSPHRASE_GENERATION_ID_FILE,
    RECOVERY_CRYPTO_GENERATION_ID_FILE,
    CommandRunner,
    DeadlineExceeded,
    atomic_write_json,
    read_root_generation_id_file,
    sha256_file,
    validate_drill_database,
    validate_generation_id,
    validate_snapshot_bundle,
)
from restore_drill import (
    RestoreConfig,
    RestoreResult,
    RestoreService,
    _parse_crypto_probe,
    _validate_preproduction_marker,
)
from sync_standby import SyncConfig, SyncResult, SyncService

SNAPSHOT_ID = re.compile(r"^\d{8}T\d{6}Z_[0-9a-f]{12}$")
RESTORE_REPORT_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "metric_scope",
        "business_rto_evidence",
        "snapshot_id",
        "git_commit",
        "database",
        "started_at",
        "finished_at",
        "restore_seconds",
        "restore_budget_seconds",
        "within_restore_budget",
        "recovery_crypto_generation_id",
        "backup_passphrase_generation_id",
        "checks",
        "crypto_probe_receipts",
        "table_counts",
    }
)
RESTORE_CHECK_FIELDS = frozenset(
    {
        "alembic_version",
        "role_flags",
        "audit_privileges",
        "crypto_generation_binding",
        "historical_ciphertext_validation",
        "pre_migration_crypto_validation",
        "post_migration_crypto_validation",
    }
)
RESTORE_TABLE_COUNT_FIELDS = frozenset(
    {"sms_batch", "sms_message", "audit_log", "raw_vendor_log"}
)
CONFIG_FIELDS = frozenset(
    {
        "schema_version",
        "environment_file",
        "output_root",
        "recovery_crypto_generation_id_file",
        "backup_passphrase_generation_id_file",
        "database",
        "retention_days",
        "minimum_snapshots",
        "max_backup_seconds",
        "max_backup_age_hours",
        "max_restore_age_hours",
        "max_restore_seconds",
    }
)
PRODUCTION_BACKUP_ROOT = Path("/var/lib/sms-platform/runtime/backups")


class SyncRunner(Protocol):
    def run(self, config: SyncConfig) -> SyncResult: ...


class DrillRunner(Protocol):
    def run(self, config: RestoreConfig) -> RestoreResult: ...


@dataclass(frozen=True, slots=True)
class LifecycleConfig:
    environment_file: Path
    output_root: Path
    database: str
    retention_days: int
    minimum_snapshots: int
    max_backup_seconds: float
    max_backup_age_hours: int
    max_restore_age_hours: int
    max_restore_seconds: float
    recovery_crypto_generation_id_file: Path = RECOVERY_CRYPTO_GENERATION_ID_FILE
    backup_passphrase_generation_id_file: Path = BACKUP_PASSPHRASE_GENERATION_ID_FILE


@dataclass(frozen=True, slots=True)
class SnapshotEvidence:
    snapshot_id: str
    created_at: datetime
    git_commit: str
    alembic_version: str
    backup_file: Path
    manifest_file: Path
    manifest_sha256: str
    database_sha256: str
    recovery_crypto_generation_id: str
    backup_passphrase_generation_id: str


@dataclass(frozen=True, slots=True)
class LifecycleStatus:
    evidence: Mapping[str, Any]
    healthy: bool


def utc_now() -> datetime:
    return datetime.now(UTC)


def _aware_datetime(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"invalid {label}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"invalid {label}") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"invalid {label}")
    return parsed.astimezone(UTC)


def _restore_report_datetime(value: object, label: str) -> datetime:
    if (
        not isinstance(value, str)
        or re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?\+00:00",
            value,
        )
        is None
    ):
        raise ValueError(f"invalid {label}")
    return _aware_datetime(value, label)


def _absolute_path(value: object, label: str) -> Path:
    if not isinstance(value, str):
        raise ValueError(f"invalid {label}")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"invalid {label}")
    return path


def _bounded_int(value: object, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"invalid {label}")
    if not minimum <= value <= maximum:
        raise ValueError(f"invalid {label}")
    return value


def load_config(path: Path) -> LifecycleConfig:
    """读取仅含路径和恢复目标的 0600 配置，拒绝链接与额外字段。"""

    if path.is_symlink() or not path.is_file():
        raise ValueError("lifecycle config must be a regular non-symlink file")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ValueError("lifecycle config permissions must be 0600")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError("invalid lifecycle config") from error
    if not isinstance(value, dict) or set(value) != CONFIG_FIELDS:
        raise ValueError("invalid lifecycle config fields")
    if value.get("schema_version") != 1:
        raise ValueError("unsupported lifecycle config")
    recovery_generation_file = _absolute_path(
        value["recovery_crypto_generation_id_file"],
        "recovery crypto generation id file",
    )
    backup_generation_file = _absolute_path(
        value["backup_passphrase_generation_id_file"],
        "backup passphrase generation id file",
    )
    if (
        recovery_generation_file != RECOVERY_CRYPTO_GENERATION_ID_FILE
        or backup_generation_file != BACKUP_PASSPHRASE_GENERATION_ID_FILE
    ):
        raise ValueError("generation id file paths are fixed")
    database = value.get("database")
    if not isinstance(database, str) or re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", database) is None:
        raise ValueError("invalid lifecycle database")
    max_restore_seconds = value.get("max_restore_seconds")
    if (
        isinstance(max_restore_seconds, bool)
        or not isinstance(max_restore_seconds, (int, float))
        or not 60 <= float(max_restore_seconds) <= 43200
    ):
        raise ValueError("invalid max restore seconds")
    max_backup_seconds = value.get("max_backup_seconds")
    if (
        isinstance(max_backup_seconds, bool)
        or not isinstance(max_backup_seconds, (int, float))
        or not 60 <= float(max_backup_seconds) <= 14400
    ):
        raise ValueError("invalid max backup seconds")
    return LifecycleConfig(
        environment_file=_absolute_path(value["environment_file"], "environment file"),
        output_root=_absolute_path(value["output_root"], "output root"),
        recovery_crypto_generation_id_file=recovery_generation_file,
        backup_passphrase_generation_id_file=backup_generation_file,
        database=database,
        retention_days=_bounded_int(value["retention_days"], "retention days", 7, 365),
        minimum_snapshots=_bounded_int(
            value["minimum_snapshots"], "minimum snapshots", 2, 30
        ),
        max_backup_seconds=float(max_backup_seconds),
        max_backup_age_hours=_bounded_int(
            value["max_backup_age_hours"], "max backup age", 1, 168
        ),
        max_restore_age_hours=_bounded_int(
            value["max_restore_age_hours"], "max restore age", 1, 744
        ),
        max_restore_seconds=float(max_restore_seconds),
    )


class LifecycleService:
    """以原子账本记录备份完整性和隔离恢复证据，失败时保持关闭。"""

    def __init__(
        self,
        repository_root: Path,
        passphrase_file: Path,
        *,
        runner: CommandRunner | None = None,
        sync_service: SyncRunner | None = None,
        restore_service: DrillRunner | None = None,
        clock: Callable[[], datetime] = utc_now,
        timer: Callable[[], float] = time.monotonic,
        generation_reader: Callable[[Path], str] = read_root_generation_id_file,
        marker_validator: Callable[[], None] = _validate_preproduction_marker,
    ) -> None:
        self.repository_root = repository_root
        self.passphrase_file = passphrase_file
        command_runner = runner or CommandRunner()
        self.sync_service = sync_service or SyncService(
            command_runner,
            generation_reader=generation_reader,
        )
        self.restore_service = restore_service or RestoreService(
            command_runner,
            generation_reader=generation_reader,
        )
        self.clock = clock
        self.timer = timer
        self.generation_reader = generation_reader
        self.marker_validator = marker_validator

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self.timer()
        if not math.isfinite(remaining) or remaining <= 0:
            raise DeadlineExceeded
        return remaining

    def _read_host_generation_ids(
        self,
        config: LifecycleConfig,
        deadline: float,
    ) -> tuple[str, str]:
        if (
            config.recovery_crypto_generation_id_file
            != RECOVERY_CRYPTO_GENERATION_ID_FILE
            or config.backup_passphrase_generation_id_file
            != BACKUP_PASSPHRASE_GENERATION_ID_FILE
        ):
            raise ValueError("generation id file paths are fixed")
        try:
            recovery_id = validate_generation_id(
                self.generation_reader(config.recovery_crypto_generation_id_file)
            )
            passphrase_id = validate_generation_id(
                self.generation_reader(config.backup_passphrase_generation_id_file)
            )
        except BaseException:
            self._remaining(deadline)
            raise
        self._remaining(deadline)
        return recovery_id, passphrase_id

    @staticmethod
    def _generation_ids_match(
        evidence: SnapshotEvidence,
        generation_ids: tuple[str, str],
    ) -> bool:
        return secrets.compare_digest(
            evidence.recovery_crypto_generation_id,
            generation_ids[0],
        ) and secrets.compare_digest(
            evidence.backup_passphrase_generation_id,
            generation_ids[1],
        )

    def _now(self) -> datetime:
        moment = self.clock()
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("lifecycle clock must be timezone-aware")
        return moment.astimezone(UTC)

    @staticmethod
    def _state_path(config: LifecycleConfig) -> Path:
        return config.output_root / "lifecycle-state.json"

    def _prevalidate_output_root(self, config: LifecycleConfig) -> None:
        if config.output_root.is_symlink():
            raise ValueError("output root must not be a symlink")
        if config.output_root.resolve(strict=False).is_relative_to(
            self.repository_root.resolve(strict=True)
        ):
            raise ValueError("backup output root must be outside the repository")
        for child_name in ("snapshots", "reports", ".incoming"):
            if config.output_root.joinpath(child_name).is_symlink():
                raise ValueError("backup output child must not be a symlink")

    @contextmanager
    def _lock(
        self,
        config: LifecycleConfig,
        deadline: float | None = None,
    ) -> Iterator[None]:
        if config.output_root.is_symlink():
            raise ValueError("output root must not be a symlink")
        config.output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not config.output_root.is_dir():
            raise ValueError("output root must be a directory")
        if config.output_root.resolve().is_relative_to(
            self.repository_root.resolve(strict=True)
        ):
            raise ValueError("backup output root must be outside the repository")
        config.output_root.chmod(0o700)
        for child_name in ("snapshots", "reports", ".incoming"):
            child = config.output_root / child_name
            if child.is_symlink():
                raise ValueError("backup output child must not be a symlink")
        lock_fd = os.open(
            config.output_root / ".lifecycle.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
        try:
            if deadline is None:
                # 完整 dump/restore 已在锁外运行；普通账本临界区等待短竞争。
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            else:
                while True:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        time.sleep(min(0.05, self._remaining(deadline)))
                self._remaining(deadline)
            yield
        finally:
            os.close(lock_fd)

    @contextmanager
    def _operation_lock(self, config: LifecycleConfig, name: str) -> Iterator[None]:
        """长操作独占自身租约，但不占用可并行的生命周期账本锁。"""

        if name not in {".drill.lock"}:
            raise ValueError("invalid lifecycle operation lock")
        if config.output_root.is_symlink() or not config.output_root.is_dir():
            raise ValueError("output root must be a non-symlink directory")
        lock_fd = os.open(
            config.output_root / name,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
        try:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("lifecycle operation already running") from error
            yield
        finally:
            os.close(lock_fd)

    def _empty_state(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "last_successful_backup": None,
            "last_successful_restore": None,
            "last_failure": None,
            "retention": None,
            "snapshots": {},
        }

    def _load_state(self, config: LifecycleConfig) -> dict[str, Any]:
        path = self._state_path(config)
        if path.is_symlink():
            raise ValueError("lifecycle state must not be a symlink")
        if not path.exists():
            return self._empty_state()
        if not path.is_file():
            raise ValueError("lifecycle state must be a regular non-symlink file")
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise ValueError("lifecycle state permissions must be 0600")
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError("invalid lifecycle state") from error
        expected = set(self._empty_state())
        if (
            not isinstance(state, dict)
            or set(state) != expected
            or state.get("schema_version") != 1
            or not isinstance(state.get("snapshots"), dict)
        ):
            raise ValueError("invalid lifecycle state")
        snapshots = state["snapshots"]
        if any(
            not isinstance(key, str)
            or SNAPSHOT_ID.fullmatch(key) is None
            or not isinstance(item, dict)
            for key, item in snapshots.items()
        ):
            raise ValueError("invalid lifecycle snapshot state")
        return state

    def _save_state(self, config: LifecycleConfig, state: Mapping[str, Any]) -> None:
        atomic_write_json(self._state_path(config), state)

    def _failure(
        self,
        config: LifecycleConfig,
        operation: str,
        error_type: str,
        *,
        snapshot_id: str | None = None,
        invalidate_integrity: bool = False,
    ) -> None:
        state = self._load_state(config)
        if snapshot_id is not None:
            snapshot = state["snapshots"].get(snapshot_id)
            if isinstance(snapshot, dict):
                snapshot["available"] = False
                snapshot["restore_verified"] = False
                if invalidate_integrity:
                    snapshot["integrity_verified"] = False
        state["last_failure"] = {
            "at": self._now().isoformat(),
            "operation": operation,
            "error_type": error_type,
        }
        self._save_state(config, state)

    def _snapshot_path(self, config: LifecycleConfig, snapshot_id: str) -> Path:
        if SNAPSHOT_ID.fullmatch(snapshot_id) is None:
            raise ValueError("invalid snapshot id")
        snapshots = config.output_root / "snapshots"
        if snapshots.is_symlink() or not snapshots.is_dir():
            raise ValueError("snapshot root is unsafe")
        candidate = snapshots / snapshot_id
        if (
            candidate.is_symlink()
            or not candidate.is_dir()
            or stat.S_IMODE(candidate.stat().st_mode) != 0o700
        ):
            raise ValueError("snapshot directory unavailable")
        if candidate.resolve().parent != snapshots.resolve():
            raise ValueError("snapshot path escapes snapshot root")
        return candidate

    def _verify_snapshot(
        self,
        config: LifecycleConfig,
        snapshot_id: str,
        deadline: float | None = None,
    ) -> SnapshotEvidence:
        snapshot = self._snapshot_path(config, snapshot_id)
        bundle = validate_snapshot_bundle(
            snapshot,
            now=self._now(),
            deadline=deadline,
            timer=self.timer,
        )
        manifest = bundle.manifest
        recovery_crypto_generation_id = validate_generation_id(
            manifest.get("recovery_crypto_generation_id")
        )
        backup_passphrase_generation_id = validate_generation_id(
            manifest.get("backup_passphrase_generation_id")
        )
        files = manifest.get("files")
        if not isinstance(files, dict):
            raise ValueError("snapshot file inventory is incomplete")
        return SnapshotEvidence(
            snapshot_id=snapshot_id,
            created_at=bundle.created_at,
            git_commit=str(manifest["git_commit"]),
            alembic_version=str(manifest["alembic_version"]),
            backup_file=bundle.files["database"],
            manifest_file=snapshot / "manifest.json",
            manifest_sha256=bundle.manifest_sha256,
            database_sha256=str(files["database"]["sha256"]),
            recovery_crypto_generation_id=recovery_crypto_generation_id,
            backup_passphrase_generation_id=backup_passphrase_generation_id,
        )

    @staticmethod
    def _evidence_matches_state(
        evidence: SnapshotEvidence,
        snapshot_state: Mapping[str, Any],
    ) -> bool:
        return (
            evidence.manifest_sha256 == snapshot_state.get("manifest_sha256")
            and evidence.database_sha256 == snapshot_state.get("database_sha256")
            and evidence.recovery_crypto_generation_id
            == snapshot_state.get("recovery_crypto_generation_id")
            and evidence.backup_passphrase_generation_id
            == snapshot_state.get("backup_passphrase_generation_id")
        )

    @staticmethod
    def _same_evidence(left: SnapshotEvidence, right: SnapshotEvidence) -> bool:
        return (
            left.manifest_sha256 == right.manifest_sha256
            and left.database_sha256 == right.database_sha256
            and left.git_commit == right.git_commit
            and left.alembic_version == right.alembic_version
            and left.recovery_crypto_generation_id
            == right.recovery_crypto_generation_id
            and left.backup_passphrase_generation_id
            == right.backup_passphrase_generation_id
        )

    def _verify_restore_report(
        self,
        report_path: Path,
        evidence: SnapshotEvidence,
        result: RestoreResult,
        expected_restore_budget: float,
        deadline: float,
    ) -> tuple[Mapping[str, Any], str]:
        self._remaining(deadline)
        if (
            report_path.is_symlink()
            or not report_path.is_file()
            or stat.S_IMODE(report_path.stat().st_mode) != 0o600
        ):
            raise ValueError("restore report is unsafe")
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError("restore report is invalid") from error
        self._remaining(deadline)
        if not isinstance(report, dict):
            raise ValueError("restore report is invalid")
        checks = report.get("checks")
        counts = report.get("table_counts")
        receipts = report.get("crypto_probe_receipts")
        restore_seconds = report.get("restore_seconds")
        restore_budget = report.get("restore_budget_seconds")
        try:
            raw_database = report.get("database")
            if not isinstance(raw_database, str):
                raise ValueError("restore report database is invalid")
            database = validate_drill_database(raw_database)
            started_at = _restore_report_datetime(
                report.get("started_at"), "restore report started_at"
            )
            finished_at = _restore_report_datetime(
                report.get("finished_at"), "restore report finished_at"
            )
        except (TypeError, ValueError) as error:
            raise ValueError("restore report generation binding is invalid") from error
        if (
            set(report) != RESTORE_REPORT_FIELDS
            or report.get("schema_version") != 2
            or report.get("status") != "success"
            or report.get("metric_scope") != "database_restore"
            or report.get("business_rto_evidence") is not False
            or report.get("snapshot_id") != evidence.snapshot_id
            or report.get("git_commit") != evidence.git_commit
            or database != result.database
            or report.get("within_restore_budget") is not True
            or report.get("recovery_crypto_generation_id")
            != evidence.recovery_crypto_generation_id
            or report.get("backup_passphrase_generation_id")
            != evidence.backup_passphrase_generation_id
            or not isinstance(checks, dict)
            or set(checks) != RESTORE_CHECK_FIELDS
            or checks.get("crypto_generation_binding")
            != "matched_host_generation_ids"
            or checks.get("alembic_version") != evidence.alembic_version
            or checks.get("role_flags") != "7|true"
            or checks.get("audit_privileges") != "true"
            or not isinstance(counts, dict)
            or set(counts) != RESTORE_TABLE_COUNT_FIELDS
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in counts.values()
            )
            or isinstance(restore_seconds, bool)
            or not isinstance(restore_seconds, (int, float))
            or not math.isfinite(float(restore_seconds))
            or float(restore_seconds) < 0
            or float(restore_seconds) != result.restore_seconds
            or isinstance(restore_budget, bool)
            or not isinstance(restore_budget, (int, float))
            or not math.isfinite(float(restore_budget))
            or float(restore_budget) <= 0
            or float(restore_budget) != expected_restore_budget
            or float(restore_seconds) > float(restore_budget)
            or started_at > finished_at
            or not isinstance(receipts, dict)
            or set(receipts) != {"pre_migration", "post_migration"}
        ):
            raise ValueError("restore report generation binding is invalid")
        try:
            pre_receipt = _parse_crypto_probe(
                json.dumps(
                    receipts["pre_migration"],
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            post_receipt = _parse_crypto_probe(
                json.dumps(
                    receipts["post_migration"],
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("restore report crypto receipts are invalid") from error
        historical_status = checks["historical_ciphertext_validation"]
        if (
            historical_status != pre_receipt["status"]
            or checks["pre_migration_crypto_validation"] != pre_receipt["status"]
            or checks["post_migration_crypto_validation"] != post_receipt["status"]
            or pre_receipt["counts"]["sms_message_rows"]
            != counts["sms_message"]
            or post_receipt["counts"]["sms_message_rows"]
            != counts["sms_message"]
        ):
            raise ValueError("historical ciphertext validation is incomplete")
        digest = sha256_file(report_path, deadline=deadline, timer=self.timer)
        return report, digest

    def _prune(
        self,
        config: LifecycleConfig,
        state: dict[str, Any],
        now: datetime,
    ) -> list[str]:
        cutoff = now - timedelta(days=config.retention_days)
        snapshots: dict[str, Any] = state["snapshots"]
        current_link = config.output_root / "current"
        current_id: str | None = None
        if current_link.is_symlink():
            target = current_link.readlink()
            if (
                len(target.parts) == 2
                and target.parts[0] == "snapshots"
                and SNAPSHOT_ID.fullmatch(target.parts[1]) is not None
            ):
                current_id = target.parts[1]
        ordered = sorted(
            snapshots,
            key=lambda item: _aware_datetime(
                snapshots[item].get("created_at"), "snapshot state creation time"
            ),
        )
        removed: list[str] = []
        for snapshot_id in ordered:
            if len(snapshots) - len(removed) <= config.minimum_snapshots:
                break
            created_at = _aware_datetime(
                snapshots[snapshot_id].get("created_at"),
                "snapshot state creation time",
            )
            if created_at >= cutoff or snapshot_id == current_id:
                continue
            snapshot_path = self._snapshot_path(config, snapshot_id)
            shutil.rmtree(snapshot_path)
            removed.append(snapshot_id)
        for snapshot_id in removed:
            snapshots.pop(snapshot_id)
        state["retention"] = {
            "checked_at": now.isoformat(),
            "boundary": cutoff.isoformat(),
            "retention_days": config.retention_days,
            "minimum_snapshots": config.minimum_snapshots,
            "removed_count": len(removed),
            "retained_count": len(snapshots),
        }
        return removed

    def backup(self, config: LifecycleConfig) -> SnapshotEvidence:
        self._prevalidate_output_root(config)
        try:
            # pg_dump/加密可耗时数小时，不能占用生命周期账本锁。
            result = self.sync_service.run(
                SyncConfig(
                    repository_root=self.repository_root,
                    compose_file=self.repository_root / "deploy/docker-compose.yml",
                    environment_file=config.environment_file,
                    output_dir=config.output_root,
                    passphrase_file=self.passphrase_file,
                    database=config.database,
                    build_only=True,
                    recovery_crypto_generation_id_file=(
                        config.recovery_crypto_generation_id_file
                    ),
                    backup_passphrase_generation_id_file=(
                        config.backup_passphrase_generation_id_file
                    ),
                    max_backup_seconds=config.max_backup_seconds,
                )
            )
        except Exception as error:
            with self._lock(config):
                self._failure(config, "backup", type(error).__name__)
            raise

        with self._lock(config):
            try:
                if SNAPSHOT_ID.fullmatch(result.snapshot_id) is None:
                    raise ValueError("backup returned an invalid snapshot id")
                state = self._load_state(config)
                state["snapshots"][result.snapshot_id] = {
                    "created_at": self._now().isoformat(),
                    "integrity_verified": False,
                    "restore_verified": False,
                    "available": False,
                    "manifest_sha256": None,
                    "database_sha256": None,
                    "recovery_crypto_generation_id": None,
                    "backup_passphrase_generation_id": None,
                    "last_restore": None,
                }
                self._save_state(config, state)
                evidence = self._verify_snapshot(config, result.snapshot_id)
                snapshot_state = state["snapshots"][result.snapshot_id]
                snapshot_state.update(
                    {
                        "created_at": evidence.created_at.isoformat(),
                        "integrity_verified": True,
                        "manifest_sha256": evidence.manifest_sha256,
                        "database_sha256": evidence.database_sha256,
                        "recovery_crypto_generation_id": (
                            evidence.recovery_crypto_generation_id
                        ),
                        "backup_passphrase_generation_id": (
                            evidence.backup_passphrase_generation_id
                        ),
                    }
                )
                completed_at = self._now()
                state["last_successful_backup"] = {
                    "snapshot_id": result.snapshot_id,
                    "completed_at": completed_at.isoformat(),
                    "created_at": evidence.created_at.isoformat(),
                    "integrity_verified": True,
                    "recovery_crypto_generation_id": (
                        evidence.recovery_crypto_generation_id
                    ),
                    "backup_passphrase_generation_id": (
                        evidence.backup_passphrase_generation_id
                    ),
                }
                state["last_failure"] = None
                self._prune(config, state, completed_at)
                self._save_state(config, state)
                return evidence
            except Exception as error:
                self._failure(config, "backup", type(error).__name__)
                raise

    def drill(self, config: LifecycleConfig) -> Mapping[str, Any]:
        started = self.timer()
        if not math.isfinite(started):
            raise ValueError("invalid monotonic clock")
        if (
            not math.isfinite(config.max_restore_seconds)
            or not 60 <= config.max_restore_seconds <= 43200
        ):
            raise ValueError("max restore seconds must be between 60 and 43200")
        deadline = started + config.max_restore_seconds
        try:
            self.marker_validator()
        except BaseException:
            self._remaining(deadline)
            raise
        self._remaining(deadline)
        generation_ids = self._read_host_generation_ids(config, deadline)
        with self._operation_lock(config, ".drill.lock"):
            self._remaining(deadline)
            return self._drill_once(config, deadline, generation_ids)

    def _drill_once(
        self,
        config: LifecycleConfig,
        deadline: float,
        generation_ids: tuple[str, str],
    ) -> Mapping[str, Any]:
        selected: str | None = None
        verified = False
        selection_completed = False
        evidence: SnapshotEvidence | None = None
        report_path: Path | None = None
        try:
            # 只在选择、钉住和最终提交证据时持锁。隔离恢复工程预算最长 12h，
            # 不能因此阻塞新的备份或小时级状态检查。
            with self._lock(config, deadline):
                state = self._load_state(config)
                latest = state.get("last_successful_backup")
                selected = (
                    latest.get("snapshot_id") if isinstance(latest, dict) else None
                )
                snapshot_state = (
                    state["snapshots"].get(selected)
                    if isinstance(selected, str)
                    else None
                )
                if (
                    not isinstance(selected, str)
                    or not isinstance(snapshot_state, dict)
                    or snapshot_state.get("integrity_verified") is not True
                ):
                    raise ValueError("latest integrity-verified snapshot is unavailable")
                evidence = self._verify_snapshot(config, selected, deadline)
                if (
                    not self._evidence_matches_state(evidence, snapshot_state)
                    or not self._generation_ids_match(evidence, generation_ids)
                ):
                    raise ValueError("latest snapshot digest changed before restore")
                verified = True
                report_dir = config.output_root / "reports"
                report_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                if report_dir.is_symlink() or not report_dir.is_dir():
                    raise ValueError("restore report directory is unsafe")
                report_dir.chmod(0o700)
                report_name = (
                    f"{selected}_{self._now():%Y%m%dT%H%M%SZ}_"
                    f"{secrets.token_hex(4)}.json"
                )
                report_path = report_dir / report_name
                selection_completed = True
                self._remaining(deadline)

            if evidence is None or report_path is None:
                raise RuntimeError("restore drill snapshot pin is unavailable")

            restore_budget = self._remaining(deadline)
            result = self.restore_service.run(
                RestoreConfig(
                    repository_root=self.repository_root,
                    compose_file=self.repository_root / "deploy/docker-compose.yml",
                    backup_file=evidence.backup_file,
                    manifest_file=evidence.manifest_file,
                    passphrase_file=self.passphrase_file,
                    report_file=report_path,
                    keep=False,
                    drill_environment=False,
                    recovery_crypto_generation_id_file=(
                        config.recovery_crypto_generation_id_file
                    ),
                    backup_passphrase_generation_id_file=(
                        config.backup_passphrase_generation_id_file
                    ),
                    max_restore_seconds=restore_budget,
                )
            )
            self._remaining(deadline)
            if not result.within_restore_budget:
                raise RuntimeError("restore drill exceeded engineering budget")
            report, report_sha256 = self._verify_restore_report(
                report_path,
                evidence,
                result,
                restore_budget,
                deadline,
            )
            finished_at = self._now()
            self._remaining(deadline)
            data_gap_seconds = max(
                0, int((finished_at - evidence.created_at).total_seconds())
            )
            restore_evidence = {
                "snapshot_id": selected,
                "finished_at": finished_at.isoformat(),
                "restore_seconds": result.restore_seconds,
                "drill_budget_seconds": config.max_restore_seconds,
                "restore_step_budget_seconds": report["restore_budget_seconds"],
                "within_drill_budget": True,
                "data_gap_seconds": data_gap_seconds,
                "report_sha256": report_sha256,
                "recovery_crypto_generation_id": (
                    evidence.recovery_crypto_generation_id
                ),
                "backup_passphrase_generation_id": (
                    evidence.backup_passphrase_generation_id
                ),
            }
            with self._lock(config, deadline):
                state = self._load_state(config)
                snapshot_state = state["snapshots"].get(selected)
                if (
                    not isinstance(snapshot_state, dict)
                    or snapshot_state.get("integrity_verified") is not True
                    or not self._evidence_matches_state(evidence, snapshot_state)
                ):
                    raise ValueError("pinned snapshot state changed during restore")
                current = self._verify_snapshot(config, selected, deadline)
                final_generation_ids = self._read_host_generation_ids(
                    config,
                    deadline,
                )
                if (
                    not self._same_evidence(current, evidence)
                    or not self._generation_ids_match(evidence, final_generation_ids)
                    or not all(
                        secrets.compare_digest(before, after)
                        for before, after in zip(
                            generation_ids,
                            final_generation_ids,
                            strict=True,
                        )
                    )
                    or sha256_file(
                        report_path,
                        deadline=deadline,
                        timer=self.timer,
                    )
                    != restore_evidence["report_sha256"]
                ):
                    raise ValueError("pinned restore evidence changed before commit")
                snapshot_state["restore_verified"] = True
                snapshot_state["available"] = True
                snapshot_state["last_restore"] = restore_evidence
                state["last_successful_restore"] = restore_evidence
                state["last_failure"] = None
                self._remaining(deadline)
                self._save_state(config, state)
                self._remaining(deadline)
            self._remaining(deadline)
            return restore_evidence
        except Exception as error:
            if report_path is not None and (
                report_path.exists() or report_path.is_symlink()
            ):
                report_path.unlink(missing_ok=True)
            # 恢复在锁外运行；失败记录必须重新取得账本锁，避免覆盖并发备份。
            if selection_completed or selected is not None:
                with self._lock(config):
                    self._failure(
                        config,
                        "drill",
                        type(error).__name__,
                        snapshot_id=selected,
                        invalidate_integrity=selected is not None and not verified,
                    )
            raise

    def status(self, config: LifecycleConfig) -> LifecycleStatus:
        """验证预生产隔离恢复证据；该状态不得作为生产同机可恢复声明。"""

        with self._lock(config):
            state = self._load_state(config)
            now = self._now()
            backup = state["last_successful_backup"]
            restore = state["last_successful_restore"]
            backup_age: int | None = None
            snapshot_age: int | None = None
            restore_age: int | None = None
            if isinstance(backup, dict):
                backup_age = max(
                    0,
                    int(
                        (
                            now
                            - _aware_datetime(
                                backup.get("completed_at"), "backup completion time"
                            )
                        ).total_seconds()
                    ),
                )
                snapshot_age = max(
                    0,
                    int(
                        (
                            now
                            - _aware_datetime(
                                backup.get("created_at"), "backup creation time"
                            )
                        ).total_seconds()
                    ),
                )
            if isinstance(restore, dict):
                restore_age = max(
                    0,
                    int(
                        (
                            now
                            - _aware_datetime(
                                restore.get("finished_at"), "restore completion time"
                            )
                        ).total_seconds()
                    ),
                )
            backup_fresh = (
                backup_age is not None
                and backup_age <= config.max_backup_age_hours * 3600
                and snapshot_age is not None
                and snapshot_age <= config.max_backup_age_hours * 3600
            )
            usable: list[tuple[datetime, str, int]] = []
            integrity_failed = False
            last_backup_id = (
                backup.get("snapshot_id") if isinstance(backup, dict) else None
            )
            for snapshot_id, item in state["snapshots"].items():
                item_created = _aware_datetime(
                    item.get("created_at"), "snapshot state creation time"
                )
                item_age = max(0, int((now - item_created).total_seconds()))
                item_restore = item.get("last_restore")
                item_restore_age: int | None = None
                if isinstance(item_restore, dict):
                    item_restore_age = max(
                        0,
                        int(
                            (
                                now
                                - _aware_datetime(
                                    item_restore.get("finished_at"),
                                    "snapshot restore completion time",
                                )
                            ).total_seconds()
                        ),
                    )
                is_available = (
                    item.get("integrity_verified") is True
                    and item.get("restore_verified") is True
                    and item.get("available") is True
                    and isinstance(item_restore, dict)
                    and item_restore.get("snapshot_id") == snapshot_id
                    and item_age <= config.max_backup_age_hours * 3600
                    and item_restore_age is not None
                    and item_restore_age <= config.max_restore_age_hours * 3600
                )
                if item.get("integrity_verified") is not True or (
                    snapshot_id != last_backup_id and not is_available
                ):
                    continue
                try:
                    current = self._verify_snapshot(config, snapshot_id)
                    if not self._evidence_matches_state(current, item):
                        raise ValueError("snapshot digest changed after verification")
                except (OSError, UnicodeError, ValueError):
                    item["integrity_verified"] = False
                    item["restore_verified"] = False
                    item["available"] = False
                    integrity_failed = True
                    if snapshot_id == last_backup_id:
                        backup_fresh = False
                else:
                    if is_available:
                        usable.append((item_created, snapshot_id, item_age))
            usable.sort(reverse=True)
            healthy = backup_fresh and bool(usable)
            failure_type = (
                None
                if healthy
                else (
                    "SnapshotIntegrityFailed"
                    if integrity_failed
                    else "EvidenceStale"
                )
            )
            evidence: dict[str, Any] = {
                "schema_version": 1,
                "evidence_scope": "preproduction_restore",
                "status": "healthy" if healthy else "stale",
                "checked_at": now.isoformat(),
                "last_successful_backup": backup,
                "last_successful_restore": restore,
                "backup_age_seconds": backup_age,
                "snapshot_age_seconds": snapshot_age,
                "restore_age_seconds": restore_age,
                "max_backup_age_seconds": config.max_backup_age_hours * 3600,
                "max_restore_age_seconds": config.max_restore_age_hours * 3600,
                "usable_snapshot_count": len(usable),
                "recovery_point_snapshot_id": usable[0][1] if usable else None,
                "recovery_point_age_seconds": usable[0][2] if usable else None,
                "retention": state["retention"],
                "failure_type": failure_type,
            }
            if not healthy:
                state["last_failure"] = {
                    "at": now.isoformat(),
                    "operation": "status",
                    "error_type": failure_type,
                }
                self._save_state(config, state)
            return LifecycleStatus(evidence=evidence, healthy=healthy)

    def backup_status(self, config: LifecycleConfig) -> LifecycleStatus:
        """只验证生产最新备份的完整性与 RPO，不采信任何同机恢复证据。"""

        with self._lock(config):
            state = self._load_state(config)
            now = self._now()
            backup = state["last_successful_backup"]
            backup_age: int | None = None
            snapshot_age: int | None = None
            snapshot_id: str | None = None
            integrity_verified = False
            integrity_failed = False
            if isinstance(backup, dict):
                candidate = backup.get("snapshot_id")
                if isinstance(candidate, str) and SNAPSHOT_ID.fullmatch(candidate):
                    snapshot_id = candidate
                backup_age = max(
                    0,
                    int(
                        (
                            now
                            - _aware_datetime(
                                backup.get("completed_at"), "backup completion time"
                            )
                        ).total_seconds()
                    ),
                )
                snapshot_age = max(
                    0,
                    int(
                        (
                            now
                            - _aware_datetime(
                                backup.get("created_at"), "backup creation time"
                            )
                        ).total_seconds()
                    ),
                )

            snapshot_state = (
                state["snapshots"].get(snapshot_id)
                if snapshot_id is not None
                else None
            )
            if isinstance(snapshot_state, dict):
                if snapshot_id is None:
                    raise RuntimeError("snapshot state exists without an id")
                try:
                    if snapshot_state.get("integrity_verified") is not True:
                        raise ValueError("latest snapshot has no integrity evidence")
                    current = self._verify_snapshot(config, snapshot_id)
                    if not self._evidence_matches_state(current, snapshot_state):
                        raise ValueError("latest snapshot digest changed after verification")
                except (OSError, UnicodeError, ValueError):
                    snapshot_state["integrity_verified"] = False
                    snapshot_state["restore_verified"] = False
                    snapshot_state["available"] = False
                    integrity_failed = True
                else:
                    integrity_verified = True

            backup_fresh = (
                backup_age is not None
                and backup_age <= config.max_backup_age_hours * 3600
                and snapshot_age is not None
                and snapshot_age <= config.max_backup_age_hours * 3600
            )
            healthy = backup_fresh and integrity_verified
            failure_type = (
                None
                if healthy
                else (
                    "SnapshotIntegrityFailed"
                    if integrity_failed
                    or (snapshot_id is not None and not integrity_verified)
                    else "EvidenceStale"
                )
            )
            evidence: dict[str, Any] = {
                "schema_version": 1,
                "evidence_scope": "production_latest_backup",
                "status": "healthy" if healthy else "stale",
                "checked_at": now.isoformat(),
                "last_successful_backup": backup,
                "snapshot_id": snapshot_id,
                "backup_age_seconds": backup_age,
                "snapshot_age_seconds": snapshot_age,
                "max_backup_age_seconds": config.max_backup_age_hours * 3600,
                "integrity_verified": integrity_verified,
                "restore_evidence_counted": False,
                "failure_type": failure_type,
            }
            if not healthy:
                state["last_failure"] = {
                    "at": now.isoformat(),
                    "operation": "backup-status",
                    "error_type": failure_type,
                }
                self._save_state(config, state)
            return LifecycleStatus(evidence=evidence, healthy=healthy)


def _runtime_config() -> tuple[LifecycleConfig, Path]:
    config_value = os.environ.get("SMS_LIFECYCLE_CONFIG", "")
    passphrase_value = os.environ.get("BACKUP_PASSPHRASE_FILE", "")
    if not config_value or not passphrase_value:
        raise ValueError("lifecycle config and passphrase file paths are required")
    config = load_config(Path(config_value))
    if config.output_root != PRODUCTION_BACKUP_ROOT:
        raise ValueError("production backup output root is fixed")
    return config, Path(passphrase_value)


def _alert(operation: str, error: BaseException) -> None:
    print(
        json.dumps(
            {
                "event": "lifecycle_alert",
                "operation": operation,
                "status": "failed",
                "error_type": type(error).__name__,
            },
            sort_keys=True,
        ),
        file=sys.stderr,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation", choices=("backup", "drill", "status", "backup-status")
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    try:
        config, passphrase = _runtime_config()
        service = LifecycleService(root, passphrase)
        if args.operation == "backup":
            evidence = service.backup(config)
            output: Mapping[str, Any] = {
                "status": "success",
                "snapshot_id": evidence.snapshot_id,
                "integrity_verified": True,
                "available": False,
            }
            exit_code = 0
        elif args.operation == "drill":
            output = service.drill(config)
            exit_code = 0
        elif args.operation == "status":
            result = service.status(config)
            output = dict(result.evidence)
            if not result.healthy:
                output.update(
                    {
                        "event": "lifecycle_alert",
                        "operation": "status",
                        "error_type": output["failure_type"],
                    }
                )
            exit_code = 0 if result.healthy else 2
        else:
            result = service.backup_status(config)
            output = dict(result.evidence)
            if not result.healthy:
                output.update(
                    {
                        "event": "lifecycle_alert",
                        "operation": "backup-status",
                        "error_type": output["failure_type"],
                    }
                )
            exit_code = 0 if result.healthy else 2
        print(json.dumps(output, sort_keys=True))
        return exit_code
    except Exception as error:
        _alert(args.operation, error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
