from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))

import test_update_store as store_module  # noqa: E402
from test_update_store import (  # noqa: E402
    TestUpdateState as UpdateState,
)
from test_update_store import (  # noqa: E402
    TestUpdateStore as UpdateStore,
)
from test_update_store import (  # noqa: E402
    TestUpdateStoreError as StoreError,
)

COMMIT = "0123456789abcdef0123456789abcdef01234567"
BASE_COMMIT = "89abcdef0123456789abcdef0123456789abcdef"
DIGEST = "a" * 64
ARCHIVE_DIGEST = "b" * 64
FIRST_ID = "test-20260716T120000Z-000000000001"
SECOND_ID = "test-20260716T120001Z-000000000002"
SUCCESS_ID = "test-20260716T120002Z-000000000003"
FAILED_ID = "test-20260716T120003Z-000000000004"
ILLEGAL_ID = "test-20260716T120004Z-000000000005"
ROLLBACK_ID = "test-20260716T120005Z-000000000006"


def _request(update_id: str, *, commit: str = COMMIT) -> str:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "update_id": update_id,
        "base_commit": BASE_COMMIT,
        "commit": commit,
        "source_ref": "origin/feature/example",
        "environment_mode": "pre-live",
        "components": ["api"],
        "images": {
            "api": {
                "ref": f"sms-platform-test-api:{commit}",
                "id": f"sha256:{DIGEST}",
                "archive_file": "api.tar",
                "archive_sha256": ARCHIVE_DIGEST,
            }
        },
        "migration": {
            "from": "0015_account_provider_model",
            "target": "0015_account_provider_model",
            "compatibility": "none",
        },
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _store(tmp_path: Path, update_id: str = FIRST_ID) -> UpdateStore:
    return UpdateStore(tmp_path / "updates", update_id)


