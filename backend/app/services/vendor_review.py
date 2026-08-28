"""模板与签名共用的厂商审核状态响应校验。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.sensitive_text import mask_phone_in_text

MAX_VENDOR_ID = 2_147_483_647
VENDOR_REVIEW_STATES = frozenset({"pending", "approved", "rejected"})


@dataclass(frozen=True, slots=True)
class VendorReview:
    state: str
    reject_reason: str | None


def map_vendor_review_state(check_type: int, *, object_name: str) -> str:
    """把厂商三态转换为平台状态，未知值一律拒绝。"""

    if not isinstance(check_type, int) or isinstance(check_type, bool):
        raise ValueError(f"unknown {object_name} checkType: {check_type}")
    try:
        return {0: "pending", 1: "approved", 2: "rejected"}[check_type]
    except KeyError:
        raise ValueError(f"unknown {object_name} checkType: {check_type}") from None


def normalize_vendor_reject_reason(state: str, remark: str | None) -> str | None:
    """仅拒绝态保留已打码且满足数据库长度上限的厂商说明。"""

    if state not in VENDOR_REVIEW_STATES:
        raise ValueError("invalid vendor review state")
    if state != "rejected" or remark is None:
        return None
    masked = mask_phone_in_text(remark)
    assert masked is not None
    return masked[:256]


def persisted_vendor_id(value: str, *, operation: str) -> int:
    """把本地持久化的厂商编号规范化为厂商 API 所需正整数。"""

    try:
        vendor_id = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"invalid local {operation} id") from None
    if vendor_id <= 0 or vendor_id > MAX_VENDOR_ID:
        raise ValueError(f"invalid local {operation} id")
    return vendor_id


def returned_vendor_id(value: int, *, operation: str) -> int:
    """校验 Bind* 返回的厂商编号。"""

    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value <= 0
        or value > MAX_VENDOR_ID
    ):
        raise ValueError(f"invalid {operation} id")
    return value


def validated_vendor_reviews(
    response: list[dict[str, Any]],
    requested_ids: list[int],
    *,
    operation: str,
    object_name: str,
) -> dict[int, VendorReview]:
    """验证批量响应与请求一一对应，完整通过后才交给仓储写回。"""

    requested = set(requested_ids)
    reviews: dict[int, VendorReview] = {}
    for item in response:
        vendor_id = item.get("id")
        check_type = item.get("checkType")
        remark = item.get("checkRemark")
        if (
            not isinstance(vendor_id, int)
            or isinstance(vendor_id, bool)
            or not isinstance(check_type, int)
            or isinstance(check_type, bool)
            or (remark is not None and not isinstance(remark, str))
        ):
            raise ValueError(f"invalid {operation} item")
        if vendor_id not in requested:
            raise ValueError(f"unexpected {operation} id")
        if vendor_id in reviews:
            raise ValueError(f"duplicate {operation} id")
        state = map_vendor_review_state(check_type, object_name=object_name)
        reviews[vendor_id] = VendorReview(
            state,
            normalize_vendor_reject_reason(state, remark),
        )
    if reviews.keys() != requested:
        raise ValueError(f"incomplete {operation} response")
    return reviews
