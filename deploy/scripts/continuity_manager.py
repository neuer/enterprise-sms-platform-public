#!/usr/bin/env python3
"""持久化生产旧系统切回围栏，并在消费者确认停止前失败关闭。"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast

STATE_ROOT = Path("/var/lib/sms-platform/continuity")
STATE_FILE = "state.json"
MAX_JSON_BYTES = 32 * 1024
SHA256 = re.compile(r"^[0-9a-f]{64}$")
OPAQUE_ID = re.compile(r"^[0-9a-f]{32}$")
UTC_SECONDS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
APPROVAL_MAX_AGE = timedelta(hours=2)
FUTURE_SKEW = timedelta(minutes=5)
CONSUMER_SERVICES = (
    "web",
    "api",
    "worker-realtime",
    "worker-bulk",
    "worker-callback",
    "outbox-dispatcher",
    "beat",
)
COMPOSE_FILES = (
    "docker-compose.yml",
    "docker-compose.production-storage.yml",
    "docker-compose.production-restart.yml",
    "docker-compose.redis-tls.yml",
)
ENGAGE_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "evidence_id",
        "approved_at",
        "outage_start",
        "business_rto_seconds",
        "old_system_fallback_allowed",
    }
)
RELEASE_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "fence_id",
        "engage_evidence_sha256",
        "outage_start",
        "approved_at",
        "change_record_sha256",
        "approver_one_subject_sha256",
        "approver_two_subject_sha256",
        "approver_one_controlled",
        "approver_two_controlled",
        "old_route_disabled",
        "new_route_exclusive",
        "inflight_reconciled",
        "uncertain_no_auto_resend",
    }
)
STATE_FIELDS = frozenset(
    {
        "schema_version",
        "fence_id",
        "status",
        "outage_start",
        "engage_evidence_sha256",
        "consumer_stop_verified",
        "engaged_at",
        "release_evidence_sha256",
        "released_at",
        "failure_code",
        "updated_at",
    }
)


class ContinuityError(RuntimeError):
    """围栏输入、持久状态或消费者读回违反失败关闭契约。"""


class Runner(Protocol):
    def run(self, command: Sequence[str]) -> bytes: ...


class SubprocessRunner:
    """以固定 argv 执行 Compose；不把命令输出拼入错误。"""

    def run(self, command: Sequence[str]) -> bytes:
        try:
            completed = subprocess.run(
                list(command),
                check=False,
                capture_output=True,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ContinuityError("continuity compose command unavailable") from exc
        if completed.returncode != 0:
            raise ContinuityError("continuity compose command failed")
        return completed.stdout


def utc_now() -> datetime:
    return datetime.now(UTC)


def _format_utc(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(value: object, context: str) -> datetime:
    if type(value) is not str or UTC_SECONDS.fullmatch(value) is None:
        raise ContinuityError(f"invalid {context}")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ContinuityError(f"invalid {context}") from exc
    return parsed


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContinuityError("continuity JSON contains duplicate fields")
        result[key] = value
    return result


def _parse_exact_json(
    raw: bytes, fields: frozenset[str], context: str
) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except ContinuityError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ContinuityError(f"invalid {context} JSON") from exc
    if type(value) is not dict or set(value) != fields:
        raise ContinuityError(f"invalid {context} fields")
    return cast(dict[str, Any], value)


def _read_safe_file(
    path: Path,
    *,
    expected_uid: int,
    expected_gid: int,
) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise ContinuityError("continuity evidence is unavailable") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ContinuityError("continuity evidence must be a regular non-symlink file")
    if before.st_uid != expected_uid or before.st_gid != expected_gid:
        raise ContinuityError("continuity evidence ownership is invalid")
    if stat.S_IMODE(before.st_mode) != 0o600:
        raise ContinuityError("continuity evidence permissions must be 0600")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ContinuityError("continuity evidence changed while opening")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, 8192)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_JSON_BYTES:
                raise ContinuityError("continuity evidence is too large")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
            or after.st_ctime_ns != opened.st_ctime_ns
        ):
            raise ContinuityError("continuity evidence changed while reading")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _validate_private_directory(
    path: Path, *, expected_uid: int, expected_gid: int
) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ContinuityError("continuity state directory is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ContinuityError("continuity state directory is unsafe")
    if info.st_uid != expected_uid or info.st_gid != expected_gid:
        raise ContinuityError("continuity state directory ownership is invalid")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise ContinuityError("continuity state directory permissions must be 0700")


def _atomic_write(
    path: Path,
    raw: bytes,
    *,
    expected_uid: int,
    expected_gid: int,
) -> None:
    _validate_private_directory(
        path.parent, expected_uid=expected_uid, expected_gid=expected_gid
    )
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        raise ContinuityError("continuity destination metadata is unavailable") from exc
    if existing is not None and (
        stat.S_ISLNK(existing.st_mode)
        or not stat.S_ISREG(existing.st_mode)
        or existing.st_uid != expected_uid
        or existing.st_gid != expected_gid
        or stat.S_IMODE(existing.st_mode) != 0o600
    ):
        raise ContinuityError("continuity destination is unsafe")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        os.fchown(descriptor, expected_uid, expected_gid)
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise ContinuityError("continuity state write made no progress")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != expected_uid
            or info.st_gid != expected_gid
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ContinuityError("continuity persisted file metadata is unsafe")
        directory_fd = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with suppress(FileNotFoundError):
            temporary.unlink()


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("ascii")


class ContinuityManager:
    """维护 production-only 持久围栏；任何不完整状态均阻断启动。"""

    def __init__(
        self,
        *,
        root: Path,
        state_root: Path = STATE_ROOT,
        runner: Runner | None = None,
        clock: Callable[[], datetime] = utc_now,
        expected_uid: int = 0,
        expected_gid: int = 0,
    ) -> None:
        self.root = root.absolute()
        self.state_root = state_root.absolute()
        self.runner = runner or SubprocessRunner()
        self.clock = clock
        self.expected_uid = expected_uid
        self.expected_gid = expected_gid

    @property
    def state_path(self) -> Path:
        return self.state_root / STATE_FILE

    def _ensure_state_root(self) -> None:
        if not self.state_root.exists():
            try:
                self.state_root.mkdir(mode=0o700)
            except OSError as exc:
                raise ContinuityError(
                    "continuity state directory cannot be created"
                ) from exc
            os.chmod(self.state_root, 0o700, follow_symlinks=False)
        _validate_private_directory(
            self.state_root,
            expected_uid=self.expected_uid,
            expected_gid=self.expected_gid,
        )

    def _read_input(
        self, path: Path, expected_sha256: str, context: str
    ) -> tuple[dict[str, Any], bytes]:
        if not path.is_absolute() or not SHA256.fullmatch(expected_sha256):
            raise ContinuityError(f"invalid {context} binding")
        raw = _read_safe_file(
            path,
            expected_uid=self.expected_uid,
            expected_gid=self.expected_gid,
        )
        actual = hashlib.sha256(raw).hexdigest()
        if not hmac.compare_digest(actual, expected_sha256):
            raise ContinuityError(f"{context} digest mismatch")
        fields = ENGAGE_FIELDS if context == "engage evidence" else RELEASE_FIELDS
        return _parse_exact_json(raw, fields, context), raw

    def _validate_approval(
        self, value: object, context: str, *, require_fresh: bool
    ) -> str:
        parsed = _parse_utc(value, context)
        if require_fresh:
            now = self.clock().astimezone(UTC)
            if parsed > now + FUTURE_SKEW or now - parsed > APPROVAL_MAX_AGE:
                raise ContinuityError(f"{context} is not fresh")
        return cast(str, value)

    def _validate_engage_evidence(
        self, value: Mapping[str, Any], *, require_fresh: bool
    ) -> None:
        if (
            value["schema_version"] != 1
            or value["kind"] != "production_continuity_engage"
            or type(value["evidence_id"]) is not str
            or OPAQUE_ID.fullmatch(cast(str, value["evidence_id"])) is None
            or value["business_rto_seconds"] != 43200
            or value["old_system_fallback_allowed"] is not True
        ):
            raise ContinuityError("invalid engage evidence values")
        self._validate_approval(
            value["approved_at"], "engage approval", require_fresh=require_fresh
        )
        outage_start = _parse_utc(value["outage_start"], "outage start")
        if outage_start > self.clock().astimezone(UTC) + FUTURE_SKEW:
            raise ContinuityError("outage start is in the future")

    def _validate_release_evidence(
        self,
        value: Mapping[str, Any],
        state: Mapping[str, Any],
        *,
        require_fresh: bool,
    ) -> None:
        hashes = (
            value["change_record_sha256"],
            value["approver_one_subject_sha256"],
            value["approver_two_subject_sha256"],
        )
        if (
            value["schema_version"] != 1
            or value["kind"] != "production_continuity_release"
            or value["fence_id"] != state["fence_id"]
            or value["engage_evidence_sha256"] != state["engage_evidence_sha256"]
            or value["outage_start"] != state["outage_start"]
            or any(
                type(item) is not str or SHA256.fullmatch(cast(str, item)) is None
                for item in hashes
            )
            or hmac.compare_digest(cast(str, hashes[1]), cast(str, hashes[2]))
        ):
            raise ContinuityError("release evidence is not bound to the current fence")
        for field in (
            "approver_one_controlled",
            "approver_two_controlled",
            "old_route_disabled",
            "new_route_exclusive",
            "inflight_reconciled",
            "uncertain_no_auto_resend",
        ):
            if value[field] is not True:
                raise ContinuityError("release evidence assertions are incomplete")
        self._validate_approval(
            value["approved_at"], "release approval", require_fresh=require_fresh
        )

    def _evidence_path(self, kind: str, fence_id: str) -> Path:
        if kind not in {"engage", "release"} or OPAQUE_ID.fullmatch(fence_id) is None:
            raise ContinuityError("invalid continuity evidence reference")
        return self.state_root / f"{kind}-{fence_id}.json"

    def _persist_evidence(self, kind: str, fence_id: str, raw: bytes) -> None:
        path = self._evidence_path(kind, fence_id)
        try:
            existing = _read_safe_file(
                path,
                expected_uid=self.expected_uid,
                expected_gid=self.expected_gid,
            )
        except ContinuityError:
            if path.exists() or path.is_symlink():
                raise
            _atomic_write(
                path,
                raw,
                expected_uid=self.expected_uid,
                expected_gid=self.expected_gid,
            )
            return
        if not hmac.compare_digest(
            hashlib.sha256(existing).digest(), hashlib.sha256(raw).digest()
        ):
            raise ContinuityError("stored continuity evidence differs")

    def _write_state(self, value: Mapping[str, Any]) -> None:
        if set(value) != STATE_FIELDS:
            raise ContinuityError("invalid continuity state fields")
        _atomic_write(
            self.state_path,
            _json_bytes(value),
            expected_uid=self.expected_uid,
            expected_gid=self.expected_gid,
        )

    def _read_state(self) -> dict[str, Any] | None:
        if not self.state_path.exists() and not self.state_path.is_symlink():
            if self.state_root.exists() or self.state_root.is_symlink():
                _validate_private_directory(
                    self.state_root,
                    expected_uid=self.expected_uid,
                    expected_gid=self.expected_gid,
                )
            return None
        _validate_private_directory(
            self.state_root,
            expected_uid=self.expected_uid,
            expected_gid=self.expected_gid,
        )
        raw = _read_safe_file(
            self.state_path,
            expected_uid=self.expected_uid,
            expected_gid=self.expected_gid,
        )
        state = _parse_exact_json(raw, STATE_FIELDS, "continuity state")
        if (
            state["schema_version"] != 1
            or type(state["fence_id"]) is not str
            or OPAQUE_ID.fullmatch(cast(str, state["fence_id"])) is None
            or state["status"] not in {"intent", "engaged", "released"}
            or type(state["engage_evidence_sha256"]) is not str
            or SHA256.fullmatch(cast(str, state["engage_evidence_sha256"])) is None
            or type(state["consumer_stop_verified"]) is not bool
        ):
            raise ContinuityError("invalid continuity state values")
        _parse_utc(state["outage_start"], "stored outage start")
        _parse_utc(state["updated_at"], "continuity update time")
        fence_id = cast(str, state["fence_id"])
        engage_raw = _read_safe_file(
            self._evidence_path("engage", fence_id),
            expected_uid=self.expected_uid,
            expected_gid=self.expected_gid,
        )
        if not hmac.compare_digest(
            hashlib.sha256(engage_raw).hexdigest(),
            cast(str, state["engage_evidence_sha256"]),
        ):
            raise ContinuityError("stored engage evidence digest mismatch")
        engage_evidence = _parse_exact_json(
            engage_raw, ENGAGE_FIELDS, "stored engage evidence"
        )
        self._validate_engage_evidence(engage_evidence, require_fresh=False)
        if engage_evidence["outage_start"] != state["outage_start"]:
            raise ContinuityError("stored engage evidence is not bound to state")
        if state["status"] == "intent":
            if (
                state["engaged_at"] is not None
                or state["released_at"] is not None
                or state["release_evidence_sha256"] is not None
                or state["consumer_stop_verified"] is not False
                or state["failure_code"] not in {None, "consumer_stop_failed"}
            ):
                raise ContinuityError("invalid intent continuity state")
        elif state["status"] == "engaged":
            if (
                state["consumer_stop_verified"] is not True
                or state["engaged_at"] is None
                or state["released_at"] is not None
                or state["release_evidence_sha256"] is not None
                or state["failure_code"] is not None
            ):
                raise ContinuityError("invalid engaged continuity state")
            _parse_utc(state["engaged_at"], "continuity engaged time")
        else:
            if (
                state["consumer_stop_verified"] is not True
                or state["released_at"] is None
                or type(state["release_evidence_sha256"]) is not str
                or SHA256.fullmatch(cast(str, state["release_evidence_sha256"])) is None
                or state["failure_code"] is not None
            ):
                raise ContinuityError("invalid released continuity state")
            if state["engaged_at"] is not None:
                _parse_utc(state["engaged_at"], "continuity engaged time")
            _parse_utc(state["released_at"], "continuity released time")
            release_raw = _read_safe_file(
                self._evidence_path("release", fence_id),
                expected_uid=self.expected_uid,
                expected_gid=self.expected_gid,
            )
            if not hmac.compare_digest(
                hashlib.sha256(release_raw).hexdigest(),
                cast(str, state["release_evidence_sha256"]),
            ):
                raise ContinuityError("stored release evidence digest mismatch")
            release = _parse_exact_json(
                release_raw, RELEASE_FIELDS, "stored release evidence"
            )
            self._validate_release_evidence(release, state, require_fresh=False)
        return state

    def _compose(self) -> list[str]:
        return [
            "docker",
            "compose",
            "--env-file",
            str(self.root / ".env"),
            *(
                argument
                for filename in COMPOSE_FILES
                for argument in ("-f", str(self.root / "deploy" / filename))
            ),
        ]

    def _consumers_are_stopped(self) -> bool:
        command = self._compose()
        for service in CONSUMER_SERVICES:
            output = self.runner.run(
                [*command, "ps", "--status", "running", "-q", service]
            )
            if output.strip():
                return False
        return True

    def gate(self) -> dict[str, Any]:
        state = self._read_state()
        if state is not None and state["status"] != "released":
            raise ContinuityError("production continuity fence is active")
        return {
            "schema_version": 1,
            "status": "released" if state is not None else "absent",
            "blocked": False,
        }

    def status(self) -> dict[str, Any]:
        state = self._read_state()
        if state is None:
            return {"schema_version": 1, "status": "absent", "blocked": False}
        return {
            "schema_version": 1,
            "status": state["status"],
            "blocked": state["status"] != "released",
            "fence_id": state["fence_id"],
            "outage_start": state["outage_start"],
            "consumer_stop_verified": state["consumer_stop_verified"],
            "updated_at": state["updated_at"],
        }

    def engage(self, evidence_path: Path, evidence_sha256: str) -> dict[str, Any]:
        evidence, raw = self._read_input(
            evidence_path, evidence_sha256, "engage evidence"
        )
        self._validate_engage_evidence(evidence, require_fresh=True)
        self._ensure_state_root()
        existing = self._read_state()
        now = _format_utc(self.clock())
        if existing is not None and existing["status"] != "released":
            if not hmac.compare_digest(
                cast(str, existing["engage_evidence_sha256"]), evidence_sha256
            ):
                raise ContinuityError("a different continuity fence is already active")
            state = existing
        else:
            fence_id = secrets.token_hex(16)
            self._persist_evidence("engage", fence_id, raw)
            state = {
                "schema_version": 1,
                "fence_id": fence_id,
                "status": "intent",
                "outage_start": evidence["outage_start"],
                "engage_evidence_sha256": evidence_sha256,
                "consumer_stop_verified": False,
                "engaged_at": None,
                "release_evidence_sha256": None,
                "released_at": None,
                "failure_code": None,
                "updated_at": now,
            }
            self._write_state(state)

        stop_failed = False
        try:
            self.runner.run([*self._compose(), "stop", *CONSUMER_SERVICES])
        except ContinuityError:
            stop_failed = True
        try:
            consumers_stopped = self._consumers_are_stopped()
        except ContinuityError:
            consumers_stopped = False
        if stop_failed or not consumers_stopped:
            failed = {
                **state,
                "status": "intent",
                "consumer_stop_verified": False,
                "engaged_at": None,
                "release_evidence_sha256": None,
                "released_at": None,
                "failure_code": "consumer_stop_failed",
                "updated_at": _format_utc(self.clock()),
            }
            self._write_state(failed)
            raise ContinuityError("continuity consumer stop verification failed")
        engaged = {
            **state,
            "status": "engaged",
            "consumer_stop_verified": True,
            "engaged_at": _format_utc(self.clock()),
            "release_evidence_sha256": None,
            "released_at": None,
            "failure_code": None,
            "updated_at": _format_utc(self.clock()),
        }
        self._write_state(engaged)
        return self.status()

    def release(self, evidence_path: Path, evidence_sha256: str) -> dict[str, Any]:
        state = self._read_state()
        if state is None:
            raise ContinuityError("no continuity fence exists")
        evidence, raw = self._read_input(
            evidence_path, evidence_sha256, "release evidence"
        )
        self._validate_release_evidence(evidence, state, require_fresh=True)
        if state["status"] == "released":
            if not hmac.compare_digest(
                cast(str, state["release_evidence_sha256"]), evidence_sha256
            ):
                raise ContinuityError(
                    "continuity fence was released with different evidence"
                )
            return self.status()
        if not self._consumers_are_stopped():
            raise ContinuityError("continuity consumers are not stopped")
        fence_id = cast(str, state["fence_id"])
        self._persist_evidence("release", fence_id, raw)
        released = {
            **state,
            "status": "released",
            "consumer_stop_verified": True,
            "release_evidence_sha256": evidence_sha256,
            "released_at": _format_utc(self.clock()),
            "failure_code": None,
            "updated_at": _format_utc(self.clock()),
        }
        self._write_state(released)
        return self.status()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="production continuity fence")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--mode", required=True, choices=("production", "development"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("gate")
    subparsers.add_parser("status")
    for name in ("engage", "release"):
        command = subparsers.add_parser(name)
        command.add_argument("--evidence", required=True, type=Path)
        command.add_argument("--evidence-sha256", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.mode != "production":
        raise ContinuityError("continuity fence is production-only")
    if (
        arguments.command in {"engage", "release"}
        and (
            os.environ.get("SMS_LIFECYCLE_LOCKED") != "1"
            or not os.environ.get("SMS_LIFECYCLE_LOCK_FD", "").isdecimal()
        )
    ):
        raise ContinuityError("continuity mutation requires lifecycle lock")
    manager = ContinuityManager(root=arguments.root)
    if arguments.command == "gate":
        result = manager.gate()
    elif arguments.command == "status":
        try:
            result = manager.status()
        except ContinuityError:
            print(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "invalid",
                        "blocked": True,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            return 1
    elif arguments.command == "engage":
        result = manager.engage(arguments.evidence, arguments.evidence_sha256)
    else:
        result = manager.release(arguments.evidence, arguments.evidence_sha256)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ContinuityError as exc:
        print(f"continuity: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
