#!/usr/bin/env python3
"""生成并原子发布每日冷备快照；永不复制 secrets 或启动备机。"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from failover_common import (
    CommandRunner,
    atomic_write_json,
    sha256_file,
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


class Runner(Protocol):
    def run(
        self,
        command: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        input_bytes: bytes | None = None,
    ) -> bytes: ...

    def pipeline_to_file(
        self,
        producer: list[str],
        consumer: list[str],
        output_path: Path,
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
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
    target: StandbyTarget | None = None


@dataclass(frozen=True, slots=True)
class SyncResult:
    snapshot_id: str
    snapshot_dir: Path
    transferred: bool


def utc_now() -> datetime:
    return datetime.now(UTC)


def validate_environment_file(path: Path) -> dict[str, str]:
    """确认生产 env 只有非秘密值，凭据键只能是 /run/secrets 文件路径。"""

    if path.is_symlink() or not path.is_file():
        raise ValueError("environment file must be a regular non-symlink file")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ValueError("environment file permissions must be 0600")
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
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
    return values


def _write_text_0600(path: Path, value: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write(value)
        output.flush()
        os.fsync(output.fileno())


class SyncService:
    """创建本地原子快照，并可在校验后发布到冷备节点。"""

    def __init__(self, runner: Runner, *, clock: Callable[[], datetime] = utc_now) -> None:
        self.runner = runner
        self.clock = clock

    @staticmethod
    def _compose(config: SyncConfig) -> list[str]:
        return ["docker", "compose", "-f", str(config.compose_file)]

    def _assert_inputs(self, config: SyncConfig) -> tuple[Path, StandbyTarget | None]:
        root = config.repository_root.resolve(strict=True)
        if not config.compose_file.is_file():
            raise ValueError("compose file unavailable")
        validate_environment_file(config.environment_file)
        passphrase = validate_passphrase_file(config.passphrase_file, root)
        if DATABASE_PATTERN.fullmatch(config.database) is None:
            raise ValueError("invalid source database name")
        if config.build_only:
            if config.target is not None:
                raise ValueError("build-only mode must not define a standby target")
            return passphrase, None
        if config.target is None:
            raise ValueError("standby target is required")
        return passphrase, config.target.validated()

    @staticmethod
    def _snapshot_id(moment: datetime, commit: str) -> str:
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("snapshot clock must be timezone-aware")
        return f"{moment.astimezone(UTC):%Y%m%dT%H%M%SZ}_{commit[:12]}"

    def _publish_local(self, config: SyncConfig, staging: Path, snapshot_id: str) -> Path:
        snapshots = config.output_dir / "snapshots"
        snapshots.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = snapshots / snapshot_id
        if destination.exists():
            raise FileExistsError(f"snapshot already exists: {snapshot_id}")
        os.replace(staging, destination)
        next_link = config.output_dir / ".current.next"
        next_link.unlink(missing_ok=True)
        next_link.symlink_to(Path("snapshots") / snapshot_id)
        os.replace(next_link, config.output_dir / "current")
        return destination

    def _publish_remote(
        self,
        snapshot_dir: Path,
        snapshot_id: str,
        target: StandbyTarget,
    ) -> None:
        endpoint = f"{target.user}@{target.host}"
        ssh = ["ssh", "-p", str(target.port), endpoint]
        incoming = f"{target.root}/.incoming/{snapshot_id}"
        self.runner.run(
            ssh
            + [
                "set -eu; "
                f"mkdir -p {shlex.quote(target.root + '/.incoming')} "
                f"{shlex.quote(target.root + '/snapshots')}"
            ]
        )
        self.runner.run(
            [
                "rsync",
                "-a",
                "--chmod=Du=rwx,Dgo=,Fu=rw,Fgo=",
                "--",
                f"{snapshot_dir}/",
                f"{endpoint}:{incoming}/",
            ]
        )
        remote = (
            "set -eu; "
            f"cd {shlex.quote(incoming)}; "
            "shasum -a 256 -c SHA256SUMS; "
            f"test ! -e {shlex.quote(target.root + '/snapshots/' + snapshot_id)}; "
            f"mv {shlex.quote(incoming)} "
            f"{shlex.quote(target.root + '/snapshots/' + snapshot_id)}; "
            f"cd {shlex.quote(target.root)}; "
            f"ln -sfn {shlex.quote('snapshots/' + snapshot_id)} current.next; "
            "mv -f current.next current"
        )
        self.runner.run(ssh + [remote])

    def run(self, config: SyncConfig) -> SyncResult:
        passphrase, target = self._assert_inputs(config)
        config.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = config.output_dir / ".sync.lock"
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        staging: Path | None = None
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            status = self.runner.run(
                ["git", "status", "--porcelain", "--untracked-files=no"],
                cwd=config.repository_root,
            )
            if status.strip():
                raise ValueError("每日同步要求 tracked 工作树干净")
            commit = self.runner.run(
                ["git", "rev-parse", "HEAD"], cwd=config.repository_root
            ).decode().strip()
            if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
                raise ValueError("invalid Git commit")
            snapshot_id = self._snapshot_id(self.clock(), commit)
            staging = config.output_dir / ".incoming" / snapshot_id
            staging.mkdir(parents=True, mode=0o700)

            compose = self._compose(config)
            alembic = self.runner.run(
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
                cwd=config.repository_root,
            ).decode().strip()
            backup = staging / f"sms_{snapshot_id}.dump.enc"
            self.runner.pipeline_to_file(
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
                cwd=config.repository_root,
            )

            archive = staging / f"repository_{snapshot_id}.tar.gz"
            self.runner.run(
                [
                    "git",
                    "archive",
                    "--format=tar.gz",
                    f"--output={archive}",
                    "HEAD",
                ],
                cwd=config.repository_root,
            )
            if not archive.is_file():
                raise RuntimeError("git archive was not created")
            archive.chmod(0o600)
            environment = staging / "production.env"
            shutil.copyfile(config.environment_file, environment)
            environment.chmod(0o600)

            files = {
                "database": {
                    "name": backup.name,
                    "sha256": sha256_file(backup),
                    "size": backup.stat().st_size,
                },
                "repository_archive": {
                    "name": archive.name,
                    "sha256": sha256_file(archive),
                    "size": archive.stat().st_size,
                },
                "environment": {
                    "name": environment.name,
                    "sha256": sha256_file(environment),
                    "size": environment.stat().st_size,
                },
            }
            manifest = staging / "manifest.json"
            atomic_write_json(
                manifest,
                {
                    "schema_version": 1,
                    "snapshot_id": snapshot_id,
                    "created_at": self.clock().astimezone(UTC).isoformat(),
                    "git_commit": commit,
                    "alembic_version": alembic,
                    "database": config.database,
                    "secrets_included": False,
                    "files": files,
                },
            )
            checksum_lines = [
                f"{sha256_file(staging / str(item['name']))}  {item['name']}"
                for item in files.values()
            ]
            checksum_lines.append(f"{sha256_file(manifest)}  {manifest.name}")
            _write_text_0600(staging / "SHA256SUMS", "\n".join(checksum_lines) + "\n")

            snapshot_dir = self._publish_local(config, staging, snapshot_id)
            staging = None
            if target is not None:
                self._publish_remote(snapshot_dir, snapshot_id, target)
            return SyncResult(snapshot_id, snapshot_dir, target is not None)
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
            os.close(lock_fd)


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
        target=target,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--environment-file", default=".env")
    parser.add_argument("--output-dir", default="var/backups/standby-sync")
    parser.add_argument("--database", default="sms")
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
