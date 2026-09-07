"""safe rejection 之后的权威后续动作：失败、切换待领取或明确暂停。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

from app.vendor.codes import VendorErrorPolicy, policy_for
from app.vendor.routing import (
    ROUTE_POLICY_VERSION,
    RouteDecision,
    RouteRequest,
    VendorAttempt,
    VendorHealth,
    VendorRecord,
    decide,
    default_vendor_registry,
    validate_vendor_id,
)

# 暂停/余额类错误即使带 safe_to_failover，也不得隐式切换供应商。
PauseFollowup = Literal["hold"]
PAUSE_FAILOVER_POLICY: dict[int, PauseFollowup] = {
    999: "hold",
    1000: "hold",
    1009: "hold",
    5000: "hold",
    10003: "hold",
    10004: "hold",
}

BLOCKING_ATTEMPT_OUTCOMES = frozenset(
    {"submitted", "uncertain", "invoking", "inconsistent"}
)
CONTRACT_REJECTED_OUTCOME = "rejected"
FIRST_INVOKE_CHUNK_STATES = frozenset({"submitting"})
CLAIMABLE_CHUNK_STATES = frozenset({"failover_pending"})
ISOLATING_CHUNK_STATES = frozenset(
    {"uncertain", "unknown_terminal", "inconsistent", "failed", "submitted"}
)


class InvokeClaimKind(StrEnum):
    """下一跳领取 CAS 的三分结果。"""

    AUTHORIZED = "authorized"
    ALREADY_HANDLED = "already_handled"
    DENIED = "denied"


class NextAction(StrEnum):
    """finalize 事务内持久化的后续动作。"""

    FAILED = "failed"
    FAILOVER_PENDING = "failover_pending"
    RETRYING = "retrying"
    BALANCE_BLOCKED = "balance_blocked"


@dataclass(frozen=True, slots=True)
class InvokeAuthorization:
    """CAS 成功后才允许发出 HTTP 的授权。"""

    attempt_id: int
    generation: int
    vendor_id: str
    adapter_id: str
    route_policy_version: int


@dataclass(frozen=True, slots=True)
class InvokeClaim:
    """claim_next_vendor_invoke 的权威结果。"""

    kind: InvokeClaimKind
    authorization: InvokeAuthorization | None = None
    reason: str = ""
    reset_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PersistedNextAction:
    """同一终结事务写入的下一跳事实。"""

    action: NextAction
    next_vendor: str | None = None
    route_generation: int | None = None
    previous_attempt_id: int | None = None
    route_policy_version: int = ROUTE_POLICY_VERSION
    reason: str = ""


def followup_for_policy(
    policy: VendorErrorPolicy,
    *,
    vendor_code: int | None,
) -> Literal["hold", "failover_or_fail", "fail"]:
    """暂停策略表优先于 safe_to_failover；不得隐式允许余额/封禁绕过。"""

    if policy.balance_blocked or policy.pause_queues:
        code = vendor_code if vendor_code is not None else -1
        return PAUSE_FAILOVER_POLICY.get(code, "hold")
    if policy.safe_to_failover:
        return "failover_or_fail"
    return "fail"


def followup_for_code(vendor_code: int) -> Literal["hold", "failover_or_fail", "fail"]:
    """按厂商码查明确后续动作。"""

    return followup_for_policy(policy_for(vendor_code), vendor_code=vendor_code)


def vendor_matches_route(
    record: VendorRecord,
    *,
    category: str,
    adapter_ids: frozenset[str] | None = None,
) -> bool:
    """候选必须启用、支持实际 category，且 adapter 已注册。"""

    if not record.enabled:
        return False
    if category not in record.categories:
        return False
    if not record.adapter:
        return False
    return adapter_ids is None or record.adapter in adapter_ids


def records_by_id(
    records: tuple[VendorRecord, ...] | None = None,
) -> dict[str, VendorRecord]:
    return {item.vendor_id: item for item in (records or default_vendor_registry())}


def eligible_vendor_ids(
    *,
    records: tuple[VendorRecord, ...] | None = None,
    category: str,
    adapter_ids: frozenset[str] | None = None,
) -> tuple[str, ...]:
    """按注册表过滤出当前 category 可调用的供应商。"""

    ordered = records or default_vendor_registry()
    return tuple(
        item.vendor_id
        for item in ordered
        if vendor_matches_route(item, category=category, adapter_ids=adapter_ids)
    )


def decide_next_after_reject(
    *,
    attempts: tuple[VendorAttempt, ...],
    category: str,
    records: tuple[VendorRecord, ...] | None = None,
    adapter_ids: frozenset[str] | None = None,
    policy_version: int = ROUTE_POLICY_VERSION,
) -> RouteDecision:
    """用实际 category 与注册表决定 safe reject 后的下一跳。"""

    registered = eligible_vendor_ids(
        records=records,
        category=category,
        adapter_ids=adapter_ids,
    )
    health = tuple(VendorHealth(vendor_id, available=True) for vendor_id in registered)
    return decide(
        RouteRequest(
            registered=registered,
            attempts=attempts,
            health=health,
            category=category,
            policy_version=policy_version,
        )
    )


def next_action_after_safe_reject(
    *,
    attempts: tuple[VendorAttempt, ...],
    category: str,
    previous_attempt_id: int,
    records: tuple[VendorRecord, ...] | None = None,
    adapter_ids: frozenset[str] | None = None,
    policy_version: int = ROUTE_POLICY_VERSION,
) -> PersistedNextAction:
    """无下一候选则失败；有合法候选则持久化 failover_pending。"""

    decision = decide_next_after_reject(
        attempts=attempts,
        category=category,
        records=records,
        adapter_ids=adapter_ids,
        policy_version=policy_version,
    )
    last = attempts[-1] if attempts else None
    generation = last.generation if last is not None else 0
    if (
        decision.action == "invoke"
        and decision.vendor_id is not None
        and (last is None or decision.vendor_id != last.vendor_id)
    ):
        return PersistedNextAction(
            action=NextAction.FAILOVER_PENDING,
            next_vendor=validate_vendor_id(decision.vendor_id),
            route_generation=generation,
            previous_attempt_id=previous_attempt_id,
            route_policy_version=policy_version,
            reason=decision.reason,
        )
    return PersistedNextAction(
        action=NextAction.FAILED,
        route_generation=generation,
        previous_attempt_id=previous_attempt_id,
        route_policy_version=policy_version,
        reason=decision.reason or "failover_exhausted",
    )


def claim_denied_reason(
    *,
    chunk_status: str,
    batch_status: str,
    expected_route_generation: int,
    actual_route_generation: int,
    previous_attempt: VendorAttempt | None,
    previous_attempt_chunk_id: int | None,
    chunk_id: int,
    expected_next_vendor: str,
    persisted_next_vendor: str | None,
    expected_route_policy_version: int,
    actual_route_policy_version: int,
    retry_due: bool,
    blocking_outcomes: frozenset[str],
    category: str,
    records: tuple[VendorRecord, ...] | None,
    adapter_ids: frozenset[str] | None,
) -> str | None:
    """返回拒绝原因；None 表示可通过领取核验。"""

    if chunk_status in ISOLATING_CHUNK_STATES:
        return f"chunk_{chunk_status}"
    if chunk_status not in CLAIMABLE_CHUNK_STATES:
        return "chunk_not_claimable"
    if batch_status not in {"queued", "sending"}:
        return "batch_not_callable"
    if actual_route_generation != expected_route_generation:
        return "generation_mismatch"
    if actual_route_policy_version != expected_route_policy_version:
        return "policy_version_mismatch"
    if not retry_due:
        return "retry_not_due"
    if previous_attempt is None or previous_attempt_chunk_id != chunk_id:
        return "previous_attempt_mismatch"
    if previous_attempt.generation != expected_route_generation:
        return "previous_generation_mismatch"
    if previous_attempt.outcome != CONTRACT_REJECTED_OUTCOME:
        return "previous_not_rejected"
    if not previous_attempt.safe_to_failover:
        return "previous_not_safe_reject"
    if persisted_next_vendor != expected_next_vendor:
        return "next_vendor_mismatch"
    if blocking_outcomes & BLOCKING_ATTEMPT_OUTCOMES:
        return "blocking_attempt"
    expected = validate_vendor_id(expected_next_vendor)
    mapping = records_by_id(records)
    record = mapping.get(expected)
    if record is None or not vendor_matches_route(
        record, category=category, adapter_ids=adapter_ids
    ):
        return "vendor_not_eligible"
    return None


def already_handled_reason(
    *,
    chunk_status: str,
    invoking_vendor: str | None,
    expected_next_vendor: str,
    invoking_generation: int | None,
    expected_route_generation: int,
) -> str | None:
    """同结果重投：已有同一 pending 下一跳的 invoking。"""

    if (
        chunk_status == "submitting"
        and invoking_vendor == expected_next_vendor
        and invoking_generation == expected_route_generation + 1
    ):
        return "already_invoking"
    return None


def submitting_timeout_includes(status: str) -> bool:
    """failover_pending 不得进入 submitting 超时转 uncertain。"""

    return status == "submitting"


def historical_safe_reject_repairable(
    *,
    chunk_status: str,
    attempts: tuple[VendorAttempt, ...],
) -> bool:
    """历史 submitting + 最后一次为合同允许的 safe reject，且无后续不可逆 attempt。"""

    if chunk_status != "submitting" or not attempts:
        return False
    if any(item.outcome in BLOCKING_ATTEMPT_OUTCOMES for item in attempts):
        return False
    last = attempts[-1]
    return last.outcome == CONTRACT_REJECTED_OUTCOME and last.safe_to_failover
