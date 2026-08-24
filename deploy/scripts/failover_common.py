"""冷备脚本共享的安全 subprocess、文件与输入校验。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

PHONE_IN_TEXT = re.compile(r"(?<!\d)(1\d{10})(?!\d)")
SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|secret|token|api[_-]?key)\s*[:=]\s*[^\s,;]+"
)
HOST_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
USER_PATTERN = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
REMOTE_ROOT_PATTERN = re.compile(r"^/[A-Za-z0-9._/-]+$")
DRILL_DATABASE_PATTERN = re.compile(r"^sms_drill_[a-z0-9_]{1,48}$")
GENERATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RECOVERY_CRYPTO_GENERATION_ID_FILE = Path(
    "/etc/sms-platform/recovery-crypto-generation-id"
)
BACKUP_PASSPHRASE_GENERATION_ID_FILE = Path(
    "/etc/sms-platform/backup-secrets/generation-id"
)
SNAPSHOT_ID_PATTERN = re.compile(r"^\d{8}T\d{6}Z_[0-9a-f]{12}$")
SNAPSHOT_GIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SNAPSHOT_SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
SNAPSHOT_SAFE_FILE_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SNAPSHOT_PAYLOAD_LABELS = frozenset(
    {"database", "repository_archive", "environment"}
)
SNAPSHOT_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "snapshot_id",
        "created_at",
        "git_commit",
        "alembic_version",
        "database",
        "secrets_included",
        "recovery_crypto_generation_id",
        "backup_passphrase_generation_id",
        "files",
    }
)


@dataclass(frozen=True, slots=True)
class ValidatedSnapshotBundle:
    manifest: dict[str, Any]
    created_at: datetime
    files: Mapping[str, Path]
    manifest_sha256: str


def sanitize_error(value: str) -> str:
    """清除命令错误中的手机号与常见凭据赋值。"""

    value = PHONE_IN_TEXT.sub(
        lambda match: f"{match.group(1)[:3]}****{match.group(1)[-4:]}", value
    )
    return SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}=<redacted>", value
    )[:1024]


class CommandFailure(RuntimeError):
    """只公开可控命令名、退出码与脱敏 stderr。"""

    def __init__(self, executable: str, returncode: int, stderr: str) -> None:
        super().__init__(
            f"command failed: {Path(executable).name} rc={returncode}: "
            f"{sanitize_error(stderr.strip())}"
        )
        self.returncode = returncode


class CommandTimeout(TimeoutError):
    """命令超过调用方预算；错误不得包含 argv、输出或凭据。"""

    def __init__(self, executable: str) -> None:
        super().__init__(f"command timed out: {Path(executable).name}")


class DeadlineExceeded(TimeoutError):
    """整个操作的绝对截止时间已耗尽。"""

    def __init__(self) -> None:
        super().__init__("operation deadline exceeded")


class CommandRunner:
    """所有本地命令均以 argv 执行，禁止 shell 展开。"""

    @staticmethod
    def _argv(command: Sequence[str]) -> list[str]:
        if isinstance(command, (str, bytes)) or not command:
            raise ValueError("command must be a non-empty argv sequence")
        values = [str(item) for item in command]
        if any(not item for item in values):
            raise ValueError("command arguments must be non-empty")
        return values

    @staticmethod
    def _timeout(value: float | None) -> float | None:
        if value is None:
            return None
        if not math.isfinite(value) or value <= 0:
            raise ValueError("timeout must be a positive finite value")
        return value

    @staticmethod
    def _remaining_timeout(started: float, timeout: float | None) -> float | None:
        if timeout is None:
            return None
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise subprocess.TimeoutExpired("command", timeout)
        return remaining

    @staticmethod
    def _terminate_and_wait(processes: Sequence[subprocess.Popen[bytes]]) -> None:
        """先 TERM、再 KILL，并对每个子进程执行有界 wait。"""

        active = [process for process in processes if process.poll() is None]
        for process in active:
            with suppress(ProcessLookupError):
                process.terminate()
        for process in active:
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    process.kill()
        for process in active:
            # 不允许清理路径无限等待；后续父进程退出仍会由 init 回收。
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=1.0)

    @staticmethod
    def _close_process_pipes(processes: Sequence[subprocess.Popen[bytes]]) -> None:
        for process in processes:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        input_bytes: bytes | None = None,
        timeout: float | None = None,
    ) -> bytes:
        argv = self._argv(command)
        bounded_timeout = self._timeout(timeout)
        started = time.monotonic()
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            stdin=subprocess.PIPE if input_bytes is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            stdout, stderr = process.communicate(
                input=input_bytes,
                timeout=self._remaining_timeout(started, bounded_timeout),
            )
        except subprocess.TimeoutExpired:
            self._terminate_and_wait((process,))
            raise CommandTimeout(argv[0]) from None
        except BaseException:
            self._terminate_and_wait((process,))
            raise
        finally:
            self._close_process_pipes((process,))
        if process.returncode != 0:
            raise CommandFailure(
                argv[0], process.returncode, stderr.decode(errors="replace")
            )
        return stdout

    @staticmethod
    def _stderr_file() -> Any:
        return tempfile.TemporaryFile(mode="w+b")

    def pipeline_to_file(
        self,
        producer: Sequence[str],
        consumer: Sequence[str],
        output_path: Path,
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> None:
        """把 producer stdout 直接交给 consumer，并原子拒绝半成品。"""

        left_argv = self._argv(producer)
        right_argv = self._argv(consumer)
        bounded_timeout = self._timeout(timeout)
        started = time.monotonic()
        output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        left_error = self._stderr_file()
        right_error = self._stderr_file()
        processes: list[subprocess.Popen[bytes]] = []
        try:
            with os.fdopen(fd, "wb") as output:
                left = subprocess.Popen(
                    left_argv,
                    cwd=cwd,
                    env=dict(env) if env is not None else None,
                    stdout=subprocess.PIPE,
                    stderr=left_error,
                )
                processes.append(left)
                assert left.stdout is not None
                right = subprocess.Popen(
                    right_argv,
                    cwd=cwd,
                    env=dict(env) if env is not None else None,
                    stdin=left.stdout,
                    stdout=output,
                    stderr=right_error,
                )
                processes.append(right)
                left.stdout.close()
                right_code = right.wait(
                    timeout=self._remaining_timeout(started, bounded_timeout)
                )
                left_code = left.wait(
                    timeout=self._remaining_timeout(started, bounded_timeout)
                )
            left_error.seek(0)
            right_error.seek(0)
            if left_code != 0:
                raise CommandFailure(
                    left_argv[0], left_code, left_error.read().decode(errors="replace")
                )
            if right_code != 0:
                raise CommandFailure(
                    right_argv[0], right_code, right_error.read().decode(errors="replace")
                )
        except subprocess.TimeoutExpired:
            self._terminate_and_wait(processes)
            output_path.unlink(missing_ok=True)
            raise CommandTimeout("pipeline") from None
        except BaseException:
            self._terminate_and_wait(processes)
            output_path.unlink(missing_ok=True)
            raise
        finally:
            self._close_process_pipes(processes)
            left_error.close()
            right_error.close()

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
        """把密文文件送入解密 producer，再直连恢复 consumer。"""

        left_argv = self._argv(producer)
        right_argv = self._argv(consumer)
        bounded_timeout = self._timeout(timeout)
        started = time.monotonic()
        left_error = self._stderr_file()
        right_error = self._stderr_file()
        processes: list[subprocess.Popen[bytes]] = []
        try:
            with input_path.open("rb") as source:
                left = subprocess.Popen(
                    left_argv,
                    cwd=cwd,
                    env=dict(env) if env is not None else None,
                    stdin=source,
                    stdout=subprocess.PIPE,
                    stderr=left_error,
                )
                processes.append(left)
                assert left.stdout is not None
                right = subprocess.Popen(
                    right_argv,
                    cwd=cwd,
                    env=dict(env) if env is not None else None,
                    stdin=left.stdout,
                    stdout=subprocess.PIPE,
                    stderr=right_error,
                )
                processes.append(right)
                left.stdout.close()
                stdout = right.communicate(
                    timeout=self._remaining_timeout(started, bounded_timeout)
                )[0]
                right_code = right.returncode
                left_code = left.wait(
                    timeout=self._remaining_timeout(started, bounded_timeout)
                )
            left_error.seek(0)
            right_error.seek(0)
            if left_code != 0:
                raise CommandFailure(
                    left_argv[0], left_code, left_error.read().decode(errors="replace")
                )
            if right_code != 0:
                raise CommandFailure(
                    right_argv[0], right_code, right_error.read().decode(errors="replace")
                )
            return stdout
        except subprocess.TimeoutExpired:
            self._terminate_and_wait(processes)
            raise CommandTimeout("pipeline") from None
        except BaseException:
            self._terminate_and_wait(processes)
            raise
        finally:
            self._close_process_pipes(processes)
            left_error.close()
            right_error.close()


def validate_passphrase_file(path: Path, repository_root: Path) -> Path:
    """口令文件必须在仓库外、非链接、普通文件且权限恰为 0600。"""

    if path.is_symlink():
        raise ValueError("备份口令文件不得是符号链接")
    resolved = path.resolve(strict=True)
    root = repository_root.resolve(strict=True)
    if resolved.is_relative_to(root):
        raise ValueError("备份口令文件必须位于仓库外")
    if not resolved.is_file():
        raise ValueError("备份口令路径必须是普通文件")
    if stat.S_IMODE(resolved.stat().st_mode) != 0o600:
        raise ValueError("备份口令文件权限必须为 0600")
    return resolved


def validate_remote(host: str, user: str, root: str) -> tuple[str, str, str]:
    """远端标识只允许无 shell 元字符的 DNS/IPv4、用户与绝对路径。"""

    if HOST_PATTERN.fullmatch(host) is None:
        raise ValueError("invalid standby host")
    if USER_PATTERN.fullmatch(user) is None:
        raise ValueError("invalid standby user")
    if REMOTE_ROOT_PATTERN.fullmatch(root) is None or ".." in Path(root).parts:
        raise ValueError("invalid standby root")
    return host, user, root.rstrip("/")


def validate_drill_database(value: str) -> str:
    """隔离演练数据库只能使用不可与生产混淆的固定前缀。"""

    if len(value) > 63 or DRILL_DATABASE_PATTERN.fullmatch(value) is None:
        raise ValueError("invalid drill database name")
    return value


def validate_generation_id(value: object) -> str:
    """非敏感代际标识只允许有界 ASCII 字符集。"""

    if not isinstance(value, str) or GENERATION_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("invalid generation id")
    return value


def read_root_generation_id_file(
    path: Path,
    *,
    expected_uid: int = 0,
    expected_gid: int = 0,
) -> str:
    """读取 root:root 0600、非链接、至多一行的非敏感代际标识。"""

    if path.is_symlink():
        raise ValueError("generation id file must not be a symlink")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise ValueError("generation id file unavailable") from error
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("generation id file must be regular")
        if metadata.st_uid != expected_uid or metadata.st_gid != expected_gid:
            raise ValueError("generation id file owner is invalid")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError("generation id file permissions must be 0600")
        content = os.read(fd, 66)
    finally:
        os.close(fd)
    if len(content) == 66:
        raise ValueError("generation id file is too large")
    try:
        value = content.decode("ascii")
    except UnicodeDecodeError as error:
        raise ValueError("generation id file must be ASCII") from error
    if value.endswith("\n"):
        value = value[:-1]
    if "\n" in value or "\r" in value:
        raise ValueError("generation id file must contain exactly one line")
    return validate_generation_id(value)


def sha256_file(
    path: Path,
    *,
    deadline: float | None = None,
    timer: Callable[[], float] = time.monotonic,
) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            if deadline is not None and timer() >= deadline:
                raise DeadlineExceeded
            digest.update(chunk)
    if deadline is not None and timer() >= deadline:
        raise DeadlineExceeded
    return digest.hexdigest()


def validate_snapshot_bundle(
    snapshot_dir: Path,
    *,
    now: datetime,
    deadline: float | None = None,
    timer: Callable[[], float] = time.monotonic,
    expected_uid: int | None = None,
    expected_gid: int | None = None,
) -> ValidatedSnapshotBundle:
    """验证快照目录、清单、三份载荷与 checksum 的完整闭集。"""

    def ensure_time() -> None:
        if deadline is not None and timer() >= deadline:
            raise DeadlineExceeded

    def owned_regular(path: Path, label: str) -> os.stat_result:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != owner_uid
            or metadata.st_gid != owner_gid
        ):
            raise ValueError(f"{label} violates snapshot file contract")
        return metadata

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("snapshot validation time must be timezone-aware")
    owner_uid = os.geteuid() if expected_uid is None else expected_uid
    owner_gid = os.getegid() if expected_gid is None else expected_gid
    ensure_time()
    directory_metadata = snapshot_dir.lstat()
    if (
        not stat.S_ISDIR(directory_metadata.st_mode)
        or stat.S_IMODE(directory_metadata.st_mode) != 0o700
        or directory_metadata.st_uid != owner_uid
        or directory_metadata.st_gid != owner_gid
        or snapshot_dir.is_symlink()
    ):
        raise ValueError("snapshot directory violates ownership contract")
    entries = {entry.name: entry for entry in os.scandir(snapshot_dir)}
    if len(entries) != 5:
        raise ValueError("snapshot directory inventory is not closed")
    ensure_time()

    manifest_path = snapshot_dir / "manifest.json"
    checksum_path = snapshot_dir / "SHA256SUMS"
    owned_regular(manifest_path, "snapshot manifest")
    owned_regular(checksum_path, "snapshot checksum file")
    try:
        manifest_text = manifest_path.read_text(encoding="utf-8")
        manifest = json.loads(
            manifest_text,
            object_pairs_hook=lambda pairs: _json_object_without_duplicates(pairs),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid snapshot manifest") from error
    ensure_time()
    if (
        not isinstance(manifest, dict)
        or set(manifest) != SNAPSHOT_MANIFEST_FIELDS
        or manifest.get("schema_version") != 1
        or manifest.get("database") != "sms"
        or manifest.get("secrets_included") is not False
    ):
        raise ValueError("invalid snapshot manifest")
    snapshot_id = manifest.get("snapshot_id")
    git_commit = manifest.get("git_commit")
    alembic_version = manifest.get("alembic_version")
    if (
        not isinstance(snapshot_id, str)
        or SNAPSHOT_ID_PATTERN.fullmatch(snapshot_id) is None
        or snapshot_dir.name != snapshot_id
        or not isinstance(git_commit, str)
        or SNAPSHOT_GIT_PATTERN.fullmatch(git_commit) is None
        or not isinstance(alembic_version, str)
        or SNAPSHOT_SAFE_ID_PATTERN.fullmatch(alembic_version) is None
    ):
        raise ValueError("invalid snapshot identity")
    raw_created_at = manifest.get("created_at")
    if (
        not isinstance(raw_created_at, str)
        or re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?\+00:00",
            raw_created_at,
        )
        is None
    ):
        raise ValueError("invalid snapshot creation time")
    try:
        created_at = datetime.fromisoformat(raw_created_at)
    except ValueError as error:
        raise ValueError("invalid snapshot creation time") from error
    if (
        created_at.tzinfo is None
        or created_at.utcoffset() is None
        or created_at > now.astimezone(UTC) + timedelta(minutes=5)
    ):
        raise ValueError("invalid snapshot creation time")
    validate_generation_id(manifest.get("recovery_crypto_generation_id"))
    validate_generation_id(manifest.get("backup_passphrase_generation_id"))

    raw_files = manifest.get("files")
    if not isinstance(raw_files, dict) or set(raw_files) != SNAPSHOT_PAYLOAD_LABELS:
        raise ValueError("snapshot file inventory is incomplete")
    files: dict[str, Path] = {}
    expected_lines: set[str] = set()
    payload_names: set[str] = set()
    for label in SNAPSHOT_PAYLOAD_LABELS:
        item = raw_files[label]
        if not isinstance(item, dict) or set(item) != {"name", "sha256", "size"}:
            raise ValueError("invalid snapshot file metadata")
        name = item.get("name")
        digest = item.get("sha256")
        size = item.get("size")
        if (
            not isinstance(name, str)
            or SNAPSHOT_SAFE_FILE_PATTERN.fullmatch(name) is None
            or name in {"manifest.json", "SHA256SUMS"}
            or name in payload_names
            or not isinstance(digest, str)
            or SHA256_PATTERN.fullmatch(digest) is None
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 1
        ):
            raise ValueError("invalid snapshot file metadata")
        payload_names.add(name)
        path = snapshot_dir / name
        metadata = owned_regular(path, "snapshot payload")
        ensure_time()
        if (
            metadata.st_size != size
            or sha256_file(path, deadline=deadline, timer=timer) != digest
        ):
            raise ValueError("snapshot file integrity check failed")
        files[label] = path
        expected_lines.add(f"{digest}  {name}")
    if set(entries) != {"manifest.json", "SHA256SUMS", *payload_names}:
        raise ValueError("snapshot directory inventory is not closed")
    manifest_digest = sha256_file(manifest_path, deadline=deadline, timer=timer)
    expected_lines.add(f"{manifest_digest}  manifest.json")
    try:
        checksum_text = checksum_path.read_text(encoding="ascii")
    except UnicodeDecodeError as error:
        raise ValueError("invalid snapshot checksum inventory") from error
    actual_lines = checksum_text.splitlines()
    if (
        not checksum_text.endswith("\n")
        or len(actual_lines) != len(expected_lines)
        or set(actual_lines) != expected_lines
    ):
        raise ValueError("snapshot checksum inventory does not match")
    ensure_time()
    return ValidatedSnapshotBundle(
        manifest=manifest,
        created_at=created_at,
        files=files,
        manifest_sha256=manifest_digest,
    )


def _json_object_without_duplicates(
    pairs: Sequence[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise json.JSONDecodeError("duplicate JSON field", key, 0)
        result[key] = value
    return result


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    """以 0600 临时文件写 JSON，fsync 后原子替换。"""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException:
        with suppress(OSError):
            os.close(fd)
        temporary.unlink(missing_ok=True)
        raise
