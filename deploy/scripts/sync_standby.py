#!/usr/bin/env python3
"""生成并原子发布每日冷备快照；永不复制 secrets 或启动备机。"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import shlex
import shutil
import stat
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from failover_common import (
    BACKUP_PASSPHRASE_GENERATION_ID_FILE,
    RECOVERY_CRYPTO_GENERATION_ID_FILE,
    CommandRunner,
    DeadlineExceeded,
    atomic_write_json,
    durability_barrier,
    fsync_directory,
    fsync_file,
    read_root_generation_id_file,
    sha256_file,
    validate_generation_id,
    validate_passphrase_file,
    validate_remote,
)

DATABASE_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
SECRET_KEY_PATTERN = re.compile(r"(?i)(password|secret|token|api_?key)")
PRODUCTION_FLAGS = {
    "ENVIRONMENT": "production",
    "DEBUG": "0",
    "AUTH_MOCK": "0",
    "VENDOR_MOCK": "0",
}
BACKUP_CAPACITY_PERCENT = 70
BACKUP_SIZE_NUMERATOR = 3
BACKUP_SIZE_DENOMINATOR = 2
REMOTE_INCOMING_TTL_MINUTES = 24 * 60
REMOTE_ENV_BLOCKLIST = frozenset(
    {"RSYNC_RSH", "GIT_SSH_COMMAND", "RSYNC_CONNECT_PROG", "RSYNC_PROXY"}
)


class Runner(Protocol):
    def run(
        self,
        command: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        input_bytes: bytes | None = None,
        timeout: float | None = None,
    ) -> bytes: ...

    def pipeline_to_file(
        self,
        producer: list[str],
        consumer: list[str],
        output_path: Path,
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class StandbyTarget:
    host: str
    user: str
    root: str
    port: int = 22

    def validated(self) -> StandbyTarget:
        host, user, root = validate_remote(self.host, self.user, self.root)
        if not 1 <= self.port <= 65535:
            raise ValueError("invalid standby SSH port")
        return StandbyTarget(host, user, root, self.port)


@dataclass(frozen=True, slots=True)
class SyncConfig:
    repository_root: Path
    compose_file: Path
    environment_file: Path
    output_dir: Path
    passphrase_file: Path
    database: str
    build_only: bool
    recovery_crypto_generation_id_file: Path = RECOVERY_CRYPTO_GENERATION_ID_FILE
    backup_passphrase_generation_id_file: Path = BACKUP_PASSPHRASE_GENERATION_ID_FILE
    max_backup_seconds: float = 14400
    target: StandbyTarget | None = None


@dataclass(frozen=True, slots=True)
class SyncResult:
    snapshot_id: str
    snapshot_dir: Path
    transferred: bool


class BackupCapacityExceeded(RuntimeError):
    """目标文件系统无法在 70% 水位内容纳保守估算快照。"""

    def __init__(self) -> None:
        super().__init__("backup capacity gate failed")


class BackupCleanupFailure(RuntimeError):
    """失败快照未能从本地 incoming 区确认清除。"""

    def __init__(self) -> None:
        super().__init__("backup staging cleanup failed")


def utc_now() -> datetime:
    return datetime.now(UTC)


def _filesystem_capacity(path: Path) -> tuple[int, int]:
    """读取将承载 output root 的文件系统总量与当前已用字节。"""

    anchor = path
    while not anchor.exists():
        parent = anchor.parent
        if parent == anchor:
            raise ValueError("backup output filesystem unavailable")
        anchor = parent
    values = os.statvfs(anchor)
    block_size = values.f_frsize
    total = values.f_blocks * block_size
    free = values.f_bfree * block_size
    if block_size <= 0 or total <= 0 or free < 0 or free > total:
        raise ValueError("invalid backup filesystem capacity")
    return total, total - free


def _read_environment_file(path: Path) -> tuple[dict[str, str], bytes]:
    """以单次、禁止跟随符号链接的读取固定生产 env 代际。"""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise ValueError("environment file must be a regular non-symlink file") from error
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("environment file must be a regular non-symlink file")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError("environment file permissions must be 0600")
        with os.fdopen(fd, "rb", closefd=False) as source:
            content = source.read()
    finally:
        os.close(fd)

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("environment file must be valid UTF-8") from error
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"invalid environment line: {line_number}")
        key, value = (part.strip() for part in line.split("=", 1))
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError(f"invalid environment key: {key}")
        if SECRET_KEY_PATTERN.search(key) and (
            not key.endswith("_FILE") or not value.startswith("/run/secrets/")
        ):
            raise ValueError(f"secret-like environment value forbidden: {key}")
        values[key] = value
    for key, expected in PRODUCTION_FLAGS.items():
        if values.get(key) != expected:
            raise ValueError(f"production environment requires {key}={expected}")
    return values, content


def validate_environment_file(path: Path) -> dict[str, str]:
    """确认生产 env 只有非秘密值，凭据键只能是 /run/secrets 文件路径。"""

    values, _ = _read_environment_file(path)
    return values


def _write_text_0600(path: Path, value: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write(value)
        output.flush()
        os.fsync(output.fileno())
    fsync_directory(path.parent)


def _write_bytes_0600(path: Path, value: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(value)
        output.flush()
        os.fsync(output.fileno())
    fsync_directory(path.parent)


class SyncService:
    """创建本地原子快照，并可在校验后发布到冷备节点。"""

    def __init__(
        self,
        runner: Runner,
        *,
        clock: Callable[[], datetime] = utc_now,
        timer: Callable[[], float] = time.monotonic,
        capacity_reader: Callable[[Path], tuple[int, int]] = _filesystem_capacity,
        generation_reader: Callable[[Path], str] = read_root_generation_id_file,
    ) -> None:
        self.runner = runner
        self.clock = clock
        self.timer = timer
        self.capacity_reader = capacity_reader
        self.generation_reader = generation_reader

    @staticmethod
    def _compose(config: SyncConfig) -> list[str]:
        return ["docker", "compose", "-f", str(config.compose_file)]

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self.timer()
        if not math.isfinite(remaining) or remaining <= 0:
            raise DeadlineExceeded
        return remaining

    def _run_command(
        self,
        command: Sequence[str],
        config: SyncConfig,
        deadline: float,
        *,
        env: dict[str, str] | None = None,
    ) -> bytes:
        try:
            output = self.runner.run(
                list(command),
                cwd=config.repository_root,
                env=env,
                timeout=self._remaining(deadline),
            )
        except BaseException:
            self._remaining(deadline)
            raise
        self._remaining(deadline)
        return output

    def _pipeline_to_file(
        self,
        producer: Sequence[str],
        consumer: Sequence[str],
        output_path: Path,
        config: SyncConfig,
        deadline: float,
    ) -> None:
        try:
            self.runner.pipeline_to_file(
                list(producer),
                list(consumer),
                output_path,
                cwd=config.repository_root,
                timeout=self._remaining(deadline),
            )
        except BaseException:
            self._remaining(deadline)
            raise
        self._remaining(deadline)

    def _sha256(self, path: Path, deadline: float) -> str:
        return sha256_file(path, deadline=deadline, timer=self.timer)

    def _assert_inputs(
        self, config: SyncConfig
    ) -> tuple[Path, StandbyTarget | None, bytes, str, str]:
        root = config.repository_root.resolve(strict=True)
        if not config.compose_file.is_file():
            raise ValueError("compose file unavailable")
        _, environment_bytes = _read_environment_file(config.environment_file)
        passphrase = validate_passphrase_file(config.passphrase_file, root)
        recovery_crypto_generation_id, backup_passphrase_generation_id = (
            self._read_generation_ids(config)
        )
        if DATABASE_PATTERN.fullmatch(config.database) is None:
            raise ValueError("invalid source database name")
        if config.build_only:
            if config.target is not None:
                raise ValueError("build-only mode must not define a standby target")
            return (
                passphrase,
                None,
                environment_bytes,
                recovery_crypto_generation_id,
                backup_passphrase_generation_id,
            )
        if config.target is None:
            raise ValueError("standby target is required")
        return (
            passphrase,
            config.target.validated(),
            environment_bytes,
            recovery_crypto_generation_id,
            backup_passphrase_generation_id,
        )

    def _read_generation_ids(self, config: SyncConfig) -> tuple[str, str]:
        if (
            config.recovery_crypto_generation_id_file
            != RECOVERY_CRYPTO_GENERATION_ID_FILE
            or config.backup_passphrase_generation_id_file
            != BACKUP_PASSPHRASE_GENERATION_ID_FILE
        ):
            raise ValueError("generation id file paths are fixed")
        recovery_crypto_generation_id = validate_generation_id(
            self.generation_reader(config.recovery_crypto_generation_id_file)
        )
        backup_passphrase_generation_id = validate_generation_id(
            self.generation_reader(config.backup_passphrase_generation_id_file)
        )
        return (
            recovery_crypto_generation_id,
            backup_passphrase_generation_id,
        )

    @staticmethod
    def _snapshot_id(moment: datetime, commit: str) -> str:
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("snapshot clock must be timezone-aware")
        return f"{moment.astimezone(UTC):%Y%m%dT%H%M%SZ}_{commit[:12]}"

    def _same_snapshot_digest(self, left: Path, right: Path) -> bool:
        left_sum = left / "SHA256SUMS"
        right_sum = right / "SHA256SUMS"
        return (
            left_sum.is_file()
            and right_sum.is_file()
            and left_sum.read_bytes() == right_sum.read_bytes()
        )

    def _publish_local(
        self,
        config: SyncConfig,
        staging: Path,
        snapshot_id: str,
        deadline: float,
    ) -> Path:
        snapshots = config.output_dir / "snapshots"
        snapshots.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = snapshots / snapshot_id
        current = config.output_dir / "current"
        if current.exists() and not current.is_symlink():
            raise ValueError("backup current pointer must be a symlink")
        previous_target = current.readlink() if current.is_symlink() else None
        next_link = config.output_dir / ".current.next"
        next_link.unlink(missing_ok=True)
        destination_created = False
        current_replaced = False
        reused = False
        try:
            self._remaining(deadline)
            if destination.exists() or destination.is_symlink():
                if not self._same_snapshot_digest(staging, destination):
                    raise ValueError(f"snapshot digest mismatch: {snapshot_id}")
                shutil.rmtree(staging, ignore_errors=True)
                reused = True
            else:
                os.replace(staging, destination)
                destination_created = True
            incoming = config.output_dir / ".incoming"
            fsync_directory(snapshots)
            if incoming.is_dir():
                fsync_directory(incoming)
            durability_barrier("snapshot_rename", destination)
            self._remaining(deadline)
            next_link.symlink_to(Path("snapshots") / snapshot_id)
            self._remaining(deadline)
            os.replace(next_link, current)
            current_replaced = True
            fsync_directory(config.output_dir)
            durability_barrier("current_switch", current)
            self._remaining(deadline)
            return destination
        except BaseException:
            next_link.unlink(missing_ok=True)
            if current_replaced:
                if previous_target is None:
                    current.unlink(missing_ok=True)
                else:
                    rollback = config.output_dir / ".current.rollback"
                    rollback.unlink(missing_ok=True)
                    rollback.symlink_to(previous_target)
                    os.replace(rollback, current)
            if destination_created and not reused:
                shutil.rmtree(destination, ignore_errors=True)
            raise

    def _ssh(self, target: StandbyTarget) -> list[str]:
        return ["ssh", "-p", str(target.port), f"{target.user}@{target.host}"]

    def _remote_env(self) -> dict[str, str]:
        """去掉可覆盖 rsync/ssh 传输通道的环境变量，只使用已校验 Target。"""

        return {
            key: value
            for key, value in os.environ.items()
            if key not in REMOTE_ENV_BLOCKLIST
        }

    def _rsync_remote_shell(self, target: StandbyTarget) -> str:
        return f"ssh -p {target.port}"

    def _run_remote(
        self,
        command: Sequence[str],
        config: SyncConfig,
        deadline: float,
    ) -> bytes:
        return self._run_command(command, config, deadline, env=self._remote_env())

    def _precheck_remote_capacity(
        self,
        target: StandbyTarget,
        config: SyncConfig,
        deadline: float,
        estimated_bytes: int,
    ) -> None:
        output = self._run_remote(
            self._ssh(target)
            + [f"set -eu; df -Pk {shlex.quote(target.root)} | tail -n 1"],
            config,
            deadline,
        ).decode("utf-8", errors="replace").split()
        if len(output) < 4 or not output[3].isdecimal():
            raise ValueError("remote backup capacity unavailable")
        available = int(output[3]) * 1024
        if available < estimated_bytes:
            raise BackupCapacityExceeded

    def _stage_remote(
        self,
        snapshot_dir: Path,
        snapshot_id: str,
        target: StandbyTarget,
        config: SyncConfig,
        deadline: float,
        estimated_bytes: int,
    ) -> None:
        endpoint = f"{target.user}@{target.host}"
        incoming_root = f"{target.root}/.incoming"
        incoming = f"{incoming_root}/{snapshot_id}"
        destination = f"{target.root}/snapshots/{snapshot_id}"
        self._run_remote(
            self._ssh(target)
            + [
                "set -eu; "
                f"mkdir -p {shlex.quote(incoming_root)} "
                f"{shlex.quote(target.root + '/snapshots')}; "
                f"find {shlex.quote(incoming_root)} -mindepth 1 -maxdepth 1 "
                f"-mmin +{REMOTE_INCOMING_TTL_MINUTES} -exec rm -rf {{}} +"
            ],
            config,
            deadline,
        )
        self._precheck_remote_capacity(target, config, deadline, estimated_bytes)
        self._run_remote(
            [
                "rsync",
                "-a",
                "-e",
                self._rsync_remote_shell(target),
                "--chmod=Du=rwx,Dgo=,Fu=rw,Fgo=",
                "--",
                f"{snapshot_dir}/",
                f"{endpoint}:{incoming}/",
            ],
            config,
            deadline,
        )
        remote = (
            "set -eu; "
            f"cd {shlex.quote(incoming)}; "
            "shasum -a 256 -c SHA256SUMS; "
            f"if [ -e {shlex.quote(destination)} ]; then "
            f"cmp -s SHA256SUMS {shlex.quote(destination + '/SHA256SUMS')} "
            "|| { echo snapshot-digest-mismatch >&2; exit 1; }; "
            f"rm -rf {shlex.quote(incoming)}; "
            "else "
            f"mv {shlex.quote(incoming)} {shlex.quote(destination)}; "
            "fi; "
            f"sync {shlex.quote(target.root + '/snapshots')}"
        )
        self._run_remote(self._ssh(target) + [remote], config, deadline)

    def _commit_remote_current(
        self,
        snapshot_id: str,
        target: StandbyTarget,
        config: SyncConfig,
        deadline: float,
    ) -> None:
        destination = f"{target.root}/snapshots/{snapshot_id}"
        remote = (
            "set -eu; "
            f"test -d {shlex.quote(destination)}; "
            f"cd {shlex.quote(target.root)}; "
            f"ln -sfn {shlex.quote('snapshots/' + snapshot_id)} current.next; "
            "mv -f current.next current; "
            "sync"
        )
        self._run_remote(self._ssh(target) + [remote], config, deadline)

    def _cleanup_remote_incoming(
        self,
        snapshot_id: str,
        target: StandbyTarget,
        config: SyncConfig,
        deadline: float,
    ) -> None:
        incoming = f"{target.root}/.incoming/{snapshot_id}"
        self._run_remote(
            self._ssh(target) + [f"set -eu; rm -rf {shlex.quote(incoming)}"],
            config,
            deadline,
        )

    def _rollback_uncommitted_remote_snapshot(
        self,
        snapshot_id: str,
        target: StandbyTarget,
        config: SyncConfig,
        deadline: float,
    ) -> None:
        destination = f"{target.root}/snapshots/{snapshot_id}"
        incoming = f"{target.root}/.incoming/{snapshot_id}"
        remote = (
            "set -eu; "
            f"cd {shlex.quote(target.root)}; "
            "cur=; "
            "if [ -L current ]; then cur=$(readlink current); fi; "
            f"if [ \"$cur\" != {shlex.quote('snapshots/' + snapshot_id)} ]; then "
            f"rm -rf {shlex.quote(destination)}; "
            "fi; "
            f"rm -rf {shlex.quote(incoming)}"
        )
        self._run_remote(self._ssh(target) + [remote], config, deadline)

    def run(self, config: SyncConfig) -> SyncResult:
        started = self.timer()
        if not math.isfinite(started):
            raise ValueError("invalid monotonic clock")
        if (
            not math.isfinite(config.max_backup_seconds)
            or not 60 <= config.max_backup_seconds <= 14400
        ):
            raise ValueError("max backup seconds must be between 60 and 14400")
        deadline = started + config.max_backup_seconds
        try:
            (
                passphrase,
                target,
                environment_bytes,
                recovery_crypto_generation_id,
                backup_passphrase_generation_id,
            ) = self._assert_inputs(config)
        except BaseException:
            self._remaining(deadline)
            raise
        self._remaining(deadline)

        compose = self._compose(config)
        database_size_text = self._run_command(
            compose
            + [
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
                config.database,
                "-Atc",
                "SELECT pg_database_size(current_database())",
            ],
            config,
            deadline,
        ).decode("utf-8", errors="strict").strip()
        if not database_size_text.isdecimal() or int(database_size_text) <= 0:
            raise ValueError("invalid source database size")
        database_size = int(database_size_text)
        try:
            total_bytes, current_used_bytes = self.capacity_reader(config.output_dir)
        except BaseException:
            self._remaining(deadline)
            raise
        self._remaining(deadline)
        if (
            total_bytes <= 0
            or current_used_bytes < 0
            or current_used_bytes > total_bytes
        ):
            raise ValueError("invalid backup filesystem capacity")
        estimated_bytes = (
            database_size * BACKUP_SIZE_NUMERATOR + BACKUP_SIZE_DENOMINATOR - 1
        ) // BACKUP_SIZE_DENOMINATOR
        capacity_limit = total_bytes * BACKUP_CAPACITY_PERCENT // 100
        if current_used_bytes + estimated_bytes > capacity_limit:
            raise BackupCapacityExceeded

        lock_fd: int | None = None
        staging: Path | None = None
        failure: BaseException | None = None
        snapshot_id: str | None = None
        remote_staged = False
        try:
            config.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._remaining(deadline)
            lock_path = config.output_dir / ".sync.lock"
            lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._remaining(deadline)
            status = self._run_command(
                ["git", "status", "--porcelain", "--untracked-files=no"],
                config,
                deadline,
            )
            if status.strip():
                raise ValueError("每日同步要求 tracked 工作树干净")
            commit = self._run_command(
                ["git", "rev-parse", "HEAD"],
                config,
                deadline,
            ).decode().strip()
            if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
                raise ValueError("invalid Git commit")
            # RPO 以 pg_dump 开始前固化的一致性点计时；不能用耗时导出完成后的
            # 时刻掩盖真实数据缺口。
            try:
                snapshot_moment = self.clock()
            except BaseException:
                self._remaining(deadline)
                raise
            self._remaining(deadline)
            snapshot_id = self._snapshot_id(snapshot_moment, commit)
            staging = config.output_dir / ".incoming" / snapshot_id
            staging.mkdir(parents=True, mode=0o700)
            self._remaining(deadline)
            environment_sha256 = hashlib.sha256(environment_bytes).hexdigest()
            self._remaining(deadline)

            alembic = self._run_command(
                compose
                + [
                    "exec",
                    "-T",
                    "postgres",
                    "psql",
                    "-U",
                    "sms_owner",
                    "-d",
                    config.database,
                    "-Atc",
                    "SELECT version_num FROM alembic_version",
                ],
                config,
                deadline,
            ).decode().strip()
            backup = staging / f"sms_{snapshot_id}.dump.enc"
            self._pipeline_to_file(
                compose
                + [
                    "exec",
                    "-T",
                    "postgres",
                    "pg_dump",
                    "-U",
                    "sms_owner",
                    "-d",
                    config.database,
                    "--format=custom",
                    "--compress=6",
                    "--no-owner",
                ],
                [
                    "openssl",
                    "enc",
                    "-aes-256-cbc",
                    "-pbkdf2",
                    "-iter",
                    "600000",
                    "-salt",
                    "-pass",
                    f"file:{passphrase}",
                ],
                backup,
                config,
                deadline,
            )
            fsync_file(backup)
            fsync_directory(staging)
            durability_barrier("dump_fsync", backup)

            archive = staging / f"repository_{snapshot_id}.tar.gz"
            self._run_command(
                [
                    "git",
                    "archive",
                    "--format=tar.gz",
                    f"--output={archive}",
                    commit,
                ],
                config,
                deadline,
            )
            if not archive.is_file():
                raise RuntimeError("git archive was not created")
            archive.chmod(0o600)
            fsync_file(archive)
            fsync_directory(staging)
            durability_barrier("archive_fsync", archive)
            self._remaining(deadline)
            environment = staging / "production.env"
            _write_bytes_0600(environment, environment_bytes)
            self._remaining(deadline)

            final_status = self._run_command(
                ["git", "status", "--porcelain", "--untracked-files=no"],
                config,
                deadline,
            )
            final_commit = self._run_command(
                ["git", "rev-parse", "HEAD"],
                config,
                deadline,
            ).decode().strip()
            final_alembic = self._run_command(
                compose
                + [
                    "exec",
                    "-T",
                    "postgres",
                    "psql",
                    "-U",
                    "sms_owner",
                    "-d",
                    config.database,
                    "-Atc",
                    "SELECT version_num FROM alembic_version",
                ],
                config,
                deadline,
            ).decode().strip()
            try:
                (
                    final_recovery_crypto_generation_id,
                    final_backup_passphrase_generation_id,
                ) = self._read_generation_ids(config)
            except BaseException:
                self._remaining(deadline)
                raise
            self._remaining(deadline)
            if (
                final_status.strip()
                or final_commit != commit
                or final_alembic != alembic
                or not secrets.compare_digest(
                    final_recovery_crypto_generation_id,
                    recovery_crypto_generation_id,
                )
                or not secrets.compare_digest(
                    final_backup_passphrase_generation_id,
                    backup_passphrase_generation_id,
                )
                or self._sha256(config.environment_file, deadline)
                != environment_sha256
                or self._sha256(environment, deadline) != environment_sha256
            ):
                raise RuntimeError("backup source generation changed during snapshot")

            files = {
                "database": {
                    "name": backup.name,
                    "sha256": self._sha256(backup, deadline),
                    "size": backup.stat().st_size,
                },
                "repository_archive": {
                    "name": archive.name,
                    "sha256": self._sha256(archive, deadline),
                    "size": archive.stat().st_size,
                },
                "environment": {
                    "name": environment.name,
                    "sha256": environment_sha256,
                    "size": environment.stat().st_size,
                },
            }
            self._remaining(deadline)
            manifest = staging / "manifest.json"
            atomic_write_json(
                manifest,
                {
                    "schema_version": 1,
                    "snapshot_id": snapshot_id,
                    "created_at": snapshot_moment.astimezone(UTC).isoformat(),
                    "git_commit": commit,
                    "alembic_version": alembic,
                    "database": config.database,
                    "secrets_included": False,
                    "recovery_crypto_generation_id": recovery_crypto_generation_id,
                    "backup_passphrase_generation_id": backup_passphrase_generation_id,
                    "files": files,
                },
            )
            self._remaining(deadline)
            checksum_lines = [
                f"{self._sha256(staging / str(item['name']), deadline)}  {item['name']}"
                for item in files.values()
            ]
            checksum_lines.append(f"{self._sha256(manifest, deadline)}  {manifest.name}")
            _write_text_0600(staging / "SHA256SUMS", "\n".join(checksum_lines) + "\n")
            fsync_directory(staging)
            durability_barrier("payload_durable", staging)
            self._remaining(deadline)

            remote_staged = False
            if target is not None:
                self._stage_remote(
                    staging,
                    snapshot_id,
                    target,
                    config,
                    deadline,
                    estimated_bytes,
                )
                remote_staged = True
            snapshot_dir = self._publish_local(config, staging, snapshot_id, deadline)
            staging = None
            if target is not None:
                self._commit_remote_current(snapshot_id, target, config, deadline)
            return SyncResult(snapshot_id, snapshot_dir, target is not None)
        except BaseException as error:
            failure = error
            raise
        finally:
            cleanup_failure: BackupCleanupFailure | None = None
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
                if staging.exists():
                    cleanup_failure = BackupCleanupFailure()
                    if failure is not None:
                        cleanup_failure.add_note(
                            "backup failed before staging cleanup also failed"
                        )
            if target is not None and snapshot_id is not None:
                try:
                    if failure is not None and remote_staged:
                        self._rollback_uncommitted_remote_snapshot(
                            snapshot_id, target, config, deadline
                        )
                    else:
                        self._cleanup_remote_incoming(
                            snapshot_id, target, config, deadline
                        )
                except BaseException:
                    if cleanup_failure is None:
                        cleanup_failure = BackupCleanupFailure()
            if lock_fd is not None:
                os.close(lock_fd)
            if cleanup_failure is not None:
                raise cleanup_failure


def _config_from_args(args: argparse.Namespace) -> SyncConfig:
    root = Path(__file__).resolve().parents[2]
    passphrase_value = os.environ.get("BACKUP_PASSPHRASE_FILE", "")
    if not passphrase_value:
        raise ValueError("BACKUP_PASSPHRASE_FILE path is required")
    target = None
    if not args.build_only:
        target = StandbyTarget(
            os.environ.get("STANDBY_HOST", ""),
            os.environ.get("STANDBY_USER", ""),
            os.environ.get("STANDBY_ROOT", ""),
            int(os.environ.get("STANDBY_SSH_PORT", "22")),
        )
    return SyncConfig(
        repository_root=root,
        compose_file=root / "deploy/docker-compose.yml",
        environment_file=Path(args.environment_file),
        output_dir=Path(args.output_dir),
        passphrase_file=Path(passphrase_value),
        database=args.database,
        build_only=args.build_only,
        max_backup_seconds=args.max_backup_seconds,
        target=target,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--environment-file", default=".env")
    parser.add_argument("--output-dir", default="var/backups/standby-sync")
    parser.add_argument("--database", default="sms")
    parser.add_argument("--max-backup-seconds", type=float, default=14400)
    args = parser.parse_args()
    try:
        result = SyncService(CommandRunner()).run(_config_from_args(args))
    except (OSError, RuntimeError, ValueError) as error:
        print(json.dumps({"status": "failed", "error": type(error).__name__}))
        return 1
    print(
        json.dumps(
            {
                "status": "success",
                "snapshot_id": result.snapshot_id,
                "snapshot_dir": str(result.snapshot_dir),
                "transferred": result.transferred,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
