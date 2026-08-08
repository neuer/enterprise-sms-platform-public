from __future__ import annotations

from typing import Any, cast

import pytest
from starlette.requests import Request

from app.api.auth import _assert_same_origin
from app.core.errors import ApiError
from app.core.origin import canonical_origin


def _request(
    *,
    scheme: str,
    host: str,
    port: int,
    origin: str | None,
) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if origin is not None:
        headers.append((b"origin", origin.encode("ascii")))
    scope: dict[str, Any] = {
        "type": "http",
        "scheme": scheme,
        "server": (host, port),
        "client": ("192.0.2.9", 1234),
        "headers": headers,
        "method": "POST",
        "path": "/api/v1/web/auth/refresh",
        "query_string": b"",
        "root_path": "",
        "http_version": "1.1",
    }
    return Request(cast(Any, scope))


@pytest.mark.parametrize(
    ("scheme", "host", "port", "expected"),
    [
        ("https", "example.com", 443, ("https", "example.com", 443)),
        ("https", "example.com", 8443, ("https", "example.com", 8443)),
        ("http", "example.com", 80, ("http", "example.com", 80)),
        ("http", "example.com", 8080, ("http", "example.com", 8080)),
    ],
)
def test_canonical_origin_uses_scheme_hostname_and_effective_port(
    scheme: str,
    host: str,
    port: int,
    expected: tuple[str, str, int],
) -> None:
    assert canonical_origin(_request(scheme=scheme, host=host, port=port, origin=None)) == expected


def test_same_origin_accepts_external_https_canonical_origin() -> None:
    request = _request(
        scheme="https",
        host="sms.example.com",
        port=443,
        origin="https://sms.example.com",
    )

    _assert_same_origin(request)


def test_same_origin_rejects_scheme_or_effective_port_mismatch() -> None:
    request = _request(
        scheme="https",
        host="sms.example.com",
        port=443,
        origin="http://sms.example.com",
    )

    with pytest.raises(ApiError, match="非同源"):
        _assert_same_origin(request)
