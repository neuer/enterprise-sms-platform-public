#!/usr/bin/env python3
"""验证生产 Web 的 HTTP 跳转、TLS、HSTS 与 CSP 边界；不发送任何凭据。"""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from http.client import HTTPMessage
from typing import IO
from urllib.parse import urljoin, urlsplit

MIN_HSTS_SECONDS = 31_536_000
ALLOWED_TLS_VERSIONS = frozenset({"TLSv1.2", "TLSv1.3"})
UNSAFE_WEB_BIND_IPS = frozenset({"0.0.0.0", "::", "[::]", ""})


class TransportProbeError(RuntimeError):
    """表示公开入口没有满足生产传输安全合同。"""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """保留第一跳响应，避免客户端自动跟随掩盖明文入口错误。"""

    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: IO[bytes],
        code: int,
        message: str,
        headers: HTTPMessage,
        new_url: str,
    ) -> None:
        return None


@dataclass(frozen=True, slots=True)
class TransportEvidence:
    """不含凭据的生产传输探针摘要。"""

    redirect_status: int
    tls_version: str
    certificate_days_remaining: int
    hsts_max_age: int


def _origin(raw: str, *, scheme: str) -> tuple[str, int, str]:
    parsed = urlsplit(raw)
    if (
        parsed.scheme != scheme
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise TransportProbeError(f"invalid {scheme} probe URL")
    port = parsed.port or (443 if scheme == "https" else 80)
    return parsed.hostname, port, f"{parsed.scheme}://{parsed.netloc}"


def validate_redirect(
    *,
    status: int,
    location: str | None,
    http_base: str,
    https_base: str,
) -> None:
    """要求明文入口只能跳到配置的同主机 HTTPS origin。"""

    if status not in {301, 302, 307, 308} or not location:
        raise TransportProbeError("HTTP entry did not return an HTTPS redirect")
    target = urlsplit(urljoin(http_base, location))
    https_host, https_port, _ = _origin(https_base, scheme="https")
    target_port = target.port or 443
    if target.scheme != "https" or target.hostname != https_host or target_port != https_port:
        raise TransportProbeError("HTTP redirect target is outside the HTTPS host")


def validate_browser_headers(headers: Mapping[str, str]) -> int:
    """验证最终 HTTPS 响应的 HSTS 与收紧后的浏览器策略。"""

    normalized = {key.casefold(): value for key, value in headers.items()}
    hsts = normalized.get("strict-transport-security", "")
    directives = {item.strip().casefold() for item in hsts.split(";") if item.strip()}
    max_age = 0
    for directive in directives:
        if directive.startswith("max-age="):
            try:
                max_age = int(directive.partition("=")[2])
            except ValueError as error:
                raise TransportProbeError("invalid HSTS max-age") from error
    if max_age < MIN_HSTS_SECONDS or "includesubdomains" not in directives:
        raise TransportProbeError("HSTS policy is missing or too weak")

    csp = normalized.get("content-security-policy", "")
    csp_directives = {
        item.strip().partition(" ")[0]: item.strip() for item in csp.split(";") if item.strip()
    }
    required = {
        "default-src": "default-src 'self'",
        "script-src": "script-src 'self'",
        "script-src-attr": "script-src-attr 'none'",
        "style-src": "style-src 'self'",
        "style-src-attr": "style-src-attr 'unsafe-inline'",
        "object-src": "object-src 'none'",
        "frame-src": "frame-src 'none'",
        "worker-src": "worker-src 'none'",
        "frame-ancestors": "frame-ancestors 'none'",
        "form-action": "form-action 'self'",
    }
    if any(csp_directives.get(name) != expected for name, expected in required.items()):
        raise TransportProbeError("CSP policy is missing required directives")
    if "'unsafe-inline'" in csp_directives.get("script-src", ""):
        raise TransportProbeError("CSP permits inline scripts")
    if "'unsafe-inline'" in csp_directives.get("style-src", ""):
        raise TransportProbeError("CSP permits inline style blocks")

    expected_headers = {
        "x-content-type-options": "nosniff",
        "x-frame-options": "DENY",
        "referrer-policy": "no-referrer",
    }
    if any(
        normalized.get(name, "").casefold() != value.casefold()
        for name, value in expected_headers.items()
    ):
        raise TransportProbeError("browser security headers are incomplete")
    return max_age


def validate_web_bind_ip(bind_ip: str, *, production: bool = True) -> str:
    """校验 Web 明文上游宿主绑定；生产模式禁止全网卡绑定。"""

    value = bind_ip.strip()
    if not value:
        raise TransportProbeError("WEB_BIND_IP must not be empty")
    if value in UNSAFE_WEB_BIND_IPS:
        raise TransportProbeError("WEB_BIND_IP must not bind all host interfaces")
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise TransportProbeError("WEB_BIND_IP is not a valid IPv4/IPv6 address") from error
    if production and not (address.is_loopback or address.is_private):
        raise TransportProbeError("WEB_BIND_IP must be loopback or a private network address")
    return bind_ip.strip()


def _probe_redirect(http_base: str, https_base: str, *, timeout_s: float) -> int:
    request = urllib.request.Request(urljoin(http_base.rstrip("/") + "/", "login"), method="GET")
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=timeout_s) as response:
            status = int(response.status)
            location = response.headers.get("Location")
    except urllib.error.HTTPError as error:
        status = error.code
        location = error.headers.get("Location")
    validate_redirect(
        status=status,
        location=location,
        http_base=http_base,
        https_base=https_base,
    )
    return status


