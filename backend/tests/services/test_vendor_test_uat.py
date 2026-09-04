from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.apikey import ApiAppContext
from app.core.auth.accounts import SecurityPrincipal
from app.services.pipeline import BatchResponse, SendRequest
from app.services.vendor_test_operation import VendorTestOperation
from app.services.vendor_test_recipient import (
    RecipientBusy,
    RecipientHmacIndexStale,
    RecipientNotFound,
    VendorTestRecipientForSend,
)
from app.services.vendor_test_uat import VendorTestAppUnavailable

NOW = datetime(2026, 7, 17, 9, tzinfo=UTC)
OPERATION_ID = "c0a80101-0000-4000-8000-000000000121"
PHONE_ENC = b"ciphertext-only"
PHONE_HMAC = "a" * 64
HISTORICAL_PHONE_HMAC = "b" * 64
ADMIN = SecurityPrincipal(1, 10, "admin", "平台部", "admin")


def test_stale_recipient_index_projects_only_safe_failure_code() -> None:
    from app.services.vendor_test_uat import VendorTestUatService

    assert (
        VendorTestUatService._safe_failure(RecipientHmacIndexStale("internal"))
        == "RECIPIENT_INDEX_STALE"
    )


def operation(status: str, *, batch_no: str | None = None) -> VendorTestOperation:
    return VendorTestOperation(
        OPERATION_ID,
        "uat_send",
        "admin",
        status,
        None,
        batch_no,
        None,
        NOW,
        NOW if status in {"succeeded", "failed"} else None,
        actor_account_id=ADMIN.account_id,
        actor_identity_id=ADMIN.identity_id,
    )


class FakeOperations:
    def __init__(
        self,
        *,
        pending: tuple[VendorTestOperation, ...] = (),
        result: object | None = None,
        expire_empty: bool = False,
    ) -> None:
        self.pending_values = pending
        self.record = operation("requested")
        self.result = result
        self.expire_empty = expire_empty
        self.heartbeat_ticks: asyncio.Queue[bool] = asyncio.Queue()
        self.events: list[tuple[object, ...]] = []

    async def pending(self) -> tuple[VendorTestOperation, ...]:
        return self.pending_values

    async def reserve_start(
        self,
        operation_id: str,
        operation_type: str,
        *,
        principal: SecurityPrincipal,
        conflicting_types: frozenset[str],
    ) -> VendorTestOperation:
        from app.services.vendor_test_operation import VendorTestOperationConflict

        if any(
            item.operation_type in conflicting_types
            and item.status in {"requested", "running"}
            and item.operation_id != operation_id
            for item in self.pending_values
        ):
            raise VendorTestOperationConflict("已有联调操作正在执行")
        return await self.request(
            operation_id,
            operation_type,
            principal=principal,
        )

    async def request(
        self,
        operation_id: str,
        operation_type: str,
        *,
        principal: SecurityPrincipal,
        batch_no: str | None = None,
        checkpoint_id: str | None = None,
    ) -> VendorTestOperation:
        self.events.append(("request", operation_id, operation_type, principal.login_name))
        self.record = operation("requested")
        return self.record

    async def mark_running(self, operation_id: str) -> VendorTestOperation:
        self.events.append(("running", operation_id))
        self.record = operation("running", batch_no=self.record.batch_no)
        return self.record

    async def claim_uat_running(
        self,
        operation_id: str,
    ) -> VendorTestOperation | None:
        return await self.mark_running(operation_id)

    async def attach_batch(
        self,
        operation_id: str,
        *,
        batch_no: str,
    ) -> VendorTestOperation:
        self.events.append(("attach_batch", operation_id, batch_no))
        self.record = operation("running", batch_no=batch_no)
        return self.record

    @asynccontextmanager
    async def acceptance_guard(
        self,
        operation_id: str,
    ) -> AsyncIterator[None]:
        self.events.append(("acceptance_guard_enter", operation_id))
        try:
            yield
        finally:
            self.events.append(("acceptance_guard_exit", operation_id))

    async def heartbeat(self, operation_id: str) -> bool:
        self.events.append(("heartbeat", operation_id))
        if self.heartbeat_ticks.empty():
            return True
        return await self.heartbeat_ticks.get()

    async def prepare_uat_acceptance(self, operation_id: str) -> bool:
        self.events.append(("prepare_uat_acceptance", operation_id))
        return True

    async def expire_uat_if_stale(
        self,
        operation_id: str,
        *,
        safe_code: str,
    ) -> VendorTestOperation | None:
        self.events.append(("expire_uat_if_stale", operation_id, safe_code))
        if not self.expire_empty:
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
    ) -> VendorTestOperation:
        self.events.append(("complete", operation_id, status, safe_code, batch_no))
        self.record = VendorTestOperation(
            OPERATION_ID,
            "uat_send",
            "admin",
            status,
            safe_code,
            batch_no,
            checkpoint_id,
            NOW,
            NOW,
            vendor_code,
        )
        return self.record

    async def get(self, operation_id: str) -> VendorTestOperation | None:
        assert operation_id == OPERATION_ID
        return self.record

    async def uat_result(
        self,
        operation_id: str,
        *,
        batch_no: str | None,
    ) -> object | None:
        assert operation_id == OPERATION_ID
        assert batch_no in {None, "batch-uat"}
        return self.result


