from __future__ import annotations

import inspect
from datetime import UTC, datetime

import pytest

from app.api import approvals, messages, replies, templates, web_messages
from app.core.apikey import ApiAppContext
from app.core.auth.accounts import SecurityPrincipal
from app.services.approval import ApprovalCase, ApprovalService
from app.services.blacklist import BlacklistService
from app.services.pipeline import PipelineConfig, SendPipeline, SendRequest
from app.services.sign_management import SignManagementService, SignRecord
from app.services.template_management import (
    TemplateManagementService,
    TemplateRecord,
)
from tests.test_blacklist import FakeCache as FakeBlacklistCache
from tests.test_blacklist import FakeRepository as FakeBlacklistRepository
from tests.test_blacklist import crypto as blacklist_crypto
from tests.test_send_pipeline import (
    ADMIN,
    FakeFrequency,
    FakeIdempotency,
    FakePublisher,
    FakeQuota,
    FakeStore,
    FakeUsageLedger,
    crypto,
)


@pytest.mark.asyncio
async def test_web_usage_reservation_key_isolated_by_stable_principal() -> None:
    ledger = FakeUsageLedger()
    store = FakeStore()
    pipeline = SendPipeline(
        store=store,
        idempotency=FakeIdempotency(),
        crypto=crypto(),
        frequency=FakeFrequency(),
        quota=FakeQuota(),
        publisher=FakePublisher(),
        usage_ledger=ledger,
        config=PipelineConfig(),
        clock=lambda: datetime(2026, 8, 11, 8, 0, tzinfo=UTC),
    )
    other = SecurityPrincipal(2, 20, "operator02", "乙部门", "operator")

    for app, actor in (
        (ApiAppContext(0, "web", "甲部门", frozenset({"notice"})), ADMIN),
        (ApiAppContext(0, "web", "乙部门", frozenset({"notice"})), other),
    ):
        await pipeline.accept(
            app,
            SendRequest(
                "notice",
                ["13800138000"],
                content="通知",
                biz_id="shared-biz",
                channel="web",
                actor=actor,
            ),
        )

    assert store.commands[0].scope_id != store.commands[1].scope_id
    assert ledger.started[0]["request_key"] != ledger.started[1]["request_key"]


@pytest.mark.asyncio
async def test_blacklist_and_approval_reject_phone_embedded_in_metadata() -> None:
    blacklist_repository = FakeBlacklistRepository()
    blacklist = BlacklistService(
        blacklist_repository,
        FakeBlacklistCache(),
        blacklist_crypto(),
    )
    with pytest.raises(ValueError, match="不得包含手机号"):
        await blacklist.add(
            ["13800138000"],
            source="manual",
            remark="请联系 13900139000 复核",
            principal=SecurityPrincipal(1, 11, "admin01", "平台部", "admin"),
            ip="10.0.0.8",
        )
    assert blacklist_repository.entries == {}

    applicant = SecurityPrincipal(11, 101, "operator01", "平台部", "operator")
    approver = SecurityPrincipal(12, 102, "approver01", "平台部", "approver")

    class ApprovalRepository:
        def __init__(self) -> None:
            self.transitioned = False

        async def get(self, _approval_id: int) -> ApprovalCase:
            return ApprovalCase(
                3,
                "batch-1",
                applicant.login_name,
                7,
                applicant.dept,
                "20260811",
                1,
                "notice",
                "pending",
                "pending_approval",
                applicant.account_id,
                applicant.identity_id,
            )

        async def transition(self, *_: object, **__: object) -> ApprovalCase:
            self.transitioned = True
            return await self.get(3)

    class Noop:
        async def refund_once(self, **_: object) -> object:
            return object()

        async def enqueue(self, *_: object, **__: object) -> None:
            return None

        async def emit(self, **_: object) -> None:
            return None

    approval_repository = ApprovalRepository()
    with pytest.raises(ValueError, match="不得包含手机号"):
        await ApprovalService(
            approval_repository,  # type: ignore[arg-type]
            Noop(),
            Noop(),
            Noop(),
        ).decide(
            3,
            action="reject",
            principal=approver,
            reason="号码 13900139000 不合规",
        )
    assert approval_repository.transitioned is False


@pytest.mark.asyncio
async def test_vendor_rejection_reasons_are_masked_before_repository() -> None:
    class TemplateRepository:
        def __init__(self) -> None:
            self.states: list[tuple[int, str, str, str | None]] = []

        async def syncable(self, _template_id: int | None = None) -> list[TemplateRecord]:
            return [TemplateRecord(1, "模板", "通知", [], "平台部", "21", "pending", None)]

        async def apply_states(
            self, states: list[tuple[int, str, str, str | None]]
        ) -> int:
            self.states = states
            return len(states)

    class TemplateVendor:
        async def get_template_state(self, _ids: list[int]) -> list[dict[str, object]]:
            return [{"id": 21, "checkType": 2, "checkRemark": "联系13900139000"}]

    template_repository = TemplateRepository()
    await TemplateManagementService(
        template_repository,  # type: ignore[arg-type]
        TemplateVendor(),  # type: ignore[arg-type]
    ).sync_pending()
    assert template_repository.states == [(1, "21", "rejected", "联系***********")]

    class SignRepository:
        def __init__(self) -> None:
            self.states: list[tuple[int, str, str | None]] = []

        async def pending(self, _sign_id: int | None = None) -> list[SignRecord]:
            return [SignRecord(1, "签名", "31", "pending", None)]

        async def apply_states(self, states: list[tuple[int, str, str | None]]) -> int:
            self.states = states
            return len(states)

    class SignVendor:
        async def get_sign_state(self, _ids: list[int]) -> list[dict[str, object]]:
            return [{"id": 31, "checkType": 2, "checkRemark": "回拨13900139000"}]

    sign_repository = SignRepository()
    await SignManagementService(
        sign_repository,  # type: ignore[arg-type]
        SignVendor(),  # type: ignore[arg-type]
    ).sync_pending()
    assert sign_repository.states == [(1, "rejected", "回拨***********")]


def test_schema_requires_encrypted_template_content_and_blocks_phone_metadata() -> None:
    schema = (
        __import__("pathlib").Path(__file__).resolve().parents[2] / "schema.sql"
    ).read_text(encoding="utf-8")
    assert "content_enc          BYTEA        NOT NULL" in schema
    assert "ck_sms_template_content_marker" in schema
    assert "name_enc             BYTEA        NOT NULL" in schema
    assert "ck_sms_template_name_marker" in schema
    assert "ck_import_task_canonical_filename" in schema
    for constraint in (
        "ck_sms_batch_remark_no_phone",
        "ck_blacklist_remark_no_phone",
        "ck_approval_reason_no_phone",
        "ck_sms_template_reject_reason_no_phone",
        "ck_sms_sign_reject_reason_no_phone",
    ):
        assert constraint in schema


def test_every_user_visible_sensitive_content_route_calls_read_auditor() -> None:
    routes = (
        messages.get_batch,
        web_messages.list_web_batches,
        web_messages.search_web_messages,
        web_messages.message_timeline,
        approvals.get_approval,
        replies.list_replies,
        templates.list_templates,
        templates.get_template,
    )
    missing = [
        route.__name__
        for route in routes
        if "auditor.record" not in inspect.getsource(route)
    ]
    assert missing == []