def _probe_https(https_base: str, *, timeout_s: float) -> tuple[Mapping[str, str], str, int]:
    host, port, _ = _origin(https_base, scheme="https")
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    request = urllib.request.Request(
        urljoin(https_base.rstrip("/") + "/", "login"),
        method="GET",
        headers={"Accept": "text/html"},
    )
    with urllib.request.urlopen(request, timeout=timeout_s, context=context) as response:
        if response.status != 200 or urlsplit(response.url).scheme != "https":
            raise TransportProbeError("HTTPS login endpoint is not directly available")
        headers = dict(response.headers.items())
    with (
        socket.create_connection((host, port), timeout=timeout_s) as raw_socket,
        context.wrap_socket(raw_socket, server_hostname=host) as tls_socket,
    ):
        tls_version = tls_socket.version() or ""
        certificate = tls_socket.getpeercert()
    if tls_version not in ALLOWED_TLS_VERSIONS:
        raise TransportProbeError("TLS version is below the supported baseline")
    if certificate is None:
        raise TransportProbeError("TLS certificate is unavailable")
    not_after = certificate.get("notAfter")
    if not isinstance(not_after, str):
        raise TransportProbeError("TLS certificate expiry is unavailable")
    days_remaining = int((ssl.cert_time_to_seconds(not_after) - time.time()) // 86_400)
    return headers, tls_version, days_remaining


def run_probe(
    *,
    http_base: str,
    https_base: str,
    min_certificate_days: int,
    timeout_s: float,
) -> TransportEvidence:
    """执行不携带 Cookie、JWT 或密码的生产入口探针。"""

    if min_certificate_days < 1 or timeout_s <= 0:
        raise TransportProbeError("probe bounds must be positive")
    _origin(http_base, scheme="http")
    _origin(https_base, scheme="https")
    redirect_status = _probe_redirect(http_base, https_base, timeout_s=timeout_s)
    headers, tls_version, days_remaining = _probe_https(https_base, timeout_s=timeout_s)
    if days_remaining < min_certificate_days:
        raise TransportProbeError("TLS certificate is too close to expiry")
    hsts_max_age = validate_browser_headers(headers)
    return TransportEvidence(
        redirect_status=redirect_status,
        tls_version=tls_version,
        certificate_days_remaining=days_remaining,
        hsts_max_age=hsts_max_age,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--http-base", required=True)
    parser.add_argument("--https-base", required=True)
    parser.add_argument(
        "--web-bind-ip",
        default=None,
        help="Web 明文上游宿主绑定地址；生产模式必须为回环或显式批准的私网地址",
    )
    parser.add_argument("--min-certificate-days", type=int, default=14)
    parser.add_argument("--timeout-s", type=float, default=10)
    args = parser.parse_args(argv)
    evidence = run_probe(
        http_base=args.http_base,
        https_base=args.https_base,
        min_certificate_days=args.min_certificate_days,
        timeout_s=args.timeout_s,
    )
    if args.web_bind_ip is not None:
        validate_web_bind_ip(args.web_bind_ip)
    print(
        json.dumps(
            {
                "status": "verified",
                "redirect_status": evidence.redirect_status,
                "tls_version": evidence.tls_version,
                "certificate_days_remaining": evidence.certificate_days_remaining,
                "hsts_max_age": evidence.hsts_max_age,
                "web_bind_ip": args.web_bind_ip,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TransportProbeError as error:
        print(json.dumps({"status": "failed", "error": str(error)}, sort_keys=True))
        raise SystemExit(1) from None
