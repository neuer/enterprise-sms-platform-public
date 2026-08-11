from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))

import public_baseline_manager as baseline_module  # noqa: E402
from public_baseline_manager import (  # noqa: E402
    ACTIVE_ROOT,
    COMPONENTS,
    PUBLIC_ORIGIN_URL,
    SCHEMA_REVISION,
    BaselineManifest,
    GitIdentity,
    HostImageInspector,
    HostVendorControlUnitManager,
    PublicBaselineManager,
    PublicBaselineManagerError,
    _unlink_verified_artifact,
    parse_baseline_manifest,
    validate_request_binding,
    verify_operator_git_access,
)
from test_update_apply import BACKEND_SERVICES  # noqa: E402
from test_update_contract import (  # noqa: E402
    TestUpdateRequest as UpdateRequest,
)
from test_update_contract import (  # noqa: E402
    parse_test_update_request,
)
from test_update_store import TestUpdateState as UpdateState  # noqa: E402
from test_update_verify import TestUpdateVerifyError as VerifyError  # noqa: E402

ACTIVATION_ID = "test-20260731T120000Z-0123456789ab"
BASE_COMMIT = "1" * 40
BASE_TREE = "2" * 40
TARGET_COMMIT = "3" * 40
TARGET_TREE = "4" * 40
API_ID = f"sha256:{'5' * 64}"
WEB_ID = f"sha256:{'6' * 64}"
API_DIGEST = "7" * 64
WEB_DIGEST = "8" * 64
BUNDLE_DIGEST = "9" * 64


def _manifest_document() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "activation_id": ACTIVATION_ID,
        "origin_url": PUBLIC_ORIGIN_URL,
        "base": {"commit": BASE_COMMIT, "tree": BASE_TREE},
        "target": {"commit": TARGET_COMMIT, "tree": TARGET_TREE},
        "bundle": {
            "file": "public-baseline.bundle",
            "sha256": BUNDLE_DIGEST,
            "ref": "refs/heads/main",
        },
        "images": {
            "api": {
                "file": "api.tar",
                "sha256": API_DIGEST,
                "ref": f"sms-platform-test-api:{TARGET_COMMIT}",
                "id": API_ID,
                "version": "1.6.0",
                "revision": TARGET_COMMIT,
                "schema_revision": SCHEMA_REVISION,
            },
            "web": {
                "file": "web.tar",
                "sha256": WEB_DIGEST,
                "ref": f"sms-platform-test-web:{TARGET_COMMIT}",
                "id": WEB_ID,
                "version": "1.6.0",
                "revision": TARGET_COMMIT,
                "schema_revision": SCHEMA_REVISION,
            },
        },
        "migration": {
            "from": SCHEMA_REVISION,
            "target": SCHEMA_REVISION,
            "compatibility": "none",
        },
    }


def _manifest() -> BaselineManifest:
    return parse_baseline_manifest(
        json.dumps(
            _manifest_document(),
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    )


def _request_document() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "update_id": ACTIVATION_ID,
        "base_commit": BASE_COMMIT,
        "commit": TARGET_COMMIT,
        "source_ref": "origin/main",
        "environment_mode": "live",
        "components": ["api", "web"],
        "images": {
            "api": {
                "ref": f"sms-platform-test-api:{TARGET_COMMIT}",
                "id": API_ID,
                "archive_file": "api.tar",
                "archive_sha256": API_DIGEST,
            },
            "web": {
                "ref": f"sms-platform-test-web:{TARGET_COMMIT}",
                "id": WEB_ID,
                "archive_file": "web.tar",
                "archive_sha256": WEB_DIGEST,
            },
        },
        "migration": {
            "from": SCHEMA_REVISION,
            "target": SCHEMA_REVISION,
            "compatibility": "none",
        },
    }


def _request() -> tuple[str, UpdateRequest]:
    raw = json.dumps(
        _request_document(),
        separators=(",", ":"),
        sort_keys=True,
    )
    return raw, parse_test_update_request(raw)


