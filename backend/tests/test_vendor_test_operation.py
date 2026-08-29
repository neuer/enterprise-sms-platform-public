from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core.auth.accounts import SecurityPrincipal

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))

from vendor_control_protocol import ControlResponse  # noqa: E402

NOW = datetime(2026, 7, 17, 9, tzinfo=UTC)
OPERATION_ID = "c0a80101-0000-4000-8000-000000000081"
SENTINEL = "formal-key-private-13800138000-" + "a" * 64
ADMIN = SecurityPrincipal(1, 10, "admin", "平台部", "admin")


def test_uat_biz_id_is_stable_and_within_idempotency_contract() -> None:
    from app.services.vendor_test_operation import uat_biz_id

    value = uat_biz_id(OPERATION_ID)

    assert value == "uat:0101000040008000000000000081"
    assert len(value) == 32


class FakeRepository:
    def __init__(self, record: object | None = None) -> None:
        self.record = record
        self.events: list[tuple[object, ...]] = []
        self.pending_records: list[object] = []

    async def reserve_start(
        self,
        operation_id: str,
        operation_type: str,
        *,
        principal: SecurityPrincipal,
        conflicting_types: frozenset[str],
        batch_no: str | None = None,
        checkpoint_id: str | None = None,
    ):
        from app.services.vendor_test_operation import VendorTestOperationConflict

        if any(
            item.operation_type in conflicting_types
            and item.status in {"requested", "running"}
            and item.operation_id != operation_id
            for item in self.pending_records
        ):
            raise VendorTestOperationConflict("已有联调操作正在执行")
        return await self.request(
            operation_id,
            operation_type,
            principal=principal,
            batch_no=batch_no,
            checkpoint_id=checkpoint_id,
        )

    async def request(
        self,
        operation_id: str,
        operation_type: str,
        *,
        principal: SecurityPrincipal,
        batch_no: str | None = None,
        checkpoint_id: str | None = None,
    ):
        from app.services.vendor_test_operation import VendorTestOperation

        self.events.append(
            ("requested_audit", operation_id, operation_type, principal.login_name)
        )
        if self.record is None:
            self.record = VendorTestOperation(
                operation_id=operation_id,
                operation_type=operation_type,
                actor=principal.login_name,
                status="requested",
                safe_code=None,
                batch_no=batch_no,
                checkpoint_id=checkpoint_id,
                requested_at=NOW,
                completed_at=None,
                actor_account_id=principal.account_id,
                actor_identity_id=principal.identity_id,
            )
        return self.record

    async def mark_running(self, operation_id: str):
        from dataclasses import replace

        assert self.record is not None
        self.events.append(("running", operation_id))
        self.record = replace(self.record, status="running")
        return self.record

    async def claim_control_running(self, operation_id: str):
        if self.record is None or self.record.status != "requested":
            return None
        return await self.mark_running(operation_id)

    async def fail_unclaimed_control(
        self,
        operation_id: str,
        *,
        safe_code: str,
    ):
        if self.record is None or self.record.status != "requested":
            return None
        return await self.complete(
            operation_id,
            status="failed",
            safe_code=safe_code,
        )

    async def complete(
        self,
        operation_id: str,
        *,
        status: str,
        safe_code: str | None,
        checkpoint_id: str | None = None,
        vendor_code: int | None = None,
        batch_no: str | None = None,
    ):
        from dataclasses import replace

        assert self.record is not None
        self.events.append(("completion_audit", operation_id, status, safe_code))
        self.record = replace(
            self.record,
            status=status,
            safe_code=safe_code,
            vendor_code=vendor_code,
            checkpoint_id=checkpoint_id,
            batch_no=batch_no,
            completed_at=NOW,
        )
        return self.record

    async def pending(self):
        return tuple(self.pending_records)

    async def get(self, operation_id: str):
        if self.record is None or self.record.operation_id != operation_id:
            return None
        return self.record


class FakeClient:
    def __init__(
        self,
        response: ControlResponse | Exception | list[ControlResponse | Exception],
    ) -> None:
        self.responses = response if isinstance(response, list) else [response]
        self.events: list[tuple[str, str, dict[str, object]]] = []

    async def request(
        self,
        operation: str,
        *,
        operation_id: str,
        body: dict[str, object],
    ) -> ControlResponse:
        self.events.append((operation, operation_id, body))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeFinalizer:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[object] = []

    async def finalize(self, record: object) -> None:
        self.calls.append(record)
        if self.error is not None:
            raise self.error


def test_only_stale_pending_reset_is_due_for_page_driven_recovery() -> None:
    from dataclasses import replace

    from app.services.vendor_test_operation import (
        VendorTestOperation,
        VendorTestOperationService,
    )

    record = VendorTestOperation(
        operation_id=OPERATION_ID,
        operation_type="reset_configuration",
        actor="admin",
        status="running",
        safe_code=None,
        batch_no=None,
        checkpoint_id=None,
        requested_at=NOW,
        completed_at=None,
    )
    service = VendorTestOperationService(
        FakeRepository(record),
        FakeClient(ControlResponse(OPERATION_ID, "ok", None, {})),
        now=lambda: NOW + timedelta(seconds=61),
    )

    assert service.reset_recovery_due(record) is True
    assert service.reset_recovery_due(
        replace(record, requested_at=NOW + timedelta(seconds=2))
    ) is False
    assert service.reset_recovery_due(replace(record, status="succeeded")) is False
    assert service.reset_recovery_due(
        replace(record, operation_type="activate")
    ) is False


