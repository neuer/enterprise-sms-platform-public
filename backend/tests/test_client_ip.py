from __future__ import annotations

from types import SimpleNamespace

from app.core.client_ip import trusted_client_ip


def request_with_host(host: str | None):
    if host is None:
        return SimpleNamespace(client=None)
    return SimpleNamespace(client=SimpleNamespace(host=host))


def test_trusted_client_ip_returns_normalized_real_client() -> None:
    assert trusted_client_ip(request_with_host("127.0.0.1")) == "127.0.0.1"
    assert trusted_client_ip(request_with_host("::ffff:10.1.2.3")) == "10.1.2.3"


def test_trusted_client_ip_fails_closed_on_missing_or_invalid_source() -> None:
    assert trusted_client_ip(request_with_host(None)) == "0.0.0.0"
    assert trusted_client_ip(request_with_host("not-an-ip")) == "0.0.0.0"