def test_manifest_parser_accepts_only_exact_target_bound_contract() -> None:
    manifest = _manifest()

    assert manifest.activation_id == ACTIVATION_ID
    assert manifest.base == GitIdentity(BASE_COMMIT, BASE_TREE)
    assert manifest.target == GitIdentity(TARGET_COMMIT, TARGET_TREE)
    assert set(manifest.images) == COMPONENTS
    assert manifest.images["api"].version == "1.6.0"

    for mutate in (
        lambda value: value.update({"extra": True}),
        lambda value: value["images"]["api"].update({"revision": BASE_COMMIT}),
        lambda value: value["migration"].update({"target": "0040_forbidden"}),
        lambda value: value.update({"origin_url": "https://github.com/attacker/repository.git"}),
    ):
        document = _manifest_document()
        mutate(document)
        with pytest.raises(PublicBaselineManagerError):
            parse_baseline_manifest(json.dumps(document).encode())


def test_manifest_parser_rejects_duplicate_fields() -> None:
    raw = (
        b'{"schema_version":1,"schema_version":1,'
        b'"activation_id":"test-20260731T120000Z-0123456789ab"}'
    )
    with pytest.raises(PublicBaselineManagerError, match="duplicate"):
        parse_baseline_manifest(raw)


def test_standard_request_must_bind_every_mutable_identity() -> None:
    manifest = _manifest()
    _raw, request = _request()
    validate_request_binding(
        manifest,
        request,
        host_source_commit=TARGET_COMMIT,
    )

    changed = _request_document()
    changed["images"]["web"]["archive_sha256"] = "a" * 64
    changed_request = parse_test_update_request(json.dumps(changed))
    with pytest.raises(PublicBaselineManagerError, match="not bound"):
        validate_request_binding(
            manifest,
            changed_request,
            host_source_commit=TARGET_COMMIT,
        )
    with pytest.raises(PublicBaselineManagerError, match="not bound"):
        validate_request_binding(
            manifest,
            request,
            host_source_commit=BASE_COMMIT,
        )


def test_operator_git_probe_uses_fixed_operator_identity_and_safe_git_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def fake_run(command: object, **kwargs: object) -> SimpleNamespace:
        argv = tuple(command)  # type: ignore[arg-type]
        calls.append((argv, kwargs))
        output = PUBLIC_ORIGIN_URL if len(calls) == 1 else ""
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr(baseline_module.subprocess, "run", fake_run)

    verify_operator_git_access(
        Path("/opt/sms-platform"),
        operator_uid=1234,
        operator_gid=2345,
    )

    assert [call[0][3:] for call in calls] == [
        ("remote", "get-url", "origin"),
        ("rev-parse", "--verify", "HEAD^{commit}"),
        ("status", "--porcelain=v1", "--untracked-files=all"),
    ]
    assert all(call[1]["user"] == 1234 for call in calls)
    assert all(call[1]["group"] == 2345 for call in calls)
    assert all(call[1]["extra_groups"] == () for call in calls)
    assert all(call[1]["env"]["GIT_CONFIG_NOSYSTEM"] == "1" for call in calls)  # type: ignore[index]


class FakeStore:
    def __init__(self, state: UpdateState = UpdateState.PREPARED) -> None:
        self.state = state
        self.created: list[str] = []
        self.events: list[tuple[object, ...]] = []
        self.update_dir = Path("/")

    def create(self, raw: str) -> None:
        self.created.append(raw)

    def transition(
        self,
        expected: UpdateState,
        target: UpdateState,
        *,
        step: str,
        **_: object,
    ) -> None:
        assert self.state is expected
        self.events.append(("transition", expected, target, step))
        self.state = target

    def block(
        self,
        expected: UpdateState,
        *,
        step: str,
        **_: object,
    ) -> None:
        assert self.state is expected
        self.events.append(("block", expected, step))
        self.state = UpdateState.BLOCKED

    def read_consistent_state(self) -> dict[str, object]:
        return {"state": self.state.value}


