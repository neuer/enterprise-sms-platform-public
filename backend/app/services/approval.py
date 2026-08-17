"""审批阈值、回避与状态迁移用例。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.core.auth.accounts import SecurityPrincipal
from app.core.sensitive_text import reject_phone_in_text
from app.services.category import queue_for_category


class SelfApprovalDenied(PermissionError):
    pass


class StateConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ApprovalCase:
    approval_id: int
    batch_no: str
    applicant: str
    app_id: int
    dept: str
    quota_date: str
    quota_cost: int
    category: str
    status: str
    batch_status: str
    applicant_account_id: int | None = None
    applicant_identity_id: int | None = None
    outbox_persisted: bool = False


class ApprovalRepository(Protocol):
    async def get(self, approval_id: int) -> ApprovalCase | None: ...

    async def transition(
        self,
        approval_id: int,
        *,
        action: str,
        principal: SecurityPrincipal,
        reason: str | None,
    ) -> ApprovalCase | None: ...

    async def expire_due(self) -> list[ApprovalCase]: ...


class QuotaRefundPort(Protocol):
    async def refund_once(
        self,
        *,
        app_id: int,
        dept: str,
        category: str,
        date_key: str,
        cost: int,
        event_id: str,
        marker_ttl_s: int,
    ) -> Any: ...


class ApprovalPublisher(Protocol):
    async def enqueue(self, batch_no: str, queue: str) -> None: ...


class ApprovalAlertPort(Protocol):
    async def emit(
        self,
        *,
        alert_type: str,
        level: str,
        title: str,
        detail: dict[str, Any],
        dedup_key: str,
        dedup_hours: int = 4,
    ) -> None: ...


def requires_approval(
    channel: str,
    category: str,
    total: int,
    *,
    notice_threshold: int,
    market_threshold: int,
) -> bool:
    if channel != "web":
        return False
    if category == "notice":
        return total >= notice_threshold
    if category == "market":
        return total >= market_threshold
    return False


class ApprovalService:
    def __init__(
        self,
        repository: ApprovalRepository,
        quota: QuotaRefundPort,
        publisher: ApprovalPublisher,
        alerts: ApprovalAlertPort,
    ) -> None:
        self.repository = repository
        self.quota = quota
        self.publisher = publisher
        self.alerts = alerts

    async def decide(
        self,
        approval_id: int,
        *,
        action: str,
        principal: SecurityPrincipal,
        reason: str | None,
    ) -> ApprovalCase:
        if action not in {"approve", "reject"}:
            raise ValueError("invalid approval action")
        cleaned_reason = reason.strip() if reason else None
        reject_phone_in_text(cleaned_reason, field_name="reason")
        if action == "reject" and not cleaned_reason:
            raise ValueError("驳回必须填写原因")
        current = await self.repository.get(approval_id)
        if current is None or current.status != "pending":
            raise StateConflict("审批单状态冲突")
        if current.applicant_account_id is None:
            raise SelfApprovalDenied("历史申请主体未解析，禁止审批")
        if current.applicant_account_id == principal.account_id:
            raise SelfApprovalDenied("不能审批本人提交")
        decided = await self.repository.transition(
            approval_id,
            action=action,
            principal=principal,
            reason=cleaned_reason,
        )
        if decided is None:
            raise StateConflict("审批单状态冲突")
        if action == "approve":
            if decided.batch_status == "queued" and not decided.outbox_persisted:
                await self.publisher.enqueue(
                    decided.batch_no,
                    queue_for_category(decided.category),
                )
        else:
            if not decided.outbox_persisted:
                await self.quota.refund_once(
                    app_id=decided.app_id,
                    dept=decided.dept,
                    category=decided.category,
                    date_key=decided.quota_date,
                    cost=decided.quota_cost,
                    event_id=f"approval:{approval_id}:rejected",
                    marker_ttl_s=172800,
                )
        return decided

    async def expire_due(self) -> int:
        expired = await self.repository.expire_due()
        for case in expired:
            if not case.outbox_persisted:
                await self.quota.refund_once(
                    app_id=case.app_id,
                    dept=case.dept,
                    category=case.category,
                    date_key=case.quota_date,
                    cost=case.quota_cost,
                    event_id=f"approval:{case.approval_id}:expired",
                    marker_ttl_s=172800,
                )
            await self.alerts.emit(
                alert_type="approval_expired",
                level="info",
                title="短信审批已过期关闭",
                detail={"batch_no": case.batch_no, "dept": case.dept},
                dedup_key=f"approval_expired:{case.batch_no}",
                dedup_hours=48,
            )
        return len(expired)
