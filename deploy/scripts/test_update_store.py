"""开发测试快速更新的私有、仅向前状态存储。"""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

from test_update_contract import (
    TestUpdateContractError,
    TestUpdateRequest,
    parse_test_update_request,
)


class TestUpdateStoreError(RuntimeError):
    """快速更新状态目录、文件或状态转换不满足安全约束。"""


class _MissingDirectoryError(FileNotFoundError):
    """安全目录链中存在尚未创建的尾部目录。"""


class TestUpdateState(StrEnum):
    PREPARED = "prepared"
    CHECKPOINTED = "checkpointed"
    MIGRATED = "migrated"
    APPLIED = "applied"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    ROLLED_BACK = "rolled_back"
    BLOCKED = "blocked"


DEFAULT_TEST_UPDATE_ROOT = Path("/var/lib/sms-platform/test-updates")

_TRANSITIONS: Mapping[TestUpdateState, frozenset[TestUpdateState]] = {
    TestUpdateState.PREPARED: frozenset(
        {
            TestUpdateState.CHECKPOINTED,
            TestUpdateState.APPLIED,
            TestUpdateState.ROLLED_BACK,
            TestUpdateState.BLOCKED,
        }
    ),
    TestUpdateState.CHECKPOINTED: frozenset(
        {TestUpdateState.MIGRATED, TestUpdateState.BLOCKED}
    ),
    TestUpdateState.MIGRATED: frozenset(
        {TestUpdateState.APPLIED, TestUpdateState.BLOCKED}
    ),
    TestUpdateState.APPLIED: frozenset(
        {
            TestUpdateState.VERIFIED,
            TestUpdateState.ROLLED_BACK,
            TestUpdateState.BLOCKED,
        }
    ),
    TestUpdateState.VERIFYING: frozenset(
        {TestUpdateState.VERIFIED, TestUpdateState.BLOCKED}
    ),
    TestUpdateState.VERIFIED: frozenset(),
    TestUpdateState.ROLLED_BACK: frozenset(),
    TestUpdateState.BLOCKED: frozenset({TestUpdateState.VERIFYING}),
}
_UPDATE_ID_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_-]{0,62}[A-Za-z0-9])?")
_STEP_RE = re.compile(r"[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_MIGRATION_HEAD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_UTC_TIMESTAMP_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:[.][0-9]{1,6})?Z"
)
_ERROR_TYPES = frozenset(
    {
        "step_failed",
        "command_failed",
        "health_check_failed",
        "invariant_failed",
        "validation_failed",
        "migration_failed",
        "state_observation_failed",
        "unknown_failure",
    }
)
_STATE_FIELDS = frozenset(
    {
        "schema_version",
        "update_id",
        "state",
        "step",
        "error_type",
        "actual_commit",
        "actual_migration_head",
        "event_sequence",
        "created_at",
        "updated_at",
    }
)
_EVENT_FIELDS = frozenset(
    {
        "schema_version",
        "sequence",
        "update_id",
        "kind",
        "from_state",
        "to_state",
        "step",
        "error_type",
        "actual_commit",
        "actual_migration_head",
        "timestamp",
    }
)
_CONTROLLED_FILES = frozenset({"request.json", "state.json", "events.jsonl"})
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_MAX_PRIVATE_FILE_BYTES = 4 * 1024 * 1024


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _json_bytes(value: Mapping[str, object]) -> bytes:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise TestUpdateStoreError("state value is not JSON serializable") from exc
    return f"{rendered}\n".encode()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TestUpdateStoreError(f"JSON contains duplicate field: {key}")
        result[key] = value
    return result


def _require_exact_fields(
    value: Mapping[str, object],
    expected: frozenset[str],
    context: str,
) -> None:
    actual = set(value)
    if actual != set(expected):
        raise TestUpdateStoreError(f"{context} fields are invalid")


def _require_timestamp(value: object, context: str) -> str:
    if type(value) is not str or _UTC_TIMESTAMP_RE.fullmatch(value) is None:
        raise TestUpdateStoreError(f"{context} timestamp is invalid")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TestUpdateStoreError(f"{context} timestamp is invalid") from exc
    return value


def _require_step(value: object) -> str:
    if type(value) is not str or _STEP_RE.fullmatch(value) is None:
        raise TestUpdateStoreError("state step is invalid")
    return value


