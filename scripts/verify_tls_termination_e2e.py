#!/usr/bin/env python3
"""真实 TLS 终结拓扑 E2E：HTTPS 反向代理 → Web Nginx → Uvicorn。

只使用标准库与临时自签名证书，不发送真实凭据或公网地址。验证：
1. 受信 TLS 终结器后的真实客户端 IP 恢复（Nginx access log 的 $remote_addr）；
2. 外部 HTTPS canonical origin 下 login/refresh/logout 成功，Refresh Cookie 带 Secure；
3. 结束时恢复直连模式并重启 Web，不遗留受信代理配置。
"""

from __future__ import annotations

import argparse
import http.client
import http.server
import json
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))

from render_trusted_proxy_conf import render  # noqa: E402

SIMULATED_CLIENTS = ("203.0.113.7", "203.0.113.8")


class TlsProxyHandler(http.server.BaseHTTPRequestHandler):
    """把 HTTPS 请求转发到 Web Nginx，模拟可信 TLS 终结器。"""

    web_port = 18080

    def _forward(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        connection = http.client.HTTPConnection("127.0.0.1", self.web_port, timeout=15)
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.casefold()
            not in {"connection", "proxy-connection", "transfer-encoding", "content-length"}
        }
        headers["X-Forwarded-For"] = self.headers.get("X-Simulated-Client", "203.0.113.7")
        headers["X-Forwarded-Proto"] = "https"
        try:
            connection.request(
                self.command,
                self.path,
                body=body,
                headers=headers,
            )
            response = connection.getresponse()
            payload = response.read()
            self.send_response(response.status)
            for name, value in response.getheaders():
                if name.casefold() not in {"connection", "transfer-encoding"}:
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        finally:
            connection.close()

    do_GET = _forward
    do_POST = _forward

    def log_message(self, _format: str, *args: object) -> None:
        return


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _start_proxy(web_port: int) -> tuple[threading.Thread, int]:
    port = _free_port()
    TlsProxyHandler.web_port = web_port
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), TlsProxyHandler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    with tempfile.TemporaryDirectory() as directory:
        cert = Path(directory) / "cert.pem"
        key = Path(directory) / "key.pem"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-days",
                "1",
                "-subj",
                "/CN=smslocal",
                "-addext",
                "subjectAltName=DNS:smslocal,IP:127.0.0.1",
            ],
            check=True,
            capture_output=True,
        )
        context.load_cert_chain(str(cert), str(key))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread, port


def _https_request(
    port: int,
    method: str,
    path: str,
    *,
    body: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    context = ssl._create_unverified_context()
    connection = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=15)
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    request_headers = {
        "Host": f"smslocal:{port}",
        "Content-Type": "application/json",
    }
    request_headers.update(headers or {})
    connection.request(method, path, body=payload, headers=request_headers)
    response = connection.getresponse()
    data = response.read()
    result_headers = {name.casefold(): value for name, value in response.getheaders()}
    connection.close()
    return response.status, result_headers, data


def _gateway(project: str) -> str:
    output = subprocess.check_output(
        [
            "docker",
            "network",
            "inspect",
            f"{project}_default",
            "--format",
            "{{(index .IPAM.Config 0).Gateway}}",
        ],
        text=True,
    )
    return output.strip()


def _render_and_restart_web(project: str, mode: str, cidrs: str) -> None:
    content = render(mode, cidrs)
    target = ROOT / "deploy" / "trusted-proxies.conf"
    target.write_text(content, encoding="utf-8")
    subprocess.run(
        ["docker", "restart", f"{project}-web-1"],
        check=True,
        capture_output=True,
    )


def _access_log_tail(project: str, lines: int = 80) -> str:
    return subprocess.check_output(
        [
            "docker",
            "exec",
            f"{project}-web-1",
            "tail",
            "-n",
            str(lines),
            "/var/log/nginx/access.log",
        ],
        text=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--web-port", type=int, required=True)
    parser.add_argument(
        "--mock-password-file",
        default=str(ROOT / "deploy/secrets/ldap_bind_password"),
    )
    arguments = parser.parse_args()
    web_port = arguments.web_port
    mock_password = Path(arguments.mock_password_file).read_text(
        encoding="utf-8"
    ).strip()
    gateway = _gateway(arguments.project)
    proxy_thread: threading.Thread | None = None
    proxy_port = 0
    try:
        _render_and_restart_web(arguments.project, "1", f"{gateway}/32")
        proxy_thread, proxy_port = _start_proxy(web_port)
        login_headers = {
            "X-Simulated-Client": SIMULATED_CLIENTS[0],
            "Origin": f"https://smslocal:{proxy_port}",
        }
        status, headers, body = _https_request(
            proxy_port,
            "POST",
            "/api/v1/web/auth/login",
            body={
                "provider_code": "ad",
                "username": "operator01",
                "password": mock_password,
            },
            headers=login_headers,
        )
        if status != 200:
            raise RuntimeError(f"TLS E2E login failed: {status} {body!r}")
        set_cookie = headers.get("set-cookie", "")
        if "Secure" not in set_cookie:
            raise RuntimeError("TLS E2E refresh cookie missing Secure")
        login_data = json.loads(body.decode("utf-8"))
        access_token = login_data.get("token")
        if not isinstance(access_token, str) or not access_token:
            raise RuntimeError("TLS E2E login omitted access token")

        refresh_headers = {
            "X-Simulated-Client": SIMULATED_CLIENTS[1],
            "Origin": f"https://smslocal:{proxy_port}",
            "Cookie": set_cookie.split(";", maxsplit=1)[0],
        }
        status, _headers, body = _https_request(
            proxy_port,
            "POST",
            "/api/v1/web/auth/refresh",
            headers=refresh_headers,
        )
        if status != 200:
            raise RuntimeError(f"TLS E2E refresh failed: {status} {body!r}")

        status, _headers, body = _https_request(
            proxy_port,
            "POST",
            "/api/v1/web/auth/logout",
            headers={
                "X-Simulated-Client": SIMULATED_CLIENTS[0],
                "Origin": f"https://smslocal:{proxy_port}",
                "Authorization": f"Bearer {access_token}",
            },
        )
        if status != 200:
            raise RuntimeError(f"TLS E2E logout failed: {status} {body!r}")

        log_tail = _access_log_tail(arguments.project)
        for simulated in SIMULATED_CLIENTS:
            if simulated not in log_tail:
                raise RuntimeError(
                    f"TLS E2E real client IP not restored: missing {simulated} in nginx access log"
                )
        print("TLS termination E2E passed: real client IP restored, https canonical origin ok")
        return 0
    finally:
        if proxy_thread is not None:
            proxy_thread.join(timeout=1)
        try:
            _render_and_restart_web(arguments.project, "0", "")
        except Exception:
            print(
                "TLS E2E cleanup failed to restore direct-mode trusted proxy config",
                file=sys.stderr,
            )


if __name__ == "__main__":
    raise SystemExit(main())
