from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from verify_web_transport import (  # noqa: E402
    TransportProbeError,
    validate_browser_headers,
    validate_redirect,
    validate_web_bind_ip,
)


def secure_headers() -> dict[str, str]:
    return {
        "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self'; script-src-attr 'none'; "
            "style-src 'self'; style-src-attr 'unsafe-inline'; font-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; media-src 'none'; "
            "object-src 'none'; frame-src 'none'; worker-src 'none'; "
            "manifest-src 'self'; base-uri 'self'; frame-ancestors 'none'; "
            "form-action 'self'"
        ),
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
    }


def test_transport_contract_accepts_same_host_https_redirect_and_headers() -> None:
    validate_redirect(
        status=308,
        location="https://sms.example.test/login",
        http_base="http://sms.example.test",
        https_base="https://sms.example.test",
    )
    assert validate_browser_headers(secure_headers()) == 31_536_000


@pytest.mark.parametrize(
    ("status", "location"),
    [
        (200, None),
        (302, "http://sms.example.test/login"),
        (302, "https://attacker.example/login"),
        (302, "https://sms.example.test:8443/login"),
    ],
)
def test_transport_contract_rejects_bypassable_http_entry(
    status: int,
    location: str | None,
) -> None:
    with pytest.raises(TransportProbeError):
        validate_redirect(
            status=status,
            location=location,
            http_base="http://sms.example.test",
            https_base="https://sms.example.test",
        )


@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("Strict-Transport-Security", "max-age=300"),
        (
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'",
        ),
        ("X-Frame-Options", "SAMEORIGIN"),
    ],
)
def test_transport_contract_rejects_weak_browser_policy(
    header: str,
    value: str,
) -> None:
    headers = secure_headers()
    headers[header] = value
    with pytest.raises(TransportProbeError):
        validate_browser_headers(headers)


@pytest.mark.parametrize(
    "bind_ip",
    ["0.0.0.0", "::", "[::]", ""],
)
def test_production_web_bind_rejects_unsafe_interfaces(bind_ip: str) -> None:
    with pytest.raises(TransportProbeError):
        validate_web_bind_ip(bind_ip)


def test_production_web_bind_accepts_loopback_and_explicit_private_address() -> None:
    assert validate_web_bind_ip("127.0.0.1") == "127.0.0.1"
    assert validate_web_bind_ip("::1") == "::1"
    assert validate_web_bind_ip("10.0.0.5", external_tls_mode=True) == "10.0.0.5"


def test_production_direct_mode_rejects_private_remote_bind() -> None:
    with pytest.raises(TransportProbeError):
        validate_web_bind_ip("10.0.0.5")


def test_production_web_bind_rejects_public_interface() -> None:
    with pytest.raises(TransportProbeError):
        validate_web_bind_ip(".".join(["8", "8", "8", "8"]))
