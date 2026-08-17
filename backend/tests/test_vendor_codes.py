from __future__ import annotations

from app.vendor.codes import policy_for


def test_1010_is_critical_alert_without_queue_pause() -> None:
    policy = policy_for(1010)

    assert policy.pause_queues is False
    assert policy.alert_level == "crit"
    assert policy.retry_delays_s == ()
    assert policy.delay_s is None


def test_unknown_vendor_code_fails_closed_without_automatic_retry() -> None:
    policy = policy_for(987654)

    assert policy.retry_delays_s == ()
    assert policy.delay_s is None
    assert policy.shrink_batch_once is False