class ConcurrentLifecycleRepository:
    """复现未持有 lifecycle 锁的 request 穿过已开始的冲突扫描。"""

    def __init__(self, *, coordinate_reset_first: bool) -> None:
        self.coordinate_reset_first = coordinate_reset_first
        self.lock = asyncio.Lock()
        self.reset_scan_started = asyncio.Event()
        self.credential_progress = asyncio.Event()
        self.records: dict[str, object] = {}

    async def reserve_start(
        self,
        operation_id: str,
        operation_type: str,
        *,
        principal: SecurityPrincipal,
        conflicting_types: frozenset[str],
        batch_no: str | None = None,
        checkpoint_id: str | None = None,
    ):
        from app.services.vendor_test_operation import VendorTestOperationConflict

        if operation_type in {"install_credentials", "rotate_credentials"}:
            self.credential_progress.set()
        async with self.lock:
            conflicts = [
                item
                for item in self.records.values()
                if item.operation_id != operation_id
                and item.operation_type in conflicting_types
                and item.status in {"requested", "running"}
            ]
            if (
                self.coordinate_reset_first
                and operation_type == "reset_configuration"
            ):
                self.reset_scan_started.set()
                await self.credential_progress.wait()
            if conflicts:
                raise VendorTestOperationConflict("已有联调操作正在执行")
            return await self.request(
                operation_id,
                operation_type,
                principal=principal,
                batch_no=batch_no,
                checkpoint_id=checkpoint_id,
            )

    async def request(
        self,
        operation_id: str,
        operation_type: str,
        *,
        principal: SecurityPrincipal,
        batch_no: str | None = None,
        checkpoint_id: str | None = None,
    ):
        from app.services.vendor_test_operation import VendorTestOperation

        existing = self.records.get(operation_id)
        if existing is not None:
            return existing
        record = VendorTestOperation(
            operation_id=operation_id,
            operation_type=operation_type,
            actor=principal.login_name,
            status="requested",
            safe_code=None,
            batch_no=batch_no,
            checkpoint_id=checkpoint_id,
            requested_at=NOW,
            completed_at=None,
            actor_account_id=principal.account_id,
            actor_identity_id=principal.identity_id,
        )
        self.records[operation_id] = record
        if operation_type in {"install_credentials", "rotate_credentials"}:
            self.credential_progress.set()
        return record

    async def mark_running(self, operation_id: str):
        from dataclasses import replace

        record = self.records[operation_id]
        running = replace(record, status="running")
        self.records[operation_id] = running
        return running

    async def claim_control_running(self, operation_id: str):
        async with self.lock:
            record = self.records[operation_id]
            if record.status != "requested":
                return None
            from dataclasses import replace

            running = replace(record, status="running")
            self.records[operation_id] = running
            return running

    async def complete(
        self,
        operation_id: str,
        *,
        status: str,
        safe_code: str | None,
        checkpoint_id: str | None = None,
        vendor_code: int | None = None,
        batch_no: str | None = None,
    ):
        from dataclasses import replace

        record = self.records[operation_id]
        terminal = replace(
            record,
            status=status,
            safe_code=safe_code,
            checkpoint_id=checkpoint_id,
            vendor_code=vendor_code,
            batch_no=batch_no,
            completed_at=NOW,
        )
        self.records[operation_id] = terminal
        return terminal

    async def get(self, operation_id: str):
        return self.records.get(operation_id)

    async def pending(self):
        return tuple(
            record
            for record in self.records.values()
            if record.status in {"requested", "running"}
        )


class BlockingClient(FakeClient):
    def __init__(self) -> None:
        super().__init__(
            ControlResponse(
                OPERATION_ID,
                "ok",
                None,
                {"operation_status": "succeeded"},
            )
        )
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def request(
        self,
        operation: str,
        *,
        operation_id: str,
        body: dict[str, object],
    ) -> ControlResponse:
        self.entered.set()
        await self.release.wait()
        return await super().request(
            operation,
            operation_id=operation_id,
            body=body,
        )


class ConcurrentExecutionRepository(FakeRepository):
    """用终态 CAS 精确复现同一 control operation 的重复执行竞争。"""

    def __init__(self) -> None:
        super().__init__()
        self.state_lock = asyncio.Lock()
        self.execution_reads = 0
        self.execution_read_barrier = asyncio.Event()

    async def get(self, operation_id: str):
        if self.record is None or self.record.operation_id != operation_id:
            return None
        self.execution_reads += 1
        snapshot = self.record
        if self.execution_reads <= 2:
            if self.execution_reads == 2:
                self.execution_read_barrier.set()
            await self.execution_read_barrier.wait()
        return snapshot

    async def mark_running(self, operation_id: str):
        from dataclasses import replace

        async with self.state_lock:
            assert self.record is not None
            if self.record.status == "requested":
                self.events.append(("running", operation_id))
                self.record = replace(self.record, status="running")
            return self.record

    async def claim_control_running(self, operation_id: str):
        from dataclasses import replace

        async with self.state_lock:
            assert self.record is not None
            if self.record.status != "requested":
                return None
            self.events.append(("running", operation_id))
            self.record = replace(self.record, status="running")
            return self.record

    async def complete(
        self,
        operation_id: str,
        *,
        status: str,
        safe_code: str | None,
        checkpoint_id: str | None = None,
        vendor_code: int | None = None,
        batch_no: str | None = None,
    ):
        from dataclasses import replace

        from app.services.vendor_test_operation import VendorTestOperationConflict

        async with self.state_lock:
            assert self.record is not None
            if self.record.status in {"succeeded", "failed"}:
                if self.record.status != status or self.record.safe_code != safe_code:
                    raise VendorTestOperationConflict("operation 终态冲突")
                return self.record
            self.events.append(("completion_audit", operation_id, status, safe_code))
            self.record = replace(
                self.record,
                status=status,
                safe_code=safe_code,
                vendor_code=vendor_code,
                checkpoint_id=checkpoint_id,
                batch_no=batch_no,
                completed_at=NOW,
            )
            return self.record


