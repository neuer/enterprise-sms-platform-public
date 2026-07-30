#!/usr/bin/env python3
"""为 Compose 生成按服务隔离的最小权限运行密钥副本。"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import os
import re
import secrets
import shutil
import stat
import sys
import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path

CANONICAL_SECRET_NAMES = frozenset(
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
VENDOR_SECRET_NAMES = frozenset({"vendor_secret_name", "vendor_secret_key"})
VENDOR_REVOCATION_TOMBSTONE = b"!"
BACKEND_SECRET_NAMES = CANONICAL_SECRET_NAMES - {"db_owner_password"}
POSTGRES_SECRET_NAMES = frozenset(
    {
        "db_owner_password",
        "db_auth_password",
        "db_accept_password",
        "db_send_password",
        "db_callback_password",
        "db_export_password",
        "db_scheduler_password",
        "db_metrics_password",
    }
)
MIGRATE_SECRET_NAMES = frozenset({"db_owner_password"})
REDIS_SECRET_NAMES = frozenset(
    {
        "redis_broker_password",
        "redis_auth_password",
        "redis_control_password",
    }
)
BACKEND_UID = 10001
POSTGRES_UID = 70
REDIS_UID = 999
REDIS_GID = 1000
MAX_SECRET_BYTES = 1024 * 1024

_SERVICE_SECRET_NAMES = {
    "backend": BACKEND_SECRET_NAMES,
    "postgres": POSTGRES_SECRET_NAMES,
    "migrate": MIGRATE_SECRET_NAMES,
    "redis": REDIS_SECRET_NAMES,
}
_DEV_ONLY_NAMES = frozenset({"dev-apikeys.txt"})
_GENERATION_NAME_PATTERN = re.compile(r"generation-[0-9a-f]{32}\Z")


class RuntimeSecretsError(RuntimeError):
    """运行密钥准备未满足安全策略。"""


def _mode(metadata: os.stat_result) -> int:
    return stat.S_IMODE(metadata.st_mode)


def _lstat(path: Path, label: str) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise RuntimeSecretsError(f"{label} is unavailable") from exc


def _read_source_file(path: Path, logical_name: str) -> bytes:
    metadata = _lstat(path, f"secret {logical_name}")
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise RuntimeSecretsError(f"secret {logical_name} must be a regular file")
    if _mode(metadata) != 0o600:
        raise RuntimeSecretsError(f"secret {logical_name} mode must be 0600")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeSecretsError(f"secret {logical_name} cannot be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise RuntimeSecretsError(f"secret {logical_name} must remain a regular file")
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise RuntimeSecretsError(f"secret {logical_name} changed during validation")
        if _mode(opened) != 0o600:
            raise RuntimeSecretsError(f"secret {logical_name} mode must remain 0600")

        value = bytearray()
        while len(value) <= MAX_SECRET_BYTES:
            chunk = os.read(descriptor, min(65536, MAX_SECRET_BYTES + 1 - len(value)))
            if not chunk:
                break
            value.extend(chunk)
        if not value:
            raise RuntimeSecretsError(f"secret {logical_name} violates non-empty policy")
        if len(value) > MAX_SECRET_BYTES:
            raise RuntimeSecretsError(f"secret {logical_name} violates bounded-size policy")
        return bytes(value)
    except OSError as exc:
        raise RuntimeSecretsError(f"secret {logical_name} could not be validated") from exc
    finally:
        os.close(descriptor)


def _validate_source_inventory(source_dir: Path, mode: str) -> None:
    metadata = _lstat(source_dir, "source directory")
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise RuntimeSecretsError("source directory must be a non-symlink directory")
    if _mode(metadata) != 0o700:
        raise RuntimeSecretsError("source directory mode must be 0700")

    try:
        names = {path.name for path in source_dir.iterdir()}
    except OSError as exc:
        raise RuntimeSecretsError("source directory inventory cannot be read") from exc
    allowed = CANONICAL_SECRET_NAMES if mode == "production" else (
        CANONICAL_SECRET_NAMES | _DEV_ONLY_NAMES
    )
    if not CANONICAL_SECRET_NAMES.issubset(names) or not names.issubset(allowed):
        raise RuntimeSecretsError("source directory inventory violates policy")


def _validate_source(source_dir: Path, mode: str) -> dict[str, bytes]:
    _validate_source_inventory(source_dir, mode)

    return {
        name: _read_source_file(source_dir / name, name)
        for name in sorted(CANONICAL_SECRET_NAMES)
    }


def _ensure_directory(path: Path, expected_mode: int, label: str) -> None:
    try:
        path.mkdir(mode=expected_mode, parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeSecretsError(f"{label} cannot be created") from exc
    metadata = _lstat(path, label)
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise RuntimeSecretsError(f"{label} must be a non-symlink directory")
    if _mode(metadata) != expected_mode:
        raise RuntimeSecretsError(f"{label} mode violates policy")


def _ensure_runtime_root(runtime_root: Path) -> None:
    if runtime_root == Path(runtime_root.anchor):
        raise RuntimeSecretsError("runtime root cannot be a filesystem root")
    _ensure_directory(runtime_root, 0o700, "runtime root")


@contextlib.contextmanager
def _runtime_lock(runtime_root: Path) -> Iterator[None]:
    lock_path = runtime_root / "prepare.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise RuntimeSecretsError("runtime lock cannot be opened safely") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeSecretsError("runtime lock must be a regular file")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeSecretsError("runtime lock is already held") from exc
        yield
    except OSError as exc:
        raise RuntimeSecretsError("runtime lock operation failed") from exc
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, value: bytes) -> None:
    offset = 0
    while offset < len(value):
        written = os.write(descriptor, value[offset:])
        if written <= 0:
            raise OSError("short write")
        offset += written


def _write_copy(path: Path, value: bytes, owner: tuple[int, int], logical_name: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o400)
    try:
        _write_all(descriptor, value)
        os.fchown(descriptor, owner[0], owner[1])
        os.fchmod(descriptor, 0o400)
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeSecretsError(f"runtime copy {logical_name} is not a regular file")
        if _mode(metadata) != 0o400:
            raise RuntimeSecretsError(f"runtime copy {logical_name} mode violates policy")
        if (metadata.st_uid, metadata.st_gid) != owner:
            raise RuntimeSecretsError(f"runtime copy {logical_name} owner violates policy")
        if metadata.st_size == 0:
            raise RuntimeSecretsError(f"runtime copy {logical_name} violates non-empty policy")
    finally:
        os.close(descriptor)


def _require_runtime_root(runtime_root: Path) -> None:
    metadata = _lstat(runtime_root, "runtime root")
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise RuntimeSecretsError("runtime root must be a non-symlink directory")
    if _mode(metadata) != 0o700:
        raise RuntimeSecretsError("runtime root mode violates policy")


def _validated_generation_path(runtime_root: Path, target: str) -> Path:
    if Path(target).is_absolute():
        raise RuntimeSecretsError("generation target must be relative")
    name = target.removeprefix("generations/")
    if target != f"generations/{name}" or not _GENERATION_NAME_PATTERN.fullmatch(name):
        raise RuntimeSecretsError("generation target is not canonical")

    generations_root = runtime_root / "generations"
    generations_metadata = _lstat(generations_root, "generation root")
    if not stat.S_ISDIR(generations_metadata.st_mode) or stat.S_ISLNK(
        generations_metadata.st_mode
    ):
        raise RuntimeSecretsError("generation root must be a non-symlink directory")
    if _mode(generations_metadata) != 0o700:
        raise RuntimeSecretsError("generation root mode violates policy")

    generation = generations_root / name
    if generation.parent != generations_root:
        raise RuntimeSecretsError("generation target escapes the generation root")
    generation_metadata = _lstat(generation, "current generation")
    if not stat.S_ISDIR(generation_metadata.st_mode) or stat.S_ISLNK(generation_metadata.st_mode):
        raise RuntimeSecretsError("current generation must be a non-symlink directory")
    return generation


def _relative_current_target(runtime_root: Path) -> str | None:
    current = runtime_root / "current"
    try:
        metadata = current.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeSecretsError("current runtime pointer cannot be inspected") from exc
    if not stat.S_ISLNK(metadata.st_mode):
        raise RuntimeSecretsError("current runtime pointer must be a symbolic link")
    target = os.readlink(current)
    _validated_generation_path(runtime_root, target)
    return target


def _remove_generation(path: Path, generations_root: Path) -> None:
    if path.parent != generations_root:
        raise RuntimeSecretsError("generation cleanup escaped the runtime root")
    metadata = _lstat(path, "runtime generation")
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise RuntimeSecretsError("runtime generation must be a non-symlink directory")
    try:
        shutil.rmtree(path)
    except OSError as exc:
        raise RuntimeSecretsError("runtime generation cleanup failed") from exc


def _rollback_current(runtime_root: Path, old_target: str | None) -> None:
    current = runtime_root / "current"
    if old_target is None:
        with contextlib.suppress(FileNotFoundError):
            current.unlink()
        _fsync_directory(runtime_root)
        return
    rollback = runtime_root / f".current-rollback-{secrets.token_hex(8)}"
    os.symlink(old_target, rollback)
    try:
        os.replace(rollback, current)
        _fsync_directory(runtime_root)
    finally:
        with contextlib.suppress(FileNotFoundError):
            rollback.unlink()


def _verify_service_inventory(generation: Path) -> None:
    for service, expected_names in _SERVICE_SECRET_NAMES.items():
        service_dir = generation / service
        actual_names = {path.name for path in service_dir.iterdir()}
        if actual_names != expected_names:
            raise RuntimeSecretsError(f"runtime {service} inventory violates policy")


def _materialize_generation(
    *,
    runtime_root: Path,
    values: dict[str, bytes],
    owners: dict[str, tuple[int, int]],
    rollback_after_switch: bool,
) -> None:
    generations_root = runtime_root / "generations"
    _ensure_directory(generations_root, 0o700, "generation root")
    old_target = _relative_current_target(runtime_root)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=generations_root))
    staging.chmod(0o700)
    final = generations_root / f"generation-{secrets.token_hex(16)}"
    installed = False
    switched = False
    pointer_temp: Path | None = None
    try:
        for service, names in _SERVICE_SECRET_NAMES.items():
            service_dir = staging / service
            service_dir.mkdir(mode=0o700)
            for name in sorted(names):
                _write_copy(service_dir / name, values[name], owners[service], name)
            _fsync_directory(service_dir)
        _verify_service_inventory(staging)
        _fsync_directory(staging)

        os.replace(staging, final)
        installed = True
        _fsync_directory(generations_root)

        pointer_temp = runtime_root / f".current-{secrets.token_hex(8)}"
        os.symlink(f"generations/{final.name}", pointer_temp)
        os.replace(pointer_temp, runtime_root / "current")
        pointer_temp = None
        switched = True
        _fsync_directory(runtime_root)
    except (OSError, RuntimeSecretsError) as exc:
        if switched and rollback_after_switch:
            with contextlib.suppress(OSError, RuntimeSecretsError):
                _rollback_current(runtime_root, old_target)
        if pointer_temp is not None:
            with contextlib.suppress(FileNotFoundError):
                pointer_temp.unlink()
        if not switched or rollback_after_switch:
            candidate = final if installed else staging
            if candidate.exists():
                with contextlib.suppress(RuntimeSecretsError):
                    _remove_generation(candidate, generations_root)
        if isinstance(exc, RuntimeSecretsError):
            raise
        raise RuntimeSecretsError("runtime secret materialization failed") from exc


def _read_runtime_copy(path: Path, logical_name: str) -> bytes:
    metadata = _lstat(path, f"runtime copy {logical_name}")
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or _mode(metadata) != 0o400
    ):
        raise RuntimeSecretsError(f"runtime copy {logical_name} violates revoked policy")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeSecretsError(
            f"runtime copy {logical_name} violates revoked policy"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
            or _mode(opened) != 0o400
        ):
            raise RuntimeSecretsError(
                f"runtime copy {logical_name} violates revoked policy"
            )
        value = os.read(descriptor, len(VENDOR_REVOCATION_TOMBSTONE) + 1)
    except OSError as exc:
        raise RuntimeSecretsError(
            f"runtime copy {logical_name} violates revoked policy"
        ) from exc
    finally:
        os.close(descriptor)
    return value


def prepare(
    *,
    source_dir: Path,
    runtime_root: Path,
    mode: str,
    backend_owner: tuple[int, int],
    postgres_owner: tuple[int, int],
    migrate_owner: tuple[int, int],
    redis_owner: tuple[int, int] | None = None,
    require_root: bool = True,
    vendor_credential_root: Path | None = None,
) -> None:
    """校验核心凭据与 Redis ACL secrets 并原子生成服务级只读副本。"""

    if mode not in {"production", "development"}:
        raise RuntimeSecretsError("secret mode violates policy")
    if require_root and os.geteuid() != 0:
        raise RuntimeSecretsError("production runtime secret preparation requires root")
    owners = {
        "backend": backend_owner,
        "postgres": postgres_owner,
        "migrate": migrate_owner,
        "redis": redis_owner or backend_owner,
    }
    if any(uid < 0 or gid < 0 for uid, gid in owners.values()):
        raise RuntimeSecretsError("runtime owner violates policy")

    runtime_root = Path(runtime_root)
    _ensure_runtime_root(runtime_root)
    with _runtime_lock(runtime_root):
        values = _validate_source(Path(source_dir), mode)
        if vendor_credential_root is not None:
            from vendor_credential_store import (  # noqa: PLC0415
                CredentialStoreError,
                VendorCredentialStore,
            )

            try:
                vendor = VendorCredentialStore(vendor_credential_root).read_active()
            except CredentialStoreError:
                raise RuntimeSecretsError(
                    "active vendor credential generation is unavailable"
                ) from None
            values["vendor_secret_name"] = vendor.secret_name.encode("utf-8")
            values["vendor_secret_key"] = vendor.secret_key.encode("utf-8")
        _materialize_generation(
            runtime_root=runtime_root,
            values=values,
            owners=owners,
            rollback_after_switch=True,
        )


def revoke_vendor(
    *,
    source_dir: Path,
    runtime_root: Path,
    mode: str,
    backend_owner: tuple[int, int],
    postgres_owner: tuple[int, int],
    migrate_owner: tuple[int, int],
    redis_owner: tuple[int, int] | None = None,
    require_root: bool = True,
) -> None:
    """生成固定撤销凭据副本，且从不读取 canonical 厂商凭据内容。"""

    if mode != "development":
        raise RuntimeSecretsError("vendor revocation requires development mode")
    if require_root and os.geteuid() != 0:
        raise RuntimeSecretsError("runtime vendor revocation requires root")
    owners = {
        "backend": backend_owner,
        "postgres": postgres_owner,
        "migrate": migrate_owner,
        "redis": redis_owner or backend_owner,
    }
    if any(uid < 0 or gid < 0 for uid, gid in owners.values()):
        raise RuntimeSecretsError("runtime owner violates policy")

    runtime_root = Path(runtime_root)
    _ensure_runtime_root(runtime_root)
    with _runtime_lock(runtime_root):
        source_dir = Path(source_dir)
        _validate_source_inventory(source_dir, mode)
        values = {
            name: _read_source_file(source_dir / name, name)
            for name in sorted(CANONICAL_SECRET_NAMES - VENDOR_SECRET_NAMES)
        }
        for name in VENDOR_SECRET_NAMES:
            values[name] = VENDOR_REVOCATION_TOMBSTONE
        _materialize_generation(
            runtime_root=runtime_root,
            values=values,
            owners=owners,
            rollback_after_switch=False,
        )


def verify_vendor_revoked(*, runtime_root: Path) -> None:
    """验证 current generation 只携带固定厂商撤销 tombstone。"""

    runtime_root = Path(runtime_root)
    _require_runtime_root(runtime_root)
    with _runtime_lock(runtime_root):
        target = _relative_current_target(runtime_root)
        if target is None:
            raise RuntimeSecretsError("runtime vendor credentials are not revoked")
        generation = _validated_generation_path(runtime_root, target)
        generation_metadata = _lstat(generation, "current generation")
        if _mode(generation_metadata) != 0o700:
            raise RuntimeSecretsError("runtime vendor credentials are not revoked")
        _verify_service_inventory(generation)
        for service in _SERVICE_SECRET_NAMES:
            service_dir = generation / service
            service_metadata = _lstat(service_dir, f"runtime {service}")
            if (
                not stat.S_ISDIR(service_metadata.st_mode)
                or stat.S_ISLNK(service_metadata.st_mode)
                or _mode(service_metadata) != 0o700
            ):
                raise RuntimeSecretsError("runtime vendor credentials are not revoked")
        for name in VENDOR_SECRET_NAMES:
            if _read_runtime_copy(generation / "backend" / name, name) != (
                VENDOR_REVOCATION_TOMBSTONE
            ):
                raise RuntimeSecretsError("runtime vendor credentials are not revoked")


def verify_only_current_generation(*, runtime_root: Path) -> None:
    """验证 runtime generations 只保留 current，且不输出目标元数据。"""

    runtime_root = Path(runtime_root)
    _require_runtime_root(runtime_root)
    with _runtime_lock(runtime_root):
        target = _relative_current_target(runtime_root)
        if target is None:
            raise RuntimeSecretsError("runtime stale generation cleanup is incomplete")
        current = _validated_generation_path(runtime_root, target)
        generations_root = runtime_root / "generations"
        try:
            entries = list(generations_root.iterdir())
        except OSError as exc:
            raise RuntimeSecretsError(
                "runtime stale generation cleanup is incomplete"
            ) from exc
        if entries != [current]:
            raise RuntimeSecretsError("runtime stale generation cleanup is incomplete")


def current_target(*, runtime_root: Path) -> str:
    """持锁读取严格校验后的当前 generation 元数据。"""

    runtime_root = Path(runtime_root)
    _require_runtime_root(runtime_root)
    with _runtime_lock(runtime_root):
        target = _relative_current_target(runtime_root)
        if target is None:
            raise RuntimeSecretsError("current generation is not available")
        return target


def activate(*, runtime_root: Path, target: str) -> None:
    """持锁原子激活已存在且位于 generations 根下的 generation。"""

    runtime_root = Path(runtime_root)
    _require_runtime_root(runtime_root)
    with _runtime_lock(runtime_root):
        if _relative_current_target(runtime_root) is None:
            raise RuntimeSecretsError("current generation is not available")
        _validated_generation_path(runtime_root, target)
        pointer_temp = runtime_root / f".current-activate-{secrets.token_hex(8)}"
        try:
            os.symlink(target, pointer_temp)
            os.replace(pointer_temp, runtime_root / "current")
            _fsync_directory(runtime_root)
        except OSError as exc:
            raise RuntimeSecretsError("generation activation failed") from exc
        finally:
            with contextlib.suppress(FileNotFoundError):
                pointer_temp.unlink()


def cleanup(*, runtime_root: Path, remove_all: bool) -> None:
    """在持锁状态下清理旧 generation 或全部运行副本。"""

    runtime_root = Path(runtime_root)
    try:
        runtime_root.lstat()
    except FileNotFoundError:
        return
    _ensure_runtime_root(runtime_root)
    with _runtime_lock(runtime_root):
        generations_root = runtime_root / "generations"
        try:
            generations_metadata = generations_root.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(generations_metadata.st_mode) or stat.S_ISLNK(
            generations_metadata.st_mode
        ):
            raise RuntimeSecretsError("generation root must be a non-symlink directory")
        if _mode(generations_metadata) != 0o700:
            raise RuntimeSecretsError("generation root mode violates policy")

        current_target_value = _relative_current_target(runtime_root)
        current_path = (
            runtime_root / current_target_value if current_target_value is not None else None
        )
        if remove_all and current_target_value is not None:
            try:
                (runtime_root / "current").unlink()
                _fsync_directory(runtime_root)
            except OSError as exc:
                raise RuntimeSecretsError("current runtime pointer cleanup failed") from exc
        entries = list(generations_root.iterdir())
        for entry in entries:
            if not remove_all and entry == current_path:
                continue
            _remove_generation(entry, generations_root)
        _fsync_directory(generations_root)
        _fsync_directory(runtime_root)


def _owners_for_cli(
    mode: str,
) -> tuple[
    tuple[int, int],
    tuple[int, int],
    tuple[int, int],
    tuple[int, int],
    bool,
]:
    if mode == "development" and sys.platform == "darwin":
        portable = (os.geteuid(), os.getegid())
        return portable, portable, portable, portable, False
    return (BACKEND_UID, BACKEND_UID), (POSTGRES_UID, POSTGRES_UID), (
        BACKEND_UID,
        BACKEND_UID,
    ), (REDIS_UID, REDIS_GID), True


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare least-privilege runtime secrets")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--source-dir", type=Path, required=True)
    prepare_parser.add_argument("--runtime-root", type=Path, required=True)
    prepare_parser.add_argument("--vendor-credential-root", type=Path)
    prepare_parser.add_argument(
        "--mode", choices=("production", "development"), default="production"
    )

    revoke_parser = subparsers.add_parser("revoke-vendor")
    revoke_parser.add_argument("--source-dir", type=Path, required=True)
    revoke_parser.add_argument("--runtime-root", type=Path, required=True)
    revoke_parser.add_argument("--mode", choices=("development",), required=True)

    verify_revoked_parser = subparsers.add_parser("verify-vendor-revoked")
    verify_revoked_parser.add_argument("--runtime-root", type=Path, required=True)

    verify_current_parser = subparsers.add_parser("verify-only-current")
    verify_current_parser.add_argument("--runtime-root", type=Path, required=True)

    cleanup_parser = subparsers.add_parser("cleanup")
    cleanup_parser.add_argument("--runtime-root", type=Path, required=True)
    cleanup_group = cleanup_parser.add_mutually_exclusive_group(required=True)
    cleanup_group.add_argument("--stale", action="store_true")
    cleanup_group.add_argument("--all", action="store_true", dest="remove_all")

    current_parser = subparsers.add_parser("current-target")
    current_parser.add_argument("--runtime-root", type=Path, required=True)

    activate_parser = subparsers.add_parser("activate")
    activate_parser.add_argument("--runtime-root", type=Path, required=True)
    activate_parser.add_argument("--target", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """执行安全预处理 CLI，输出仅包含阶段状态。"""

    args = _build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            (
                backend_owner,
                postgres_owner,
                migrate_owner,
                redis_owner,
                require_root,
            ) = _owners_for_cli(args.mode)
            prepare(
                source_dir=args.source_dir,
                runtime_root=args.runtime_root,
                mode=args.mode,
                backend_owner=backend_owner,
                postgres_owner=postgres_owner,
                migrate_owner=migrate_owner,
                redis_owner=redis_owner,
                require_root=require_root,
                vendor_credential_root=args.vendor_credential_root,
            )
            print("runtime secrets prepared")
        elif args.command == "revoke-vendor":
            (
                backend_owner,
                postgres_owner,
                migrate_owner,
                redis_owner,
                require_root,
            ) = _owners_for_cli(args.mode)
            revoke_vendor(
                source_dir=args.source_dir,
                runtime_root=args.runtime_root,
                mode=args.mode,
                backend_owner=backend_owner,
                postgres_owner=postgres_owner,
                migrate_owner=migrate_owner,
                redis_owner=redis_owner,
                require_root=require_root,
            )
            print("runtime vendor credentials revoked")
        elif args.command == "verify-vendor-revoked":
            verify_vendor_revoked(runtime_root=args.runtime_root)
            print("runtime vendor credentials are revoked")
        elif args.command == "verify-only-current":
            verify_only_current_generation(runtime_root=args.runtime_root)
            print("runtime stale generations are absent")
        elif args.command == "cleanup":
            cleanup(runtime_root=args.runtime_root, remove_all=args.remove_all)
            print("runtime secrets cleaned")
        elif args.command == "current-target":
            print(current_target(runtime_root=args.runtime_root))
        else:
            activate(runtime_root=args.runtime_root, target=args.target)
            print("runtime secrets activated")
    except RuntimeSecretsError as exc:
        print(f"runtime secrets error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
