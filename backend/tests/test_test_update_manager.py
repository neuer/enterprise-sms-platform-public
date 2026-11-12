from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))

import test_update_manager as update_manager_module  # noqa: E402
from test_update_apply import BACKEND_SERVICES  # noqa: E402
from test_update_contract import ChangedScope  # noqa: E402
from test_update_manager import (  # noqa: E402
    HostTestUpdateOperations,
    _prepare_from_source,
    _restore_operator_git_read_access,
    _restore_operator_worktree_read_access,
)
from test_update_manager import (  # noqa: E402
    TestUpdateManager as UpdateManager,
)
from test_update_manager import TestUpdateManagerError as ManagerError  # noqa: E402
from test_update_manager import main as manager_main  # noqa: E402
from test_update_store import TestUpdateState as State  # noqa: E402
from test_update_store import TestUpdateStore as UpdateStore  # noqa: E402
from test_update_verify import TestUpdateVerify as UpdateVerify  # noqa: E402
from test_update_verify import TestUpdateVerifyError as VerifyError  # noqa: E402


class FakeStore:
    def __init__(self) -> None:
        self.state = State.PREPARED
        self.events: list[tuple[State, State, str]] = []

    def transition(self, expected: State, target: State, *, step: str, **_: object) -> None:
        assert self.state is expected
        self.events.append((expected, target, step))
        self.state = target

    def block(self, expected: State, *, step: str, **_: object) -> None:
        assert self.state is expected
        self.events.append((expected, State.BLOCKED, step))
        self.state = State.BLOCKED


