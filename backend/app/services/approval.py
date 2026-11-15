"""审批阈值、回避与状态迁移用例。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.core.auth.accounts import SecurityPrincipal
from app.core.sensitive_text import reject_phone_in_text
from app.services.alert import alert_reference


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
    deferred_reason: str | None = None
    generation: int = 1


class ApprovalRepository(Protocol):
    """状态迁移与发送或释放 Outbox 事件必须在同一事务内提交。"""

    async def get(self, approval_id: int) -> ApprovalCase | None: ...

    async def transition(
        self,
        approval_id: int,
        *,
        action: str,
        principal: SecurityPrincipal,
        reason: str | None,
        expected_generation: int,
    ) -> ApprovalCase | None: ...

    async def expire_due(self) -> list[ApprovalCase]: ...


class ApprovalAlertPort(Protocol):
    async def emit(
        self,
        *,
        alert_type: str,
        level: str,
        title: str,
        detail: dict[str, object],
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
        alerts: ApprovalAlertPort,
    ) -> None:
        self.repository = repository
        self.alerts = alerts

    async def decide(
        self,
        approval_id: int,
        *,
        action: str,
        principal: SecurityPrincipal,
        reason: str | None,
        expected_generation: int,
    ) -> ApprovalCase:
        if action not in {"approve", "reject"}:
            raise ValueError("invalid approval action")
        if expected_generation < 1:
            raise StateConflict("审批版本缺失，请刷新后重新确认")
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
        if current.generation != expected_generation:
            raise StateConflict("审批版本已变化，请刷新后重新确认")
        decided = await self.repository.transition(
            approval_id,
            action=action,
            principal=principal,
            reason=cleaned_reason,
            expected_generation=expected_generation,
        )
        if decided is None:
            raise StateConflict("审批单状态冲突")
        return decided

    async def expire_due(self) -> int:
        expired = await self.repository.expire_due()
        for case in expired:
            reference, reference_detail = alert_reference("batch_no", case.batch_no)
            await self.alerts.emit(
                alert_type="approval_expired",
                level="info",
                title="短信审批已过期关闭",
                detail={**reference_detail, "dept": case.dept},
                dedup_key=f"approval_expired:{reference}",
                dedup_hours=48,
            )
        return len(expired)
