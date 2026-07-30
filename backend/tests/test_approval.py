from __future__ import annotations

from dataclasses import replace

import pytest

from app.core.auth.accounts import SecurityPrincipal
from app.services.approval import (
    ApprovalCase,
    ApprovalService,
    SelfApprovalDenied,
    StateConflict,
    requires_approval,
)

OPERATOR = SecurityPrincipal(11, 101, "operator01", "平台部", "operator")
APPROVER = SecurityPrincipal(12, 102, "approver01", "平台部", "approver")


def test_notice_and_market_use_independent_thresholds() -> None:
    assert requires_approval("web", "notice", 100, notice_threshold=100, market_threshold=50)
    assert not requires_approval("web", "notice", 99, notice_threshold=100, market_threshold=50)
    assert requires_approval("web", "market", 50, notice_threshold=100, market_threshold=50)
    assert not requires_approval("api", "market", 1000, notice_threshold=100, market_threshold=50)


class FakeRepository:
    def __init__(self, case: ApprovalCase, *, batch_status: str = "queued") -> None:
        self.case = case
        self.batch_status = batch_status
        self.transitioned: list[tuple[int, str, str, str | None]] = []
        self.expired: list[ApprovalCase] = []

    async def get(self, approval_id: int) -> ApprovalCase | None:
        return self.case if approval_id == self.case.approval_id else None

    async def transition(
        self,
        approval_id: int,
        *,
        action: str,
        principal: SecurityPrincipal,
        reason: str | None,
    ) -> ApprovalCase | None:
        if self.case.status != "pending":
            return None
        self.transitioned.append((approval_id, action, principal.login_name, reason))
        return replace(self.case, batch_status=self.batch_status)

    async def expire_due(self) -> list[ApprovalCase]:
        return self.expired


class FakeQuota:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def refund_once(self, **values: object) -> object:
        self.calls.append(values)
        return object()


class FakePublisher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def enqueue(self, batch_no: str, queue: str) -> None:
        self.calls.append((batch_no, queue))


class FakeAlerts:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def emit(self, **values: object) -> None:
        self.events.append(values)


def case(*, applicant: str = "operator01", status: str = "pending") -> ApprovalCase:
    return ApprovalCase(
        3,
        "batch-1",
        applicant,
        7,
        "平台部",
        "20260711",
        20,
        "market",
        status,
        "pending_approval",
        OPERATOR.account_id,
        OPERATOR.identity_id,
    )


@pytest.mark.asyncio
@pytest.mark.authorization
async def test_self_approval_is_rejected_before_state_transition() -> None:
    repository = FakeRepository(case())
    with pytest.raises(SelfApprovalDenied):
        await ApprovalService(repository, FakeQuota(), FakePublisher(), FakeAlerts()).decide(
            3, action="approve", principal=OPERATOR, reason=None
        )
    assert repository.transitioned == []


@pytest.mark.asyncio
async def test_approve_enqueues_by_category_and_reject_refunds_once() -> None:
    repository = FakeRepository(case())
    quota = FakeQuota()
    publisher = FakePublisher()
    service = ApprovalService(repository, quota, publisher, FakeAlerts())
    await service.decide(3, action="approve", principal=APPROVER, reason=None)
    assert publisher.calls == [("batch-1", "bulk")]
    assert quota.calls == []

    repository = FakeRepository(case())
    quota = FakeQuota()
    await ApprovalService(repository, quota, FakePublisher(), FakeAlerts()).decide(
        3, action="reject", principal=APPROVER, reason="内容不合规"
    )
    assert quota.calls[0]["event_id"] == "approval:3:rejected"
    assert quota.calls[0]["category"] == "market"


@pytest.mark.asyncio
async def test_outbox_rejection_does_not_apply_a_second_direct_refund() -> None:
    repository = FakeRepository(replace(case(), outbox_persisted=True))
    quota = FakeQuota()

    await ApprovalService(repository, quota, FakePublisher(), FakeAlerts()).decide(
        3, action="reject", principal=APPROVER, reason="内容不合规"
    )

    assert quota.calls == []


@pytest.mark.asyncio
async def test_approve_does_not_enqueue_batch_that_remains_scheduled() -> None:
    repository = FakeRepository(case(), batch_status="scheduled")
    publisher = FakePublisher()
    quota = FakeQuota()

    await ApprovalService(repository, quota, publisher, FakeAlerts()).decide(
        3, action="approve", principal=APPROVER, reason=None
    )

    assert publisher.calls == []
    assert quota.calls == []


@pytest.mark.asyncio
async def test_duplicate_decision_is_state_conflict() -> None:
    with pytest.raises(StateConflict):
        service = ApprovalService(
            FakeRepository(case(status="approved")),
            FakeQuota(),
            FakePublisher(),
            FakeAlerts(),
        )
        await service.decide(3, action="approve", principal=APPROVER, reason=None)


@pytest.mark.asyncio
async def test_expiry_replays_idempotent_quota_refund_event() -> None:
    repository = FakeRepository(case())
    repository.expired = [case()]
    quota = FakeQuota()
    alerts = FakeAlerts()
    assert await ApprovalService(repository, quota, FakePublisher(), alerts).expire_due() == 1
    assert quota.calls[0]["event_id"] == "approval:3:expired"
    assert quota.calls[0]["category"] == "market"
    assert alerts.events == [
        {
            "alert_type": "approval_expired",
            "level": "info",
            "title": "短信审批已过期关闭",
            "detail": {"batch_no": "batch-1", "dept": "平台部"},
            "dedup_key": "approval_expired:batch-1",
            "dedup_hours": 48,
        }
    ]


@pytest.mark.asyncio
async def test_outbox_expiry_keeps_log_sink_alert_without_direct_refund() -> None:
    repository = FakeRepository(case())
    repository.expired = [replace(case(), outbox_persisted=True)]
    quota = FakeQuota()
    alerts = FakeAlerts()

    assert await ApprovalService(repository, quota, FakePublisher(), alerts).expire_due() == 1

    assert quota.calls == []
    assert alerts.events[0]["alert_type"] == "approval_expired"
    assert alerts.events[0]["detail"] == {"batch_no": "batch-1", "dept": "平台部"}