class DuplicateExecutionClient:
    """首次调用等待；若出现重复调用，则让未知结果抢先落终态。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []
        self.first_entered = asyncio.Event()
        self.release_first = asyncio.Event()

    async def request(
        self,
        operation: str,
        *,
        operation_id: str,
        body: dict[str, object],
    ) -> ControlResponse:
        self.events.append((operation, operation_id, body))
        if len(self.events) == 1:
            self.first_entered.set()
            await self.release_first.wait()
            return ControlResponse(
                operation_id,
                "ok",
                None,
                {"operation_status": "succeeded"},
            )
        return ControlResponse(
            operation_id,
            "error",
            "CONTROL_RESULT_UNKNOWN",
            {"operation_status": "failed"},
        )


def test_reset_configuration_is_a_control_operation_type() -> None:
    from app.services.vendor_test_operation import CONTROL_OPERATION_TYPES

    assert "reset_configuration" in CONTROL_OPERATION_TYPES


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation_type",
    [
        "install_credentials",
        "rotate_credentials",
        "activate",
        "pause",
        "resume",
        "reset_configuration",
    ],
)
async def test_concurrent_execute_reserved_has_one_agent_caller_and_pending_loser(
    operation_type: str,
) -> None:
    from app.services.vendor_test_operation import (
        VendorTestOperationPending,
        VendorTestOperationService,
    )

    repository = ConcurrentExecutionRepository()
    client = DuplicateExecutionClient()
    service = VendorTestOperationService(repository, client)
    await service.start(
        operation_id=OPERATION_ID,
        operation_type=operation_type,
        principal=ADMIN,
        body={},
    )
    body = {"ciphertext": SENTINEL} if "credentials" in operation_type else {}

    first = asyncio.create_task(
        service.execute_reserved(
            operation_id=OPERATION_ID,
            operation_type=operation_type,
            principal=ADMIN,
            body=body,
        )
    )
    second = asyncio.create_task(
        service.execute_reserved(
            operation_id=OPERATION_ID,
            operation_type=operation_type,
            principal=ADMIN,
            body=body,
        )
    )
    await asyncio.wait_for(client.first_entered.wait(), timeout=1)
    done, _ = await asyncio.wait(
        {first, second},
        timeout=1,
        return_when=asyncio.FIRST_COMPLETED,
    )
    assert len(done) == 1
    client.release_first.set()
    results = await asyncio.wait_for(
        asyncio.gather(first, second, return_exceptions=True),
        timeout=1,
    )

    assert len(client.events) == 1
    assert sum(isinstance(result, VendorTestOperationPending) for result in results) == 1
    succeeded = [result for result in results if not isinstance(result, BaseException)]
    assert len(succeeded) == 1
    assert succeeded[0].status == "succeeded"
    assert repository.record.status == "succeeded"
    assert repository.record.safe_code is None
    assert SENTINEL not in repr(repository.events)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["succeeded", "failed"])
async def test_control_claim_loser_returns_terminal_record_idempotently(
    terminal_status: str,
) -> None:
    from dataclasses import replace

    from app.services.vendor_test_operation import VendorTestOperationService

    repository = FakeRepository()
    await repository.request(OPERATION_ID, "activate", principal=ADMIN)

    async def lose_claim(operation_id: str):
        assert operation_id == OPERATION_ID
        assert repository.record is not None
        repository.record = replace(
            repository.record,
            status=terminal_status,
            safe_code="CONTROL_COMMAND_FAILED" if terminal_status == "failed" else None,
            completed_at=NOW,
        )
        return None

    repository.claim_control_running = lose_claim  # type: ignore[method-assign]
    result = await VendorTestOperationService(
        repository,
        FakeClient(AssertionError("agent must not be called")),
    ).execute_reserved(
        operation_id=OPERATION_ID,
        operation_type="activate",
        principal=ADMIN,
        body={},
    )

    assert result.status == terminal_status


@pytest.mark.asyncio
async def test_control_claim_loser_rejects_unexpected_nonterminal_state() -> None:
    from app.services.vendor_test_operation import (
        VendorTestOperationConflict,
        VendorTestOperationService,
    )

    repository = FakeRepository()
    await repository.request(OPERATION_ID, "activate", principal=ADMIN)

    async def lose_claim(operation_id: str):
        assert operation_id == OPERATION_ID
        return None

    repository.claim_control_running = lose_claim  # type: ignore[method-assign]
    with pytest.raises(VendorTestOperationConflict, match="状态冲突"):
        await VendorTestOperationService(
            repository,
            FakeClient(AssertionError("agent must not be called")),
        ).execute_reserved(
            operation_id=OPERATION_ID,
            operation_type="activate",
            principal=ADMIN,
            body={},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "credential_operation",
    ["install_credentials", "rotate_credentials"],
)
async def test_reset_reservation_blocks_concurrent_credential_operation(
    credential_operation: str,
) -> None:
    from app.services.vendor_test_operation import (
        VendorTestOperationConflict,
        VendorTestOperationService,
    )

    repository = ConcurrentLifecycleRepository(coordinate_reset_first=True)
    client = FakeClient(
        ControlResponse(OPERATION_ID, "ok", None, {"operation_status": "succeeded"})
    )
    service = VendorTestOperationService(repository, client)
    reset_id = "c0a80101-0000-4000-8000-000000000082"

    reset_task = asyncio.create_task(
        service.start(
            operation_id=reset_id,
            operation_type="reset_configuration",
            principal=ADMIN,
            body={},
        )
    )
    await repository.reset_scan_started.wait()
    credential_result = await asyncio.gather(
        service.execute(
            operation_id=OPERATION_ID,
            operation_type=credential_operation,
            principal=ADMIN,
            body={"ciphertext": SENTINEL},
        ),
        return_exceptions=True,
    )
    reset = await reset_task

    assert reset.status == "requested"
    assert isinstance(credential_result[0], VendorTestOperationConflict)
    assert client.events == []
    assert SENTINEL not in repr(repository.records)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "credential_operation",
    ["install_credentials", "rotate_credentials"],
)
async def test_credential_reservation_blocks_concurrent_reset(
    credential_operation: str,
) -> None:
    from app.services.vendor_test_operation import (
        VendorTestOperationConflict,
        VendorTestOperationService,
    )

    repository = ConcurrentLifecycleRepository(coordinate_reset_first=False)
    client = BlockingClient()
    service = VendorTestOperationService(repository, client)
    credential_task = asyncio.create_task(
        service.execute(
            operation_id=OPERATION_ID,
            operation_type=credential_operation,
            principal=ADMIN,
            body={"ciphertext": SENTINEL},
        )
    )
    await client.entered.wait()

    with pytest.raises(VendorTestOperationConflict, match="正在执行"):
        await service.start(
            operation_id="c0a80101-0000-4000-8000-000000000082",
            operation_type="reset_configuration",
            principal=ADMIN,
            body={},
        )

    client.release.set()
    assert (await credential_task).status == "succeeded"
    assert SENTINEL not in repr(repository.records)


def test_api_factory_does_not_register_destructive_reset_finalizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import vendor_test as vendor_test_api

    captured: dict[str, object] = {}

    class CapturingService:
        def __init__(
            self,
            repository: object,
            client: object,
            *,
            finalizers: dict[str, object] | None = None,
        ) -> None:
            captured["finalizers"] = finalizers

    monkeypatch.setattr(vendor_test_api, "get_settings", lambda: "settings")
    monkeypatch.setattr(vendor_test_api, "VendorTestOperationService", CapturingService)

    vendor_test_api.get_vendor_operation_service()

    assert captured == {"finalizers": None}


@pytest.mark.asyncio
async def test_requested_audit_precedes_agent_and_completion_audit_without_payload_leak() -> None:
    from app.services.vendor_test_operation import VendorTestOperationService

    repository = FakeRepository()
    client = FakeClient(
        ControlResponse(OPERATION_ID, "ok", None, {"operation_status": "succeeded"})
    )
    service = VendorTestOperationService(repository, client)
    body = {
        "session_id": "c0a80101-0000-4000-8000-000000000099",
        "wrapped_key": SENTINEL,
        "nonce": "nonce",
        "ciphertext": SENTINEL,
        "aad": "aad",
        "algorithm": "RSA-OAEP-256+A256GCM",
    }

    result = await service.execute(
        operation_id=OPERATION_ID,
        operation_type="install_credentials",
        principal=ADMIN,
        body=body,
    )

    combined = repository.events[:2] + [("agent_call",)] + repository.events[2:]
    assert [event[0] for event in combined] == [
        "requested_audit",
        "running",
        "agent_call",
        "completion_audit",
    ]
    assert result.status == "succeeded"
    assert client.events == [
        (
            "install_credentials",
            OPERATION_ID,
            {**body, "actor": "admin"},
        )
    ]
    assert "actor" not in body
    assert SENTINEL not in repr(repository.events)


@pytest.mark.asyncio
async def test_terminal_operation_id_replay_does_not_call_agent_again() -> None:
    from app.services.vendor_test_operation import (
        VendorTestOperation,
        VendorTestOperationService,
    )

    terminal = VendorTestOperation(
        OPERATION_ID,
        "activate",
        "admin",
        "succeeded",
        None,
        None,
        "checkpoint-1",
        NOW,
        NOW,
        actor_account_id=ADMIN.account_id,
        actor_identity_id=ADMIN.identity_id,
    )
    repository = FakeRepository(terminal)
    client = FakeClient(AssertionError("agent must not be called"))

    replay = await VendorTestOperationService(repository, client).execute(
        operation_id=OPERATION_ID,
        operation_type="activate",
        principal=ADMIN,
        body={},
    )

    assert replay is terminal
    assert client.events == []


@pytest.mark.asyncio
async def test_terminal_reset_replay_does_not_call_agent_or_finalizer_again() -> None:
    from app.services.vendor_test_operation import (
        VendorTestOperation,
        VendorTestOperationService,
    )

    terminal = VendorTestOperation(
        OPERATION_ID,
        "reset_configuration",
        "admin",
        "succeeded",
        None,
        None,
        None,
        NOW,
        NOW,
        actor_account_id=ADMIN.account_id,
        actor_identity_id=ADMIN.identity_id,
    )
    repository = FakeRepository(terminal)
    client = FakeClient(AssertionError("agent must not be called"))
    finalizer = FakeFinalizer(AssertionError("finalizer must not be called"))

    replay = await VendorTestOperationService(
        repository,
        client,
        finalizers={"reset_configuration": finalizer},
    ).execute(
        operation_id=OPERATION_ID,
        operation_type="reset_configuration",
        principal=ADMIN,
        body={},
    )

    assert replay is terminal
    assert client.events == []
    assert finalizer.calls == []


@pytest.mark.asyncio
async def test_agent_transport_interruption_keeps_operation_reconcilable() -> None:
    from app.services.vendor_control_client import ControlAgentUnavailable
    from app.services.vendor_test_operation import (
        VendorTestOperationPending,
        VendorTestOperationService,
    )

    repository = FakeRepository()
    client = FakeClient(ControlAgentUnavailable("控制代理不可用"))

    with pytest.raises(VendorTestOperationPending) as captured:
        await VendorTestOperationService(repository, client).execute(
            operation_id=OPERATION_ID,
            operation_type="activate",
            principal=ADMIN,
            body={},
        )

    assert repository.record.status == "running"
    assert "completion_audit" not in {event[0] for event in repository.events}
    assert SENTINEL not in str(captured.value)


@pytest.mark.asyncio
async def test_start_rejects_a_second_nonterminal_control_mutation() -> None:
    from app.services.vendor_test_operation import (
        VendorTestOperation,
        VendorTestOperationConflict,
        VendorTestOperationService,
    )

    active = VendorTestOperation(
        OPERATION_ID,
        "activate",
        "admin",
        "running",
        None,
        None,
        None,
        NOW,
        None,
    )
    repository = FakeRepository()
    repository.pending_records = [active]
    service = VendorTestOperationService(
        repository,
        FakeClient(AssertionError("agent must not be called")),
    )

    with pytest.raises(VendorTestOperationConflict, match="正在执行"):
        await service.start(
            operation_id="c0a80101-0000-4000-8000-000000000082",
            operation_type="pause",
            principal=ADMIN,
            body={"pause_kind": "manual"},
        )

    assert repository.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_type", ["pause", "uat_send"])
async def test_reset_start_conflicts_with_any_pending_control_or_uat(
    pending_type: str,
) -> None:
    from app.services.vendor_test_operation import (
        VendorTestOperation,
        VendorTestOperationConflict,
        VendorTestOperationService,
    )

    pending = VendorTestOperation(
        OPERATION_ID,
        pending_type,
        "admin",
        "requested",
        None,
        None,
        None,
        NOW,
        None,
    )
    repository = FakeRepository()
    repository.pending_records = [pending]

    with pytest.raises(VendorTestOperationConflict, match="正在执行"):
        await VendorTestOperationService(
            repository,
            FakeClient(AssertionError("agent must not be called")),
        ).start(
            operation_id="c0a80101-0000-4000-8000-000000000082",
            operation_type="reset_configuration",
            principal=ADMIN,
            body={},
        )

    assert repository.events == []


@pytest.mark.asyncio
async def test_reset_success_runs_agent_then_finalizer_before_completion() -> None:
    from app.services.vendor_test_operation import VendorTestOperationService

    repository = FakeRepository()
    client = FakeClient(
        ControlResponse(OPERATION_ID, "ok", None, {"operation_status": "succeeded"})
    )

    class OrderedFinalizer(FakeFinalizer):
        async def finalize(self, record: object) -> None:
            assert client.events == [("reset_configuration", OPERATION_ID, {})]
            assert "completion_audit" not in {
                event[0] for event in repository.events
            }
            await super().finalize(record)

    finalizer = OrderedFinalizer()
    result = await VendorTestOperationService(
        repository,
        client,
        finalizers={"reset_configuration": finalizer},
    ).execute(
        operation_id=OPERATION_ID,
        operation_type="reset_configuration",
        principal=ADMIN,
        body={},
    )

    assert result.status == "succeeded"
    assert len(finalizer.calls) == 1
    assert repository.events[-1] == (
        "completion_audit",
        OPERATION_ID,
        "succeeded",
        None,
    )


@pytest.mark.asyncio
async def test_reset_finalizer_failure_keeps_running_for_reconciliation() -> None:
    from app.services.vendor_test_operation import (
        VendorTestOperationPending,
        VendorTestOperationService,
    )
    from app.services.vendor_test_recipient import RecipientBusy

    repository = FakeRepository()
    finalizer = FakeFinalizer(RecipientBusy(SENTINEL))
    service = VendorTestOperationService(
        repository,
        FakeClient(
            ControlResponse(
                OPERATION_ID,
                "ok",
                None,
                {"operation_status": "succeeded"},
            )
        ),
        finalizers={"reset_configuration": finalizer},
    )

    with pytest.raises(VendorTestOperationPending) as captured:
        await service.execute(
            operation_id=OPERATION_ID,
            operation_type="reset_configuration",
            principal=ADMIN,
            body={},
        )

    assert repository.record.status == "running"
    assert "completion_audit" not in {event[0] for event in repository.events}
    assert len(finalizer.calls) == 1
    assert SENTINEL not in str(captured.value)


@pytest.mark.asyncio
async def test_reset_finalizer_unexpected_failure_propagates_and_keeps_running() -> None:
    from app.services.vendor_test_operation import VendorTestOperationService

    repository = FakeRepository()
    finalizer = FakeFinalizer(RuntimeError(SENTINEL))
    service = VendorTestOperationService(
        repository,
        FakeClient(
            ControlResponse(
                OPERATION_ID,
                "ok",
                None,
                {"operation_status": "succeeded"},
            )
        ),
        finalizers={"reset_configuration": finalizer},
    )

    with pytest.raises(RuntimeError) as captured:
        await service.execute(
            operation_id=OPERATION_ID,
            operation_type="reset_configuration",
            principal=ADMIN,
            body={},
        )

    assert type(captured.value) is RuntimeError
    assert repository.record.status == "running"
    assert "completion_audit" not in {event[0] for event in repository.events}
    assert SENTINEL not in repr(repository.events)


@pytest.mark.asyncio
async def test_agent_failure_never_runs_reset_finalizer() -> None:
    from app.services.vendor_test_operation import VendorTestOperationService

    repository = FakeRepository()
    finalizer = FakeFinalizer(AssertionError("finalizer must not be called"))
    result = await VendorTestOperationService(
        repository,
        FakeClient(
            ControlResponse(
                OPERATION_ID,
                "error",
                "CONTROL_COMMAND_FAILED",
                {"operation_status": "failed"},
            )
        ),
        finalizers={"reset_configuration": finalizer},
    ).execute(
        operation_id=OPERATION_ID,
        operation_type="reset_configuration",
        principal=ADMIN,
        body={},
    )

    assert result.status == "failed"
    assert finalizer.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "safe_code",
    [
        "CONTROL_STATE_SYNC_FAILED",
        "CONTROL_RESULT_UNKNOWN",
        "CONTROL_OPERATION_IN_PROGRESS",
    ],
)
async def test_reset_recoverable_agent_response_keeps_operation_running(
    safe_code: str,
) -> None:
    from app.services.vendor_test_operation import (
        VendorTestOperationPending,
        VendorTestOperationService,
    )

    repository = FakeRepository()
    finalizer = FakeFinalizer(AssertionError("finalizer must not be called"))

    with pytest.raises(VendorTestOperationPending):
        await VendorTestOperationService(
            repository,
            FakeClient(
                ControlResponse(
                    OPERATION_ID,
                    "error",
                    safe_code,
                    {"operation_status": "running"},
                )
            ),
            finalizers={"reset_configuration": finalizer},
        ).execute(
            operation_id=OPERATION_ID,
            operation_type="reset_configuration",
            principal=ADMIN,
            body={},
        )

    assert repository.record.status == "running"
    assert "completion_audit" not in {event[0] for event in repository.events}
    assert finalizer.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation_type",
    [
        "install_credentials",
        "rotate_credentials",
        "activate",
        "pause",
        "resume",
    ],
)
async def test_ordinary_control_in_progress_keeps_operation_running(
    operation_type: str,
) -> None:
    from app.services.vendor_test_operation import (
        VendorTestOperationPending,
        VendorTestOperationService,
    )

    repository = FakeRepository()

    with pytest.raises(VendorTestOperationPending):
        await VendorTestOperationService(
            repository,
            FakeClient(
                ControlResponse(
                    OPERATION_ID,
                    "error",
                    "CONTROL_OPERATION_IN_PROGRESS",
                    {},
                )
            ),
        ).execute(
            operation_id=OPERATION_ID,
            operation_type=operation_type,
            principal=ADMIN,
            body={},
        )

    assert repository.record.status == "running"
    assert "completion_audit" not in {event[0] for event in repository.events}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation_type", "body"),
    [
        ("activate", {"operation_status": "running"}),
        ("reset_configuration", {"operation_status": "failed"}),
    ],
)
async def test_recoverable_code_is_not_pending_without_strict_reset_running_shape(
    operation_type: str,
    body: dict[str, object],
) -> None:
    from app.services.vendor_test_operation import VendorTestOperationService

    repository = FakeRepository()
    result = await VendorTestOperationService(
        repository,
        FakeClient(
            ControlResponse(
                OPERATION_ID,
                "error",
                "CONTROL_STATE_SYNC_FAILED",
                body,
            )
        ),
    ).execute(
        operation_id=OPERATION_ID,
        operation_type=operation_type,
        principal=ADMIN,
        body={},
    )

    assert result.status == "failed"
    assert result.safe_code == "CONTROL_STATE_SYNC_FAILED"


@pytest.mark.asyncio
async def test_finalizer_cancellation_is_not_converted_to_pending() -> None:
    from app.services.vendor_test_operation import VendorTestOperationService

    repository = FakeRepository()
    finalizer = FakeFinalizer(asyncio.CancelledError())
    service = VendorTestOperationService(
        repository,
        FakeClient(
            ControlResponse(
                OPERATION_ID,
                "ok",
                None,
                {"operation_status": "succeeded"},
            )
        ),
        finalizers={"reset_configuration": finalizer},
    )

    with pytest.raises(asyncio.CancelledError):
        await service.execute(
            operation_id=OPERATION_ID,
            operation_type="reset_configuration",
            principal=ADMIN,
            body={},
        )

    assert repository.record.status == "running"
    assert "completion_audit" not in {event[0] for event in repository.events}


@pytest.mark.asyncio
async def test_vendor_error_persists_only_integer_code_without_vendor_message() -> None:
    from app.services.vendor_test_operation import VendorTestOperationService

    repository = FakeRepository()
    client = FakeClient(
        ControlResponse(
            OPERATION_ID,
            "error",
            "VENDOR_ERROR",
            {"vendor_code": 1010},
        )
    )

    result = await VendorTestOperationService(repository, client).execute(
        operation_id=OPERATION_ID,
        operation_type="activate",
        principal=ADMIN,
        body={},
    )

    assert result.safe_code == "VENDOR_ERROR"
    assert result.vendor_code == 1010
    assert "ip check failed from carrier" not in repr(repository.events).casefold()


@pytest.mark.asyncio
async def test_reconcile_queries_agent_journal_and_only_completes_terminal_records() -> None:
    from app.services.vendor_test_operation import (
        VendorTestOperation,
        VendorTestOperationService,
    )

    pending = VendorTestOperation(
        OPERATION_ID,
        "pause",
        "admin",
        "running",
        None,
        None,
        None,
        NOW,
        None,
    )
    repository = FakeRepository(pending)
    repository.pending_records = [pending]
    client = FakeClient(
        ControlResponse(
            OPERATION_ID,
            "error",
            "CONTROL_COMMAND_FAILED",
            {"operation_status": "failed"},
        )
    )

    reconciled = await VendorTestOperationService(repository, client).reconcile_once()

    assert reconciled == 1
    assert client.events == [("status", OPERATION_ID, {})]
    assert repository.record.status == "failed"
    assert repository.record.safe_code == "CONTROL_COMMAND_FAILED"


@pytest.mark.asyncio
async def test_reconcile_agent_success_reruns_finalizer_before_completion() -> None:
    from app.services.vendor_test_operation import (
        VendorTestOperation,
        VendorTestOperationService,
    )

    pending = VendorTestOperation(
        OPERATION_ID,
        "reset_configuration",
        "admin",
        "running",
        None,
        None,
        None,
        NOW,
        None,
    )
    repository = FakeRepository(pending)
    repository.pending_records = [pending]
    client = FakeClient(
        ControlResponse(
            OPERATION_ID,
            "ok",
            None,
            {"operation_status": "succeeded"},
        )
    )

    class ReconcileFinalizer(FakeFinalizer):
        async def finalize(self, record: object) -> None:
            assert client.events == [("status", OPERATION_ID, {})]
            assert "completion_audit" not in {
                event[0] for event in repository.events
            }
            await super().finalize(record)

    finalizer = ReconcileFinalizer()
    reconciled = await VendorTestOperationService(
        repository,
        client,
        finalizers={"reset_configuration": finalizer},
    ).reconcile_once()

    assert reconciled == 1
    assert len(finalizer.calls) == 1
    assert repository.record.status == "succeeded"


@pytest.mark.asyncio
async def test_reconcile_replays_running_reset_with_same_operation_id() -> None:
    from app.services.vendor_test_operation import (
        VendorTestOperation,
        VendorTestOperationService,
    )

    pending = VendorTestOperation(
        OPERATION_ID,
        "reset_configuration",
        "admin",
        "running",
        None,
        None,
        None,
        NOW,
        None,
    )
    repository = FakeRepository(pending)
    repository.pending_records = [pending]
    client = FakeClient(
        [
            ControlResponse(
                OPERATION_ID,
                "error",
                "CONTROL_OPERATION_IN_PROGRESS",
                {"operation_status": "running"},
            ),
            ControlResponse(
                OPERATION_ID,
                "ok",
                None,
                {"operation_status": "succeeded"},
            ),
        ]
    )
    finalizer = FakeFinalizer()

    reconciled = await VendorTestOperationService(
        repository,
        client,
        finalizers={"reset_configuration": finalizer},
    ).reconcile_once()

    assert reconciled == 1
    assert client.events == [
        ("status", OPERATION_ID, {}),
        ("reset_configuration", OPERATION_ID, {}),
    ]
    assert len(finalizer.calls) == 1
    assert repository.record.status == "succeeded"


@pytest.mark.asyncio
async def test_reconcile_persistent_reset_recovery_failure_stays_running() -> None:
    from app.services.vendor_test_operation import (
        VendorTestOperation,
        VendorTestOperationService,
    )

    pending = VendorTestOperation(
        OPERATION_ID,
        "reset_configuration",
        "admin",
        "running",
        None,
        None,
        None,
        NOW,
        None,
    )
    repository = FakeRepository(pending)
    repository.pending_records = [pending]
    client = FakeClient(
        [
            ControlResponse(
                OPERATION_ID,
                "error",
                "CONTROL_OPERATION_IN_PROGRESS",
                {"operation_status": "running"},
            ),
            ControlResponse(
                OPERATION_ID,
                "error",
                "CONTROL_STATE_SYNC_FAILED",
                {"operation_status": "running"},
            ),
        ]
    )
    finalizer = FakeFinalizer(AssertionError("finalizer must not be called"))

    reconciled = await VendorTestOperationService(
        repository,
        client,
        finalizers={"reset_configuration": finalizer},
    ).reconcile_once()

    assert reconciled == 0
    assert repository.record.status == "running"
    assert "completion_audit" not in {event[0] for event in repository.events}
    assert finalizer.calls == []


@pytest.mark.asyncio
async def test_reconcile_finalizer_failure_keeps_operation_running() -> None:
    from app.services.vendor_test_operation import (
        VendorTestOperation,
        VendorTestOperationService,
    )
    from app.services.vendor_test_recipient import RecipientBusy

    pending = VendorTestOperation(
        OPERATION_ID,
        "reset_configuration",
        "admin",
        "running",
        None,
        None,
        None,
        NOW,
        None,
    )
    repository = FakeRepository(pending)
    repository.pending_records = [pending]
    finalizer = FakeFinalizer(RecipientBusy(SENTINEL))
    reconciled = await VendorTestOperationService(
        repository,
        FakeClient(
            ControlResponse(
                OPERATION_ID,
                "ok",
                None,
                {"operation_status": "succeeded"},
            )
        ),
        finalizers={"reset_configuration": finalizer},
    ).reconcile_once()

    assert reconciled == 0
    assert repository.record.status == "running"
    assert "completion_audit" not in {event[0] for event in repository.events}


@pytest.mark.asyncio
async def test_reconcile_unexpected_finalizer_failure_propagates() -> None:
    from app.services.vendor_test_operation import (
        VendorTestOperation,
        VendorTestOperationService,
    )

    pending = VendorTestOperation(
        OPERATION_ID,
        "reset_configuration",
        "admin",
        "running",
        None,
        None,
        None,
        NOW,
        None,
    )
    repository = FakeRepository(pending)
    repository.pending_records = [pending]
    finalizer = FakeFinalizer(RuntimeError(SENTINEL))

    with pytest.raises(RuntimeError):
        await VendorTestOperationService(
            repository,
            FakeClient(
                ControlResponse(
                    OPERATION_ID,
                    "ok",
                    None,
                    {"operation_status": "succeeded"},
                )
            ),
            finalizers={"reset_configuration": finalizer},
        ).reconcile_once()

    assert repository.record.status == "running"
    assert "completion_audit" not in {event[0] for event in repository.events}
    assert SENTINEL not in repr(repository.events)


@pytest.mark.asyncio
async def test_reconcile_reset_journal_failure_never_runs_finalizer() -> None:
    from app.services.vendor_test_operation import (
        VendorTestOperation,
        VendorTestOperationService,
    )

    pending = VendorTestOperation(
        OPERATION_ID,
        "reset_configuration",
        "admin",
        "running",
        None,
        None,
        None,
        NOW,
        None,
    )
    repository = FakeRepository(pending)
    repository.pending_records = [pending]
    finalizer = FakeFinalizer(AssertionError("finalizer must not be called"))

    reconciled = await VendorTestOperationService(
        repository,
        FakeClient(
            ControlResponse(
                OPERATION_ID,
                "error",
                "CONTROL_COMMAND_FAILED",
                {"operation_status": "failed"},
            )
        ),
        finalizers={"reset_configuration": finalizer},
    ).reconcile_once()

    assert reconciled == 1
    assert repository.record.status == "failed"
    assert finalizer.calls == []


@pytest.mark.asyncio
async def test_reconcile_gives_fresh_requested_control_operation_time_to_start() -> None:
    from app.services.vendor_test_operation import (
        VendorTestOperation,
        VendorTestOperationService,
    )

    pending = VendorTestOperation(
        OPERATION_ID,
        "pause",
        "admin",
        "requested",
        None,
        None,
        None,
        NOW,
        None,
    )
    repository = FakeRepository(pending)
    repository.pending_records = [pending]
    client = FakeClient(
        ControlResponse(
            OPERATION_ID,
            "error",
            "OPERATION_NOT_FOUND",
            {"operation_status": "not_found"},
        )
    )

    reconciled = await VendorTestOperationService(
        repository,
        client,
        now=lambda: NOW + timedelta(seconds=59),
    ).reconcile_once()

    assert reconciled == 0
    assert client.events == []
    assert repository.record.status == "requested"


@pytest.mark.asyncio
async def test_reconcile_boundedly_fails_stale_requested_control_operation() -> None:
    from app.services.vendor_test_operation import (
        VendorTestOperation,
        VendorTestOperationService,
    )

    pending = VendorTestOperation(
        OPERATION_ID,
        "pause",
        "admin",
        "requested",
        None,
        None,
        None,
        NOW,
        None,
    )
    repository = FakeRepository(pending)
    repository.pending_records = [pending]
    client = FakeClient(
        ControlResponse(
            OPERATION_ID,
            "error",
            "OPERATION_NOT_FOUND",
            {"operation_status": "not_found"},
        )
    )

    reconciled = await VendorTestOperationService(
        repository,
        client,
        now=lambda: NOW + timedelta(seconds=60),
    ).reconcile_once()

    assert reconciled == 1
    assert client.events == [("status", OPERATION_ID, {})]
    assert repository.record.status == "failed"
    assert repository.record.safe_code == "CONTROL_OPERATION_NOT_FOUND"


@pytest.mark.asyncio
async def test_reconcile_cannot_fail_control_claimed_after_requested_snapshot() -> None:
    from app.services.vendor_test_operation import (
        VendorTestOperation,
        VendorTestOperationService,
    )

    pending = VendorTestOperation(
        OPERATION_ID,
        "reset_configuration",
        "admin",
        "requested",
        None,
        None,
        None,
        NOW,
        None,
    )
    repository = FakeRepository(pending)
    repository.pending_records = [pending]
    await repository.claim_control_running(OPERATION_ID)
    client = FakeClient(
        ControlResponse(
            OPERATION_ID,
            "error",
            "OPERATION_NOT_FOUND",
            {"operation_status": "not_found"},
        )
    )

    reconciled = await VendorTestOperationService(
        repository,
        client,
        now=lambda: NOW + timedelta(seconds=60),
    ).reconcile_once()

    assert reconciled == 0
    assert repository.record.status == "running"
    assert "completion_audit" not in {event[0] for event in repository.events}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_code", "expected_status", "expected_code", "expected_reconciled"),
    [
        ("OPERATION_NOT_FOUND", "running", None, 0),
        ("CONTROL_RESULT_UNKNOWN", "failed", "CONTROL_RESULT_UNKNOWN", 1),
    ],
)
async def test_reconcile_boundedly_fails_missing_or_interrupted_agent_operation(
    agent_code: str,
    expected_status: str,
    expected_code: str | None,
    expected_reconciled: int,
) -> None:
    from app.services.vendor_test_operation import (
        VendorTestOperation,
        VendorTestOperationService,
    )

    pending = VendorTestOperation(
        OPERATION_ID,
        "activate",
        "admin",
        "running",
        None,
        None,
        None,
        NOW,
        None,
    )
    repository = FakeRepository(pending)
    repository.pending_records = [pending]
    client = FakeClient(
        ControlResponse(
            OPERATION_ID,
            "error",
            agent_code,
            {"operation_status": "not_found" if agent_code == "OPERATION_NOT_FOUND" else "failed"},
        )
    )

    reconciled = await VendorTestOperationService(repository, client).reconcile_once()

    assert reconciled == expected_reconciled
    assert repository.record.status == expected_status
    assert repository.record.safe_code == expected_code


@pytest.mark.asyncio
async def test_reconcile_restores_checkpoint_and_integer_vendor_code() -> None:
    from app.services.vendor_test_operation import (
        VendorTestOperation,
        VendorTestOperationService,
    )

    pending = VendorTestOperation(
        OPERATION_ID,
        "activate",
        "admin",
        "running",
        None,
        None,
        None,
        NOW,
        None,
    )
    repository = FakeRepository(pending)
    repository.pending_records = [pending]
    client = FakeClient(
        ControlResponse(
            OPERATION_ID,
            "error",
            "VENDOR_ERROR",
            {"operation_status": "failed", "vendor_code": 1010},
        )
    )

    await VendorTestOperationService(repository, client).reconcile_once()

    assert repository.record.vendor_code == 1010

    success = VendorTestOperation(
        OPERATION_ID,
        "activate",
        "admin",
        "running",
        None,
        None,
        None,
        NOW,
        None,
    )
    repository = FakeRepository(success)
    repository.pending_records = [success]
    client = FakeClient(
        ControlResponse(
            OPERATION_ID,
            "ok",
            None,
            {
                "operation_status": "succeeded",
                "checkpoint_id": "activation-20260717T090000Z",
            },
        )
    )

    await VendorTestOperationService(repository, client).reconcile_once()

    assert repository.record.checkpoint_id == "activation-20260717T090000Z"