def test_create_uses_only_private_directories_and_fixed_private_files(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create(_request(FIRST_ID))

    update_root = tmp_path / "updates"
    update_dir = update_root / FIRST_ID
    for directory in (update_root, update_dir):
        info = directory.lstat()
        assert stat.S_ISDIR(info.st_mode)
        assert stat.S_IMODE(info.st_mode) == 0o700
        assert info.st_uid == os.geteuid()

    assert {path.name for path in update_dir.iterdir()} == {
        "request.json",
        "state.json",
        "events.jsonl",
    }
    for path in update_dir.iterdir():
        info = path.lstat()
        assert stat.S_ISREG(info.st_mode)
        assert stat.S_IMODE(info.st_mode) == 0o600
        assert info.st_uid == os.geteuid()
    assert not any("env" in path.name.lower() for path in update_dir.iterdir())


@pytest.mark.parametrize("update_id", ["../escape", "/absolute", "a/b", ".", "", "-bad"])
def test_rejects_unsafe_update_ids(tmp_path: Path, update_id: str) -> None:
    with pytest.raises(StoreError, match="invalid update ID"):
        UpdateStore(tmp_path / "updates", update_id)


def test_rejects_symlinked_or_non_directory_roots_and_update_directories(
    tmp_path: Path,
) -> None:
    real_root = tmp_path / "real"
    real_root.mkdir(mode=0o700)
    linked_root = tmp_path / "updates"
    linked_root.symlink_to(real_root, target_is_directory=True)
    with pytest.raises(StoreError, match="symlink"):
        UpdateStore(linked_root, FIRST_ID).create(_request(FIRST_ID))

    linked_root.unlink()
    linked_root.write_text("not a directory")
    with pytest.raises(StoreError, match="directory"):
        UpdateStore(linked_root, FIRST_ID).create(_request(FIRST_ID))

    linked_root.unlink()
    linked_root.mkdir(mode=0o700)
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    (linked_root / FIRST_ID).symlink_to(target, target_is_directory=True)
    with pytest.raises(StoreError, match="symlink"):
        UpdateStore(linked_root, FIRST_ID).create(_request(FIRST_ID))


def test_rejects_a_symlink_in_any_existing_root_ancestor(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir(mode=0o700)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    store = UpdateStore(linked_parent / "child" / "updates", FIRST_ID)

    with pytest.raises(StoreError, match="ancestor.*symlink|symlink.*ancestor"):
        store.create(_request(FIRST_ID))

    assert not (real_parent / "child").exists()


def test_ancestor_symlink_cannot_bypass_the_outside_git_check(tmp_path: Path) -> None:
    real_checkout = tmp_path / "real-checkout"
    real_checkout.mkdir(mode=0o700)
    (real_checkout / ".git").mkdir(mode=0o700)
    linked_checkout = tmp_path / "linked-checkout"
    linked_checkout.symlink_to(real_checkout, target_is_directory=True)
    store = UpdateStore(linked_checkout / "private" / "updates", FIRST_ID)

    with pytest.raises(StoreError, match="ancestor.*symlink|symlink.*ancestor"):
        store.create(_request(FIRST_ID))

    assert not (real_checkout / "private").exists()


def test_directory_anchor_rejects_replacement_between_stat_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor = tmp_path / "anchor"
    anchor.mkdir(mode=0o700)
    moved = tmp_path / "moved-anchor"
    real_open = store_module.os.open
    replaced = False

    def replace_ancestor_before_open(
        path: str | bytes | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal replaced
        if path == "anchor" and dir_fd is not None and not replaced:
            replaced = True
            anchor.rename(moved)
            anchor.mkdir(mode=0o700)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(store_module.os, "open", replace_ancestor_before_open)
    with pytest.raises(StoreError, match="changed while opening"):
        UpdateStore(anchor / "updates", FIRST_ID).create(_request(FIRST_ID))

    assert replaced is True
    assert not (anchor / "updates").exists()
    assert not (moved / "updates").exists()


@pytest.mark.parametrize("filename", ["request.json", "state.json", "events.jsonl"])
def test_rejects_symlinked_or_non_regular_controlled_files(
    tmp_path: Path,
    filename: str,
) -> None:
    store = _store(tmp_path)
    store.create(_request(FIRST_ID))
    path = tmp_path / "updates" / FIRST_ID / filename
    path.unlink()
    target = tmp_path / "target"
    target.write_text("outside")
    path.symlink_to(target)

    with pytest.raises(StoreError, match="symlink"):
        store.create(_request(FIRST_ID))

    path.unlink()
    path.mkdir(mode=0o700)
    with pytest.raises(StoreError, match="regular file"):
        store.create(_request(FIRST_ID))


@pytest.mark.parametrize("failed_filename", ["request.json", "events.jsonl", "state.json"])
def test_create_controlled_replace_failure_leaves_no_partial_store_and_can_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_filename: str,
) -> None:
    store = _store(tmp_path)
    request = _request(FIRST_ID)
    root = tmp_path / "updates"
    real_replace = store_module.os.replace
    injected = False

    def fail_selected_replace(
        source: str | bytes | Path,
        target: str | bytes | Path,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal injected
        if not injected and Path(target).name == failed_filename:
            injected = True
            raise OSError("injected create replace failure")
        real_replace(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(store_module.os, "replace", fail_selected_replace)
    with pytest.raises(OSError, match="injected create replace failure"):
        store.create(request)

    assert injected is True
    assert list(root.iterdir()) == []
    store.create(request)
    assert store.read_state()["state"] == "prepared"
    assert len(store.read_events()) == 1
    assert not [path for path in root.iterdir() if path.name.startswith(".")]


@pytest.mark.parametrize("failed_directory", ["update", "root"])
def test_create_post_replace_directory_fsync_failure_is_retryable_without_partial_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_directory: str,
) -> None:
    store = _store(tmp_path)
    request = _request(FIRST_ID)
    root = tmp_path / "updates"
    real_fsync_directory = UpdateStore._fsync_directory
    injected = False

    def fail_selected_fsync(
        self: UpdateStore,
        directory: Path,
        *,
        expected_identity: tuple[int, int] | None = None,
    ) -> None:
        nonlocal injected
        real_fsync_directory(
            self,
            directory,
            expected_identity=expected_identity,
        )
        is_update = directory.name.startswith(f".{FIRST_ID}.")
        selected = is_update if failed_directory == "update" else directory == root
        if not injected and selected:
            injected = True
            raise OSError("injected create directory fsync failure")

    monkeypatch.setattr(
        UpdateStore,
        "_fsync_directory",
        fail_selected_fsync,
    )
    with pytest.raises(OSError, match="injected create directory fsync failure"):
        store.create(request)

    assert injected is True
    assert not [path for path in root.iterdir() if path.name.startswith(".")]
    if store.update_dir.exists():
        assert store.read_state()["state"] == "prepared"
        assert len(store.read_events()) == 1
    store.create(request)
    assert store.read_state()["state"] == "prepared"
    assert len(store.read_events()) == 1


def test_rejects_wrong_owner_and_uses_no_follow_when_opening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    store.create(_request(FIRST_ID))
    real_open = store_module.os.open
    observed_flags: list[int] = []

    def recording_open(
        path: str | bytes | Path,
        flags: int,
        *args: object,
        **kwargs: object,
    ) -> int:
        observed_flags.append(flags)
        return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(store_module.os, "open", recording_open)
    store.read_state()
    assert observed_flags
    if hasattr(os, "O_NOFOLLOW"):
        assert all(flags & os.O_NOFOLLOW for flags in observed_flags)

    monkeypatch.setattr(store_module.os, "open", real_open)
    actual_uid = os.geteuid()
    monkeypatch.setattr(store_module.os, "geteuid", lambda: actual_uid + 1)
    with pytest.raises(StoreError, match="owner"):
        store.read_state()


def test_atomic_replace_is_same_directory_and_fsyncs_file_and_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    store.create(_request(FIRST_ID))
    real_replace = store_module.os.replace
    real_fsync = store_module.os.fsync
    replacements: list[tuple[Path, Path, int | None, int | None]] = []
    fsynced_modes: list[int] = []

    def recording_replace(
        source: str | bytes | Path,
        target: str | bytes | Path,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        replacements.append((Path(source), Path(target), src_dir_fd, dst_dir_fd))
        real_replace(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    def recording_fsync(descriptor: int) -> None:
        fsynced_modes.append(os.fstat(descriptor).st_mode)
        real_fsync(descriptor)

    monkeypatch.setattr(store_module.os, "replace", recording_replace)
    monkeypatch.setattr(store_module.os, "fsync", recording_fsync)

    store.transition(
        UpdateState.PREPARED,
        UpdateState.APPLIED,
        step="apply",
        actual_commit=COMMIT,
        actual_migration_head="0015_account_provider_model",
    )

    assert replacements
    assert all(
        source.parent == target.parent and src_dir_fd == dst_dir_fd
        for source, target, src_dir_fd, dst_dir_fd in replacements
    )
    assert any(stat.S_ISREG(mode) for mode in fsynced_modes)
    assert any(stat.S_ISDIR(mode) for mode in fsynced_modes)
    assert not list((tmp_path / "updates" / FIRST_ID).glob(".*.tmp"))


def _apply_test_transition(store: UpdateStore, operation: str) -> None:
    if operation == "transition":
        store.transition(UpdateState.PREPARED, UpdateState.APPLIED, step="apply")
        return
    store.fail(
        UpdateState.PREPARED,
        step="prepare",
        error_type="command_failed",
        actual_commit=COMMIT,
        actual_migration_head="0015_account_provider_model",
    )


@pytest.mark.parametrize("operation", ["transition", "fail"])
@pytest.mark.parametrize("failed_filename", ["events.jsonl", "state.json"])
def test_controlled_replace_failure_preserves_the_complete_previous_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    failed_filename: str,
) -> None:
    store = _store(tmp_path)
    request = _request(FIRST_ID)
    store.create(request)
    update_dir = tmp_path / "updates" / FIRST_ID
    controlled = {
        name: update_dir / name for name in ("request.json", "state.json", "events.jsonl")
    }
    original = {name: path.read_bytes() for name, path in controlled.items()}
    real_replace = store_module.os.replace
    injected = False

    def fail_selected_replace(
        source: str | bytes | Path,
        target: str | bytes | Path,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal injected
        if not injected and Path(target).name == failed_filename:
            injected = True
            raise OSError("injected controlled replace failure")
        real_replace(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(store_module.os, "replace", fail_selected_replace)
    with pytest.raises(OSError, match="injected controlled replace failure"):
        _apply_test_transition(store, operation)

    assert injected is True
    assert {name: path.read_bytes() for name, path in controlled.items()} == original
    assert store.read_state()["state"] == "prepared"
    assert len(store.read_events()) == 1
    store.create(request)
    assert not list(update_dir.glob(".*.tmp"))

    _apply_test_transition(store, operation)
    expected_state = "applied" if operation == "transition" else "blocked"
    assert store.read_state()["state"] == expected_state
    assert len(store.read_events()) == 2


@pytest.mark.parametrize("operation", ["transition", "fail"])
@pytest.mark.parametrize("failed_write_number", [1, 2])
def test_late_atomic_write_failure_is_compensated_to_the_previous_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    failed_write_number: int,
) -> None:
    store = _store(tmp_path)
    request = _request(FIRST_ID)
    store.create(request)
    update_dir = tmp_path / "updates" / FIRST_ID
    controlled = {
        name: update_dir / name for name in ("request.json", "state.json", "events.jsonl")
    }
    original = {name: path.read_bytes() for name, path in controlled.items()}
    real_fsync_directory = UpdateStore._fsync_directory
    fsync_count = 0
    injected = False

    def fail_after_selected_replace(
        self: UpdateStore,
        directory: Path,
        *,
        expected_identity: tuple[int, int] | None = None,
    ) -> None:
        nonlocal fsync_count, injected
        fsync_count += 1
        real_fsync_directory(
            self,
            directory,
            expected_identity=expected_identity,
        )
        if not injected and fsync_count == failed_write_number:
            injected = True
            raise OSError("injected post-replace failure")

    monkeypatch.setattr(
        UpdateStore,
        "_fsync_directory",
        fail_after_selected_replace,
    )
    with pytest.raises(OSError, match="injected post-replace failure"):
        _apply_test_transition(store, operation)

    assert injected is True
    assert {name: path.read_bytes() for name, path in controlled.items()} == original
    assert store.read_state()["state"] == "prepared"
    assert len(store.read_events()) == 1
    store.create(request)
    assert not list(update_dir.glob(".*.tmp"))

    _apply_test_transition(store, operation)
    expected_state = "applied" if operation == "transition" else "blocked"
    assert store.read_state()["state"] == expected_state
    assert len(store.read_events()) == 2


def test_compensation_failure_is_reported_as_a_fixed_corruption_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    store.create(_request(FIRST_ID))
    update_dir = tmp_path / "updates" / FIRST_ID
    events_path = update_dir / "events.jsonl"
    state_path = update_dir / "state.json"
    real_replace = store_module.os.replace
    state_failed = False
    compensation_failed = False

    def fail_commit_and_compensation(
        source: str | bytes | Path,
        target: str | bytes | Path,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal state_failed, compensation_failed
        target_name = Path(target).name
        if target_name == state_path.name and not state_failed:
            state_failed = True
            raise OSError("sensitive original failure text")
        if target_name == events_path.name and state_failed and not compensation_failed:
            compensation_failed = True
            raise OSError("sensitive compensation failure text")
        real_replace(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(store_module.os, "replace", fail_commit_and_compensation)
    with pytest.raises(StoreError) as captured:
        store.transition(UpdateState.PREPARED, UpdateState.APPLIED, step="apply")

    assert str(captured.value) == "state/event atomic commit is corrupt"
    assert "sensitive" not in str(captured.value)
    assert state_failed is True
    assert compensation_failed is True
    assert not list(update_dir.glob(".*.tmp"))


def test_identical_create_is_idempotent_but_changed_request_is_rejected(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    request = _request(FIRST_ID)
    store.create(request)
    state_before = store.read_state()
    events_before = store.read_events()

    store.create(request)
    assert store.read_state() == state_before
    assert store.read_events() == events_before

    with pytest.raises(StoreError, match="different request"):
        store.create(_request(FIRST_ID, commit="1" * 40))


def test_request_update_id_must_match_store(tmp_path: Path) -> None:
    with pytest.raises(StoreError, match="update ID"):
        _store(tmp_path).create(_request(SECOND_ID))


def test_failed_update_does_not_block_a_new_fix_forward_update(tmp_path: Path) -> None:
    first = _store(tmp_path, FIRST_ID)
    first.create(_request(FIRST_ID))
    first.transition(UpdateState.PREPARED, UpdateState.APPLIED, step="apply")
    first.fail(
        UpdateState.APPLIED,
        step="web_health",
        error_type="health_check_failed",
        actual_commit=COMMIT,
        actual_migration_head="0015_account_provider_model",
    )

    second = _store(tmp_path, SECOND_ID)
    second.create(_request(SECOND_ID, commit="1" * 40))

    assert first.read_state()["state"] == "blocked"
    assert second.read_state()["state"] == "prepared"


def test_only_declared_fix_forward_state_transitions_are_accepted(tmp_path: Path) -> None:
    assert {state.value for state in UpdateState} == {
        "prepared",
        "checkpointed",
        "migrated",
        "applied",
        "verifying",
        "verified",
        "rolled_back",
        "blocked",
    }

    backend = _store(tmp_path, SECOND_ID)
    backend.create(_request(SECOND_ID))
    backend.transition(
        UpdateState.PREPARED,
        UpdateState.CHECKPOINTED,
        step="checkpoint",
    )
    backend.transition(
        UpdateState.CHECKPOINTED,
        UpdateState.MIGRATED,
        step="migrate",
    )
    backend.transition(
        UpdateState.MIGRATED,
        UpdateState.APPLIED,
        step="apply",
    )
    backend.transition(
        UpdateState.APPLIED,
        UpdateState.VERIFIED,
        step="verify",
    )
    assert backend.read_state()["state"] == "verified"

    succeeded = _store(tmp_path, SUCCESS_ID)
    succeeded.create(_request(SUCCESS_ID))
    succeeded.transition(UpdateState.PREPARED, UpdateState.APPLIED, step="apply")
    succeeded.transition(UpdateState.APPLIED, UpdateState.VERIFIED, step="verify")
    with pytest.raises(StoreError, match="terminal"):
        succeeded.transition(UpdateState.VERIFIED, UpdateState.APPLIED, step="again")

    rolled_back = _store(tmp_path, ROLLBACK_ID)
    rolled_back.create(_request(ROLLBACK_ID))
    rolled_back.transition(
        UpdateState.PREPARED,
        UpdateState.ROLLED_BACK,
        step="rollback",
    )
    with pytest.raises(StoreError, match="terminal"):
        rolled_back.transition(
            UpdateState.ROLLED_BACK,
            UpdateState.APPLIED,
            step="again",
        )

    prepare_recovery_id = "test-20260829T120940Z-fa28fa63f496"
    prepare_recovery = _store(tmp_path, prepare_recovery_id)
    prepare_recovery.create(_request(prepare_recovery_id))
    prepare_recovery.fail(UpdateState.PREPARED, step="secret_expansion")
    prepare_recovery.transition(
        UpdateState.BLOCKED,
        UpdateState.CHECKPOINTED,
        step="recover_prepare",
    )
    assert prepare_recovery.read_state()["state"] == "checkpointed"

    failed = _store(tmp_path, FAILED_ID)
    failed.create(_request(FAILED_ID))
    failed.fail(UpdateState.PREPARED, step="prepare")
    with pytest.raises(StoreError, match="illegal state transition"):
        failed.transition(UpdateState.BLOCKED, UpdateState.APPLIED, step="again")
    failed.transition(
        UpdateState.BLOCKED,
        UpdateState.VERIFYING,
        step="recover_verify",
    )
    failed.transition(
        UpdateState.VERIFYING,
        UpdateState.VERIFIED,
        step="verify",
    )
    assert failed.read_state()["state"] == "verified"

    illegal = _store(tmp_path, ILLEGAL_ID)
    illegal.create(_request(ILLEGAL_ID))
    with pytest.raises(StoreError, match="illegal state transition"):
        illegal.transition(UpdateState.PREPARED, UpdateState.VERIFIED, step="skip")


def test_failed_state_requires_fixed_redacted_metadata(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create(_request(FIRST_ID))

    with pytest.raises(StoreError, match="error type"):
        store.fail(UpdateState.PREPARED, step="prepare", error_type="secret=raw message")
    with pytest.raises(StoreError, match="actual commit"):
        store.fail(UpdateState.PREPARED, step="prepare", actual_commit="not-a-commit")
    with pytest.raises(StoreError, match="migration head"):
        store.fail(UpdateState.PREPARED, step="prepare", actual_migration_head="../head")

    store.fail(
        UpdateState.PREPARED,
        step="prepare",
        error_type="step_failed",
        actual_commit=None,
        actual_migration_head=None,
    )
    state = store.read_state()
    assert state["error_type"] == "step_failed"
    assert state["actual_commit"] is None
    assert state["actual_migration_head"] is None
    assert "message" not in state
    assert all("message" not in event for event in store.read_events())


@pytest.mark.parametrize("filename", ["request.json", "state.json", "events.jsonl"])
def test_strict_reads_reject_corrupt_duplicate_unknown_and_wrong_typed_json(
    tmp_path: Path,
    filename: str,
) -> None:
    store = _store(tmp_path)
    store.create(_request(FIRST_ID))
    path = tmp_path / "updates" / FIRST_ID / filename

    if filename == "request.json":
        bad_values = [
            b"not-json",
            _request(FIRST_ID).replace(
                '"schema_version":1', '"schema_version":1,"schema_version":1'
            ).encode(),
            _request(FIRST_ID).replace(
                '"schema_version":1', '"schema_version":1,"unknown":true'
            ).encode(),
            _request(FIRST_ID).replace('"schema_version":1', '"schema_version":"1"').encode(),
        ]
        reader = store.read_request
    elif filename == "state.json":
        state = json.loads(path.read_text())
        bad_values = [
            b"not-json",
            (path.read_text().rstrip("\n")[:-1] + ',"state":"prepared"}\n').encode(),
            json.dumps({**state, "unknown": True}).encode(),
            json.dumps({**state, "event_sequence": True}).encode(),
        ]
        reader = store.read_state
    else:
        event = json.loads(path.read_text().splitlines()[0])
        bad_values = [
            b"not-json\n",
            (path.read_text().splitlines()[0][:-1] + ',"kind":"state_transition"}\n').encode(),
            (json.dumps({**event, "unknown": True}) + "\n").encode(),
            (json.dumps({**event, "sequence": True}) + "\n").encode(),
        ]
        reader = store.read_events

    for bad_value in bad_values:
        path.write_bytes(bad_value)
        path.chmod(0o600)
        with pytest.raises(StoreError):
            reader()


def test_private_modes_and_update_event_sequence_are_strict(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create(_request(FIRST_ID))
    state_path = tmp_path / "updates" / FIRST_ID / "state.json"
    state_path.chmod(0o644)
    with pytest.raises(StoreError, match="mode"):
        store.read_state()

    state_path.chmod(0o600)
    events_path = state_path.parent / "events.jsonl"
    event = json.loads(events_path.read_text().splitlines()[0])
    event["sequence"] = 2
    events_path.write_text(json.dumps(event) + "\n")
    events_path.chmod(0o600)
    with pytest.raises(StoreError, match="sequence"):
        store.read_events()


@pytest.mark.parametrize(
    "operation",
    ["read_state", "read_request", "read_events", "transition", "create"],
)
def test_every_entry_rejects_a_non_private_store_root_without_repairing_it(
    tmp_path: Path,
    operation: str,
) -> None:
    store = _store(tmp_path)
    request = _request(FIRST_ID)
    store.create(request)
    store.root.chmod(0o755)

    with pytest.raises(StoreError, match="directory mode must be 0700: updates"):
        if operation == "read_state":
            store.read_state()
        elif operation == "read_request":
            store.read_request()
        elif operation == "read_events":
            store.read_events()
        elif operation == "transition":
            store.transition(UpdateState.PREPARED, UpdateState.APPLIED, step="apply")
        else:
            store.create(request)

    assert stat.S_IMODE(store.root.lstat().st_mode) == 0o755
    store.root.chmod(0o700)
    assert store.read_state()["state"] == "prepared"
    assert store.read_request().update_id == FIRST_ID
    assert len(store.read_events()) == 1
    store.transition(UpdateState.PREPARED, UpdateState.APPLIED, step="apply")
    assert store.read_state()["state"] == "applied"


def test_every_access_rejects_an_invalid_owner_reported_for_the_exact_store_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    store.create(_request(FIRST_ID))
    root_info = store.root.stat()
    real_fstat = store_module.os.fstat

    def wrong_owner_for_root(descriptor: int) -> os.stat_result:
        info = real_fstat(descriptor)
        if (info.st_dev, info.st_ino) != (root_info.st_dev, root_info.st_ino):
            return info
        values = list(info)
        values[4] = os.geteuid() + 1
        return os.stat_result(values)

    monkeypatch.setattr(store_module.os, "fstat", wrong_owner_for_root)
    with pytest.raises(StoreError, match="private directory owner is invalid: updates"):
        store.read_state()


def test_public_api_exposes_only_terminal_no_migration_rollback_state(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    forbidden = {
        "restore",
        "restore_env",
        "snapshot_env",
        "resume",
        "reset",
        "initialize_data",
        "delete_volume",
    }
    assert forbidden.isdisjoint(set(dir(store)))
    assert "rolled_back" in {state.value for state in UpdateState}
