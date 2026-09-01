#!/usr/bin/env python3
"""vendor-live aware 快速更新 prepare 控制面。"""

from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol, cast

from check_test_update_migration import check_expand_only
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from test_secure_access_contract import HOST_CONTROL_SOURCE_PATHS
from test_secure_access_manager import require_test_host_marker
from test_update_apply import BACKEND_SERVICES, TestUpdateApply
from test_update_backup import (
    BackupConfig,
    TestUpdateBackup,
    require_inherited_lifecycle_lock,
)
from test_update_contract import (
    ChangedScope,
    TestUpdateRequest,
    classify_changed_paths,
    classify_rebaseline_paths,
    parse_test_update_request,
)
from test_update_store import TestUpdateState, TestUpdateStore
from test_update_verify import TestUpdateVerify
from vendor_test_files import (
    VendorTestFileError,
    _parse_dotenv,
    read_vendor_test_marker,
    reconcile_pure_mock_dotenv,
)

UNSAFE_CHUNK_STATUSES = ("submitting", "retrying", "uncertain")
_DATABASE = "sms"
_BACKUP_OUTPUT_ROOT = Path("/var/lib/sms-platform/test-backups")
_BACKUP_KEY_FILE = Path("/etc/sms-platform/test-update-backup-key")
_PUBLIC_CUTOVER_BOOTSTRAP_STATE = Path(
    "/var/lib/sms-platform/public-cutover-bootstrap/state.json"
)
_PUBLIC_CUTOVER_STATE_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "base_commit",
        "target_commit",
        "redis_image_id",
        "old_redis_image_id",
        "backup_dir",
        "old_secrets_dir",
        "old_runtime_target",
    }
)
_REDIS_IMAGE_ENV_KEY = "SMS_REDIS_IMAGE"
_IMAGE_ENV_KEYS = {"api": "SMS_API_IMAGE", "web": "SMS_WEB_IMAGE"}
_ACTIVATABLE_IMAGE_ENV_KEYS = frozenset(
    {*_IMAGE_ENV_KEYS.values(), _REDIS_IMAGE_ENV_KEY}
)
_TRUSTED_PROXY_DOTENV_KEYS = frozenset(
    {"SMS_EXTERNAL_TLS_MODE", "SMS_TRUSTED_PROXY_CIDRS", "SMS_TRUSTED_PROXY_CONF"}
)
_TRUSTED_PROXY_RUNTIME_CONF = "/usr/local/share/sms-platform/trusted-proxies.conf"
_BACKEND_RUNTIME_GID = 10001
_BACKUP_CONFIG_FIELDS = frozenset(
    {"schema_version", "database", "output_root", "key_file"}
)
_PUBLIC_CUTOVER_RESULT_FIELDS = frozenset(
    {
        "components",
        "cutover",
        "logical_changed",
        "migration_changed",
        "private_merge_base",
        "publication_commit",
        "risk",
        "runtime_changed",
        "source_commit",
    }
)
_SOURCE_FETCH_BACKOFF_SECONDS = (1.0, 2.0)
_REBASELINE_MIGRATION_FROM = "0053_idempotency_scope"
_REBASELINE_MIGRATION_TARGET = "0061_vendor_binding_outbox"
_REBASELINE_RESUME_MIGRATION_FROM = "0079_security_daily_publish_outbox"
_REBASELINE_RESUME_MIGRATION_TARGET = "0081_sign_adoption_contract"
_APPROVED_REBASELINE_MIGRATIONS = frozenset(
    {
        (_REBASELINE_MIGRATION_FROM, _REBASELINE_MIGRATION_TARGET),
        (_REBASELINE_RESUME_MIGRATION_FROM, _REBASELINE_RESUME_MIGRATION_TARGET),
    }
)
_REBASELINE_VERIFY_STEPS = frozenset(
    {
        "environment_mode",
        "budget",
        "pauses",
        "get_balance",
        "recover_services",
        "services",
        "restore_owned_pauses",
    }
)
_REBASELINE_OLD_SECRET_NAMES = frozenset(
    {
        "vendor_secret_name",
        "vendor_secret_key",
        "data_aes_key",
        "data_hmac_key",
        "jwt_secret",
        "ldap_bind_password",
        "metrics_scrape_token",
        "db_owner_password",
        "db_auth_password",
        "db_accept_password",
        "db_send_password",
        "db_callback_password",
        "db_export_password",
        "db_scheduler_password",
        "db_metrics_password",
        "redis_broker_password",
        "redis_auth_password",
        "redis_control_password",
    }
)
_REBASELINE_AUDIT_SECRET_NAMES = (
    "audit_context_key",
    "audit_system_api_context_key",
    "audit_system_realtime_context_key",
    "audit_system_bulk_context_key",
)
_REBASELINE_ALERT_PUBLIC_KEY = "alert_credential_public_key"
_REBASELINE_ALERT_PRIVATE_KEY = "alert_credential_private_key"
_REBASELINE_NEW_SECRET_NAMES = frozenset(
    {
        *_REBASELINE_AUDIT_SECRET_NAMES,
        _REBASELINE_ALERT_PUBLIC_KEY,
        _REBASELINE_ALERT_PRIVATE_KEY,
    }
)
_REBASELINE_DEV_SECRET = "dev-apikeys.txt"
_MAX_SECRET_BYTES = 1024 * 1024


def _needs_legacy_rebaseline_secret_expansion(
    migration_from: str,
    migration_target: str,
) -> bool:
    return (migration_from, migration_target) == (
        _REBASELINE_MIGRATION_FROM,
        _REBASELINE_MIGRATION_TARGET,
    )


class TestUpdateManagerError(RuntimeError):
    """快速更新准备阶段被安全门禁阻断。"""


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_rebaseline_secret_file(
    path: Path,
    *,
    expected_uid: int,
) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise TestUpdateManagerError(
            "rebaseline authoritative secret inventory is unsafe"
        ) from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != expected_uid
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
        or not 1 <= metadata.st_size <= _MAX_SECRET_BYTES
    ):
        raise TestUpdateManagerError(
            "rebaseline authoritative secret inventory is unsafe"
        )
    return metadata


