#!/usr/bin/env python3
"""vendor-live aware 快速更新 apply 控制面。"""

from __future__ import annotations

import contextlib
from typing import Literal, Protocol

from test_update_store import TestUpdateState

BACKEND_SERVICES = (
    "api",
    "worker-realtime",
    "worker-bulk",
    "worker-callback",
    "outbox-dispatcher",
    "beat",
)
UpdateKind = Literal["web-only", "backend-safe"]


class TestUpdateApplyError(RuntimeError):
    """快速更新应用阶段被阻断。"""


class StateStore(Protocol):
    def transition(
        self,
        expected: TestUpdateState,
        target: TestUpdateState,
        *,
        step: str,
        error_type: str | None = None,
        actual_commit: str | None = None,
        actual_migration_head: str | None = None,
    ) -> None: ...

    def block(
        self,
        expected: TestUpdateState,
        *,
        step: str,
        error_type: str = "step_failed",
        actual_commit: str | None = None,
        actual_migration_head: str | None = None,
    ) -> None: ...


class ApplyOperations(Protocol):
    def require_lifecycle_lock(self) -> None: ...

    def validate_vendor_update_mode(self) -> str: ...

    def require_owned_update_pauses(self, update_id: str) -> None: ...

    def run_expand_migration(self, source: str, target: str) -> str: ...

    def replace_backend_services(self, services: tuple[str, ...]) -> None: ...

    def replace_web(self) -> None: ...

    def rollback_no_migration(
        self,
        kind: str,
        update_id: str,
    ) -> tuple[str, str]: ...

    def hold_fail_closed(self, update_id: str) -> None: ...


class TestUpdateApply:
    def __init__(self, store: StateStore, operations: ApplyOperations) -> None:
        self.store = store
        self.operations = operations

    def apply(
        self,
        kind: UpdateKind,
        *,
        update_id: str,
        commit: str,
        migration_from: str,
        migration_target: str,
    ) -> None:
        current = (
            TestUpdateState.PREPARED
            if kind == "web-only" or migration_from == migration_target
            else TestUpdateState.CHECKPOINTED
        )
        step = "lock"
        locked = False
        try:
            self.operations.require_lifecycle_lock()
            locked = True
            if kind == "web-only":
                step = "replace_web"
                self.operations.replace_web()
                self.store.transition(
                    current,
                    TestUpdateState.APPLIED,
                    step="apply_web",
                    actual_commit=commit,
                    actual_migration_head=migration_target,
                )
                return

            step = "environment_mode"
            environment_mode = self.operations.validate_vendor_update_mode()
            if environment_mode not in {"pre-live", "live"}:
                raise TestUpdateApplyError("update environment mode is invalid")
            step = "pauses"
            self.operations.require_owned_update_pauses(update_id)
            actual_head = migration_from
            if migration_from != migration_target:
                step = "migrate"
                actual_head = self.operations.run_expand_migration(
                    migration_from,
                    migration_target,
                )
                if actual_head != migration_target:
                    raise TestUpdateApplyError("migration target was not reached")
                self.store.transition(
                    current,
                    TestUpdateState.MIGRATED,
                    step="migrate",
                    actual_commit=commit,
                    actual_migration_head=actual_head,
                )
                current = TestUpdateState.MIGRATED
            step = "replace_backend"
            self.operations.replace_backend_services(BACKEND_SERVICES)
            self.store.transition(
                current,
                TestUpdateState.APPLIED,
                step="apply_backend",
                actual_commit=commit,
                actual_migration_head=actual_head,
            )
        except Exception:
            rolled_back = False
            if (
                locked
                and migration_from == migration_target
                and step in {"replace_web", "replace_backend"}
            ):
                try:
                    actual_commit, actual_head = (
                        self.operations.rollback_no_migration(kind, update_id)
                    )
                    self.store.transition(
                        current,
                        TestUpdateState.ROLLED_BACK,
                        step="rollback",
                        actual_commit=actual_commit,
                        actual_migration_head=actual_head,
                    )
                    rolled_back = True
                except Exception:
                    rolled_back = False
            if rolled_back:
                raise TestUpdateApplyError(
                    f"test update apply rolled back at {step}"
                ) from None
            if locked:
                with contextlib.suppress(Exception):
                    self.store.block(
                        current,
                        step=step,
                        error_type="step_failed",
                        actual_commit=commit,
                        actual_migration_head=(
                            migration_target
                            if current is TestUpdateState.MIGRATED
                            else migration_from
                        ),
                    )
                if kind == "backend-safe":
                    with contextlib.suppress(Exception):
                        self.operations.hold_fail_closed(update_id)
            raise TestUpdateApplyError(f"test update apply blocked at {step}") from None
