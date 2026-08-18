from __future__ import annotations

import base64

import pytest

from app.services.crypto import CryptoService
from app.vendor.identifiers import (
    protect_vendor_custom_id,
    validate_vendor_custom_id,
    validate_vendor_task_id,
)


@pytest.mark.parametrize(
    "value",
    [
        "13800138000",
        " 13800138000 ",
    ],
)
def test_vendor_identifiers_reject_exact_phone(value: str) -> None:
    with pytest.raises(ValueError, match="不得包含手机号"):
        validate_vendor_custom_id(value)
    with pytest.raises(ValueError, match="不得包含手机号"):
        validate_vendor_task_id(value)


def test_vendor_task_id_rejects_delimited_phone() -> None:
    with pytest.raises(ValueError, match="不得包含手机号"):
        validate_vendor_task_id("task-13800138000")


@pytest.mark.parametrize(
    "value",
    [
        "390d6892939546adb08dc16600000001",
        "aaaaaaaaaaaaaaaaaaaaaaaa00000001",
        "custom1",
    ],
)
def test_vendor_custom_id_allows_hex_digit_collision(value: str) -> None:
    assert validate_vendor_custom_id(value) == value


@pytest.mark.parametrize("value", ["", "  "])
def test_protect_vendor_custom_id_returns_empty_pair_without_fingerprint(value: str) -> None:
    """厂商合同允许空 customId（旧发送/无关联上行），不得对空字节做 HMAC 抛错。"""

    key = base64.b64encode(b"u" * 32).decode()
    crypto = CryptoService.from_secret_values(key, key)
    assert protect_vendor_custom_id(crypto, value) == ("", "")