class FakeRecipients:
    def __init__(self, *, missing: bool = False) -> None:
        self.missing = missing
        self.resolved: list[int] = []

    async def resolve_for_send(self, recipient_id: int) -> VendorTestRecipientForSend:
        self.resolved.append(recipient_id)
        if self.missing:
            raise RecipientNotFound("disabled")
        return VendorTestRecipientForSend(
            recipient_id,
            PHONE_ENC,
            PHONE_HMAC,
            "139****0001",
            2,
            ((1, HISTORICAL_PHONE_HMAC), (2, PHONE_HMAC)),
        )


class FakeApps:
    def __init__(self, *, status: int = 1) -> None:
        self.status = status

    async def get(self, app_id: int) -> dict[str, Any] | None:
        assert app_id == 7
        return {
            "id": 7,
            "name": "uat-app",
            "dept": "平台部",
            "allowed_categories": ["verify", "notice", "market"],
            "default_sign": "测试签名",
            "daily_quota": 1000,
            "blacklist_check": True,
            "freq_override": None,
            "rate_limit_per_min": 60,
            "status": self.status,
        }


class FakePreviewConfig:
    def __init__(self) -> None:
        self.depts: list[str] = []

    async def load_config(self, dept: str) -> dict[str, str]:
        self.depts.append(dept)
        return {
            "unsubscribe_suffix": "退订回N",
            "unsubscribe_auto_append": "true",
            "verify_otp_mask": "true",
            "approval_threshold": "10",
            "market_approval_threshold": "5",
        }


class FakePreviewRenderer:
    def __init__(self) -> None:
        self.calls: list[tuple[int, tuple[str, ...], str]] = []

    async def render(self, template_id: int, params: tuple[str, ...], dept: str) -> str:
        self.calls.append((template_id, params, dept))
        return f"模板内容{params[0]}"


class FakePreviewSigns:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def resolve(self, name: str) -> str:
        self.calls.append(name)
        return f"【{name}】"


class FakePipeline:
    def __init__(self) -> None:
        self.calls: list[tuple[ApiAppContext, SendRequest]] = []

    async def accept(self, app: ApiAppContext, request: SendRequest) -> BatchResponse:
        self.calls.append((app, request))
        return BatchResponse(
            "batch-uat", False, 1, 0, 0, 0, 1, 1, "queued", None, None
        )


def test_uat_biz_id_is_deterministic_collision_resistant_and_within_contract() -> None:
    from app.services import vendor_test_operation as operation_module
    from app.services.idempotency import IdempotencyCoordinator, IdempotencyScope

    helper = getattr(operation_module, "vendor_test_uat_biz_id", None)

    assert callable(helper), "缺少集中式 UAT biz_id helper"
    first = helper(OPERATION_ID)
    assert first == helper(OPERATION_ID)
    assert first != helper("c0a80101-0000-4000-8000-000000000122")
    assert first.startswith("vuat:")
    assert len(first) == 27
    assert len(first) <= 32
    assert IdempotencyCoordinator.key(IdempotencyScope("app", "7"), first) == (
        f"idem:app:7:{first}"
    )


def service(
    *,
    operations: FakeOperations | None = None,
    recipients: FakeRecipients | None = None,
    apps: FakeApps | None = None,
) -> tuple[Any, FakeOperations, FakeRecipients, FakePipeline]:
    from app.services.vendor_test_uat import VendorTestUatService

    operation_store = operations or FakeOperations()
    recipient_store = recipients or FakeRecipients()
    pipeline = FakePipeline()

    @asynccontextmanager
    async def pipeline_factory(_app: ApiAppContext) -> AsyncIterator[FakePipeline]:
        yield pipeline

    return (
        VendorTestUatService(
            operation_store,
            recipient_store,
            apps or FakeApps(),
            pipeline_factory,
        ),
        operation_store,
        recipient_store,
        pipeline,
    )


