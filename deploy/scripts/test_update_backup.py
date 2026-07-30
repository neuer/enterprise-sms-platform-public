#!/usr/bin/env python3
"""快速更新前的 AES-GCM 数据库 checkpoint。"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from run_with_lifecycle_lock import LifecycleLockError, _verify_held_lock

CHECKPOINT_AAD = b"sms-test-update-v1"


class TestUpdateBackupError(RuntimeError):
    """checkpoint 配置、锁或加密边界不安全。"""


class Runner(Protocol):
    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        input_bytes: bytes | None = None,
    ) -> bytes: ...


@dataclass(frozen=True, slots=True)
class BackupConfig:
    output_root: Path
    key_file: Path
    database: str
    pg_dump_argv: tuple[str, ...]
    pg_restore_argv: tuple[str, ...]
    runtime_root: Path


@dataclass(frozen=True, slots=True)
class BackupResult:
    checkpoint_id: str
    ciphertext_file: Path
    manifest_file: Path
    complete: bool


def require_inherited_lifecycle_lock(
    runtime_root: Path,
    *,
    environment: Mapping[str, str] | None = None,
) -> None:
    """验证当前进程继承了 sms-compose 持有的同一生命周期锁。"""

    values = environment if environment is not None else os.environ
    if values.get("SMS_LIFECYCLE_LOCKED") != "1":
        raise TestUpdateBackupError("inherited lifecycle lock is required")
    try:
        descriptor = int(values["SMS_LIFECYCLE_LOCK_FD"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TestUpdateBackupError("inherited lifecycle lock is required") from exc
    try:
        _verify_held_lock(runtime_root, descriptor)
    except (LifecycleLockError, OSError) as exc:
        raise TestUpdateBackupError("inherited lifecycle lock is invalid") from exc


def _read_key(path: Path) -> bytes:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) not in {0o400, 0o600}
        ):
            raise TestUpdateBackupError("checkpoint key file is unsafe")
        key = path.read_bytes()
    except TestUpdateBackupError:
        raise
    except OSError as exc:
        raise TestUpdateBackupError("checkpoint key file is unsafe") from exc
    if len(key) != 32:
        raise TestUpdateBackupError("checkpoint key file is unsafe")
    return key


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise TestUpdateBackupError("checkpoint directory is unsafe")
    if metadata.st_uid != os.geteuid():
        raise TestUpdateBackupError("checkpoint directory is unsafe")
    path.chmod(0o700)


def _write_private(path: Path, payload: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


class TestUpdateBackup:
    """在持锁事务边界内生成、加密并验证数据库 checkpoint。"""

    def __init__(
        self,
        runner: Runner,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.runner = runner
        self.clock = clock

    def create(
        self,
        config: BackupConfig,
        checkpoint_id: str,
        *,
        verify_lock: bool = True,
    ) -> BackupResult:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", checkpoint_id) is None:
            raise TestUpdateBackupError("checkpoint ID is invalid")
        if verify_lock:
            require_inherited_lifecycle_lock(config.runtime_root)
        key = _read_key(config.key_file)
        _private_directory(config.output_root)
        final = config.output_root / checkpoint_id
        if final.exists() or final.is_symlink():
            raise TestUpdateBackupError("checkpoint already exists")
        staging = config.output_root / f".{checkpoint_id}.{uuid.uuid4().hex}.tmp"
        staging.mkdir(mode=0o700)
        try:
            plaintext = self.runner.run(list(config.pg_dump_argv))
            nonce = os.urandom(12)
            ciphertext = AESGCM(key).encrypt(nonce, plaintext, CHECKPOINT_AAD)
            ciphertext_file = staging / "database.dump.aesgcm"
            _write_private(ciphertext_file, ciphertext)
            verified_plaintext = AESGCM(key).decrypt(
                nonce,
                ciphertext_file.read_bytes(),
                CHECKPOINT_AAD,
            )
            self.runner.run(
                list(config.pg_restore_argv),
                input_bytes=verified_plaintext,
            )
            manifest = {
                "schema_version": 1,
                "checkpoint_id": checkpoint_id,
                "created_at": self.clock().astimezone(UTC).isoformat(),
                "database": config.database,
                "cipher": "AES-256-GCM",
                "aad": CHECKPOINT_AAD.decode("ascii"),
                "nonce": nonce.hex(),
                "ciphertext_file": ciphertext_file.name,
                "restore_readable": True,
                "complete": True,
            }
            manifest_file = staging / "manifest.json"
            _write_private(
                manifest_file,
                (json.dumps(manifest, separators=(",", ":"), sort_keys=True) + "\n").encode(),
            )
            os.replace(staging, final)
            return BackupResult(
                checkpoint_id,
                final / ciphertext_file.name,
                final / manifest_file.name,
                True,
            )
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