def _read_rebaseline_key(path: Path, *, expected_uid: int) -> bytes:
    before = _validate_rebaseline_secret_file(path, expected_uid=expected_uid)
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if (
            (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_uid != expected_uid
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_nlink != 1
        ):
            raise OSError("secret identity changed")
        raw = os.read(descriptor, 129)
    except OSError as exc:
        raise TestUpdateManagerError(
            "rebaseline generated secret is unsafe"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        decoded = base64.b64decode(raw.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise TestUpdateManagerError(
            "rebaseline generated secret is invalid"
        ) from exc
    if len(decoded) != 32 or raw != base64.b64encode(decoded) + b"\n":
        raise TestUpdateManagerError("rebaseline generated secret is invalid")
    return decoded


def _write_rebaseline_key(
    path: Path,
    value: bytes,
    *,
    expected_uid: int,
) -> None:
    if len(value) != 32:
        raise TestUpdateManagerError("rebaseline generated secret is invalid")
    descriptor = -1
    created = False
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        created = True
        os.fchmod(descriptor, 0o600)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise OSError("generated secret identity is unsafe")
        payload = base64.b64encode(value) + b"\n"
        if os.write(descriptor, payload) != len(payload):
            raise OSError("generated secret write was incomplete")
        os.fsync(descriptor)
    except OSError as exc:
        if created:
            with contextlib.suppress(OSError):
                path.unlink()
        raise TestUpdateManagerError(
            "rebaseline generated secret could not be committed"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    _fsync_directory(path.parent)


def _x25519_public_key(private_value: bytes) -> bytes:
    try:
        return X25519PrivateKey.from_private_bytes(private_value).public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    except (TypeError, ValueError) as exc:
        raise TestUpdateManagerError(
            "rebaseline alert credential keypair is invalid"
        ) from exc


class FixedCommandRunner:
    """仅执行固定 argv，失败时不回显子进程输出。"""

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        input_bytes: bytes | None = None,
    ) -> bytes:
        try:
            result = subprocess.run(
                list(command),
                cwd=cwd,
                env=dict(env) if env is not None else None,
                input=input_bytes,
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            raise TestUpdateManagerError("controlled command is unavailable") from exc
        if result.returncode != 0:
            raise TestUpdateManagerError(
                f"controlled command failed: {Path(command[0]).name}"
            )
        return result.stdout


def _decode_output(payload: bytes) -> str:
    try:
        return payload.decode("utf-8").strip()
    except UnicodeError as exc:
        raise TestUpdateManagerError("controlled command output is invalid") from exc


def _tracked_worktree_paths(root: Path) -> list[Path]:
    """读取 Git index 中的 tracked 路径，拒绝越界或不可解析的路径。"""

    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise TestUpdateManagerError(
            "tracked worktree paths could not be enumerated"
        ) from exc
    if result.returncode != 0:
        raise TestUpdateManagerError("tracked worktree paths could not be enumerated")

    paths: set[Path] = set()
    try:
        for raw_name in result.stdout.split(b"\0"):
            if not raw_name:
                continue
            relative = Path(os.fsdecode(raw_name))
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("tracked path escapes checkout")
            path = root / relative
            paths.add(path)
            paths.update(
                parent
                for parent in path.parents
                if parent != root and root in parent.parents
            )
    except (UnicodeError, ValueError) as exc:
        raise TestUpdateManagerError("tracked worktree paths are invalid") from exc
    return sorted(paths, key=lambda path: (len(path.parts), str(path)))


def _restore_operator_worktree_read_access(
    root: Path,
    operator_gid: int,
    *,
    tracked_paths: Sequence[Path] | None = None,
) -> None:
    """让 operator 可读 tracked 工作树，同时不触碰 ignored secrets/data。"""

    paths = (
        list(tracked_paths)
        if tracked_paths is not None
        else _tracked_worktree_paths(root)
    )
    try:
        for path in sorted(paths, key=lambda item: (len(item.parts), str(item))):
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise OSError("tracked worktree symlink")
            mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISDIR(metadata.st_mode):
                if metadata.st_gid != operator_gid:
                    os.chown(path, -1, operator_gid, follow_symlinks=False)
                os.chmod(
                    path,
                    mode | stat.S_IRGRP | stat.S_IXGRP,
                    follow_symlinks=False,
                )
            elif stat.S_ISREG(metadata.st_mode):
                if metadata.st_gid != operator_gid:
                    os.chown(path, -1, operator_gid, follow_symlinks=False)
                os.chmod(path, mode | stat.S_IRGRP, follow_symlinks=False)
            else:
                raise OSError("unsafe tracked worktree entry")
    except OSError as exc:
        raise TestUpdateManagerError(
            "operator tracked worktree access could not be restored"
        ) from exc


def _restore_operator_git_read_access(root: Path) -> None:
    """恢复 root checkout 后更新用户读取完整 Git 读路径所需的权限。

    root 可能在切换时创建新的 loose object；只修复 ``HEAD`` 和 ``index``
    会让 operator 能读取提交指针，却无法读取工作树。先完整检查元数据树，
    再修复 tracked 工作树的 group 读权限，避免 ignored secrets/data 被触碰。
    """

    git_dir = root / ".git"
    try:
        git_metadata = git_dir.lstat()
        if (
            not stat.S_ISDIR(git_metadata.st_mode)
            or stat.S_ISLNK(git_metadata.st_mode)
            or git_metadata.st_gid == 0
        ):
            raise OSError("unsafe git metadata directory")

        operator_gid = git_metadata.st_gid
        directories: list[Path] = []
        files: list[Path] = []
        pending = [git_dir]
        while pending:
            directory = pending.pop()
            directories.append(directory)
            with os.scandir(directory) as entries:
                for entry in entries:
                    metadata = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(metadata.st_mode):
                        raise OSError("unsafe git metadata symlink")
                    path = Path(entry.path)
                    if stat.S_ISDIR(metadata.st_mode):
                        pending.append(path)
                    elif stat.S_ISREG(metadata.st_mode):
                        files.append(path)
                    else:
                        raise OSError("unsafe git metadata entry")

        for path in directories:
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise OSError("unsafe git metadata directory")
            if metadata.st_gid != operator_gid:
                os.chown(path, -1, operator_gid, follow_symlinks=False)
            os.chmod(
                path,
                (stat.S_IMODE(metadata.st_mode) & 0o700) | 0o050,
                follow_symlinks=False,
            )
        for path in files:
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise OSError("unsafe git metadata file")
            if metadata.st_gid != operator_gid:
                os.chown(path, -1, operator_gid, follow_symlinks=False)
            os.chmod(
                path,
                (stat.S_IMODE(metadata.st_mode) & 0o700) | 0o040,
                follow_symlinks=False,
            )
        _restore_operator_worktree_read_access(root, operator_gid)
    except OSError as exc:
        raise TestUpdateManagerError(
            "operator Git metadata access could not be restored"
        ) from exc


def _reject_backup_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise TestUpdateManagerError("backup config is unsafe")
        result[key] = value
    return result


def _reject_control_state_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise TestUpdateManagerError("vendor control state is invalid")
        result[key] = value
    return result


def _public_cutover_scope(
    raw: str,
    *,
    expected_source_commit: str,
    expected_private_merge_base: str,
) -> ChangedScope:
    """把不可变验真器的最小 JSON 结果收窄为快速更新范围。"""

    try:
        document = json.loads(raw, object_pairs_hook=_reject_control_state_duplicates)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise TestUpdateManagerError("public cutover result is invalid") from exc
    if type(document) is not dict or set(document) != set(
        _PUBLIC_CUTOVER_RESULT_FIELDS
    ):
        raise TestUpdateManagerError("public cutover result is invalid")
    components = document["components"]
    risk = document["risk"]
    commit_fields = (
        document["private_merge_base"],
        document["publication_commit"],
        document["source_commit"],
    )
    if (
        document["cutover"] is not True
        or type(components) is not list
        or not components
        or any(type(component) is not str for component in components)
        or len(components) != len(set(components))
        or not set(components).issubset({"api", "web"})
        or type(document["logical_changed"]) is not int
        or document["logical_changed"] < 1
        or type(document["migration_changed"]) is not bool
        or type(document["runtime_changed"]) is not bool
        or document["runtime_changed"] is not True
        or type(risk) is not str
        or risk not in {"web-only", "backend-safe", "high-risk"}
        or any(
            type(value) is not str
            or re.fullmatch(r"[0-9a-f]{40}", value) is None
            for value in commit_fields
        )
        or document["source_commit"] != expected_source_commit
        or document["private_merge_base"] != expected_private_merge_base
    ):
        raise TestUpdateManagerError("public cutover result is invalid")
    return ChangedScope(
        components=frozenset(cast(list[str], components)),
        migration_changed=document["migration_changed"],
        backend_tests=(),
        frontend_tests=(),
        runtime_changed=True,
        risk=cast(Literal["web-only", "backend-safe", "high-risk"], risk),
        high_risk_paths=(),
    )


def _read_backup_config(path: Path, *, expected_uid: int) -> tuple[Path, Path]:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or stat.S_IMODE(metadata.st_mode) not in {0o400, 0o600}
        ):
            raise TestUpdateManagerError("backup config is unsafe")
        document = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_backup_duplicates,
        )
    except TestUpdateManagerError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TestUpdateManagerError("backup config is unsafe") from exc
    if (
        type(document) is not dict
        or set(document) != set(_BACKUP_CONFIG_FIELDS)
        or document.get("schema_version") != 1
        or document.get("database") != _DATABASE
        or document.get("output_root") != str(_BACKUP_OUTPUT_ROOT)
        or document.get("key_file") != str(_BACKUP_KEY_FILE)
    ):
        raise TestUpdateManagerError("backup config is unsafe")
    return _BACKUP_OUTPUT_ROOT, _BACKUP_KEY_FILE


class HostUpdateOperations:
    """快速更新所需的最小固定宿主能力，不依赖可变 checkout 模块。"""

    def __init__(
        self,
        *,
        root: Path,
        runtime_root: Path,
        backup_config_file: Path,
        expected_uid: int,
        runner: FixedCommandRunner | None,
    ) -> None:
        self.root = root
        self.runtime_root = runtime_root
        self.backup_config_file = backup_config_file
        self.expected_uid = expected_uid
        self.runner = runner or FixedCommandRunner()
        self.compose = (
            "docker",
            "compose",
            "--env-file",
            str(root / ".env"),
            "-f",
            str(root / "deploy/docker-compose.yml"),
        )
        self.public_cutover_base: str | None = None
        self.public_cutover_target: str | None = None

    def bind_public_cutover(self, *, base: str, target: str) -> None:
        if (
            re.fullmatch(r"[0-9a-f]{40}", base) is None
            or re.fullmatch(r"[0-9a-f]{40}", target) is None
            or base == target
        ):
            raise TestUpdateManagerError("public cutover binding is invalid")
        self.public_cutover_base = base
        self.public_cutover_target = target

    def _docker(self, *arguments: str, input_bytes: bytes | None = None) -> str:
        return _decode_output(
            self.runner.run(
                ["docker", *arguments],
                input_bytes=input_bytes,
            )
        )

    def _public_cutover_state(self) -> dict[str, object]:
        path = _PUBLIC_CUTOVER_BOOTSTRAP_STATE
        try:
            metadata = path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != self.expected_uid
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise OSError("unsafe state")
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise TestUpdateManagerError(
                "public cutover bootstrap state is unavailable"
            ) from exc
        if (
            type(value) is not dict
            or set(value) != set(_PUBLIC_CUTOVER_STATE_FIELDS)
            or value.get("schema_version") != 1
            or value.get("status") not in {"ready", "activated"}
            or value.get("base_commit") != self.public_cutover_base
            or value.get("target_commit") != self.public_cutover_target
            or type(value.get("redis_image_id")) is not str
            or re.fullmatch(r"sha256:[0-9a-f]{64}", value["redis_image_id"]) is None
        ):
            raise TestUpdateManagerError(
                "public cutover bootstrap state is invalid"
            )
        return value

    def _transitional_control_container(self) -> str:
        self._public_cutover_state()
        observed = self._docker(
            "ps",
            "--filter",
            "label=com.docker.compose.project=sms-platform",
            "--filter",
            "label=com.docker.compose.service=redis-control",
            "--filter",
            "status=running",
            "--format",
            "{{.ID}}",
        ).splitlines()
        if len(observed) != 1 or re.fullmatch(r"[0-9a-f]{12,64}", observed[0]) is None:
            raise TestUpdateManagerError(
                "public cutover control Redis identity is invalid"
            )
        identity = self._docker(
            "inspect",
            "--format",
            (
                '{{index .Config.Labels "com.docker.compose.project"}}|'
                '{{index .Config.Labels "com.docker.compose.service"}}|'
                "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}"
            ),
            observed[0],
        )
        if identity != "sms-platform|redis-control|running|healthy":
            raise TestUpdateManagerError(
                "public cutover control Redis is not healthy"
            )
        state = self._public_cutover_state()
        image_id = self._docker(
            "inspect",
            "--format",
            "{{.Image}}",
            observed[0],
        )
        if image_id != state["redis_image_id"]:
            raise TestUpdateManagerError(
                "public cutover control Redis image is invalid"
            )
        return observed[0]

    def _run(self, *arguments: str, input_bytes: bytes | None = None) -> str:
        return _decode_output(
            self.runner.run(
                [*self.compose, *arguments],
                input_bytes=input_bytes,
            )
        )

    def _redis(self, *arguments: str) -> str:
        script = (
            'exec redis-cli --user sms_control --askpass --raw "$@" '
            "< /run/secrets/redis_control_password"
        )
        if self.public_cutover_base is not None:
            current = _decode_output(
                self.runner.run(
                    [
                        "git",
                        "-C",
                        str(self.root),
                        "rev-parse",
                        "HEAD",
                    ]
                )
            )
            if current == self.public_cutover_base:
                container = self._transitional_control_container()
                return self._docker(
                    "exec",
                    "-i",
                    container,
                    "sh",
                    "-ec",
                    script,
                    "sh",
                    *arguments,
                )
            if current != self.public_cutover_target:
                raise TestUpdateManagerError(
                    "public cutover repository identity is invalid"
                )
        return self._run(
            "exec",
            "-T",
            "redis-control",
            "sh",
            "-ec",
            script,
            "sh",
            *arguments,
        )

    def activate_public_cutover_redis(self) -> None:
        state = self._public_cutover_state()
        target = self.public_cutover_target
        assert target is not None
        if state["status"] == "activated":
            return
        image_id = state["redis_image_id"]
        assert isinstance(image_id, str)
        revision = self._docker(
            "image",
            "inspect",
            "--format",
            '{{index .Config.Labels "org.opencontainers.image.revision"}}',
            image_id,
        )
        if revision != target:
            raise TestUpdateManagerError(
                "public cutover Redis image revision is invalid"
            )
        _atomic_update_image_env(
            self.root / ".env",
            _REDIS_IMAGE_ENV_KEY,
            image_id,
            expected_uid=self.expected_uid,
        )
        self._run(
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            "120",
            "redis",
            "redis-auth",
            "redis-control",
        )
        state["status"] = "activated"
        _atomic_write_json(
            _PUBLIC_CUTOVER_BOOTSTRAP_STATE,
            state,
            expected_uid=self.expected_uid,
        )

    def _psql(self, sql: str) -> str:
        return self._run(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "sms_owner",
            "-d",
            _DATABASE,
            "-Atqc",
            sql,
        )

    def require_lifecycle_lock(self) -> None:
        require_inherited_lifecycle_lock(self.runtime_root)

    def stop_senders(self) -> None:
        self._run(
            "stop",
            "beat",
            "worker-realtime",
            "worker-report",
            "worker-bulk",
            "worker-callback",
            "outbox-dispatcher",
        )

    def create_encrypted_checkpoint(self) -> str:
        output_root, key_file = _read_backup_config(
            self.backup_config_file,
            expected_uid=self.expected_uid,
        )
        checkpoint_id = (
            f"test-update-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-"
            f"{uuid.uuid4().hex[:12]}"
        )
        result = TestUpdateBackup(self.runner).create(
            BackupConfig(
                output_root=output_root,
                key_file=key_file,
                database=_DATABASE,
                pg_dump_argv=(
                    *self.compose,
                    "exec",
                    "-T",
                    "postgres",
                    "pg_dump",
                    "-U",
                    "sms_owner",
                    "-d",
                    _DATABASE,
                    "--format=custom",
                ),
                pg_restore_argv=(
                    *self.compose,
                    "exec",
                    "-T",
                    "postgres",
                    "pg_restore",
                    "--list",
                ),
                runtime_root=self.runtime_root,
            ),
            checkpoint_id,
        )
        if not result.complete:
            raise TestUpdateManagerError("encrypted checkpoint is incomplete")
        return checkpoint_id

    def active_recipient_count(self) -> int:
        raw = self._psql(
            "SELECT count(*) FROM vendor_test_recipient WHERE status='active'"
        )
        try:
            count = int(raw)
        except ValueError as exc:
            raise TestUpdateManagerError("active recipient count is invalid") from exc
        if count < 0:
            raise TestUpdateManagerError("active recipient count is invalid")
        return count

    def probe_balance(self) -> None:
        program = """import asyncio
from app.vendor.zhihui import ZhihuiClient

async def probe():
    async with ZhihuiClient.from_settings() as client:
        await client.get_balance()

asyncio.run(probe())
"""
        self._run(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "worker-realtime",
            "python",
            "-c",
            program,
        )


class StateStore(Protocol):
    def transition(
        self,
        expected: TestUpdateState,
        target: TestUpdateState,
        *,
        step: str,
        error_type: str | None = None,
        actual_commit: str | None = None,
        actual_migration_head: str | None = None,
    ) -> None: ...

    def block(
        self,
        expected: TestUpdateState,
        *,
        step: str,
        error_type: str = "step_failed",
        actual_commit: str | None = None,
        actual_migration_head: str | None = None,
    ) -> None: ...


class PrepareOperations(Protocol):
    def require_lifecycle_lock(self) -> None: ...

    def validate_vendor_update_mode(self) -> str: ...

    def pause_lanes_for_update(self, update_id: str) -> None: ...

    def finalize_pause_ownership(self, update_id: str) -> None: ...

    def unsafe_status_counts(self) -> Mapping[str, int]: ...

    def create_encrypted_checkpoint(self, update_id: str) -> str: ...

    def check_expand_migration(self, migration_from: str, target: str) -> None: ...

    def expand_rebaseline_secrets(self) -> None: ...

    def hold_fail_closed(self, update_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class PreparedUpdate:
    kind: str
    checkpoint_id: str | None


def _require_no_unsafe_chunks(
    counts: Mapping[str, int],
    *,
    include_uncertain: bool,
) -> None:
    if set(counts) != set(UNSAFE_CHUNK_STATUSES):
        raise TestUpdateManagerError("unsafe status observation is invalid")
    if any(
        type(counts[status]) is not int or counts[status] < 0
        for status in UNSAFE_CHUNK_STATUSES
    ):
        raise TestUpdateManagerError("unsafe status observation is invalid")
    blocking_statuses = ("submitting", "retrying", "uncertain") if include_uncertain else (
        "submitting",
        "retrying",
    )
    if any(counts[status] for status in blocking_statuses):
        raise TestUpdateManagerError("unsafe vendor chunks must be resolved first")


class TestUpdateManager:
    """按组件准备已通过 CI 的更新，并按迁移与安全路径收紧远端检查。"""

    def __init__(self, store: StateStore, operations: PrepareOperations) -> None:
        self.store = store
        self.operations = operations

    def prepare(
        self,
        scope: ChangedScope,
        *,
        update_id: str,
        commit: str,
        migration_from: str,
        migration_target: str,
        operation: Literal["apply", "rebaseline"] = "apply",
    ) -> PreparedUpdate:
        if operation not in {"apply", "rebaseline"}:
            raise TestUpdateManagerError("update operation is invalid")
        if scope.risk not in {"web-only", "backend-safe", "high-risk"}:
            raise TestUpdateManagerError("update has no supported runtime scope")
        effective_risk = (
            "backend-safe"
            if "api" in scope.components
            else "web-only"
        )

        step = "lock"
        locked = False
        try:
            self.operations.require_lifecycle_lock()
            locked = True
            if effective_risk == "web-only":
                return PreparedUpdate("web-only", None)

            step = "mode"
            environment_mode = self.operations.validate_vendor_update_mode()
            if environment_mode not in {"pre-live", "live"}:
                raise TestUpdateManagerError("update environment mode is invalid")
            step = "pause"
            self.operations.pause_lanes_for_update(update_id)
            step = "unsafe_counts"
            migration_changed = migration_from != migration_target
            _require_no_unsafe_chunks(
                self.operations.unsafe_status_counts(),
                include_uncertain=scope.risk == "high-risk" or migration_changed,
            )
            if not migration_changed:
                step = "pause_owner"
                self.operations.finalize_pause_ownership(update_id)
                return PreparedUpdate("backend-safe", None)
            step = "checkpoint"
            checkpoint_id = self.operations.create_encrypted_checkpoint(update_id)
            if not checkpoint_id:
                raise TestUpdateManagerError("encrypted checkpoint is invalid")
            step = "migration_check"
            self.operations.check_expand_migration(migration_from, migration_target)
            if operation == "rebaseline" and _needs_legacy_rebaseline_secret_expansion(
                migration_from,
                migration_target,
            ):
                step = "secret_expansion"
                self.operations.expand_rebaseline_secrets()
            step = "pause_owner"
            self.operations.finalize_pause_ownership(update_id)
            self.store.transition(
                TestUpdateState.PREPARED,
                TestUpdateState.CHECKPOINTED,
                step="checkpoint",
                actual_commit=commit,
                actual_migration_head=migration_from,
            )
            return PreparedUpdate("backend-safe", checkpoint_id)
        except Exception:
            if locked and effective_risk == "backend-safe":
                with contextlib.suppress(Exception):
                    self.store.block(
                        TestUpdateState.PREPARED,
                        step=step,
                        error_type="validation_failed",
                        actual_commit=commit,
                        actual_migration_head=migration_from,
                    )
                with contextlib.suppress(Exception):
                    self.operations.hold_fail_closed(update_id)
            raise TestUpdateManagerError(
                f"test update prepare blocked at {step}"
            ) from None


def _prepare_from_source(
    store: StateStore,
    operations: HostTestUpdateOperations,
    *,
    request: TestUpdateRequest,
) -> None:
    """执行 prepare，并在 root Git 操作失败后恢复 operator 读路径。

    ``verify_source_scope`` 在进入 ``TestUpdateManager.prepare`` 之前可能执行
    root-owned fetch/checkout。权限恢复必须覆盖这段前置阶段，否则任一 Git
    失败都会把日常更新用户锁在 checkout 外，下一次正式入口只能失败关闭。
    """

    prepare_error: Exception | None = None
    try:
        scope = operations.verify_source_scope()
        operations.load_and_validate_images()
        TestUpdateManager(store, operations).prepare(
            scope,
            update_id=request.update_id,
            commit=request.commit,
            migration_from=request.migration_from,
            migration_target=request.migration_target,
            operation=request.operation,
        )
    except Exception as error:
        prepare_error = error
        with contextlib.suppress(Exception):
            store.block(
                TestUpdateState.PREPARED,
                step="prepare",
                error_type="validation_failed",
                actual_commit=request.base_commit,
                actual_migration_head=request.migration_from,
            )
        raise
    finally:
        # This is deliberately outside the error handler: a failed restoration
        # is itself a hard failure and must never be reported as a successful
        # prepare. The helper only touches .git and tracked worktree paths.
        try:
            _restore_operator_git_read_access(operations.root)
        except Exception as error:
            with contextlib.suppress(Exception):
                store.block(
                    TestUpdateState.PREPARED,
                    step="git_permissions",
                    error_type="validation_failed",
                    actual_commit=request.base_commit,
                    actual_migration_head=request.migration_from,
                )
            if prepare_error is None:
                raise
            raise TestUpdateManagerError(
                "operator Git metadata access could not be restored"
            ) from error


_UPDATE_KINDS = {"web-only", "backend-safe"}
_UPDATE_PAUSE_RE = re.compile(r"test-update:[A-Za-z0-9_-]{1,64}")


def _read_private_request(path: Path, *, expected_uid: int) -> tuple[str, TestUpdateRequest]:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise TestUpdateManagerError("incoming request is unsafe")
        raw = path.read_text(encoding="utf-8")
    except TestUpdateManagerError:
        raise
    except (OSError, UnicodeError) as exc:
        raise TestUpdateManagerError("incoming request is unavailable") from exc
    return raw, parse_test_update_request(raw)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise TestUpdateManagerError("image archive is unavailable") from exc
    return digest.hexdigest()


def _atomic_update_image_env(
    path: Path,
    key: str,
    image_id: str,
    *,
    expected_uid: int,
) -> None:
    if (
        key not in _ACTIVATABLE_IMAGE_ENV_KEYS
        or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None
    ):
        raise TestUpdateManagerError("image activation value is invalid")
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise TestUpdateManagerError("dotenv image activation is unsafe")
        lines = path.read_text(encoding="utf-8").splitlines()
    except TestUpdateManagerError:
        raise
    except (OSError, UnicodeError) as exc:
        raise TestUpdateManagerError("dotenv image activation is unsafe") from exc
    positions = [index for index, line in enumerate(lines) if line.startswith(f"{key}=")]
    if len(positions) > 1:
        raise TestUpdateManagerError("dotenv image key is duplicated")
    rendered = f"{key}={image_id}"
    if positions:
        lines[positions[0]] = rendered
    else:
        lines.append(rendered)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(("\n".join(lines) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write_json(
    path: Path,
    value: Mapping[str, object],
    *,
    expected_uid: int,
) -> None:
    try:
        parent = path.parent.lstat()
        current = path.lstat()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or stat.S_ISLNK(parent.st_mode)
            or parent.st_uid != expected_uid
            or stat.S_IMODE(parent.st_mode) != 0o700
            or not stat.S_ISREG(current.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or current.st_uid != expected_uid
            or stat.S_IMODE(current.st_mode) != 0o600
        ):
            raise TestUpdateManagerError(
                "public cutover bootstrap state update is unsafe"
            )
        payload = (
            json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode("utf-8")
    except TestUpdateManagerError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise TestUpdateManagerError(
            "public cutover bootstrap state update is unsafe"
        ) from exc
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


class HostTestUpdateOperations:
    """固定绑定 live-test 快速更新；无迁移失败时仅回退应用镜像。"""

    def __init__(
        self,
        *,
        root: Path,
        runtime_root: Path,
        marker_file: Path,
        state_root: Path,
        request: TestUpdateRequest,
        host_source_commit: str | None = None,
        expected_uid: int = 0,
        runner: FixedCommandRunner | None = None,
    ) -> None:
        self.root = root
        self.runtime_root = runtime_root
        self.marker_file = marker_file
        self.state_root = state_root
        self.request = request
        if (
            host_source_commit is not None
            and re.fullmatch(r"[0-9a-f]{40}", host_source_commit) is None
        ):
            raise TestUpdateManagerError("host source commit is invalid")
        self.host_source_commit = host_source_commit
        self.expected_uid = expected_uid
        self.pause_value = f"test-update:{request.update_id}"
        if _UPDATE_PAUSE_RE.fullmatch(self.pause_value) is None:
            raise TestUpdateManagerError("update pause value is invalid")
        self.pending_pause_owner: str | None = None
        self.host = HostUpdateOperations(
            root=root,
            runtime_root=runtime_root,
            backup_config_file=Path("/etc/sms-platform/test-update-backup.json"),
            expected_uid=expected_uid,
            runner=runner,
        )
        if request.public_cutover is not None:
            self.host.bind_public_cutover(
                base=request.base_commit,
                target=request.commit,
            )

    def _command(self, *argv: str) -> str:
        payload = self.host.runner.run(list(argv))
        try:
            return payload.decode("utf-8").strip()
        except UnicodeError as exc:
            raise TestUpdateManagerError("controlled command output is invalid") from exc

    def require_lifecycle_lock(self) -> None:
        self.host.require_lifecycle_lock()

    def _read_fresh_control_state(self) -> dict[str, object]:
        path = Path("/var/lib/sms-platform/vendor-test/control-state.json")
        fields = {
            "schema_version",
            "mode",
            "heartbeat_at",
            "credential_configured",
            "active_recipient_count",
            "pause_kind",
            "daily_limit",
        }
        descriptor = -1
        try:
            metadata = path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != self.expected_uid
                or metadata.st_gid != _BACKEND_RUNTIME_GID
                or stat.S_IMODE(metadata.st_mode) != 0o640
                or not 1 <= metadata.st_size <= 4096
            ):
                raise OSError("unsafe control state")
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino)
                != (metadata.st_dev, metadata.st_ino)
                or opened.st_uid != self.expected_uid
                or opened.st_gid != _BACKEND_RUNTIME_GID
                or stat.S_IMODE(opened.st_mode) != 0o640
                or not 1 <= opened.st_size <= 4096
            ):
                raise OSError("unsafe control state")
            raw = os.read(descriptor, 4097)
            if len(raw) > 4096:
                raise OSError("oversized control state")
            document = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_reject_control_state_duplicates,
            )
        except TestUpdateManagerError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise TestUpdateManagerError("vendor control state is unavailable") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if type(document) is not dict or set(document) != fields:
            raise TestUpdateManagerError("vendor control state is invalid")
        # reset_configuration 的官方投影是 setup_required，且可以留下已登记号码。
        if (
            (
                document["mode"] == "setup_required"
                and (
                    document["credential_configured"] is not False
                    or document["pause_kind"] is not None
                )
            )
            or (
                document["mode"] == "inactive"
                and (
                    document["credential_configured"] is not True
                    or document["pause_kind"] is not None
                )
            )
        ):
            raise TestUpdateManagerError("vendor control state is inconsistent")
        if (
            document.get("schema_version") != 1
            or document.get("mode")
            not in {"setup_required", "inactive", "controlled", "blocked"}
            or type(document.get("credential_configured")) is not bool
            or type(document.get("active_recipient_count")) is not int
            or document["active_recipient_count"] < 0
            or document.get("pause_kind") not in {None, "manual", "critical", "daily"}
            or document.get("daily_limit") != 100
            or type(document.get("heartbeat_at")) is not str
        ):
            raise TestUpdateManagerError("vendor control state is invalid")
        try:
            heartbeat = datetime.fromisoformat(str(document["heartbeat_at"]))
        except ValueError as exc:
            raise TestUpdateManagerError("vendor control state is invalid") from exc
        if heartbeat.tzinfo is None:
            raise TestUpdateManagerError("vendor control state is invalid")
        age = datetime.now(UTC) - heartbeat.astimezone(UTC)
        if age > timedelta(seconds=30) or age < -timedelta(seconds=5):
            raise TestUpdateManagerError("vendor control state is stale")
        return document

    def _activation_marker_exists(self) -> bool:
        try:
            self.marker_file.lstat()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise TestUpdateManagerError("vendor activation marker is unavailable") from exc
        return True

    def validate_vendor_update_mode(self) -> str:
        require_test_host_marker(expected_uid=self.expected_uid)
        if self.request.environment_mode == "pre-live":
            if self._activation_marker_exists():
                raise TestUpdateManagerError("pre-live activation marker must be absent")
            reconcile_pure_mock_dotenv(
                self.root / ".env",
                expected_uid=self.expected_uid,
            )
            state = self._read_fresh_control_state()
            if state["mode"] == "controlled":
                raise TestUpdateManagerError("pre-live control state is invalid")
            return "pre-live"

        if not self._activation_marker_exists():
            raise TestUpdateManagerError("vendor live marker is required")
        marker = read_vendor_test_marker(
            self.marker_file,
            expected_uid=self.expected_uid,
        )
        if marker.mode != "development-vendor-live":
            raise TestUpdateManagerError("vendor live mode is required")
        if self.host.active_recipient_count() < 1:
            raise TestUpdateManagerError("active vendor recipients are empty")
        return "live"

    def pause_lanes_for_update(self, update_id: str) -> None:
        if update_id != self.request.update_id:
            raise TestUpdateManagerError("update ID mismatch")
        script = (
            "local a=redis.call('get',KEYS[1]); "
            "local b=redis.call('get',KEYS[2]); "
            "if not a and not b then "
            "redis.call('set',KEYS[1],ARGV[1]); "
            "redis.call('set',KEYS[2],ARGV[1]); return 'acquired' end; "
            "if a==ARGV[1] and b==ARGV[1] then return 'owned' end; "
            "if a and a==b then return a end; return 'blocked'"
        )
        observed = self.host._redis(
            "EVAL",
            script,
            "2",
            "queue:paused:realtime",
            "queue:paused:bulk",
            self.pause_value,
        )
        if observed in {"acquired", "owned"}:
            self.pending_pause_owner = None
            return
        if (
            _UPDATE_PAUSE_RE.fullmatch(observed) is None
            or observed == self.pause_value
        ):
            raise TestUpdateManagerError("existing critical pause must be preserved")
        predecessor_id = observed.removeprefix("test-update:")
        try:
            predecessor = TestUpdateStore(self.state_root, predecessor_id)
            predecessor.read_request()
            predecessor_state = predecessor.read_consistent_state()
        except Exception as exc:
            raise TestUpdateManagerError(
                "update pause predecessor is unavailable"
            ) from exc
        if predecessor_state["state"] != TestUpdateState.BLOCKED.value:
            raise TestUpdateManagerError(
                "update pause requires a blocked predecessor"
            )
        self.pending_pause_owner = observed

    def finalize_pause_ownership(self, update_id: str) -> None:
        if update_id != self.request.update_id:
            raise TestUpdateManagerError("update ID mismatch")
        if self.pending_pause_owner is None:
            return
        script = (
            "if redis.call('get',KEYS[1])~=ARGV[1] or "
            "redis.call('get',KEYS[2])~=ARGV[1] then return 0 end; "
            "redis.call('set',KEYS[1],ARGV[2]); "
            "redis.call('set',KEYS[2],ARGV[2]); return 1"
        )
        result = self.host._redis(
            "EVAL",
            script,
            "2",
            "queue:paused:realtime",
            "queue:paused:bulk",
            self.pending_pause_owner,
            self.pause_value,
        )
        if result != "1":
            raise TestUpdateManagerError("update pause ownership changed")
        self.pending_pause_owner = None

    def require_owned_update_pauses(self, update_id: str) -> None:
        if update_id != self.request.update_id:
            raise TestUpdateManagerError("update ID mismatch")
        script = (
            "if redis.call('get',KEYS[1])==ARGV[1] and "
            "redis.call('get',KEYS[2])==ARGV[1] then return 1 end; return 0"
        )
        result = self.host._redis(
            "EVAL",
            script,
            "2",
            "queue:paused:realtime",
            "queue:paused:bulk",
            self.pause_value,
        )
        if result != "1":
            raise TestUpdateManagerError("update pause ownership is invalid")

    def unsafe_status_counts(self) -> dict[str, int]:
        sql = """
        SELECT 'submitting|'||count(*) FROM sms_chunk WHERE status='submitting'
        UNION ALL SELECT 'retrying|'||count(*) FROM sms_chunk WHERE status='retrying'
        UNION ALL SELECT 'uncertain|'||count(*) FROM sms_chunk WHERE status='uncertain'
        """
        result: dict[str, int] = {}
        for line in self.host._psql(sql).splitlines():
            fields = line.split("|")
            if len(fields) != 2 or fields[0] not in UNSAFE_CHUNK_STATUSES:
                raise TestUpdateManagerError("unsafe status observation is invalid")
            try:
                result[fields[0]] = int(fields[1])
            except ValueError as exc:
                raise TestUpdateManagerError(
                    "unsafe status observation is invalid"
                ) from exc
        return result

    def create_encrypted_checkpoint(self, update_id: str) -> str:
        if update_id != self.request.update_id:
            raise TestUpdateManagerError("update ID mismatch")
        return self.host.create_encrypted_checkpoint()

    def check_expand_migration(self, migration_from: str, target: str) -> None:
        incoming = self.state_root / "incoming"
        temporary_root = Path(
            tempfile.mkdtemp(prefix=".migration-check-", dir=incoming)
        )
        checkout = temporary_root / "source"
        try:
            self._command(
                "git",
                "-C",
                str(self.root),
                "worktree",
                "add",
                "--detach",
                str(checkout),
                self.request.commit,
            )
            check_expand_only(
                checkout / "backend/migrations/versions",
                migration_from,
                target,
            )
        finally:
            with contextlib.suppress(Exception):
                self._command(
                    "git",
                    "-C",
                    str(self.root),
                    "worktree",
                    "remove",
                    "--force",
                    str(checkout),
                )
            shutil.rmtree(temporary_root, ignore_errors=True)

    def _require_approved_rebaseline_migration(self) -> None:
        if (
            self.request.operation != "rebaseline"
            or (
                self.request.migration_from,
                self.request.migration_target,
            )
            not in _APPROVED_REBASELINE_MIGRATIONS
            or self.request.migration_compatibility != "expand"
        ):
            raise TestUpdateManagerError(
                "rebaseline migration scope is invalid"
            )

    def _require_legacy_rebaseline_secret_migration(self) -> None:
        self._require_approved_rebaseline_migration()
        if not _needs_legacy_rebaseline_secret_expansion(
            self.request.migration_from,
            self.request.migration_target,
        ):
            raise TestUpdateManagerError(
                "rebaseline secret expansion scope is invalid"
            )

    def expand_rebaseline_secrets(self) -> None:
        """在精确 18 件旧清单上可恢复地追加本次所需的六件随机密钥。"""

        self._require_legacy_rebaseline_secret_migration()
        source = self.root / "deploy/secrets"
        try:
            metadata = source.lstat()
            names = {entry.name for entry in source.iterdir()}
        except OSError as exc:
            raise TestUpdateManagerError(
                "rebaseline authoritative secret inventory is unavailable"
            ) from exc
        allowed = (
            _REBASELINE_OLD_SECRET_NAMES
            | _REBASELINE_NEW_SECRET_NAMES
            | {_REBASELINE_DEV_SECRET}
        )
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != self.expected_uid
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or not _REBASELINE_OLD_SECRET_NAMES.issubset(names)
            or not names.issubset(allowed)
        ):
            raise TestUpdateManagerError(
                "rebaseline authoritative secret inventory is unsafe"
            )
        for name in sorted(_REBASELINE_OLD_SECRET_NAMES):
            _validate_rebaseline_secret_file(
                source / name,
                expected_uid=self.expected_uid,
            )
        if _REBASELINE_DEV_SECRET in names:
            _validate_rebaseline_secret_file(
                source / _REBASELINE_DEV_SECRET,
                expected_uid=self.expected_uid,
            )

        audit_values: dict[str, bytes] = {}
        for name in _REBASELINE_AUDIT_SECRET_NAMES:
            path = source / name
            if name in names:
                value = _read_rebaseline_key(path, expected_uid=self.expected_uid)
            else:
                value = os.urandom(32)
                while value in audit_values.values():
                    value = os.urandom(32)
                _write_rebaseline_key(
                    path,
                    value,
                    expected_uid=self.expected_uid,
                )
                names.add(name)
            if value in audit_values.values():
                raise TestUpdateManagerError(
                    "rebaseline audit context keys are not independent"
                )
            audit_values[name] = value

        private_path = source / _REBASELINE_ALERT_PRIVATE_KEY
        public_path = source / _REBASELINE_ALERT_PUBLIC_KEY
        if _REBASELINE_ALERT_PRIVATE_KEY in names:
            private_value = _read_rebaseline_key(
                private_path,
                expected_uid=self.expected_uid,
            )
        elif _REBASELINE_ALERT_PUBLIC_KEY in names:
            raise TestUpdateManagerError(
                "rebaseline alert credential keypair is incomplete"
            )
        else:
            private_key = X25519PrivateKey.generate()
            private_value = private_key.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
            _write_rebaseline_key(
                private_path,
                private_value,
                expected_uid=self.expected_uid,
            )
            names.add(_REBASELINE_ALERT_PRIVATE_KEY)

        expected_public = _x25519_public_key(private_value)
        if _REBASELINE_ALERT_PUBLIC_KEY in names:
            public_value = _read_rebaseline_key(
                public_path,
                expected_uid=self.expected_uid,
            )
            if public_value != expected_public:
                raise TestUpdateManagerError(
                    "rebaseline alert credential keypair is invalid"
                )
        else:
            _write_rebaseline_key(
                public_path,
                expected_public,
                expected_uid=self.expected_uid,
            )
            names.add(_REBASELINE_ALERT_PUBLIC_KEY)

        expected_names = _REBASELINE_OLD_SECRET_NAMES | _REBASELINE_NEW_SECRET_NAMES
        if _REBASELINE_DEV_SECRET in names:
            expected_names = expected_names | {_REBASELINE_DEV_SECRET}
        if names != expected_names:
            raise TestUpdateManagerError(
                "rebaseline authoritative secret inventory is incomplete"
            )
        _fsync_directory(source)

    def hold_fail_closed(self, update_id: str) -> None:
        if update_id != self.request.update_id:
            return
        for lane in ("realtime", "bulk"):
            with contextlib.suppress(Exception):
                self.host._redis(
                    "SET",
                    f"queue:paused:{lane}",
                    self.pause_value,
                    "NX",
                )
        with contextlib.suppress(Exception):
            self.host.stop_senders()

    def current_migration_head(self) -> str:
        head = self.host._psql("SELECT version_num FROM alembic_version")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", head) is None:
            raise TestUpdateManagerError("migration head observation is invalid")
        return head

    def verify_source_scope(self) -> ChangedScope:
        operation = getattr(self.request, "operation", "apply")
        if operation not in {"apply", "rebaseline"}:
            raise TestUpdateManagerError("request operation is invalid")
        if operation == "rebaseline" and (
            self.request.source_ref != "origin/main"
            or self.request.public_cutover is not None
            or self.request.components != frozenset({"api", "web"})
            or self.request.migration_compatibility != "expand"
            or self.request.migration_from == self.request.migration_target
        ):
            raise TestUpdateManagerError("rebaseline request scope is invalid")
        if self._command(
            "git",
            "-c",
            "status.showUntrackedFiles=no",
            "-C",
            str(self.root),
            "status",
            "--porcelain",
        ):
            raise TestUpdateManagerError("server checkout tracked content must be clean")
        actual = self._command("git", "-C", str(self.root), "rev-parse", "HEAD")
        if actual != self.request.base_commit:
            raise TestUpdateManagerError("base commit drifted")
        source_branch = self.request.source_ref.removeprefix("origin/")
        fetched_ref = f"refs/test-updates/{self.request.update_id}/source"
        source_refspec = f"+refs/heads/{source_branch}:{fetched_ref}"
        fetch_command = (
            "git",
            "-C",
            str(self.root),
            "fetch",
            "--prune",
            "--no-tags",
            "origin",
            source_refspec,
        )
        for attempt in range(len(_SOURCE_FETCH_BACKOFF_SECONDS) + 1):
            try:
                self._command(*fetch_command)
                break
            except TestUpdateManagerError:
                if attempt == len(_SOURCE_FETCH_BACKOFF_SECONDS):
                    raise
                time.sleep(_SOURCE_FETCH_BACKOFF_SECONDS[attempt])
        resolved = self._command(
            "git",
            "-C",
            str(self.root),
            "rev-parse",
            f"{fetched_ref}^{{commit}}",
        )
        if resolved != self.request.commit:
            raise TestUpdateManagerError("target commit is not the pushed source ref")
        host_source_commit = getattr(self, "host_source_commit", None)
        if host_source_commit is not None:
            control_diff = self._command(
                "git",
                "-C",
                str(self.root),
                "diff",
                "--name-only",
                "--no-renames",
                "-z",
                host_source_commit,
                self.request.commit,
                "--",
                *HOST_CONTROL_SOURCE_PATHS,
            )
            if control_diff:
                raise TestUpdateManagerError(
                    "host control snapshot does not match target"
                )
        try:
            merge_bases = self._command(
                "git",
                "-C",
                str(self.root),
                "merge-base",
                "--all",
                self.request.base_commit,
                self.request.commit,
            ).splitlines()
        except TestUpdateManagerError:
            merge_bases = []
        if merge_bases == [self.request.base_commit]:
            if self.request.public_cutover is not None:
                raise TestUpdateManagerError(
                    "related source history must not carry cutover evidence"
                )
            changed_raw = self._command(
                "git",
                "-C",
                str(self.root),
                "diff",
                "--name-only",
                "--no-renames",
                "-z",
                self.request.base_commit,
                self.request.commit,
            )
            changed = [path for path in changed_raw.split("\0") if path]
            scope = (
                classify_rebaseline_paths(changed)
                if operation == "rebaseline"
                else classify_changed_paths(changed)
            )
        else:
            if operation == "rebaseline":
                raise TestUpdateManagerError(
                    "rebaseline requires a descendant of the server baseline"
                )
            if self.request.source_ref != "origin/main":
                raise TestUpdateManagerError(
                    "unrelated source history requires public main cutover"
                )
            evidence = self.request.public_cutover
            if evidence is None:
                raise TestUpdateManagerError(
                    "public main cutover requires source evidence"
                )
            incoming = self.state_root / "incoming"
            source_pack = incoming / evidence.pack_file
            verifier = Path(__file__).with_name(
                "verify_public_snapshot_cutover.py"
            )
            if not verifier.is_file():
                repository_verifier = (
                    Path(__file__).resolve().parents[2]
                    / "scripts"
                    / verifier.name
                )
                if repository_verifier.is_file():
                    verifier = repository_verifier
            try:
                try:
                    metadata = source_pack.lstat()
                except OSError as exc:
                    raise TestUpdateManagerError(
                        "public cutover source evidence is unavailable"
                    ) from exc
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or stat.S_ISLNK(metadata.st_mode)
                    or metadata.st_uid != self.expected_uid
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                    or metadata.st_size <= 0
                    or metadata.st_size > 64 * 1024 * 1024
                    or _sha256_file(source_pack) != evidence.pack_sha256
                ):
                    raise TestUpdateManagerError(
                        "public cutover source evidence is unsafe"
                    )
                verified_raw = self._command(
                    "/usr/bin/python3",
                    str(verifier),
                    "--repository",
                    str(self.root),
                    "--baseline",
                    self.request.base_commit,
                    "--target",
                    self.request.commit,
                    "--ref",
                    self.request.source_ref,
                    "--resolved-ref",
                    fetched_ref,
                    "--source-pack",
                    str(source_pack),
                    "--expected-source-commit",
                    evidence.source_commit,
                    "--expected-private-merge-base",
                    evidence.private_merge_base,
                )
            finally:
                try:
                    source_pack.unlink(missing_ok=True)
                except OSError as exc:
                    raise TestUpdateManagerError(
                        "public cutover source evidence cleanup failed"
                    ) from exc
            scope = _public_cutover_scope(
                verified_raw,
                expected_source_commit=evidence.source_commit,
                expected_private_merge_base=evidence.private_merge_base,
            )
        if scope.components != self.request.components:
            raise TestUpdateManagerError("request components do not match source diff")
        return scope

    def load_and_validate_images(self) -> None:
        incoming = self.state_root / "incoming"
        for component, image in self.request.images.items():
            archive = incoming / image.archive_file
            try:
                metadata = archive.lstat()
            except OSError as exc:
                raise TestUpdateManagerError("image archive is unavailable") from exc
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != self.expected_uid
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or _sha256_file(archive) != image.archive_sha256
            ):
                raise TestUpdateManagerError("image archive is unsafe")
            self._command("docker", "load", "--input", str(archive))
            observed = self._command(
                "docker",
                "image",
                "inspect",
                "--format",
                (
                    "{{.Id}}|{{.Architecture}}|"
                    '{{index .Config.Labels "org.opencontainers.image.revision"}}|'
                    '{{index .Config.Labels "com.sms-platform.schema-revision"}}'
                ),
                image.ref,
            )
            if observed != (
                f"{image.image_id}|amd64|{self.request.commit}|"
                f"{self.request.migration_target}"
            ):
                raise TestUpdateManagerError("loaded image identity is invalid")
            if component not in _IMAGE_ENV_KEYS:
                raise TestUpdateManagerError("image component is invalid")

    def require_loaded_request_images(self) -> None:
        """复核 prepare 已加载的镜像仍与不可变请求完全一致。"""

        for component, image in self.request.images.items():
            if component not in _IMAGE_ENV_KEYS:
                raise TestUpdateManagerError("image component is invalid")
            observed = self._command(
                "docker",
                "image",
                "inspect",
                "--format",
                (
                    "{{.Id}}|{{.Architecture}}|"
                    '{{index .Config.Labels "org.opencontainers.image.revision"}}|'
                    '{{index .Config.Labels "com.sms-platform.schema-revision"}}'
                ),
                image.ref,
            )
            if observed != (
                f"{image.image_id}|amd64|{self.request.commit}|"
                f"{self.request.migration_target}"
            ):
                raise TestUpdateManagerError("loaded image identity is invalid")

    def _activate_source_and_image(self, component: str) -> None:
        image = self.request.images[component]
        self._command(
            "git",
            "-C",
            str(self.root),
            "checkout",
            "--detach",
            self.request.commit,
        )
        _restore_operator_git_read_access(self.root)
        _atomic_update_image_env(
            self.root / ".env",
            _IMAGE_ENV_KEYS[component],
            image.image_id,
            expected_uid=self.expected_uid,
        )

    def _prepare_rebaseline_runtime_secrets(self) -> None:
        if self.request.operation != "rebaseline":
            return
        self._require_approved_rebaseline_migration()
        if not _needs_legacy_rebaseline_secret_expansion(
            self.request.migration_from,
            self.request.migration_target,
        ):
            return
        command = [
            "/usr/bin/python3",
            str(self.root / "deploy/scripts/prepare_runtime_secrets.py"),
        ]
        if self.request.environment_mode == "live":
            command.extend(
                [
                    "prepare",
                    "--source-dir",
                    str(self.root / "deploy/secrets"),
                    "--runtime-root",
                    str(self.runtime_root),
                    "--mode",
                    "development",
                    "--vendor-credential-root",
                    "/var/lib/sms-platform/vendor-test/credentials",
                ]
            )
        elif self.request.environment_mode == "pre-live":
            command.extend(
                [
                    "revoke-vendor",
                    "--source-dir",
                    str(self.root / "deploy/secrets"),
                    "--runtime-root",
                    str(self.runtime_root),
                    "--mode",
                    "development",
                ]
            )
        else:
            raise TestUpdateManagerError(
                "rebaseline runtime secret mode is invalid"
            )
        self._command(*command)

    def _rollback_ref(self, component: str) -> str:
        if component not in _IMAGE_ENV_KEYS:
            raise TestUpdateManagerError("rollback component is invalid")
        return (
            f"sms-platform-test-rollback-{component}:"
            f"{self.request.update_id}"
        )

    def prepare_rollback_images(self) -> None:
        """在任何源码、unit 或容器切换前锁定原始镜像身份。"""

        self._prepare_rollback_images(
            self.request.components,
            require_current_match=True,
        )

    def _prepare_rollback_images(
        self,
        components: frozenset[str],
        *,
        require_current_match: bool = False,
    ) -> None:
        for component in sorted(components):
            rollback_ref = self._rollback_ref(component)
            existing = self._command(
                "docker",
                "image",
                "ls",
                "--no-trunc",
                "--quiet",
                rollback_ref,
            ).splitlines()
            if existing:
                if (
                    len(existing) != 1
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", existing[0]) is None
                ):
                    raise TestUpdateManagerError(
                        "rollback image identity is invalid"
                    )
                if require_current_match:
                    container_id = self.host._run("ps", "-q", component)
                    if re.fullmatch(r"[0-9a-f]{12,64}", container_id) is None:
                        raise TestUpdateManagerError(
                            "rollback container identity is invalid"
                        )
                    current_image = self._command(
                        "docker",
                        "inspect",
                        "--format",
                        "{{.Image}}",
                        container_id,
                    )
                    if current_image != existing[0]:
                        raise TestUpdateManagerError(
                            "rollback image identity drifted"
                        )
                continue
            container_id = self.host._run("ps", "-q", component)
            if re.fullmatch(r"[0-9a-f]{12,64}", container_id) is None:
                raise TestUpdateManagerError("rollback container identity is invalid")
            image_id = self._command(
                "docker",
                "inspect",
                "--format",
                "{{.Image}}",
                container_id,
            )
            if re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
                raise TestUpdateManagerError("rollback image identity is invalid")
            self._command("docker", "image", "tag", image_id, rollback_ref)

    def run_expand_migration(self, source: str, target: str) -> str:
        self._activate_source_and_image("api")
        self._prepare_rebaseline_runtime_secrets()
        check_expand_only(self.root / "backend/migrations/versions", source, target)
        if source != target:
            self.host._run("run", "--rm", "migrate")
        return self.current_migration_head()

    def replace_backend_services(self, services: tuple[str, ...]) -> None:
        if services != BACKEND_SERVICES or "mock-vendor" in services:
            raise TestUpdateManagerError("backend service plan is invalid")
        self._prepare_rollback_images(self.request.components)
        self._activate_source_and_image("api")
        if self.request.public_cutover is not None:
            self.host.activate_public_cutover_redis()
        self.host._run(
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            *services,
        )
        if "web" in self.request.components:
            self._activate_source_and_image("web")
            self._render_trusted_proxy_conf()
            self.host._run(
                "up",
                "-d",
                "--no-deps",
                "--force-recreate",
                "--wait",
                "--wait-timeout",
                "120",
                "web",
            )

    def replace_web(self) -> None:
        self._prepare_rollback_images(frozenset({"web"}))
        self._activate_source_and_image("web")
        self._render_trusted_proxy_conf()
        self.host._run(
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            "120",
            "web",
        )

    def _render_trusted_proxy_conf(self) -> None:
        """启动 web 前渲染受信 TLS 终结器配置，并保证容器 uid 101 可读。"""

        env_path = self.root / ".env"
        try:
            raw = env_path.read_text(encoding="utf-8")
            lines, _positions = _parse_dotenv(raw)
        except (OSError, UnicodeError, VendorTestFileError) as exc:
            raise TestUpdateManagerError("trusted proxy dotenv is unavailable") from exc
        values: dict[str, str] = {}
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            key, value = line.split("=", 1)
            if key in _TRUSTED_PROXY_DOTENV_KEYS:
                values[key] = value
        if "SMS_EXTERNAL_TLS_MODE" not in values:
            raise TestUpdateManagerError(
                "trusted proxy mode must be explicit in root dotenv"
            )
        output = values.get("SMS_TRUSTED_PROXY_CONF") or _TRUSTED_PROXY_RUNTIME_CONF
        self.host.runner.run(
            [
                "/usr/bin/python3",
                str(self.root / "deploy/scripts/render_trusted_proxy_conf.py"),
                "--mode",
                values["SMS_EXTERNAL_TLS_MODE"],
                "--cidrs",
                values.get("SMS_TRUSTED_PROXY_CIDRS", ""),
                "--output",
                output,
            ]
        )

    def rollback_no_migration(
        self,
        kind: str,
        update_id: str,
    ) -> tuple[str, str]:
        return self._rollback_no_migration(
            kind,
            update_id,
            cleanup_images=True,
        )

    def rollback_no_migration_preserving_images(
        self,
        kind: str,
        update_id: str,
    ) -> tuple[str, str]:
        """完成物理回退但保留旧镜像标签，供外层先耐久记录终态。"""

        return self._rollback_no_migration(
            kind,
            update_id,
            cleanup_images=False,
        )

    def _rollback_no_migration(
        self,
        kind: str,
        update_id: str,
        *,
        cleanup_images: bool,
    ) -> tuple[str, str]:
        if update_id != self.request.update_id or kind not in _UPDATE_KINDS:
            raise TestUpdateManagerError("rollback request is invalid")
        if self.current_migration_head() != self.request.migration_from:
            raise TestUpdateManagerError("rollback cannot cross a migration")
        if kind == "backend-safe":
            self.hold_fail_closed(update_id)
        components = (
            self.request.components
            if kind == "backend-safe"
            else frozenset({"web"})
        )
        self._command(
            "git",
            "-C",
            str(self.root),
            "checkout",
            "--detach",
            self.request.base_commit,
        )
        _restore_operator_git_read_access(self.root)
        for component in sorted(components):
            image_id = self._command(
                "docker",
                "image",
                "inspect",
                "--format",
                "{{.Id}}",
                self._rollback_ref(component),
            )
            _atomic_update_image_env(
                self.root / ".env",
                _IMAGE_ENV_KEYS[component],
                image_id,
                expected_uid=self.expected_uid,
            )
        if kind == "backend-safe":
            self.host._run(
                "up",
                "-d",
                "--no-deps",
                "--force-recreate",
                *BACKEND_SERVICES,
            )
            if "web" in components:
                self.host._run(
                    "up",
                    "-d",
                    "--no-deps",
                    "--force-recreate",
                    "--wait",
                    "--wait-timeout",
                    "120",
                    "web",
                )
            self.verify_backend_services()
            self.restore_owned_update_pauses(update_id)
        else:
            self.host._run(
                "up",
                "-d",
                "--no-deps",
                "--force-recreate",
                "--wait",
                "--wait-timeout",
                "120",
                "web",
            )
            self.verify_web()
        if cleanup_images:
            self.cleanup_rollback_images(update_id)
        return self.request.base_commit, self.request.migration_from

    def cleanup_rollback_images(self, update_id: str) -> None:
        if update_id != self.request.update_id:
            raise TestUpdateManagerError("update ID mismatch")
        for component in sorted(self.request.components):
            with contextlib.suppress(Exception):
                self._command(
                    "docker",
                    "image",
                    "rm",
                    self._rollback_ref(component),
                )

    def cleanup_rollback_images_verified(self, update_id: str) -> None:
        """严格删除并复核本次回退标签；用于已验收的基线清理。"""

        if update_id != self.request.update_id:
            raise TestUpdateManagerError("update ID mismatch")
        for component in sorted(self.request.components):
            rollback_ref = self._rollback_ref(component)
            existing = self._command(
                "docker",
                "image",
                "ls",
                "--no-trunc",
                "--quiet",
                rollback_ref,
            ).splitlines()
            if not existing:
                continue
            if (
                len(existing) != 1
                or re.fullmatch(r"sha256:[0-9a-f]{64}", existing[0]) is None
            ):
                raise TestUpdateManagerError(
                    "rollback image identity is invalid"
                )
            self._command("docker", "image", "rm", rollback_ref)
            remaining = self._command(
                "docker",
                "image",
                "ls",
                "--no-trunc",
                "--quiet",
                rollback_ref,
            )
            if remaining:
                raise TestUpdateManagerError(
                    "rollback image cleanup did not verify"
                )

    def recover_blocked_rebaseline_prepare(
        self,
        store: TestUpdateStore,
    ) -> None:
        """越过已误用的旧密钥扩展门禁，复用同一 checkpoint 与镜像。"""

        self._require_approved_rebaseline_migration()
        if (
            self.request.migration_from != _REBASELINE_RESUME_MIGRATION_FROM
            or self.request.migration_target != _REBASELINE_RESUME_MIGRATION_TARGET
        ):
            raise TestUpdateManagerError(
                "blocked rebaseline prepare recovery scope is invalid"
            )
        self.require_lifecycle_lock()
        state = store.read_consistent_state()
        if (
            state.get("state") != TestUpdateState.BLOCKED.value
            or state.get("step") != "secret_expansion"
            or state.get("error_type") != "validation_failed"
            or state.get("actual_commit") != self.request.commit
            or state.get("actual_migration_head") != self.request.migration_from
            or state.get("event_sequence") != 2
        ):
            raise TestUpdateManagerError(
                "blocked rebaseline prepare recovery state is invalid"
            )
        if self.host_source_commit is None:
            raise TestUpdateManagerError(
                "blocked rebaseline prepare recovery snapshot is invalid"
            )
        secret_contract_diff = self._command(
            "git",
            "-C",
            str(self.root),
            "diff",
            "--name-only",
            "--no-renames",
            self.request.base_commit,
            self.request.commit,
            "--",
            "deploy/docker-compose.yml",
            "deploy/scripts/prepare_runtime_secrets.py",
            "deploy/scripts/vendor_runtime_reset.py",
        )
        if secret_contract_diff:
            raise TestUpdateManagerError(
                "blocked rebaseline prepare recovery secret contract changed"
            )
        head = self._command("git", "-C", str(self.root), "rev-parse", "HEAD")
        git_status = self._command(
            "git", "-C", str(self.root), "status", "--porcelain"
        )
        if (
            head != self.request.base_commit
            or git_status
            or self.current_migration_head() != self.request.migration_from
        ):
            raise TestUpdateManagerError(
                "blocked rebaseline prepare recovery identity is invalid"
            )
        self.require_owned_update_pauses(self.request.update_id)
        _require_no_unsafe_chunks(
            self.unsafe_status_counts(),
            include_uncertain=True,
        )
        self.require_loaded_request_images()
        store.transition(
            TestUpdateState.BLOCKED,
            TestUpdateState.CHECKPOINTED,
            step="recover_prepare",
            actual_commit=self.request.commit,
            actual_migration_head=self.request.migration_from,
        )

    def recover_blocked_rebaseline_before_migration(
        self,
        store: TestUpdateStore,
    ) -> None:
        """幂等恢复尚未迁移的 rebaseline checkout/image 指针，保持暂停与日志。"""

        self._require_approved_rebaseline_migration()
        self.require_lifecycle_lock()
        state = store.read_consistent_state()
        if (
            state.get("state") != TestUpdateState.BLOCKED.value
            or state.get("step") != "migrate"
            or state.get("error_type") != "step_failed"
            or state.get("actual_commit") != self.request.commit
            or state.get("actual_migration_head") != self.request.migration_from
        ):
            raise TestUpdateManagerError(
                "blocked rebaseline recovery state is invalid"
            )
        migration_head = self.current_migration_head()
        head = self._command("git", "-C", str(self.root), "rev-parse", "HEAD")
        git_status = self._command(
            "git", "-C", str(self.root), "status", "--porcelain"
        )
        _restore_operator_git_read_access(self.root)
        if (
            migration_head != self.request.migration_from
            or head not in {self.request.commit, self.request.base_commit}
            or git_status
        ):
            raise TestUpdateManagerError(
                "blocked rebaseline recovery identity is invalid"
            )
        self.require_owned_update_pauses(self.request.update_id)

        running_images: dict[str, str] = {}
        for component in ("api", "web"):
            container_id = self.host._run("ps", "-q", component)
            if re.fullmatch(r"[0-9a-f]{12,64}", container_id) is None:
                raise TestUpdateManagerError(
                    "blocked rebaseline running service is unavailable"
                )
            observed = self._command(
                "docker",
                "inspect",
                "--format",
                (
                    "{{.Image}}|"
                    '{{index .Config.Labels "org.opencontainers.image.revision"}}|'
                    '{{index .Config.Labels "com.sms-platform.schema-revision"}}'
                ),
                container_id,
            ).split("|")
            if (
                len(observed) != 3
                or re.fullmatch(r"sha256:[0-9a-f]{64}", observed[0]) is None
                or observed[1] != self.request.base_commit
                or observed[2] != self.request.migration_from
            ):
                raise TestUpdateManagerError(
                    "blocked rebaseline running image identity is invalid"
                )
            running_images[component] = observed[0]

        for component, image_id in running_images.items():
            _atomic_update_image_env(
                self.root / ".env",
                _IMAGE_ENV_KEYS[component],
                image_id,
                expected_uid=self.expected_uid,
            )
        if head == self.request.commit:
            try:
                self._command(
                    "git",
                    "-C",
                    str(self.root),
                    "checkout",
                    "--detach",
                    self.request.base_commit,
                )
            finally:
                _restore_operator_git_read_access(self.root)
        final_head = self._command(
            "git", "-C", str(self.root), "rev-parse", "HEAD"
        )
        final_status = self._command(
            "git", "-C", str(self.root), "status", "--porcelain"
        )
        _restore_operator_git_read_access(self.root)
        final_migration_head = self.current_migration_head()
        if (
            final_head != self.request.base_commit
            or final_status
            or final_migration_head != self.request.migration_from
        ):
            raise TestUpdateManagerError(
                "blocked rebaseline recovery verification failed"
            )

    def recover_blocked_rebaseline_verify(
        self,
        store: TestUpdateStore,
    ) -> str:
        """仅为迁移后 GetBalance 阻断重开同一 update 的完整 verify。"""

        self._require_approved_rebaseline_migration()
        self.require_lifecycle_lock()
        if self.host_source_commit is None:
            raise TestUpdateManagerError(
                "blocked rebaseline verify recovery snapshot is invalid"
            )
        state = store.read_consistent_state()
        state_value = state.get("state")
        blocked_identity = (
            state_value == TestUpdateState.BLOCKED.value
            and state.get("error_type") == "invariant_failed"
            and state.get("actual_commit") == self.request.commit
            and state.get("actual_migration_head") == self.request.migration_target
        )
        initial_blocked_state = blocked_identity and state.get("step") == "get_balance"
        retry_blocked_state = False
        if (
            not initial_blocked_state
            and blocked_identity
            and state.get("step") in _REBASELINE_VERIFY_STEPS
        ):
            events = store.read_events()
            if len(events) >= 2:
                recovering_event, blocked_event = events[-2:]
                retry_blocked_state = (
                    recovering_event.get("from_state")
                    == TestUpdateState.BLOCKED.value
                    and recovering_event.get("to_state")
                    == TestUpdateState.VERIFYING.value
                    and recovering_event.get("step") == "recover_verify"
                    and recovering_event.get("error_type") is None
                    and recovering_event.get("actual_commit") == self.request.commit
                    and recovering_event.get("actual_migration_head")
                    == self.request.migration_target
                    and blocked_event.get("from_state")
                    == TestUpdateState.VERIFYING.value
                    and blocked_event.get("to_state")
                    == TestUpdateState.BLOCKED.value
                    and blocked_event.get("step") == state.get("step")
                    and blocked_event.get("error_type") == "invariant_failed"
                    and blocked_event.get("actual_commit") == self.request.commit
                    and blocked_event.get("actual_migration_head")
                    == self.request.migration_target
                )
        blocked_state = initial_blocked_state or retry_blocked_state
        recovering_state = (
            state_value == TestUpdateState.VERIFYING.value
            and state.get("step") == "recover_verify"
            and state.get("error_type") is None
            and state.get("actual_commit") == self.request.commit
            and state.get("actual_migration_head") == self.request.migration_target
        )
        verified_state = (
            state_value == TestUpdateState.VERIFIED.value
            and state.get("step") == "verify"
            and state.get("error_type") is None
            and state.get("actual_commit") == self.request.commit
            and state.get("actual_migration_head") == self.request.migration_target
        )
        if not (blocked_state or recovering_state or verified_state):
            raise TestUpdateManagerError(
                "blocked rebaseline verify recovery state is invalid"
            )

        try:
            head = self._command("git", "-C", str(self.root), "rev-parse", "HEAD")
            git_status = self._command(
                "git", "-C", str(self.root), "status", "--porcelain"
            )
        finally:
            _restore_operator_git_read_access(self.root)
        if (
            head != self.request.commit
            or git_status
            or self.current_migration_head() != self.request.migration_target
        ):
            raise TestUpdateManagerError(
                "blocked rebaseline verify recovery identity is invalid"
            )
        if verified_state:
            return "verified"

        self.require_owned_update_pauses(self.request.update_id)
        if blocked_state:
            store.transition(
                TestUpdateState.BLOCKED,
                TestUpdateState.VERIFYING,
                step="recover_verify",
                actual_commit=self.request.commit,
                actual_migration_head=self.request.migration_target,
            )
        TestUpdateVerify(store, self).verify(
            "backend-safe",
            update_id=self.request.update_id,
            commit=self.request.commit,
            migration_from=self.request.migration_from,
            migration_target=self.request.migration_target,
            expected_state=TestUpdateState.VERIFYING,
        )
        return "verified"

    def verify_web(self) -> None:
        running = set(self.host._run("ps", "--status", "running", "--services").splitlines())
        if "web" not in running:
            raise TestUpdateManagerError("web is not running")

    def verify_budget_conservation(self) -> None:
        invalid = self.host._psql(
            "SELECT count(*) FROM vendor_test_daily_usage WHERE "
            "confirmed_segments<0 OR in_flight_segments<0 OR uncertain_segments<0 "
            "OR confirmed_segments+in_flight_segments+uncertain_segments>100"
        )
        if invalid != "0":
            raise TestUpdateManagerError("vendor budget conservation failed")

    def verify_pause_state(self) -> None:
        self.require_owned_update_pauses(self.request.update_id)

    def probe_balance(self) -> None:
        self.host.probe_balance()

    def recover_backend_services(self) -> None:
        """GetBalance 成功后仅重启被 fail-closed 停止的既有后端服务。"""

        self._require_approved_rebaseline_migration()
        self.require_owned_update_pauses(self.request.update_id)
        self.host._run(
            "up",
            "-d",
            "--no-deps",
            *BACKEND_SERVICES,
        )

    def restore_operator_git_read_access(self) -> None:
        """在 root 恢复动作后安全恢复 operator 的 Git 只读路径。"""

        self._require_approved_rebaseline_migration()
        _restore_operator_git_read_access(self.root)

    def recover_verified_rebaseline_services(self) -> None:
        """幂等收尾 verified 恢复遗留的成对 update 暂停。"""

        self._require_approved_rebaseline_migration()
        script = (
            "local a=redis.call('get',KEYS[1]); "
            "local b=redis.call('get',KEYS[2]); "
            "if a==ARGV[1] and b==ARGV[1] then return 'owned' end; "
            "if not a and not b then return 'released' end; return 'blocked'"
        )
        observed = self.host._redis(
            "EVAL",
            script,
            "2",
            "queue:paused:realtime",
            "queue:paused:bulk",
            self.pause_value,
        )
        if observed == "owned":
            self.host._run(
                "up",
                "-d",
                "--no-deps",
                *BACKEND_SERVICES,
            )
            self.verify_backend_services()
            self.restore_owned_update_pauses(self.request.update_id)
            return
        if observed == "released":
            self.verify_backend_services()
            return
        raise TestUpdateManagerError(
            "verified rebaseline update pause state is invalid"
        )

    def verify_backend_services(self) -> None:
        running = set(self.host._run("ps", "--status", "running", "--services").splitlines())
        if not set(BACKEND_SERVICES).issubset(running):
            raise TestUpdateManagerError("backend services are not all running")
        if "web" in self.request.components and "web" not in running:
            raise TestUpdateManagerError("selected web service is not running")
        if self.current_migration_head() != self.request.migration_target:
            raise TestUpdateManagerError("migration head drifted")

    def verify_running_image_identity(self) -> None:
        """复核当前 API/Web 容器仍绑定本次 rebaseline 的精确镜像。"""

        for component in sorted(self.request.components):
            image = self.request.images[component]
            container_id = self.host._run("ps", "-q", component)
            if re.fullmatch(r"[0-9a-f]{12,64}", container_id) is None:
                raise TestUpdateManagerError(
                    "rebaseline running service is unavailable"
                )
            observed = self._command(
                "docker",
                "inspect",
                "--format",
                (
                    "{{.Image}}|"
                    '{{index .Config.Labels "org.opencontainers.image.revision"}}|'
                    '{{index .Config.Labels "com.sms-platform.schema-revision"}}'
                ),
                container_id,
            )
            if observed != (
                f"{image.image_id}|{self.request.commit}|"
                f"{self.request.migration_target}"
            ):
                raise TestUpdateManagerError(
                    "rebaseline running image identity is invalid"
                )

    def restore_owned_update_pauses(self, update_id: str) -> None:
        if update_id != self.request.update_id:
            raise TestUpdateManagerError("update ID mismatch")
        script = (
            "if redis.call('get',KEYS[1]) == ARGV[1] then "
            "return redis.call('del',KEYS[1]) else return 0 end"
        )
        for lane in ("realtime", "bulk"):
            self.host._redis(
                "EVAL",
                script,
                "1",
                f"queue:paused:{lane}",
                self.pause_value,
            )


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="test_update_manager.py")
    parser.add_argument(
        "command",
        choices=(
            "prepare",
            "apply",
            "verify",
            "status",
            "capability",
            "recover-rebaseline",
            "recover-rebaseline-prepare",
            "recover-rebaseline-verify",
        ),
    )
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--marker-file", required=True, type=Path)
    return parser


