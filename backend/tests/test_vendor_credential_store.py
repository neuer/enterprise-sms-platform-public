from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))

NAME = "formal-name-sentinel"
KEY = "formal-key-sentinel"
NOW = datetime(2026, 7, 17, 8, tzinfo=UTC)


def test_first_install_creates_private_generation_and_safe_status(tmp_path: Path) -> None:
    import vendor_credential_store as store_module

    store = store_module.VendorCredentialStore(tmp_path / "credentials", clock=lambda: NOW)

    status = store.install(store_module.VendorCredentials(NAME, KEY))

    assert status == store_module.CredentialStatus(True, "normal", NOW)
    assert NAME not in repr(status) and KEY not in repr(status)
    assert not hasattr(status, "generation")
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    active = store.root / "active"
    assert active.is_file() and not active.is_symlink()
    assert stat.S_IMODE(active.stat().st_mode) == 0o600
    generation = store.active_generation()
    assert generation.is_dir() and not generation.is_symlink()
    assert stat.S_IMODE(generation.stat().st_mode) == 0o700
    files = {path.name: path for path in generation.iterdir()}
    assert set(files) == {"vendor_secret_name", "vendor_secret_key", "installed_at"}
    assert all(path.is_file() and not path.is_symlink() for path in files.values())
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in files.values())


def test_active_read_always_returns_both_values_from_same_generation(tmp_path: Path) -> None:
    import vendor_credential_store as store_module

    store = store_module.VendorCredentialStore(tmp_path / "credentials", clock=lambda: NOW)
    store.install(store_module.VendorCredentials("name-v1", "key-v1"))
    first = store.active_generation()
    store.install(store_module.VendorCredentials("name-v2", "key-v2"))
    second = store.active_generation()

    active = store.read_active()

    assert (active.secret_name, active.secret_key) == ("name-v2", "key-v2")
    assert first != second
    generations = sorted(path for path in store.root.iterdir() if path.is_dir())
    assert generations == sorted((first, second))


def test_rotation_transaction_remains_durable_until_runtime_commit(
    tmp_path: Path,
) -> None:
    import vendor_credential_store as store_module

    store = store_module.VendorCredentialStore(tmp_path / "credentials", clock=lambda: NOW)
    store.install(store_module.VendorCredentials("name-v1", "key-v1"))
    previous = store.active_generation()

    staged = store.stage(store_module.VendorCredentials("name-v2", "key-v2"))

    assert staged.state == "rotating"
    assert store.active_generation() == previous
    assert (store.read_active().secret_name, store.read_active().secret_key) == (
        "name-v1",
        "key-v1",
    )

    transaction = store.begin_rotation()

    assert transaction.previous_generation == previous
    assert transaction.new_generation == store.active_generation()
    assert transaction.phase == "switched"
    assert store.read_rotation_transaction() == transaction
    state_file = store.root / "rotation-state.json"
    assert state_file.is_file()
    assert stat.S_IMODE(state_file.stat().st_mode) == 0o600
    state_payload = state_file.read_text(encoding="ascii")
    assert "name-v1" not in state_payload
    assert "key-v1" not in state_payload
    assert "name-v2" not in state_payload
    assert "key-v2" not in state_payload
    assert (store.root / "pending").is_file()
    assert (store.read_active().secret_name, store.read_active().secret_key) == (
        "name-v2",
        "key-v2",
    )

    store.commit_rotation(transaction)

    assert store.read_rotation_transaction() is None
    assert not (store.root / "rotation-state.json").exists()
    assert not (store.root / "pending").exists()
    assert (store.read_active().secret_name, store.read_active().secret_key) == (
        "name-v2",
        "key-v2",
    )


def test_rotation_transaction_rolls_back_idempotently_before_cleanup(
    tmp_path: Path,
) -> None:
    import vendor_credential_store as store_module

    store = store_module.VendorCredentialStore(tmp_path / "credentials", clock=lambda: NOW)
    store.install(store_module.VendorCredentials("name-v1", "key-v1"))
    previous = store.active_generation()
    store.stage(store_module.VendorCredentials("name-v2", "key-v2"))
    transaction = store.begin_rotation()

    rolling_back = store.rollback_to_previous(transaction)

    assert rolling_back.phase == "rollback_started"
    assert store.active_generation() == previous
    assert store.read_rotation_transaction() == rolling_back
    assert (store.root / "pending").exists()

    repeated = store.rollback_to_previous(rolling_back)
    store.complete_rollback(repeated)

    assert store.active_generation() == previous
    assert store.read_rotation_transaction() is None
    assert not (store.root / "pending").exists()
    assert (store.read_active().secret_name, store.read_active().secret_key) == (
        "name-v1",
        "key-v1",
    )