class FakeCore:
    def __init__(self, *, cleanup_state: str = "verified") -> None:
        self.events: list[str] = []
        self.staged_root = Path("/opt/.sms-platform-public-staged")
        self.recovery_root = Path("/opt/.sms-platform-public-recovery")
        self.cleanup_state = cleanup_state

    def prepare(self, request: object) -> object:
        self.events.append("core_prepare")
        return SimpleNamespace(
            activation_id=ACTIVATION_ID,
            staged_root=self.staged_root,
            commit=TARGET_COMMIT,
            tree=TARGET_TREE,
        )

    def activate(self, request: object) -> object:
        self.events.append("core_activate")
        return SimpleNamespace(
            activation_id=ACTIVATION_ID,
            active_root=ACTIVE_ROOT,
            recovery_root=self.recovery_root,
            commit=TARGET_COMMIT,
            tree=TARGET_TREE,
        )

    def rollback(self) -> object:
        self.events.append("core_rollback")
        return SimpleNamespace(
            activation_id=ACTIVATION_ID,
            active_root=ACTIVE_ROOT,
            recovery_root=self.recovery_root,
            commit=BASE_COMMIT,
            tree=BASE_TREE,
        )

    def finalize(self) -> object:
        self.events.append("core_finalize")
        return SimpleNamespace(
            activation_id=ACTIVATION_ID,
            state="verified",
            active_root=ACTIVE_ROOT,
            recovery_root=self.recovery_root,
            commit=TARGET_COMMIT,
            tree=TARGET_TREE,
        )

    def cleanup(self) -> object:
        self.events.append(f"core_cleanup:{self.cleanup_state}")
        self.cleanup_state = "cleaned"
        return SimpleNamespace(
            activation_id=ACTIVATION_ID,
            state="cleaned",
            active_root=ACTIVE_ROOT,
            recovery_root=self.recovery_root,
            commit=TARGET_COMMIT,
            tree=TARGET_TREE,
        )


class FakeSourceInspector:
    def __init__(self) -> None:
        self.events: list[tuple[object, ...]] = []
        self.observed = GitIdentity(TARGET_COMMIT, TARGET_TREE)

    def verify(
        self,
        root: Path,
        *,
        identity: GitIdentity,
        origin_url: str,
    ) -> None:
        self.events.append(("source_verify", root, identity, origin_url))

    def observe(self, root: Path) -> GitIdentity:
        self.events.append(("source_observe", root))
        return self.observed


class FakeImageInspector:
    def __init__(self, fail: bool = False) -> None:
        self.events: list[str] = []
        self.fail = fail

    def verify(self, manifest: BaselineManifest) -> None:
        self.events.append("image_verify")
        if self.fail:
            raise RuntimeError("injected image drift")


class FakeUnitManager:
    def __init__(self, fail_activate: bool = False, fail_verify: bool = False) -> None:
        self.events: list[tuple[object, ...] | str] = []
        self.fail_activate = fail_activate
        self.fail_verify = fail_verify

    def preflight(self, active_root: Path, staged_root: Path) -> None:
        self.events.append(("unit_preflight", active_root, staged_root))

    def activate(self, outcome: object) -> None:
        self.events.append("unit_activate")
        if self.fail_activate:
            raise RuntimeError("injected unit activation failure")

    def restore(self, outcome: object) -> None:
        self.events.append("unit_restore")

    def verify(self, active_root: Path) -> None:
        self.events.append(("unit_verify", active_root))
        if self.fail_verify:
            raise RuntimeError("injected unit drift")


