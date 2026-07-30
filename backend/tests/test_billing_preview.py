from app.services.billing_preview import build_billing_preview


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
