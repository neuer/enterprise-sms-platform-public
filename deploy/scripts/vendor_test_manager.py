#!/usr/bin/env python3
"""一次性从 Mock 切换到真实厂商的 fail-closed 编排器。"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import stat
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, Self

from test_update_backup import (
    BackupConfig,
    Runner,
    TestUpdateBackup,
    require_inherited_lifecycle_lock,
)
from vendor_credential_store import (
    CredentialStoreError,
    RotationTransaction,
    VendorCredentialStore,
)
from vendor_test_files import (
    VendorTestFileError,
    activate_vendor_live_dotenv,
    read_vendor_test_marker,
    reconcile_pure_mock_dotenv,
    require_pii_free_evidence,
    write_pii_free_evidence,
    write_vendor_test_marker,
)

ACTIVE_STATUSES = ("queued", "scheduled", "submitting", "retrying", "uncertain")
_CREDENTIAL_FILES = ("vendor_secret_name", "vendor_secret_key")
_PAUSE_VALUE = "vendor-test-activation"
_MANUAL_PAUSE_VALUE = "vendor-test-manual"
_ROTATION_FAILURE_VALUE = "vendor-test-rotation-failed"
_DATABASE = "sms"
_BACKUP_OUTPUT_ROOT = Path("/var/lib/sms-platform/test-backups")
_BACKUP_KEY_FILE = Path("/etc/sms-platform/test-update-backup-key")
_BACKEND_RUNTIME_GID = 10001
_BACKUP_CONFIG_FIELDS = frozenset(
    {"schema_version", "output_root", "key_file", "database"}
)


class VendorTestActivationError(RuntimeError):
    """真实厂商切换被安全门禁阻断；异常不携带底层敏感详情。"""


class VendorTestProbeRejected(VendorTestActivationError):
    """GetBalance 被厂商拒绝，只携带可告知操作者的整数错误码。"""

    def __init__(self, vendor_code: int) -> None:
        if type(vendor_code) is not int or not 1 <= vendor_code <= 99_999:
            raise VendorTestActivationError("vendor probe error code is invalid")
        self.vendor_code = vendor_code
        super().__init__(f"vendor probe rejected: code={vendor_code}")


class BalanceClient(Protocol):
    async def __aenter__(self) -> Self: ...

    async def __aexit__(self, *_args: object) -> None: ...

    async def get_balance(self) -> int: ...


class ActivationOperations(Protocol):
    """由固定 wrapper 实现的受控宿主操作集合。"""

    def require_lifecycle_lock(self) -> None: ...

    def pause_activation_lanes(self) -> None: ...

    def stop_senders(self) -> None: ...

    def active_status_counts(self) -> Mapping[str, int]: ...

    def create_encrypted_checkpoint(self) -> str: ...

    def active_recipient_count(self) -> int: ...

    def remove_mock_vendor(self) -> None: ...

    def validate_compose(self) -> None: ...

    def start_core(self) -> None: ...

    def probe_balance(self) -> BalanceProbe: ...

    def budget_snapshot(self) -> BudgetSnapshot: ...

    def start_senders(self) -> None: ...

    def clear_activation_pause(self) -> None: ...

    def hold_fail_closed(self) -> None: ...


class PauseOperations(Protocol):
    """人工暂停和恢复只操作固定暂停键，并复用同一生命周期锁。"""

    def require_lifecycle_lock(self) -> None: ...

    def current_pause_kind(self) -> str | None: ...

    def set_manual_pause(self) -> None: ...

    def probe_balance(self) -> BalanceProbe: ...

    def clear_pause(self, pause_kind: str) -> None: ...


class RotationOperations(ActivationOperations, Protocol):
    def current_pause_kind(self) -> str | None: ...

    def prepare_runtime_secrets(self) -> None: ...

    def rebuild_backend(self) -> None: ...


class RotationCredentialStore(Protocol):
    def begin_rotation(self) -> RotationTransaction: ...

    def commit_rotation(self, transaction: RotationTransaction) -> object: ...

    def rollback_to_previous(
        self,
        transaction: RotationTransaction,
    ) -> RotationTransaction: ...

    def complete_rollback(self, transaction: RotationTransaction) -> object: ...

    def read_rotation_transaction(self) -> RotationTransaction | None: ...

    def discard_pending(self) -> None: ...

    def recover_pending(self) -> str: ...


@dataclass(frozen=True, slots=True)
class ActivationPaths:
    runtime_root: Path
    credential_source: Path
    marker_file: Path
    dotenv_file: Path
    evidence_file: Path


@dataclass(frozen=True, slots=True)
class BalanceProbe:
    code: int
    message: str
    balance: int
    observed_at: str
    correlation_id: str


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    confirmed_segments: int
    in_flight_segments: int
    uncertain_segments: int


@dataclass(frozen=True, slots=True)
class ActivationResult:
    status: str
    checkpoint_id: str
    balance: int


@dataclass(frozen=True, slots=True)
class PauseResult:
    status: str
    pause_kind: str


@dataclass(frozen=True, slots=True)
class RotationResult:
    status: str
    checkpoint_id: str
    balance: int


class FixedCommandRunner(Runner):
    """仅执行调用方提供的 argv；错误不回显子进程输出。"""

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
            raise VendorTestActivationError("controlled command is unavailable") from exc
        if result.returncode != 0:
            raise VendorTestActivationError(
                f"controlled command failed: {Path(command[0]).name}"
            )
        return result.stdout


def _reject_backup_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise VendorTestActivationError("backup config is unsafe")
        result[key] = value
    return result


def _decode_output(payload: bytes) -> str:
    try:
        return payload.decode("utf-8").strip()
    except UnicodeError as exc:
        raise VendorTestActivationError("controlled command output is invalid") from exc


def _read_strict_backup_config(path: Path, *, expected_uid: int) -> tuple[Path, Path]:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or stat.S_IMODE(metadata.st_mode) not in {0o400, 0o600}
        ):
            raise VendorTestActivationError("backup config is unsafe")
        decoded = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_backup_duplicates,
        )
    except VendorTestActivationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VendorTestActivationError("backup config is unsafe") from exc
    if type(decoded) is not dict or set(decoded) != set(_BACKUP_CONFIG_FIELDS):
        raise VendorTestActivationError("backup config is unsafe")
    if type(decoded["schema_version"]) is not int or decoded["schema_version"] != 1:
        raise VendorTestActivationError("backup config is unsafe")
    if decoded["database"] != _DATABASE or type(decoded["database"]) is not str:
        raise VendorTestActivationError("backup config is unsafe")
    output_root = decoded["output_root"]
    key_file = decoded["key_file"]
    if type(output_root) is not str or type(key_file) is not str:
        raise VendorTestActivationError("backup config is unsafe")
    output_path = Path(output_root)
    key_path = Path(key_file)
    if (
        not output_path.is_absolute()
        or not key_path.is_absolute()
        or ".." in output_path.parts
        or ".." in key_path.parts
        or output_path != _BACKUP_OUTPUT_ROOT
        or key_path != _BACKUP_KEY_FILE
    ):
        raise VendorTestActivationError("backup config is unsafe")
    return output_path, key_path


class HostActivationOperations:
    """将 activation 协议绑定到固定 Compose、Redis 与 PostgreSQL 命令。"""

    def __init__(
        self,
        *,
        root: Path,
        runtime_root: Path,
        backup_config_file: Path,
        expected_uid: int = 0,
        runner: FixedCommandRunner | None = None,
    ) -> None:
        self.root = root
        self.runtime_root = runtime_root
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
        self.backup_config_file = backup_config_file
        self.correlation_id = f"activation-{uuid.uuid4().hex}"

    def _run(self, *arguments: str, input_bytes: bytes | None = None) -> str:
        return _decode_output(
            self.runner.run(
                [*self.compose, *arguments],
                input_bytes=input_bytes,
            )
        )

    def _redis(self, *arguments: str) -> str:
        return self._run(
            "exec",
            "-T",
            "redis-control",
            "sh",
            "-ec",
            (
                'exec redis-cli --user sms_control --askpass --raw "$@" '
                "< /run/secrets/redis_control_password"
            ),
            "sh",
            *arguments,
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

    def pause_activation_lanes(self) -> None:
        for lane in ("realtime", "bulk"):
            key = f"queue:paused:{lane}"
            current = self._redis("GET", key)
            if current == _PAUSE_VALUE:
                continue
            if current:
                raise VendorTestActivationError("an existing queue pause must be resolved")
            if self._redis("SET", key, _PAUSE_VALUE, "NX") != "OK":
                raise VendorTestActivationError("activation pause could not be acquired")

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

    def active_status_counts(self) -> dict[str, int]:
        sql = """
        SELECT 'queued|'||count(*) FROM sms_batch WHERE status='queued'
        UNION ALL SELECT 'scheduled|'||count(*) FROM sms_batch WHERE status='scheduled'
        UNION ALL SELECT 'submitting|'||count(*) FROM sms_chunk WHERE status='submitting'
        UNION ALL SELECT 'retrying|'||count(*) FROM sms_chunk WHERE status='retrying'
        UNION ALL SELECT 'uncertain|'||count(*) FROM sms_chunk WHERE status='uncertain'
        """
        result: dict[str, int] = {}
        for line in self._psql(sql).splitlines():
            fields = line.split("|")
            if len(fields) != 2 or fields[0] not in ACTIVE_STATUSES:
                raise VendorTestActivationError("active status observation is invalid")
            try:
                result[fields[0]] = int(fields[1])
            except ValueError as exc:
                raise VendorTestActivationError(
                    "active status observation is invalid"
                ) from exc
        return result

    def create_encrypted_checkpoint(self) -> str:
        output_root, key_file = _read_strict_backup_config(
            self.backup_config_file,
            expected_uid=self.expected_uid,
        )
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        checkpoint_id = f"vendor-activation-{timestamp}-{uuid.uuid4().hex[:12]}"
        config = BackupConfig(
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
        )
        result = TestUpdateBackup(self.runner).create(config, checkpoint_id)
        if not result.complete:
            raise VendorTestActivationError("encrypted checkpoint is incomplete")
        return checkpoint_id

    def active_recipient_count(self) -> int:
        raw = self._psql(
            "SELECT count(*) FROM vendor_test_recipient WHERE status='active'"
        )
        try:
            count = int(raw)
        except ValueError:
            raise VendorTestActivationError("active recipient count is invalid") from None
        if count < 0:
            raise VendorTestActivationError("active recipient count is invalid")
        return count

    def remove_mock_vendor(self) -> None:
        self._run("rm", "-s", "-f", "mock-vendor")

    def validate_compose(self) -> None:
        self._run("config", "--quiet")

    def start_core(self) -> None:
        self._run(
            "up",
            "-d",
            "--wait",
            "--wait-timeout",
            "120",
            "postgres",
            "redis",
            "migrate",
            "api",
        )

    def probe_balance(self) -> BalanceProbe:
        program = """import asyncio
