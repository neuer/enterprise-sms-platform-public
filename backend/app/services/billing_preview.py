"""Web 发送最终内容与计费分段预览。"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.approval import requires_approval
from app.services.billing import calculate_quota_cost, calculate_segments
from app.services.pipeline import prepare_content


@dataclass(frozen=True, slots=True)
class SegmentPart:
    used: int
    capacity: int
    partial: bool


@dataclass(frozen=True, slots=True)
class BillingPreview:
    final_length: int
    est_segments: int
    quota_cost: int
    segment_parts: list[SegmentPart]
    next_segment_at: int
    approval_required: bool
    unsubscribe_appended: bool
    deferred_reason: str | None = None


def build_billing_preview(
    *,
    category: str,
    content: str,
    sign_name: str | None,
    accepted_count: int,
    consent_confirmed: bool,
    unsubscribe_suffix: str,
    unsubscribe_auto_append: bool = True,
    verify_otp_mask: bool = True,
    notice_threshold: int,
    market_threshold: int,
) -> BillingPreview:
    prepared = prepare_content(
        category=category,
        channel="web",
        rendered_content=content,
        sign_name=sign_name,
        unsubscribe_suffix=unsubscribe_suffix,
        unsubscribe_auto_append=unsubscribe_auto_append,
        consent_confirmed=consent_confirmed,
        verify_otp_mask=verify_otp_mask,
    )
    final_content = f"{sign_name or ''}{prepared.send_content}"
    length = len(final_content)
    segments = calculate_segments(final_content)
    capacity = 70 if segments == 1 else 67
    remaining = length
    parts: list[SegmentPart] = []
    for _ in range(segments):
        used = min(capacity, remaining)
        parts.append(SegmentPart(used, capacity, used < capacity))
        remaining -= used
    next_boundary = 71 if segments == 1 else segments * 67 + 1
    return BillingPreview(
        final_length=length,
        est_segments=segments,
        quota_cost=calculate_quota_cost(
            final_content,
            recipient_count=accepted_count,
        ),
        segment_parts=parts,
        next_segment_at=max(1, next_boundary - length),
        approval_required=requires_approval(
            "web",
            category,
            accepted_count,
            notice_threshold=notice_threshold,
            market_threshold=market_threshold,
        ),
        unsubscribe_appended=(category == "market" and prepared.send_content != content),
    )
