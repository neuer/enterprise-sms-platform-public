from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))

from render_trusted_proxy_conf import render  # noqa: E402


def test_direct_mode_never_trusts_client_proxy_headers(tmp_path: Path) -> None:
    content = render("0", "")

    assert "geo $realip_remote_addr $sms_trusted_proxy" in content
    assert "default 0;" in content
    assert "set_real_ip_from" not in content
    assert "geo $realip_remote_addr $sms_plaintext_client_allowed" in content
    assert "default 1;" in content


def test_external_tls_mode_renders_exact_host_trust_contract(tmp_path: Path) -> None:
    content = render("1", "192.0.2.10/32,2001:db8::10/128")

    assert "192.0.2.10/32 1;" in content
    assert "2001:db8::10/128 1;" in content
    assert "set_real_ip_from 192.0.2.10/32;" in content
    assert "set_real_ip_from 2001:db8::10/128;" in content
    assert "real_ip_header X-Forwarded-For;" in content
    assert "real_ip_recursive on;" in content
    assert "geo $realip_remote_addr $sms_plaintext_client_allowed" in content
    assert "default 0;" in content


@pytest.mark.parametrize(
    "cidrs",
    [
        "",
        "0.0.0.0/0",
        "::/0",
        "10.0.0.0/16",
        "198.51.100.0/24",
        "198.51.100.0/28",
        "2001:db8::/64",
        "2001:db8::/120",
        "not-a-cidr",
    ],
)
def test_external_tls_mode_fails_closed_on_missing_or_too_broad_cidrs(
    cidrs: str,
) -> None:
    with pytest.raises(ValueError):
        render("1", cidrs)
