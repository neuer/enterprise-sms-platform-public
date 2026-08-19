from datetime import datetime
from zoneinfo import ZoneInfo

from app.services.billing_preview import QuotaSummary, build_billing_preview

SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_preview_uses_final_market_content_and_returns_server_segment_parts() -> None:
    preview = build_billing_preview(
        category="market",
        content="活" * 146,
        sign_name=None,
        accepted_count=10,
        consent_confirmed=True,
        unsubscribe_suffix="回T退订",
        notice_threshold=100,
        market_threshold=50,
    )
    assert preview.est_segments == 3
    assert preview.quota_cost == 30
    assert preview.unsubscribe_appended is True
    assert sum(item.used for item in preview.segment_parts) == 150
    assert [item.capacity for item in preview.segment_parts] == [67, 67, 67]


def test_preview_length_and_parts_include_sign_name() -> None:
    preview = build_billing_preview(
        category="notice",
        content="通" * 67,
        sign_name="【平台】",
        accepted_count=1,
        consent_confirmed=False,
        unsubscribe_suffix="回T退订",
        notice_threshold=100,
        market_threshold=50,
    )
    assert preview.final_length == 71
    assert preview.est_segments == 2
    assert sum(item.used for item in preview.segment_parts) == 71


def test_preview_final_content_assembles_sign_body_and_suffix() -> None:
    preview = build_billing_preview(
        category="market",
        content="全场 8 折",
        sign_name="【平台】",
        accepted_count=3,
        consent_confirmed=True,
        unsubscribe_suffix="回T退订",
        notice_threshold=100,
        market_threshold=50,
    )
    assert preview.final_content == "【平台】全场 8 折回T退订"
    assert preview.final_length == len("【平台】全场 8 折回T退订")
    assert preview.unsubscribe_appended is True


def test_preview_defers_market_outside_send_window() -> None:
    outside = datetime(2026, 8, 19, 22, 30, tzinfo=SHANGHAI)
    inside = datetime(2026, 8, 19, 10, 0, tzinfo=SHANGHAI)
    base = dict(
        content="活动",
        sign_name=None,
        accepted_count=1,
        consent_confirmed=True,
        unsubscribe_suffix="回T退订",
        notice_threshold=100,
        market_threshold=50,
        market_window="08:00-21:00",
    )
    deferred = build_billing_preview(category="market", now=outside, **base)
    assert deferred.deferred_reason == "market_window"
    in_window = build_billing_preview(category="market", now=inside, **base)
    assert in_window.deferred_reason is None
    notice = build_billing_preview(
        category="notice",
        **{**base, "consent_confirmed": False},
        now=outside,
    )
    assert notice.deferred_reason is None


def test_preview_passes_through_quota_summary() -> None:
    quota = QuotaSummary(used=3412, limit=20000, remaining=16588)
    preview = build_billing_preview(
        category="notice",
        content="通知",
        sign_name=None,
        accepted_count=1,
        consent_confirmed=False,
        unsubscribe_suffix="回T退订",
        notice_threshold=100,
        market_threshold=50,
        quota=quota,
    )
    assert preview.quota == quota
    degraded = build_billing_preview(
        category="notice",
        content="通知",
        sign_name=None,
        accepted_count=1,
        consent_confirmed=False,
        unsubscribe_suffix="回T退订",
        notice_threshold=100,
        market_threshold=50,
    )
    assert degraded.quota is None