def test_rollback_cleanup_can_resume_after_candidate_was_already_removed(
    tmp_path: Path,
) -> None:
    import vendor_credential_store as store_module

    store = store_module.VendorCredentialStore(tmp_path / "credentials", clock=lambda: NOW)
    store.install(store_module.VendorCredentials("name-v1", "key-v1"))
    store.stage(store_module.VendorCredentials("name-v2", "key-v2"))
    transaction = store.rollback_to_previous(store.begin_rotation())

    (store.root / "pending").unlink()
    shutil.rmtree(transaction.new_generation)

    recovered = store.read_rotation_transaction()

    assert recovered == transaction
    store.complete_rollback(recovered)
    assert store.read_rotation_transaction() is None
    assert (store.read_active().secret_name, store.read_active().secret_key) == (
        "name-v1",
        "key-v1",
    )


def test_recover_pending_discards_candidate_not_yet_activated(tmp_path: Path) -> None:
    import vendor_credential_store as store_module

    store = store_module.VendorCredentialStore(tmp_path / "credentials", clock=lambda: NOW)
    store.install(store_module.VendorCredentials("name-v1", "key-v1"))
    previous = store.active_generation()
    store.stage(store_module.VendorCredentials("name-v2", "key-v2"))

    recovered = store.recover_pending()

    assert recovered == "discarded"
    assert store.active_generation() == previous
    assert not (store.root / "pending").exists()
    assert (store.read_active().secret_name, store.read_active().secret_key) == (
        "name-v1",
        "key-v1",
    )


def test_recover_pending_never_erases_an_incomplete_rotation_transaction(
    tmp_path: Path,
) -> None:
    import vendor_credential_store as store_module

    store = store_module.VendorCredentialStore(tmp_path / "credentials", clock=lambda: NOW)
    store.install(store_module.VendorCredentials("name-v1", "key-v1"))
    store.stage(store_module.VendorCredentials("name-v2", "key-v2"))
    transaction = store.begin_rotation()

    with pytest.raises(store_module.CredentialStoreError, match="事务"):
        store.recover_pending()

    assert store.read_rotation_transaction() == transaction
    assert store.active_generation() == transaction.new_generation
    assert (store.root / "pending").exists()
    assert (store.read_active().secret_name, store.read_active().secret_key) == (
        "name-v2",
        "key-v2",
    )


def test_active_switch_failure_leaves_prepared_transaction_for_recovery(
    tmp_path: Path,
) -> None:
    import vendor_credential_store as store_module

    store = store_module.VendorCredentialStore(tmp_path / "credentials", clock=lambda: NOW)
    store.install(store_module.VendorCredentials("name-v1", "key-v1"))
    previous = store.active_generation()
    store.stage(store_module.VendorCredentials("name-v2", "key-v2"))

    def fail_active_pointer(source: Path, destination: Path) -> None:
        if Path(destination).name == "active":
            raise OSError("private-injected-detail")
        os.replace(source, destination)

    failing = store_module.VendorCredentialStore(
        store.root,
        clock=lambda: NOW,
        replace=fail_active_pointer,
    )

    with pytest.raises(store_module.CredentialStoreError):
        failing.begin_rotation()

    transaction = store.read_rotation_transaction()
    assert transaction is not None
    assert transaction.phase == "prepared"
    assert transaction.previous_generation == previous
    assert store.active_generation() == previous
    assert (store.root / "pending").exists()


def test_failed_active_pointer_switch_keeps_previous_generation_active(
    tmp_path: Path,
) -> None:
    import vendor_credential_store as store_module

    store = store_module.VendorCredentialStore(tmp_path / "credentials", clock=lambda: NOW)
    store.install(store_module.VendorCredentials("name-v1", "key-v1"))
    previous = store.active_generation()

    def fail_pointer(source: Path, destination: Path) -> None:
        if Path(destination).name == "active":
            raise OSError("private-injected-detail")
        os.replace(source, destination)

    failing = store_module.VendorCredentialStore(
        store.root,
        clock=lambda: NOW,
        replace=fail_pointer,
    )
    with pytest.raises(store_module.CredentialStoreError) as captured:
        failing.install(store_module.VendorCredentials(NAME, KEY))

    assert store.active_generation() == previous
    assert (store.read_active().secret_name, store.read_active().secret_key) == (
        "name-v1",
        "key-v1",
    )
    assert NAME not in str(captured.value) and KEY not in str(captured.value)
    assert "private-injected-detail" not in str(captured.value)


