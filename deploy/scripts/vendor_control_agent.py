#!/usr/bin/env python3
"""root systemd vendor-control-agent，仅通过固定 UDS 暴露枚举操作。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import socket
import stat
import struct
import subprocess
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from vendor_control_journal import (
    JOURNALED_OPERATIONS,
    RESET_AUTHORIZED_PHASE,
    RESET_RUNTIME_REVOKED_PHASE,
    JournalError,
    JournalRecord,
    VendorControlJournal,
)
from vendor_credential_store import (
    CredentialStatus,
    CredentialStoreError,
    VendorCredentialStore,
)
from vendor_credential_store import (
    VendorCredentials as StoredVendorCredentials,
)
from vendor_seal_sessions import (
    SealedCredentialEnvelope,
    SealSessionError,
    SealSessionManager,
)

from vendor_control_protocol import (
    MAX_FRAME_BYTES,
    ControlRequest,
    ControlResponse,
    ProtocolError,
    decode_request,
    encode_response,
)

CONTROL_SOCKET = Path("/run/sms-platform/vendor-control/vendor-control.sock")
CREDENTIAL_ROOT = Path("/var/lib/sms-platform/vendor-test/credentials")
JOURNAL_ROOT = Path("/var/lib/sms-platform/vendor-test/journal")
CONTROL_STATE = Path("/var/lib/sms-platform/vendor-test/control-state.json")
WRAPPER = "/usr/local/sbin/sms-compose"
HEARTBEAT_INTERVAL_SECONDS = 10
DAILY_LIMIT = 100
_WRAPPER_OPERATIONS = frozenset({"activate", "pause", "resume"})
_FIXED_WRAPPER_OPERATIONS = _WRAPPER_OPERATIONS | {
    "recover-rotation",
    "reset-runtime",
    "rotate",
    "status",
}
_STATE_MUTATING_OPERATIONS = _WRAPPER_OPERATIONS | {
    "install_credentials",
    "reset_configuration",
    "rotate_credentials",
}
_MAX_COMPLETED = 4096


class PeerDenied(PermissionError):
    """Unix peer credentials 不属于固定 API runtime 身份。"""


class UnsupportedAgentOperation(ValueError):
    """拒绝未枚举的 agent 或 wrapper 操作。"""


class WrapperRunner(Protocol):
    def run(self, operation: str) -> WrapperResult: ...


@dataclass(frozen=True, slots=True)
class WrapperResult:
    returncode: int
    safe_code: str | None
    body: dict[str, object]


class CredentialStore(Protocol):
    def status(self) -> CredentialStatus: ...

    def reset_required(self) -> bool: ...

    def install(self, credentials: StoredVendorCredentials) -> CredentialStatus: ...

    def stage(self, credentials: StoredVendorCredentials) -> CredentialStatus: ...

    def discard_pending(self) -> None: ...

    def reset(self) -> CredentialStatus: ...


class SealSessions(Protocol):
    def create(self, operation: str, actor: str) -> Any: ...

    def open(
        self,
        envelope: SealedCredentialEnvelope,
        *,
        operation: str,
        actor: str,
    ) -> Any: ...


class ControlJournal(Protocol):
    def get(self, operation_id: str) -> JournalRecord | None: ...

    def begin(self, operation_id: str, operation: str) -> tuple[JournalRecord, bool]: ...

    def authorize_reset(self, operation_id: str) -> JournalRecord: ...

    def mark_runtime_revoked(self, operation_id: str) -> JournalRecord: ...

    def finish(
        self,
        operation_id: str,
        operation: str,
        *,
        status: str,
        safe_code: str | None,
        checkpoint_id: str | None = None,
        vendor_code: int | None = None,
    ) -> JournalRecord: ...


class FixedWrapperRunner:
    """只执行固定 wrapper 三元组，不接受 argv/path/env。"""

    def __init__(
        self,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.command_runner = command_runner

    @staticmethod
    def _safe_result(returncode: int, stdout: str) -> WrapperResult:
        lines = [line for line in stdout.splitlines() if line.strip()]
        try:
            document = json.loads(lines[-1])
        except (IndexError, json.JSONDecodeError):
            return WrapperResult(1, "CONTROL_COMMAND_FAILED", {})
        if type(document) is not dict or type(document.get("status")) is not str:
            return WrapperResult(1, "CONTROL_COMMAND_FAILED", {})
        status_value = document["status"]
        if returncode == 0:
            if status_value not in {
                "activated",
                "paused",
                "recovered",
                "resumed",
                "rotated",
            }:
                return WrapperResult(1, "CONTROL_COMMAND_FAILED", {})
            body: dict[str, object] = {}
            checkpoint_id = document.get("checkpoint_id")
            if checkpoint_id is not None:
                if (
                    type(checkpoint_id) is not str
                    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", checkpoint_id)
                    is None
                ):
                    return WrapperResult(1, "CONTROL_COMMAND_FAILED", {})
                body["checkpoint_id"] = checkpoint_id
            return WrapperResult(0, None, body)
        if status_value != "error":
            return WrapperResult(1, "CONTROL_COMMAND_FAILED", {})
        safe_code = document.get("safe_code")
        if safe_code != "VENDOR_ERROR":
            return WrapperResult(1, "CONTROL_COMMAND_FAILED", {})
        vendor_code = document.get("vendor_code")
        if type(vendor_code) is not int or not 1 <= vendor_code <= 99_999:
            return WrapperResult(1, "CONTROL_COMMAND_FAILED", {})
        return WrapperResult(1, "VENDOR_ERROR", {"vendor_code": vendor_code})

    @staticmethod
    def _safe_runtime_reset_result(returncode: int, stdout: str) -> WrapperResult:
        lines = [line for line in stdout.splitlines() if line.strip()]
        if len(lines) != 1:
            return WrapperResult(1, "CONTROL_COMMAND_FAILED", {})
        try:
            document = json.loads(lines[0])
        except json.JSONDecodeError:
            return WrapperResult(1, "CONTROL_COMMAND_FAILED", {})
        if returncode != 0 or document != {"status": "runtime_revoked"}:
            return WrapperResult(1, "CONTROL_COMMAND_FAILED", {})
        return WrapperResult(0, None, {"runtime_revoked": True})

    @staticmethod
    def _safe_status_result(returncode: int, stdout: str) -> WrapperResult:
        lines = [line for line in stdout.splitlines() if line.strip()]
        try:
            document = json.loads(lines[-1])
        except (IndexError, json.JSONDecodeError):
            return WrapperResult(1, "CONTROL_COMMAND_FAILED", {})
        if type(document) is not dict:
            return WrapperResult(1, "CONTROL_COMMAND_FAILED", {})
        status = document.get("status")
        common = {
            "schema_version",
            "status",
            "recipient_count",
            "pause_kind",
            "actual_migration_head",
        }
        controlled = common | {
            "mode",
            "vendor_origin",
            "daily_segment_limit",
            "timezone",
        }
        if (
            returncode != 0
            or status not in {"inactive", "controlled", "blocked"}
            or set(document) != (common if status == "inactive" else controlled)
            or document.get("schema_version") != 1
            or type(document.get("recipient_count")) is not int
            or document["recipient_count"] < 0
            or type(document.get("actual_migration_head")) is not str
            or re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
                document["actual_migration_head"],
            )
            is None
        ):
            return WrapperResult(1, "CONTROL_COMMAND_FAILED", {})
        pause_kind = document.get("pause_kind")
        if (
            (status in {"inactive", "controlled"} and pause_kind is not None)
            or (status == "blocked" and pause_kind not in {"manual", "critical", "daily"})
        ):
            return WrapperResult(1, "CONTROL_COMMAND_FAILED", {})
        return WrapperResult(
            0,
            None,
            {
                "mode": status,
                "pause_kind": pause_kind,
                "active_recipient_count": document["recipient_count"],
            },
        )

    def run(self, operation: str) -> WrapperResult:
        if operation not in _FIXED_WRAPPER_OPERATIONS:
            raise UnsupportedAgentOperation("不支持的控制操作")
        result = self.command_runner(
            [WRAPPER, "vendor-test", operation],
            check=False,
            text=True,
            capture_output=True,
            shell=False,
        )
        if operation == "status":
            return self._safe_status_result(int(result.returncode), result.stdout)
        if operation == "reset-runtime":
            return self._safe_runtime_reset_result(int(result.returncode), result.stdout)
        return self._safe_result(int(result.returncode), result.stdout)


def secure_socket(path: Path, *, expected_uid: int, expected_gid: int) -> None:
    """把已绑定 socket 固定为 root:api-runtime-gid 0660。"""

    os.chown(path, expected_uid, expected_gid, follow_symlinks=False)
    os.chmod(path, 0o660, follow_symlinks=False)
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or (metadata.st_uid, metadata.st_gid) != (expected_uid, expected_gid)
        or stat.S_IMODE(metadata.st_mode) != 0o660
    ):
        raise RuntimeError("控制 socket 权限无效")


def write_control_state(
    path: Path,
    *,
    mode: str,
    heartbeat_at: datetime,
    credential_configured: bool,
    active_recipient_count: int,
    pause_kind: str | None,
    backend_gid: int,
    expected_uid: int = 0,
) -> None:
    """原子写 root:backend 0640 安全投影，不接收自由字段。"""

    if (
        mode not in {"setup_required", "inactive", "controlled", "blocked"}
        or type(credential_configured) is not bool
        or type(active_recipient_count) is not int
        or active_recipient_count < 0
        or (pause_kind is not None and pause_kind not in {"manual", "critical", "daily"})
        or heartbeat_at.tzinfo is None
        or backend_gid < 0
        or expected_uid < 0
    ):
        raise RuntimeError("控制状态投影无效")
    path.parent.mkdir(mode=0o710, parents=True, exist_ok=True)
    os.chown(path.parent, expected_uid, backend_gid, follow_symlinks=False)
    os.chmod(path.parent, 0o710, follow_symlinks=False)
    parent = path.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or stat.S_ISLNK(parent.st_mode)
        or (parent.st_uid, parent.st_gid) != (expected_uid, backend_gid)
        or stat.S_IMODE(parent.st_mode) != 0o710
    ):
        raise RuntimeError("控制状态目录权限无效")
    payload = (
        json.dumps(
            {
                "schema_version": 1,
                "mode": mode,
                "heartbeat_at": heartbeat_at.astimezone(UTC).isoformat(),
                "credential_configured": credential_configured,
                "active_recipient_count": active_recipient_count,
                "pause_kind": pause_kind,
                "daily_limit": DAILY_LIMIT,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchown(descriptor, expected_uid, backend_gid)
        os.fchmod(descriptor, 0o640)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise RuntimeError("控制状态写入失败")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


class VendorControlAgent:
    """校验 peer、幂等 operation id，并执行固定本地能力。"""

    def __init__(
        self,
        *,
        runner: WrapperRunner,
        credential_store: CredentialStore,
        seal_sessions: SealSessions,
        expected_uid: int,
        expected_gid: int,
        journal: ControlJournal | None = None,
        state_path: Path | None = None,
        state_expected_uid: int = 0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.runner = runner
        self.credential_store = credential_store
        self.seal_sessions = seal_sessions
        self.expected_uid = expected_uid
        self.expected_gid = expected_gid
        self.journal = journal
        self.state_path = state_path
        self.state_expected_uid = state_expected_uid
        self.clock = clock
        try:
            configured = self.credential_store.status().configured
        except CredentialStoreError:
            configured = False
        self._mode = "inactive" if configured else "setup_required"
        self._pause_kind: str | None = None
        self._recipient_count = 0
        self._completed: dict[str, tuple[str, ControlResponse]] = {}

    def _refresh_host_state(self) -> None:
        try:
            result = self.runner.run("status")
        except (OSError, subprocess.SubprocessError, UnsupportedAgentOperation):
            result = WrapperResult(1, "CONTROL_COMMAND_FAILED", {})
        if result.returncode != 0:
            self._mode = "blocked"
            self._pause_kind = "critical"
            self._recipient_count = 0
            return
        mode = result.body.get("mode")
        pause_kind = result.body.get("pause_kind")
        recipient_count = result.body.get("active_recipient_count")
        if (
            mode not in {"inactive", "controlled", "blocked"}
            or (pause_kind is not None and pause_kind not in {"manual", "critical", "daily"})
            or type(recipient_count) is not int
            or recipient_count < 0
        ):
            self._mode = "blocked"
            self._pause_kind = "critical"
            self._recipient_count = 0
            return
        self._mode = str(mode)
        self._pause_kind = str(pause_kind) if pause_kind is not None else None
        self._recipient_count = recipient_count

    def write_heartbeat(self) -> None:
        if self.state_path is None:
            return
        try:
            configured = self.credential_store.status().configured
        except CredentialStoreError:
            configured = False
            self._mode = "blocked"
            self._pause_kind = "critical"
            self._recipient_count = 0
        else:
            self._refresh_host_state()
            if (
                not configured
                and self._mode == "inactive"
                and self._pause_kind is None
            ):
                self._mode = "setup_required"
        write_control_state(
            self.state_path,
            mode=self._mode,
            heartbeat_at=self.clock(),
            credential_configured=configured,
            active_recipient_count=self._recipient_count,
            pause_kind=self._pause_kind,
            backend_gid=self.expected_gid,
            expected_uid=self.state_expected_uid,
        )

    def _apply_state_transition(
        self,
        request: ControlRequest,
        response: ControlResponse,
    ) -> None:
        if response.status != "ok":
            if request.operation == "activate":
                self._mode = "blocked"
                self._pause_kind = "critical"
            return
        if request.operation in {"install_credentials", "rotate_credentials"}:
            self._mode = "inactive"
        elif request.operation == "reset_configuration":
            self._mode = "setup_required"
            self._pause_kind = None
            self._recipient_count = 0
        elif request.operation == "activate":
            self._mode = "controlled"
            self._pause_kind = None
        elif request.operation == "pause":
            self._mode = "blocked"
            self._pause_kind = str(request.body["pause_kind"])
        elif request.operation == "resume":
            self._mode = "controlled"
            self._pause_kind = None

    def _synchronize_state(
        self,
        request: ControlRequest,
        response: ControlResponse,
    ) -> ControlResponse:
        """状态变更必须先原子落安全投影，客户端才能收到成功。"""

        self._apply_state_transition(request, response)
        if request.operation not in _STATE_MUTATING_OPERATIONS or self.state_path is None:
            return response
        try:
            self.write_heartbeat()
        except Exception:
            if request.operation == "reset_configuration":
                self._mode = "setup_required"
                self._pause_kind = None
                self._recipient_count = 0
                return ControlResponse(
                    request.operation_id,
                    "error",
                    "CONTROL_STATE_SYNC_FAILED",
                    {"operation_status": "running"},
                )
            self._mode = "blocked"
            self._pause_kind = "critical"
            self._recipient_count = 0
            return ControlResponse(
                request.operation_id,
                "error",
                "CONTROL_STATE_SYNC_FAILED",
                {"operation_status": "failed"},
            )
        return response

    def _require_peer(self, peer_uid: int, peer_gid: int) -> None:
        if (peer_uid, peer_gid) != (self.expected_uid, self.expected_gid):
            raise PeerDenied("控制调用方身份无效")

    @staticmethod
    def _credential_body(request: ControlRequest) -> SealedCredentialEnvelope:
        try:
            return SealedCredentialEnvelope(
                session_id=str(request.body["session_id"]),
                wrapped_key=str(request.body["wrapped_key"]),
                nonce=str(request.body["nonce"]),
                ciphertext=str(request.body["ciphertext"]),
                aad=str(request.body["aad"]),
                algorithm=str(request.body["algorithm"]),
            )
        except KeyError:
            raise ProtocolError("凭据 envelope 无效") from None

    def _dispatch(
        self,
        request: ControlRequest,
        *,
        reset_journal_phase: str | None = None,
    ) -> ControlResponse:
        operation = request.operation
        if operation in {"health", "status"}:
            status = self.credential_store.status()
            body: dict[str, object] = {
                "agent_status": "healthy",
                "configured": status.configured,
                "credential_state": status.state,
            }
            if status.installed_at is not None:
                body["installed_at"] = status.installed_at.isoformat()
            return ControlResponse(request.operation_id, "ok", None, body)
        if operation == "create_seal_session":
            session = self.seal_sessions.create(
                str(request.body["operation"]),
                str(request.body["actor"]),
            )
            return ControlResponse(
                request.operation_id,
                "ok",
                None,
                {
                    "session_id": session.session_id,
                    "public_key": session.public_key,
                    "expires_at": session.expires_at.isoformat(),
                    "aad": session.aad,
                },
            )
        if operation == "reset_configuration":
            if request.body:
                raise ProtocolError("重置请求 body 必须为空")
            if self.journal is None:
                return ControlResponse(
                    request.operation_id,
                    "error",
                    "CONTROL_OPERATION_REJECTED",
                    {},
                )
            current = self.credential_store.status()
            if (
                not current.configured
                and reset_journal_phase
                not in {RESET_AUTHORIZED_PHASE, RESET_RUNTIME_REVOKED_PHASE}
            ):
                return ControlResponse(
                    request.operation_id,
                    "error",
                    "CONTROL_OPERATION_REJECTED",
                    {},
                )
            self._refresh_host_state()
            allowed = self._mode == "inactive" and self._pause_kind is None
            if not allowed:
                return ControlResponse(
                    request.operation_id,
                    "error",
                    "CONTROL_OPERATION_REJECTED",
                    {},
                )
            authorized = self.journal.authorize_reset(request.operation_id)
            if authorized.phase not in {
                RESET_AUTHORIZED_PHASE,
                RESET_RUNTIME_REVOKED_PHASE,
            }:
                return ControlResponse(
                    request.operation_id,
                    "error",
                    "CONTROL_OPERATION_REJECTED",
                    {},
                )
            reset = (
                self.credential_store.reset()
                if self.credential_store.reset_required()
                else current
            )
            if reset.configured or self.credential_store.status().configured:
                return ControlResponse(
                    request.operation_id,
                    "error",
                    "CONTROL_OPERATION_REJECTED",
                    {},
                )
            runtime = self.runner.run("reset-runtime")
            if runtime.returncode != 0 or runtime.body != {"runtime_revoked": True}:
                return ControlResponse(
                    request.operation_id,
                    "error",
                    runtime.safe_code or "CONTROL_COMMAND_FAILED",
                    {},
                )
            if self.credential_store.status().configured:
                return ControlResponse(
                    request.operation_id,
                    "error",
                    "CONTROL_OPERATION_REJECTED",
                    {},
                )
            revoked = self.journal.mark_runtime_revoked(request.operation_id)
            if revoked.phase != RESET_RUNTIME_REVOKED_PHASE:
                return ControlResponse(
                    request.operation_id,
                    "error",
                    "CONTROL_OPERATION_REJECTED",
                    {},
                )
            return ControlResponse(
                request.operation_id,
                "ok",
                None,
                {
                    "configured": reset.configured,
                    "credential_state": reset.state,
                },
            )
        if operation in {"install_credentials", "rotate_credentials"}:
            current = self.credential_store.status()
            if operation == "install_credentials" and current.configured:
                return ControlResponse(
                    request.operation_id,
                    "error",
                    "CREDENTIAL_ALREADY_CONFIGURED",
                    {},
                )
            if operation == "rotate_credentials" and not current.configured:
                return ControlResponse(
                    request.operation_id,
                    "error",
                    "CREDENTIAL_NOT_CONFIGURED",
                    {},
                )
            if operation == "rotate_credentials":
                recovery = self.runner.run("recover-rotation")
                if recovery.returncode != 0:
                    return ControlResponse(
                        request.operation_id,
                        "error",
                        recovery.safe_code or "CONTROL_COMMAND_FAILED",
                        recovery.body,
                    )
            opened = self.seal_sessions.open(
                self._credential_body(request),
                operation=operation,
                actor=str(request.body["actor"]),
            )
            candidate = StoredVendorCredentials(opened.secret_name, opened.secret_key)
            wrapper_body: dict[str, object] = {}
            if operation == "install_credentials":
                installed = self.credential_store.install(candidate)
            else:
                self.credential_store.stage(candidate)
                try:
                    result = self.runner.run("rotate")
                except (OSError, subprocess.SubprocessError):
                    with suppress(CredentialStoreError):
                        self.credential_store.discard_pending()
                    raise
                if result.returncode != 0:
                    return ControlResponse(
                        request.operation_id,
                        "error",
                        result.safe_code or "CONTROL_COMMAND_FAILED",
                        result.body,
                    )
                installed = self.credential_store.status()
                wrapper_body = result.body
            return ControlResponse(
                request.operation_id,
                "ok",
                None,
                {
                    "configured": installed.configured,
                    "credential_state": installed.state,
                    "installed_at": installed.installed_at.isoformat()
                    if installed.installed_at is not None
                    else None,
                    **wrapper_body,
                },
            )
        if operation in _WRAPPER_OPERATIONS:
            if operation == "pause" and self._pause_kind is not None:
                return ControlResponse(
                    request.operation_id,
                    "error",
                    "PAUSE_KIND_MISMATCH",
                    {},
                )
            if operation == "resume" and self._pause_kind != request.body["pause_kind"]:
                return ControlResponse(
                    request.operation_id,
                    "error",
                    "PAUSE_KIND_MISMATCH",
                    {},
                )
            result = self.runner.run(operation)
            if result.returncode != 0:
                return ControlResponse(
                    request.operation_id,
                    "error",
                    result.safe_code or "CONTROL_COMMAND_FAILED",
                    result.body,
                )
            return ControlResponse(
                request.operation_id,
                "ok",
                None,
                {"operation_status": "succeeded", **result.body},
            )
        raise UnsupportedAgentOperation("不支持的控制操作")

    @staticmethod
    def _journal_response(record: JournalRecord) -> ControlResponse:
        if record.status == "running":
            if record.operation == "reset_configuration":
                return ControlResponse(
                    record.operation_id,
                    "error",
                    "CONTROL_OPERATION_IN_PROGRESS",
                    {"operation_status": "running"},
                )
            return ControlResponse(
                record.operation_id,
                "error",
                "CONTROL_RESULT_UNKNOWN",
                {"operation_status": "failed"},
            )
        body: dict[str, object] = {"operation_status": record.status}
        if record.checkpoint_id is not None:
            body["checkpoint_id"] = record.checkpoint_id
        if record.vendor_code is not None:
            body["vendor_code"] = record.vendor_code
        return ControlResponse(
            record.operation_id,
            "ok" if record.status == "succeeded" else "error",
            record.safe_code,
            body,
        )

    def _safe_dispatch(
        self,
        request: ControlRequest,
        *,
        reset_journal_phase: str | None = None,
    ) -> ControlResponse:
        try:
            response = self._dispatch(
                request,
                reset_journal_phase=reset_journal_phase,
            )
        except (
            CredentialStoreError,
            JournalError,
            SealSessionError,
            ProtocolError,
            OSError,
            subprocess.SubprocessError,
        ):
            response = ControlResponse(
                request.operation_id,
                "error",
                "CONTROL_OPERATION_REJECTED",
                {},
            )
        except UnsupportedAgentOperation:
            response = ControlResponse(
                request.operation_id,
                "error",
                "UNSUPPORTED_OPERATION",
                {},
            )
        return response

    @staticmethod
    def _reset_is_running(
        request: ControlRequest,
        response: ControlResponse,
    ) -> bool:
        return (
            request.operation == "reset_configuration"
            and response.status == "error"
            and response.safe_code
            in {
                "CONTROL_OPERATION_IN_PROGRESS",
                "CONTROL_RESULT_UNKNOWN",
                "CONTROL_STATE_SYNC_FAILED",
            }
            and response.body == {"operation_status": "running"}
        )

    def _preserve_authorized_reset_recovery(
        self,
        request: ControlRequest,
        response: ControlResponse,
    ) -> ControlResponse:
        """phase 已授权后的非成功结果保持 running，等待同 operation 重放。"""

        if (
            request.operation != "reset_configuration"
            or response.status == "ok"
            or self.journal is None
        ):
            return response
        try:
            recorded = self.journal.get(request.operation_id)
        except JournalError:
            return ControlResponse(
                request.operation_id,
                "error",
                "CONTROL_RESULT_UNKNOWN",
                {"operation_status": "running"},
            )
        if (
            recorded is not None
            and recorded.operation == "reset_configuration"
            and recorded.status == "running"
            and recorded.phase == RESET_AUTHORIZED_PHASE
        ):
            if self._reset_is_running(request, response):
                return response
            return ControlResponse(
                request.operation_id,
                "error",
                "CONTROL_RESULT_UNKNOWN",
                {"operation_status": "running"},
            )
        return response

    def handle(
        self,
        request: ControlRequest,
        *,
        peer_uid: int,
        peer_gid: int,
    ) -> ControlResponse:
        self._require_peer(peer_uid, peer_gid)
        if request.operation == "status" and self.journal is not None:
            try:
                recorded = self.journal.get(request.operation_id)
            except JournalError:
                return ControlResponse(
                    request.operation_id,
                    "error",
                    "CONTROL_OPERATION_REJECTED",
                    {},
                )
            if recorded is None:
                return ControlResponse(
                    request.operation_id,
                    "error",
                    "OPERATION_NOT_FOUND",
                    {"operation_status": "not_found"},
                )
            return self._journal_response(recorded)
        cached = self._completed.get(request.operation_id)
        if cached is not None:
            if cached[0] == request.operation:
                return cached[1]
            return ControlResponse(
                request.operation_id,
                "error",
                "OPERATION_ID_CONFLICT",
                {},
            )
        if request.operation in JOURNALED_OPERATIONS and self.journal is not None:
            try:
                recorded, created = self.journal.begin(
                    request.operation_id,
                    request.operation,
                )
            except JournalError:
                return ControlResponse(
                    request.operation_id,
                    "error",
                    "CONTROL_OPERATION_REJECTED",
                    {},
                )
            if not created:
                if (
                    recorded.status != "running"
                    or request.operation != "reset_configuration"
                ):
                    response = self._journal_response(recorded)
                    return self._synchronize_state(request, response)
                response = self._synchronize_state(
                    request,
                    self._safe_dispatch(
                        request,
                        reset_journal_phase=recorded.phase,
                    ),
                )
                response = self._preserve_authorized_reset_recovery(
                    request,
                    response,
                )
                if self._reset_is_running(request, response):
                    return response
                try:
                    checkpoint_value = response.body.get("checkpoint_id")
                    vendor_code_value = response.body.get("vendor_code")
                    recorded = self.journal.finish(
                        request.operation_id,
                        request.operation,
                        status="succeeded" if response.status == "ok" else "failed",
                        safe_code=response.safe_code,
                        checkpoint_id=(
                            checkpoint_value
                            if type(checkpoint_value) is str
                            else None
                        ),
                        vendor_code=(
                            vendor_code_value
                            if type(vendor_code_value) is int
                            else None
                        ),
                    )
                except JournalError:
                    return ControlResponse(
                        request.operation_id,
                        "error",
                        "CONTROL_RESULT_UNKNOWN",
                        {"operation_status": "running"},
                    )
                return self._journal_response(recorded)
            response = self._synchronize_state(
                request,
                self._safe_dispatch(
                    request,
                    reset_journal_phase=recorded.phase,
                ),
            )
            response = self._preserve_authorized_reset_recovery(
                request,
                response,
            )
            if self._reset_is_running(request, response):
                return response
            try:
                checkpoint_value = response.body.get("checkpoint_id")
                vendor_code_value = response.body.get("vendor_code")
                self.journal.finish(
                    request.operation_id,
                    request.operation,
                    status="succeeded" if response.status == "ok" else "failed",
                    safe_code=response.safe_code,
                    checkpoint_id=(
                        checkpoint_value if type(checkpoint_value) is str else None
                    ),
                    vendor_code=(
                        vendor_code_value if type(vendor_code_value) is int else None
                    ),
                )
            except JournalError:
                if request.operation == "reset_configuration":
                    return ControlResponse(
                        request.operation_id,
                        "error",
                        "CONTROL_RESULT_UNKNOWN",
                        {"operation_status": "running"},
                    )
                response = ControlResponse(
                    request.operation_id,
                    "error",
                    "CONTROL_RESULT_UNKNOWN",
                    {"operation_status": "failed"},
                )
        else:
            response = self._synchronize_state(
                request,
                self._safe_dispatch(request),
            )
        if len(self._completed) >= _MAX_COMPLETED:
            self._completed.pop(next(iter(self._completed)))
        self._completed[request.operation_id] = (request.operation, response)
        return response


def _peer_credentials(writer: asyncio.StreamWriter) -> tuple[int, int]:
    peer_socket = writer.get_extra_info("socket")
    if peer_socket is None or not hasattr(socket, "SO_PEERCRED"):
        raise PeerDenied("无法验证控制调用方")
    raw = peer_socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, gid = struct.unpack("3i", raw)
    return uid, gid


async def _serve_client(
    agent: VendorControlAgent,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        uid, gid = _peer_credentials(writer)
        header = await reader.readexactly(4)
        declared = struct.unpack("!I", header)[0]
        if declared < 1 or declared > MAX_FRAME_BYTES:
            raise ProtocolError("请求帧长度无效")
        request = decode_request(header + await reader.readexactly(declared))
        response = agent.handle(request, peer_uid=uid, peer_gid=gid)
        writer.write(encode_response(response))
        await writer.drain()
    except (PeerDenied, ProtocolError, asyncio.IncompleteReadError, OSError):
        pass
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


def prepare_socket_directory(
    socket_path: Path,
    *,
    expected_uid: int,
    expected_gid: int,
) -> None:
    """创建独立 UDS 目录并固定为 root:backend 0750。"""

    socket_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    metadata = socket_path.parent.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise RuntimeError("控制 socket 目录无效")
    os.chown(socket_path.parent, expected_uid, expected_gid, follow_symlinks=False)
    os.chmod(socket_path.parent, 0o750, follow_symlinks=False)
    metadata = socket_path.parent.lstat()
    if (
        (metadata.st_uid, metadata.st_gid) != (expected_uid, expected_gid)
        or stat.S_IMODE(metadata.st_mode) != 0o750
    ):
        raise RuntimeError("控制 socket 目录权限无效")


def _prepare_socket_path(
    socket_path: Path,
    *,
    expected_uid: int,
    expected_gid: int,
) -> None:
    """同步完成固定 socket 目录与既有节点安全检查。"""

    prepare_socket_directory(
        socket_path,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    if socket_path.exists() or socket_path.is_symlink():
        metadata = socket_path.lstat()
        if not stat.S_ISSOCK(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise RuntimeError("既有控制 socket 路径不安全")
        socket_path.unlink()


class HeartbeatWriter(Protocol):
    def write_heartbeat(self) -> None: ...


async def heartbeat_loop(
    agent: HeartbeatWriter,
    *,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """每 10 秒刷新安全投影；单次写失败不终止后续重试。"""

    while True:
        with suppress(Exception):
            agent.write_heartbeat()
        await sleeper(HEARTBEAT_INTERVAL_SECONDS)


async def serve(agent: VendorControlAgent, *, socket_path: Path = CONTROL_SOCKET) -> None:
    """只绑定固定 Unix Socket；socket_path 参数仅供单元测试。"""

    if socket_path != CONTROL_SOCKET:
        raise RuntimeError("控制 socket 路径必须固定")
    _prepare_socket_path(
        socket_path,
        expected_uid=0,
        expected_gid=agent.expected_gid,
    )
    server = await asyncio.start_unix_server(
        lambda reader, writer: _serve_client(agent, reader, writer),
        path=socket_path,
        limit=MAX_FRAME_BYTES + 4,
    )
    secure_socket(socket_path, expected_uid=0, expected_gid=agent.expected_gid)
    heartbeat = asyncio.create_task(heartbeat_loop(agent), name="vendor-control-heartbeat")
    try:
        async with server:
            await server.serve_forever()
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the fixed vendor control agent")
    parser.add_argument("--api-runtime-uid", type=int, required=True)
    parser.add_argument("--api-runtime-gid", type=int, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if os.geteuid() != 0 or os.environ.get("SMS_SECRETS_MODE") != "development":
        return 1
    configured_root = os.environ.get("SMS_VENDOR_CREDENTIAL_ROOT", str(CREDENTIAL_ROOT))
    if Path(configured_root) != CREDENTIAL_ROOT:
        return 1
    if args.api_runtime_uid != 10001 or args.api_runtime_gid != 10001:
        return 1
    agent = VendorControlAgent(
        runner=FixedWrapperRunner(),
        credential_store=VendorCredentialStore(CREDENTIAL_ROOT),
        seal_sessions=SealSessionManager(),
        expected_uid=args.api_runtime_uid,
        expected_gid=args.api_runtime_gid,
        journal=VendorControlJournal(JOURNAL_ROOT),
        state_path=CONTROL_STATE,
    )
    asyncio.run(serve(agent))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