class FakeHostOperations:
    def __init__(
        self,
        *,
        counts: dict[str, int] | None = None,
        fail_replace: bool = False,
        fail_lock: bool = False,
        migration_head: str = SCHEMA_REVISION,
    ) -> None:
        self.events: list[object] = []
        self.counts = counts or {
            "submitting": 0,
            "retrying": 0,
            "uncertain": 0,
        }
        self.fail_replace = fail_replace
        self.fail_lock = fail_lock
        self.migration_head = migration_head

    def require_lifecycle_lock(self) -> None:
        self.events.append("lock")
        if self.fail_lock:
            raise RuntimeError("injected lock failure")

    def load_and_validate_images(self) -> None:
        self.events.append("load_images")

    def prepare_rollback_images(self) -> None:
        self.events.append("prepare_rollback_images")

    def validate_vendor_update_mode(self) -> str:
        self.events.append("mode")
        return "live"

    def pause_lanes_for_update(self, update_id: str) -> None:
        self.events.append(("pause", update_id))

    def finalize_pause_ownership(self, update_id: str) -> None:
        self.events.append(("pause_owner", update_id))

    def unsafe_status_counts(self) -> dict[str, int]:
        self.events.append("unsafe_counts")
        return self.counts

    def create_encrypted_checkpoint(self, update_id: str) -> str:
        raise AssertionError("no-migration baseline must not create a checkpoint")

    def check_expand_migration(self, source: str, target: str) -> None:
        raise AssertionError("no-migration baseline must not inspect a migration")

    def require_owned_update_pauses(self, update_id: str) -> None:
        self.events.append(("owned_pauses", update_id))

    def replace_backend_services(self, services: tuple[str, ...]) -> None:
        self.events.append(("replace_backend", services))
        if self.fail_replace:
            raise RuntimeError("injected replacement failure")

    def rollback_no_migration(
        self,
        kind: str,
        update_id: str,
    ) -> tuple[str, str]:
        self.events.append(("restore_images", kind, update_id))
        return BASE_COMMIT, SCHEMA_REVISION

    def rollback_no_migration_preserving_images(
        self,
        kind: str,
        update_id: str,
    ) -> tuple[str, str]:
        self.events.append(("restore_images_preserved", kind, update_id))
        return BASE_COMMIT, SCHEMA_REVISION

    def verify_web(self) -> None:
        self.events.append("verify_web")

    def verify_budget_conservation(self) -> None:
        self.events.append("budget")

    def verify_pause_state(self) -> None:
        self.events.append("verify_pauses")

    def probe_balance(self) -> None:
        self.events.append("balance")

    def verify_backend_services(self) -> None:
        self.events.append("verify_services")

    def restore_owned_update_pauses(self, update_id: str) -> None:
        self.events.append(("restore_pauses", update_id))

    def cleanup_rollback_images(self, update_id: str) -> None:
        self.events.append(("cleanup_images", update_id))

    def cleanup_rollback_images_verified(self, update_id: str) -> None:
        self.events.append(("cleanup_images_verified", update_id))

    def hold_fail_closed(self, update_id: str) -> None:
        self.events.append(("hold", update_id))

    def recover_blocked_rebaseline_verify(self, store: FakeStore) -> str:
        self.events.append("recover_verify")
        store.state = UpdateState.VERIFIED
        return "verified"

    def current_migration_head(self) -> str:
        self.events.append("migration_head")
        return self.migration_head


def _manager(
    *,
    store: FakeStore | None = None,
    core: FakeCore | None = None,
    operations: FakeHostOperations | None = None,
    source: FakeSourceInspector | None = None,
    images: FakeImageInspector | None = None,
    unit: FakeUnitManager | None = None,
    operator_git_probe: Callable[[Path], None] | None = None,
) -> tuple[
    PublicBaselineManager,
    FakeStore,
    FakeCore,
    FakeHostOperations,
    FakeSourceInspector,
    FakeImageInspector,
    FakeUnitManager,
]:
    raw, request = _request()
    actual_store = store or FakeStore()
    actual_core = core or FakeCore()
    actual_operations = operations or FakeHostOperations()
    actual_source = source or FakeSourceInspector()
    actual_images = images or FakeImageInspector()
    actual_unit = unit or FakeUnitManager()
    manager = PublicBaselineManager(
        manifest=_manifest(),
        core_request=object(),
        request_raw=raw,
        request=request,
        store=actual_store,  # type: ignore[arg-type]
        core=actual_core,  # type: ignore[arg-type]
        operations_factory=lambda _root, _request: actual_operations,  # type: ignore[arg-type,return-value]
        source_inspector=actual_source,
        image_inspector=actual_images,
        unit_manager=actual_unit,
        host_source_commit=TARGET_COMMIT,
        operator_git_probe=operator_git_probe or (lambda _root: None),
    )
    return (
        manager,
        actual_store,
        actual_core,
        actual_operations,
        actual_source,
        actual_images,
        actual_unit,
    )


