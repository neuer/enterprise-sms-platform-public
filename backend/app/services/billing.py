"""最终短信内容的唯一计费条计算实现。"""

from __future__ import annotations

SINGLE_SEGMENT_CHARS = 70
MULTI_SEGMENT_CHARS = 67


def calculate_segments(final_content: str) -> int:
    """按最终内容字符数计算计费条：70 内一条，之后每 67 字一条。"""

    length = len(final_content)
    if length <= SINGLE_SEGMENT_CHARS:
        return 1
    return (length + MULTI_SEGMENT_CHARS - 1) // MULTI_SEGMENT_CHARS


def segment_capacity(segments: int) -> int:
    """返回给定计费条数下每条可容纳的字符数，供预览展示复用同一口径。"""

    if segments < 1:
        raise ValueError("segments must be positive")
    return SINGLE_SEGMENT_CHARS if segments == 1 else MULTI_SEGMENT_CHARS


def calculate_quota_cost(final_content: str, *, recipient_count: int) -> int:
    """复用唯一分段口径计算批次配额消耗。"""

    if recipient_count < 0:
        raise ValueError("recipient_count must be non-negative")
    return calculate_segments(final_content) * recipient_count
