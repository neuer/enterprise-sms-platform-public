"""厂商标识符的统一入站合同，禁止借普通元数据持久化敏感文本。"""

from __future__ import annotations

import re
from typing import Protocol

from app.core.sensitive_text import reject_phone_in_text

VENDOR_TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
VENDOR_CUSTOM_ID = re.compile(r"^[A-Za-z0-9]{0,36}$")
VENDOR_EXT_CODE = re.compile(r"^[0-9]{0,6}$")
VENDOR_IDENTIFIER_PSEUDONYM = re.compile(r"^[0-9a-f]{64}$")


class IdentifierFingerprint(Protocol):
    def stable_hmac_fingerprint(
        self,
        canonical_value: bytes,
        *,
        domain: str,
    ) -> tuple[int, str]: ...


def vendor_identifier_pseudonym(
    crypto: IdentifierFingerprint,
    value: str,
    *,
    domain: str,
) -> str:
    """把厂商原值变为不可离线枚举、可稳定关联的非敏感伪标识。"""

    _version, digest = crypto.stable_hmac_fingerprint(
        value.encode("utf-8"),
        domain=domain,
    )
    if VENDOR_IDENTIFIER_PSEUDONYM.fullmatch(digest) is None:
        raise ValueError("vendor identifier fingerprint is invalid")
    return digest


def validate_vendor_task_id(value: object) -> str:
    """验证任务标识；错误不得回显厂商原值。"""

    if not isinstance(value, str):
        raise ValueError("taskId must be a string")
    normalized = value.strip()
    if VENDOR_TASK_ID.fullmatch(normalized) is None:
        raise ValueError("taskId format is invalid")
    reject_phone_in_text(normalized, field_name="taskId")
    return normalized


def validate_vendor_custom_id(value: object) -> str:
    """只接受厂商合同内的字母数字 customId，空值用于旧发送。"""

    if not isinstance(value, str):
        raise ValueError("customId must be a string")
    normalized = value.strip()
    if VENDOR_CUSTOM_ID.fullmatch(normalized) is None:
        raise ValueError("customId format is invalid")
    reject_phone_in_text(normalized, field_name="customId")
    return normalized


def validate_vendor_ext_code(value: object) -> str:
    """扩展号遵循厂商的 0..6 位数字合同。"""

    if not isinstance(value, str) or VENDOR_EXT_CODE.fullmatch(value) is None:
        raise ValueError("extCode must be at most 6 digits")
    return value


def protect_vendor_task_id(crypto: IdentifierFingerprint, value: object) -> tuple[str, str]:
    """返回仅内存匹配原值与可持久化任务伪标识。"""

    raw = validate_vendor_task_id(value)
    return raw, vendor_identifier_pseudonym(crypto, raw, domain="vendor-task-id")


def protect_vendor_custom_id(
    crypto: IdentifierFingerprint,
    value: object,
) -> tuple[str, str]:
    """返回仅内存匹配原值与可持久化 customId 伪标识。"""

    raw = validate_vendor_custom_id(value)
    return raw, vendor_identifier_pseudonym(crypto, raw, domain="vendor-custom-id")