@pytest.mark.parametrize("target", ("symlink-root", "symlink-active", "symlink-file"))
def test_store_rejects_symlink_boundaries(tmp_path: Path, target: str) -> None:
    import vendor_credential_store as store_module

    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    if target == "symlink-root":
        linked = tmp_path / "credentials"
        linked.symlink_to(real, target_is_directory=True)
        store = store_module.VendorCredentialStore(linked, clock=lambda: NOW)
    else:
        store = store_module.VendorCredentialStore(tmp_path / "credentials", clock=lambda: NOW)
        store.install(store_module.VendorCredentials("name-v1", "key-v1"))
        if target == "symlink-active":
            (store.root / "active").unlink()
            (store.root / "active").symlink_to("generation-invalid")
        else:
            generation = store.active_generation()
            (generation / "vendor_secret_key").unlink()
            (generation / "vendor_secret_key").symlink_to(tmp_path / "outside")

    with pytest.raises(store_module.CredentialStoreError):
        store.read_active()


def test_reset_removes_all_valid_credential_artifacts_and_is_idempotent(
    tmp_path: Path,
) -> None:
    import vendor_credential_store as store_module

    store = store_module.VendorCredentialStore(tmp_path / "credentials", clock=lambda: NOW)
    store.install(store_module.VendorCredentials("name-v1", "key-v1"))
    store.stage(store_module.VendorCredentials("name-v2", "key-v2"))
    store.begin_rotation()
    staging = store.root / (".staging-" + "a" * 32)
    staging.mkdir(mode=0o700)

    first = store.reset()
    second = store.reset()

    expected = store_module.CredentialStatus(False, "setup_required", None)
    assert first == expected and second == expected
    assert store.status() == expected
    assert store.root.is_dir() and not store.root.is_symlink()
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert list(store.root.iterdir()) == []


def test_reset_required_distinguishes_clean_store_from_safe_partial_inventory(
    tmp_path: Path,
) -> None:
    import vendor_credential_store as store_module

    store = store_module.VendorCredentialStore(tmp_path / "credentials", clock=lambda: NOW)

    assert store.reset_required() is False

    store.install(store_module.VendorCredentials(NAME, KEY))
    assert store.reset_required() is True

    (store.active_generation() / "vendor_secret_key").unlink()
    assert store.reset_required() is True

    store.reset()
    assert store.reset_required() is False


def test_reset_required_rejects_unknown_inventory(
    tmp_path: Path,
) -> None:
    import vendor_credential_store as store_module

    store = store_module.VendorCredentialStore(tmp_path / "credentials", clock=lambda: NOW)
    store.reset()
    (store.root / "unexpected").write_text("private-content", encoding="utf-8")

    with pytest.raises(store_module.CredentialStoreError) as captured:
        store.reset_required()

    assert "private-content" not in str(captured.value)


def test_reset_rejects_unknown_inventory_without_deleting_known_data(
    tmp_path: Path,
) -> None:
    import vendor_credential_store as store_module

    store = store_module.VendorCredentialStore(tmp_path / "credentials", clock=lambda: NOW)
    store.install(store_module.VendorCredentials(NAME, KEY))
    active = store.root / "active"
    unknown = store.root / "unexpected"
    unknown.write_text("untrusted-path-content", encoding="utf-8")

    with pytest.raises(store_module.CredentialStoreError) as captured:
        store.reset()

    assert active.exists()
    assert unknown.exists()
    message = str(captured.value)
    assert NAME not in message and KEY not in message
    assert "untrusted-path-content" not in message


def test_reset_never_follows_unsafe_entry_outside_credential_root(
    tmp_path: Path,
) -> None:
    import vendor_credential_store as store_module

    store = store_module.VendorCredentialStore(tmp_path / "credentials", clock=lambda: NOW)
    store.install(store_module.VendorCredentials(NAME, KEY))
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "must-remain"
    sentinel.write_text("outside-private-content", encoding="utf-8")
    unsafe = store.root / (".staging-" + "b" * 32)
    unsafe.symlink_to(outside, target_is_directory=True)

    with pytest.raises(store_module.CredentialStoreError) as captured:
        store.reset()

    assert sentinel.read_text(encoding="utf-8") == "outside-private-content"
    assert unsafe.is_symlink()
    message = str(captured.value)
    assert NAME not in message and KEY not in message
    assert "outside-private-content" not in message