def test_prepare_validates_staging_and_images_before_fixed_high_risk_pause() -> None:
    manager, store, core, operations, source, images, unit = _manager()

    manager.prepare()

    assert len(store.created) == 1
    assert core.events == ["core_prepare"]
    assert source.events[0][0] == "source_verify"
    assert unit.events[0][0] == "unit_preflight"
    assert images.events == ["image_verify"]
    assert operations.events == [
        "lock",
        "migration_head",
        "load_images",
        "prepare_rollback_images",
        "lock",
        "mode",
        ("pause", ACTIVATION_ID),
        "unsafe_counts",
        ("pause_owner", ACTIVATION_ID),
    ]
    assert store.state is UpdateState.PREPARED


def test_prepare_high_risk_rejects_uncertain_without_checkpoint() -> None:
    operations = FakeHostOperations(counts={"submitting": 0, "retrying": 0, "uncertain": 1})
    manager, store, _core, operations, *_ = _manager(operations=operations)

    with pytest.raises(Exception, match="prepare blocked"):
        manager.prepare()

    assert store.state is UpdateState.BLOCKED
    assert not any(
        isinstance(event, tuple) and event[0] == "checkpoint" for event in operations.events
    )
    assert operations.events[-1] == ("hold", ACTIVATION_ID)


def test_prepare_lock_failure_does_not_create_state_pause_or_touch_core() -> None:
    operations = FakeHostOperations(fail_lock=True)
    manager, store, core, operations, *_ = _manager(operations=operations)

    with pytest.raises(PublicBaselineManagerError, match="lock"):
        manager.prepare()

    assert store.created == []
    assert store.events == []
    assert core.events == []
    assert operations.events == ["lock"]


def test_prepare_migration_drift_blocks_before_staging_or_images() -> None:
    operations = FakeHostOperations(migration_head="0040_unexpected")
    manager, store, core, operations, *_ = _manager(operations=operations)

    with pytest.raises(PublicBaselineManagerError, match="prepare blocked"):
        manager.prepare()

    assert core.events == []
    assert operations.events == [
        "lock",
        "migration_head",
        ("hold", ACTIVATION_ID),
    ]
    assert store.state is UpdateState.BLOCKED


def test_apply_switches_root_then_unit_then_reuses_fixed_service_replacement() -> None:
    manager, store, core, operations, source, images, unit = _manager()

    manager.apply()

    assert core.events == ["core_activate"]
    assert unit.events[0] == "unit_activate"
    assert ("replace_backend", BACKEND_SERVICES) in operations.events
    assert store.state is UpdateState.APPLIED
    assert len(source.events) == 2
    assert images.events == ["image_verify", "image_verify"]


def test_apply_service_failure_restores_root_unit_and_old_images() -> None:
    operations = FakeHostOperations(fail_replace=True)
    manager, store, core, operations, _source, _images, unit = _manager(operations=operations)

    with pytest.raises(Exception, match="rolled back"):
        manager.apply()

    assert core.events == ["core_activate", "core_rollback"]
    assert unit.events[-1] == "unit_restore"
    assert (
        "restore_images_preserved",
        "backend-safe",
        ACTIVATION_ID,
    ) in operations.events
    assert store.state is UpdateState.ROLLED_BACK


def test_apply_rolls_back_when_operator_cannot_read_git_baseline() -> None:
    def fail_operator_git(_root: Path) -> None:
        raise PublicBaselineManagerError("operator Git preflight failed")

    manager, store, core, operations, _source, _images, unit = _manager(
        operator_git_probe=fail_operator_git,
    )

    with pytest.raises(
        PublicBaselineManagerError,
        match="rolled back before service replacement",
    ):
        manager.apply()

    assert core.events == ["core_activate", "core_rollback"]
    assert unit.events == ["unit_restore"]
    assert not any(
        isinstance(event, tuple) and event[0] == "replace_backend"
        for event in operations.events
    )
    assert store.state is UpdateState.ROLLED_BACK


