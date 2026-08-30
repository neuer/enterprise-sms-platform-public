#!/usr/bin/env python3
"""Build and attest immutable production control snapshots from approved Git objects.

The module deliberately never copies bytes from the live checkout.  A trusted
caller supplies one exact commit after its own release/ref checks, and every
tracked file is then read from that commit's Git object database as the fixed
production operator.  Activation and status enforce the V1 dual-channel
binding: the root-owned snapshot, exact Git commit/tree, and clean operator
checkout HEAD must continue to identify the same source revision.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, cast

DEFAULT_REPOSITORY = Path("/opt/sms-platform")
DEFAULT_CONTROL_ROOT = Path(
    "/usr/local/libexec/sms-platform/production-control"
)
DEFAULT_LIFECYCLE_LOCK = Path("/run/sms-platform/secrets.lifecycle.lock")
DEFAULT_APPROVAL_ROOT = Path(
    "/etc/sms-platform/production-control-approved"
)
PRODUCTION_OPERATOR_UID = 1000
PRODUCTION_OPERATOR_GID = 1000
GIT = "/usr/bin/git"
SNAPSHOT_MANIFEST_NAME = ".sms-platform-production-control-manifest.json"

MAX_TRACKED_FILES = 10_000
MAX_SINGLE_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_SNAPSHOT_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_PATH_BYTES = 512
MAX_TREE_LISTING_BYTES = 16 * 1024 * 1024

_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_OBJECT_RE = re.compile(r"[0-9a-f]{40,64}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SAFE_PATH_RE = re.compile(r"[A-Za-z0-9._+@/-]+")
_SNAPSHOT_FIELDS = frozenset({"schema_version", "commit", "tree", "files"})
_SNAPSHOT_FILE_FIELDS = frozenset({"path", "mode", "size", "sha256"})
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class ProductionControlSnapshotError(RuntimeError):
    """The production root-control snapshot contract cannot be proven."""


class CommandRunner(Protocol):
    """Small injectable boundary for fixed, shell-free commands."""

    def run(
        self,
        argv: Sequence[str],
        *,
        pass_fds: Sequence[int] = (),
        user: int | None = None,
        group: int | None = None,
        extra_groups: Sequence[int] | None = None,
    ) -> subprocess.CompletedProcess[bytes]: ...


class SubprocessRunner:
    def run(
        self,
        argv: Sequence[str],
        *,
        pass_fds: Sequence[int] = (),
        user: int | None = None,
        group: int | None = None,
        extra_groups: Sequence[int] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        environment = {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "HOME": "/nonexistent",
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/bin:/bin",
        }
        return subprocess.run(
            list(argv),
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=environment,
            pass_fds=tuple(pass_fds),
            user=user,
            group=group,
            extra_groups=None if extra_groups is None else tuple(extra_groups),
        )


@dataclass(frozen=True, slots=True)
class SnapshotPaths:
    repository: Path = DEFAULT_REPOSITORY
    control_root: Path = DEFAULT_CONTROL_ROOT
    lifecycle_lock: Path = DEFAULT_LIFECYCLE_LOCK
    approval_root: Path = DEFAULT_APPROVAL_ROOT

    @property
    def versions_root(self) -> Path:
        return self.control_root / "versions"

    @property
    def current(self) -> Path:
        return self.control_root / "current"


@dataclass(frozen=True, slots=True)
class GitEntry:
    path: str
    mode: str
    object_id: str
    size: int
    sha256: str | None = None

    @property
    def filesystem_mode(self) -> int:
        return 0o555 if self.mode == "100755" else 0o444


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProductionControlSnapshotError("JSON contains a duplicate key")
        result[key] = value
    return result


def _parse_json_object(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except ProductionControlSnapshotError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProductionControlSnapshotError(f"{label} is not strict JSON") from exc
    if type(value) is not dict:
        raise ProductionControlSnapshotError(f"{label} must be an object")
    return cast(dict[str, Any], value)


def _require_exact_fields(
    value: dict[str, Any], expected: frozenset[str], *, label: str
) -> None:
    if set(value) != expected:
        raise ProductionControlSnapshotError(f"{label} fields are invalid")


def _safe_relative_path(value: object) -> str:
    if type(value) is not str:
        raise ProductionControlSnapshotError("snapshot path is invalid")
    try:
        encoded = value.encode("ascii")
    except UnicodeError as exc:
        raise ProductionControlSnapshotError("snapshot path is invalid") from exc
    pure = PurePosixPath(value)
    if (
        not value
        or len(encoded) > MAX_PATH_BYTES
        or value.startswith("/")
        or value.endswith("/")
        or "//" in value
        or _SAFE_PATH_RE.fullmatch(value) is None
        or any(part in {"", ".", "..", ".git"} for part in pure.parts)
        or any(len(part.encode("ascii")) > 255 for part in pure.parts)
        or value == SNAPSHOT_MANIFEST_NAME
    ):
        raise ProductionControlSnapshotError("snapshot path is invalid")
    return value


def _read_secure_file(
    path: Path,
    *,
    expected_uid: int,
    expected_gid: int,
    expected_mode: int,
    maximum: int,
    minimum: int = 1,
    label: str,
) -> bytes:
    """Read one non-link inode once and reject metadata changes around the read."""

    try:
        before = path.lstat()
        descriptor = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except OSError as exc:
        raise ProductionControlSnapshotError(f"{label} is unavailable") from exc
    try:
        opened = os.fstat(descriptor)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_uid,
            before.st_gid,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        opened_identity = (
            opened.st_dev,
            opened.st_ino,
            opened.st_mode,
            opened.st_uid,
            opened.st_gid,
            opened.st_nlink,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        )
        if before_identity != opened_identity:
            raise ProductionControlSnapshotError(f"{label} changed while opening")
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != expected_uid
            or opened.st_gid != expected_gid
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != expected_mode
            or not minimum <= opened.st_size <= maximum
        ):
            raise ProductionControlSnapshotError(f"{label} metadata is unsafe")
        remaining = opened.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                raise ProductionControlSnapshotError(f"{label} is incomplete")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ProductionControlSnapshotError(f"{label} changed while reading")
        after = os.fstat(descriptor)
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_uid,
            after.st_gid,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if before_identity != after_identity:
            raise ProductionControlSnapshotError(f"{label} changed while reading")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _set_owner(path: Path, *, uid: int, gid: int) -> None:
    metadata = path.lstat()
    if (metadata.st_uid, metadata.st_gid) != (uid, gid):
        os.chown(path, uid, gid, follow_symlinks=False)


def _validate_directory(path: Path, *, uid: int, gid: int, mode: int) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ProductionControlSnapshotError("control directory is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != uid
        or metadata.st_gid != gid
        or stat.S_IMODE(metadata.st_mode) != mode
    ):
        raise ProductionControlSnapshotError("control directory metadata is unsafe")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW,
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_trusted_parent(path: Path, *, uid: int, gid: int) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ProductionControlSnapshotError(
            "control parent directory is unavailable"
        ) from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != uid
        or metadata.st_gid != gid
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise ProductionControlSnapshotError(
            "control parent directory metadata is unsafe"
        )


class ProductionControlSnapshot:
    """Prepare, activate, and attest immutable root execution bytes."""

    def __init__(
        self,
        *,
        paths: SnapshotPaths | None = None,
        runner: CommandRunner | None = None,
        expected_uid: int = 0,
        expected_gid: int = 0,
        expected_operator_uid: int = PRODUCTION_OPERATOR_UID,
        expected_operator_gid: int = PRODUCTION_OPERATOR_GID,
    ) -> None:
        active_paths = paths or SnapshotPaths()
        self.paths = active_paths
        self.runner = runner or SubprocessRunner()
        self.expected_uid = expected_uid
        self.expected_gid = expected_gid
        self.expected_operator_uid = expected_operator_uid
        self.expected_operator_gid = expected_operator_gid
        for path in (
            active_paths.repository,
            active_paths.control_root,
            active_paths.lifecycle_lock,
            active_paths.approval_root,
        ):
            if not path.is_absolute() or ".." in path.parts:
                raise ProductionControlSnapshotError(
                    "production control paths must be absolute and normalized"
                )

    def _run(
        self,
        argv: Sequence[str],
        *,
        pass_fds: Sequence[int] = (),
        run_as_operator: bool = False,
    ) -> bytes:
        try:
            completed = self.runner.run(
                argv,
                pass_fds=pass_fds,
                user=self.expected_operator_uid if run_as_operator else None,
                group=self.expected_operator_gid if run_as_operator else None,
                extra_groups=() if run_as_operator else None,
            )
        except OSError as exc:
            raise ProductionControlSnapshotError("required command is unavailable") from exc
        if completed.returncode != 0:
            raise ProductionControlSnapshotError("required command rejected the input")
        if type(completed.stdout) is not bytes:
            raise ProductionControlSnapshotError("required command returned invalid output")
        return completed.stdout

    def _git_argv(self, *arguments: str) -> list[str]:
        return [
            GIT,
            "--no-replace-objects",
            "-c",
            f"safe.directory={self.paths.repository}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(self.paths.repository),
            *arguments,
        ]

    def _validated_expected_commit(self, expected_commit: str) -> str:
        if type(expected_commit) is not str or _COMMIT_RE.fullmatch(expected_commit) is None:
            raise ProductionControlSnapshotError(
                "expected private-repository commit must be 40 lowercase hex characters"
            )
        return expected_commit

    def _require_approved_commit(self, commit: str) -> None:
        """要求独立 OS 变更预先批准该精确 Git commit。"""

        _validate_directory(
            self.paths.approval_root,
            uid=self.expected_uid,
            gid=self.expected_gid,
            mode=0o755,
        )
        marker = _read_secure_file(
            self.paths.approval_root / commit,
            expected_uid=self.expected_uid,
            expected_gid=self.expected_gid,
            expected_mode=0o444,
            minimum=41,
            maximum=41,
            label="production control approval marker",
        )
        if marker != f"{commit}\n".encode("ascii"):
            raise ProductionControlSnapshotError(
                "production control approval marker is invalid"
            )

    def _git_inventory(self, commit: str) -> tuple[str, list[GitEntry]]:
        if _COMMIT_RE.fullmatch(commit) is None:
            raise ProductionControlSnapshotError("release commit is invalid")
        object_type = self._run(
            self._git_argv("cat-file", "-t", commit), run_as_operator=True
        )
        if object_type != b"commit\n":
            raise ProductionControlSnapshotError("release commit object is unavailable")
        tree_raw = self._run(
            self._git_argv("rev-parse", "--verify", f"{commit}^{{tree}}"),
            run_as_operator=True,
        )
        try:
            tree = tree_raw.rstrip(b"\n").decode("ascii")
        except UnicodeError as exc:
            raise ProductionControlSnapshotError("release tree ID is invalid") from exc
        if tree_raw != f"{tree}\n".encode("ascii") or _OBJECT_RE.fullmatch(tree) is None:
            raise ProductionControlSnapshotError("release tree ID is invalid")
        listing = self._run(
            self._git_argv("ls-tree", "-r", "-l", "-z", "--full-tree", commit),
            run_as_operator=True,
        )
        if len(listing) > MAX_TREE_LISTING_BYTES or not listing.endswith(b"\0"):
            raise ProductionControlSnapshotError("release tree inventory is invalid")
        records = listing[:-1].split(b"\0") if listing != b"\0" else []
        if not 1 <= len(records) <= MAX_TRACKED_FILES:
            raise ProductionControlSnapshotError("release tree file count is invalid")
        entries: list[GitEntry] = []
        seen: set[str] = set()
        previous_path = b""
        total_size = 0
        for record in records:
            metadata, separator, raw_path = record.partition(b"\t")
            fields = metadata.split()
            if separator != b"\t" or len(fields) != 4:
                raise ProductionControlSnapshotError("release tree entry is invalid")
            try:
                mode = fields[0].decode("ascii")
                object_type_value = fields[1].decode("ascii")
                object_id = fields[2].decode("ascii")
                raw_size = fields[3].decode("ascii")
                path = raw_path.decode("ascii")
            except UnicodeError as exc:
                raise ProductionControlSnapshotError(
                    "release tree entry is invalid"
                ) from exc
            safe_path = _safe_relative_path(path)
            if (
                mode not in {"100644", "100755"}
                or object_type_value != "blob"
                or _OBJECT_RE.fullmatch(object_id) is None
                or not raw_size.isdecimal()
                or (len(raw_size) > 1 and raw_size.startswith("0"))
                or raw_path <= previous_path
                or safe_path in seen
            ):
                raise ProductionControlSnapshotError("release tree entry is invalid")
            size = int(raw_size)
            if size > MAX_SINGLE_FILE_BYTES:
                raise ProductionControlSnapshotError("release tree file is oversized")
            total_size += size
            if total_size > MAX_TOTAL_BYTES:
                raise ProductionControlSnapshotError("release tree is oversized")
            entries.append(
                GitEntry(
                    path=safe_path,
                    mode=mode,
                    object_id=object_id,
                    size=size,
                )
            )
            seen.add(safe_path)
            previous_path = raw_path
        return tree, entries

    def _read_blob(self, entry: GitEntry) -> bytes:
        payload = self._run(
            self._git_argv("cat-file", "blob", entry.object_id),
            run_as_operator=True,
        )
        if len(payload) != entry.size or len(payload) > MAX_SINGLE_FILE_BYTES:
            raise ProductionControlSnapshotError("release Git blob size is invalid")
        return payload

    def _live_checkout_matches(self, commit: str) -> None:
        """以固定 operator 身份验证 HEAD 与 tracked/untracked 工作树均无漂移。"""

        head_raw = self._run(
            self._git_argv("rev-parse", "--verify", "HEAD"),
            run_as_operator=True,
        )
        if head_raw != f"{commit}\n".encode("ascii"):
            raise ProductionControlSnapshotError(
                "live checkout HEAD does not match the active control commit"
            )
        status_output = self._run(
            self._git_argv(
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ),
            run_as_operator=True,
        )
        if status_output:
            raise ProductionControlSnapshotError("live checkout is not clean")

    def _ensure_control_layout(self) -> None:
        _validate_trusted_parent(
            self.paths.control_root.parent,
            uid=self.expected_uid,
            gid=self.expected_gid,
        )
        for path in (self.paths.control_root, self.paths.versions_root):
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                try:
                    path.mkdir(mode=0o755)
                    _set_owner(path, uid=self.expected_uid, gid=self.expected_gid)
                    os.chmod(path, 0o755, follow_symlinks=False)
                except OSError as exc:
                    raise ProductionControlSnapshotError(
                        "control directory cannot be prepared"
                    ) from exc
            except OSError as exc:
                raise ProductionControlSnapshotError(
                    "control directory cannot be inspected"
                ) from exc
            else:
                if stat.S_ISLNK(metadata.st_mode):
                    raise ProductionControlSnapshotError(
                        "control directory metadata is unsafe"
                    )
            _validate_directory(
                path,
                uid=self.expected_uid,
                gid=self.expected_gid,
                mode=0o755,
            )

    def _write_snapshot_file(self, path: Path, payload: bytes, *, mode: int) -> None:
        descriptor = -1
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                0o600,
            )
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise OSError("short snapshot write")
                offset += written
            os.fchown(descriptor, self.expected_uid, self.expected_gid)
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
        except OSError as exc:
            raise ProductionControlSnapshotError("snapshot file cannot be written") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _snapshot_document(
        self, *, commit: str, tree: str, entries: Sequence[GitEntry]
    ) -> dict[str, object]:
        files: list[dict[str, object]] = []
        for entry in entries:
            if entry.sha256 is None:
                raise ProductionControlSnapshotError("snapshot digest is unavailable")
            files.append(
                {
                    "mode": entry.mode,
                    "path": entry.path,
                    "sha256": entry.sha256,
                    "size": entry.size,
                }
            )
        return {
            "commit": commit,
            "files": files,
            "schema_version": 1,
            "tree": tree,
        }

    def _cleanup_temporary_snapshot(self, path: Path) -> None:
        if not path.name.startswith(".prepare-") or path.parent != self.paths.versions_root:
            return
        with suppress(OSError):
            for root, directories, _ in os.walk(path, topdown=False):
                for name in directories:
                    os.chmod(Path(root) / name, 0o700, follow_symlinks=False)
                os.chmod(root, 0o700, follow_symlinks=False)
            shutil.rmtree(path)

    def _build_snapshot(
        self, *, commit: str, tree: str, inventory: Sequence[GitEntry]
    ) -> list[GitEntry]:
        destination = self.paths.versions_root / commit
        if destination.exists() or destination.is_symlink():
            self._verify_snapshot(commit=commit, inventory=inventory)
            manifest = self._load_snapshot_manifest(destination)
            return self._manifest_entries(manifest)
        temporary = self.paths.versions_root / f".prepare-{commit}-{secrets.token_hex(8)}"
        built: list[GitEntry] = []
        try:
            temporary.mkdir(mode=0o700)
            _set_owner(temporary, uid=self.expected_uid, gid=self.expected_gid)
            created_directories: set[Path] = {temporary}
            for entry in inventory:
                relative = PurePosixPath(entry.path)
                parent = temporary
                for part in relative.parts[:-1]:
                    parent /= part
                    if parent not in created_directories:
                        parent.mkdir(mode=0o700)
                        _set_owner(parent, uid=self.expected_uid, gid=self.expected_gid)
                        created_directories.add(parent)
                payload = self._read_blob(entry)
                digest = hashlib.sha256(payload).hexdigest()
                final_entry = replace(entry, sha256=digest)
                self._write_snapshot_file(
                    temporary.joinpath(*relative.parts),
                    payload,
                    mode=final_entry.filesystem_mode,
                )
                built.append(final_entry)
            manifest_bytes = _canonical_json(
                self._snapshot_document(commit=commit, tree=tree, entries=built)
            )
            if len(manifest_bytes) > MAX_SNAPSHOT_MANIFEST_BYTES:
                raise ProductionControlSnapshotError(
                    "snapshot manifest exceeds the allowed size"
                )
            self._write_snapshot_file(
                temporary / SNAPSHOT_MANIFEST_NAME,
                manifest_bytes,
                mode=0o444,
            )
            for directory in sorted(
                created_directories,
                key=lambda value: len(value.parts),
                reverse=True,
            ):
                os.chmod(directory, 0o555, follow_symlinks=False)
                _fsync_directory(directory)
            try:
                os.replace(temporary, destination)
            except OSError:
                if not destination.exists() or destination.is_symlink():
                    raise
            directory_fd = os.open(self.paths.versions_root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except (OSError, ProductionControlSnapshotError) as exc:
            self._cleanup_temporary_snapshot(temporary)
            if isinstance(exc, ProductionControlSnapshotError):
                raise
            raise ProductionControlSnapshotError("snapshot cannot be published") from exc
        self._cleanup_temporary_snapshot(temporary)
        self._verify_snapshot(commit=commit, inventory=inventory)
        return built

    def _load_snapshot_manifest(self, snapshot: Path) -> dict[str, Any]:
        raw = _read_secure_file(
            snapshot / SNAPSHOT_MANIFEST_NAME,
            expected_uid=self.expected_uid,
            expected_gid=self.expected_gid,
            expected_mode=0o444,
            maximum=MAX_SNAPSHOT_MANIFEST_BYTES,
            label="snapshot manifest",
        )
        document = _parse_json_object(raw, label="snapshot manifest")
        _require_exact_fields(document, _SNAPSHOT_FIELDS, label="snapshot manifest")
        return document

    def _manifest_entries(self, document: dict[str, Any]) -> list[GitEntry]:
        files = document.get("files")
        if type(files) is not list or not 1 <= len(files) <= MAX_TRACKED_FILES:
            raise ProductionControlSnapshotError("snapshot manifest files are invalid")
        result: list[GitEntry] = []
        previous = ""
        total = 0
        for raw_entry in files:
            if type(raw_entry) is not dict:
                raise ProductionControlSnapshotError("snapshot manifest entry is invalid")
            entry = cast(dict[str, Any], raw_entry)
            _require_exact_fields(
                entry, _SNAPSHOT_FILE_FIELDS, label="snapshot manifest entry"
            )
            path = _safe_relative_path(entry["path"])
            mode = entry["mode"]
            size = entry["size"]
            sha256 = entry["sha256"]
            if (
                path <= previous
                or mode not in {"100644", "100755"}
                or type(size) is not int
                or not 0 <= size <= MAX_SINGLE_FILE_BYTES
                or type(sha256) is not str
                or _SHA256_RE.fullmatch(sha256) is None
            ):
                raise ProductionControlSnapshotError("snapshot manifest entry is invalid")
            total += size
            if total > MAX_TOTAL_BYTES:
                raise ProductionControlSnapshotError("snapshot manifest is oversized")
            result.append(
                GitEntry(
                    path=path,
                    mode=mode,
                    object_id="",
                    size=size,
                    sha256=sha256,
                )
            )
            previous = path
        return result

    def _verify_snapshot(
        self,
        *,
        commit: str,
        inventory: Sequence[GitEntry] | None = None,
        expected_manifest_sha256: str | None = None,
    ) -> tuple[str, list[GitEntry], str]:
        snapshot = self.paths.versions_root / commit
        _validate_directory(
            snapshot,
            uid=self.expected_uid,
            gid=self.expected_gid,
            mode=0o555,
        )
        manifest_path = snapshot / SNAPSHOT_MANIFEST_NAME
        manifest_raw = _read_secure_file(
            manifest_path,
            expected_uid=self.expected_uid,
            expected_gid=self.expected_gid,
            expected_mode=0o444,
            maximum=MAX_SNAPSHOT_MANIFEST_BYTES,
            label="snapshot manifest",
        )
        manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()
        if (
            expected_manifest_sha256 is not None
            and not hmac.compare_digest(manifest_sha256, expected_manifest_sha256)
        ):
            raise ProductionControlSnapshotError("snapshot manifest digest drifted")
        document = _parse_json_object(manifest_raw, label="snapshot manifest")
        _require_exact_fields(document, _SNAPSHOT_FIELDS, label="snapshot manifest")
        if (
            type(document["schema_version"]) is not int
            or document["schema_version"] != 1
            or document["commit"] != commit
            or type(document["tree"]) is not str
            or _OBJECT_RE.fullmatch(document["tree"]) is None
        ):
            raise ProductionControlSnapshotError("snapshot manifest identity is invalid")
        entries = self._manifest_entries(document)
        if inventory is not None:
            expected = [(item.path, item.mode, item.size) for item in inventory]
            observed = [(item.path, item.mode, item.size) for item in entries]
            if expected != observed:
                raise ProductionControlSnapshotError(
                    "snapshot does not match the release Git tree"
                )
        expected_directories: set[str] = set()
        expected_files = {entry.path: entry for entry in entries}
        for entry in entries:
            parts = PurePosixPath(entry.path).parts
            for index in range(1, len(parts)):
                expected_directories.add("/".join(parts[:index]))
        observed_files: set[str] = set()
        observed_directories: set[str] = set()
        stack: list[tuple[Path, str]] = [(snapshot, "")]
        while stack:
            directory, prefix = stack.pop()
            try:
                children = list(os.scandir(directory))
            except OSError as exc:
                raise ProductionControlSnapshotError(
                    "snapshot cannot be inventoried"
                ) from exc
            for child in children:
                relative = f"{prefix}/{child.name}" if prefix else child.name
                metadata = child.stat(follow_symlinks=False)
                if child.is_symlink():
                    raise ProductionControlSnapshotError("snapshot contains a symbolic link")
                if child.is_dir(follow_symlinks=False):
                    if (
                        metadata.st_uid != self.expected_uid
                        or metadata.st_gid != self.expected_gid
                        or stat.S_IMODE(metadata.st_mode) != 0o555
                    ):
                        raise ProductionControlSnapshotError(
                            "snapshot directory metadata drifted"
                        )
                    observed_directories.add(relative)
                    stack.append((Path(child.path), relative))
                    continue
                if not child.is_file(follow_symlinks=False):
                    raise ProductionControlSnapshotError(
                        "snapshot contains a non-regular entry"
                    )
                if relative == SNAPSHOT_MANIFEST_NAME:
                    continue
                expected_entry = expected_files.get(relative)
                if expected_entry is None:
                    raise ProductionControlSnapshotError("snapshot contains an extra file")
                payload = _read_secure_file(
                    Path(child.path),
                    expected_uid=self.expected_uid,
                    expected_gid=self.expected_gid,
                    expected_mode=expected_entry.filesystem_mode,
                    minimum=0,
                    maximum=max(1, expected_entry.size),
                    label="snapshot file",
                )
                if len(payload) != expected_entry.size or not hmac.compare_digest(
                    hashlib.sha256(payload).hexdigest(), cast(str, expected_entry.sha256)
                ):
                    raise ProductionControlSnapshotError("snapshot file digest drifted")
                observed_files.add(relative)
        if observed_files != set(expected_files) or observed_directories != expected_directories:
            raise ProductionControlSnapshotError("snapshot tree is incomplete or has drifted")
        return document["tree"], entries, manifest_sha256

    @contextmanager
    def _lifecycle_lock(self) -> Iterator[None]:
        inherited = self._inherited_lifecycle_lock_fd()
        if inherited is not None:
            self._verify_inherited_lifecycle_lock(inherited)
            yield
            return
        descriptor = -1
        try:
            descriptor = os.open(self.paths.lifecycle_lock, os.O_RDWR | _NOFOLLOW)
            metadata = os.fstat(descriptor)
            expected = self.paths.lifecycle_lock.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != self.expected_uid
                or metadata.st_gid != self.expected_gid
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or (metadata.st_dev, metadata.st_ino)
                != (expected.st_dev, expected.st_ino)
            ):
                raise ProductionControlSnapshotError("lifecycle lock metadata is unsafe")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ProductionControlSnapshotError) as exc:
            if descriptor >= 0:
                os.close(descriptor)
            raise ProductionControlSnapshotError("lifecycle lock is unavailable") from exc
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _inherited_lifecycle_lock_fd(self) -> int | None:
        marker = os.environ.get("SMS_LIFECYCLE_LOCKED")
        raw_descriptor = os.environ.get("SMS_LIFECYCLE_LOCK_FD")
        if marker is None and raw_descriptor is None:
            return None
        if (
            marker != "1"
            or raw_descriptor is None
            or not raw_descriptor.isdecimal()
            or not 0 <= int(raw_descriptor) <= 2**31 - 1
        ):
            raise ProductionControlSnapshotError(
                "inherited lifecycle lock contract is invalid"
            )
        return int(raw_descriptor)

    def _verify_inherited_lifecycle_lock(self, descriptor: int) -> None:
        try:
            inherited = os.fstat(descriptor)
            expected = self.paths.lifecycle_lock.lstat()
        except OSError as exc:
            raise ProductionControlSnapshotError(
                "inherited lifecycle lock is unavailable"
            ) from exc
        for metadata in (inherited, expected):
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != self.expected_uid
                or metadata.st_gid != self.expected_gid
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise ProductionControlSnapshotError(
                    "inherited lifecycle lock metadata is unsafe"
                )
        if (inherited.st_dev, inherited.st_ino) != (expected.st_dev, expected.st_ino):
            raise ProductionControlSnapshotError(
                "inherited lifecycle lock targets the wrong inode"
            )
        probe = -1
        try:
            probe = os.open(self.paths.lifecycle_lock, os.O_RDWR | _NOFOLLOW)
            probe_metadata = os.fstat(probe)
            if (probe_metadata.st_dev, probe_metadata.st_ino) != (
                inherited.st_dev,
                inherited.st_ino,
            ):
                raise ProductionControlSnapshotError(
                    "inherited lifecycle lock changed during verification"
                )
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise ProductionControlSnapshotError(
                        "inherited lifecycle lock cannot be verified"
                    ) from exc
            else:
                fcntl.flock(probe, fcntl.LOCK_UN)
                raise ProductionControlSnapshotError(
                    "inherited lifecycle lock is not held"
                )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ProductionControlSnapshotError(
                    "inherited lifecycle lock is not the held descriptor"
                ) from exc
        finally:
            if probe >= 0:
                os.close(probe)

    def _read_optional_current(self) -> str | None:
        if not os.path.lexists(self.paths.current):
            return None
        try:
            metadata = self.paths.current.lstat()
            target = os.readlink(self.paths.current)
        except OSError as exc:
            raise ProductionControlSnapshotError(
                "production control current pointer is unavailable"
            ) from exc
        if (
            not stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != self.expected_uid
            or metadata.st_gid != self.expected_gid
            or re.fullmatch(r"versions/[0-9a-f]{40}", target) is None
        ):
            raise ProductionControlSnapshotError(
                "production control current pointer is invalid"
            )
        return target

    def _atomic_switch_current(self, target: str) -> None:
        temporary = self.paths.control_root / f".current-{secrets.token_hex(8)}"
        try:
            os.symlink(target, temporary)
            os.replace(temporary, self.paths.current)
            _fsync_directory(self.paths.control_root)
        except OSError as exc:
            with suppress(OSError):
                temporary.unlink()
            raise ProductionControlSnapshotError(
                "production control current pointer cannot be published"
            ) from exc

    def _activation_already_selected(self, target_commit: str) -> bool:
        current_target = self._read_optional_current()
        if current_target is None:
            return False
        current_commit = current_target.removeprefix("versions/")
        self._require_approved_commit(current_commit)
        current_git_tree, current_inventory = self._git_inventory(current_commit)
        current_snapshot_tree, _, _ = self._verify_snapshot(
            commit=current_commit,
            inventory=current_inventory,
        )
        if current_git_tree != current_snapshot_tree:
            raise ProductionControlSnapshotError(
                "current production control tree identity drifted"
            )
        if current_commit == target_commit:
            return True
        try:
            ancestry_output = self._run(
                self._git_argv(
                    "merge-base",
                    "--is-ancestor",
                    current_commit,
                    target_commit,
                ),
                run_as_operator=True,
            )
        except ProductionControlSnapshotError as exc:
            raise ProductionControlSnapshotError(
                "production control activation is not a forward transition"
            ) from exc
        if ancestry_output:
            raise ProductionControlSnapshotError(
                "production control ancestry check returned invalid output"
            )
        return False

    def plan(self, expected_commit: str) -> dict[str, object]:
        """只读验证显式控制资产来源 commit，不创建或切换快照。"""

        commit = self._validated_expected_commit(expected_commit)
        self._require_approved_commit(commit)
        tree, inventory = self._git_inventory(commit)
        prepared = False
        destination = self.paths.versions_root / commit
        if destination.exists() or destination.is_symlink():
            _validate_trusted_parent(
                self.paths.control_root.parent,
                uid=self.expected_uid,
                gid=self.expected_gid,
            )
            _validate_directory(
                self.paths.control_root,
                uid=self.expected_uid,
                gid=self.expected_gid,
                mode=0o755,
            )
            _validate_directory(
                self.paths.versions_root,
                uid=self.expected_uid,
                gid=self.expected_gid,
                mode=0o755,
            )
            verified_tree, _, _ = self._verify_snapshot(
                commit=commit,
                inventory=inventory,
            )
            if verified_tree != tree:
                raise ProductionControlSnapshotError(
                    "prepared snapshot tree identity drifted"
                )
            prepared = True
        return {
            "bytes": sum(entry.size for entry in inventory),
            "commit": commit,
            "files": len(inventory),
            "prepared": prepared,
            "status": "planned",
            "tree": tree,
        }

    def prepare(self, expected_commit: str) -> dict[str, object]:
        """从降权可读的 Git object 构建 root-owned 控制资产快照。"""

        commit = self._validated_expected_commit(expected_commit)
        self._require_approved_commit(commit)
        tree, inventory = self._git_inventory(commit)
        self._ensure_control_layout()
        self._build_snapshot(commit=commit, tree=tree, inventory=inventory)
        verified_tree, entries, _ = self._verify_snapshot(
            commit=commit,
            inventory=inventory,
        )
        if verified_tree != tree:
            raise ProductionControlSnapshotError("prepared snapshot tree identity drifted")
        return {
            "bytes": sum(entry.size for entry in entries),
            "commit": commit,
            "current_target": None,
            "files": len(entries),
            "status": "prepared",
            "tree": tree,
        }

    def activate(self, expected_commit: str) -> dict[str, object]:
        """在生命周期锁内用一个原子指针提交已验真的快照。"""

        with self._lifecycle_lock():
            commit = self._validated_expected_commit(expected_commit)
            self._require_approved_commit(commit)
            self._live_checkout_matches(commit)
            git_tree, inventory = self._git_inventory(commit)
            verified_tree, _, _ = self._verify_snapshot(
                commit=commit,
                inventory=inventory,
            )
            if git_tree != verified_tree:
                raise ProductionControlSnapshotError(
                    "prepared snapshot tree identity drifted"
                )
            if self._activation_already_selected(commit):
                return self.status()
            current_target = f"versions/{commit}"
            self._atomic_switch_current(current_target)
            result = self.status()
            if result["commit"] != commit:
                raise ProductionControlSnapshotError(
                    "production control activation verification failed"
                )
            return result

    def status(self) -> dict[str, object]:
        """只读验真 root 快照，并绑定固定 operator 的干净 checkout HEAD。"""

        _validate_trusted_parent(
            self.paths.control_root.parent,
            uid=self.expected_uid,
            gid=self.expected_gid,
        )
        _validate_directory(
            self.paths.control_root,
            uid=self.expected_uid,
            gid=self.expected_gid,
            mode=0o755,
        )
        _validate_directory(
            self.paths.versions_root,
            uid=self.expected_uid,
            gid=self.expected_gid,
            mode=0o755,
        )
        target = self._read_optional_current()
        if target is None:
            raise ProductionControlSnapshotError(
                "production control current pointer is unavailable"
            )
        commit = target.removeprefix("versions/")
        self._require_approved_commit(commit)
        git_tree, inventory = self._git_inventory(commit)
        tree, entries, _ = self._verify_snapshot(
            commit=commit,
            inventory=inventory,
        )
        if tree != git_tree:
            raise ProductionControlSnapshotError("production control tree identity drifted")
        self._live_checkout_matches(commit)
        return {
            "bytes": sum(entry.size for entry in entries),
            "commit": commit,
            "current_target": target,
            "files": len(entries),
            "status": "active",
            "tree": tree,
        }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "prepare", "activate", "status"))
    parser.add_argument("--expected-commit")
    return parser


def _validate_cli_identity(command: str) -> None:
    if os.geteuid() != 0:
        raise ProductionControlSnapshotError(
            "production control CLI must execute as root"
        )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)
    manager = ProductionControlSnapshot()
    try:
        _validate_cli_identity(arguments.command)
        if arguments.command == "status":
            if arguments.expected_commit is not None:
                raise ProductionControlSnapshotError(
                    "status does not accept an expected commit"
                )
            result = manager.status()
        else:
            if arguments.expected_commit is None:
                raise ProductionControlSnapshotError(
                    "an expected private-repository commit is required"
                )
            operation = getattr(manager, arguments.command)
            result = operation(arguments.expected_commit)
    except ProductionControlSnapshotError as exc:
        print(f"production-control-snapshot: {exc}", file=sys.stderr)
        return 2
    print(_canonical_json(result).decode("ascii"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