def test_reset_can_retry_after_partial_removal_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vendor_credential_store as store_module

    store = store_module.VendorCredentialStore(tmp_path / "credentials", clock=lambda: NOW)
    store.install(store_module.VendorCredentials(NAME, KEY))
    original_rmtree = store_module.shutil.rmtree
    failed = False

    def fail_once(path: Path) -> None:
        nonlocal failed
        if not failed:
            failed = True
            (Path(path) / "vendor_secret_key").unlink()
            raise OSError("private-removal-detail")
        original_rmtree(path)

    monkeypatch.setattr(store_module.shutil, "rmtree", fail_once)
    with pytest.raises(store_module.CredentialStoreError) as captured:
        store.reset()
    monkeypatch.setattr(store_module.shutil, "rmtree", original_rmtree)

    assert not (store.root / "active").exists()
    assert any(store.root.iterdir())
    assert "private-removal-detail" not in str(captured.value)
    assert NAME not in str(captured.value) and KEY not in str(captured.value)
    assert store.reset() == store_module.CredentialStatus(
        False,
        "setup_required",
        None,
    )
    assert list(store.root.iterdir()) == []


def test_reset_accepts_partially_deleted_rollback_candidate(
    tmp_path: Path,
) -> None:
    import vendor_credential_store as store_module

    store = store_module.VendorCredentialStore(tmp_path / "credentials", clock=lambda: NOW)
    store.install(store_module.VendorCredentials("name-v1", "key-v1"))
    store.stage(store_module.VendorCredentials("name-v2", "key-v2"))
    transaction = store.rollback_to_previous(store.begin_rotation())
    (transaction.new_generation / "vendor_secret_key").unlink()

    status = store.reset()

    assert status == store_module.CredentialStatus(
        False,
        "setup_required",
        None,
    )
    assert list(store.root.iterdir()) == []


def test_reset_accepts_strict_pointer_to_already_removed_generation(
    tmp_path: Path,
) -> None:
    import vendor_credential_store as store_module

    store = store_module.VendorCredentialStore(tmp_path / "credentials", clock=lambda: NOW)
    store.install(store_module.VendorCredentials(NAME, KEY))
    generation = store.active_generation()
    shutil.rmtree(generation)

    assert store.reset() == store_module.CredentialStatus(
        False,
        "setup_required",
        None,
    )
    assert list(store.root.iterdir()) == []


def test_reset_rejects_malformed_pointer_without_deleting_generation(
    tmp_path: Path,
) -> None:
    import vendor_credential_store as store_module

    store = store_module.VendorCredentialStore(tmp_path / "credentials", clock=lambda: NOW)
    store.install(store_module.VendorCredentials(NAME, KEY))
    generation = store.active_generation()
    (store.root / "active").write_text(generation.name, encoding="ascii")

    with pytest.raises(store_module.CredentialStoreError) as captured:
        store.reset()

    assert generation.exists()
    assert (store.root / "active").exists()
    assert NAME not in str(captured.value) and KEY not in str(captured.value)


def test_reset_accepts_rollback_candidate_that_is_already_missing(
    tmp_path: Path,
) -> None:
    import vendor_credential_store as store_module

    store = store_module.VendorCredentialStore(tmp_path / "credentials", clock=lambda: NOW)
    store.install(store_module.VendorCredentials("name-v1", "key-v1"))
    store.stage(store_module.VendorCredentials("name-v2", "key-v2"))
    transaction = store.rollback_to_previous(store.begin_rotation())
    shutil.rmtree(transaction.new_generation)

    assert store.reset() == store_module.CredentialStatus(
        False,
        "setup_required",
        None,
    )
    assert list(store.root.iterdir()) == []


@pytest.mark.parametrize(
    "fault",
    ("duplicate", "fields", "schema", "phase", "same_generation"),
)
def test_reset_rejects_invalid_rotation_transaction_without_deleting_data(
    tmp_path: Path,
    fault: str,
) -> None:
    import vendor_credential_store as store_module

    store = store_module.VendorCredentialStore(tmp_path / "credentials", clock=lambda: NOW)
    store.install(store_module.VendorCredentials("name-v1", "key-v1"))
    store.stage(store_module.VendorCredentials("name-v2", "key-v2"))
    store.begin_rotation()
    state_path = store.root / "rotation-state.json"
    document = json.loads(state_path.read_text(encoding="ascii"))
    if fault == "duplicate":
        payload = (
            state_path.read_text(encoding="ascii")
            .replace('"phase":"switched"', '"phase":"switched","phase":"prepared"')
        )
    else:
        if fault == "fields":
            document["unexpected"] = "private-content"
        elif fault == "schema":
            document["schema_version"] = 2
        elif fault == "phase":
            document["phase"] = "unknown"
        else:
            document["new_generation"] = document["previous_generation"]
        payload = json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n"
    state_path.write_text(payload, encoding="ascii")

    with pytest.raises(store_module.CredentialStoreError) as captured:
        store.reset()

    assert (store.root / "active").exists()
    assert state_path.exists()
    assert any(path.is_dir() for path in store.root.iterdir())
    assert "private-content" not in str(captured.value)
