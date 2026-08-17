from __future__ import annotations

import pytest

from app.vendor.identifiers import validate_vendor_custom_id, validate_vendor_task_id


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
