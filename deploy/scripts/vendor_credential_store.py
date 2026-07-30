#!/usr/bin/env python3
"""root 私有的正式厂商凭据版本化 generation 存储。"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

GENERATION_PATTERN = re.compile(r"generation-[0-9a-f]{32}\Z")
STAGING_PATTERN = re.compile(r"\.staging-[0-9a-f]{32}\Z")
MAX_CREDENTIAL_BYTES = 1024
_FILES = frozenset({"vendor_secret_name", "vendor_secret_key", "installed_at"})
_ROTATION_STATE_FILE = "rotation-state.json"
_RESET_FILES = frozenset({"active", "pending", _ROTATION_STATE_FILE})
_ROTATION_STATE_FIELDS = frozenset(
    {"schema_version", "phase", "previous_generation", "new_generation"}
)
_ROTATION_PHASES = frozenset({"prepared", "switched", "rollback_started"})


class CredentialStoreError(RuntimeError):
    """凭据 generation 不可用；错误永不携带值或底层异常。"""


@dataclass(frozen=True, slots=True, repr=False)
class VendorCredentials:
    secret_name: str
    secret_key: str

    def __repr__(self) -> str:
        return "VendorCredentials(<redacted>)"


@dataclass(frozen=True, slots=True)
class CredentialStatus:
    configured: bool
    state: str
    installed_at: datetime | None


@dataclass(frozen=True, slots=True, repr=False)
class RotationTransaction:
    previous_generation: Path
    new_generation: Path
    phase: str


def utc_now() -> datetime:
    return datetime.now(UTC)


def _mode(metadata: os.stat_result) -> int:
    return stat.S_IMODE(metadata.st_mode)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _credential(value: str) -> bytes:
    if type(value) is not str:
        raise CredentialStoreError("厂商凭据格式无效")
    encoded = value.encode("utf-8")
    if (
        not encoded
        or len(encoded) > MAX_CREDENTIAL_BYTES
        or "\x00" in value
        or "\r" in value
        or "\n" in value
    ):
        raise CredentialStoreError("厂商凭据格式无效")
    return encoded


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CredentialStoreError("凭据轮换事务无效")
        result[key] = value
    return result


class VendorCredentialStore:
    """以普通文件 active 指针原子切换成对凭据，最多保留当前与上一代。"""

    def __init__(
        self,
        root: Path,
        *,
        clock: Callable[[], datetime] = utc_now,
        replace: Callable[[Path, Path], None] = os.replace,
    ) -> None:
        self.root = Path(root)
        self.clock = clock
        self.replace = replace

    def _ensure_root(self) -> None:
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            metadata = self.root.lstat()
        except OSError:
            raise CredentialStoreError("凭据目录不可用") from None
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or _mode(metadata) != 0o700
        ):
            raise CredentialStoreError("凭据目录不安全")

    @staticmethod
    def _write_file(path: Path, value: bytes) -> None:
        descriptor = -1
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            offset = 0
            while offset < len(value):
                written = os.write(descriptor, value[offset:])
                if written <= 0:
                    raise OSError("short write")
                offset += written
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        except OSError:
            raise CredentialStoreError("凭据 generation 写入失败") from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    @staticmethod
    def _read_file(path: Path, label: str) -> bytes:
        try:
            metadata = path.lstat()
        except OSError:
            raise CredentialStoreError(f"{label} 不可用") from None
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or _mode(metadata) != 0o600
            or metadata.st_size < 1
            or metadata.st_size > MAX_CREDENTIAL_BYTES * 2
        ):
            raise CredentialStoreError(f"{label} 不安全")
        descriptor = -1
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise CredentialStoreError(f"{label} 读取期间发生变化")
            return os.read(descriptor, metadata.st_size + 1)
        except CredentialStoreError:
            raise
        except OSError:
            raise CredentialStoreError(f"{label} 读取失败") from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _generation_from_pointer(self, pointer_name: str) -> Path:
        self._ensure_root()
        if pointer_name not in {"active", "pending"}:
            raise CredentialStoreError("凭据指针无效")
        raw = self._read_file(self.root / pointer_name, f"{pointer_name} 指针")
        try:
            name = raw.decode("ascii").rstrip("\n")
        except UnicodeError:
            raise CredentialStoreError(f"{pointer_name} 指针无效") from None
        if raw != f"{name}\n".encode("ascii") or GENERATION_PATTERN.fullmatch(name) is None:
            raise CredentialStoreError(f"{pointer_name} 指针无效")
        generation = self.root / name
        return self._validate_generation(generation)

    def _validate_generation(self, generation: Path) -> Path:
        try:
            metadata = generation.lstat()
        except OSError:
            raise CredentialStoreError("活动凭据 generation 不可用") from None
        if (
            generation.parent != self.root
            or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or _mode(metadata) != 0o700
        ):
            raise CredentialStoreError("活动凭据 generation 不安全")
        try:
            inventory = {item.name for item in generation.iterdir()}
        except OSError:
            raise CredentialStoreError("活动凭据 generation 不可用") from None
        if inventory != _FILES:
            raise CredentialStoreError("活动凭据 generation 清单无效")
        return generation

    def active_generation(self) -> Path:
        return self._generation_from_pointer("active")

    def _write_pointer(self, pointer_name: str, generation: Path) -> None:
        if pointer_name not in {"active", "pending"}:
            raise CredentialStoreError("凭据指针无效")
        checked = self._validate_generation(generation)
        temporary = self.root / f".{pointer_name}-{uuid4().hex}"
        try:
            self._write_file(temporary, f"{checked.name}\n".encode("ascii"))
            self.replace(temporary, self.root / pointer_name)
            _fsync_directory(self.root)
        except (OSError, CredentialStoreError):
            with contextlib.suppress(OSError):
                temporary.unlink()
            raise CredentialStoreError("凭据指针切换失败") from None

    def _write_rotation_transaction(self, transaction: RotationTransaction) -> None:
        if transaction.phase not in _ROTATION_PHASES:
            raise CredentialStoreError("凭据轮换事务无效")
        previous = self._validate_generation(transaction.previous_generation)
        new = self._validate_generation(transaction.new_generation)
        if previous == new:
            raise CredentialStoreError("凭据轮换事务无效")
        payload = {
            "schema_version": 1,
            "phase": transaction.phase,
            "previous_generation": previous.name,
            "new_generation": new.name,
        }
        temporary = self.root / f".{_ROTATION_STATE_FILE}-{uuid4().hex}"
        try:
            self._write_file(
                temporary,
                (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode(
                    "ascii"
                ),
            )
            self.replace(temporary, self.root / _ROTATION_STATE_FILE)
            _fsync_directory(self.root)
        except (OSError, CredentialStoreError):
            with contextlib.suppress(OSError):
                temporary.unlink()
            raise CredentialStoreError("凭据轮换事务写入失败") from None

    def read_rotation_transaction(self) -> RotationTransaction | None:
        """读取不含凭据值的崩溃恢复事务；任何漂移均 fail closed。"""

        self._ensure_root()
        path = self.root / _ROTATION_STATE_FILE
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        except OSError:
            raise CredentialStoreError("凭据轮换事务不可用") from None
        try:
            decoded = json.loads(
                self._read_file(path, "凭据轮换事务").decode("ascii"),
                object_pairs_hook=_reject_duplicate_json_keys,
            )
        except CredentialStoreError:
            raise
        except (UnicodeError, json.JSONDecodeError):
            raise CredentialStoreError("凭据轮换事务无效") from None
        if type(decoded) is not dict or set(decoded) != set(_ROTATION_STATE_FIELDS):
            raise CredentialStoreError("凭据轮换事务无效")
        if decoded.get("schema_version") != 1 or type(decoded.get("schema_version")) is not int:
            raise CredentialStoreError("凭据轮换事务无效")
        phase = decoded.get("phase")
        previous_name = decoded.get("previous_generation")
        new_name = decoded.get("new_generation")
        if (
            type(phase) is not str
            or phase not in _ROTATION_PHASES
            or type(previous_name) is not str
            or GENERATION_PATTERN.fullmatch(previous_name) is None
            or type(new_name) is not str
            or GENERATION_PATTERN.fullmatch(new_name) is None
            or previous_name == new_name
        ):
            raise CredentialStoreError("凭据轮换事务无效")
        previous = self._validate_generation(self.root / previous_name)
        new_path = self.root / new_name
        try:
            new = self._validate_generation(new_path)
        except CredentialStoreError:
            try:
                new_path.lstat()
            except FileNotFoundError:
                if phase != "rollback_started":
                    raise CredentialStoreError("凭据轮换事务无效") from None
                new = new_path
            except OSError:
                raise CredentialStoreError("凭据轮换事务无效") from None
            else:
                raise CredentialStoreError("凭据轮换事务无效") from None
        return RotationTransaction(previous, new, phase)

    def _reject_rotation_artifacts(self) -> None:
        for name in ("pending", _ROTATION_STATE_FILE):
            path = self.root / name
            if path.exists() or path.is_symlink():
                raise CredentialStoreError("已有待处理凭据轮换")

    def _write_generation(self, credentials: VendorCredentials) -> tuple[Path, datetime]:
        self._ensure_root()
        secret_name = _credential(credentials.secret_name)
        secret_key = _credential(credentials.secret_key)
        installed_at = self.clock()
        staging = self.root / f".staging-{uuid4().hex}"
        final = self.root / f"generation-{uuid4().hex}"
        try:
            staging.mkdir(mode=0o700)
            self._write_file(staging / "vendor_secret_name", secret_name)
            self._write_file(staging / "vendor_secret_key", secret_key)
            self._write_file(
                staging / "installed_at",
                f"{installed_at.isoformat()}\n".encode("ascii"),
            )
            _fsync_directory(staging)
            self.replace(staging, final)
            _fsync_directory(self.root)
            self._validate_generation(final)
        except (OSError, CredentialStoreError):
            for candidate in (staging, final):
                if candidate.exists() and candidate.is_dir():
                    with contextlib.suppress(OSError):
                        shutil.rmtree(candidate)
            raise CredentialStoreError("厂商凭据安装失败") from None
        return final, installed_at

    def _installed_at(self, generation: Path) -> datetime:
        try:
            return datetime.fromisoformat(
                self._read_file(generation / "installed_at", "安装时间")
                .decode("ascii")
                .rstrip("\n")
            )
        except (CredentialStoreError, UnicodeError, ValueError):
            raise CredentialStoreError("凭据安装时间无效") from None

    def _cleanup_generations(self, keep: set[Path]) -> None:
        for candidate in self.root.iterdir():
            if candidate.is_dir() and candidate not in keep:
                with contextlib.suppress(OSError):
                    shutil.rmtree(candidate)
        _fsync_directory(self.root)

    def read_active(self) -> VendorCredentials:
        generation = self.active_generation()
        try:
            secret_name = self._read_file(
                generation / "vendor_secret_name", "vendor secret name"
            ).decode("utf-8")
            secret_key = self._read_file(
                generation / "vendor_secret_key", "vendor secret key"
            ).decode("utf-8")
        except UnicodeError:
            raise CredentialStoreError("活动厂商凭据编码无效") from None
        _credential(secret_name)
        _credential(secret_key)
        return VendorCredentials(secret_name, secret_key)

    def status(self) -> CredentialStatus:
        try:
            generation = self.active_generation()
        except CredentialStoreError:
            return CredentialStatus(False, "setup_required", None)
        try:
            installed_at = self._installed_at(generation)
        except CredentialStoreError:
            raise
        return CredentialStatus(True, "normal", installed_at)

    def _validate_reset_pointer(
        self,
        path: Path,
        safe_generation_names: set[str],
    ) -> None:
        """只校验固定指针结构，不要求半删 generation 仍可正常读取。"""

        raw = self._read_file(path, "凭据重置指针")
        try:
            name = raw.decode("ascii").rstrip("\n")
        except UnicodeError:
            raise CredentialStoreError("凭据重置指针无效") from None
        if raw != f"{name}\n".encode("ascii") or GENERATION_PATTERN.fullmatch(name) is None:
            raise CredentialStoreError("凭据重置指针无效")
        target = self.root / name
        try:
            target.lstat()
        except FileNotFoundError:
            return
        except OSError:
            raise CredentialStoreError("凭据重置指针无效") from None
        if name not in safe_generation_names:
            raise CredentialStoreError("凭据重置指针无效")

    def _validate_reset_rotation_state(
        self,
        path: Path,
        safe_generation_names: set[str],
    ) -> None:
        """校验事务引用边界，同时允许 rollback candidate 已安全半删。"""

        try:
            decoded = json.loads(
                self._read_file(path, "凭据重置轮换事务").decode("ascii"),
                object_pairs_hook=_reject_duplicate_json_keys,
            )
        except CredentialStoreError:
            raise
        except (UnicodeError, json.JSONDecodeError):
            raise CredentialStoreError("凭据重置轮换事务无效") from None
        if type(decoded) is not dict or set(decoded) != set(_ROTATION_STATE_FIELDS):
            raise CredentialStoreError("凭据重置轮换事务无效")
        schema_version = decoded.get("schema_version")
        phase = decoded.get("phase")
        previous_name = decoded.get("previous_generation")
        new_name = decoded.get("new_generation")
        if (
            type(schema_version) is not int
            or schema_version != 1
            or type(phase) is not str
            or phase not in _ROTATION_PHASES
            or type(previous_name) is not str
            or GENERATION_PATTERN.fullmatch(previous_name) is None
            or type(new_name) is not str
            or GENERATION_PATTERN.fullmatch(new_name) is None
            or previous_name == new_name
            or previous_name not in safe_generation_names
        ):
            raise CredentialStoreError("凭据重置轮换事务无效")
        if new_name in safe_generation_names:
            return
        try:
            (self.root / new_name).lstat()
        except FileNotFoundError:
            if phase == "rollback_started":
                return
        except OSError:
            pass
        raise CredentialStoreError("凭据重置轮换事务无效")

    def _reset_inventory(self) -> tuple[list[Path], list[Path]]:
        """先验证完整清单，避免未知条目与安全条目一起被部分删除。"""

        try:
            entries = list(self.root.iterdir())
        except OSError:
            raise CredentialStoreError("凭据目录清单不可用") from None
        fixed: list[Path] = []
        directories: list[Path] = []
        for entry in entries:
            name = entry.name
            try:
                metadata = entry.lstat()
            except OSError:
                raise CredentialStoreError("凭据目录清单不可用") from None
            if name in _RESET_FILES:
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or stat.S_ISLNK(metadata.st_mode)
                    or _mode(metadata) != 0o600
                ):
                    raise CredentialStoreError("凭据目录清单不安全")
                fixed.append(entry)
                continue
            if (
                GENERATION_PATTERN.fullmatch(name) is None
                and STAGING_PATTERN.fullmatch(name) is None
            ):
                raise CredentialStoreError("凭据目录包含未知条目")
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or _mode(metadata) != 0o700
            ):
                raise CredentialStoreError("凭据目录清单不安全")
            try:
                children = list(entry.iterdir())
            except OSError:
                raise CredentialStoreError("凭据目录清单不可用") from None
            if not {child.name for child in children}.issubset(_FILES):
                raise CredentialStoreError("凭据目录包含未知条目")
            for child in children:
                try:
                    child_metadata = child.lstat()
                except OSError:
                    raise CredentialStoreError("凭据目录清单不可用") from None
                if (
                    not stat.S_ISREG(child_metadata.st_mode)
                    or stat.S_ISLNK(child_metadata.st_mode)
                    or _mode(child_metadata) != 0o600
                ):
                    raise CredentialStoreError("凭据目录清单不安全")
            directories.append(entry)

        fixed_names = {path.name for path in fixed}
        safe_generation_names = {
            path.name
            for path in directories
            if GENERATION_PATTERN.fullmatch(path.name) is not None
        }
        for pointer_name in ("active", "pending"):
            if pointer_name in fixed_names:
                self._validate_reset_pointer(
                    self.root / pointer_name,
                    safe_generation_names,
                )
        if _ROTATION_STATE_FILE in fixed_names:
            self._validate_reset_rotation_state(
                self.root / _ROTATION_STATE_FILE,
                safe_generation_names,
            )
        return fixed, directories

    def reset_required(self) -> bool:
        """判断是否仍有经过完整安全校验的凭据工件需要清理。"""

        self._ensure_root()
        fixed, directories = self._reset_inventory()
        return bool(fixed or directories)

    def reset(self) -> CredentialStatus:
        """幂等清除固定凭据工件，不返回或记录任何凭据元数据。"""

        self._ensure_root()
        fixed, directories = self._reset_inventory()
        fixed_order = {"active": 0, "pending": 1, _ROTATION_STATE_FILE: 2}
        for path in sorted(fixed, key=lambda candidate: fixed_order[candidate.name]):
            try:
                path.unlink()
                _fsync_directory(self.root)
            except OSError:
                raise CredentialStoreError("凭据固定文件清理失败") from None
        for path in directories:
            try:
                shutil.rmtree(path)
                _fsync_directory(self.root)
            except OSError:
                with contextlib.suppress(OSError):
                    _fsync_directory(self.root)
                raise CredentialStoreError("凭据目录清理失败") from None
        return CredentialStatus(False, "setup_required", None)

    def install(self, credentials: VendorCredentials) -> CredentialStatus:
        previous: Path | None
        try:
            previous = self.active_generation()
        except CredentialStoreError:
            previous = None
        self._ensure_root()
        self._reject_rotation_artifacts()
        final, installed_at = self._write_generation(credentials)
        try:
            self._write_pointer("active", final)
        except CredentialStoreError:
            with contextlib.suppress(OSError):
                shutil.rmtree(final)
            raise CredentialStoreError("厂商凭据安装失败") from None
        keep = {final}
        if previous is not None:
            keep.add(previous)
        self._cleanup_generations(keep)
        return CredentialStatus(True, "normal", installed_at)

    def stage(self, credentials: VendorCredentials) -> CredentialStatus:
        """写入待轮换 generation，但保持 active 指针不变。"""

        previous = self.active_generation()
        self._reject_rotation_artifacts()
        final, installed_at = self._write_generation(credentials)
        try:
            self._write_pointer("pending", final)
        except CredentialStoreError:
            with contextlib.suppress(OSError):
                shutil.rmtree(final)
            raise
        self._cleanup_generations({previous, final})
        return CredentialStatus(True, "rotating", installed_at)

    def begin_rotation(self) -> RotationTransaction:
        """先持久化 previous/new，再切换 active；事务保留到运行态验证完成。"""

        if self.read_rotation_transaction() is not None:
            raise CredentialStoreError("已有待处理凭据轮换事务")
        previous = self.active_generation()
        pending = self._generation_from_pointer("pending")
        prepared = RotationTransaction(previous, pending, "prepared")
        self._write_rotation_transaction(prepared)
        self._write_pointer("active", pending)
        switched = RotationTransaction(previous, pending, "switched")
        self._write_rotation_transaction(switched)
        return switched

    def commit_rotation(self, transaction: RotationTransaction) -> CredentialStatus:
        """仅在新运行态探针成功后清理事务和 pending。"""

        current = self.read_rotation_transaction()
        if current is None or current != transaction or current.phase != "switched":
            raise CredentialStoreError("凭据轮换事务状态冲突")
        if self.active_generation() != current.new_generation:
            raise CredentialStoreError("凭据轮换事务状态冲突")
        pending_pointer = self.root / "pending"
        has_pending = pending_pointer.exists() or pending_pointer.is_symlink()
        if has_pending and (
            self._generation_from_pointer("pending") != current.new_generation
        ):
            raise CredentialStoreError("凭据轮换事务状态冲突")
        if has_pending:
            try:
                pending_pointer.unlink()
                _fsync_directory(self.root)
            except OSError:
                raise CredentialStoreError("pending 指针清理失败") from None
        try:
            (self.root / _ROTATION_STATE_FILE).unlink()
            _fsync_directory(self.root)
        except OSError:
            raise CredentialStoreError("凭据轮换事务清理失败") from None
        self._cleanup_generations(
            {current.previous_generation, current.new_generation}
        )
        return CredentialStatus(
            True,
            "normal",
            self._installed_at(current.new_generation),
        )

    def rollback_to_previous(
        self,
        transaction: RotationTransaction,
    ) -> RotationTransaction:
        """持久化 rollback_started 后回切旧 generation，可安全重复。"""

        current = self.read_rotation_transaction()
        if (
            current is None
            or current.previous_generation != transaction.previous_generation
            or current.new_generation != transaction.new_generation
        ):
            raise CredentialStoreError("凭据轮换事务状态冲突")
        active = self.active_generation()
        if active not in {current.previous_generation, current.new_generation}:
            raise CredentialStoreError("凭据轮换事务状态冲突")
        rolling_back = RotationTransaction(
            current.previous_generation,
            current.new_generation,
            "rollback_started",
        )
        if current.phase != "rollback_started":
            self._write_rotation_transaction(rolling_back)
        if active != current.previous_generation:
            self._write_pointer("active", current.previous_generation)
        return rolling_back

    def complete_rollback(self, transaction: RotationTransaction) -> CredentialStatus:
        """旧运行态重新构建并探针成功后，才删除候选与事务。"""

        current = self.read_rotation_transaction()
        if (
            current is None
            or current.phase != "rollback_started"
            or current != transaction
            or self.active_generation() != current.previous_generation
        ):
            raise CredentialStoreError("凭据轮换事务状态冲突")
        pending_pointer = self.root / "pending"
        has_pending = pending_pointer.exists() or pending_pointer.is_symlink()
        if has_pending and (
            self._generation_from_pointer("pending") != current.new_generation
        ):
            raise CredentialStoreError("凭据轮换事务状态冲突")
        if has_pending:
            try:
                pending_pointer.unlink()
                _fsync_directory(self.root)
            except OSError:
                raise CredentialStoreError("pending 指针清理失败") from None
        if current.new_generation.exists():
            try:
                shutil.rmtree(current.new_generation)
                _fsync_directory(self.root)
            except OSError:
                raise CredentialStoreError("pending 凭据清理失败") from None
        try:
            (self.root / _ROTATION_STATE_FILE).unlink()
            _fsync_directory(self.root)
        except OSError:
            raise CredentialStoreError("凭据轮换事务清理失败") from None
        self._cleanup_generations({current.previous_generation})
        return CredentialStatus(
            True,
            "normal",
            self._installed_at(current.previous_generation),
        )

    def activate_pending(self) -> tuple[CredentialStatus, Path]:
        """在已暂停的宿主编排中原子切换 pending，并返回回退 generation。"""

        if self.read_rotation_transaction() is not None:
            raise CredentialStoreError("已有待处理凭据轮换事务")
        previous = self.active_generation()
        pending = self._generation_from_pointer("pending")
        self._write_pointer("active", pending)
        try:
            (self.root / "pending").unlink()
            _fsync_directory(self.root)
        except OSError:
            with contextlib.suppress(CredentialStoreError):
                self._write_pointer("active", previous)
            raise CredentialStoreError("pending 指针清理失败") from None
        self._cleanup_generations({previous, pending})
        return CredentialStatus(True, "normal", self._installed_at(pending)), previous

    def restore(self, generation: Path) -> CredentialStatus:
        """失败时只允许回切同一私有根内的已验证 generation。"""

        previous = self._validate_generation(Path(generation))
        current = self.active_generation()
        self._write_pointer("active", previous)
        self._cleanup_generations({previous, current})
        return CredentialStatus(True, "normal", self._installed_at(previous))

    def discard_pending(self) -> None:
        """在切换前失败时删除未激活的 pending generation。"""

        if self.read_rotation_transaction() is not None:
            raise CredentialStoreError("凭据轮换事务必须先恢复")
        pending = self._generation_from_pointer("pending")
        try:
            (self.root / "pending").unlink()
            shutil.rmtree(pending)
            _fsync_directory(self.root)
        except OSError:
            raise CredentialStoreError("pending 凭据清理失败") from None

    def recover_pending(self) -> str:
        """在 lifecycle lock 内收敛崩溃残留，不误删已切换的 active。"""

        if self.read_rotation_transaction() is not None:
            raise CredentialStoreError("凭据轮换事务必须先恢复")
        active = self.active_generation()
        pending_pointer = self.root / "pending"
        if not pending_pointer.exists() and not pending_pointer.is_symlink():
            return "clean"
        pending = self._generation_from_pointer("pending")
        try:
            pending_pointer.unlink()
            if pending != active:
                shutil.rmtree(pending)
            _fsync_directory(self.root)
        except OSError:
            raise CredentialStoreError("pending 凭据恢复失败") from None
        return "committed" if pending == active else "discarded"