def test_apply_unit_failure_rolls_back_before_any_service_replacement() -> None:
    unit = FakeUnitManager(fail_activate=True)
    manager, store, core, operations, _source, _images, unit = _manager(unit=unit)

    with pytest.raises(
        PublicBaselineManagerError,
        match="rolled back before service replacement",
    ):
        manager.apply()

    assert core.events == ["core_activate", "core_rollback"]
    assert unit.events == ["unit_activate", "unit_restore"]
    assert not any(
        isinstance(event, tuple) and event[0] == "replace_backend" for event in operations.events
    )
    assert (
        "restore_images_preserved",
        "backend-safe",
        ACTIVATION_ID,
    ) in operations.events
    assert store.state is UpdateState.ROLLED_BACK


def test_apply_lock_failure_does_not_pause_switch_or_rollback() -> None:
    operations = FakeHostOperations(fail_lock=True)
    manager, store, core, operations, *_ = _manager(operations=operations)

    with pytest.raises(PublicBaselineManagerError, match="lock"):
        manager.apply()

    assert core.events == []
    assert operations.events == ["lock"]
    assert store.state is UpdateState.PREPARED


def test_apply_migration_drift_blocks_without_switch_or_rollback() -> None:
    operations = FakeHostOperations(migration_head="0040_unexpected")
    manager, store, core, operations, *_ = _manager(operations=operations)

    with pytest.raises(
        PublicBaselineManagerError,
        match="blocked before root switch",
    ):
        manager.apply()

    assert core.events == []
    assert operations.events == [
        "lock",
        "migration_head",
        ("hold", ACTIVATION_ID),
    ]
    assert store.state is UpdateState.BLOCKED


def test_apply_resume_from_applied_only_revalidates_target() -> None:
    store = FakeStore(UpdateState.APPLIED)
    manager, store, core, operations, source, images, unit = _manager(store=store)

    manager.apply()

    assert core.events == []
    assert operations.events == ["lock", "verify_services"]
    assert len(source.events) == 1
    assert images.events == ["image_verify"]
    assert unit.events == [("unit_verify", ACTIVE_ROOT)]
    assert store.state is UpdateState.APPLIED


def test_verify_reuses_live_invariants_and_advances_verified() -> None:
    store = FakeStore(UpdateState.APPLIED)
    manager, store, _core, operations, source, images, unit = _manager(store=store)

    manager.verify()

    assert operations.events == [
        "lock",
        "mode",
        "budget",
        "verify_pauses",
        "balance",
        "verify_services",
        ("restore_pauses", ACTIVATION_ID),
    ]
    assert len(source.events) == 1
    assert images.events == ["image_verify"]
    assert unit.events == [("unit_verify", ACTIVE_ROOT)]
    assert store.state is UpdateState.VERIFIED


def test_verify_resume_from_verified_revalidates_and_releases_pauses() -> None:
    store = FakeStore(UpdateState.VERIFIED)
    manager, store, core, operations, source, images, unit = _manager(store=store)

    manager.verify()

    assert core.events == []
    assert operations.events == [
        "lock",
        "verify_services",
        ("restore_pauses", ACTIVATION_ID),
    ]
    assert len(source.events) == 1
    assert images.events == ["image_verify"]
    assert unit.events == [("unit_verify", ACTIVE_ROOT)]
    assert store.state is UpdateState.VERIFIED


def test_recover_verify_repairs_unit_before_resuming_rebaseline() -> None:
    store = FakeStore(UpdateState.BLOCKED)
    manager, store, core, operations, source, images, unit = _manager(store=store)

    result = manager.recover_verify()

    assert result == "verified"
    assert core.events == ["core_activate"]
    assert operations.events == [
        "lock",
        "migration_head",
        "recover_verify",
        "verify_services",
    ]
    assert len(source.events) == 2
    assert images.events == ["image_verify", "image_verify"]
    assert unit.events == ["unit_activate", ("unit_verify", ACTIVE_ROOT)]
    assert store.state is UpdateState.VERIFIED


def test_recover_verify_unit_failure_holds_fail_closed_before_state_resume() -> None:
    store = FakeStore(UpdateState.BLOCKED)
    unit = FakeUnitManager(fail_activate=True)
    manager, store, core, operations, _source, _images, unit = _manager(
        store=store,
        unit=unit,
    )

    with pytest.raises(
        PublicBaselineManagerError,
        match="verify recovery blocked",
    ):
        manager.recover_verify()

    assert core.events == ["core_activate"]
    assert operations.events == [
        "lock",
        "migration_head",
        ("hold", ACTIVATION_ID),
    ]
    assert unit.events == ["unit_activate"]
    assert store.state is UpdateState.BLOCKED


