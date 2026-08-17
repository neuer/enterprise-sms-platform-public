"""普通文本中的手机号明文检测与等长打码。"""

from __future__ import annotations

import re

PHONE_IN_TEXT = re.compile(r"(?<!\d)1\d{10}(?!\d)")
PHONE_AS_TOKEN = re.compile(r"(?<![0-9A-Za-z])1\d{10}(?![0-9A-Za-z])")
PHONE_REDACTION = "*" * 11


def reject_phone_in_text(value: str | None, *, field_name: str) -> None:
    """拒绝不应承载手机号的普通元数据，错误中不得回显原值。"""

    if value is not None and PHONE_IN_TEXT.search(value):
        raise ValueError(f"{field_name}不得包含手机号")


def reject_phone_identifier(value: str | None, *, field_name: str) -> None:
    """拒绝把规范手机号当作独立标识；允许 hex ID 中的数字碰撞。"""

    if value is not None and PHONE_AS_TOKEN.search(value):
        raise ValueError(f"{field_name}不得包含手机号")


def mask_phone_in_text(value: str | None) -> str | None:
    """对外部自由文本中的规范手机号做等长打码。"""

    if value is None:
        return None
    return PHONE_IN_TEXT.sub(PHONE_REDACTION, value)
