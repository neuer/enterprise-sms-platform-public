"""最终短信内容的唯一计费条计算实现。"""

from __future__ import annotations


def calculate_segments(final_content: str) -> int:
    """按最终内容字符数计算计费条：70 内一条，之后每 67 字一条。"""

    length = len(final_content)
    if length <= 70:
        return 1
    return (length + 66) // 67


def calculate_quota_cost(final_content: str, *, recipient_count: int) -> int:
    """复用唯一分段口径计算批次配额消耗。"""

    if recipient_count < 0:
        raise ValueError("recipient_count must be non-negative")
    return calculate_segments(final_content) * recipient_count
