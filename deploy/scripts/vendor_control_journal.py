#!/usr/bin/env python3
"""root 控制代理的无 PII 原子 operation journal。"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

SCHEMA_VERSION = 4
JOURNALED_OPERATIONS = frozenset(
    {
        "install_credentials",
        "reset_configuration",
        "rotate_credentials",
        "activate",
        "pause",
        "resume",
    }
)
JOURNAL_STATUSES = frozenset({"running", "succeeded", "failed"})
_FIELDS_V1 = {
    "schema_version",
    "operation_id",
    "operation",
    "status",
    "safe_code",
    "recorded_at",
}
_FIELDS_V2 = _FIELDS_V1 | {"checkpoint_id", "vendor_code"}
_FIELDS_V3 = _FIELDS_V2 | {"phase"}
_SAFE_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_SAFE_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_CHECKPOINT_OPERATIONS = frozenset({"activate", "rotate_credentials"})
RESET_AUTHORIZED_PHASE = "reset_authorized"
RESET_RUNTIME_REVOKED_PHASE = "runtime_revoked"
_RESET_PHASES = frozenset({RESET_AUTHORIZED_PHASE, RESET_RUNTIME_REVOKED_PHASE})


class JournalError(RuntimeError):
    """journal 数据、权限或原子写入不符合合同。"""


class JournalConflict(JournalError):
    """operation ID 已绑定其他操作或不兼容终态。"""


@dataclass(frozen=True, slots=True)
class JournalRecord:
    operation_id: str
    operation: str
    status: str
    safe_code: str | None
    recorded_at: str
    checkpoint_id: str | None = None
    vendor_code: int | None = None
    phase: str | None = None


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise JournalError("journal 包含重复字段")
        document[key] = value
    return document


def _operation_id(value: str) -> str:
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        raise JournalError("journal operation ID 无效") from None
    if str(parsed) != value:
        raise JournalError("journal operation ID 无效")
    return value


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


class VendorControlJournal:
    """每个 operation 一个 root 私有文件；请求体在 API 上完全不可表达。"""

    def __init__(self, root: Path, *, expected_uid: int = 0) -> None:
        if not root.is_absolute():
            raise JournalError("journal 路径必须为绝对路径")
        self.root = root
        self.expected_uid = expected_uid

    def ensure_root(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = self.root.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != self.expected_uid
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise JournalError("journal 目录权限无效")

    def _path(self, operation_id: str) -> Path:
        return self.root / f"{_operation_id(operation_id)}.json"

    def _decode(self, path: Path) -> JournalRecord:
        try:
            metadata = path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != self.expected_uid
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise JournalError("journal 文件权限无效")
            document = json.loads(
                path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
            )
        except JournalError:
            raise
        except FileNotFoundError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise JournalError("journal 文件无效") from None
        if type(document) is not dict:
            raise JournalError("journal 字段无效")
        typed = cast(dict[str, object], document)
        schema_version = typed.get("schema_version")
        if type(schema_version) is not int:
            raise JournalError("journal schema 版本无效")
        if schema_version == 1 and set(document) == _FIELDS_V1:
            checkpoint_id: object = None
            vendor_code: object = None
            phase: object = None
        elif schema_version == 2 and set(document) == _FIELDS_V2:
            checkpoint_id = typed.get("checkpoint_id")
            vendor_code = typed.get("vendor_code")
            phase = None
        elif schema_version in {3, SCHEMA_VERSION} and set(document) == _FIELDS_V3:
            checkpoint_id = typed.get("checkpoint_id")
            vendor_code = typed.get("vendor_code")
            phase = typed.get("phase")
        else:
            raise JournalError("journal schema 版本无效")
        operation_id = _operation_id(typed.get("operation_id"))  # type: ignore[arg-type]
        operation = typed.get("operation")
        status_value = typed.get("status")
        safe_code = typed.get("safe_code")
        recorded_at = typed.get("recorded_at")
        if operation not in JOURNALED_OPERATIONS or status_value not in JOURNAL_STATUSES:
            raise JournalError("journal 枚举无效")
        if safe_code is not None and (
            type(safe_code) is not str or _SAFE_CODE.fullmatch(safe_code) is None
        ):
            raise JournalError("journal 安全码无效")
        if status_value == "failed" and safe_code is None:
            raise JournalError("journal 失败状态缺少安全码")
        if status_value != "failed" and safe_code is not None:
            raise JournalError("journal 非失败状态含安全码")
        if type(recorded_at) is not str:
            raise JournalError("journal 时间无效")
        if checkpoint_id is not None and (
            type(checkpoint_id) is not str
            or _SAFE_REFERENCE.fullmatch(checkpoint_id) is None
            or operation not in _CHECKPOINT_OPERATIONS
            or status_value != "succeeded"
        ):
            raise JournalError("journal checkpoint 无效")
        if vendor_code is not None and (
            type(vendor_code) is not int
            or not 1 <= vendor_code <= 99_999
            or status_value != "failed"
            or safe_code != "VENDOR_ERROR"
        ):
            raise JournalError("journal 厂商错误码无效")
        if phase is not None and (
            type(phase) is not str
            or phase not in _RESET_PHASES
            or operation != "reset_configuration"
        ):
            raise JournalError("journal phase 无效")
        if schema_version == 3 and phase == RESET_RUNTIME_REVOKED_PHASE:
            raise JournalError("journal phase 无效")
        if (
            schema_version == SCHEMA_VERSION
            and operation == "reset_configuration"
            and status_value == "succeeded"
            and phase != RESET_RUNTIME_REVOKED_PHASE
        ):
            raise JournalError("journal phase 无效")
        try:
            parsed_time = datetime.fromisoformat(recorded_at)
        except ValueError:
            raise JournalError("journal 时间无效") from None
        if parsed_time.tzinfo is None:
            raise JournalError("journal 时间无效")
        return JournalRecord(
            operation_id,
            str(operation),
            str(status_value),
            str(safe_code) if safe_code is not None else None,
            recorded_at,
            str(checkpoint_id) if checkpoint_id is not None else None,
            int(vendor_code) if vendor_code is not None else None,
            str(phase) if phase is not None else None,
        )

    @staticmethod
    def _document(record: JournalRecord) -> bytes:
        return (
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "operation_id": record.operation_id,
                    "operation": record.operation,
                    "status": record.status,
                    "safe_code": record.safe_code,
                    "checkpoint_id": record.checkpoint_id,
                    "vendor_code": record.vendor_code,
                    "phase": record.phase,
                    "recorded_at": record.recorded_at,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

    def _write_temp(self, record: JournalRecord) -> Path:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{record.operation_id}.",
            suffix=".tmp",
            dir=self.root,
        )
        path = Path(name)
        try:
            os.fchmod(descriptor, 0o600)
            payload = self._document(record)
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise JournalError("journal 写入失败")
                view = view[written:]
            os.fsync(descriptor)
        except BaseException:
            os.close(descriptor)
            path.unlink(missing_ok=True)
            raise
        os.close(descriptor)
        return path

    def _sync_root(self) -> None:
        descriptor = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def get(self, operation_id: str) -> JournalRecord | None:
        self.ensure_root()
        operation_id = _operation_id(operation_id)
        path = self._path(operation_id)
        try:
            record = self._decode(path)
        except FileNotFoundError:
            return None
        if record.operation_id != operation_id:
            raise JournalError("journal operation ID 绑定无效")
        return record

    def begin(self, operation_id: str, operation: str) -> tuple[JournalRecord, bool]:
        self.ensure_root()
        operation_id = _operation_id(operation_id)
        if operation not in JOURNALED_OPERATIONS:
            raise JournalError("journal operation 无效")
        existing = self.get(operation_id)
        if existing is not None:
            if existing.operation != operation:
                raise JournalConflict("journal operation ID 冲突")
            return existing, False
        record = JournalRecord(
            operation_id=operation_id,
            operation=operation,
            status="running",
            safe_code=None,
            recorded_at=_timestamp(),
        )
        temporary = self._write_temp(record)
        target = self._path(operation_id)
        try:
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError:
            existing = self.get(operation_id)
            if existing is None or existing.operation != operation:
                raise JournalConflict("journal operation ID 冲突") from None
            return existing, False
        except OSError:
            raise JournalError("journal 原子创建失败") from None
        finally:
            temporary.unlink(missing_ok=True)
        self._sync_root()
        return record, True

    def authorize_reset(self, operation_id: str) -> JournalRecord:
        """持久化 reset 授权 phase，成功返回前完成文件与目录 fsync。"""

        existing = self.get(operation_id)
        if (
            existing is None
            or existing.operation != "reset_configuration"
            or existing.status != "running"
        ):
            raise JournalConflict("journal reset 授权状态冲突")
        if existing.phase in _RESET_PHASES:
            try:
                self._sync_root()
            except OSError:
                raise JournalError("journal reset 授权同步失败") from None
            return existing
        record = JournalRecord(
            operation_id=existing.operation_id,
            operation=existing.operation,
            status=existing.status,
            safe_code=existing.safe_code,
            recorded_at=_timestamp(),
            checkpoint_id=existing.checkpoint_id,
            vendor_code=existing.vendor_code,
            phase=RESET_AUTHORIZED_PHASE,
        )
        temporary = self._write_temp(record)
        try:
            os.replace(temporary, self._path(operation_id))
            self._sync_root()
        except OSError:
            raise JournalError("journal reset 授权写入失败") from None
        finally:
            temporary.unlink(missing_ok=True)
        return record

    def mark_runtime_revoked(self, operation_id: str) -> JournalRecord:
        """只允许已授权 running reset 持久化 runtime 撤销完成 phase。"""

        existing = self.get(operation_id)
        if (
            existing is None
            or existing.operation != "reset_configuration"
            or existing.status != "running"
            or existing.phase not in _RESET_PHASES
        ):
            raise JournalConflict("journal runtime 撤销状态冲突")
        if existing.phase == RESET_RUNTIME_REVOKED_PHASE:
            try:
                self._sync_root()
            except OSError:
                raise JournalError("journal runtime 撤销同步失败") from None
            return existing
        record = JournalRecord(
            operation_id=existing.operation_id,
            operation=existing.operation,
            status=existing.status,
            safe_code=existing.safe_code,
            recorded_at=_timestamp(),
            checkpoint_id=existing.checkpoint_id,
            vendor_code=existing.vendor_code,
            phase=RESET_RUNTIME_REVOKED_PHASE,
        )
        temporary = self._write_temp(record)
        try:
            os.replace(temporary, self._path(operation_id))
            self._sync_root()
        except OSError:
            raise JournalError("journal runtime 撤销写入失败") from None
        finally:
            temporary.unlink(missing_ok=True)
        return record

    def finish(
        self,
        operation_id: str,
        operation: str,
        *,
        status: str,
        safe_code: str | None,
        checkpoint_id: str | None = None,
        vendor_code: int | None = None,
    ) -> JournalRecord:
        if status not in {"succeeded", "failed"}:
            raise JournalError("journal 终态无效")
        if status == "succeeded" and safe_code is not None:
            raise JournalError("journal 成功状态含安全码")
        if status == "failed" and (
            safe_code is None or _SAFE_CODE.fullmatch(safe_code) is None
        ):
            raise JournalError("journal 失败安全码无效")
        if checkpoint_id is not None and (
            operation not in _CHECKPOINT_OPERATIONS
            or status != "succeeded"
            or _SAFE_REFERENCE.fullmatch(checkpoint_id) is None
        ):
            raise JournalError("journal checkpoint 无效")
        if vendor_code is not None and (
            type(vendor_code) is not int
            or not 1 <= vendor_code <= 99_999
            or status != "failed"
            or safe_code != "VENDOR_ERROR"
        ):
            raise JournalError("journal 厂商错误码无效")
        existing = self.get(operation_id)
        if existing is None or existing.operation != operation:
            raise JournalConflict("journal operation ID 冲突")
        if existing.status != "running":
            if (
                existing.status == status
                and existing.safe_code == safe_code
                and existing.checkpoint_id == checkpoint_id
                and existing.vendor_code == vendor_code
            ):
                return existing
            raise JournalConflict("journal 终态冲突")
        if (
            operation == "reset_configuration"
            and status == "succeeded"
            and existing.phase != RESET_RUNTIME_REVOKED_PHASE
        ):
            raise JournalConflict("journal reset runtime 未撤销")
        record = JournalRecord(
            operation_id=operation_id,
            operation=operation,
            status=status,
            safe_code=safe_code,
            recorded_at=_timestamp(),
            checkpoint_id=checkpoint_id,
            vendor_code=vendor_code,
            phase=existing.phase,
        )
        temporary = self._write_temp(record)
        try:
            os.replace(temporary, self._path(operation_id))
            self._sync_root()
        except OSError:
            raise JournalError("journal 原子完成失败") from None
        finally:
            temporary.unlink(missing_ok=True)
        return record