def _require_error_type(value: object, *, required: bool) -> str | None:
    if value is None and not required:
        return None
    if type(value) is not str or value not in _ERROR_TYPES:
        raise TestUpdateStoreError("state error type is invalid")
    if not required:
        raise TestUpdateStoreError("non-failed state must not contain an error type")
    return value


def _require_commit(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or _COMMIT_RE.fullmatch(value) is None:
        raise TestUpdateStoreError("actual commit is invalid")
    return value


def _require_migration_head(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or _MIGRATION_HEAD_RE.fullmatch(value) is None:
        raise TestUpdateStoreError("actual migration head is invalid")
    return value


class TestUpdateStore:
    """持久化单个快速更新请求、状态和无敏感信息事件。"""

    def __init__(self, root: Path, update_id: str) -> None:
        if not isinstance(root, Path):
            raise TestUpdateStoreError("state root must be a Path")
        if not root.is_absolute() or ".." in root.parts:
            raise TestUpdateStoreError("state root must be absolute and normalized")
        if type(update_id) is not str or _UPDATE_ID_RE.fullmatch(update_id) is None:
            raise TestUpdateStoreError("invalid update ID")
        self.root = root
        self.update_id = update_id
        self.update_dir = self.root / update_id

    @property
    def _request_path(self) -> Path:
        return self.update_dir / "request.json"

    @property
    def _state_path(self) -> Path:
        return self.update_dir / "state.json"

    @property
    def _events_path(self) -> Path:
        return self.update_dir / "events.jsonl"

    def _reject_git_checkout_path(self) -> None:
        try:
            with self._open_anchored_directory(self.root):
                pass
        except _MissingDirectoryError:
            return

    @staticmethod
    def _validate_directory_info(
        info: os.stat_result,
        name: str,
        *,
        private: bool,
    ) -> None:
        if stat.S_ISLNK(info.st_mode):
            raise TestUpdateStoreError(
                f"directory ancestor must not be a symlink: {name}"
            )
        if not stat.S_ISDIR(info.st_mode):
            raise TestUpdateStoreError(f"directory ancestor is not a directory: {name}")
        if private and info.st_uid != os.geteuid():
            raise TestUpdateStoreError(f"private directory owner is invalid: {name}")
        if info.st_uid not in {0, os.geteuid()}:
            raise TestUpdateStoreError(f"directory ancestor owner is invalid: {name}")
        if private and stat.S_IMODE(info.st_mode) != 0o700:
            raise TestUpdateStoreError(f"directory mode must be 0700: {name}")

    @staticmethod
    def _reject_git_marker(descriptor: int) -> None:
        try:
            os.stat(".git", dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise TestUpdateStoreError("cannot inspect Git boundary") from exc
        raise TestUpdateStoreError("state root must be outside the Git checkout")

    @contextmanager
    def _open_anchored_directory(
        self,
        path: Path,
        *,
        private_leaf: bool = False,
    ) -> Iterator[tuple[int, os.stat_result]]:
        if not path.is_absolute() or ".." in path.parts:
            raise TestUpdateStoreError("directory path must be absolute and normalized")
        descriptor = os.open("/", os.O_RDONLY | _DIRECTORY | _NOFOLLOW)
        try:
            opened = os.fstat(descriptor)
            current_path = Path("/")
            private_node = current_path in {self.root, self.update_dir}
            self._validate_directory_info(opened, "/", private=private_node)
            self._reject_git_marker(descriptor)
            for component in path.parts[1:]:
                if component in {"", ".", ".."}:
                    raise TestUpdateStoreError("directory path contains an unsafe component")
                try:
                    before = os.stat(
                        component,
                        dir_fd=descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError as exc:
                    raise _MissingDirectoryError(component) from exc
                except OSError as exc:
                    raise TestUpdateStoreError(
                        f"cannot inspect directory ancestor: {component}"
                    ) from exc
                current_path /= component
                private_node = current_path in {self.root, self.update_dir}
                self._validate_directory_info(
                    before,
                    component,
                    private=private_node,
                )
                child = -1
                try:
                    child = os.open(
                        component,
                        os.O_RDONLY | _DIRECTORY | _NOFOLLOW,
                        dir_fd=descriptor,
                    )
                    opened = os.fstat(child)
                    self._validate_directory_info(
                        opened,
                        component,
                        private=private_node,
                    )
                    if (opened.st_dev, opened.st_ino) != (
                        before.st_dev,
                        before.st_ino,
                    ):
                        raise TestUpdateStoreError(
                            f"directory ancestor changed while opening: {component}"
                        )
                    self._reject_git_marker(child)
                except BaseException:
                    if child >= 0:
                        os.close(child)
                    raise
                os.close(descriptor)
                descriptor = child
            if private_leaf and not private_node:
                self._validate_directory_info(opened, path.name or "/", private=True)
            yield descriptor, opened
        finally:
            os.close(descriptor)

    def _validate_private_directory(self, path: Path) -> os.stat_result:
        with self._open_anchored_directory(path, private_leaf=True) as (_, info):
            return info

    def _ensure_private_directory(self, path: Path) -> None:
        try:
            self._validate_private_directory(path)
            return
        except _MissingDirectoryError:
            pass
        try:
            with self._open_anchored_directory(path.parent) as (parent_fd, _):
                with suppress(FileExistsError):
                    os.mkdir(path.name, mode=0o700, dir_fd=parent_fd)
                before = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
                self._validate_directory_info(before, path.name, private=True)
                child_fd = os.open(
                    path.name,
                    os.O_RDONLY | _DIRECTORY | _NOFOLLOW,
                    dir_fd=parent_fd,
                )
                try:
                    opened = os.fstat(child_fd)
                    self._validate_directory_info(opened, path.name, private=True)
                    if (opened.st_dev, opened.st_ino) != (
                        before.st_dev,
                        before.st_ino,
                    ):
                        raise TestUpdateStoreError(
                            f"private directory changed while opening: {path.name}"
                        )
                    os.fsync(parent_fd)
                finally:
                    os.close(child_fd)
        except _MissingDirectoryError:
            self._ensure_private_directory(path.parent)
            with self._open_anchored_directory(path.parent) as (parent_fd, _):
                with suppress(FileExistsError):
                    os.mkdir(path.name, mode=0o700, dir_fd=parent_fd)
                os.fsync(parent_fd)
        self._validate_private_directory(path)

    def _make_private_child(self, parent: Path, name: str) -> Path:
        child = parent / name
        with self._open_anchored_directory(parent, private_leaf=True) as (parent_fd, _):
            with suppress(FileExistsError):
                os.mkdir(name, mode=0o700, dir_fd=parent_fd)
            os.fsync(parent_fd)
        self._validate_private_directory(child)
        return child

    @staticmethod
    def _validate_regular_info(info: os.stat_result, name: str) -> os.stat_result:
        if stat.S_ISLNK(info.st_mode):
            raise TestUpdateStoreError(f"private file must not be a symlink: {name}")
        if not stat.S_ISREG(info.st_mode):
            raise TestUpdateStoreError(f"private path is not a regular file: {name}")
        if info.st_uid != os.geteuid():
            raise TestUpdateStoreError(f"private file owner is invalid: {name}")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise TestUpdateStoreError(f"private file mode is invalid: {name}")
        return info

    @classmethod
    def _validate_regular_at(
        cls,
        parent_fd: int,
        name: str,
    ) -> os.stat_result:
        try:
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise TestUpdateStoreError(f"required private file is missing: {name}") from exc
        return cls._validate_regular_info(info, name)

    def _validate_regular_file(self, path: Path) -> os.stat_result:
        with self._open_anchored_directory(path.parent, private_leaf=True) as (
            parent_fd,
            _,
        ):
            return self._validate_regular_at(parent_fd, path.name)

    def _read_regular_file(self, path: Path) -> bytes:
        with self._open_anchored_directory(path.parent, private_leaf=True) as (
            parent_fd,
            _,
        ):
            before = self._validate_regular_at(parent_fd, path.name)
            descriptor = os.open(
                path.name,
                os.O_RDONLY | _NOFOLLOW,
                dir_fd=parent_fd,
            )
            try:
                opened = os.fstat(descriptor)
                self._validate_regular_info(opened, path.name)
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise TestUpdateStoreError(
                        f"private file changed while opening: {path.name}"
                    )
                chunks: list[bytes] = []
                size = 0
                while True:
                    chunk = os.read(descriptor, 64 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > _MAX_PRIVATE_FILE_BYTES:
                        raise TestUpdateStoreError(
                            f"private file is too large: {path.name}"
                        )
                    chunks.append(chunk)
                return b"".join(chunks)
            finally:
                os.close(descriptor)

    def _fsync_directory(
        self,
        directory: Path,
        *,
        expected_identity: tuple[int, int] | None = None,
    ) -> None:
        with self._open_anchored_directory(directory, private_leaf=True) as (
            descriptor,
            opened,
        ):
            if expected_identity is not None and (opened.st_dev, opened.st_ino) != (
                expected_identity
            ):
                raise TestUpdateStoreError("private directory changed before fsync")
            self._validate_directory_info(opened, directory.name, private=True)
            os.fsync(descriptor)

    def _atomic_write(self, path: Path, data: bytes) -> None:
        with self._open_anchored_directory(path.parent, private_leaf=True) as (
            parent_fd,
            parent_info,
        ):
            try:
                self._validate_regular_at(parent_fd, path.name)
            except TestUpdateStoreError as exc:
                try:
                    os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise exc

            temporary = f".{path.name}.{uuid.uuid4().hex}.tmp"
            descriptor = -1
            try:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                    0o600,
                    dir_fd=parent_fd,
                )
                os.fchmod(descriptor, 0o600)
                view = memoryview(data)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("private atomic write made no progress")
                    view = view[written:]
                os.fsync(descriptor)
                os.close(descriptor)
                descriptor = -1
                os.replace(
                    temporary,
                    path.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                self._validate_regular_at(parent_fd, path.name)
                self._fsync_directory(
                    path.parent,
                    expected_identity=(parent_info.st_dev, parent_info.st_ino),
                )
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=parent_fd)

    def _commit_state_and_events(
        self,
        *,
        previous_state: bytes,
        next_state: bytes,
        previous_events: bytes,
        next_events: bytes,
    ) -> None:
        """提交成对状态；任一步失败即补偿回调用前的完整字节。"""

        try:
            self._atomic_write(self._events_path, next_events)
            self._atomic_write(self._state_path, next_state)
        except BaseException:
            compensation_failed = False
            try:
                self._atomic_write(self._state_path, previous_state)
            except BaseException:
                compensation_failed = True
            try:
                self._atomic_write(self._events_path, previous_events)
            except BaseException:
                compensation_failed = True
            if compensation_failed:
                raise TestUpdateStoreError(
                    "state/event atomic commit is corrupt"
                ) from None
            raise

    def _cleanup_temporary_directory(self, directory: Path) -> None:
        try:
            with self._open_anchored_directory(
                directory,
                private_leaf=True,
            ) as (directory_fd, directory_info):
                actual = set(os.listdir(directory_fd))
                unexpected = actual - set(_CONTROLLED_FILES)
                if unexpected:
                    raise TestUpdateStoreError(
                        "temporary update directory contents are invalid"
                    )
                for filename in actual:
                    self._validate_regular_at(directory_fd, filename)
                    os.unlink(filename, dir_fd=directory_fd)
                self._fsync_directory(
                    directory,
                    expected_identity=(directory_info.st_dev, directory_info.st_ino),
                )
        except _MissingDirectoryError:
            return
        with self._open_anchored_directory(
            directory.parent,
            private_leaf=True,
        ) as (parent_fd, _):
            try:
                os.rmdir(directory.name, dir_fd=parent_fd)
            except FileNotFoundError:
                return
            os.fsync(parent_fd)

    def _validate_update_directory_contents(self) -> None:
        with self._open_anchored_directory(
            self.update_dir,
            private_leaf=True,
        ) as (directory_fd, _):
            try:
                actual = set(os.listdir(directory_fd))
            except OSError as exc:
                raise TestUpdateStoreError("cannot inspect update directory") from exc
            if actual != set(_CONTROLLED_FILES):
                raise TestUpdateStoreError("update directory contents are invalid")
            for filename in _CONTROLLED_FILES:
                self._validate_regular_at(directory_fd, filename)

    def _decode_request(self, raw: bytes) -> TestUpdateRequest:
        try:
            text = raw.decode("utf-8")
        except UnicodeError as exc:
            raise TestUpdateStoreError("request file is not valid UTF-8") from exc
        try:
            request = parse_test_update_request(text)
        except TestUpdateContractError as exc:
            raise TestUpdateStoreError("request file violates the update contract") from exc
        if request.update_id != self.update_id:
            raise TestUpdateStoreError("request update ID does not match state store")
        return request

    def _parse_state(self, raw: bytes) -> dict[str, object]:
        try:
            decoded = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
        except TestUpdateStoreError:
            raise
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise TestUpdateStoreError("state file is not valid JSON") from exc
        if type(decoded) is not dict:
            raise TestUpdateStoreError("state file must contain an object")
        state = cast(dict[str, object], decoded)
        _require_exact_fields(state, _STATE_FIELDS, "state")
        if type(state["schema_version"]) is not int or state["schema_version"] != 1:
            raise TestUpdateStoreError("state schema version is invalid")
        if type(state["update_id"]) is not str or state["update_id"] != self.update_id:
            raise TestUpdateStoreError("state update ID is invalid")
        state_value = state["state"]
        if type(state_value) is not str:
            raise TestUpdateStoreError("state value is invalid")
        try:
            current = TestUpdateState(state_value)
        except (TypeError, ValueError) as exc:
            raise TestUpdateStoreError("state value is invalid") from exc
        step = _require_step(state["step"])
        error_type = _require_error_type(
            state["error_type"], required=current is TestUpdateState.BLOCKED
        )
        actual_commit = _require_commit(state["actual_commit"])
        migration_head = _require_migration_head(state["actual_migration_head"])
        event_sequence = state["event_sequence"]
        if type(event_sequence) is not int or event_sequence < 1:
            raise TestUpdateStoreError("state event sequence is invalid")
        created_at = _require_timestamp(state["created_at"], "created_at")
        updated_at = _require_timestamp(state["updated_at"], "updated_at")
        return {
            "schema_version": 1,
            "update_id": self.update_id,
            "state": current.value,
            "step": step,
            "error_type": error_type,
            "actual_commit": actual_commit,
            "actual_migration_head": migration_head,
            "event_sequence": event_sequence,
            "created_at": created_at,
            "updated_at": updated_at,
        }

    def _parse_events(self, raw: bytes) -> list[dict[str, object]]:
        if not raw or not raw.endswith(b"\n"):
            raise TestUpdateStoreError("events file framing is invalid")
        lines = raw.splitlines()
        if not lines or any(not line for line in lines):
            raise TestUpdateStoreError("events file contains an empty event")
        events: list[dict[str, object]] = []
        previous_target: TestUpdateState | None = None
        for sequence, line in enumerate(lines, start=1):
            try:
                decoded = json.loads(line, object_pairs_hook=_reject_duplicate_keys)
            except TestUpdateStoreError:
                raise
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise TestUpdateStoreError("event is not valid JSON") from exc
            if type(decoded) is not dict:
                raise TestUpdateStoreError("event must contain an object")
            event = cast(dict[str, object], decoded)
            _require_exact_fields(event, _EVENT_FIELDS, "event")
            if type(event["schema_version"]) is not int or event["schema_version"] != 1:
                raise TestUpdateStoreError("event schema version is invalid")
            if type(event["sequence"]) is not int or event["sequence"] != sequence:
                raise TestUpdateStoreError("event sequence is invalid")
            if type(event["update_id"]) is not str or event["update_id"] != self.update_id:
                raise TestUpdateStoreError("event update ID is invalid")
            if event["kind"] != "state_transition" or type(event["kind"]) is not str:
                raise TestUpdateStoreError("event kind is invalid")
            target_raw = event["to_state"]
            if type(target_raw) is not str:
                raise TestUpdateStoreError("event target state is invalid")
            try:
                target = TestUpdateState(target_raw)
            except (TypeError, ValueError) as exc:
                raise TestUpdateStoreError("event target state is invalid") from exc
            from_raw = event["from_state"]
            if sequence == 1:
                if from_raw is not None or target is not TestUpdateState.PREPARED:
                    raise TestUpdateStoreError("initial event state is invalid")
            else:
                if type(from_raw) is not str:
                    raise TestUpdateStoreError("event source state is invalid")
                try:
                    source = TestUpdateState(from_raw)
                except (TypeError, ValueError) as exc:
                    raise TestUpdateStoreError("event source state is invalid") from exc
                if source is not previous_target or target not in _TRANSITIONS[source]:
                    raise TestUpdateStoreError("event state transition is invalid")
            step = _require_step(event["step"])
            error_type = _require_error_type(
                event["error_type"], required=target is TestUpdateState.BLOCKED
            )
            actual_commit = _require_commit(event["actual_commit"])
            migration_head = _require_migration_head(event["actual_migration_head"])
            timestamp = _require_timestamp(event["timestamp"], "event")
            events.append(
                {
                    "schema_version": 1,
                    "sequence": sequence,
                    "update_id": self.update_id,
                    "kind": "state_transition",
                    "from_state": None if sequence == 1 else cast(str, from_raw),
                    "to_state": target.value,
                    "step": step,
                    "error_type": error_type,
                    "actual_commit": actual_commit,
                    "actual_migration_head": migration_head,
                    "timestamp": timestamp,
                }
            )
            previous_target = target
        return events

    def _load_consistent_state(self) -> tuple[dict[str, object], list[dict[str, object]]]:
        state = self.read_state()
        events = self.read_events()
        latest = events[-1]
        if (
            state["event_sequence"] != latest["sequence"]
            or state["state"] != latest["to_state"]
            or state["step"] != latest["step"]
            or state["error_type"] != latest["error_type"]
            or state["actual_commit"] != latest["actual_commit"]
            or state["actual_migration_head"] != latest["actual_migration_head"]
            or state["updated_at"] != latest["timestamp"]
        ):
            raise TestUpdateStoreError("state and events are inconsistent")
        return state, events

    def create(self, request: str) -> None:
        """原子创建更新记录；只有完全相同的请求可幂等重试。"""

        if type(request) is not str:
            raise TestUpdateStoreError("request must be an exact str")
        request_bytes = request.encode("utf-8")
        if not request_bytes or len(request_bytes) > _MAX_PRIVATE_FILE_BYTES:
            raise TestUpdateStoreError("request size is invalid")
        self._decode_request(request_bytes)
        self._reject_git_checkout_path()
        self._ensure_private_directory(self.root)

        try:
            self._validate_private_directory(self.update_dir)
        except _MissingDirectoryError:
            temporary = self._make_private_child(
                self.root,
                f".{self.update_id}.{uuid.uuid4().hex}.tmpdir",
            )
            try:
                timestamp = _utc_now()
                event = {
                    "schema_version": 1,
                    "sequence": 1,
                    "update_id": self.update_id,
                    "kind": "state_transition",
                    "from_state": None,
                    "to_state": TestUpdateState.PREPARED.value,
                    "step": "create",
                    "error_type": None,
                    "actual_commit": None,
                    "actual_migration_head": None,
                    "timestamp": timestamp,
                }
                state = {
                    "schema_version": 1,
                    "update_id": self.update_id,
                    "state": TestUpdateState.PREPARED.value,
                    "step": "create",
                    "error_type": None,
                    "actual_commit": None,
                    "actual_migration_head": None,
                    "event_sequence": 1,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                }
                self._atomic_write(temporary / "request.json", request_bytes)
                self._atomic_write(temporary / "events.jsonl", _json_bytes(event))
                self._atomic_write(temporary / "state.json", _json_bytes(state))
                with self._open_anchored_directory(
                    self.root,
                    private_leaf=True,
                ) as (root_fd, root_info):
                    os.rename(
                        temporary.name,
                        self.update_id,
                        src_dir_fd=root_fd,
                        dst_dir_fd=root_fd,
                    )
                self._fsync_directory(
                    self.root,
                    expected_identity=(root_info.st_dev, root_info.st_ino),
                )
            except BaseException:
                self._cleanup_temporary_directory(temporary)
                raise
            self._validate_update_directory_contents()
            return

        self._validate_update_directory_contents()
        existing = self._read_regular_file(self._request_path)
        self._decode_request(existing)
        if existing != request_bytes:
            raise TestUpdateStoreError("duplicate update ID has a different request")
        self._load_consistent_state()

    def read_request(self) -> TestUpdateRequest:
        """严格读取并返回已验证的不可变快速更新请求。"""

        self._validate_update_directory_contents()
        return self._decode_request(self._read_regular_file(self._request_path))

    def read_state(self) -> dict[str, object]:
        """严格读取固定字段的当前状态。"""

        self._validate_update_directory_contents()
        return self._parse_state(self._read_regular_file(self._state_path))

    def read_events(self) -> list[dict[str, object]]:
        """严格读取连续、固定字段的状态转换事件。"""

        self._validate_update_directory_contents()
        return self._parse_events(self._read_regular_file(self._events_path))

    def read_consistent_state(self) -> dict[str, object]:
        """读取同时通过事件链一致性校验的当前状态。"""

        state, _events = self._load_consistent_state()
        return state

    def transition(
        self,
        expected: TestUpdateState,
        target: TestUpdateState,
        *,
        step: str = "state_transition",
        error_type: str | None = None,
        actual_commit: str | None = None,
        actual_migration_head: str | None = None,
    ) -> None:
        """按唯一邻接表先记录事件，再原子推进状态。"""

        if type(expected) is not TestUpdateState or type(target) is not TestUpdateState:
            raise TestUpdateStoreError("state transition values are invalid")
        current, events = self._load_consistent_state()
        current_value = current["state"]
        if type(current_value) is not str:
            raise TestUpdateStoreError("current state is invalid")
        try:
            current_state = TestUpdateState(current_value)
        except (TypeError, ValueError) as exc:
            raise TestUpdateStoreError("current state is invalid") from exc
        if current_state is not expected:
            raise TestUpdateStoreError("update state does not match expected state")
        if not _TRANSITIONS[current_state]:
            raise TestUpdateStoreError("terminal update state cannot transition")
        if target not in _TRANSITIONS[current_state]:
            raise TestUpdateStoreError(
                f"illegal state transition: {current_state.value} -> {target.value}"
            )
        checked_step = _require_step(step)
        checked_error = _require_error_type(
            error_type, required=target is TestUpdateState.BLOCKED
        )
        checked_commit = _require_commit(actual_commit)
        checked_head = _require_migration_head(actual_migration_head)
        timestamp = _utc_now()
        sequence = cast(int, current["event_sequence"]) + 1
        event = {
            "schema_version": 1,
            "sequence": sequence,
            "update_id": self.update_id,
            "kind": "state_transition",
            "from_state": current_state.value,
            "to_state": target.value,
            "step": checked_step,
            "error_type": checked_error,
            "actual_commit": checked_commit,
            "actual_migration_head": checked_head,
            "timestamp": timestamp,
        }
        updated = {
            **current,
            "state": target.value,
            "step": checked_step,
            "error_type": checked_error,
            "actual_commit": checked_commit,
            "actual_migration_head": checked_head,
            "event_sequence": sequence,
            "updated_at": timestamp,
        }
        if len(events) + 1 != sequence:
            raise TestUpdateStoreError("event sequence changed during transition")
        previous_state_bytes = self._read_regular_file(self._state_path)
        previous_event_bytes = self._read_regular_file(self._events_path)
        self._commit_state_and_events(
            previous_state=previous_state_bytes,
            next_state=_json_bytes(updated),
            previous_events=previous_event_bytes,
            next_events=previous_event_bytes + _json_bytes(event),
        )

    def block(
        self,
        expected: TestUpdateState,
        *,
        step: str,
        error_type: str = "step_failed",
        actual_commit: str | None = None,
        actual_migration_head: str | None = None,
    ) -> None:
        """以固定错误类型阻断本次更新，不接受异常文本或任意载荷。"""

        self.transition(
            expected,
            TestUpdateState.BLOCKED,
            step=step,
            error_type=error_type,
            actual_commit=actual_commit,
            actual_migration_head=actual_migration_head,
        )

    def fail(
        self,
        expected: TestUpdateState,
        *,
        step: str,
        error_type: str = "step_failed",
        actual_commit: str | None = None,
        actual_migration_head: str | None = None,
    ) -> None:
        """兼容早期调用方；语义统一为 blocked。"""

        self.block(
            expected,
            step=step,
            error_type=error_type,
            actual_commit=actual_commit,
            actual_migration_head=actual_migration_head,
        )
