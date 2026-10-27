"""Web 发送最终内容与计费分段预览。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.services.approval import requires_approval
from app.services.billing import calculate_quota_cost, calculate_segments, segment_capacity
from app.services.category import SHANGHAI, coerce_market_dispatch
from app.services.pipeline import prepare_content


@dataclass(frozen=True, slots=True)
class SegmentPart:
    used: int
    capacity: int
    partial: bool


@dataclass(frozen=True, slots=True)
class QuotaSummary:
    """部门日配额只读摘要；limit=0 表示不限，此时 remaining 为 None。"""

    used: int
    limit: int
    remaining: int | None


@dataclass(frozen=True, slots=True)
class BillingPreview:
    final_length: int
    est_segments: int
    quota_cost: int
    segment_parts: list[SegmentPart]
    next_segment_at: int
    approval_required: bool
    unsubscribe_appended: bool
    final_content: str
    deferred_reason: str | None = None
    quota: QuotaSummary | None = None


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
    market_window: str = "08:00-21:00",
    now: datetime | None = None,
    quota: QuotaSummary | None = None,
    is_test: bool = False,
) -> BillingPreview:
    """汇总最终内容、计费分段、审批与时间窗判定，供提交前确认。

    final_content 为签名前置且自动追加退订语后的完整下发文本。
    测试发送与正式入队一致：不计审批，也不因营销窗外转为定时。
    deferred_reason 仅评估非测试的"此刻立即发送"是否落在营销窗口外，
    显式定时由提交链路另行裁决；quota 为调用方预先读取的只读摘要，
    投影不可用时传 None，前端降级展示而不阻断预检。
    """

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
    capacity = segment_capacity(segments)
    remaining = length
    parts: list[SegmentPart] = []
    for _ in range(segments):
        used = min(capacity, remaining)
        parts.append(SegmentPart(used, capacity, used < capacity))
        remaining -= used
    next_boundary = segments * capacity + 1
    deferred_reason: str | None = None
    approval_required = False
    if not is_test:
        if category == "market":
            _, deferred_reason, _ = coerce_market_dispatch(
                now or datetime.now(SHANGHAI),
                market_window,
                None,
            )
        approval_required = requires_approval(
            "web",
            category,
            accepted_count,
            notice_threshold=notice_threshold,
            market_threshold=market_threshold,
        )
    return BillingPreview(
        final_length=length,
        est_segments=segments,
        quota_cost=calculate_quota_cost(
            final_content,
            recipient_count=accepted_count,
        ),
        segment_parts=parts,
        next_segment_at=max(1, next_boundary - length),
        approval_required=approval_required,
        unsubscribe_appended=(category == "market" and prepared.send_content != content),
        final_content=final_content,
        deferred_reason=deferred_reason,
        quota=quota,
    )