def _require_cli_contract(args: argparse.Namespace) -> None:
    expected_state = Path("/var/lib/sms-platform/test-updates")
    if (
        args.state_root != expected_state
        or args.request != expected_state / "incoming/request.json"
    ):
        raise TestUpdateManagerError("test update state paths are invalid")
    if args.marker_file != Path("/etc/sms-platform/test-environment"):
        raise TestUpdateManagerError("test update marker path is invalid")
    for path in (args.root, args.runtime_root, args.state_root, args.request, args.marker_file):
        if not path.is_absolute() or ".." in path.parts:
            raise TestUpdateManagerError("test update path is invalid")


def _kind(request: TestUpdateRequest) -> str:
    return "backend-safe" if "api" in request.components else "web-only"


def main(argv: list[str] | None = None) -> int:
    args = _build_cli_parser().parse_args(argv)
    try:
        _require_cli_contract(args)
        host_source_commit = os.environ.get("SMS_HOST_SOURCE_COMMIT")
        if (
            host_source_commit is not None
            and re.fullmatch(r"[0-9a-f]{40}", host_source_commit) is None
        ):
            raise TestUpdateManagerError("host source commit is invalid")
        if args.command == "capability":
            capability: dict[str, object] = {
                "schema_version": 2,
                "host_control_snapshot": True,
            }
            if host_source_commit is not None:
                capability["source_commit"] = host_source_commit
            print(
                json.dumps(
                    capability,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            return 0
        raw, request = _read_private_request(args.request, expected_uid=0)
        store = TestUpdateStore(args.state_root, request.update_id)
        operations = HostTestUpdateOperations(
            root=args.root,
            runtime_root=args.runtime_root,
            marker_file=args.marker_file,
            state_root=args.state_root,
            request=request,
            host_source_commit=host_source_commit,
            expected_uid=0,
        )
        if args.command == "status":
            state = (
                store.read_consistent_state()
                if store.update_dir.exists()
                else {"state": "incoming"}
            )
            print(
                json.dumps(
                    {
                        "update_id": request.update_id,
                        "state": state["state"],
                        "actual_commit": operations._command(
                            "git", "-C", str(args.root), "rev-parse", "HEAD"
                        ),
                        "actual_migration_head": operations.current_migration_head(),
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            return 0

        operations.require_lifecycle_lock()
        if args.command == "recover-rebaseline":
            operations.recover_blocked_rebaseline_before_migration(store)
            print(
                json.dumps(
                    {
                        "status": "recovered",
                        "update_id": request.update_id,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "recover-rebaseline-prepare":
            operations.recover_blocked_rebaseline_prepare(store)
            print(
                json.dumps(
                    {
                        "commit": request.commit,
                        "migration_target": request.migration_target,
                        "status": "checkpointed",
                        "update_id": request.update_id,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "recover-rebaseline-verify":
            recovery_status = operations.recover_blocked_rebaseline_verify(store)
            print(
                json.dumps(
                    {
                        "status": recovery_status,
                        "update_id": request.update_id,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            return 0
        kind = _kind(request)
        if kind not in _UPDATE_KINDS:
            raise TestUpdateManagerError("update kind is invalid")
        if args.command == "prepare":
            store.create(raw)
            _prepare_from_source(store, operations, request=request)
        elif args.command == "apply":
            TestUpdateApply(store, operations).apply(
                kind,  # type: ignore[arg-type]
                update_id=request.update_id,
                commit=request.commit,
                migration_from=request.migration_from,
                migration_target=request.migration_target,
            )
        else:
            TestUpdateVerify(store, operations).verify(
                kind,  # type: ignore[arg-type]
                update_id=request.update_id,
                commit=request.commit,
                migration_from=request.migration_from,
                migration_target=request.migration_target,
            )
        print(json.dumps({"update_id": request.update_id, "status": args.command}))
        return 0
    except Exception:
        print(f"test-update {args.command} blocked", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
