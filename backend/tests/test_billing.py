from __future__ import annotations

import pytest

from app.services.billing import calculate_quota_cost, calculate_segments


@pytest.mark.parametrize(
    ("length", "expected"),
    [(0, 1), (1, 1), (70, 1), (71, 2), (134, 2), (135, 3), (500, 8)],
)
def test_segment_boundaries(length: int, expected: int) -> None:
    assert calculate_segments("测" * length) == expected


def test_unicode_length_uses_python_code_points_not_utf8_bytes() -> None:
    assert len(("验" * 70).encode("utf-8")) > 70
    assert calculate_segments("验" * 70) == 1


def test_quota_cost_reuses_segment_calculation() -> None:
    assert calculate_quota_cost("x" * 135, recipient_count=12) == 36
    assert calculate_quota_cost("", recipient_count=0) == 0


def test_quota_cost_rejects_negative_recipient_count() -> None:
    with pytest.raises(ValueError, match="recipient_count"):
        calculate_quota_cost("通知", recipient_count=-1)
