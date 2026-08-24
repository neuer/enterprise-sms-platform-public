from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))

import release_store as release_store_module  # noqa: E402
from release_store import (  # noqa: E402
    ReleaseState,
    ReleaseStore,
    ReleaseStoreError,
)

MANIFEST_BYTES = b'{"release_id":"release-1"}\n'


def _store(tmp_path: Path) -> ReleaseStore:
    return ReleaseStore(tmp_path / "releases", "release-1")


def test_atomic_write_fsyncs_parent_directory_after_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    synced_directories: list[int] = []
    real_fsync = os.fsync

    def spy_fsync(fd: int) -> None:
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode):
            synced_directories.append(info.st_ino)
        real_fsync(fd)

    monkeypatch.setattr(release_store_module.os, "fsync", spy_fsync)
    store = _store(tmp_path)
    store.create(MANIFEST_BYTES)
    assert synced_directories, "ReleaseStore 必须 fsync 父目录"


def test_store_creates_root_owned_style_0700_directories(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create(MANIFEST_BYTES)

    for directory in (
        tmp_path / "releases",
        tmp_path / "releases" / "release-1",
        tmp_path / "releases" / "release-1" / "artifacts",
    ):
        info = directory.lstat()
        assert stat.S_ISDIR(info.st_mode)
        assert stat.S_IMODE(info.st_mode) == 0o700
        assert info.st_uid == os.geteuid()


def test_store_rejects_symlinked_release_root(tmp_path: Path) -> None:
    real_root = tmp_path / "real-releases"
    real_root.mkdir(mode=0o700)
    release_root = tmp_path / "releases"
    release_root.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(ReleaseStoreError, match="symlink"):
        ReleaseStore(release_root, "release-1").create(MANIFEST_BYTES)


def test_store_writes_0600_regular_files_atomically(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create(MANIFEST_BYTES)
    store.record_intent("verify", {"image": "api"})
    store.record_observation("verify", {"passed": True})

    release_dir = tmp_path / "releases" / "release-1"
    for filename in ("manifest.json", "state.json", "events.jsonl"):
        info = (release_dir / filename).lstat()
        assert stat.S_ISREG(info.st_mode)
        assert stat.S_IMODE(info.st_mode) == 0o600
        assert info.st_uid == os.geteuid()
    assert not list(release_dir.glob(".*.tmp"))
    events = [json.loads(line) for line in (release_dir / "events.jsonl").read_text().splitlines()]
    assert [event["kind"] for event in events] == ["intent", "observation"]


def test_duplicate_release_id_is_idempotent_only_for_identical_manifest(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.create(MANIFEST_BYTES)
    original_state = store.read_state()

    store.create(MANIFEST_BYTES)
    assert store.read_state() == original_state

    with pytest.raises(ReleaseStoreError, match="different manifest"):
        store.create(b'{"release_id":"release-1","changed":true}\n')


def test_atomic_failure_preserves_previous_state_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    store.create(MANIFEST_BYTES)
    state_path = tmp_path / "releases" / "release-1" / "state.json"
    previous = state_path.read_bytes()
    real_replace = os.replace

    def fail_state_replace(source: str | Path, target: str | Path) -> None:
        if Path(target) == state_path:
            raise OSError("injected replace failure")
        real_replace(source, target)

    monkeypatch.setattr(release_store_module.os, "replace", fail_state_replace)

    with pytest.raises(OSError, match="injected"):
        store.transition(ReleaseState.STAGED, ReleaseState.FAILED, failure_type="test")

    assert state_path.read_bytes() == previous
    assert not list(state_path.parent.glob(".*.tmp"))


def test_original_env_snapshot_is_never_returned_by_status(tmp_path: Path) -> None:
    tmp_path.chmod(0o755)
    store = _store(tmp_path)
    store.create(MANIFEST_BYTES)
    env_path = tmp_path / ".env"
    env_path.write_bytes(b"SETTING=opaque-value\n")
    env_path.chmod(0o600)

    store.snapshot_env(env_path)
    env_path.write_bytes(b"SETTING=changed\n")
    store.restore_env(env_path)

    status = store.read_state()
    assert "opaque-value" not in json.dumps(status)
    assert "original_env" not in status
    assert env_path.read_bytes() == b"SETTING=opaque-value\n"


def test_state_transition_rejects_illegal_edges(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create(MANIFEST_BYTES)
    store.transition(ReleaseState.STAGED, ReleaseState.PREPARED, step="checks-complete")

    with pytest.raises(ReleaseStoreError, match="illegal state transition"):
        store.transition(ReleaseState.PREPARED, ReleaseState.SUCCEEDED)

    assert store.read_state()["state"] == ReleaseState.PREPARED


def test_store_reads_strict_events_and_checkpoints_without_state_transition(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.create(MANIFEST_BYTES)
    store.record_intent("activate", {"phase": "env"})
    store.record_observation("activate", {"completed": True})

    events = store.read_events()
    store.checkpoint(ReleaseState.STAGED, interrupted_signal="TERM")

    assert [event["kind"] for event in events] == ["intent", "observation"]
    assert store.read_state()["interrupted_signal"] == "TERM"
    assert store.read_state()["state"] == ReleaseState.STAGED


def test_store_rejects_checkpoint_for_wrong_state_or_forbidden_fields(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.create(MANIFEST_BYTES)

    with pytest.raises(ReleaseStoreError, match="expected state"):
        store.checkpoint(ReleaseState.ACTIVATING, current_step="web")
    with pytest.raises(ReleaseStoreError, match="forbidden"):
        store.checkpoint(ReleaseState.STAGED, state="succeeded")
