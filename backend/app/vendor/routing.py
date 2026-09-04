"""多供应商安全路由：uncertain 后禁止自动切换。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

VENDOR_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
PRIMARY_VENDOR_ID = "zhihui"
ROUTE_POLICY_VERSION = 1
IRREVERSIBLE_OUTCOMES = frozenset({"submitted", "uncertain", "invoking"})
HOLD_OUTCOMES = frozenset({"retry_scheduled", "delayed", "paused"})
ATTEMPT_OUTCOMES = frozenset(
    {
        "not_invoked",
        "rejected",
        "submitted",
        "uncertain",
        "failed",
        "retry_scheduled",
        "delayed",
        "paused",
        "stale",
        "invoking",
        "cancelled_before_invoke",
    }
)


def validate_vendor_id(value: object) -> str:
    """厂商标识只允许短小写标识，禁止 Secret/PII。"""

    if not isinstance(value, str) or VENDOR_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("vendor_id is invalid")
    return value


@dataclass(frozen=True, slots=True)
class VendorRecord:
    """注册表中的供应商能力快照；凭据只引用域，不入库。"""

    vendor_id: str
    enabled: bool
    categories: frozenset[str]
    adapter: str
    token_bucket_key: str
    pause_prefix: str
    credential_domain: str


@dataclass(frozen=True, slots=True)
class VendorAttempt:
    """一次供应商副作用记录，可供对账重放。"""

    vendor_id: str
    generation: int
    outcome: str
    safe_to_failover: bool
    vendor_code: int | None = None


@dataclass(frozen=True, slots=True)
class VendorHealth:
    """确定性健康；未知必须记为不可用，禁止据此猜测切换。"""

    vendor_id: str
    available: bool
    pause_reason: str | None = None


@dataclass(frozen=True, slots=True)
class RouteRequest:
    """一次路由判定的无 PII 输入。"""

    registered: tuple[str, ...]
    attempts: tuple[VendorAttempt, ...]
    health: tuple[VendorHealth, ...]
    category: str = "notice"
    policy_version: int = ROUTE_POLICY_VERSION


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """可审计的路由决定。"""

    action: Literal[
        "invoke",
        "hold",
        "terminal_uncertain",
        "terminal_failed",
        "exhausted",
    ]
    vendor_id: str | None
    reason: str
    generation: int
    policy_version: int = ROUTE_POLICY_VERSION


def default_vendor_registry() -> tuple[VendorRecord, ...]:
    """生产只注册智慧信息；第二供应商必须显式接入后才能进入候选。"""

    return (
        VendorRecord(
            vendor_id=PRIMARY_VENDOR_ID,
            enabled=True,
            categories=frozenset({"verify", "notice", "market"}),
            adapter="zhihui",
            token_bucket_key="ratelimit:vendor",
            pause_prefix="queue:paused",
            credential_domain="zhihui",
        ),
    )


def report_may_apply(
    *,
    report_vendor_id: str,
    selected_vendor: str,
    irreversible_vendors: frozenset[str],
) -> bool:
    """回执必须属于已不可逆提交的供应商；拒绝后的迟到回执不得改写另一供应商结果。"""

    report = validate_vendor_id(report_vendor_id)
    selected = validate_vendor_id(selected_vendor)
    if report in irreversible_vendors:
        return True
    return not irreversible_vendors and report == selected


def decide(request: RouteRequest) -> RouteDecision:
    """按 attempt 与确定性健康选择下一跳；uncertain/submitted 永不切换。"""

    registered = tuple(validate_vendor_id(item) for item in request.registered)
    if not registered:
        return RouteDecision("exhausted", None, "empty_registry", 0)

    irreversible = [item for item in request.attempts if item.outcome in IRREVERSIBLE_OUTCOMES]
    if irreversible:
        last = irreversible[-1]
        if last.outcome in {"uncertain", "invoking"}:
            return RouteDecision(
                "terminal_uncertain",
                last.vendor_id,
                "uncertain_blocks_failover"
                if last.outcome == "uncertain"
                else "invoking_blocks_failover",
                last.generation,
            )
        return RouteDecision(
            "terminal_failed",
            last.vendor_id,
            "already_submitted",
            last.generation,
        )

    previous = request.attempts[-1] if request.attempts else None
    if previous is not None and previous.outcome in HOLD_OUTCOMES:
        return RouteDecision(
            "invoke",
            previous.vendor_id,
            "same_vendor_retry",
            previous.generation + 1,
        )
    if previous is not None and previous.outcome == "rejected" and not previous.safe_to_failover:
        return RouteDecision(
            "terminal_failed",
            previous.vendor_id,
            "failover_exhausted",
            previous.generation,
        )

    available = {
        validate_vendor_id(item.vendor_id)
        for item in request.health
        if item.available
    }
    hard_failed = {
        item.vendor_id
        for item in request.attempts
        if item.outcome == "rejected" and not item.safe_to_failover
    }
    safe_rejected = {
        item.vendor_id
        for item in request.attempts
        if item.outcome == "rejected" and item.safe_to_failover
    }
    skipped = {
        item.vendor_id
        for item in request.attempts
        if item.outcome == "not_invoked"
    }
    candidates = [
        vendor_id
        for vendor_id in registered
        if vendor_id in available
        and vendor_id not in hard_failed
        and vendor_id not in safe_rejected
        and vendor_id not in skipped
    ]
    generation = previous.generation + 1 if previous is not None else 1
    if not candidates:
        if previous is not None and previous.outcome == "rejected":
            return RouteDecision(
                "terminal_failed",
                previous.vendor_id,
                "failover_exhausted",
                previous.generation,
            )
        return RouteDecision("exhausted", None, "no_available_vendor", generation)

    chosen = candidates[0]
    if previous is None:
        reason = "primary" if chosen == registered[0] else "pre_invoke_unavailable"
    elif previous.outcome == "rejected" and previous.safe_to_failover:
        reason = "safe_failover"
    else:
        reason = "pre_invoke_unavailable"
    return RouteDecision("invoke", chosen, reason, generation)


class VendorRouter:
    """生产默认只暴露主供应商；测试可注入有序注册表。"""

    def __init__(self, registered: tuple[str, ...] | None = None) -> None:
        vendors = registered or (PRIMARY_VENDOR_ID,)
        self._registered = tuple(validate_vendor_id(item) for item in vendors)

    def registered_ids(self) -> tuple[str, ...]:
        return self._registered

    def decide(self, request: RouteRequest) -> RouteDecision:
        if request.registered:
            return decide(request)
        return decide(
            RouteRequest(
                registered=self._registered,
                attempts=request.attempts,
                health=request.health,
                category=request.category,
                policy_version=request.policy_version,
            )
        )
