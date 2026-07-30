"""OTP 与文本手机号在持久化、外发展示边界的统一打码。"""

from __future__ import annotations

import re

OTP_PATTERN = re.compile(r"(?<!\d)\d{4,8}(?!\d)")
PHONE_IN_TEXT_PATTERN = re.compile(r"(?<!\d)(1\d{10})(?!\d)")


def mask_verify_otp(content: str) -> str:
    """把独立的 4–8 位连续数字替换为等长星号。"""

    return OTP_PATTERN.sub(lambda match: "*" * len(match.group()), content)


def mask_phone_text(content: str) -> str:
    """把文本中独立的 11 位手机号替换为统一掩码，保持原字符串长度。"""

    return PHONE_IN_TEXT_PATTERN.sub(
        lambda match: f"{match.group(1)[:3]}****{match.group(1)[-4:]}",
        content,
    )
