#!/usr/bin/env python3
"""一次性安装锁定的测试主机资产与密文 checkpoint 前置条件。"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from test_secure_access_contract import (
    CLOUDFLARED_PATH,
    CLOUDFLARED_SHA256,
    CLOUDFLARED_VERSION,
    HOST_ASSET_NAMES,
    HOST_ASSET_ROOT,
    HOST_CONTROL_SOURCE_ASSETS,
    HOST_MANIFEST_PATH,
    SERVICE_NAME,
    TEST_HOST_MARKER_PATH,
    serialize_host_manifest,
    serialize_test_host_marker,
)

DEFAULT_ROOT = Path("/opt/sms-platform")
INSTALLED_UNIT = Path("/etc/systemd/system") / SERVICE_NAME
BOOTSTRAP_ROOT = Path("/var/lib/sms-platform/test-secure-access-bootstrap")
BOOTSTRAP_BINARY = BOOTSTRAP_ROOT / "cloudflared-linux-amd64"
HOST_ASSET_TRAVERSE_ROOT = Path("/usr/local")
TEST_UPDATE_BACKUP_CONFIG = Path("/etc/sms-platform/test-update-backup.json")
TEST_UPDATE_BACKUP_KEY = Path("/etc/sms-platform/test-update-backup-key")
TEST_UPDATE_BACKUP_ROOT = Path("/var/lib/sms-platform/test-backups")
TEST_UPDATE_LIFECYCLE_LOCK = Path("/run/sms-platform/secrets.lifecycle.lock")
_VERSION_RE = re.compile(rf"cloudflared version {re.escape(CLOUDFLARED_VERSION)}(?:[ \n]|\Z)")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_CHECKPOINT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_NONCE_RE = re.compile(r"[0-9a-f]{24}")
_CHECKPOINT_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "checkpoint_id",
        "created_at",
        "database",
        "cipher",
        "aad",
        "nonce",
        "ciphertext_file",
        "restore_readable",
        "complete",
    }
)


class SecureAccessInstallerError(RuntimeError):
    """锁定主机资产无法安全安装。"""


class CommandRunner(Protocol):
    def run(
        self,
        *argv: str,
        check: bool,
    ) -> subprocess.CompletedProcess[str]: ...


class CommitVerifier(Protocol):
    def verify(
        self,
        *,
        commit: str,
        path: str,
        sha256: str,
        git_mode: str,
    ) -> None: ...


class SubprocessRunner:
    def run(
        self,
        *argv: str,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(argv),
            check=check,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )


class GitCommitVerifier:
    """把 staged 资产逐字节绑定到服务器已获取的目标 Git object。"""

    def __init__(self, repository: Path = DEFAULT_ROOT) -> None:
        self.repository = repository

    def verify(
        self,
        *,
        commit: str,
        path: str,
        sha256: str,
        git_mode: str,
    ) -> None:
        if (
            _COMMIT_RE.fullmatch(commit) is None
            or not path
            or path.startswith("/")
            or ".." in Path(path).parts
            or re.fullmatch(r"[A-Za-z0-9_./-]+", path) is None
            or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
            or git_mode not in {"100644", "100755"}
        ):
            raise SecureAccessInstallerError("secure access source commit is invalid")
        try:
            object_type = subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(self.repository),
                    "cat-file",
                    "-t",
                    commit,
                ],
                check=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
            )
            tree = subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(self.repository),
                    "ls-tree",
                    "-z",
                    commit,
                    "--",
                    path,
                ],
                check=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
            )
            result = subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(self.repository),
                    "cat-file",
                    "blob",
                    f"{commit}:{path}",
                ],
                check=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
            )
        except OSError as exc:
            raise SecureAccessInstallerError("secure access source commit is unavailable") from exc
        entries = tree.stdout.split(b"\0")
        metadata, separator, observed_path = (
            entries[0].partition(b"\t")
            if tree.returncode == 0 and len(entries) == 2 and entries[1] == b""
            else (b"", b"", b"")
        )
        fields = metadata.split()
        if (
            object_type.returncode != 0
            or object_type.stdout != b"commit\n"
            or tree.returncode != 0
            or separator != b"\t"
            or observed_path != path.encode("ascii")
            or len(fields) != 3
            or fields[0] != git_mode.encode("ascii")
            or fields[1] != b"blob"
            or re.fullmatch(rb"[0-9a-f]{40,64}", fields[2]) is None
            or result.returncode != 0
            or hashlib.sha256(result.stdout).hexdigest() != sha256
        ):
            raise SecureAccessInstallerError(
                "secure access source commit does not match staged assets"
            )


@dataclass(frozen=True, slots=True)
class InstallResult:
    status: str = "installed"
    service: str = SERVICE_NAME
    version: str = CLOUDFLARED_VERSION

    def as_dict(self) -> dict[str, str]:
        return {
            "status": self.status,
            "service": self.service,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class _PrivateFileSnapshot:
    payload: bytes
    device: int
    inode: int


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _prepare_directory(path: Path, *, expected_uid: int) -> None:
    try:
        created = not path.exists()
        path.mkdir(mode=0o755, parents=True, exist_ok=True)
        metadata = path.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise OSError("unsafe directory")
        if created:
            os.chown(path, expected_uid, 0 if expected_uid == 0 else os.getegid())
            os.chmod(path, 0o755)
    except OSError as exc:
        raise SecureAccessInstallerError("secure access install directory is unsafe") from exc


def _prepare_dynamic_user_asset_tree(
    traverse_root: Path,
    asset_root: Path,
    *,
    expected_uid: int,
) -> None:
    """只增加目录的 other traverse 位，不授予目录列举权限。"""

    try:
        relative = asset_root.relative_to(traverse_root)
    except ValueError as exc:
        raise SecureAccessInstallerError("secure access host asset path is invalid") from exc

    paths = [traverse_root]
    current = traverse_root
    for part in relative.parts:
        current /= part
        paths.append(current)

    for path in paths:
        _prepare_directory(path, expected_uid=expected_uid)
        descriptor = -1
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            metadata = os.fstat(descriptor)
            mode = stat.S_IMODE(metadata.st_mode)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != expected_uid
                or mode & 0o022
            ):
                raise OSError("unsafe dynamic user asset directory")
            if not mode & 0o001:
                os.fchmod(descriptor, mode | 0o001)
                mode = stat.S_IMODE(os.fstat(descriptor).st_mode)
            if not mode & 0o001 or mode & 0o022:
                raise OSError("dynamic user asset directory is inaccessible")
        except OSError as exc:
            raise SecureAccessInstallerError("secure access install directory is unsafe") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)


def _open_safe_source(
    path: Path,
    *,
    expected_uid: int,
    expected_mode: int,
) -> int:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or stat.S_IMODE(metadata.st_mode) != expected_mode
        ):
            raise OSError("unsafe source")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
            or opened.st_uid != expected_uid
            or stat.S_IMODE(opened.st_mode) != expected_mode
        ):
            os.close(descriptor)
            raise OSError("source changed")
        return descriptor
    except OSError as exc:
        raise SecureAccessInstallerError("secure access install source is unsafe") from exc


def _validate_source_root(path: Path, *, expected_uid: int) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SecureAccessInstallerError(
            "secure access install source root is unavailable"
        ) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != expected_uid
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise SecureAccessInstallerError("secure access install source root is unsafe")


def _validate_bootstrap_parent(path: Path, *, expected_uid: int) -> None:
    if (
        path.parent != BOOTSTRAP_ROOT
        or not path.name.startswith("source-")
        or _COMMIT_RE.fullmatch(path.name.removeprefix("source-")) is None
    ):
        return
    try:
        metadata = BOOTSTRAP_ROOT.lstat()
    except OSError as exc:
        raise SecureAccessInstallerError("secure access bootstrap parent is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != expected_uid
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise SecureAccessInstallerError("secure access bootstrap parent is unsafe")


def _temporary_path(destination: Path) -> Path:
    if destination.suffix == ".service":
        return destination.parent / f".{destination.stem}.{uuid.uuid4().hex}.service"
    return destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"


def _copy_source_to_temporary(
    source: Path,
    destination: Path,
    *,
    mode: int,
    source_mode: int,
    expected_uid: int,
) -> tuple[Path, str, bytes]:
    source_fd = _open_safe_source(
        source,
        expected_uid=expected_uid,
        expected_mode=source_mode,
    )
    temporary = _temporary_path(destination)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    target_fd = -1
    digest = hashlib.sha256()
    prefix = bytearray()
    try:
        target_fd = os.open(temporary, flags, mode)
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            if len(prefix) < 64:
                prefix.extend(chunk[: 64 - len(prefix)])
            view = memoryview(chunk)
            while view:
                written = os.write(target_fd, view)
                view = view[written:]
        os.fchown(target_fd, expected_uid, 0 if expected_uid == 0 else os.getegid())
        os.fchmod(target_fd, mode)
        os.fsync(target_fd)
        return temporary, digest.hexdigest(), bytes(prefix)
    except OSError as exc:
        with suppress(FileNotFoundError):
            temporary.unlink()
        raise SecureAccessInstallerError("secure access install staging failed") from exc
    finally:
        os.close(source_fd)
        if target_fd >= 0:
            os.close(target_fd)


def _write_payload_to_temporary(
    payload: bytes,
    destination: Path,
    *,
    mode: int,
    expected_uid: int,
) -> Path:
    temporary = _temporary_path(destination)
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fchown(descriptor, expected_uid, 0 if expected_uid == 0 else os.getegid())
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        return temporary
    except OSError as exc:
        with suppress(FileNotFoundError):
            temporary.unlink()
        raise SecureAccessInstallerError("secure access install staging failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _checkpoint_error() -> SecureAccessInstallerError:
    return SecureAccessInstallerError("secure access checkpoint prerequisites are unsafe")


def _prepare_checkpoint_directory(path: Path, *, expected_uid: int) -> None:
    _prepare_directory(path.parent, expected_uid=expected_uid)
    created = False
    try:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            path.mkdir(mode=0o700)
            created = True
            metadata = path.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise OSError("unsafe checkpoint directory")
        if created:
            os.chown(path, expected_uid, 0 if expected_uid == 0 else os.getegid())
            os.chmod(path, 0o700)
    except OSError as exc:
        raise _checkpoint_error() from exc


def _open_checkpoint_lifecycle_lock(path: Path, *, expected_uid: int) -> int:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        opened = os.fstat(descriptor)
        metadata = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
            or opened.st_uid != expected_uid
            or metadata.st_uid != expected_uid
            or stat.S_IMODE(opened.st_mode) != 0o600
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or opened.st_nlink != 1
            or metadata.st_nlink != 1
        ):
            raise OSError("unsafe lifecycle lock")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = os.fstat(descriptor)
        current = path.lstat()
        if (
            (locked.st_dev, locked.st_ino) != (opened.st_dev, opened.st_ino)
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
            or locked.st_nlink != 1
            or current.st_nlink != 1
        ):
            raise OSError("lifecycle lock changed")
        return descriptor
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise _checkpoint_error() from exc


def _private_file_state(
    path: Path,
    *,
    expected_uid: int,
) -> _PrivateFileSnapshot | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _checkpoint_error() from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != expected_uid
        or stat.S_IMODE(metadata.st_mode) not in {0o400, 0o600}
        or metadata.st_size > 4096
        or metadata.st_nlink != 1
    ):
        raise _checkpoint_error()
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (
            (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != expected_uid
            or stat.S_IMODE(opened.st_mode) not in {0o400, 0o600}
            or opened.st_size > 4096
            or opened.st_nlink != 1
        ):
            raise OSError("checkpoint file changed")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 4096)
            if not chunk:
                break
            chunks.append(chunk)
        return _PrivateFileSnapshot(
            b"".join(chunks),
            opened.st_dev,
            opened.st_ino,
        )
    except OSError as exc:
        raise _checkpoint_error() from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _reject_checkpoint_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise _checkpoint_error()
        document[key] = value
    return document


def _validate_amd64_elf(prefix: bytes) -> None:
    if (
        len(prefix) < 20
        or prefix[:4] != b"\x7fELF"
        or prefix[4] != 2
        or prefix[5] != 1
        or int.from_bytes(prefix[18:20], "little") != 62
    ):
        raise SecureAccessInstallerError("secure access install binary architecture is invalid")


class SecureAccessInstaller:
    """验真并安装固定主机资产，保全密文 checkpoint 权威密钥。"""

    def __init__(
        self,
        *,
        root: Path = DEFAULT_ROOT,
        source_binary: Path,
        binary_path: Path = CLOUDFLARED_PATH,
        installed_unit: Path = INSTALLED_UNIT,
        host_asset_root: Path = HOST_ASSET_ROOT,
        host_asset_traverse_root: Path | None = None,
        manifest_path: Path | None = None,
        marker_path: Path = TEST_HOST_MARKER_PATH,
        backup_config_path: Path = TEST_UPDATE_BACKUP_CONFIG,
        backup_key_path: Path = TEST_UPDATE_BACKUP_KEY,
        backup_output_root: Path = TEST_UPDATE_BACKUP_ROOT,
        lifecycle_lock_path: Path = TEST_UPDATE_LIFECYCLE_LOCK,
        source_commit: str,
        commit_verifier: CommitVerifier | None = None,
        expected_uid: int = 0,
        expected_sha256: str = CLOUDFLARED_SHA256,
        runner: CommandRunner | None = None,
    ) -> None:
        self.root = root
        self.source_binary = source_binary
        self.binary_path = binary_path
        self.installed_unit = installed_unit
        self.host_asset_root = host_asset_root
        self.host_asset_traverse_root = host_asset_traverse_root or (
            HOST_ASSET_TRAVERSE_ROOT
            if host_asset_root == HOST_ASSET_ROOT
            else host_asset_root.parent
        )
        self.manifest_path = manifest_path or (
            HOST_MANIFEST_PATH
            if host_asset_root == HOST_ASSET_ROOT
            else host_asset_root / "manifest.json"
        )
        self.marker_path = marker_path
        self.backup_config_path = backup_config_path
        self.backup_key_path = backup_key_path
        self.backup_output_root = backup_output_root
        self.lifecycle_lock_path = lifecycle_lock_path
        if type(source_commit) is not str or _COMMIT_RE.fullmatch(source_commit) is None:
            raise SecureAccessInstallerError("secure access source commit is invalid")
        self.source_commit = source_commit
        self.commit_verifier = commit_verifier or GitCommitVerifier()
        self.expected_uid = expected_uid
        self.expected_sha256 = expected_sha256
        self.runner = runner or SubprocessRunner()

    @property
    def source_unit(self) -> Path:
        return self.root / "deploy/systemd" / SERVICE_NAME

    @property
    def source_host_assets(self) -> dict[str, Path]:
        return {name: self.root / path for name, path in HOST_CONTROL_SOURCE_ASSETS}

    def _validate_version(self, staged_binary: Path) -> None:
        try:
            result = self.runner.run(
                str(staged_binary),
                "--version",
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SecureAccessInstallerError("secure access install version check failed") from exc
        if _VERSION_RE.match(result.stdout) is None:
            raise SecureAccessInstallerError("secure access install version is invalid")

    def _validate_python_runtime(
        self,
        *,
        verified_assets: dict[str, Path],
    ) -> None:
        script = (
            "import sys;"
            "sys.version_info < (3, 11) and sys.exit(1);"
            "sys.path.insert(0, sys.argv[1]);"
            "from cryptography.hazmat.primitives.ciphers.aead import AESGCM;"
            "import test_secure_access_manager;"
            "import cloudflare_tunnel_manager;"
            "import public_baseline_activation;"
            "import public_baseline_manager;"
            "import public_cutover_bootstrap;"
            "import test_update_manager;"
            "import verify_public_snapshot_cutover;"
            "import verify_web_transport"
        )
        import_root = Path(
            tempfile.mkdtemp(
                prefix=".python-import-",
                dir=self.host_asset_root,
            )
        )
        try:
            import_root.chmod(0o700)
            for name, source in verified_assets.items():
                if not name.endswith(".py"):
                    continue
                os.link(
                    source,
                    import_root / name,
                    follow_symlinks=False,
                )
            self.runner.run(
                "/usr/bin/python3",
                "-I",
                "-c",
                script,
                str(import_root),
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SecureAccessInstallerError(
                "secure access host Python dependency is unavailable"
            ) from exc
        finally:
            shutil.rmtree(import_root, ignore_errors=True)

    def _checkpoint_config_payload(self) -> bytes:
        return (
            json.dumps(
                {
                    "schema_version": 1,
                    "output_root": str(self.backup_output_root),
                    "key_file": str(self.backup_key_path),
                    "database": "sms",
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")

    def _validate_checkpoint_config(self, payload: bytes) -> None:
        try:
            document = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_reject_checkpoint_duplicates,
            )
        except SecureAccessInstallerError:
            raise
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise _checkpoint_error() from exc
        expected = {
            "schema_version": 1,
            "output_root": str(self.backup_output_root),
            "key_file": str(self.backup_key_path),
            "database": "sms",
        }
        if (
            type(document) is not dict
            or type(document.get("schema_version")) is not int
            or document != expected
        ):
            raise _checkpoint_error()

    def _checkpoint_root_is_empty(self) -> bool:
        try:
            with os.scandir(self.backup_output_root) as entries:
                return next(entries, None) is None
        except OSError as exc:
            raise _checkpoint_error() from exc

    def _validate_completed_checkpoints(self) -> None:
        try:
            entries = list(os.scandir(self.backup_output_root))
        except OSError as exc:
            raise _checkpoint_error() from exc
        for entry in entries:
            checkpoint = Path(entry.path)
            try:
                metadata = checkpoint.lstat()
            except OSError as exc:
                raise _checkpoint_error() from exc
            if (
                _CHECKPOINT_ID_RE.fullmatch(entry.name) is None
                or not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != self.expected_uid
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                raise _checkpoint_error()
            try:
                child_names = {child.name for child in os.scandir(checkpoint)}
            except OSError as exc:
                raise _checkpoint_error() from exc
            if child_names != {"database.dump.aesgcm", "manifest.json"}:
                raise _checkpoint_error()

            ciphertext = checkpoint / "database.dump.aesgcm"
            try:
                ciphertext_metadata = ciphertext.lstat()
            except OSError as exc:
                raise _checkpoint_error() from exc
            if (
                not stat.S_ISREG(ciphertext_metadata.st_mode)
                or stat.S_ISLNK(ciphertext_metadata.st_mode)
                or ciphertext_metadata.st_uid != self.expected_uid
                or stat.S_IMODE(ciphertext_metadata.st_mode) not in {0o400, 0o600}
                or ciphertext_metadata.st_nlink != 1
                or ciphertext_metadata.st_size < 16
            ):
                raise _checkpoint_error()

            manifest_snapshot = _private_file_state(
                checkpoint / "manifest.json",
                expected_uid=self.expected_uid,
            )
            if manifest_snapshot is None:
                raise _checkpoint_error()
            try:
                manifest = json.loads(
                    manifest_snapshot.payload.decode("utf-8"),
                    object_pairs_hook=_reject_checkpoint_duplicates,
                )
            except SecureAccessInstallerError:
                raise
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise _checkpoint_error() from exc
            if type(manifest) is not dict or set(manifest) != _CHECKPOINT_MANIFEST_FIELDS:
                raise _checkpoint_error()
            created_at = manifest.get("created_at")
            if type(created_at) is not str:
                raise _checkpoint_error()
            try:
                parsed_created_at = datetime.fromisoformat(created_at)
            except ValueError as exc:
                raise _checkpoint_error() from exc
            if (
                type(manifest.get("schema_version")) is not int
                or manifest["schema_version"] != 1
                or manifest.get("checkpoint_id") != entry.name
                or parsed_created_at.tzinfo is None
                or parsed_created_at.utcoffset() is None
                or manifest.get("database") != "sms"
                or manifest.get("cipher") != "AES-256-GCM"
                or manifest.get("aad") != "sms-test-update-v1"
                or type(manifest.get("nonce")) is not str
                or _NONCE_RE.fullmatch(manifest["nonce"]) is None
                or manifest.get("ciphertext_file") != "database.dump.aesgcm"
                or manifest.get("restore_readable") is not True
                or manifest.get("complete") is not True
            ):
                raise _checkpoint_error()

    def _require_unchanged_private_file(
        self,
        path: Path,
        expected: _PrivateFileSnapshot,
    ) -> _PrivateFileSnapshot:
        current = _private_file_state(path, expected_uid=self.expected_uid)
        if current != expected:
            raise _checkpoint_error()
        return current

    def _durably_revalidate_checkpoint_authority(
        self,
        config_snapshot: _PrivateFileSnapshot,
        key_snapshot: _PrivateFileSnapshot,
    ) -> None:
        """补齐已提交 config/key 的目录耐久性后再次绑定身份。"""

        try:
            _fsync_directory(self.backup_config_path.parent)
            if self.backup_key_path.parent != self.backup_config_path.parent:
                _fsync_directory(self.backup_key_path.parent)
        except OSError as exc:
            raise _checkpoint_error() from exc
        self._require_unchanged_private_file(
            self.backup_config_path,
            config_snapshot,
        )
        self._require_unchanged_private_file(
            self.backup_key_path,
            key_snapshot,
        )

    def _ensure_checkpoint_prerequisites(self) -> None:
        if (
            not self.backup_config_path.is_absolute()
            or not self.backup_key_path.is_absolute()
            or not self.backup_output_root.is_absolute()
            or not self.lifecycle_lock_path.is_absolute()
            or len(
                {
                    self.backup_config_path,
                    self.backup_key_path,
                    self.backup_output_root,
                    self.lifecycle_lock_path,
                }
            )
            != 4
            or any(
                ".." in path.parts
                for path in (
                    self.backup_config_path,
                    self.backup_key_path,
                    self.backup_output_root,
                    self.lifecycle_lock_path,
                )
            )
        ):
            raise _checkpoint_error()
        _prepare_checkpoint_directory(
            self.lifecycle_lock_path.parent,
            expected_uid=self.expected_uid,
        )
        lock_descriptor = _open_checkpoint_lifecycle_lock(
            self.lifecycle_lock_path,
            expected_uid=self.expected_uid,
        )
        try:
            self._ensure_checkpoint_prerequisites_locked()
        finally:
            os.close(lock_descriptor)

    def _ensure_checkpoint_prerequisites_locked(self) -> None:
        _prepare_directory(
            self.backup_config_path.parent,
            expected_uid=self.expected_uid,
        )
        if self.backup_key_path.parent != self.backup_config_path.parent:
            _prepare_directory(
                self.backup_key_path.parent,
                expected_uid=self.expected_uid,
            )
        _prepare_checkpoint_directory(
            self.backup_output_root,
            expected_uid=self.expected_uid,
        )
        config_snapshot = _private_file_state(
            self.backup_config_path,
            expected_uid=self.expected_uid,
        )
        key_snapshot = _private_file_state(
            self.backup_key_path,
            expected_uid=self.expected_uid,
        )
        if config_snapshot is not None:
            self._validate_checkpoint_config(config_snapshot.payload)
        if key_snapshot is not None and len(key_snapshot.payload) != 32:
            raise _checkpoint_error()
        if config_snapshot is not None and key_snapshot is not None:
            self._validate_completed_checkpoints()
            self._require_unchanged_private_file(
                self.backup_config_path,
                config_snapshot,
            )
            self._require_unchanged_private_file(
                self.backup_key_path,
                key_snapshot,
            )
            self._durably_revalidate_checkpoint_authority(
                config_snapshot,
                key_snapshot,
            )
            return
        if config_snapshot is not None:
            raise _checkpoint_error()
        if not self._checkpoint_root_is_empty():
            raise _checkpoint_error()

        config_temp: Path | None = None
        key_temp: Path | None = None
        try:
            config_payload = self._checkpoint_config_payload()
            config_temp = _write_payload_to_temporary(
                config_payload,
                self.backup_config_path,
                mode=0o600,
                expected_uid=self.expected_uid,
            )
            staged_config = _private_file_state(
                config_temp,
                expected_uid=self.expected_uid,
            )
            if staged_config is None or staged_config.payload != config_payload:
                raise _checkpoint_error()
            if key_snapshot is None:
                key_payload = os.urandom(32)
                key_temp = _write_payload_to_temporary(
                    key_payload,
                    self.backup_key_path,
                    mode=0o600,
                    expected_uid=self.expected_uid,
                )
                staged_key = _private_file_state(
                    key_temp,
                    expected_uid=self.expected_uid,
                )
                if staged_key is None or staged_key.payload != key_payload:
                    raise _checkpoint_error()
                os.link(
                    key_temp,
                    self.backup_key_path,
                    follow_symlinks=False,
                )
                key_temp.unlink()
                key_temp = None
                _fsync_directory(self.backup_key_path.parent)
                key_snapshot = _private_file_state(
                    self.backup_key_path,
                    expected_uid=self.expected_uid,
                )
                if key_snapshot != staged_key:
                    raise _checkpoint_error()
            self._require_unchanged_private_file(
                self.backup_key_path,
                key_snapshot,
            )
            os.link(
                config_temp,
                self.backup_config_path,
                follow_symlinks=False,
            )
            config_temp.unlink()
            config_temp = None
            _fsync_directory(self.backup_config_path.parent)
            committed_config = _private_file_state(
                self.backup_config_path,
                expected_uid=self.expected_uid,
            )
            if committed_config != staged_config:
                raise _checkpoint_error()
            self._validate_checkpoint_config(committed_config.payload)
            self._require_unchanged_private_file(
                self.backup_key_path,
                key_snapshot,
            )
        except SecureAccessInstallerError:
            raise
        except OSError as exc:
            raise _checkpoint_error() from exc
        finally:
            for temporary in (config_temp, key_temp):
                if temporary is not None:
                    with suppress(FileNotFoundError):
                        temporary.unlink()

    def _unit_preexists_safely(self) -> bool:
        try:
            metadata = self.installed_unit.lstat()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise SecureAccessInstallerError(
                "secure access install service state is unavailable"
            ) from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != self.expected_uid
            or stat.S_IMODE(metadata.st_mode) != 0o644
        ):
            raise SecureAccessInstallerError("secure access install service unit is unsafe")
        return True

    def _require_inactive_static_unit(self, *, installed: bool) -> None:
        try:
            active = self.runner.run(
                "systemctl",
                "is-active",
                SERVICE_NAME,
                check=False,
            ).stdout.strip()
            enabled = self.runner.run(
                "systemctl",
                "is-enabled",
                SERVICE_NAME,
                check=False,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise SecureAccessInstallerError(
                "secure access install service state is unavailable"
            ) from exc
        allowed_enabled = {"static"} if installed else {"disabled", "static", "not-found"}
        if active not in {"inactive", "unknown", ""} or enabled not in allowed_enabled:
            raise SecureAccessInstallerError(
                "secure access install service is not inactive and static"
            )

    def run(self) -> InstallResult:
        _validate_source_root(self.root, expected_uid=self.expected_uid)
        _validate_bootstrap_parent(self.root, expected_uid=self.expected_uid)
        _prepare_directory(self.binary_path.parent, expected_uid=self.expected_uid)
        _prepare_directory(self.installed_unit.parent, expected_uid=self.expected_uid)
        _prepare_directory(self.host_asset_root, expected_uid=self.expected_uid)
        _prepare_directory(self.marker_path.parent, expected_uid=self.expected_uid)
        binary_temp: Path | None = None
        unit_temp: Path | None = None
        manifest_temp: Path | None = None
        marker_temp: Path | None = None
        host_temps: dict[str, Path] = {}
        try:
            binary_temp, digest, prefix = _copy_source_to_temporary(
                self.source_binary,
                self.binary_path,
                mode=0o755,
                source_mode=0o644,
                expected_uid=self.expected_uid,
            )
            if digest != self.expected_sha256:
                raise SecureAccessInstallerError("secure access install checksum is invalid")
            _validate_amd64_elf(prefix)
            self._validate_version(binary_temp)

            digests = {"cloudflared": digest}
            for name, source in self.source_host_assets.items():
                destination = self.host_asset_root / name
                mode = 0o755 if name == "sms-compose-bootstrap" else 0o644
                staged, asset_digest, _prefix = _copy_source_to_temporary(
                    source,
                    destination,
                    mode=mode,
                    source_mode=mode,
                    expected_uid=self.expected_uid,
                )
                if name.endswith(".py"):
                    try:
                        compile(staged.read_bytes(), name, "exec")
                    except (OSError, SyntaxError) as exc:
                        raise SecureAccessInstallerError(
                            "secure access install Python asset is invalid"
                        ) from exc
                elif name == "sms-compose-bootstrap":
                    self.runner.run("/bin/bash", "-n", str(staged), check=True)
                host_temps[name] = staged
                digests[name] = asset_digest
                self.commit_verifier.verify(
                    commit=self.source_commit,
                    path=str(source.relative_to(self.root)),
                    sha256=asset_digest,
                    git_mode=("100755" if name == "sms-compose-bootstrap" else "100644"),
                )
            if set(digests) != set(HOST_ASSET_NAMES):
                raise SecureAccessInstallerError("secure access install host asset set is invalid")
            self._validate_python_runtime(verified_assets=host_temps)
            manifest_temp = _write_payload_to_temporary(
                serialize_host_manifest(
                    digests,
                    source_commit=self.source_commit,
                ).encode("utf-8"),
                self.manifest_path,
                mode=0o644,
                expected_uid=self.expected_uid,
            )

            unit_temp, _unit_digest, _unit_prefix = _copy_source_to_temporary(
                self.source_unit,
                self.installed_unit,
                mode=0o644,
                source_mode=0o644,
                expected_uid=self.expected_uid,
            )
            self.runner.run(
                "systemd-analyze",
                "verify",
                str(unit_temp),
                check=True,
            )
            self._ensure_checkpoint_prerequisites()
            _prepare_dynamic_user_asset_tree(
                self.host_asset_traverse_root,
                self.host_asset_root,
                expected_uid=self.expected_uid,
            )
            unit_preexists = self._unit_preexists_safely()
            if unit_preexists:
                self.runner.run(
                    "systemctl",
                    "disable",
                    "--now",
                    SERVICE_NAME,
                    check=True,
                )
                self.runner.run(
                    "systemctl",
                    "reset-failed",
                    SERVICE_NAME,
                    check=False,
                )
            self._require_inactive_static_unit(installed=False)
            marker_temp = _write_payload_to_temporary(
                serialize_test_host_marker().encode("utf-8"),
                self.marker_path,
                mode=0o600,
                expected_uid=self.expected_uid,
            )
            os.replace(binary_temp, self.binary_path)
            binary_temp = None
            _fsync_directory(self.binary_path.parent)
            for name in sorted(host_temps):
                os.replace(host_temps[name], self.host_asset_root / name)
            host_temps.clear()
            _fsync_directory(self.host_asset_root)
            os.replace(unit_temp, self.installed_unit)
            unit_temp = None
            _fsync_directory(self.installed_unit.parent)
            self.runner.run("systemctl", "daemon-reload", check=True)
            self._require_inactive_static_unit(installed=True)
            os.replace(marker_temp, self.marker_path)
            marker_temp = None
            _fsync_directory(self.marker_path.parent)
            os.replace(manifest_temp, self.manifest_path)
            manifest_temp = None
            _fsync_directory(self.host_asset_root)
            return InstallResult()
        except SecureAccessInstallerError:
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            raise SecureAccessInstallerError("secure access install failed") from exc
        finally:
            for temporary in (
                binary_temp,
                unit_temp,
                manifest_temp,
                marker_temp,
                *host_temps.values(),
            ):
                if temporary is not None:
                    with suppress(FileNotFoundError):
                        temporary.unlink()


def _safe_absolute_path(value: str) -> Path:
    path = Path(value)
    if (
        not path.is_absolute()
        or ".." in path.parts
        or "\0" in value
        or "\n" in value
        or "\r" in value
    ):
        raise SecureAccessInstallerError("secure access install invocation is blocked")
    return path


def parse_install_invocation(
    argv: Sequence[str],
    *,
    euid: int,
) -> tuple[Path, Path, str]:
    """只接受 root 提供的固定 binary 和可选只读源码根。"""

    if euid != 0 or len(argv) not in {4, 6} or argv[0] != "--cloudflared-file":
        raise SecureAccessInstallerError("secure access install invocation is blocked")
    source = _safe_absolute_path(argv[1])
    root = DEFAULT_ROOT
    commit_index = 2
    if len(argv) == 6:
        if argv[2] != "--source-root":
            raise SecureAccessInstallerError("secure access install invocation is blocked")
        root = _safe_absolute_path(argv[3])
        commit_index = 4
    if (
        argv[commit_index] != "--source-commit"
        or _COMMIT_RE.fullmatch(argv[commit_index + 1]) is None
    ):
        raise SecureAccessInstallerError("secure access install invocation is blocked")
    expected_root = BOOTSTRAP_ROOT / f"source-{argv[commit_index + 1]}"
    if source != BOOTSTRAP_BINARY or root != expected_root:
        raise SecureAccessInstallerError("secure access install invocation is blocked")
    return source, root, argv[commit_index + 1]


def parse_install_source(argv: Sequence[str], *, euid: int) -> Path:
    """兼容既有测试和默认安装，只返回 binary 路径。"""

    if euid != 0 or len(argv) != 2 or argv[0] != "--cloudflared-file":
        raise SecureAccessInstallerError("secure access install invocation is blocked")
    return _safe_absolute_path(argv[1])


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        source, root, source_commit = parse_install_invocation(
            arguments,
            euid=os.geteuid(),
        )
        result = SecureAccessInstaller(
            root=root,
            source_binary=source,
            source_commit=source_commit,
        ).run()
        print(json.dumps(result.as_dict(), separators=(",", ":"), sort_keys=True))
        return 0
    except Exception:
        print("secure access install blocked", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
