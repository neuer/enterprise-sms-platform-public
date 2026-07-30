#!/usr/bin/env python3
"""vendor-live aware 快速更新 verify 控制面。"""

from __future__ import annotations

import contextlib
from typing import Protocol

from test_update_apply import UpdateKind
from test_update_store import TestUpdateState


class TestUpdateVerifyError(RuntimeError):
    """快速更新验证阶段被阻断。"""


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


class VerifyOperations(Protocol):
    def require_lifecycle_lock(self) -> None: ...

    def verify_web(self) -> None: ...

    def validate_vendor_update_mode(self) -> str: ...

    def verify_budget_conservation(self) -> None: ...

    def verify_pause_state(self) -> None: ...

    def probe_balance(self) -> None: ...

    def verify_backend_services(self) -> None: ...

    def restore_owned_update_pauses(self, update_id: str) -> None: ...

    def rollback_no_migration(
        self,
        kind: str,
        update_id: str,
    ) -> tuple[str, str]: ...

    def cleanup_rollback_images(self, update_id: str) -> None: ...

    def hold_fail_closed(self, update_id: str) -> None: ...


class TestUpdateVerify:
    def __init__(self, store: StateStore, operations: VerifyOperations) -> None:
        self.store = store
        self.operations = operations

    def verify(
        self,
        kind: UpdateKind,
        *,
        update_id: str,
        commit: str,
        migration_from: str,
        migration_target: str,
    ) -> None:
        step = "lock"
        locked = False
        try:
            self.operations.require_lifecycle_lock()
            locked = True
            if kind == "web-only":
                step = "verify_web"
                self.operations.verify_web()
            else:
                step = "environment_mode"
                environment_mode = self.operations.validate_vendor_update_mode()
                if environment_mode not in {"pre-live", "live"}:
                    raise TestUpdateVerifyError("update environment mode is invalid")
                step = "budget"
                self.operations.verify_budget_conservation()
                step = "pauses"
                self.operations.verify_pause_state()
                if environment_mode == "live":
                    step = "get_balance"
                    self.operations.probe_balance()
                step = "services"
                self.operations.verify_backend_services()
                step = "restore_owned_pauses"
                self.operations.restore_owned_update_pauses(update_id)
            self.operations.cleanup_rollback_images(update_id)
            self.store.transition(
                TestUpdateState.APPLIED,
                TestUpdateState.VERIFIED,
                step="verify",
                actual_commit=commit,
                actual_migration_head=migration_target,
            )
        except Exception:
            rolled_back = False
            if locked and migration_from == migration_target:
                try:
                    actual_commit, actual_head = (
                        self.operations.rollback_no_migration(kind, update_id)
                    )
                    self.store.transition(
                        TestUpdateState.APPLIED,
                        TestUpdateState.ROLLED_BACK,
                        step="rollback",
                        actual_commit=actual_commit,
                        actual_migration_head=actual_head,
                    )
                    rolled_back = True
                except Exception:
                    rolled_back = False
            if rolled_back:
                raise TestUpdateVerifyError(
                    f"test update verify rolled back at {step}"
                ) from None
            if locked:
                with contextlib.suppress(Exception):
                    self.store.block(
                        TestUpdateState.APPLIED,
                        step=step,
                        error_type="invariant_failed",
                        actual_commit=commit,
                        actual_migration_head=migration_target,
                    )
                if kind == "backend-safe":
                    with contextlib.suppress(Exception):
                        self.operations.hold_fail_closed(update_id)
            raise TestUpdateVerifyError(
                f"test update verify blocked at {step}"
            ) from None