@pytest.mark.asyncio
async def test_uat_keeps_historical_recipient_encrypted_through_api_pipeline() -> None:
    uat, operations, recipients, pipeline = service()

    result = await uat.send(
        operation_id=OPERATION_ID,
        recipient_id=9,
        app_id=7,
        category="verify",
        principal=ADMIN,
        content="验证码123456",
        template_id=None,
        template_params=None,
        sign_name=None,
        consent_confirmed=False,
        remark="首条受控联调",
    )

    assert result.status == "running"
    assert result.batch_no == "batch-uat"
    assert recipients.resolved == [9]
    app, request = pipeline.calls[0]
    assert app.app_id == 7
    assert request.mobiles == ()
    assert request.vendor_test_uat is True
    assert request.is_test is True
    assert len(request.protected_mobiles) == 1
    protected = request.protected_mobiles[0]
    assert protected.phone_enc == PHONE_ENC
    assert protected.phone_hmac == PHONE_HMAC
    assert protected.phone_mask == "139****0001"
    assert protected.key_version == 2
    assert request.protected_hmac_candidates == (
        (1, HISTORICAL_PHONE_HMAC),
        (2, PHONE_HMAC),
    )
    from app.services.vendor_test_operation import vendor_test_uat_biz_id

    assert request.biz_id == vendor_test_uat_biz_id(OPERATION_ID)
    assert [event[0] for event in operations.events] == [
        "request",
        "running",
        "acceptance_guard_enter",
        "prepare_uat_acceptance",
        "attach_batch",
        "acceptance_guard_exit",
    ]
    assert "13800138000" not in repr(operations.events)
    assert PHONE_HMAC not in repr(operations.events)