@pytest.mark.parametrize("rollback", [False, True])
def test_update_manager_preserves_completed_protocol_for_new_update(
    rollback: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import writer_cutover

    from app.services.app_ratelimit_cutover import CUTOVER_MARKER_KEY
    from tests.support.lua_redis import LuaRedis
    from tests.test_app_ratelimit_writer_cutover import _seed_active_marker

    redis = LuaRedis(1_778_202_000)
    _seed_active_marker(redis, generation=7)
    before = redis.hgetall(CUTOVER_MARKER_KEY)
    projected: list[object] = []
    monkeypatch.setattr(writer_cutover, "write_local_marker", projected.append)

    def control(*argv: str) -> str:
        if argv[0] == "HGETALL":
            return "\n".join(item for pair in redis.hgetall(argv[1]).items() for item in pair)
        assert argv[0] == "EVAL"
        result = redis.eval(argv[1], int(argv[2]), *argv[3:])
        return "\n".join(str(item) for item in result)

    def command(*argv: str) -> str:
        assert "base-commit" in " ".join(argv)
        if "ls-tree" in argv:
            return "deploy/writer-protocol.json"
        assert "show" in argv
        return (ROOT / "deploy/writer-protocol.json").read_text(encoding="utf-8")

    def unexpected_command(*_args: object) -> str:
        pytest.fail("compatible update must not stop or reopen writers")

    operations = object.__new__(HostTestUpdateOperations)
    operations.root = ROOT
    operations.request = SimpleNamespace(update_id="U2", base_commit="base-commit")
    operations.host = SimpleNamespace(_redis=control, _run=unexpected_command)
    operations._command = command  # type: ignore[method-assign]
    operations.run_writer_cutover("U2", rollback=rollback)
    assert redis.hgetall(CUTOVER_MARKER_KEY) == before
    assert len(projected) == 1


def test_restore_operator_git_read_access_repairs_checkout_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    git_dir.chmod(0o750)
    objects_dir = git_dir / "objects" / "a0"
    objects_dir.mkdir(parents=True)
    objects_dir.chmod(0o700)
    object_file = objects_dir / ("b" * 38)
    object_file.write_bytes(b"object")
    object_file.chmod(0o600)
    for name in ("HEAD", "index"):
        path = git_dir / name
        path.write_text("metadata", encoding="utf-8")
        path.chmod(0o600)

    monkeypatch.setattr(update_manager_module, "_tracked_worktree_paths", lambda _: [])
    _restore_operator_git_read_access(tmp_path)

    assert stat.S_IMODE(git_dir.stat().st_mode) & stat.S_IRGRP
    assert stat.S_IMODE(git_dir.stat().st_mode) & stat.S_IXGRP
    assert stat.S_IMODE(objects_dir.stat().st_mode) & stat.S_IRGRP
    assert stat.S_IMODE(objects_dir.stat().st_mode) & stat.S_IXGRP
    assert stat.S_IMODE(object_file.stat().st_mode) & stat.S_IRGRP
    for name in ("HEAD", "index"):
        assert stat.S_IMODE((git_dir / name).stat().st_mode) & stat.S_IRGRP


def test_restore_operator_worktree_read_access_repairs_tracked_paths(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "deploy"
    directory.mkdir()
    directory.chmod(0o700)
    script = directory / "sms-compose"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o600)

    _restore_operator_worktree_read_access(
        tmp_path,
        os.getgid(),
        tracked_paths=[directory, script],
    )

    assert stat.S_IMODE(directory.stat().st_mode) & stat.S_IRGRP
    assert stat.S_IMODE(directory.stat().st_mode) & stat.S_IXGRP
    assert stat.S_IMODE(script.stat().st_mode) & stat.S_IRGRP


def test_restore_operator_worktree_read_access_rejects_symlink(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.write_text("target", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(target)

    with pytest.raises(ManagerError, match="tracked worktree"):
        _restore_operator_worktree_read_access(
            tmp_path,
            os.getgid(),
            tracked_paths=[link],
        )


def test_restore_operator_git_read_access_rejects_nested_symlink(
    tmp_path: Path,
) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "objects").mkdir()
    (git_dir / "objects" / "escape").symlink_to(tmp_path)

    with pytest.raises(ManagerError, match="metadata"):
        _restore_operator_git_read_access(tmp_path)


def test_prepare_restores_operator_git_access_when_source_verification_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    git_dir.chmod(0o750)
    index = git_dir / "index"
    index.write_text("metadata", encoding="utf-8")
    index.chmod(0o600)
    monkeypatch.setattr(update_manager_module, "_tracked_worktree_paths", lambda _: [])

    class Operations:
        root = tmp_path

        def verify_source_scope(self) -> ChangedScope:
            raise RuntimeError("source checkout failed")

        def load_and_validate_images(self) -> None:
            raise AssertionError("image loading must not run")

    request = SimpleNamespace(
        update_id="test-permission-recovery",
        base_commit="0" * 40,
        commit="1" * 40,
        migration_from="0015",
        migration_target="0015",
    )
    store = FakeStore()

    with pytest.raises(RuntimeError, match="source checkout failed"):
        _prepare_from_source(store, Operations(), request=request)  # type: ignore[arg-type]

    assert store.state is State.BLOCKED
    assert stat.S_IMODE(index.stat().st_mode) & stat.S_IRGRP


class FakePrepareOperations:
    def __init__(
        self,
        counts: dict[str, int] | None = None,
        *,
        mode: str = "live",
    ) -> None:
        self.events: list[object] = []
        self.counts = counts or {"submitting": 0, "retrying": 0, "uncertain": 0}
        self.mode = mode

    def require_lifecycle_lock(self) -> None:
        self.events.append("lock")

    def validate_vendor_update_mode(self) -> str:
        self.events.append(("mode", self.mode))
        return self.mode

    def pause_lanes_for_update(self, update_id: str) -> None:
        self.events.append(("pause", update_id))

    def finalize_pause_ownership(self, update_id: str) -> None:
        self.events.append(("pause_owner", update_id))

    def unsafe_status_counts(self) -> dict[str, int]:
        self.events.append("counts")
        return self.counts

    def create_encrypted_checkpoint(self, update_id: str) -> str:
        self.events.append(("checkpoint", update_id))
        return f"checkpoint-{update_id}"

    def check_expand_migration(self, migration_from: str, target: str) -> None:
        self.events.append(("migration_check", migration_from, target))

    def hold_fail_closed(self, update_id: str) -> None:
        self.events.append(("hold", update_id))


def _scope(risk: str, components: frozenset[str]) -> ChangedScope:
    return ChangedScope(
        components=components,
        migration_changed=False,
        backend_tests=(),
        frontend_tests=(),
        runtime_changed=True,
        risk=risk,  # type: ignore[arg-type]
        high_risk_paths=(),
    )


def test_backend_prepare_pauses_checks_unsafe_states_and_checkpoints() -> None:
    store = FakeStore()
    operations = FakePrepareOperations()
    update = UpdateManager(store, operations)

    result = update.prepare(
        _scope("backend-safe", frozenset({"api"})),
        update_id="test-1",
        commit="0" * 40,
        migration_from="0015",
        migration_target="0016",
    )

    assert result.kind == "backend-safe"
    assert operations.events == [
        "lock",
        ("mode", "live"),
        ("pause", "test-1"),
        "counts",
        ("checkpoint", "test-1"),
        ("migration_check", "0015", "0016"),
        ("pause_owner", "test-1"),
    ]
    assert store.state is State.CHECKPOINTED


def test_queued_and_scheduled_do_not_block_but_unsafe_chunk_states_do() -> None:
    operations = FakePrepareOperations({"submitting": 0, "retrying": 0, "uncertain": 1})
    store = FakeStore()

    with pytest.raises(ManagerError, match="blocked"):
        UpdateManager(store, operations).prepare(
            _scope("backend-safe", frozenset({"api"})),
            update_id="test-2",
            commit="0" * 40,
            migration_from="0015",
            migration_target="0016",
        )

    assert not any(
        isinstance(event, tuple) and event[0] == "checkpoint"
        for event in operations.events
    )
    assert operations.events[-1] == ("hold", "test-2")
    assert store.state is State.BLOCKED


def test_web_only_prepare_never_touches_vendor_db_or_lanes() -> None:
    store = FakeStore()
    operations = FakePrepareOperations()

    result = UpdateManager(store, operations).prepare(
        _scope("web-only", frozenset({"web"})),
        update_id="test-web",
        commit="0" * 40,
        migration_from="0015",
        migration_target="0015",
    )

    assert result.kind == "web-only"
    assert operations.events == ["lock"]
    assert store.state is State.PREPARED


def test_high_risk_without_migration_skips_database_checkpoint() -> None:
    store = FakeStore()
    operations = FakePrepareOperations()

    result = UpdateManager(store, operations).prepare(
        _scope("high-risk", frozenset({"api"})),
        update_id="test-risk",
        commit="0" * 40,
        migration_from="0015",
        migration_target="0015",
    )

    assert result.kind == "backend-safe"
    assert operations.events == [
        "lock",
        ("mode", "live"),
        ("pause", "test-risk"),
        "counts",
        ("pause_owner", "test-risk"),
    ]
    assert result.checkpoint_id is None
    assert store.state is State.PREPARED


def test_loaded_image_is_bound_to_target_commit_and_migration_labels(
    tmp_path: Path,
) -> None:
    target = "a" * 40
    image_id = f"sha256:{'b' * 64}"
    archive = tmp_path / "incoming/api.tar"
    archive.parent.mkdir()
    archive.write_bytes(b"image archive")
    archive.chmod(0o600)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    image = SimpleNamespace(
        archive_file="api.tar",
        archive_sha256=digest,
        ref=f"sms-platform-test-api:{target}",
        image_id=image_id,
    )
    operations = object.__new__(HostTestUpdateOperations)
    operations.state_root = tmp_path
    operations.expected_uid = os.geteuid()
    operations.request = SimpleNamespace(
        images={"api": image},
        commit=target,
        migration_target="0039_manual_job_outbox",
    )
    calls: list[tuple[str, ...]] = []

    def command(*arguments: str) -> str:
        calls.append(arguments)
        if arguments[1:2] == ("load",):
            return ""
        return (
            f"{image_id}|amd64|{target}|0039_manual_job_outbox"
        )

    operations._command = command  # type: ignore[method-assign]

    operations.load_and_validate_images()

    assert calls[-1][0:3] == ("docker", "image", "inspect")
    assert "org.opencontainers.image.revision" in calls[-1][-2]
    assert "com.sms-platform.schema-revision" in calls[-1][-2]


def test_loaded_image_rejects_revision_or_schema_label_drift(
    tmp_path: Path,
) -> None:
    target = "a" * 40
    image_id = f"sha256:{'b' * 64}"
    archive = tmp_path / "incoming/api.tar"
    archive.parent.mkdir()
    archive.write_bytes(b"image archive")
    archive.chmod(0o600)
    image = SimpleNamespace(
        archive_file="api.tar",
        archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        ref=f"sms-platform-test-api:{target}",
        image_id=image_id,
    )
    operations = object.__new__(HostTestUpdateOperations)
    operations.state_root = tmp_path
    operations.expected_uid = os.geteuid()
    operations.request = SimpleNamespace(
        images={"api": image},
        commit=target,
        migration_target="0039_manual_job_outbox",
    )

    def command(*arguments: str) -> str:
        if arguments[1:2] == ("load",):
            return ""
        return f"{image_id}|amd64|{'c' * 40}|0039_manual_job_outbox"

    operations._command = command  # type: ignore[method-assign]

    with pytest.raises(ManagerError, match="identity"):
        operations.load_and_validate_images()


def test_prepare_diagnostics_reports_only_fixed_safe_booleans(tmp_path: Path) -> None:
    base = "a" * 40
    target = "b" * 40
    image_id = f"sha256:{'c' * 64}"
    archive = tmp_path / "incoming/api.tar"
    archive.parent.mkdir()
    archive.write_bytes(b"image archive")
    archive.chmod(0o600)
    image = SimpleNamespace(
        archive_file="api.tar",
        archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        ref=f"sms-platform-test-api:{target}",
        image_id=image_id,
    )
    operations = object.__new__(HostTestUpdateOperations)
    operations.root = Path("/opt/sms-platform")
    operations.state_root = tmp_path
    operations.expected_uid = os.geteuid()
    operations.host_source_commit = target
    operations.request = SimpleNamespace(
        images={"api": image},
        base_commit=base,
        commit=target,
        migration_target="0082_outbox_realtime_report_queue",
    )

    def command(*arguments: str) -> str:
        if arguments[-2:] == ("status", "--porcelain"):
            return ""
        if arguments[-2:] == ("rev-parse", "HEAD"):
            return base
        if arguments[-2:] == ("rev-parse", f"{target}^{{commit}}"):
            return target
        if "diff" in arguments:
            return ""
        if arguments[0:3] == ("docker", "image", "inspect"):
            return (
                f"{image_id}|amd64|{target}|"
                "0082_outbox_realtime_report_queue"
            )
        raise AssertionError(arguments)

    operations._command = command  # type: ignore[method-assign]

    assert operations.prepare_diagnostics() == {
        "archives_safe": True,
        "base_commit_matches": True,
        "host_control_snapshot_matches": True,
        "images_loaded": True,
        "target_commit_available": True,
        "tracked_checkout_clean": True,
    }


def test_prepare_diagnostics_fails_closed_without_exposing_errors(tmp_path: Path) -> None:
    operations = object.__new__(HostTestUpdateOperations)
    operations.root = Path("/opt/sms-platform")
    operations.state_root = tmp_path
    operations.expected_uid = os.geteuid()
    operations.host_source_commit = "c" * 40
    operations.request = SimpleNamespace(
        images={
            "api": SimpleNamespace(
                archive_file="missing.tar",
                archive_sha256="d" * 64,
                ref="sms-platform-test-api:missing",
                image_id=f"sha256:{'e' * 64}",
            )
        },
        base_commit="a" * 40,
        commit="b" * 40,
        migration_target="0082_outbox_realtime_report_queue",
    )
    operations._command = (  # type: ignore[method-assign]
        lambda *arguments: (_ for _ in ()).throw(ManagerError(str(arguments)))
    )

    assert operations.prepare_diagnostics() == {
        "archives_safe": False,
        "base_commit_matches": False,
        "host_control_snapshot_matches": False,
        "images_loaded": False,
        "target_commit_available": False,
        "tracked_checkout_clean": False,
    }


def test_pre_live_high_risk_without_migration_skips_checkpoint() -> None:
    store = FakeStore()
    operations = FakePrepareOperations(mode="pre-live")

    result = UpdateManager(store, operations).prepare(
        _scope("high-risk", frozenset({"api", "web"})),
        update_id="test-pre-live",
        commit="0" * 40,
        migration_from="0015",
        migration_target="0015",
    )

    assert result.kind == "backend-safe"
    assert operations.events == [
        "lock",
        ("mode", "pre-live"),
        ("pause", "test-pre-live"),
        "counts",
        ("pause_owner", "test-pre-live"),
    ]
    assert result.checkpoint_id is None
    assert store.state is State.PREPARED


def test_backend_safe_without_migration_does_not_wait_for_uncertain_chunks() -> None:
    store = FakeStore()
    operations = FakePrepareOperations(
        {"submitting": 0, "retrying": 0, "uncertain": 3}
    )

    result = UpdateManager(store, operations).prepare(
        _scope("backend-safe", frozenset({"api"})),
        update_id="test-ordinary",
        commit="0" * 40,
        migration_from="0015",
        migration_target="0015",
    )

    assert result.checkpoint_id is None
    assert store.state is State.PREPARED
    assert ("pause_owner", "test-ordinary") in operations.events


def test_capability_reports_high_risk_support_without_reading_request(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = manager_main(
        [
            "capability",
            "--root",
            "/opt/sms-platform",
            "--runtime-root",
            "/run/sms-platform/secrets",
            "--state-root",
            "/var/lib/sms-platform/test-updates",
            "--request",
            "/var/lib/sms-platform/test-updates/incoming/request.json",
            "--marker-file",
            "/etc/sms-platform/test-environment",
        ]
    )

    assert result == 0
    assert capsys.readouterr().out.strip() == (
        '{"host_control_snapshot":true,"schema_version":2}'
    )


def test_bootstrap_capability_binds_exact_host_source_commit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SMS_HOST_SOURCE_COMMIT", "a" * 40)

    result = manager_main(
        [
            "capability",
            "--root",
            "/opt/sms-platform",
            "--runtime-root",
            "/run/sms-platform/secrets",
            "--state-root",
            "/var/lib/sms-platform/test-updates",
            "--request",
            "/var/lib/sms-platform/test-updates/incoming/request.json",
            "--marker-file",
            "/etc/sms-platform/test-environment",
        ]
    )

    assert result == 0
    assert capsys.readouterr().out.strip() == (
        '{"host_control_snapshot":true,"schema_version":2,'
        f'"source_commit":"{"a" * 40}"'
        "}"
    )


def _stored_request(update_id: str, *, commit: str = "a" * 40) -> str:
    return (
        json.dumps(
            {
                "schema_version": 1,
                "update_id": update_id,
                "base_commit": "0" * 40,
                "commit": commit,
                "source_ref": "origin/fix-forward",
                "environment_mode": "live",
                "components": ["api"],
                "images": {
                    "api": {
                        "ref": f"sms-platform-test-api:{commit}",
                        "id": f"sha256:{'1' * 64}",
                        "archive_file": "api.tar",
                        "archive_sha256": "2" * 64,
                    }
                },
                "migration": {
                    "from": "0015",
                    "target": "0015",
                    "compatibility": "none",
                },
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )


def test_host_pause_takeover_requires_blocked_predecessor_and_is_deferred(
    tmp_path: Path,
) -> None:
    old_id = "test-20260719T010101Z-aaaaaaaaaaaa"
    new_id = "test-20260719T020202Z-bbbbbbbbbbbb"
    old = UpdateStore(tmp_path, old_id)
    old.create(_stored_request(old_id))
    old.block(State.PREPARED, step="verify", error_type="invariant_failed")
    calls: list[tuple[str, ...]] = []

    class Redis:
        def _redis(self, *argv: str) -> str:
            calls.append(argv)
            return (
                f"test-update:{old_id}"
                if len(calls) == 1
                else "1"
            )

    operations = object.__new__(HostTestUpdateOperations)
    operations.state_root = tmp_path
    operations.request = SimpleNamespace(update_id=new_id)
    operations.pause_value = f"test-update:{new_id}"
    operations.pending_pause_owner = None
    operations.host = Redis()  # type: ignore[assignment]

    operations.pause_lanes_for_update(new_id)

    assert operations.pending_pause_owner == f"test-update:{old_id}"
    assert len(calls) == 1

    operations.finalize_pause_ownership(new_id)

    assert len(calls) == 2
    assert calls[1][-2:] == (
        f"test-update:{old_id}",
        f"test-update:{new_id}",
    )


def test_host_pause_takeover_rejects_nonblocked_or_untracked_pause(
    tmp_path: Path,
) -> None:
    old_id = "test-20260719T010101Z-aaaaaaaaaaaa"
    new_id = "test-20260719T020202Z-bbbbbbbbbbbb"
    old = UpdateStore(tmp_path, old_id)
    old.create(_stored_request(old_id))

    class Redis:
        def _redis(self, *argv: str) -> str:
            del argv
            return f"test-update:{old_id}"

    operations = object.__new__(HostTestUpdateOperations)
    operations.state_root = tmp_path
    operations.request = SimpleNamespace(update_id=new_id)
    operations.pause_value = f"test-update:{new_id}"
    operations.pending_pause_owner = None
    operations.host = Redis()  # type: ignore[assignment]

    with pytest.raises(ManagerError, match="blocked predecessor"):
        operations.pause_lanes_for_update(new_id)


def test_failed_update_pause_is_taken_over_without_an_unpaused_gap() -> None:
    class SharedPause:
        value: str | None = None
        blocked: set[str] = set()
        history: list[str | None] = []

        def set(self, value: str | None) -> None:
            self.value = value
            self.history.append(value)

    shared = SharedPause()

    class RepairStore(FakeStore):
        def __init__(self, update_id: str) -> None:
            super().__init__()
            self.update_id = update_id

        def block(self, expected: State, *, step: str, **kwargs: object) -> None:
            super().block(expected, step=step, **kwargs)
            shared.blocked.add(self.update_id)

    class RepairPrepare(FakePrepareOperations):
        def __init__(self, update_id: str) -> None:
            super().__init__()
            self.update_id = update_id
            self.predecessor: str | None = None

        def pause_lanes_for_update(self, update_id: str) -> None:
            assert update_id == self.update_id
            if shared.value is None:
                shared.set(update_id)
            elif shared.value != update_id:
                assert shared.value in shared.blocked
                self.predecessor = shared.value
            super().pause_lanes_for_update(update_id)

        def finalize_pause_ownership(self, update_id: str) -> None:
            assert shared.value in {update_id, self.predecessor}
            if shared.value != update_id:
                shared.set(update_id)
            super().finalize_pause_ownership(update_id)

    class RepairVerify:
        def __init__(self, update_id: str, *, fail: bool) -> None:
            self.update_id = update_id
            self.fail = fail

        def require_lifecycle_lock(self) -> None:
            pass

        def validate_vendor_update_mode(self) -> str:
            return "live"

        def verify_budget_conservation(self) -> None:
            pass

        def verify_pause_state(self) -> None:
            assert shared.value == self.update_id

        def probe_balance(self) -> None:
            if self.fail:
                raise RuntimeError("injected")

        def verify_backend_services(self) -> None:
            pass

        def restore_owned_update_pauses(self, update_id: str) -> None:
            assert update_id == self.update_id
            assert shared.value == update_id
            shared.set(None)

        def rollback_no_migration(
            self,
            kind: str,
            update_id: str,
        ) -> tuple[str, str]:
            del kind, update_id
            raise AssertionError("migration updates must not roll back")

        def cleanup_rollback_images(self, update_id: str) -> None:
            del update_id

        def hold_fail_closed(self, update_id: str) -> None:
            assert update_id == self.update_id
            assert shared.value == update_id

        def verify_web(self) -> None:
            raise AssertionError("backend repair must not use web-only verification")

    old_id = "test-old"
    old_store = RepairStore(old_id)
    UpdateManager(old_store, RepairPrepare(old_id)).prepare(
        _scope("backend-safe", frozenset({"api"})),
        update_id=old_id,
        commit="0" * 40,
        migration_from="0015",
        migration_target="0015",
    )
    old_store.state = State.APPLIED
    with pytest.raises(VerifyError, match="blocked"):
        UpdateVerify(old_store, RepairVerify(old_id, fail=True)).verify(
            "backend-safe",
            update_id=old_id,
            commit="0" * 40,
            migration_from="0014",
            migration_target="0015",
        )
    assert shared.value == old_id
    assert old_store.state is State.BLOCKED

    new_id = "test-fix"
    new_store = RepairStore(new_id)
    UpdateManager(new_store, RepairPrepare(new_id)).prepare(
        _scope("backend-safe", frozenset({"api"})),
        update_id=new_id,
        commit="1" * 40,
        migration_from="0015",
        migration_target="0015",
    )
    assert shared.value == new_id
    new_store.state = State.APPLIED
    UpdateVerify(new_store, RepairVerify(new_id, fail=False)).verify(
        "backend-safe",
        update_id=new_id,
        commit="1" * 40,
        migration_from="0015",
        migration_target="0015",
    )

    assert new_store.state is State.VERIFIED
    assert shared.history == [old_id, new_id, None]


def test_host_budget_verification_queries_the_canonical_vendor_ledger() -> None:
    statements: list[str] = []

    class Host:
        def _psql(self, statement: str) -> str:
            statements.append(statement)
            return "0"

    operations = object.__new__(HostTestUpdateOperations)
    operations.host = Host()  # type: ignore[assignment]

    operations.verify_budget_conservation()

    assert len(statements) == 1
    assert "FROM vendor_test_daily_usage" in statements[0]
    assert "FROM live_test_daily_usage" not in statements[0]


def test_host_no_migration_rollback_restores_old_images_without_data_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    redis_calls: list[tuple[str, ...]] = []
    old_api = "sha256:" + "a" * 64
    old_web = "sha256:" + "b" * 64
    update_id = "test-20260728T120000Z-cccccccccccc"
    base_commit = "d" * 40

    class Host:
        def _psql(self, statement: str) -> str:
            assert "alembic_version" in statement
            return "0028"

        def _run(self, *argv: str) -> str:
            calls.append(argv)
            if argv == ("ps", "--status", "running", "--services"):
                return "\n".join(
                    (
                        "api",
                        "worker-realtime",
                        "worker-report",
                        "worker-bulk",
                        "worker-callback",
                        "outbox-dispatcher",
                        "beat",
                        "web",
                    )
                )
            return ""

        def _redis(self, *argv: str) -> str:
            redis_calls.append(argv)
            return "1"

        def stop_senders(self) -> None:
            calls.append(("stop_senders",))

    operations = object.__new__(HostTestUpdateOperations)
    operations.root = tmp_path
    operations.expected_uid = os.geteuid()
    operations.request = SimpleNamespace(
        update_id=update_id,
        base_commit=base_commit,
        migration_from="0028",
        migration_target="0028",
        components=frozenset({"api", "web"}),
    )
    operations.pause_value = f"test-update:{update_id}"
    operations.host = Host()  # type: ignore[assignment]
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    git_dir.chmod(0o750)
    for name in ("HEAD", "index"):
        metadata = git_dir / name
        metadata.write_text("metadata", encoding="utf-8")
        metadata.chmod(0o600)
    monkeypatch.setattr(update_manager_module, "_tracked_worktree_paths", lambda _: [])
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "SMS_API_IMAGE=sha256:" + "1" * 64 + "\n"
        "SMS_WEB_IMAGE=sha256:" + "2" * 64 + "\n",
        encoding="utf-8",
    )
    dotenv.chmod(0o600)

    def command(*argv: str) -> str:
        calls.append(argv)
        joined = " ".join(argv)
        if "image inspect" in joined and "rollback-api" in joined:
            return old_api
        if "image inspect" in joined and "rollback-web" in joined:
            return old_web
        return ""

    operations._command = command  # type: ignore[method-assign]
    operations.run_writer_cutover = (  # type: ignore[method-assign]
        lambda update_id, rollback=False: calls.append(("writer_cutover", update_id, rollback))
    )

    actual = operations.rollback_no_migration("backend-safe", update_id)

    assert actual == (base_commit, "0028")
    assert f"SMS_API_IMAGE={old_api}" in dotenv.read_text(encoding="utf-8")
    assert f"SMS_WEB_IMAGE={old_web}" in dotenv.read_text(encoding="utf-8")
    assert ("git", "-C", str(tmp_path), "checkout", "--detach", base_commit) in calls
    assert not any(
        token in {"down", "-v", "volume", "alembic", "migrate"}
        for call in calls
        for token in call
    )
    assert len(redis_calls) == 4
    assert ("stop_senders",) in calls


def test_host_rollback_refuses_to_cross_a_migration(tmp_path: Path) -> None:
    class Host:
        def _psql(self, statement: str) -> str:
            assert "alembic_version" in statement
            return "0029"

    operations = object.__new__(HostTestUpdateOperations)
    operations.root = tmp_path
    operations.request = SimpleNamespace(
        update_id="test-20260728T120000Z-cccccccccccc",
        migration_from="0028",
    )
    operations.host = Host()  # type: ignore[assignment]

    with pytest.raises(ManagerError, match="cannot cross a migration"):
        operations.rollback_no_migration(
            "backend-safe",
            "test-20260728T120000Z-cccccccccccc",
        )


def test_checkpoint_contract_matches_existing_vendor_and_server_config() -> None:
    import test_update_manager as update_module
    import vendor_test_manager as vendor_module

    example = json.loads(
        (ROOT / "deploy/test-update-backup.server.example.json").read_text(
            encoding="utf-8"
        )
    )
    expected_key = Path("/etc/sms-platform/test-update-backup-key")
    expected_output = Path("/var/lib/sms-platform/test-backups")
    dotenv = (ROOT / "deploy/.env.example").read_text(encoding="utf-8")

    assert update_module._DATABASE == vendor_module._DATABASE == "sms"
    assert (
        update_module._BACKUP_OUTPUT_ROOT
        == vendor_module._BACKUP_OUTPUT_ROOT
        == expected_output
    )
    assert (
        update_module._BACKUP_KEY_FILE
        == vendor_module._BACKUP_KEY_FILE
        == expected_key
    )
    assert example == {
        "schema_version": 1,
        "output_root": str(expected_output),
        "key_file": str(expected_key),
        "database": "sms",
    }
    assert "POSTGRES_DB=sms" in dotenv


def test_host_control_managers_use_authenticated_control_redis(
    tmp_path: Path,
) -> None:
    import test_update_manager as update_module
    import vendor_test_manager as vendor_module

    class Runner:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def run(
            self,
            command: list[str] | tuple[str, ...],
            **_kwargs: object,
        ) -> bytes:
            self.calls.append(list(command))
            return b"OK\n"

    runner = Runner()
    update_host = update_module.HostUpdateOperations(
        root=tmp_path,
        runtime_root=tmp_path / "runtime",
        backup_config_file=tmp_path / "backup.json",
        expected_uid=os.geteuid(),
        runner=runner,  # type: ignore[arg-type]
    )
    vendor_host = vendor_module.HostActivationOperations(
        root=tmp_path,
        runtime_root=tmp_path / "runtime",
        backup_config_file=tmp_path / "backup.json",
        expected_uid=os.geteuid(),
        runner=runner,  # type: ignore[arg-type]
    )

    assert update_host._redis("GET", "queue:paused:realtime") == "OK"
    assert vendor_host._redis("SET", "queue:paused:bulk", "manual", "NX") == "OK"
    for call in runner.calls:
        redis_index = call.index("redis-control")
        assert call[redis_index : redis_index + 3] == ["redis-control", "sh", "-ec"]
        script = call[redis_index + 3]
        assert "--user sms_control --askpass --raw" in script
        assert "/run/secrets/redis_control_password" in script
        assert "manual" not in script


def test_host_update_balance_probe_uses_ephemeral_authorized_worker() -> None:
    import test_update_manager as update_module

    calls: list[tuple[str, ...]] = []
    host = object.__new__(update_module.HostUpdateOperations)

    def run(*args: str, **_kwargs: object) -> str:
        calls.append(args)
        return ""

    host._run = run  # type: ignore[method-assign]
    host.probe_balance()

    assert calls[0][:7] == (
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "worker-realtime",
        "python",
        "-c",
    )
    assert "get_balance" in calls[0][7]
    assert "get_report" not in calls[0][7]
    assert "get_reply" not in calls[0][7]


def test_replace_backend_renders_trusted_proxy_before_web_up() -> None:
    events: list[object] = []

    class Host:
        def _run(self, *arguments: str) -> str:
            events.append(arguments)
            return ""

    operations = object.__new__(HostTestUpdateOperations)
    operations.request = SimpleNamespace(  # type: ignore[assignment]
        components=frozenset({"api", "web"}),
    )
    operations.host = Host()  # type: ignore[assignment]
    operations._prepare_rollback_images = lambda components: events.append(  # type: ignore[method-assign]
        ("rollback", components)
    )
    operations._activate_source_and_image = lambda component: events.append(  # type: ignore[method-assign]
        ("activate", component)
    )
    operations._render_trusted_proxy_conf = lambda: events.append(  # type: ignore[method-assign]
        "render"
    )

    operations.replace_backend_services(BACKEND_SERVICES)

    assert events == [
        ("rollback", frozenset({"api", "web"})),
        ("activate", "api"),
        (
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            *BACKEND_SERVICES,
        ),
        ("activate", "web"),
        "render",
        (
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            "120",
            "web",
        ),
    ]


def test_replace_redis_services_pins_image_before_cutover_and_skips_later_replace() -> None:
    events: list[object] = []

    class Host:
        def _run(self, *arguments: str) -> str:
            events.append(arguments)
            return ""

    operations = object.__new__(HostTestUpdateOperations)
    operations.request = SimpleNamespace(  # type: ignore[assignment]
        components=frozenset({"api", "redis"}),
    )
    operations.host = Host()  # type: ignore[assignment]
    operations._redis_services_replaced = False
    operations._prepare_rollback_images = lambda components: events.append(  # type: ignore[method-assign]
        ("rollback", components)
    )
    operations._activate_source_and_image = lambda component: events.append(  # type: ignore[method-assign]
        ("activate", component)
    )

    operations.replace_redis_services()
    operations.replace_backend_services(BACKEND_SERVICES)

    assert events == [
        ("rollback", frozenset({"redis"})),
        ("activate", "redis"),
        (
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            "120",
            "redis",
            "redis-auth",
            "redis-control",
        ),
        ("rollback", frozenset({"api", "redis"})),
        ("activate", "api"),
        (
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            *BACKEND_SERVICES,
        ),
    ]


def test_replace_redis_services_noops_without_redis_component() -> None:
    events: list[object] = []

    class Host:
        def _run(self, *arguments: str) -> str:
            events.append(arguments)
            return ""

    operations = object.__new__(HostTestUpdateOperations)
    operations.request = SimpleNamespace(  # type: ignore[assignment]
        components=frozenset({"api"}),
    )
    operations.host = Host()  # type: ignore[assignment]
    operations._redis_services_replaced = False
    operations._prepare_rollback_images = lambda components: events.append(  # type: ignore[method-assign]
        ("rollback", components)
    )
    operations._activate_source_and_image = lambda component: events.append(  # type: ignore[method-assign]
        ("activate", component)
    )

    operations.replace_redis_services()

    assert events == []


def test_replace_backend_activates_redis_component_and_recreates_domains() -> None:
    events: list[object] = []

    class Host:
        def _run(self, *arguments: str) -> str:
            events.append(arguments)
            return ""

    operations = object.__new__(HostTestUpdateOperations)
    operations.request = SimpleNamespace(  # type: ignore[assignment]
        components=frozenset({"redis"}),
    )
    operations.host = Host()  # type: ignore[assignment]
    operations._prepare_rollback_images = lambda components: events.append(  # type: ignore[method-assign]
        ("rollback", components)
    )
    operations._activate_source_and_image = lambda component: events.append(  # type: ignore[method-assign]
        ("activate", component)
    )

    operations.replace_backend_services(BACKEND_SERVICES)

    assert events == [
        ("rollback", frozenset({"redis"})),
        ("activate", "redis"),
        (
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            "120",
            "redis",
            "redis-auth",
            "redis-control",
        ),
    ]


def test_render_trusted_proxy_conf_uses_dotenv_values(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    class Runner:
        def run(
            self,
            command: list[str] | tuple[str, ...],
            **_kwargs: object,
        ) -> bytes:
            calls.append(list(command))
            return b""

    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "SMS_EXTERNAL_TLS_MODE=1\n"
        "SMS_TRUSTED_PROXY_CIDRS=203.0.113.9/32\n"
        "SMS_TRUSTED_PROXY_CONF=/tmp/trusted.conf\n",
        encoding="utf-8",
    )
    operations = object.__new__(HostTestUpdateOperations)
    operations.root = tmp_path  # type: ignore[assignment]
    operations.host = SimpleNamespace(runner=Runner())  # type: ignore[assignment]

    operations._render_trusted_proxy_conf()

    assert calls == [
        [
            "/usr/bin/python3",
            str(tmp_path / "deploy/scripts/render_trusted_proxy_conf.py"),
            "--mode",
            "1",
            "--cidrs",
            "203.0.113.9/32",
            "--output",
            "/tmp/trusted.conf",
        ]
    ]


def test_render_trusted_proxy_conf_requires_explicit_mode(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    class Runner:
        def run(
            self,
            command: list[str] | tuple[str, ...],
            **_kwargs: object,
        ) -> bytes:
            calls.append(list(command))
            return b""

    dotenv = tmp_path / ".env"
    dotenv.write_text("ENVIRONMENT=development\n", encoding="utf-8")
    operations = object.__new__(HostTestUpdateOperations)
    operations.root = tmp_path  # type: ignore[assignment]
    operations.host = SimpleNamespace(runner=Runner())  # type: ignore[assignment]

    with pytest.raises(ManagerError, match="mode must be explicit"):
        operations._render_trusted_proxy_conf()

    assert calls == []


def test_rollback_image_tags_are_idempotent_across_apply_resume() -> None:
    existing_api = f"sha256:{'1' * 64}"
    old_web = f"sha256:{'2' * 64}"
    calls: list[tuple[str, ...]] = []

    class Host:
        def _run(self, *arguments: str) -> str:
            calls.append(arguments)
            assert arguments == ("ps", "--all", "-q", "web")
            return "abcdef123456"

    operations = object.__new__(HostTestUpdateOperations)
    operations.request = SimpleNamespace(
        update_id="test-20260731T120000Z-aaaaaaaaaaaa"
    )
    operations.host = Host()  # type: ignore[assignment]

    def command(*arguments: str) -> str:
        calls.append(arguments)
        joined = " ".join(arguments)
        if "image ls" in joined and "rollback-api" in joined:
            return existing_api
        if "image ls" in joined and "rollback-web" in joined:
            return ""
        if arguments[-2:] == ("{{.Image}}", "abcdef123456"):
            return old_web
        return ""

    operations._command = command  # type: ignore[method-assign]

    operations._prepare_rollback_images(frozenset({"api", "web"}))

    assert not any(
        call[:3] == ("docker", "image", "tag") and call[-1].endswith("rollback-api")
        for call in calls
    )
    assert (
        "docker",
        "image",
        "tag",
        old_web,
        "sms-platform-test-rollback-web:"
        "test-20260731T120000Z-aaaaaaaaaaaa",
    ) in calls


def test_initial_rollback_image_binding_rejects_stale_existing_tag() -> None:
    operations = object.__new__(HostTestUpdateOperations)
    operations.request = SimpleNamespace(
        update_id="test-20260731T120000Z-aaaaaaaaaaaa",
        components=frozenset({"api"}),
    )
    operations.host = SimpleNamespace(_run=lambda *args: "abcdef123456")

    def command(*arguments: str) -> str:
        if arguments[1:3] == ("image", "ls"):
            return f"sha256:{'1' * 64}"
        if arguments[-2:] == ("{{.Image}}", "abcdef123456"):
            return f"sha256:{'2' * 64}"
        raise AssertionError(arguments)

    operations._command = command  # type: ignore[method-assign]

    with pytest.raises(ManagerError, match="drifted"):
        operations.prepare_rollback_images()


def test_live_update_mode_uses_postgres_recipient_truth_not_host_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import test_update_manager as module

    operations = object.__new__(HostTestUpdateOperations)
    operations.marker_file = Path("/etc/sms-platform/test-environment")
    operations.expected_uid = 0
    operations.request = SimpleNamespace(environment_mode="live")
    operations.host = SimpleNamespace(active_recipient_count=lambda: 1)
    monkeypatch.setattr(module, "require_test_host_marker", lambda **_kwargs: None)
    monkeypatch.setattr(
        operations,
        "_activation_marker_exists",
        lambda: True,
    )
    monkeypatch.setattr(
        module,
        "read_vendor_test_marker",
        lambda *_args, **_kwargs: SimpleNamespace(mode="development-vendor-live"),
    )

    assert operations.validate_vendor_update_mode() == "live"
    source = (ROOT / "deploy/scripts/test_update_manager.py").read_text(
        encoding="utf-8"
    )
    assert "read_vendor_test_allowlist_count" not in source


def test_pre_live_update_mode_reconciles_legacy_pure_mock_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import test_update_manager as module

    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "ENVIRONMENT=development\n"
        "DEBUG=1   # development\n"
        "AUTH_MOCK=1   # local auth\n"
        "VENDOR_MOCK=1   # mock only\n"
        "VENDOR_BASE_URL=http://mock-vendor:9028   # mock endpoint\n"
        "COMPOSE_PROFILES=dev\n"
        "LDAP_BASE_DN=dc=example,dc=test\n"
        "BOOTSTRAP_ADMIN_USERS=legacy.admin\n",
        encoding="utf-8",
    )
    dotenv.chmod(0o600)
    operations = object.__new__(HostTestUpdateOperations)
    operations.root = tmp_path
    operations.marker_file = tmp_path / "missing-marker"
    operations.expected_uid = os.geteuid()
    operations.request = SimpleNamespace(environment_mode="pre-live")
    monkeypatch.setattr(module, "require_test_host_marker", lambda **_kwargs: None)
    monkeypatch.setattr(operations, "_activation_marker_exists", lambda: False)
    monkeypatch.setattr(
        operations,
        "_read_fresh_control_state",
        lambda: {"mode": "setup_required"},
    )

    assert operations.validate_vendor_update_mode() == "pre-live"
    assert "LDAP_BASE_DN=" not in dotenv.read_text(encoding="utf-8")
    assert "BOOTSTRAP_ADMIN_USERS=" not in dotenv.read_text(encoding="utf-8")
    assert "VENDOR_MOCK=1\n" in dotenv.read_text(encoding="utf-8")


def test_setup_required_projection_allows_leftover_recipients_after_reset() -> None:
    source = (ROOT / "deploy/scripts/test_update_manager.py").read_text(
        encoding="utf-8"
    )
    assert 'document["active_recipient_count"] != 0' not in source
    assert 'document["credential_configured"] is not False' in source
    assert 'document["pause_kind"] is not None' in source


def test_host_source_scope_fetches_requested_branch_and_uses_nul_safe_diff() -> None:
    calls: list[tuple[str, ...]] = []
    base = "a" * 40
    commit = "b" * 40
    source_ref = "origin/codex/web-fast-update-rehearsal"
    update_id = "test-20260718T070301Z-9a008db8c725"
    fetched_ref = f"refs/test-updates/{update_id}/source"
    operations = object.__new__(HostTestUpdateOperations)
    operations.root = Path("/opt/sms-platform")
    operations.request = SimpleNamespace(
        update_id=update_id,
        base_commit=base,
        commit=commit,
        source_ref=source_ref,
        components=frozenset({"web"}),
    )  # type: ignore[assignment]

    def command(*argv: str) -> str:
        calls.append(argv)
        if argv[-2:] == ("status", "--porcelain"):
            return ""
        if argv[-2:] == ("rev-parse", "HEAD"):
            return base
        if argv[-5:] == (
            "fetch",
            "--prune",
            "--no-tags",
            "origin",
            "+refs/heads/codex/web-fast-update-rehearsal:"
            f"{fetched_ref}",
        ):
            return ""
        if argv[-2:] == ("rev-parse", f"{fetched_ref}^{{commit}}"):
            return commit
        if "merge-base" in argv:
            return base
        if "diff" in argv:
            return "frontend/src/views/DashboardView.vue\0"
        raise AssertionError(argv)

    operations._command = command  # type: ignore[method-assign]

    scope = operations.verify_source_scope()

    assert scope.risk == "web-only"
    status_call = next(call for call in calls if call[-2:] == ("status", "--porcelain"))
    assert status_call[1:3] == ("-c", "status.showUntrackedFiles=no")
    fetch_call = next(call for call in calls if "fetch" in call)
    assert fetch_call[-5:] == (
        "fetch",
        "--prune",
        "--no-tags",
        "origin",
        "+refs/heads/codex/web-fast-update-rehearsal:"
        f"{fetched_ref}",
    )
    diff_call = next(call for call in calls if "diff" in call)
    assert "--no-renames" in diff_call
    assert "-z" in diff_call


def test_host_source_scope_rejects_tracked_checkout_drift_before_fetch() -> None:
    calls: list[tuple[str, ...]] = []
    operations = object.__new__(HostTestUpdateOperations)
    operations.root = Path("/opt/sms-platform")
    operations.request = SimpleNamespace(
        update_id="test-20260901T120000Z-bbbbbbbbbbbb",
        base_commit="a" * 40,
        commit="b" * 40,
        source_ref="origin/main",
        components=frozenset({"web"}),
    )  # type: ignore[assignment]

    def command(*argv: str) -> str:
        calls.append(argv)
        if argv[-2:] == ("status", "--porcelain"):
            assert argv[1:3] == ("-c", "status.showUntrackedFiles=no")
            return " M frontend/src/views/DashboardView.vue"
        raise AssertionError(argv)

    operations._command = command  # type: ignore[method-assign]

    with pytest.raises(ManagerError, match="tracked content must be clean"):
        operations.verify_source_scope()

    assert not any("fetch" in call for call in calls)


def test_host_source_scope_retries_only_the_fixed_fetch_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    sleeps: list[float] = []
    base = "a" * 40
    commit = "b" * 40
    update_id = "test-20260810T091621Z-bbbbbbbbbbbb"
    fetched_ref = f"refs/test-updates/{update_id}/source"
    operations = object.__new__(HostTestUpdateOperations)
    operations.root = Path("/opt/sms-platform")
    operations.request = SimpleNamespace(
        update_id=update_id,
        base_commit=base,
        commit=commit,
        source_ref="origin/main",
        components=frozenset({"web"}),
    )  # type: ignore[assignment]
    fetch_attempts = 0

    def command(*argv: str) -> str:
        nonlocal fetch_attempts
        calls.append(argv)
        if argv[-2:] == ("status", "--porcelain"):
            return ""
        if argv[-2:] == ("rev-parse", "HEAD"):
            return base
        if "fetch" in argv:
            fetch_attempts += 1
            if fetch_attempts < 3:
                raise ManagerError("controlled command failed")
            return ""
        if argv[-2:] == ("rev-parse", f"{fetched_ref}^{{commit}}"):
            return commit
        if "merge-base" in argv:
            return base
        if "diff" in argv:
            return "frontend/src/views/DashboardView.vue\0"
        raise AssertionError(argv)

    operations._command = command  # type: ignore[method-assign]
    monkeypatch.setattr(update_manager_module.time, "sleep", sleeps.append)

    scope = operations.verify_source_scope()

    fetch_calls = [call for call in calls if "fetch" in call]
    assert len(fetch_calls) == 3
    assert len(set(fetch_calls)) == 1
    assert sleeps == [1.0, 2.0]
    assert scope.risk == "web-only"


def test_host_source_scope_fails_closed_after_fetch_retries_are_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    update_id = "test-20260810T091621Z-bbbbbbbbbbbb"
    fetched_ref = f"refs/test-updates/{update_id}/source"
    operations = object.__new__(HostTestUpdateOperations)
    operations.root = Path("/opt/sms-platform")
    operations.request = SimpleNamespace(
        update_id=update_id,
        base_commit="a" * 40,
        commit="b" * 40,
        source_ref="origin/main",
        components=frozenset({"web"}),
    )  # type: ignore[assignment]

    def command(*argv: str) -> str:
        calls.append(argv)
        if argv[-2:] == ("status", "--porcelain"):
            return ""
        if argv[-2:] == ("rev-parse", "HEAD"):
            return "a" * 40
        if "fetch" in argv:
            raise ManagerError("controlled command failed")
        raise AssertionError(argv)

    operations._command = command  # type: ignore[method-assign]
    monkeypatch.setattr(update_manager_module.time, "sleep", lambda _: None)

    with pytest.raises(ManagerError, match="controlled command failed"):
        operations.verify_source_scope()

    assert len([call for call in calls if "fetch" in call]) == 3
    assert not any(
        call[-2:] == ("rev-parse", f"{fetched_ref}^{{commit}}")
        for call in calls
    )


def test_host_source_scope_never_uses_cutover_for_descendant_forbidden_diff() -> None:
    base = "a" * 40
    commit = "b" * 40
    update_id = "test-20260728T063501Z-a06082dbc95c"
    fetched_ref = f"refs/test-updates/{update_id}/source"
    operations = object.__new__(HostTestUpdateOperations)
    operations.root = Path("/opt/sms-platform")
    operations.host_source_commit = commit
    operations.request = SimpleNamespace(
        update_id=update_id,
        base_commit=base,
        commit=commit,
        source_ref="origin/main",
        components=frozenset({"api"}),
    )  # type: ignore[assignment]

    def command(*argv: str) -> str:
        if argv[-2:] == ("status", "--porcelain"):
            return ""
        if argv[-2:] == ("rev-parse", "HEAD"):
            return base
        if argv[-2:] == ("rev-parse", f"{fetched_ref}^{{commit}}"):
            return commit
        if "fetch" in argv:
            return ""
        if "diff" in argv and "--" in argv:
            return ""
        if "merge-base" in argv:
            return base
        if "diff" in argv:
            return "deploy/future-runtime.sh\0"
        raise AssertionError(argv)

    operations._command = command  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="forbidden"):
        operations.verify_source_scope()


def test_host_source_scope_rejects_target_that_changes_immutable_controller() -> None:
    base = "a" * 40
    commit = "b" * 40
    update_id = "test-20260718T070301Z-9a008db8c725"
    fetched_ref = f"refs/test-updates/{update_id}/source"
    operations = object.__new__(HostTestUpdateOperations)
    operations.root = Path("/opt/sms-platform")
    operations.host_source_commit = base
    operations.request = SimpleNamespace(
        update_id=update_id,
        base_commit=base,
        commit=commit,
        source_ref="origin/fix/controller",
        components=frozenset({"api"}),
    )  # type: ignore[assignment]

    def command(*argv: str) -> str:
        if argv[-2:] == ("status", "--porcelain"):
            return ""
        if argv[-2:] == ("rev-parse", "HEAD"):
            return base
        if argv[-2:] == ("rev-parse", f"{fetched_ref}^{{commit}}"):
            return commit
        if "fetch" in argv:
            return ""
        if "diff" in argv and "--" in argv:
            return "deploy/sms-compose\0"
        raise AssertionError(argv)

    operations._command = command  # type: ignore[method-assign]

    with pytest.raises(ManagerError, match="snapshot"):
        operations.verify_source_scope()


def test_host_source_scope_rejects_unrelated_histories() -> None:
    """服务器基线不是目标祖先时失败关闭，不存在跨历史切换路径。"""
    base = "a" * 40
    commit = "b" * 40
    update_id = "test-20260810T070301Z-bbbbbbbbbbbb"
    fetched_ref = f"refs/test-updates/{update_id}/source"
    operations = object.__new__(HostTestUpdateOperations)
    operations.root = Path("/opt/sms-platform")
    operations.request = SimpleNamespace(
        update_id=update_id,
        base_commit=base,
        commit=commit,
        source_ref="origin/main",
        components=frozenset({"api", "web"}),
        migration_from="0135_example",
        migration_target="0136_example",
        migration_compatibility="expand",
    )  # type: ignore[assignment]

    def command(*argv: str) -> str:
        if argv[-2:] == ("status", "--porcelain"):
            return ""
        if argv[-2:] == ("rev-parse", "HEAD"):
            return base
        if "fetch" in argv:
            return ""
        if argv[-2:] == ("rev-parse", f"{fetched_ref}^{{commit}}"):
            return commit
        if "merge-base" in argv:
            raise ManagerError("unrelated histories")
        raise AssertionError(argv)

    operations._command = command  # type: ignore[method-assign]

    with pytest.raises(ManagerError, match="descend from the server baseline"):
        operations.verify_source_scope()
