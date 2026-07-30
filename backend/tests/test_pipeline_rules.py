from __future__ import annotations

import pytest

from app.services.category import CategoryNotAllowed, policy_for_category
from app.services.masking import mask_phone_text, mask_verify_otp
from app.services.pipeline import ConsentRequired, prepare_content


def test_category_policy_is_the_only_queue_and_blacklist_matrix() -> None:
    verify = policy_for_category("verify", {"verify", "notice"})
    notice = policy_for_category("notice", {"verify", "notice"}, notice_blacklist=False)
    market = policy_for_category("market", {"market"})

    assert (verify.queue, verify.blacklist_required) == ("realtime", False)
    assert (notice.queue, notice.blacklist_required) == ("realtime", False)
    assert (market.queue, market.blacklist_required) == ("bulk", True)
    with pytest.raises(CategoryNotAllowed):
        policy_for_category("market", {"notice"})


def test_market_suffix_is_appended_before_billing_and_not_duplicated() -> None:
    prepared = prepare_content(
        category="market",
        channel="api",
        rendered_content="新品发布",
        sign_name="【青鸾】",
        unsubscribe_suffix="回T退订",
        unsubscribe_auto_append=True,
        consent_confirmed=False,
        verify_otp_mask=True,
    )
    assert prepared.send_content == "新品发布回T退订"
    assert prepared.persisted_content == prepared.send_content
    assert prepared.segments == 1

    existing = prepare_content(
        category="market",
        channel="api",
        rendered_content="新品发布回T退订",
        sign_name=None,
        unsubscribe_suffix="回T退订",
        unsubscribe_auto_append=True,
        consent_confirmed=False,
        verify_otp_mask=True,
    )
    assert existing.send_content.count("回T退订") == 1


def test_web_market_requires_consent_but_api_market_does_not() -> None:
    with pytest.raises(ConsentRequired):
        prepare_content(
            category="market",
            channel="web",
            rendered_content="活动通知",
            sign_name=None,
            unsubscribe_suffix="回T退订",
            unsubscribe_auto_append=True,
            consent_confirmed=False,
            verify_otp_mask=True,
        )


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("验证码1234", "验证码****"),
        ("验证码12345678，有效5分钟", "验证码********，有效5分钟"),
        ("订单123与流水123456789", "订单123与流水123456789"),
        ("前123456后，另9876", "前******后，另****"),
    ],
)
def test_verify_otp_mask_is_equal_length_and_only_matches_four_to_eight_digits(
    content: str,
    expected: str,
) -> None:
    assert mask_verify_otp(content) == expected
    assert len(mask_verify_otp(content)) == len(content)


def test_verify_uses_original_for_send_and_masked_value_for_persistence() -> None:
    prepared = prepare_content(
        category="verify",
        channel="api",
        rendered_content="验证码123456",
        sign_name="【青鸾】",
        unsubscribe_suffix="回T退订",
        unsubscribe_auto_append=True,
        consent_confirmed=False,
        verify_otp_mask=True,
    )
    assert prepared.send_content == "验证码123456"
    assert prepared.persisted_content == "验证码******"
    assert len(prepared.send_content) == len(prepared.persisted_content)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("联系13800138000处理", "联系138****8000处理"),
        ("13800138000，备用13900139000。", "138****8000，备用139****9000。"),
        ("订单9138001380001和138001380001不应误判", "订单9138001380001和138001380001不应误判"),
    ],
)
def test_phone_text_masker_only_replaces_standalone_mobile_numbers(
    content: str,
    expected: str,
) -> None:
    assert mask_phone_text(content) == expected


def test_phone_in_send_content_is_preserved_only_for_send_and_masked_for_persistence() -> None:
    prepared = prepare_content(
        category="notice",
        channel="api",
        rendered_content="请确认手机号13800138000",
        sign_name="【青鸾】",
        unsubscribe_suffix="回T退订",
        unsubscribe_auto_append=True,
        consent_confirmed=False,
        verify_otp_mask=True,
    )

    assert prepared.send_content == "请确认手机号13800138000"
    assert prepared.persisted_content == "请确认手机号138****8000"
