import pytest

from app.services.sign import SignNotApproved, format_sign_name


def test_sign_name_is_normalized_once_for_billing_and_vendor() -> None:
    assert format_sign_name("青鸾平台") == "【青鸾平台】"
    assert format_sign_name("【青鸾平台】") == "【青鸾平台】"


@pytest.mark.parametrize("name", ["", " ", "【】", "青【鸾", "青鸾】平台", "签" * 21])
def test_invalid_sign_name_is_rejected(name: str) -> None:
    with pytest.raises(ValueError):
        format_sign_name(name)


def test_unapproved_sign_has_explicit_domain_error() -> None:
    assert str(SignNotApproved("签名未审核通过")) == "签名未审核通过"
