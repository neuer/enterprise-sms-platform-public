"""冷备脚本共享的安全 subprocess、文件与输入校验。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
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

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        input_bytes: bytes | None = None,
    ) -> bytes:
        argv = self._argv(command)
        result = subprocess.run(
            argv,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            input=input_bytes,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise CommandFailure(
                argv[0], result.returncode, result.stderr.decode(errors="replace")
            )
        return result.stdout

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
    ) -> None:
        """把 producer stdout 直接交给 consumer，并原子拒绝半成品。"""

        left_argv = self._argv(producer)
        right_argv = self._argv(consumer)
        output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        left_error = self._stderr_file()
        right_error = self._stderr_file()
        try:
            with os.fdopen(fd, "wb") as output:
                left = subprocess.Popen(
                    left_argv,
                    cwd=cwd,
                    env=dict(env) if env is not None else None,
                    stdout=subprocess.PIPE,
                    stderr=left_error,
                )
                assert left.stdout is not None
                right = subprocess.Popen(
                    right_argv,
                    cwd=cwd,
                    env=dict(env) if env is not None else None,
                    stdin=left.stdout,
                    stdout=output,
                    stderr=right_error,
                )
                left.stdout.close()
                right_code = right.wait()
                left_code = left.wait()
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
        except BaseException:
            output_path.unlink(missing_ok=True)
            raise
        finally:
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
    ) -> bytes:
        """把密文文件送入解密 producer，再直连恢复 consumer。"""

        left_argv = self._argv(producer)
        right_argv = self._argv(consumer)
        left_error = self._stderr_file()
        right_error = self._stderr_file()
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
                assert left.stdout is not None
                right = subprocess.Popen(
                    right_argv,
                    cwd=cwd,
                    env=dict(env) if env is not None else None,
                    stdin=left.stdout,
                    stdout=subprocess.PIPE,
                    stderr=right_error,
                )
                left.stdout.close()
                stdout = right.communicate()[0]
                right_code = right.returncode
                left_code = left.wait()
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
        finally:
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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
