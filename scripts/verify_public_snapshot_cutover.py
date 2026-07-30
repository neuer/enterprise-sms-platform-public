#!/usr/bin/env python3
"""验真私有基线到无历史公开快照的单次快速更新边界。"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))

from check_public_readiness import is_excluded, load_policy  # noqa: E402
from export_public_snapshot import export_snapshot  # noqa: E402
from test_update_contract import classify_public_cutover_paths  # noqa: E402

_MANIFEST_FIELDS = frozenset(
    {"schema_version", "source_commit", "policy_sha256", "history_included"}
)
_RESOLVED_REF_RE = re.compile(
    r"refs/test-updates/test-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}/source"
)
_PACK_HASH_RE = re.compile(r"pack[\t ]+([0-9a-f]{40})")
_MAX_SOURCE_PACK_BYTES = 64 * 1024 * 1024


class PublicSnapshotCutoverError(ValueError):
    """公开快照 cutover 证据链不完整或不一致。"""


def _is_full_sha(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 40
        and all(character in "0123456789abcdef" for character in value)
    )


def _git(
    repository: Path,
    *arguments: str,
    input_bytes: bytes | None = None,
) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        input=input_bytes,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise PublicSnapshotCutoverError("cutover Git evidence is unavailable")
    return result.stdout


def _git_text(
    repository: Path,
    *arguments: str,
    input_bytes: bytes | None = None,
) -> str:
    try:
        return _git(
            repository,
            *arguments,
            input_bytes=input_bytes,
        ).decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise PublicSnapshotCutoverError("cutover Git evidence is invalid") from error


def _is_ancestor(repository: Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "merge-base",
            "--is-ancestor",
            ancestor,
            descendant,
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode not in {0, 1}:
        raise PublicSnapshotCutoverError("cutover ancestry is unavailable")
    return result.returncode == 0


def _merge_base(repository: Path, left: str, right: str) -> str:
    candidates = _git_text(
        repository,
        "merge-base",
        "--all",
        left,
        right,
    ).splitlines()
    if len(candidates) != 1 or not _is_full_sha(candidates[0]):
        raise PublicSnapshotCutoverError(
            "test baseline and snapshot source lack one trusted merge base"
        )
    return candidates[0]


def _parse_manifest(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublicSnapshotCutoverError("public snapshot manifest is invalid") from error
    if type(value) is not dict or set(value) != _MANIFEST_FIELDS:
        raise PublicSnapshotCutoverError("public snapshot manifest is invalid")
    if (
        value.get("schema_version") != 1
        or value.get("history_included") is not False
        or not _is_full_sha(value.get("source_commit"))
        or type(value.get("policy_sha256")) is not str
        or len(value["policy_sha256"]) != 64
        or any(
            character not in "0123456789abcdef"
            for character in value["policy_sha256"]
        )
    ):
        raise PublicSnapshotCutoverError("public snapshot manifest is invalid")
    return value


def _extract_commit(repository: Path, commit: str, destination: Path) -> None:
    archive_path = destination.parent / f"{commit}.tar"
    _git(
        repository,
        "archive",
        "--format=tar",
        f"--output={archive_path}",
        commit,
    )
    destination.mkdir(mode=0o700)
    try:
        with tarfile.open(archive_path, mode="r:") as archive:
            archive.extractall(destination, filter="data")
    except (OSError, tarfile.TarError) as error:
        raise PublicSnapshotCutoverError(
            "public snapshot tree is unavailable"
        ) from error
    finally:
        archive_path.unlink(missing_ok=True)


def _content_map(root: Path) -> dict[str, tuple[str, str, bool]]:
    result: dict[str, tuple[str, str, bool]] = {}
    try:
        for candidate in sorted(root.rglob("*")):
            relative = candidate.relative_to(root).as_posix()
            metadata = candidate.lstat()
            if stat.S_ISDIR(metadata.st_mode):
                continue
            if stat.S_ISLNK(metadata.st_mode):
                result[relative] = ("symlink", os.readlink(candidate), False)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise PublicSnapshotCutoverError(
                    "public snapshot tree contains an unsupported entry"
                )
            result[relative] = (
                "file",
                hashlib.sha256(candidate.read_bytes()).hexdigest(),
                bool(metadata.st_mode & stat.S_IXUSR),
            )
    except OSError as error:
        raise PublicSnapshotCutoverError(
            "public snapshot tree is unavailable"
        ) from error
    return result


def _changed_paths(repository: Path, older: str, newer: str) -> list[str]:
    raw = _git(
        repository,
        "diff",
        "--name-only",
        "--no-renames",
        "-z",
        older,
        newer,
    )
    try:
        return [item.decode("utf-8", errors="strict") for item in raw.split(b"\0") if item]
    except UnicodeDecodeError as error:
        raise PublicSnapshotCutoverError("cutover path evidence is invalid") from error


def _pack_object_ids(repository: Path, index_path: Path) -> set[str]:
    output = _git_text(repository, "verify-pack", "-v", str(index_path))
    object_ids: set[str] = set()
    for line in output.splitlines():
        fields = line.split()
        if fields and _is_full_sha(fields[0]):
            object_ids.add(fields[0])
    if not object_ids:
        raise PublicSnapshotCutoverError("public cutover source pack is invalid")
    return object_ids


@contextlib.contextmanager
def _source_pack_repository(
    repository: Path,
    *,
    source_pack: Path,
    source_commit: str,
    target: str,
    resolved_ref: str,
) -> Any:
    try:
        pack_metadata = source_pack.lstat()
    except OSError as error:
        raise PublicSnapshotCutoverError(
            "public cutover source pack is unavailable"
        ) from error
    if (
        not stat.S_ISREG(pack_metadata.st_mode)
        or stat.S_ISLNK(pack_metadata.st_mode)
        or pack_metadata.st_size <= 0
        or pack_metadata.st_size > _MAX_SOURCE_PACK_BYTES
    ):
        raise PublicSnapshotCutoverError("public cutover source pack is invalid")

    object_path_raw = _git_text(
        repository,
        "rev-parse",
        "--git-path",
        "objects",
    )
    object_path = Path(object_path_raw)
    if not object_path.is_absolute():
        object_path = repository / object_path
    object_path = object_path.resolve()
    if not object_path.is_dir() or object_path.is_symlink():
        raise PublicSnapshotCutoverError(
            "public cutover base object store is invalid"
        )

    with tempfile.TemporaryDirectory(prefix="sms-public-source-pack-") as temporary:
        evidence_repository = Path(temporary) / "evidence.git"
        init = subprocess.run(
            ["git", "init", "--bare", "--quiet", str(evidence_repository)],
            capture_output=True,
            check=False,
        )
        if init.returncode != 0:
            raise PublicSnapshotCutoverError(
                "public cutover evidence repository is unavailable"
            )
        alternates = evidence_repository / "objects" / "info" / "alternates"
        try:
            alternates.write_text(f"{object_path}\n", encoding="utf-8")
            pack_bytes = source_pack.read_bytes()
        except OSError as error:
            raise PublicSnapshotCutoverError(
                "public cutover source pack is unavailable"
            ) from error

        pack_result = _git_text(
            evidence_repository,
            "index-pack",
            "--stdin",
            input_bytes=pack_bytes,
        )
        match = _PACK_HASH_RE.fullmatch(pack_result)
        if match is None:
            raise PublicSnapshotCutoverError(
                "public cutover source pack is invalid"
            )
        index_path = (
            evidence_repository
            / "objects"
            / "pack"
            / f"pack-{match.group(1)}.idx"
        )
        if not index_path.is_file() or index_path.is_symlink():
            raise PublicSnapshotCutoverError(
                "public cutover source pack is invalid"
            )
        _git(
            evidence_repository,
            "cat-file",
            "-e",
            f"{source_commit}^{{commit}}",
        )
        expected_objects = {
            source_commit,
            *(
                _git_text(
                    evidence_repository,
                    "rev-list",
                    "--objects",
                    "--no-object-names",
                    f"{source_commit}^{{tree}}",
                ).splitlines()
            ),
        }
        if (
            any(not _is_full_sha(object_id) for object_id in expected_objects)
            or _pack_object_ids(evidence_repository, index_path) != expected_objects
        ):
            raise PublicSnapshotCutoverError(
                "public cutover source pack contains unexpected objects"
            )
        _git(
            evidence_repository,
            "update-ref",
            resolved_ref,
            target,
        )
        yield evidence_repository


def verify_public_snapshot_cutover(
    repository: Path,
    *,
    baseline: str,
    target: str,
    ref: str,
    resolved_ref: str | None = None,
    source_pack: Path | None = None,
    expected_source_commit: str | None = None,
    expected_private_merge_base: str | None = None,
) -> dict[str, object]:
    """验证快照重建、祖先关系并返回受控逻辑差异分类。"""

    repository = repository.resolve()
    if not repository.is_dir() or not _is_full_sha(baseline) or not _is_full_sha(target):
        raise PublicSnapshotCutoverError("cutover invocation is invalid")
    if ref != "origin/main":
        raise PublicSnapshotCutoverError("cutover ref must be origin/main")
    if resolved_ref is not None and _RESOLVED_REF_RE.fullmatch(resolved_ref) is None:
        raise PublicSnapshotCutoverError("cutover resolved ref is invalid")
    target_ref = resolved_ref or ref
    if (
        _git_text(repository, "rev-parse", "--verify", f"{target_ref}^{{commit}}")
        != target
    ):
        raise PublicSnapshotCutoverError("cutover target does not match origin/main")
    if _is_ancestor(repository, baseline, target):
        raise PublicSnapshotCutoverError("cutover requires unrelated public history")

    target_manifest_raw = _git(
        repository,
        "show",
        f"{target}:PUBLIC-SNAPSHOT.json",
    )
    manifest = _parse_manifest(target_manifest_raw)
    source_commit = manifest["source_commit"]
    assert isinstance(source_commit, str)
    if source_pack is None:
        if (
            expected_source_commit is not None
            or expected_private_merge_base is not None
        ):
            raise PublicSnapshotCutoverError(
                "public cutover source pack binding is incomplete"
            )
        _git(repository, "cat-file", "-e", f"{source_commit}^{{commit}}")
        private_merge_base = _merge_base(repository, baseline, source_commit)
        source_repository_context: Any = contextlib.nullcontext(repository)
    else:
        if (
            resolved_ref is None
            or expected_source_commit != source_commit
            or not _is_full_sha(expected_private_merge_base)
        ):
            raise PublicSnapshotCutoverError(
                "public cutover source pack binding is invalid"
            )
        assert isinstance(expected_private_merge_base, str)
        _git(
            repository,
            "cat-file",
            "-e",
            f"{expected_private_merge_base}^{{commit}}",
        )
        if not _is_ancestor(repository, expected_private_merge_base, baseline):
            raise PublicSnapshotCutoverError(
                "public cutover private merge base is invalid"
            )
        private_merge_base = expected_private_merge_base
        source_repository_context = _source_pack_repository(
            repository,
            source_pack=source_pack,
            source_commit=source_commit,
            target=target,
            resolved_ref=resolved_ref,
        )

    with source_repository_context as source_repository:
        policy_raw = _git(
            source_repository,
            "show",
            f"{source_commit}:public-repository.json",
        )
        if hashlib.sha256(policy_raw).hexdigest() != manifest["policy_sha256"]:
            raise PublicSnapshotCutoverError(
                "public snapshot policy digest does not match"
            )
        with tempfile.TemporaryDirectory(prefix="sms-public-cutover-") as temporary:
            temporary_root = Path(temporary)
            policy_path = temporary_root / "public-repository.json"
            policy_path.write_bytes(policy_raw)
            exported_root = temporary_root / "exported"
            try:
                exported_commit = export_snapshot(
                    source_repository,
                    ref=source_commit,
                    output=exported_root,
                    policy_path=policy_path,
                )
            except ValueError as error:
                raise PublicSnapshotCutoverError(
                    "public snapshot source cannot be reproduced"
                ) from error
            if exported_commit != source_commit:
                raise PublicSnapshotCutoverError(
                    "public snapshot source cannot be reproduced"
                )
            expected_tree = _content_map(exported_root)

            publication_commits: list[str] = []
            candidates = _git_text(
                source_repository,
                "rev-list",
                "--reverse",
                target,
                "--",
                "PUBLIC-SNAPSHOT.json",
            ).splitlines()
            for index, candidate_commit in enumerate(candidates):
                if not _is_full_sha(candidate_commit):
                    raise PublicSnapshotCutoverError(
                        "public snapshot history is invalid"
                    )
                candidate_manifest = _git(
                    source_repository,
                    "show",
                    f"{candidate_commit}:PUBLIC-SNAPSHOT.json",
                )
                if candidate_manifest != target_manifest_raw:
                    continue
                candidate_root = temporary_root / f"candidate-{index}"
                _extract_commit(
                    source_repository,
                    candidate_commit,
                    candidate_root,
                )
                if _content_map(candidate_root) == expected_tree:
                    publication_commits.append(candidate_commit)

            if len(publication_commits) != 1:
                raise PublicSnapshotCutoverError(
                    "public snapshot publication commit is ambiguous"
                )
            publication_commit = publication_commits[0]
            policy = load_policy(policy_path)
            logical_paths = {
                changed_path
                for changed_path in _changed_paths(
                    source_repository,
                    baseline,
                    source_commit,
                )
                if not is_excluded(changed_path, policy)
            }
            logical_paths.update(
                _changed_paths(
                    source_repository,
                    publication_commit,
                    target,
                )
            )

    scope = classify_public_cutover_paths(sorted(logical_paths))
    return {
        "components": sorted(scope.components),
        "cutover": True,
        "logical_changed": len(logical_paths),
        "migration_changed": scope.migration_changed,
        "private_merge_base": private_merge_base,
        "publication_commit": publication_commit,
        "risk": scope.risk,
        "runtime_changed": scope.runtime_changed,
        "source_commit": source_commit,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--resolved-ref")
    parser.add_argument("--source-pack", type=Path)
    parser.add_argument("--expected-source-commit")
    parser.add_argument("--expected-private-merge-base")
    args = parser.parse_args()
    try:
        result = verify_public_snapshot_cutover(
            args.repository,
            baseline=args.baseline,
            target=args.target,
            ref=args.ref,
            resolved_ref=args.resolved_ref,
            source_pack=args.source_pack,
            expected_source_commit=args.expected_source_commit,
            expected_private_merge_base=args.expected_private_merge_base,
        )
    except (OSError, PublicSnapshotCutoverError, ValueError):
        print("public snapshot cutover blocked", file=sys.stderr)
        return 1
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
