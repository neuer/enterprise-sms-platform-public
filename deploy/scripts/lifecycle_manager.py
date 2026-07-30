#!/usr/bin/env python3
"""自动生成加密备份、随机恢复演练并维护无 PII 的恢复证据账本。"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import secrets
import shutil
import stat
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from failover_common import CommandRunner, atomic_write_json, sha256_file
from restore_drill import RestoreConfig, RestoreResult, RestoreService
from sync_standby import SyncConfig, SyncResult, SyncService

SNAPSHOT_ID = re.compile(r"^\d{8}T\d{6}Z_[0-9a-f]{12}$")
SAFE_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_FILES = frozenset({"database", "repository_archive", "environment"})
CONFIG_FIELDS = frozenset(
    {
        "schema_version",
        "environment_file",
        "output_root",
        "database",
        "retention_days",
        "minimum_snapshots",
        "max_backup_age_hours",
        "max_restore_age_hours",
        "max_restore_seconds",
    }
)


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
    max_backup_age_hours: int
    max_restore_age_hours: int
    max_restore_seconds: float


@dataclass(frozen=True, slots=True)
class SnapshotEvidence:
    snapshot_id: str
    created_at: datetime
    backup_file: Path
    manifest_file: Path
    manifest_sha256: str
    database_sha256: str


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
    database = value.get("database")
    if not isinstance(database, str) or re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", database) is None:
        raise ValueError("invalid lifecycle database")
    max_restore_seconds = value.get("max_restore_seconds")
    if (
        isinstance(max_restore_seconds, bool)
        or not isinstance(max_restore_seconds, (int, float))
        or not 60 <= float(max_restore_seconds) <= 86400
    ):
        raise ValueError("invalid max restore seconds")
    return LifecycleConfig(
        environment_file=_absolute_path(value["environment_file"], "environment file"),
        output_root=_absolute_path(value["output_root"], "output root"),
        database=database,
        retention_days=_bounded_int(value["retention_days"], "retention days", 7, 365),
        minimum_snapshots=_bounded_int(
            value["minimum_snapshots"], "minimum snapshots", 2, 30
        ),
        max_backup_age_hours=_bounded_int(
            value["max_backup_age_hours"], "max backup age", 1, 168
        ),
        max_restore_age_hours=_bounded_int(
            value["max_restore_age_hours"], "max restore age", 1, 744
        ),
        max_restore_seconds=float(max_restore_seconds),
    )


class LifecycleService:
    """以原子状态账本证明备份完整性、RPO 与 RTO，失败时保持 fail closed。"""

    def __init__(
        self,
        repository_root: Path,
        passphrase_file: Path,
        *,
        runner: CommandRunner | None = None,
        sync_service: SyncRunner | None = None,
        restore_service: DrillRunner | None = None,
        clock: Callable[[], datetime] = utc_now,
        chooser: Callable[[Sequence[str]], str] = secrets.choice,
    ) -> None:
        self.repository_root = repository_root
        self.passphrase_file = passphrase_file
        command_runner = runner or CommandRunner()
        self.sync_service = sync_service or SyncService(command_runner)
        self.restore_service = restore_service or RestoreService(command_runner)
        self.clock = clock
        self.chooser = chooser

    def _now(self) -> datetime:
        moment = self.clock()
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("lifecycle clock must be timezone-aware")
        return moment.astimezone(UTC)

    @staticmethod
    def _state_path(config: LifecycleConfig) -> Path:
        return config.output_root / "lifecycle-state.json"

    @contextmanager
    def _lock(self, config: LifecycleConfig) -> Iterator[None]:
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
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("lifecycle operation already running") from error
            else:
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
    ) -> SnapshotEvidence:
        snapshot = self._snapshot_path(config, snapshot_id)
        manifest_path = snapshot / "manifest.json"
        if (
            manifest_path.is_symlink()
            or not manifest_path.is_file()
            or stat.S_IMODE(manifest_path.stat().st_mode) != 0o600
        ):
            raise ValueError("snapshot manifest is unsafe")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError("invalid snapshot manifest") from error
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != 1
            or manifest.get("snapshot_id") != snapshot_id
            or manifest.get("secrets_included") is not False
        ):
            raise ValueError("invalid snapshot manifest")
        created_at = _aware_datetime(manifest.get("created_at"), "snapshot creation time")
        if created_at > self._now() + timedelta(minutes=5):
            raise ValueError("snapshot creation time is in the future")
        files = manifest.get("files")
        if not isinstance(files, dict) or set(files) != REQUIRED_FILES:
            raise ValueError("snapshot file inventory is incomplete")
        verified: dict[str, Path] = {}
        expected_checksum_lines: set[str] = set()
        for label, item in files.items():
            if not isinstance(item, dict) or set(item) != {"name", "sha256", "size"}:
                raise ValueError("invalid snapshot file metadata")
            name = item.get("name")
            digest = item.get("sha256")
            size = item.get("size")
            if (
                not isinstance(name, str)
                or SAFE_FILE.fullmatch(name) is None
                or not isinstance(digest, str)
                or SHA256.fullmatch(digest) is None
                or isinstance(size, bool)
                or not isinstance(size, int)
                or size < 1
            ):
                raise ValueError("invalid snapshot file metadata")
            path = snapshot / name
            if (
                path.is_symlink()
                or not path.is_file()
                or stat.S_IMODE(path.stat().st_mode) != 0o600
                or path.stat().st_size != size
                or sha256_file(path) != digest
            ):
                raise ValueError("snapshot file integrity check failed")
            verified[label] = path
            expected_checksum_lines.add(f"{digest}  {name}")
        expected_checksum_lines.add(
            f"{sha256_file(manifest_path)}  {manifest_path.name}"
        )
        checksums = snapshot / "SHA256SUMS"
        if (
            checksums.is_symlink()
            or not checksums.is_file()
            or stat.S_IMODE(checksums.stat().st_mode) != 0o600
        ):
            raise ValueError("snapshot checksum file is unsafe")
        actual_lines = set(checksums.read_text(encoding="ascii").splitlines())
        if actual_lines != expected_checksum_lines:
            raise ValueError("snapshot checksum inventory does not match")
        return SnapshotEvidence(
            snapshot_id=snapshot_id,
            created_at=created_at,
            backup_file=verified["database"],
            manifest_file=manifest_path,
            manifest_sha256=sha256_file(manifest_path),
            database_sha256=str(files["database"]["sha256"]),
        )

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
        with self._lock(config):
            try:
                result = self.sync_service.run(
                    SyncConfig(
                        repository_root=self.repository_root,
                        compose_file=self.repository_root / "deploy/docker-compose.yml",
                        environment_file=config.environment_file,
                        output_dir=config.output_root,
                        passphrase_file=self.passphrase_file,
                        database=config.database,
                        build_only=True,
                    )
                )
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
                    }
                )
                completed_at = self._now()
                state["last_successful_backup"] = {
                    "snapshot_id": result.snapshot_id,
                    "completed_at": completed_at.isoformat(),
                    "created_at": evidence.created_at.isoformat(),
                    "integrity_verified": True,
                }
                state["last_failure"] = None
                self._prune(config, state, completed_at)
                self._save_state(config, state)
                return evidence
            except Exception as error:
                self._failure(config, "backup", type(error).__name__)
                raise

    def drill(self, config: LifecycleConfig) -> Mapping[str, Any]:
        selected: str | None = None
        verified = False
        with self._lock(config):
            try:
                state = self._load_state(config)
                candidates = sorted(
                    snapshot_id
                    for snapshot_id, item in state["snapshots"].items()
                    if item.get("integrity_verified") is True
                )
                if not candidates:
                    raise ValueError("no integrity-verified snapshot is available")
                selected = self.chooser(candidates)
                if selected not in candidates:
                    raise ValueError("snapshot chooser returned an invalid candidate")
                evidence = self._verify_snapshot(config, selected)
                verified = True
                report_dir = config.output_root / "reports"
                report_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                if report_dir.is_symlink() or not report_dir.is_dir():
                    raise ValueError("restore report directory is unsafe")
                report_dir.chmod(0o700)
                report_name = f"{selected}_{self._now():%Y%m%dT%H%M%SZ}.json"
                report_path = report_dir / report_name
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
                        max_restore_seconds=config.max_restore_seconds,
                    )
                )
                if not result.within_rto:
                    raise RuntimeError("restore drill exceeded RTO")
                if (
                    report_path.is_symlink()
                    or not report_path.is_file()
                    or stat.S_IMODE(report_path.stat().st_mode) != 0o600
                ):
                    raise ValueError("restore report is unsafe")
                finished_at = self._now()
                data_gap_seconds = max(
                    0, int((finished_at - evidence.created_at).total_seconds())
                )
                restore_evidence = {
                    "snapshot_id": selected,
                    "finished_at": finished_at.isoformat(),
                    "restore_seconds": result.restore_seconds,
                    "rto_limit_seconds": config.max_restore_seconds,
                    "within_rto": True,
                    "data_gap_seconds": data_gap_seconds,
                    "report_sha256": sha256_file(report_path),
                }
                state = self._load_state(config)
                snapshot_state = state["snapshots"][selected]
                snapshot_state["restore_verified"] = True
                snapshot_state["available"] = True
                snapshot_state["last_restore"] = restore_evidence
                state["last_successful_restore"] = restore_evidence
                state["last_failure"] = None
                self._save_state(config, state)
                return restore_evidence
            except Exception as error:
                self._failure(
                    config,
                    "drill",
                    type(error).__name__,
                    snapshot_id=selected,
                    invalidate_integrity=selected is not None and not verified,
                )
                raise

    def status(self, config: LifecycleConfig) -> LifecycleStatus:
        with self._lock(config):
            state = self._load_state(config)
            now = self._now()
            backup = state["last_successful_backup"]
            restore = state["last_successful_restore"]
            backup_age: int | None = None
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
            )
            restore_fresh = (
                restore_age is not None
                and restore_age <= config.max_restore_age_hours * 3600
            )
            usable: list[str] = []
            integrity_failed = False
            last_backup_id = (
                backup.get("snapshot_id") if isinstance(backup, dict) else None
            )
            for snapshot_id, item in state["snapshots"].items():
                is_available = (
                    item.get("restore_verified") is True
                    and item.get("available") is True
                )
                if item.get("integrity_verified") is not True or (
                    snapshot_id != last_backup_id and not is_available
                ):
                    continue
                try:
                    current = self._verify_snapshot(config, snapshot_id)
                    if (
                        current.manifest_sha256 != item.get("manifest_sha256")
                        or current.database_sha256 != item.get("database_sha256")
                    ):
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
                        usable.append(snapshot_id)
            usable.sort()
            healthy = backup_fresh and restore_fresh and bool(usable)
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
                "status": "healthy" if healthy else "stale",
                "checked_at": now.isoformat(),
                "last_successful_backup": backup,
                "last_successful_restore": restore,
                "backup_age_seconds": backup_age,
                "restore_age_seconds": restore_age,
                "max_backup_age_seconds": config.max_backup_age_hours * 3600,
                "max_restore_age_seconds": config.max_restore_age_hours * 3600,
                "usable_snapshot_count": len(usable),
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


def _runtime_config() -> tuple[LifecycleConfig, Path]:
    config_value = os.environ.get("SMS_LIFECYCLE_CONFIG", "")
    passphrase_value = os.environ.get("BACKUP_PASSPHRASE_FILE", "")
    if not config_value or not passphrase_value:
        raise ValueError("lifecycle config and passphrase file paths are required")
    return load_config(Path(config_value)), Path(passphrase_value)


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
    parser.add_argument("operation", choices=("backup", "drill", "status"))
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
        else:
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
        print(json.dumps(output, sort_keys=True))
        return exit_code
    except Exception as error:
        _alert(args.operation, error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