@pytest.mark.asyncio
async def test_reset_and_uat_concurrent_start_can_reserve_only_one_operation() -> None:
    from app.services.vendor_test_operation import (
        VendorTestOperation,
        VendorTestOperationConflict,
        VendorTestOperationService,
    )

    reset_id = "c0a80101-0000-4000-8000-000000000131"
    uat_id = "c0a80101-0000-4000-8000-000000000132"

    class AtomicOperations(FakeOperations):
        def __init__(self) -> None:
            super().__init__()
            self.records: dict[str, VendorTestOperation] = {}
            self.pending_arrivals = 0
            self.pending_barrier = asyncio.Event()
            self.reserve_arrivals = 0
            self.reserve_barrier = asyncio.Event()
            self.reserve_lock = asyncio.Lock()

        async def pending(self) -> tuple[VendorTestOperation, ...]:
            snapshot = tuple(self.records.values())
            self.pending_arrivals += 1
            if self.pending_arrivals == 2:
                self.pending_barrier.set()
            await self.pending_barrier.wait()
            return snapshot

        async def request(
            self,
            operation_id: str,
            operation_type: str,
            *,
            principal: SecurityPrincipal,
            batch_no: str | None = None,
            checkpoint_id: str | None = None,
        ) -> VendorTestOperation:
            record = VendorTestOperation(
                operation_id,
                operation_type,
                principal.login_name,
                "requested",
                None,
                batch_no,
                checkpoint_id,
                NOW,
                None,
                actor_account_id=principal.account_id,
                actor_identity_id=principal.identity_id,
            )
            self.records[operation_id] = record
            return record

        async def reserve_start(
            self,
            operation_id: str,
            operation_type: str,
            *,
            principal: SecurityPrincipal,
            conflicting_types: frozenset[str],
            batch_no: str | None = None,
            checkpoint_id: str | None = None,
        ) -> VendorTestOperation:
            assert batch_no is None and checkpoint_id is None
            self.reserve_arrivals += 1
            if self.reserve_arrivals == 2:
                self.reserve_barrier.set()
            await self.reserve_barrier.wait()
            async with self.reserve_lock:
                if any(
                    record.operation_type in conflicting_types
                    and record.status in {"requested", "running"}
                    and record.operation_id != operation_id
                    for record in self.records.values()
                ):
                    raise VendorTestOperationConflict("已有联调操作正在执行")
                return await self.request(
                    operation_id,
                    operation_type,
                    principal=principal,
                )

        async def mark_running(self, operation_id: str) -> VendorTestOperation:
            record = replace(self.records[operation_id], status="running")
            self.records[operation_id] = record
            self.record = record
            return record

        async def attach_batch(
            self,
            operation_id: str,
            *,
            batch_no: str,
        ) -> VendorTestOperation:
            record = replace(self.records[operation_id], batch_no=batch_no)
            self.records[operation_id] = record
            return record

    class NoAgent:
        async def request(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("start must not call agent")

    operations = AtomicOperations()
    reset_service = VendorTestOperationService(operations, NoAgent())
    uat_service, *_ = service(operations=operations)

    reset_result, uat_result = await asyncio.gather(
        reset_service.start(
            operation_id=reset_id,
            operation_type="reset_configuration",
            principal=ADMIN,
            body={},
        ),
        uat_service.send(
            operation_id=uat_id,
            recipient_id=9,
            app_id=7,
            category="notice",
            principal=ADMIN,
            content="维护通知",
            template_id=None,
            template_params=None,
            sign_name=None,
            consent_confirmed=False,
            remark=None,
        ),
        return_exceptions=True,
    )

    outcomes = (reset_result, uat_result)
    assert sum(isinstance(item, VendorTestOperation) for item in outcomes) == 1
    assert sum(
        isinstance(item, (VendorTestOperationConflict, RecipientBusy))
        for item in outcomes
    ) == 1
    assert len(operations.records) == 1


@pytest.mark.asyncio
async def test_uat_preview_reads_selected_app_department_and_covers_verify() -> None:
    from app.services.vendor_test_uat import VendorTestUatPreviewService

    config = FakePreviewConfig()
    renderer = FakePreviewRenderer()
    signs = FakePreviewSigns()
    previewer = VendorTestUatPreviewService(FakeApps(), config, renderer, signs)

    result = await previewer.preview(
        app_id=7,
        category="verify",
        content=None,
        template_id=9,
        template_params=("123456",),
        sign_name=None,
        consent_confirmed=False,
    )

    assert result.quota_cost == 1
    assert result.final_length == len("【测试签名】模板内容123456")
    assert config.depts == ["平台部"]
    assert renderer.calls == [(9, ("123456",), "平台部")]
    assert signs.calls == ["测试签名"]


@pytest.mark.asyncio
async def test_disabled_or_busy_recipient_is_rejected_before_pipeline() -> None:
    busy_operation = replace(
        operation("running"),
        operation_id="c0a80101-0000-4000-8000-000000000122",
    )
    busy_uat, *_ = service(
        operations=FakeOperations(pending=(busy_operation,))
    )
    with pytest.raises(RecipientBusy):
        await busy_uat.send(
            operation_id=OPERATION_ID,
            recipient_id=9,
            app_id=7,
            category="notice",
            principal=ADMIN,
            content="维护通知",
            template_id=None,
            template_params=None,
            sign_name=None,
            consent_confirmed=False,
            remark=None,
        )

    disabled_uat, operations, _, pipeline = service(
        recipients=FakeRecipients(missing=True)
    )
    with pytest.raises(RecipientNotFound):
        await disabled_uat.send(
            operation_id=OPERATION_ID,
            recipient_id=9,
            app_id=7,
            category="notice",
            principal=ADMIN,
            content="维护通知",
            template_id=None,
            template_params=None,
            sign_name=None,
            consent_confirmed=False,
            remark=None,
        )
    assert pipeline.calls == []
    assert operations.events[-1][2:4] == ("failed", "RECIPIENT_NOT_AVAILABLE")


@pytest.mark.asyncio
async def test_disabled_app_is_rejected_and_safe_failure_contains_no_request_data() -> None:
    uat, operations, _, pipeline = service(apps=FakeApps(status=0))

    with pytest.raises(VendorTestAppUnavailable):
        await uat.send(
            operation_id=OPERATION_ID,
            recipient_id=9,
            app_id=7,
            category="market",
            principal=ADMIN,
            content="formal-content-sentinel",
            template_id=None,
            template_params=None,
            sign_name=None,
            consent_confirmed=True,
            remark=None,
        )

    assert pipeline.calls == []
    assert operations.events[-1][2:4] == ("failed", "APP_NOT_AVAILABLE")
    assert "formal-content-sentinel" not in repr(operations.events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result_status", "safe_code", "vendor_code"),
    [
        ("succeeded", None, None),
        ("failed", "VENDOR_ERROR", 1010),
        ("failed", "RESULT_UNCERTAIN", None),
    ],
)
async def test_uat_poll_completes_only_from_safe_worker_projection(
    result_status: str,
    safe_code: str | None,
    vendor_code: int | None,
) -> None:
    from app.services.vendor_test_uat import UatBatchResult

    operations = FakeOperations(
        result=UatBatchResult("batch-uat", result_status, safe_code, vendor_code)
    )
    operations.record = operation("running", batch_no="batch-uat")
    uat, *_ = service(operations=operations)

    record = await uat.get(OPERATION_ID)

    assert record is not None
    assert record.status == result_status
    assert record.safe_code == safe_code
    assert record.vendor_code == vendor_code
    assert [event[0] for event in operations.events] == ["complete"]


@pytest.mark.asyncio
async def test_uat_poll_keeps_retrying_or_unsent_batch_running() -> None:
    from app.services.vendor_test_uat import UatBatchResult

    operations = FakeOperations(
        result=UatBatchResult("batch-uat", "running", None, None)
    )
    operations.record = operation("running", batch_no="batch-uat")
    uat, *_ = service(operations=operations)

    record = await uat.get(OPERATION_ID)

    assert record is operations.record
    assert operations.events == []


@pytest.mark.asyncio
async def test_requested_crash_without_batch_expires_without_calling_pipeline() -> None:
    operations = FakeOperations(expire_empty=True)
    operations.record = operation("requested")
    uat, _, _, pipeline = service(operations=operations)

    record = await uat.get(OPERATION_ID)

    assert record is not None
    assert record.status == "failed"
    assert record.safe_code == "UAT_ACCEPTANCE_EXPIRED"
    assert pipeline.calls == []
    assert [event[0] for event in operations.events] == [
        "expire_uat_if_stale",
        "complete",
    ]


@pytest.mark.asyncio
async def test_running_crash_before_accept_expires_without_calling_pipeline() -> None:
    operations = FakeOperations(expire_empty=True)
    operations.record = operation("running")
    uat, _, _, pipeline = service(operations=operations)

    record = await uat.get(OPERATION_ID)

    assert record is not None
    assert record.status == "failed"
    assert record.safe_code == "UAT_ACCEPTANCE_EXPIRED"
    assert pipeline.calls == []
    assert [event[0] for event in operations.events] == [
        "expire_uat_if_stale",
        "complete",
    ]


@pytest.mark.asyncio
async def test_periodic_recovery_attaches_committed_batch_without_reaccepting() -> None:
    from app.services import vendor_test_uat as uat_module
    from app.services.vendor_test_uat import UatBatchResult

    operations = FakeOperations(
        pending=(operation("running"),),
        result=UatBatchResult("batch-uat", "running", None, None),
    )
    reconciler_type = getattr(uat_module, "VendorTestUatReconciler", None)

    assert reconciler_type is not None, "缺少 UAT 周期恢复器"
    reconciled = await reconciler_type(operations).reconcile_once()

    assert reconciled == 1
    assert operations.record.status == "running"
    assert operations.record.batch_no == "batch-uat"
    assert [event[0] for event in operations.events] == ["attach_batch"]


@pytest.mark.asyncio
async def test_periodic_recovery_completes_attached_uncertain_without_reaccepting() -> None:
    from app.services import vendor_test_uat as uat_module
    from app.services.vendor_test_uat import UatBatchResult

    attached = operation("running", batch_no="batch-uat")
    operations = FakeOperations(
        pending=(attached,),
        result=UatBatchResult("batch-uat", "failed", "RESULT_UNCERTAIN", None),
    )
    operations.record = attached
    reconciler_type = getattr(uat_module, "VendorTestUatReconciler", None)

    assert reconciler_type is not None, "缺少 UAT 周期恢复器"
    reconciled = await reconciler_type(operations).reconcile_once()

    assert reconciled == 1
    assert operations.record.status == "failed"
    assert operations.record.safe_code == "RESULT_UNCERTAIN"
    assert [event[0] for event in operations.events] == ["complete"]


@pytest.mark.asyncio
async def test_long_acceptance_renews_operation_lease_until_batch_is_attached() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    heartbeat_tick = asyncio.Event()

    class BlockingPipeline(FakePipeline):
        async def accept(
            self,
            app: ApiAppContext,
            request: SendRequest,
        ) -> BatchResponse:
            entered.set()
            await release.wait()
            return await super().accept(app, request)

    operations = FakeOperations()
    pipeline = BlockingPipeline()

    @asynccontextmanager
    async def pipeline_factory(_app: ApiAppContext) -> AsyncIterator[FakePipeline]:
        yield pipeline

    async def sleeper(_delay: float) -> None:
        await heartbeat_tick.wait()
        heartbeat_tick.clear()

    from app.services.vendor_test_uat import VendorTestUatService

    uat = VendorTestUatService(
        operations,
        FakeRecipients(),
        FakeApps(),
        pipeline_factory,
        heartbeat_interval_s=10,
        sleeper=sleeper,
    )
    send_task = asyncio.create_task(
        uat.send(
            operation_id=OPERATION_ID,
            recipient_id=9,
            app_id=7,
            category="notice",
            principal=ADMIN,
            content="维护通知",
            template_id=None,
            template_params=None,
            sign_name=None,
            consent_confirmed=False,
            remark=None,
        )
    )
    await entered.wait()
    heartbeat_tick.set()
    for _ in range(20):
        if any(event[0] == "heartbeat" for event in operations.events):
            break
        await asyncio.sleep(0)
    release.set()
    result = await send_task

    assert result.batch_no == "batch-uat"
    assert any(event[0] == "heartbeat" for event in operations.events)
    assert operations.events.index(("acceptance_guard_enter", OPERATION_ID)) < (
        operations.events.index(("attach_batch", OPERATION_ID, "batch-uat"))
    )
    assert operations.events.index(("attach_batch", OPERATION_ID, "batch-uat")) < (
        operations.events.index(("acceptance_guard_exit", OPERATION_ID))
    )


@pytest.mark.asyncio
async def test_heartbeat_storage_failure_does_not_mask_guarded_batch_attachment() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    heartbeat_tick = asyncio.Event()

    class FailingHeartbeatOperations(FakeOperations):
        async def heartbeat(self, operation_id: str) -> bool:
            self.events.append(("heartbeat_failed", operation_id))
            raise RuntimeError("heartbeat storage unavailable")

    class BlockingPipeline(FakePipeline):
        async def accept(
            self,
            app: ApiAppContext,
            request: SendRequest,
        ) -> BatchResponse:
            entered.set()
            await release.wait()
            return await super().accept(app, request)

    operations = FailingHeartbeatOperations()
    pipeline = BlockingPipeline()

    @asynccontextmanager
    async def pipeline_factory(_app: ApiAppContext) -> AsyncIterator[FakePipeline]:
        yield pipeline

    async def sleeper(_delay: float) -> None:
        await heartbeat_tick.wait()

    from app.services.vendor_test_uat import VendorTestUatService

    uat = VendorTestUatService(
        operations,
        FakeRecipients(),
        FakeApps(),
        pipeline_factory,
        heartbeat_interval_s=10,
        sleeper=sleeper,
    )
    send_task = asyncio.create_task(
        uat.send(
            operation_id=OPERATION_ID,
            recipient_id=9,
            app_id=7,
            category="notice",
            principal=ADMIN,
            content="维护通知",
            template_id=None,
            template_params=None,
            sign_name=None,
            consent_confirmed=False,
            remark=None,
        )
    )
    await entered.wait()
    heartbeat_tick.set()
    for _ in range(20):
        if any(event[0] == "heartbeat_failed" for event in operations.events):
            break
        await asyncio.sleep(0)
    release.set()

    result = await send_task

    assert result.batch_no == "batch-uat"
    assert operations.record.batch_no == "batch-uat"


@pytest.mark.asyncio
async def test_same_operation_concurrency_can_call_pipeline_accept_only_once() -> None:
    class SingleClaimOperations(FakeOperations):
        def __init__(self) -> None:
            super().__init__()
            self.reserve_count = 0
            self.reserve_barrier = asyncio.Event()
            self.claim_lock = asyncio.Lock()
            self.claimed = False

        async def reserve_start(
            self,
            operation_id: str,
            operation_type: str,
            *,
            principal: SecurityPrincipal,
            conflicting_types: frozenset[str],
        ) -> VendorTestOperation:
            self.reserve_count += 1
            if self.reserve_count == 2:
                self.reserve_barrier.set()
            await self.reserve_barrier.wait()
            return self.record

        async def claim_uat_running(
            self,
            operation_id: str,
        ) -> VendorTestOperation | None:
            async with self.claim_lock:
                if self.claimed:
                    return None
                self.claimed = True
                return await self.mark_running(operation_id)

    operations = SingleClaimOperations()
    uat, _, _, pipeline = service(operations=operations)
    kwargs = {
        "operation_id": OPERATION_ID,
        "recipient_id": 9,
        "app_id": 7,
        "category": "notice",
        "principal": ADMIN,
        "content": "维护通知",
        "template_id": None,
        "template_params": None,
        "sign_name": None,
        "consent_confirmed": False,
        "remark": None,
    }

    first, second = await asyncio.gather(
        uat.send(**kwargs),
        uat.send(**kwargs),
    )

    assert isinstance(first, VendorTestOperation)
    assert isinstance(second, VendorTestOperation)
    assert len(pipeline.calls) == 1


@pytest.mark.asyncio
async def test_expired_operation_cannot_accept_after_waiting_for_guard() -> None:
    class ExpiredBeforeGuardOperations(FakeOperations):
        @asynccontextmanager
        async def acceptance_guard(
            self,
            operation_id: str,
        ) -> AsyncIterator[None]:
            self.events.append(("acceptance_guard_enter", operation_id))
            self.record = operation("failed")
            try:
                yield
            finally:
                self.events.append(("acceptance_guard_exit", operation_id))

        async def prepare_uat_acceptance(self, operation_id: str) -> bool:
            self.events.append(("prepare_uat_acceptance", operation_id))
            return False

    operations = ExpiredBeforeGuardOperations()
    uat, _, _, pipeline = service(operations=operations)

    result = await uat.send(
        operation_id=OPERATION_ID,
        recipient_id=9,
        app_id=7,
        category="notice",
        principal=ADMIN,
        content="维护通知",
        template_id=None,
        template_params=None,
        sign_name=None,
        consent_confirmed=False,
        remark=None,
    )

    assert result.status == "failed"
    assert pipeline.calls == []
    assert ("prepare_uat_acceptance", OPERATION_ID) in operations.events


@pytest.mark.asyncio
async def test_attach_failure_after_accept_stays_running_and_recovers_without_reaccept() -> None:
    from app.services.vendor_test_operation import VendorTestOperationPending
    from app.services.vendor_test_uat import UatBatchResult

    class FailFirstAttachOperations(FakeOperations):
        def __init__(self) -> None:
            super().__init__(
                result=UatBatchResult("batch-uat", "running", None, None),
            )
            self.fail_attach = True

        async def attach_batch(
            self,
            operation_id: str,
            *,
            batch_no: str,
        ) -> VendorTestOperation:
            self.events.append(("attach_batch", operation_id, batch_no))
            if self.fail_attach:
                self.fail_attach = False
                raise RuntimeError("attach storage unavailable")
            self.record = operation("running", batch_no=batch_no)
            return self.record

    operations = FailFirstAttachOperations()
    uat, _, _, pipeline = service(operations=operations)

    with pytest.raises(VendorTestOperationPending):
        await uat.send(
            operation_id=OPERATION_ID,
            recipient_id=9,
            app_id=7,
            category="notice",
            principal=ADMIN,
            content="维护通知",
            template_id=None,
            template_params=None,
            sign_name=None,
            consent_confirmed=False,
            remark=None,
        )

    assert operations.record.status == "running"
    assert operations.record.batch_no is None
    assert not any(event[0] == "complete" for event in operations.events)
    recovered = await uat.get(OPERATION_ID)

    assert recovered is not None
    assert recovered.status == "running"
    assert recovered.batch_no == "batch-uat"
    assert len(pipeline.calls) == 1


@pytest.mark.asyncio
async def test_cancel_before_accept_stays_nonterminal_then_expires_without_accept() -> None:
    entered = asyncio.Event()

    operations = FakeOperations()
    pipeline = FakePipeline()

    @asynccontextmanager
    async def pipeline_factory(_app: ApiAppContext) -> AsyncIterator[FakePipeline]:
        entered.set()
        await asyncio.Future()
        yield pipeline

    from app.services.vendor_test_uat import VendorTestUatService

    uat = VendorTestUatService(
        operations,
        FakeRecipients(),
        FakeApps(),
        pipeline_factory,
    )
    send_task = asyncio.create_task(
        uat.send(
            operation_id=OPERATION_ID,
            recipient_id=9,
            app_id=7,
            category="notice",
            principal=ADMIN,
            content="维护通知",
            template_id=None,
            template_params=None,
            sign_name=None,
            consent_confirmed=False,
            remark=None,
        )
    )
    await entered.wait()
    send_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await send_task

    assert operations.record.status == "running"
    assert operations.record.batch_no is None
    assert pipeline.calls == []
    assert not any(event[0] == "complete" for event in operations.events)

    operations.expire_empty = True
    recovered = await uat.get(OPERATION_ID)

    assert recovered is not None
    assert recovered.status == "failed"
    assert recovered.safe_code == "UAT_ACCEPTANCE_EXPIRED"
    assert pipeline.calls == []


@pytest.mark.asyncio
async def test_cancel_during_accept_recovers_pg_fact_without_reaccept() -> None:
    from app.services.vendor_test_operation import UatBatchResult
    from app.services.vendor_test_uat import VendorTestUatService

    entered = asyncio.Event()

    class BlockingPipeline(FakePipeline):
        async def accept(
            self,
            app: ApiAppContext,
            request: SendRequest,
        ) -> BatchResponse:
            self.calls.append((app, request))
            entered.set()
            await asyncio.Future()
            raise AssertionError("unreachable")

    operations = FakeOperations()
    pipeline = BlockingPipeline()

    @asynccontextmanager
    async def pipeline_factory(_app: ApiAppContext) -> AsyncIterator[FakePipeline]:
        yield pipeline

    uat = VendorTestUatService(
        operations,
        FakeRecipients(),
        FakeApps(),
        pipeline_factory,
    )
    send_task = asyncio.create_task(
        uat.send(
            operation_id=OPERATION_ID,
            recipient_id=9,
            app_id=7,
            category="notice",
            principal=ADMIN,
            content="维护通知",
            template_id=None,
            template_params=None,
            sign_name=None,
            consent_confirmed=False,
            remark=None,
        )
    )
    await entered.wait()
    send_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await send_task

    assert operations.record.status == "running"
    assert operations.record.batch_no is None
    assert len(pipeline.calls) == 1
    assert not any(event[0] == "complete" for event in operations.events)

    operations.result = UatBatchResult("batch-uat", "running", None, None)
    recovered = await uat.get(OPERATION_ID)

    assert recovered is not None
    assert recovered.status == "running"
    assert recovered.batch_no == "batch-uat"
    assert len(pipeline.calls) == 1


@pytest.mark.asyncio
async def test_accept_unknown_result_stays_nonterminal_until_pg_fact_recovery() -> None:
    from app.services.vendor_test_operation import VendorTestOperationPending
    from app.services.vendor_test_uat import VendorTestUatService

    class UnknownPipeline(FakePipeline):
        async def accept(
            self,
            app: ApiAppContext,
            request: SendRequest,
        ) -> BatchResponse:
            self.calls.append((app, request))
            raise RuntimeError("accept commit result unknown")

    operations = FakeOperations(expire_empty=True)
    pipeline = UnknownPipeline()

    @asynccontextmanager
    async def pipeline_factory(_app: ApiAppContext) -> AsyncIterator[FakePipeline]:
        yield pipeline

    uat = VendorTestUatService(
        operations,
        FakeRecipients(),
        FakeApps(),
        pipeline_factory,
    )

    with pytest.raises(VendorTestOperationPending):
        await uat.send(
            operation_id=OPERATION_ID,
            recipient_id=9,
            app_id=7,
            category="notice",
            principal=ADMIN,
            content="维护通知",
            template_id=None,
            template_params=None,
            sign_name=None,
            consent_confirmed=False,
            remark=None,
        )

    assert operations.record.status == "running"
    assert not any(event[0] == "complete" for event in operations.events)
    recovered = await uat.get(OPERATION_ID)

    assert recovered is not None
    assert recovered.status == "failed"
    assert recovered.safe_code == "UAT_ACCEPTANCE_EXPIRED"
    assert len(pipeline.calls) == 1


def test_uat_service_has_no_direct_vendor_or_consuming_poll_calls() -> None:
    source = (
        Path(__file__).resolve().parents[2] / "app/services/vendor_test_uat.py"
    ).read_text(encoding="utf-8")

    assert "httpx" not in source
    assert "GetReport" not in source
    assert "GetReply" not in source
    assert "ZhihuiClient" not in source


@pytest.mark.asyncio
async def test_page_uat_pipeline_uses_shared_application_rate_limiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import vendor_test_uat as module
    from app.services.app_ratelimit import ApplicationRateLimiter

    class FakeRedis:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    class FakeStore:
        async def load_config(self, dept: str) -> dict[str, str]:
            assert dept == "平台部"
            return {}

    redis = FakeRedis()
    guard = object()
    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(redis_control_url="redis://test"))
    monkeypatch.setattr(module.CryptoService, "from_settings", lambda _settings: object())
    monkeypatch.setattr(module.Redis, "from_url", lambda *_args, **_kwargs: redis)
    monkeypatch.setattr(module, "SqlPipelineStore", lambda _settings: FakeStore())
    monkeypatch.setattr(module, "get_send_admission_guard", lambda: guard)

    service = module.build_vendor_test_uat_service()
    app = module.VendorTestUatService._app(await FakeApps().get(7), 7)

    async with service.pipeline_factory(app) as pipeline:
        assert isinstance(pipeline.acceptance_limiter, ApplicationRateLimiter)
        assert pipeline.acceptance_limiter.redis is redis
        assert pipeline.admission_guard is guard

    assert redis.closed is True
