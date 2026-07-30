"""仓库外私有发布状态的安全、原子持久化。"""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, cast


class ReleaseStoreError(RuntimeError):
    """发布状态目录或状态迁移不满足安全约束。"""


class ReleaseState(StrEnum):
    STAGED = "staged"
    PREPARED = "prepared"
    ACTIVATING = "activating"
    SUCCEEDED = "succeeded"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"
    RECOVERY_REQUIRED = "recovery_required"


_TRANSITIONS: Mapping[ReleaseState, frozenset[ReleaseState]] = {
    ReleaseState.STAGED: frozenset({ReleaseState.PREPARED, ReleaseState.FAILED}),
    ReleaseState.PREPARED: frozenset({ReleaseState.ACTIVATING, ReleaseState.FAILED}),
    ReleaseState.ACTIVATING: frozenset(
        {
            ReleaseState.SUCCEEDED,
            ReleaseState.ROLLING_BACK,
            ReleaseState.RECOVERY_REQUIRED,
        }
    ),
    ReleaseState.ROLLING_BACK: frozenset(
        {ReleaseState.ROLLED_BACK, ReleaseState.RECOVERY_REQUIRED}
    ),
    ReleaseState.SUCCEEDED: frozenset(),
    ReleaseState.ROLLED_BACK: frozenset(),
    ReleaseState.FAILED: frozenset(),
    ReleaseState.RECOVERY_REQUIRED: frozenset(),
}
_RELEASE_ID_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_-]{0,62}[A-Za-z0-9])?")
_STEP_RE = re.compile(r"[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_MAX_PRIVATE_FILE_BYTES = 1024 * 1024
_FORBIDDEN_STATE_FIELDS = frozenset({"state", "release_id", "original_env", "original_env_bytes"})


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
        raise ReleaseStoreError("release state is not JSON serializable") from exc
    return f"{rendered}\n".encode()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseStoreError("release event contains duplicate fields")
        result[key] = value
    return result


class ReleaseStore:
    """管理单个 release ID 的私有状态、事件与原始环境快照。"""

    def __init__(self, release_root: Path, release_id: str) -> None:
        if _RELEASE_ID_RE.fullmatch(release_id) is None:
            raise ReleaseStoreError("invalid release ID")
        self.release_root = release_root.absolute()
        self.release_id = release_id
        self.release_dir = self.release_root / release_id

    @property
    def _manifest_path(self) -> Path:
        return self.release_dir / "manifest.json"

    @property
    def _state_path(self) -> Path:
        return self.release_dir / "state.json"

    @property
    def _events_path(self) -> Path:
        return self.release_dir / "events.jsonl"

    @property
    def _original_env_path(self) -> Path:
        return self.release_dir / "original.env"

    def _reject_git_checkout_path(self) -> None:
        for ancestor in (self.release_root, *self.release_root.parents):
            try:
                (ancestor / ".git").lstat()
            except FileNotFoundError:
                continue
            raise ReleaseStoreError("release root must be outside the Git checkout")

    @staticmethod
    def _validate_directory(path: Path) -> os.stat_result:
        info = ReleaseStore._validate_owned_directory(path)
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise ReleaseStoreError(f"directory mode must be 0700: {path.name}")
        return info

    @staticmethod
    def _validate_owned_directory(path: Path) -> os.stat_result:
        try:
            info = path.lstat()
        except FileNotFoundError as exc:
            raise ReleaseStoreError(f"required directory is missing: {path.name}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise ReleaseStoreError(f"directory must not be a symlink: {path.name}")
        if not stat.S_ISDIR(info.st_mode):
            raise ReleaseStoreError(f"path is not a directory: {path.name}")
        if info.st_uid != os.geteuid():
            raise ReleaseStoreError(f"directory owner is invalid: {path.name}")
        return info

    @classmethod
    def _ensure_directory(cls, path: Path) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            try:
                parent_info = path.parent.lstat()
            except FileNotFoundError as exc:
                raise ReleaseStoreError("release directory parent is missing") from exc
            if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
                raise ReleaseStoreError("release directory parent is unsafe") from None
            with suppress(FileExistsError):
                path.mkdir(mode=0o700)
            cls._validate_directory(path)
            return
        if stat.S_ISLNK(info.st_mode):
            raise ReleaseStoreError(f"directory must not be a symlink: {path.name}")
        cls._validate_directory(path)

    @staticmethod
    def _validate_regular_file(path: Path, *, expected_mode: int | None) -> os.stat_result:
        try:
            info = path.lstat()
        except FileNotFoundError as exc:
            raise ReleaseStoreError(f"required private file is missing: {path.name}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise ReleaseStoreError(f"private file must not be a symlink: {path.name}")
        if not stat.S_ISREG(info.st_mode):
            raise ReleaseStoreError(f"private path is not a regular file: {path.name}")
        if info.st_uid != os.geteuid():
            raise ReleaseStoreError(f"private file owner is invalid: {path.name}")
        if expected_mode is not None and stat.S_IMODE(info.st_mode) != expected_mode:
            raise ReleaseStoreError(f"private file mode is invalid: {path.name}")
        return info

    @classmethod
    def _read_regular_file(
        cls,
        path: Path,
        *,
        expected_mode: int | None = 0o600,
    ) -> tuple[bytes, os.stat_result]:
        before = cls._validate_regular_file(path, expected_mode=expected_mode)
        descriptor = os.open(path, os.O_RDONLY | _NOFOLLOW)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise ReleaseStoreError(f"private file changed while opening: {path.name}")
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = os.read(descriptor, 64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > _MAX_PRIVATE_FILE_BYTES:
                    raise ReleaseStoreError(f"private file is too large: {path.name}")
                chunks.append(chunk)
            return b"".join(chunks), opened
        finally:
            os.close(descriptor)

    @classmethod
    def _fsync_directory(cls, directory: Path, *, private: bool = True) -> None:
        validator = cls._validate_directory if private else cls._validate_owned_directory
        validator(directory)
        descriptor = os.open(directory, os.O_RDONLY | _DIRECTORY | _NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @classmethod
    def _atomic_write(
        cls,
        path: Path,
        data: bytes,
        *,
        mode: int = 0o600,
        private_parent: bool = True,
    ) -> None:
        parent_validator = (
            cls._validate_directory if private_parent else cls._validate_owned_directory
        )
        parent_validator(path.parent)
        try:
            cls._validate_regular_file(path, expected_mode=mode)
        except ReleaseStoreError as exc:
            try:
                path.lstat()
            except FileNotFoundError:
                pass
            else:
                raise exc

        temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        descriptor = -1
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                mode,
            )
            os.fchmod(descriptor, mode)
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("atomic private write made no progress")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, path)
            cls._validate_regular_file(path, expected_mode=mode)
            cls._fsync_directory(path.parent, private=private_parent)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            with suppress(FileNotFoundError):
                temporary.unlink()

    @staticmethod
    def _cleanup_initialization_directory(directory: Path) -> None:
        for filename in ("manifest.json", "state.json", "events.jsonl"):
            with suppress(FileNotFoundError):
                (directory / filename).unlink()
        with suppress(FileNotFoundError):
            (directory / "artifacts").rmdir()
        with suppress(FileNotFoundError):
            directory.rmdir()

    @classmethod
    def _initialize_directory(cls, directory: Path, manifest_bytes: bytes, release_id: str) -> None:
        cls._validate_directory(directory)
        artifacts = directory / "artifacts"
        artifacts.mkdir(mode=0o700)
        cls._validate_directory(artifacts)
        timestamp = _utc_now()
        cls._atomic_write(directory / "manifest.json", manifest_bytes)
        cls._atomic_write(
            directory / "state.json",
            _json_bytes(
                {
                    "release_id": release_id,
                    "state": ReleaseState.STAGED.value,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                }
            ),
        )
        cls._atomic_write(directory / "events.jsonl", b"")

    def create(self, manifest_bytes: bytes) -> None:
        """原子创建发布目录；相同清单重试幂等，不同清单拒绝。"""

        if type(manifest_bytes) is not bytes or not manifest_bytes:
            raise ReleaseStoreError("manifest bytes must be non-empty bytes")
        if len(manifest_bytes) > _MAX_PRIVATE_FILE_BYTES:
            raise ReleaseStoreError("manifest is too large")
        self._reject_git_checkout_path()
        self._ensure_directory(self.release_root)

        try:
            self.release_dir.lstat()
        except FileNotFoundError:
            temporary = self.release_root / f".{self.release_id}.{uuid.uuid4().hex}.tmpdir"
            temporary.mkdir(mode=0o700)
            try:
                self._initialize_directory(temporary, manifest_bytes, self.release_id)
                os.rename(temporary, self.release_dir)
                self._fsync_directory(self.release_root)
            except BaseException:
                self._cleanup_initialization_directory(temporary)
                raise
            self._validate_directory(self.release_dir)
            return

        self._validate_directory(self.release_dir)
        existing, _ = self._read_regular_file(self._manifest_path)
        if existing != manifest_bytes:
            raise ReleaseStoreError("duplicate release ID has a different manifest")
        self._read_regular_file(self._state_path)
        self._read_regular_file(self._events_path)
        self._validate_directory(self.release_dir / "artifacts")

    def read_state(self) -> dict[str, object]:
        """返回可公开的状态字段，绝不读取或返回 original.env 内容。"""

        raw, _ = self._read_regular_file(self._state_path)
        try:
            value = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ReleaseStoreError("state file is not valid JSON") from exc
        if type(value) is not dict:
            raise ReleaseStoreError("state file must contain an object")
        state = cast(dict[str, object], value)
        for forbidden in ("original_env", "original_env_bytes"):
            state.pop(forbidden, None)
        return state

    def transition(
        self,
        expected: ReleaseState,
        target: ReleaseState,
        **fields: object,
    ) -> None:
        """仅按显式邻接表原子推进状态。"""

        current = self.read_state()
        if current.get("state") != expected.value:
            raise ReleaseStoreError("release state does not match expected state")
        if target not in _TRANSITIONS[expected]:
            raise ReleaseStoreError(f"illegal state transition: {expected.value} -> {target.value}")
        forbidden = set(fields) & _FORBIDDEN_STATE_FIELDS
        if forbidden:
            raise ReleaseStoreError("transition contains forbidden state fields")
        updated = {**current, **fields}
        updated["state"] = target.value
        updated["updated_at"] = _utc_now()
        self._atomic_write(self._state_path, _json_bytes(updated))

    def checkpoint(self, expected: ReleaseState, **fields: object) -> None:
        """在不改变状态的情况下原子持久化中断点或恢复观察。"""

        current = self.read_state()
        if current.get("state") != expected.value:
            raise ReleaseStoreError("release state does not match expected state")
        forbidden = set(fields) & _FORBIDDEN_STATE_FIELDS
        if forbidden:
            raise ReleaseStoreError("checkpoint contains forbidden state fields")
        updated = {**current, **fields, "updated_at": _utc_now()}
        self._atomic_write(self._state_path, _json_bytes(updated))

    def _record_event(self, kind: str, step: str, details: Mapping[str, object]) -> None:
        if _STEP_RE.fullmatch(step) is None:
            raise ReleaseStoreError("invalid release event step")
        event = _json_bytes(
            {
                "kind": kind,
                "step": step,
                "details": dict(details),
                "timestamp": _utc_now(),
            }
        )
        self._validate_regular_file(self._events_path, expected_mode=0o600)
        descriptor = os.open(self._events_path, os.O_WRONLY | os.O_APPEND | _NOFOLLOW)
        try:
            view = memoryview(event)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("release event append made no progress")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def record_intent(self, step: str, details: Mapping[str, object]) -> None:
        """在外部副作用前记录无敏感信息的执行意图。"""

        self._record_event("intent", step, details)

    def record_observation(self, step: str, details: Mapping[str, object]) -> None:
        """在外部副作用成功后记录无敏感信息的观察结果。"""

        self._record_event("observation", step, details)

    def read_events(self) -> list[dict[str, object]]:
        """严格读取持久化事件；畸形、重复或未知字段一律拒绝。"""

        raw, _ = self._read_regular_file(self._events_path)
        events: list[dict[str, object]] = []
        for line in raw.splitlines():
            if not line:
                continue
            try:
                value = json.loads(line, object_pairs_hook=_reject_duplicate_keys)
            except ReleaseStoreError:
                raise
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise ReleaseStoreError("release event is not valid JSON") from exc
            if type(value) is not dict or set(value) != {
                "kind",
                "step",
                "details",
                "timestamp",
            }:
                raise ReleaseStoreError("release event fields are invalid")
            event = cast(dict[str, object], value)
            if (
                event["kind"] not in {"intent", "observation"}
                or type(event["step"]) is not str
                or _STEP_RE.fullmatch(event["step"]) is None
                or type(event["details"]) is not dict
                or type(event["timestamp"]) is not str
            ):
                raise ReleaseStoreError("release event values are invalid")
            events.append(event)
        return events

    def snapshot_env(self, env_path: Path) -> None:
        """私有保存原始 env 字节，重复快照不得覆盖不同原件。"""

        env_bytes, _ = self._read_regular_file(env_path, expected_mode=None)
        try:
            existing, _ = self._read_regular_file(self._original_env_path)
        except ReleaseStoreError:
            try:
                self._original_env_path.lstat()
            except FileNotFoundError:
                self._atomic_write(self._original_env_path, env_bytes)
                return
            raise
        if existing != env_bytes:
            raise ReleaseStoreError("original env snapshot already exists")

    def restore_env(self, env_path: Path) -> None:
        """以保存的原始字节原子恢复 env，并保留目标文件 mode。"""

        original, _ = self._read_regular_file(self._original_env_path)
        _, target_info = self._read_regular_file(env_path, expected_mode=None)
        self._atomic_write(
            env_path,
            original,
            mode=stat.S_IMODE(target_info.st_mode),
            private_parent=False,
        )