def test_verify_state_write_failure_keeps_old_image_tags_for_rollback() -> None:
    class FailingVerifiedStore(FakeStore):
        def transition(
            self,
            expected: UpdateState,
            target: UpdateState,
            *,
            step: str,
            **values: object,
        ) -> None:
            if target is UpdateState.VERIFIED:
                raise RuntimeError("injected verified state write failure")
            super().transition(expected, target, step=step, **values)

    store = FailingVerifiedStore(UpdateState.APPLIED)
    manager, store, core, operations, *_ = _manager(store=store)

    with pytest.raises(VerifyError, match="rolled back"):
        manager.verify()

    assert core.events == ["core_rollback"]
    assert ("cleanup_images", ACTIVATION_ID) not in operations.events
    assert (
        "restore_images_preserved",
        "backend-safe",
        ACTIVATION_ID,
    ) in operations.events
    assert store.state is UpdateState.ROLLED_BACK


def test_verify_unit_drift_uses_baseline_rollback_not_cross_history_checkout() -> None:
    store = FakeStore(UpdateState.APPLIED)
    unit = FakeUnitManager(fail_verify=True)
    manager, store, core, operations, _source, _images, unit = _manager(
        store=store,
        unit=unit,
    )

    with pytest.raises(VerifyError, match="rolled back"):
        manager.verify()

    assert core.events == ["core_rollback"]
    assert unit.events[-1] == "unit_restore"
    assert (
        "restore_images_preserved",
        "backend-safe",
        ACTIVATION_ID,
    ) in operations.events
    assert store.state is UpdateState.ROLLED_BACK


class ImageIdentityRunner:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.commands: list[tuple[str, ...]] = []

    def run(self, command: Any, **_: object) -> bytes:
        argv = tuple(command)
        self.commands.append(argv)
        return f"{self.values[argv[-1]]}\n".encode()


def test_loaded_images_require_version_revision_and_schema_labels() -> None:
    runner = ImageIdentityRunner(
        {
            f"sms-platform-test-api:{TARGET_COMMIT}": (
                f"{API_ID}|amd64|1.6.0|{TARGET_COMMIT}|{SCHEMA_REVISION}"
            ),
            f"sms-platform-test-web:{TARGET_COMMIT}": (
                f"{WEB_ID}|amd64|1.6.0|{TARGET_COMMIT}|{SCHEMA_REVISION}"
            ),
        }
    )
    HostImageInspector(runner).verify(_manifest())  # type: ignore[arg-type]

    assert len(runner.commands) == 2
    assert all(
        command[:4] == ("docker", "image", "inspect", "--format") for command in runner.commands
    )

    runner.values[f"sms-platform-test-api:{TARGET_COMMIT}"] = (
        f"{API_ID}|amd64|1.6.0|{BASE_COMMIT}|{SCHEMA_REVISION}"
    )
    with pytest.raises(PublicBaselineManagerError, match="identity"):
        HostImageInspector(runner).verify(_manifest())  # type: ignore[arg-type]


def test_finalize_requires_verified_and_preserves_core_recovery_contract() -> None:
    manager, store, core, operations, *_ = _manager()
    with pytest.raises(PublicBaselineManagerError, match="verified"):
        manager.finalize()
    assert core.events == []

    store.state = UpdateState.VERIFIED
    manager.finalize()
    assert core.events == ["core_finalize"]
    assert ("restore_pauses", ACTIVATION_ID) in operations.events
    assert ("cleanup_images", ACTIVATION_ID) not in operations.events


