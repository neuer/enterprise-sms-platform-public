from __future__ import annotations

from app.api.signs import _error as sign_error
from app.api.templates import _error as template_error
from app.vendor.zhihui import VendorApiError


def test_vendor_raw_message_never_crosses_template_or_sign_api_boundary() -> None:
    reflected = "secretKey=reusable-credential content=验证码839204"
    vendor_error = VendorApiError(1002, reflected)

    for error in (sign_error(vendor_error), template_error(vendor_error)):
        assert error.detail == {
            "vendor_code": 1002,
            "vendor_message": "内容格式错误",
        }
        assert reflected not in str(error.detail)
