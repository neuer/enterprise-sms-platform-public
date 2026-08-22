from __future__ import annotations

import pytest

from app.vendor.zhihui import (  # noqa: E402
    RawPulledPayload,
    VendorProtocolError,
    decode_pulled_payload,
)


def decode(raw: bytes):
    return decode_pulled_payload(RawPulledPayload(raw, 200), "GetReport")


def test_single_trailing_space_typo_remains_compatible() -> None:
    result = decode(
        b'{"code":0,"msg":null,"data":[{"customId ":"chunk-1","phone":"13800138000"}]}'
    )
    assert result == [{"customId": "chunk-1", "phone": "13800138000"}]


@pytest.mark.parametrize(
    "payload",
    [
        b'{"code":5002,"code":0,"msg":null,"data":[]}',
        b'{"code":0,"msg":null,"data":[],"data":[]}',
        b'{"code":0,"msg":null,"data":[{"customId":"a","customId ":"b"}]}',
        b'{"code":0,"msg":null,"data":[{"taskId":"a","taskId\\t":"b"}]}',
        b'{"code":0,"msg":null,"data":[{"phone":"a"," phone":"b"}]}',
        b'{"code":0,"msg":null,"data":[{"nested":{"x":1,"x ":2}}]}',
        b'{"code":0,"msg":null,"data":[{"same":1,"same":1}]}',
        b'{"code":0,"msg":null,"data":[{" ":1}]}',
    ],
)
def test_duplicate_or_normalized_collision_is_rejected(payload: bytes) -> None:
    with pytest.raises(
        VendorProtocolError,
        match="duplicate or ambiguous JSON keys",
    ):
        decode(payload)


def test_exception_does_not_echo_untrusted_key() -> None:
    key = "secretName=do-not-log"
    payload = (
        '{"code":0,"msg":null,"data":[{"%s":1," %s ":2}]}' % (key, key)
    ).encode()
    with pytest.raises(VendorProtocolError) as captured:
        decode(payload)
    assert key not in str(captured.value)


def test_valid_field_order_is_irrelevant() -> None:
    first = decode(b'{"code":0,"msg":null,"data":{"taskId":"t","customId ":"c"}}')
    second = decode(b'{"data":{"customId ":"c","taskId":"t"},"msg":null,"code":0}')
    assert first == second == {"taskId": "t", "customId": "c"}


def test_unknown_non_conflicting_fields_remain_available_to_item_validation() -> None:
    result = decode(
        b'{"code":0,"msg":null,"data":[{"customId ":"c","futureField":7}]}'
    )
    assert result == [{"customId": "c", "futureField": 7}]