@pytest.mark.parametrize("core_state", ["finalizing", "cleaned"])
def test_cleanup_retries_core_cleanup_without_reentering_finalize(
    core_state: str,
) -> None:
    store = FakeStore(UpdateState.VERIFIED)
    core = FakeCore(cleanup_state=core_state)
    manager, _store, core, operations, *_ = _manager(
        store=store,
        core=core,
    )
    artifact_events: list[str] = []
    manager._cleanup_large_artifacts = lambda: artifact_events.append(  # type: ignore[method-assign]
        "cleanup_artifacts"
    )

    manager.cleanup()

    assert core.events == [f"core_cleanup:{core_state}"]
    assert ("restore_pauses", ACTIVATION_ID) in operations.events
    assert operations.events[-1] == (
        "cleanup_images_verified",
        ACTIVATION_ID,
    )
    assert artifact_events == ["cleanup_artifacts"]


def test_cleanup_artifact_requires_exact_digest_and_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incoming = tmp_path / "incoming"
    incoming.mkdir(mode=0o700)
    archive = incoming / "api.tar"
    archive.write_bytes(b"public image archive")
    archive.chmod(0o600)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    monkeypatch.setattr(baseline_module, "INCOMING_ROOT", incoming)
    monkeypatch.setattr(baseline_module, "SYSTEM_GID", os.getgid())

    _unlink_verified_artifact(
        archive,
        expected_sha256=digest,
        expected_uid=os.getuid(),
        maximum_size=1024,
    )
    assert not archive.exists()

    _unlink_verified_artifact(
        archive,
        expected_sha256=digest,
        expected_uid=os.getuid(),
        maximum_size=1024,
    )

    archive.write_bytes(b"drifted")
    archive.chmod(0o600)
    with pytest.raises(PublicBaselineManagerError, match="digest"):
        _unlink_verified_artifact(
            archive,
            expected_sha256=digest,
            expected_uid=os.getuid(),
            maximum_size=1024,
        )
    assert archive.exists()


class UnitRunner:
    def run(self, command: Any, **_: object) -> bytes:
        raise AssertionError(f"unexpected unit command: {tuple(command)}")


def test_unit_preflight_accepts_only_server_base_and_public_target_profiles(
    tmp_path: Path,
) -> None:
    active = tmp_path / "active"
    staged = tmp_path / "staged"
    for root, mode in ((active, 0o640), (staged, 0o644)):
        source = root / "deploy/systemd/vendor-control-agent.service"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"[Service]\nExecStart=/bin/true\n")
        source.chmod(mode)
    installed = tmp_path / "vendor-control-agent.service"
    installed.write_bytes(
        (active / "deploy/systemd/vendor-control-agent.service").read_bytes()
    )
    installed.chmod(0o644)
    manager = HostVendorControlUnitManager(
        expected_uid=os.getuid(),
        expected_operator_gid=os.getgid(),
        expected_system_gid=os.getgid(),
        runner=UnitRunner(),  # type: ignore[arg-type]
        unit_path=installed,
    )

    manager.preflight(active, staged)

    (active / "deploy/systemd/vendor-control-agent.service").chmod(0o644)
    with pytest.raises(PublicBaselineManagerError, match="unsafe"):
        manager.preflight(active, staged)


def test_status_is_read_only_and_contains_only_safe_identity_fields() -> None:
    store = FakeStore(UpdateState.VERIFIED)
    manager, _store, core, operations, *_ = _manager(store=store)

    status = manager.status()

    assert status == {
        "activation_id": ACTIVATION_ID,
        "state": "verified",
        "actual_commit": TARGET_COMMIT,
        "actual_tree": TARGET_TREE,
        "actual_migration_head": SCHEMA_REVISION,
        "target_commit": TARGET_COMMIT,
        "target_tree": TARGET_TREE,
        "operator_git_access": True,
    }
    assert core.events == []
    assert operations.events == ["migration_head"]


def test_status_reports_operator_git_access_failure_without_mutating_state() -> None:
    def fail_operator_git(_root: Path) -> None:
        raise PublicBaselineManagerError("operator Git preflight failed")

    store = FakeStore(UpdateState.VERIFIED)
    manager, _store, core, operations, *_ = _manager(
        store=store,
        operator_git_probe=fail_operator_git,
    )

    status = manager.status()

    assert status["operator_git_access"] is False
    assert store.state is UpdateState.VERIFIED
    assert core.events == []
    assert operations.events == ["migration_head"]
