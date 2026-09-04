"""多供应商路由：调用前失败、明确拒绝、超时与回执去重。"""

from __future__ import annotations

import pytest

from app.vendor.routing import (
    PRIMARY_VENDOR_ID,
    ROUTE_POLICY_VERSION,
    RouteRequest,
    VendorAttempt,
    VendorHealth,
    VendorRouter,
    decide,
    default_vendor_registry,
    report_may_apply,
    validate_vendor_id,
)


def _health(*pairs: tuple[str, bool]) -> tuple[VendorHealth, ...]:
    return tuple(VendorHealth(vendor_id, available) for vendor_id, available in pairs)


def _request(
    *,
    attempts: tuple[VendorAttempt, ...] = (),
    health: tuple[VendorHealth, ...] | None = None,
    registered: tuple[str, ...] = (PRIMARY_VENDOR_ID, "secondary"),
) -> RouteRequest:
    return RouteRequest(
        registered=registered,
        attempts=attempts,
        health=health
        or _health((PRIMARY_VENDOR_ID, True), ("secondary", True)),
    )


def test_primary_is_selected_when_available() -> None:
    decision = decide(_request())
    assert decision.action == "invoke"
    assert decision.vendor_id == PRIMARY_VENDOR_ID
    assert decision.reason == "primary"
    assert decision.generation == 1
    assert decision.policy_version == ROUTE_POLICY_VERSION


def test_pre_invoke_unavailability_selects_secondary() -> None:
    decision = decide(
        _request(health=_health((PRIMARY_VENDOR_ID, False), ("secondary", True)))
    )
    assert decision.action == "invoke"
    assert decision.vendor_id == "secondary"
    assert decision.reason == "pre_invoke_unavailable"


def test_explicit_reject_with_safe_to_failover_switches_vendor() -> None:
    decision = decide(
        _request(
            attempts=(
                VendorAttempt(PRIMARY_VENDOR_ID, 1, "rejected", True, vendor_code=1002),
            )
        )
    )
    assert decision.action == "invoke"
    assert decision.vendor_id == "secondary"
    assert decision.reason == "safe_failover"
    assert decision.generation == 2


def test_timeout_uncertain_never_calls_another_vendor() -> None:
    decision = decide(
        _request(
            attempts=(VendorAttempt(PRIMARY_VENDOR_ID, 1, "uncertain", False),)
        )
    )
    assert decision.action == "terminal_uncertain"
    assert decision.reason == "uncertain_blocks_failover"
    assert decision.vendor_id == PRIMARY_VENDOR_ID


def test_submitted_chunk_cannot_be_retried_on_another_vendor() -> None:
    decision = decide(
        _request(
            attempts=(VendorAttempt(PRIMARY_VENDOR_ID, 1, "submitted", False),)
        )
    )
    assert decision.action == "terminal_failed"
    assert decision.reason == "already_submitted"


def test_unsafe_reject_does_not_failover() -> None:
    decision = decide(
        _request(
            attempts=(
                VendorAttempt(PRIMARY_VENDOR_ID, 1, "rejected", False, vendor_code=5001),
            )
        )
    )
    assert decision.action == "terminal_failed"
    assert decision.reason == "failover_exhausted"


def test_same_vendor_retry_invokes_new_generation() -> None:
    decision = decide(
        _request(
            attempts=(
                VendorAttempt(PRIMARY_VENDOR_ID, 1, "retry_scheduled", False, 5002),
            )
        )
    )
    assert decision.action == "invoke"
    assert decision.vendor_id == PRIMARY_VENDOR_ID
    assert decision.reason == "same_vendor_retry"
    assert decision.generation == 2


def test_paused_balance_block_retries_same_vendor_after_resume() -> None:
    decision = decide(
        _request(
            attempts=(VendorAttempt(PRIMARY_VENDOR_ID, 1, "paused", False, 999),)
        )
    )
    assert decision.action == "invoke"
    assert decision.vendor_id == PRIMARY_VENDOR_ID
    assert decision.reason == "same_vendor_retry"
    assert decision.generation == 2


def test_invoking_is_irreversible_uncertain() -> None:
    decision = decide(
        _request(
            attempts=(VendorAttempt(PRIMARY_VENDOR_ID, 1, "invoking", False),)
        )
    )
    assert decision.action == "terminal_uncertain"
    assert decision.reason == "invoking_blocks_failover"


def test_recovery_jitter_does_not_return_to_rejected_primary_on_same_chunk() -> None:
    decision = decide(
        _request(
            attempts=(
                VendorAttempt(PRIMARY_VENDOR_ID, 1, "rejected", True, 1010),
                VendorAttempt("secondary", 2, "retry_scheduled", False, 429),
            ),
            health=_health((PRIMARY_VENDOR_ID, True), ("secondary", True)),
        )
    )
    assert decision.action == "invoke"
    assert decision.vendor_id == "secondary"
    assert decision.generation == 3


def test_unknown_health_is_not_treated_as_available() -> None:
    decision = decide(
        _request(health=_health((PRIMARY_VENDOR_ID, False), ("secondary", False)))
    )
    assert decision.action == "exhausted"
    assert decision.reason == "no_available_vendor"


def test_report_from_non_irreversible_vendor_is_ignored() -> None:
    assert report_may_apply(
        report_vendor_id=PRIMARY_VENDOR_ID,
        selected_vendor="secondary",
        irreversible_vendors=frozenset({"secondary"}),
    ) is False
    assert report_may_apply(
        report_vendor_id="secondary",
        selected_vendor="secondary",
        irreversible_vendors=frozenset({"secondary"}),
    ) is True
    assert report_may_apply(
        report_vendor_id=PRIMARY_VENDOR_ID,
        selected_vendor=PRIMARY_VENDOR_ID,
        irreversible_vendors=frozenset(),
    ) is True


def test_production_registry_only_enables_zhihui() -> None:
    records = default_vendor_registry()
    assert [item.vendor_id for item in records] == [PRIMARY_VENDOR_ID]
    assert all(item.enabled for item in records)
    assert records[0].token_bucket_key == "ratelimit:vendor"
    assert records[0].credential_domain == "zhihui"


def test_vendor_id_rejects_secrets_and_phones() -> None:
    with pytest.raises(ValueError, match="vendor_id is invalid"):
        validate_vendor_id("secretKey")
    with pytest.raises(ValueError, match="vendor_id is invalid"):
        validate_vendor_id("13800138000")


def test_router_defaults_to_primary_only() -> None:
    router = VendorRouter()
    decision = router.decide(
        RouteRequest(
            registered=(),
            attempts=(),
            health=_health((PRIMARY_VENDOR_ID, True), ("secondary", True)),
        )
    )
    assert decision.vendor_id == PRIMARY_VENDOR_ID
    assert router.registered_ids() == (PRIMARY_VENDOR_ID,)
