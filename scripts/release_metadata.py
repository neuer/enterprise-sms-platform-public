#!/usr/bin/env python3
"""生成发布候选的版本、提交、迁移、契约、workflow 与 SBOM 绑定。"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import stat
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_REVISION_RE = re.compile(r"[0-9]{4}_[a-z0-9_]+")
_REPOSITORY_RE = re.compile(r"(?:local|[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_IMAGES = ("api", "web", "postgres", "redis")
_MAX_RELEASE_INPUT_BYTES = 512 * 1024 * 1024


class ReleaseMetadataError(ValueError):
    """发布身份无法从受控源唯一确定。"""


@dataclass(frozen=True, slots=True)
class ReleaseMetadata:
    app_version: str
    git_sha: str
    schema_revision: str
    openapi_sha256: str
    workflow_repository: str
    workflow_run_id: int
    workflow_run_attempt: int
    sbom_sha256: Mapping[str, str]

    def as_json(self) -> dict[str, object]:
        return asdict(self)


def _literal_assignment(path: Path, name: str) -> str | tuple[str, ...] | None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, SyntaxError) as error:
        raise ReleaseMetadataError("migration source is unreadable") from error
    for statement in tree.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        if not any(isinstance(target, ast.Name) and target.id == name for target in targets):
            continue
        if statement.value is None:
            raise ReleaseMetadataError("migration identity is missing")
        try:
            value = ast.literal_eval(statement.value)
        except (ValueError, TypeError) as error:
            raise ReleaseMetadataError("migration identity is not literal") from error
        if value is None or isinstance(value, str):
            return value
        if isinstance(value, tuple) and all(isinstance(item, str) for item in value):
            return value
        raise ReleaseMetadataError("migration identity has an invalid shape")
    raise ReleaseMetadataError(f"migration {name} is missing")


def schema_head(root: Path) -> str:
    revisions: set[str] = set()
    parents: set[str] = set()
    parent_map: dict[str, tuple[str, ...]] = {}
    migration_root = root / "backend/migrations/versions"
    for path in sorted(migration_root.glob("*.py")):
        revision = _literal_assignment(path, "revision")
        down_revision = _literal_assignment(path, "down_revision")
        if not isinstance(revision, str) or _REVISION_RE.fullmatch(revision) is None:
            raise ReleaseMetadataError("migration revision is invalid")
        if revision in revisions:
            raise ReleaseMetadataError("migration revision is duplicated")
        revisions.add(revision)
        if isinstance(down_revision, str):
            parents.add(down_revision)
            parent_map[revision] = (down_revision,)
        elif isinstance(down_revision, tuple):
            parents.update(down_revision)
            parent_map[revision] = down_revision
        else:
            parent_map[revision] = ()
    if parents - revisions:
        raise ReleaseMetadataError("migration graph references an unknown parent")
    heads = revisions - parents
    if len(heads) != 1:
        raise ReleaseMetadataError("migration graph must have exactly one head")
    head = heads.pop()
    reachable: set[str] = set()
    pending = [head]
    while pending:
        revision = pending.pop()
        if revision in reachable:
            continue
        reachable.add(revision)
        pending.extend(parent_map[revision])
    if reachable != revisions:
        raise ReleaseMetadataError("migration graph is disconnected or cyclic")
    return head


def source_version(root: Path) -> str:
    try:
        version = (root / "VERSION").read_text(encoding="ascii").strip()
        backend = tomllib.loads(
            (root / "backend/pyproject.toml").read_text(encoding="utf-8")
        )["project"]["version"]
        frontend = json.loads(
            (root / "frontend/package.json").read_text(encoding="utf-8")
        )["version"]
        frontend_lock = json.loads(
            (root / "frontend/package-lock.json").read_text(encoding="utf-8")
        )
        openapi = (root / "openapi.yaml").read_text(encoding="utf-8")
    except (OSError, UnicodeError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ReleaseMetadataError("version projection is unreadable") from error
    match = re.search(r"(?m)^  version: ([^\s]+)$", openapi)
    values = {
        version,
        str(backend),
        str(frontend),
        str(frontend_lock.get("version")),
        str(frontend_lock.get("packages", {}).get("", {}).get("version")),
        match.group(1) if match is not None else "",
    }
    if len(values) != 1 or _VERSION_RE.fullmatch(version) is None:
        raise ReleaseMetadataError("version projections do not match VERSION")
    return version


def _sha256(path: Path) -> str:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ReleaseMetadataError("release evidence input is unsafe")
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > _MAX_RELEASE_INPUT_BYTES:
                raise ReleaseMetadataError("release evidence input is too large")
            digest.update(chunk)
        return digest.hexdigest()
    except ReleaseMetadataError:
        raise
    except OSError as error:
        raise ReleaseMetadataError("release evidence input is unavailable") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def collect_release_metadata(
    root: Path,
    *,
    commit: str,
    workflow_repository: str,
    workflow_run_id: int,
    workflow_run_attempt: int,
    sboms: Mapping[str, Path],
) -> ReleaseMetadata:
    if not root.is_absolute():
        raise ReleaseMetadataError("release root must use an absolute path")
    if _COMMIT_RE.fullmatch(commit) is None:
        raise ReleaseMetadataError("release commit is invalid")
    if _REPOSITORY_RE.fullmatch(workflow_repository) is None:
        raise ReleaseMetadataError("workflow repository is invalid")
    if workflow_run_id < 0 or workflow_run_attempt < 0:
        raise ReleaseMetadataError("workflow identity is invalid")
    if workflow_repository != "local" and (
        workflow_run_id < 1 or workflow_run_attempt < 1
    ):
        raise ReleaseMetadataError("GitHub workflow identity is incomplete")
    if set(sboms) != set(_IMAGES):
        raise ReleaseMetadataError("exactly four image SBOMs are required")
    if any(not path.is_absolute() for path in sboms.values()):
        raise ReleaseMetadataError("SBOM paths must be absolute")
    if len(set(sboms.values())) != len(_IMAGES):
        raise ReleaseMetadataError("image SBOM paths must be distinct")
    hashes = {name: _sha256(sboms[name]) for name in _IMAGES}
    if any(_SHA256_RE.fullmatch(value) is None for value in hashes.values()):
        raise ReleaseMetadataError("SBOM digest is invalid")
    return ReleaseMetadata(
        source_version(root),
        commit,
        schema_head(root),
        _sha256(root / "openapi.yaml"),
        workflow_repository,
        workflow_run_id,
        workflow_run_attempt,
        hashes,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload: dict[str, Any] = {
            "app_version": source_version(args.root),
            "schema_revision": schema_head(args.root),
            "openapi_sha256": _sha256(args.root / "openapi.yaml"),
        }
    except ReleaseMetadataError as error:
        print(f"release-metadata: {error}", file=sys.stderr)
        return 1
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