import json
from app.vendor.zhihui import VendorApiError, ZhihuiClient

async def probe():
    try:
        async with ZhihuiClient.from_settings() as client:
            balance = await client.get_balance()
    except VendorApiError as error:
        print(json.dumps({"code": error.code}))
    else:
        print(json.dumps({"balance": balance}))

asyncio.run(probe())
"""
        raw = self._run(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "worker-realtime",
            "python",
            "-c",
            program,
        )
        try:
            decoded = json.loads(raw.splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:
            raise VendorTestActivationError("GetBalance probe is invalid") from exc
        if type(decoded) is not dict:
            raise VendorTestActivationError("GetBalance probe is invalid")
        if set(decoded) == {"code"}:
            raise VendorTestProbeRejected(decoded["code"])
        if set(decoded) != {"balance"}:
            raise VendorTestActivationError("GetBalance probe is invalid")
        balance = decoded["balance"]
        if type(balance) is not int or balance < 0:
            raise VendorTestActivationError("GetBalance probe is invalid")
        return BalanceProbe(
            0,
            "ok",
            balance,
            _utc_timestamp(datetime.now(UTC)),
            self.correlation_id,
        )

    def budget_snapshot(self) -> BudgetSnapshot:
        raw = self._psql(
            "SELECT confirmed_segments||'|'||in_flight_segments||'|'||"
            "uncertain_segments FROM vendor_test_daily_usage "
            "WHERE usage_date=(now() AT TIME ZONE 'Asia/Shanghai')::date"
        )
        if not raw:
            return BudgetSnapshot(0, 0, 0)
        fields = raw.split("|")
        if len(fields) != 3:
            raise VendorTestActivationError("vendor budget observation is invalid")
        try:
            values = tuple(int(field) for field in fields)
        except ValueError as exc:
            raise VendorTestActivationError("vendor budget observation is invalid") from exc
        return BudgetSnapshot(*values)

    def start_senders(self) -> None:
        self._run(
            "up",
            "-d",
            "--no-deps",
            "worker-realtime",
            "worker-report",
            "worker-bulk",
            "worker-callback",
            "outbox-dispatcher",
            "beat",
        )

    def prepare_runtime_secrets(self) -> None:
        self.runner.run(
            [
                "python3",
                str(self.root / "deploy/scripts/prepare_runtime_secrets.py"),
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

    def rebuild_backend(self) -> None:
        self._run(
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            "120",
            "api",
            "worker-realtime",
            "worker-report",
            "worker-bulk",
            "worker-callback",
            "outbox-dispatcher",
            "beat",
        )

    def _delete_owned_pause(self, lane: str) -> None:
        script = (
            "if redis.call('get',KEYS[1]) == ARGV[1] then "
            "return redis.call('del',KEYS[1]) else return 0 end"
        )
        self._redis(
            "EVAL",
            script,
            "1",
            f"queue:paused:{lane}",
            _PAUSE_VALUE,
        )

    def clear_activation_pause(self) -> None:
        for lane in ("realtime", "bulk"):
            self._delete_owned_pause(lane)

    def hold_fail_closed(self) -> None:
        for lane in ("realtime", "bulk"):
            with contextlib.suppress(Exception):
                self._redis(
                    "SET",
                    f"queue:paused:vendor-test-rotation-failed:{lane}",
                    _ROTATION_FAILURE_VALUE,
                )
            with contextlib.suppress(Exception):
                self._redis("SET", f"queue:paused:{lane}", _PAUSE_VALUE, "NX")
        with contextlib.suppress(Exception):
            self.stop_senders()

    def current_pause_kind(self) -> str | None:
        stale = [
            self._redis("GET", f"queue:paused:vendor-test-agent-stale:{lane}")
            for lane in ("realtime", "bulk")
        ]
        rotation_failed = [
            self._redis("GET", f"queue:paused:vendor-test-rotation-failed:{lane}")
            for lane in ("realtime", "bulk")
        ]
        primary = [
            self._redis("GET", f"queue:paused:{lane}")
            for lane in ("realtime", "bulk")
        ]
        if (
            any(stale)
            or any(rotation_failed)
            or any(value and value != _MANUAL_PAUSE_VALUE for value in primary)
        ):
            return "critical"
        daily = [
            self._redis("GET", f"queue:paused:vendor-test-daily:{lane}")
            for lane in ("realtime", "bulk")
        ]
        if any(daily):
            return "daily"
        if primary == [_MANUAL_PAUSE_VALUE, _MANUAL_PAUSE_VALUE]:
            return "manual"
        if any(primary):
            raise VendorTestActivationError("queue pause state is inconsistent")
        return None

    def set_manual_pause(self) -> None:
        script = (
            "local a=redis.call('get',KEYS[1]); local b=redis.call('get',KEYS[2]); "
            "if (a and a~=ARGV[1]) or (b and b~=ARGV[1]) then return 0 end; "
            "redis.call('set',KEYS[1],ARGV[1]); redis.call('set',KEYS[2],ARGV[1]); "
            "return 1"
        )
        result = self._redis(
            "EVAL",
            script,
            "2",
            "queue:paused:realtime",
            "queue:paused:bulk",
            _MANUAL_PAUSE_VALUE,
        )
        if result != "1":
            raise VendorTestActivationError("critical pause must be preserved")

    def clear_pause(self, pause_kind: str) -> None:
        if pause_kind == "manual":
            script = (
                "if redis.call('get',KEYS[1])~=ARGV[1] or "
                "redis.call('get',KEYS[2])~=ARGV[1] then return 0 end; "
                "redis.call('del',KEYS[1]); redis.call('del',KEYS[2]); return 1"
            )
            result = self._redis(
                "EVAL",
                script,
                "2",
                "queue:paused:realtime",
                "queue:paused:bulk",
                _MANUAL_PAUSE_VALUE,
            )
        elif pause_kind == "critical":
            script = (
                "local a=redis.call('get',KEYS[1]); local b=redis.call('get',KEYS[2]); "
                "local critical=false; "
                "if (a and a~=ARGV[1]) or (b and b~=ARGV[1]) then critical=true end; "
                "for i=3,6 do if redis.call('exists',KEYS[i])==1 then "
                "critical=true end end; if not critical then return 0 end; "
                "if a and a~=ARGV[1] then redis.call('del',KEYS[1]) end; "
                "if b and b~=ARGV[1] then redis.call('del',KEYS[2]) end; "
                "for i=3,6 do redis.call('del',KEYS[i]) end; return 1"
            )
            result = self._redis(
                "EVAL",
                script,
                "6",
                "queue:paused:realtime",
                "queue:paused:bulk",
                "queue:paused:vendor-test-agent-stale:realtime",
                "queue:paused:vendor-test-agent-stale:bulk",
                "queue:paused:vendor-test-rotation-failed:realtime",
                "queue:paused:vendor-test-rotation-failed:bulk",
                _MANUAL_PAUSE_VALUE,
            )
        else:
            raise VendorTestActivationError("pause kind is invalid")
        if result != "1":
            raise VendorTestActivationError("pause state changed during resume")


def _utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise VendorTestActivationError("activation clock must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def validate_installed_vendor_credentials(
    source: Path,
    *,
    expected_uid: int = 0,
) -> None:
    """仅检查 canonical 文件元数据和非空状态，不读取任何凭据内容。"""

    active_pointer = source / "active"
    if active_pointer.exists():
        try:
            root_metadata = source.lstat()
            pointer_metadata = active_pointer.lstat()
            store = VendorCredentialStore(source)
            generation = store.active_generation()
            generation_metadata = generation.lstat()
            files = [
                generation / "vendor_secret_name",
                generation / "vendor_secret_key",
                generation / "installed_at",
            ]
            file_metadata = [path.lstat() for path in files]
            configured = store.status().configured
        except (OSError, CredentialStoreError):
            raise VendorTestActivationError("vendor credentials are not installed") from None
        if (
            not configured
            or not stat.S_ISDIR(root_metadata.st_mode)
            or stat.S_ISLNK(root_metadata.st_mode)
            or root_metadata.st_uid != expected_uid
            or stat.S_IMODE(root_metadata.st_mode) != 0o700
            or not stat.S_ISREG(pointer_metadata.st_mode)
            or stat.S_ISLNK(pointer_metadata.st_mode)
            or pointer_metadata.st_uid != expected_uid
            or stat.S_IMODE(pointer_metadata.st_mode) != 0o600
            or not stat.S_ISDIR(generation_metadata.st_mode)
            or stat.S_ISLNK(generation_metadata.st_mode)
            or generation_metadata.st_uid != expected_uid
            or stat.S_IMODE(generation_metadata.st_mode) != 0o700
            or any(
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != expected_uid
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size <= 0
                for metadata in file_metadata
            )
        ):
            raise VendorTestActivationError("vendor credentials are not installed")
        return
    try:
        directory = source.lstat()
    except OSError as exc:
        raise VendorTestActivationError("vendor credentials are not installed") from exc
    if (
        not stat.S_ISDIR(directory.st_mode)
        or stat.S_ISLNK(directory.st_mode)
        or directory.st_uid != expected_uid
        or stat.S_IMODE(directory.st_mode) != 0o700
    ):
        raise VendorTestActivationError("vendor credentials are not installed")
    for name in _CREDENTIAL_FILES:
        path = source / name
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise VendorTestActivationError("vendor credentials are not installed") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size <= 0
        ):
            raise VendorTestActivationError("vendor credentials are not installed")


def _require_zero_active_counts(counts: Mapping[str, int]) -> None:
    if set(counts) != set(ACTIVE_STATUSES):
        raise VendorTestActivationError("active status observation is invalid")
    if any(type(counts[name]) is not int or counts[name] < 0 for name in ACTIVE_STATUSES):
        raise VendorTestActivationError("active status observation is invalid")
    if any(counts[name] != 0 for name in ACTIVE_STATUSES):
        raise VendorTestActivationError("active vendor work must be resolved first")


def _require_zero_budget(snapshot: BudgetSnapshot) -> None:
    values = (
        snapshot.confirmed_segments,
        snapshot.in_flight_segments,
        snapshot.uncertain_segments,
    )
    if any(type(value) is not int or value != 0 for value in values):
        raise VendorTestActivationError("vendor activation budget must be zero")


def _probe_evidence(probe: BalanceProbe) -> dict[str, object]:
    if (
        type(probe.code) is not int
        or probe.code != 0
        or type(probe.balance) is not int
        or probe.balance < 0
        or not probe.observed_at
        or not probe.correlation_id
    ):
        raise VendorTestActivationError("GetBalance probe is invalid")
    evidence: dict[str, object] = {
        "code": probe.code,
        "message": probe.message,
        "balance": probe.balance,
        "observed_at": probe.observed_at,
        "correlation_id": probe.correlation_id,
    }
    require_pii_free_evidence(evidence)
    return evidence


async def probe_balance_with_zhihui(
    client_factory: Callable[[], BalanceClient],
    *,
    correlation_id: str,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> BalanceProbe:
    """仅调用 ZhihuiClient.get_balance；禁止以消费型拉取接口做探针。"""

    async with client_factory() as client:
        balance = await client.get_balance()
    return BalanceProbe(
        code=0,
        message="ok",
        balance=balance,
        observed_at=_utc_timestamp(clock()),
        correlation_id=correlation_id,
    )


class VendorTestActivationManager:
    """在同一生命周期锁内执行一次性真实厂商切换。"""

    def __init__(
        self,
        operations: ActivationOperations,
        paths: ActivationPaths,
        *,
        expected_uid: int = 0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.operations = operations
        self.paths = paths
        self.expected_uid = expected_uid
        self.clock = clock

    def _blocked(self, step: str, *, vendor_code: int | None = None) -> None:
        with contextlib.suppress(Exception):
            self.operations.hold_fail_closed()
        payload = {
            "schema_version": 1,
            "status": "blocked",
            "step": step,
            "error_type": "validation_failed",
            "observed_at": _utc_timestamp(self.clock()),
        }
        if vendor_code is not None:
            payload["vendor_code"] = vendor_code
        with contextlib.suppress(Exception):
            write_pii_free_evidence(
                self.paths.evidence_file,
                payload,
                expected_uid=self.expected_uid,
            )

    def activate(self) -> ActivationResult:
        step = "lock"
        locked = False
        try:
            self.operations.require_lifecycle_lock()
            locked = True
            step = "pause"
            self.operations.pause_activation_lanes()
            step = "stop_senders"
            self.operations.stop_senders()
            step = "active_counts"
            _require_zero_active_counts(self.operations.active_status_counts())
            step = "checkpoint"
            checkpoint_id = self.operations.create_encrypted_checkpoint()
            if not checkpoint_id:
                raise VendorTestActivationError("encrypted checkpoint is invalid")
            step = "credentials"
            validate_installed_vendor_credentials(
                self.paths.credential_source,
                expected_uid=self.expected_uid,
            )
            step = "recipients"
            if self.operations.active_recipient_count() < 1:
                raise VendorTestActivationError("active vendor recipients must not be empty")
            step = "marker"
            write_vendor_test_marker(
                self.paths.marker_file,
                expected_uid=self.expected_uid,
            )
            step = "dotenv"
            activate_vendor_live_dotenv(
                self.paths.dotenv_file,
                expected_uid=self.expected_uid,
            )
            step = "remove_mock"
            self.operations.remove_mock_vendor()
            step = "compose_config"
            self.operations.validate_compose()
            step = "start_core"
            self.operations.start_core()
            step = "get_balance"
            probe = self.operations.probe_balance()
            probe_fields = _probe_evidence(probe)
            step = "budget"
            _require_zero_budget(self.operations.budget_snapshot())
            step = "start_senders"
            self.operations.start_senders()
            step = "unpause_activation"
            self.operations.clear_activation_pause()
            step = "evidence"
            evidence = {
                "schema_version": 1,
                "status": "activated",
                "checkpoint_id": checkpoint_id,
                **probe_fields,
            }
            write_pii_free_evidence(
                self.paths.evidence_file,
                evidence,
                expected_uid=self.expected_uid,
            )
            return ActivationResult("activated", checkpoint_id, probe.balance)
        except VendorTestProbeRejected as error:
            if locked:
                self._blocked(step, vendor_code=error.vendor_code)
            raise
        except (Exception, VendorTestFileError):
            if locked:
                self._blocked(step)
            raise VendorTestActivationError(
                f"vendor activation blocked at {step}"
            ) from None


class VendorTestPauseManager:
    """隔离 manual/daily/critical 暂停，critical 恢复前只做 GetBalance 探针。"""

    def __init__(self, operations: PauseOperations) -> None:
        self.operations = operations

    def pause(self) -> PauseResult:
        self.operations.require_lifecycle_lock()
        current = self.operations.current_pause_kind()
        if current is None:
            self.operations.set_manual_pause()
        elif current != "manual":
            raise VendorTestActivationError(f"{current} pause must be preserved")
        return PauseResult("paused", "manual")

    def resume(self) -> PauseResult:
        self.operations.require_lifecycle_lock()
        current = self.operations.current_pause_kind()
        if current is None:
            raise VendorTestActivationError("environment is not paused")
        if current == "daily":
            raise VendorTestActivationError("daily pause cannot be cleared manually")
        if current == "critical":
            _probe_evidence(self.operations.probe_balance())
        self.operations.clear_pause(current)
        return PauseResult("resumed", current)


class VendorTestRotationManager:
    """在单一 lifecycle lock 内轮换凭据，失败回切并保持 critical pause。"""

    def __init__(
        self,
        operations: RotationOperations,
        credentials: RotationCredentialStore,
    ) -> None:
        self.operations = operations
        self.credentials = credentials

    def _rollback(self, transaction: RotationTransaction | None) -> None:
        with contextlib.suppress(Exception):
            self.operations.hold_fail_closed()
        if transaction is None:
            with contextlib.suppress(Exception):
                self.credentials.discard_pending()
            return
        try:
            rolling_back = self.credentials.rollback_to_previous(transaction)
            self.operations.prepare_runtime_secrets()
            self.operations.validate_compose()
            self.operations.rebuild_backend()
            _probe_evidence(self.operations.probe_balance())
            self.credentials.complete_rollback(rolling_back)
        except Exception:
            # 事务和独立 critical 键都保留，后续只能走 recover-rotation。
            return

    def rotate(self) -> RotationResult:
        transaction: RotationTransaction | None = None
        try:
            self.operations.require_lifecycle_lock()
            existing_pause = self.operations.current_pause_kind()
            owns_activation_pause = existing_pause is None
            if owns_activation_pause:
                self.operations.pause_activation_lanes()
            self.operations.stop_senders()
            _require_zero_active_counts(self.operations.active_status_counts())
            checkpoint_id = self.operations.create_encrypted_checkpoint()
            if not checkpoint_id:
                raise VendorTestActivationError("encrypted checkpoint is invalid")
            transaction = self.credentials.begin_rotation()
            self.operations.prepare_runtime_secrets()
            self.operations.validate_compose()
            self.operations.rebuild_backend()
            probe = self.operations.probe_balance()
            _probe_evidence(probe)
            self.credentials.commit_rotation(transaction)
            if owns_activation_pause:
                self.operations.clear_activation_pause()
            return RotationResult("rotated", checkpoint_id, probe.balance)
        except VendorTestProbeRejected:
            self._rollback(transaction)
            raise
        except Exception:
            self._rollback(transaction)
            raise VendorTestActivationError("vendor credential rotation blocked") from None


class VendorTestRotationRecoveryManager:
    """在 lifecycle lock 内将崩溃中的轮换收敛到已验证的上一代运行态。"""

    def __init__(
        self,
        operations: RotationOperations,
        credentials: RotationCredentialStore,
    ) -> None:
        self.operations = operations
        self.credentials = credentials

    def recover(self) -> str:
        self.operations.require_lifecycle_lock()
        try:
            transaction = self.credentials.read_rotation_transaction()
        except Exception:
            self.operations.hold_fail_closed()
            raise VendorTestActivationError("vendor credential recovery blocked") from None
        if transaction is None:
            return self.credentials.recover_pending()
        self.operations.hold_fail_closed()
        try:
            self.operations.stop_senders()
            rolling_back = self.credentials.rollback_to_previous(transaction)
            self.operations.prepare_runtime_secrets()
            self.operations.validate_compose()
            self.operations.rebuild_backend()
            _probe_evidence(self.operations.probe_balance())
            self.credentials.complete_rollback(rolling_back)
        except VendorTestProbeRejected:
            raise
        except Exception:
            raise VendorTestActivationError("vendor credential recovery blocked") from None
        return "rolled_back"


def _private_directory(
    path: Path,
    *,
    expected_uid: int,
    create: bool = False,
    expected_mode: int = 0o700,
    expected_gid: int | None = None,
) -> None:
    if create:
        path.mkdir(mode=expected_mode, parents=False, exist_ok=True)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise VendorTestActivationError("controlled directory is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != expected_uid
        or stat.S_IMODE(metadata.st_mode) != expected_mode
        or (expected_gid is not None and metadata.st_gid != expected_gid)
    ):
        raise VendorTestActivationError("controlled directory is unsafe")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vendor_test_manager.py")
    parser.add_argument(
        "command",
        choices=(
            "activate",
            "pause",
            "preflight",
            "recover-rotation",
            "resume",
            "rotate",
            "status",
        ),
    )
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--marker-file", required=True, type=Path)
    parser.add_argument("--dotenv-file", required=True, type=Path)
    return parser


def _require_cli_paths(arguments: argparse.Namespace) -> None:
    if (
        not arguments.root.is_absolute()
        or not arguments.runtime_root.is_absolute()
        or not arguments.state_dir.is_absolute()
        or not arguments.marker_file.is_absolute()
        or not arguments.dotenv_file.is_absolute()
    ):
        raise VendorTestActivationError("controlled paths must be absolute")
    if any(
        ".." in path.parts
        for path in (
            arguments.root,
            arguments.runtime_root,
            arguments.state_dir,
            arguments.marker_file,
            arguments.dotenv_file,
        )
    ):
        raise VendorTestActivationError("controlled paths are invalid")
    if arguments.marker_file != Path("/etc/sms-platform/test-environment"):
        raise VendorTestActivationError("marker path is invalid")
    if arguments.state_dir != Path("/var/lib/sms-platform/vendor-test"):
        raise VendorTestActivationError("vendor state path is invalid")
    if arguments.dotenv_file != arguments.root / ".env":
        raise VendorTestActivationError("dotenv path is invalid")


def _safe_status(
    marker_file: Path,
    recipient_count: int,
    *,
    expected_uid: int,
) -> dict[str, object]:
    if type(recipient_count) is not int or recipient_count < 0:
        raise VendorTestActivationError("active recipient count is invalid")
    try:
        marker_file.lstat()
    except FileNotFoundError:
        return {
            "schema_version": 1,
            "recipient_count": recipient_count,
            "status": "inactive",
        }
    except OSError as exc:
        raise VendorTestFileError("controlled file is unavailable") from exc
    marker = read_vendor_test_marker(marker_file, expected_uid=expected_uid)
    return {
        "schema_version": marker.schema_version,
        "mode": marker.mode,
        "vendor_origin": marker.vendor_origin,
        "daily_segment_limit": marker.daily_segment_limit,
        "timezone": marker.timezone,
        "recipient_count": recipient_count,
        "status": "controlled",
    }


def _read_marker_if_present(path: Path, *, expected_uid: int) -> object | None:
    """缺失表示尚未首次激活；存在但不安全时仍严格失败。"""

    try:
        path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise VendorTestFileError("controlled file is unavailable") from exc
    return read_vendor_test_marker(path, expected_uid=expected_uid)


def main(argv: list[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)
    try:
        _require_cli_paths(arguments)
        expected_uid = 0
        _private_directory(
            arguments.state_dir,
            expected_uid=expected_uid,
            expected_mode=0o710,
            expected_gid=_BACKEND_RUNTIME_GID,
        )
        if arguments.command == "status":
            operations = HostActivationOperations(
                root=arguments.root,
                runtime_root=arguments.runtime_root,
                backup_config_file=Path(
                    "/etc/sms-platform/test-update-backup.json"
                ),
                expected_uid=expected_uid,
            )
            recipient_count = operations.active_recipient_count()
            status = _safe_status(
                arguments.marker_file,
                recipient_count,
                expected_uid=expected_uid,
            )
            status["pause_kind"] = None
            if status["status"] == "controlled":
                pause_kind = operations.current_pause_kind()
                if pause_kind is not None:
                    status["status"] = "blocked"
                    status["pause_kind"] = pause_kind
            migration_head = operations._psql(
                "SELECT version_num FROM alembic_version"
            )
            if re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
                migration_head,
            ) is None:
                raise VendorTestActivationError("migration head is invalid")
            status["actual_migration_head"] = migration_head
            print(
                json.dumps(
                    status,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            return 0

        require_inherited_lifecycle_lock(arguments.runtime_root)
        operations = HostActivationOperations(
            root=arguments.root,
            runtime_root=arguments.runtime_root,
            backup_config_file=Path("/etc/sms-platform/test-update-backup.json"),
            expected_uid=expected_uid,
        )
        credential_store = VendorCredentialStore(
            arguments.state_dir / "credentials"
        )
        if arguments.command == "recover-rotation":
            recovery = VendorTestRotationRecoveryManager(
                operations,
                credential_store,
            ).recover()
            print(
                json.dumps(
                    {"status": "recovered", "result": recovery},
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "pause":
            pause_result = VendorTestPauseManager(operations).pause()
            print(
                json.dumps(
                    {
                        "status": pause_result.status,
                        "pause_kind": pause_result.pause_kind,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            return 0
        marker = (
            _read_marker_if_present(
                arguments.marker_file,
                expected_uid=expected_uid,
            )
            if arguments.command in {"preflight", "resume", "rotate"}
            else None
        )
        validate_installed_vendor_credentials(
            arguments.state_dir / "credentials",
            expected_uid=expected_uid,
        )
        if arguments.command == "rotate" and marker is None:
            try:
                reconcile_pure_mock_dotenv(
                    arguments.dotenv_file,
                    expected_uid=expected_uid,
                )
            except Exception:
                operations.hold_fail_closed()
                raise
            credential_store.activate_pending()
            print(json.dumps({"status": "rotated"}, separators=(",", ":")))
            return 0
        if (
            arguments.command == "resume"
            and credential_store.read_rotation_transaction() is not None
        ):
            operations.hold_fail_closed()
            raise VendorTestActivationError("vendor credential recovery is required")
        if operations.active_recipient_count() < 1:
            raise VendorTestActivationError("active vendor recipients must not be empty")
        if arguments.command == "resume":
            if marker is None:
                raise VendorTestActivationError("marker is unavailable")
            resume_result = VendorTestPauseManager(operations).resume()
            print(
                json.dumps(
                    {
                        "status": resume_result.status,
                        "pause_kind": resume_result.pause_kind,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "preflight":
            if marker is None:
                raise VendorTestActivationError("marker is unavailable")
            probe = operations.probe_balance()
            print(
                json.dumps(
                    {"status": "ready", **_probe_evidence(probe)},
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "rotate":
            rotation_result = VendorTestRotationManager(
                operations,
                credential_store,
            ).rotate()
            print(
                json.dumps(
                    {
                        "status": rotation_result.status,
                        "checkpoint_id": rotation_result.checkpoint_id,
                        "balance": rotation_result.balance,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            return 0

        evidence_directory = arguments.state_dir / "evidence"
        _private_directory(
            evidence_directory,
            expected_uid=expected_uid,
            create=True,
        )
        activation_result = VendorTestActivationManager(
            operations,
            ActivationPaths(
                runtime_root=arguments.runtime_root,
                credential_source=arguments.state_dir / "credentials",
                marker_file=arguments.marker_file,
                dotenv_file=arguments.dotenv_file,
                evidence_file=evidence_directory / "activation.json",
            ),
            expected_uid=expected_uid,
        ).activate()
        print(
            json.dumps(
                {
                    "status": activation_result.status,
                    "checkpoint_id": activation_result.checkpoint_id,
                    "balance": activation_result.balance,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 0
    except VendorTestProbeRejected as error:
        print(
            json.dumps(
                {
                    "status": "error",
                    "safe_code": "VENDOR_ERROR",
                    "vendor_code": error.vendor_code,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        print(
            f"vendor-test {arguments.command} blocked: "
            f"vendor_code={error.vendor_code}",
            file=sys.stderr,
        )
        return 1
    except Exception:
        print(
            json.dumps(
                {"status": "error", "safe_code": "CONTROL_COMMAND_FAILED"},
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        print(f"vendor-test {arguments.command} blocked", file=sys.stderr)
        return 1


__all__ = [
    "ACTIVE_STATUSES",
    "ActivationPaths",
    "ActivationResult",
    "BalanceProbe",
    "BudgetSnapshot",
    "PauseResult",
    "VendorTestActivationError",
    "VendorTestActivationManager",
    "VendorTestPauseManager",
    "VendorTestProbeRejected",
    "probe_balance_with_zhihui",
    "validate_installed_vendor_credentials",
]


if __name__ == "__main__":
    raise SystemExit(main())
