#!/usr/bin/env python3
"""真实 Nginx 并发/代理头回归；独立无外网容器、无宿主发布端口、仅合成 HTTP。"""

from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def client(mode: str) -> None:
    """慢请求体占槽后检查另一来源、共享 NAT、全局和导入预算。"""
    held: list[socket.socket] = []

    def connect(ip: str) -> socket.socket:
        connection = socket.socket()
        connection.settimeout(2)
        connection.bind((ip, 0))
        connection.connect(("127.0.0.1", 8080))
        return connection

    def hold(ip: str, forwarded: str = "", path: str = "/api/probe") -> int:
        connection = connect(ip)
        connection.sendall((
            f"POST {path} HTTP/1.1\r\nHost: test\r\nContent-Length: 10000\r\n"
            f"X-Forwarded-For: {forwarded}\r\n\r\nx"
        ).encode())
        connection.settimeout(0.05)
        try:
            data = connection.recv(100)
            connection.close()
            return int(data.split()[1]) if data else 444
        except TimeoutError:
            held.append(connection)
            return 0

    def get(ip: str, forwarded: str = "") -> tuple[int, str]:
        with connect(ip) as connection:
            connection.sendall((
                "GET /api/probe HTTP/1.1\r\nHost: test\r\n"
                f"X-Forwarded-For: {forwarded}\r\nX-Forwarded-Proto: https\r\n"
                "Connection: close\r\n\r\n"
            ).encode())
            output = b""
            while data := connection.recv(2048):
                output += data
            return (int(output.split()[1]), output.decode()) if output else (444, "")

    def clear() -> None:
        for connection in held:
            connection.close()
        held.clear()
        time.sleep(0.15)

    deadline = time.monotonic() + 10
    while True:
        try:
            with connect("127.0.0.2"):
                break
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)
    results: dict[str, str] = {}
    try:
        if mode == "direct":
            outcomes = [hold("127.0.0.2", f"192.0.2.{i + 1}") for i in range(16)]
            assert outcomes.count(0) == 8 and outcomes.count(503) == 8, outcomes
            assert get("127.0.0.3")[0] == 200
            assert get("127.0.0.2")[0] == 503
            results["single_source_and_nat"] = "8 held, other source 200, same NAT 503"
            clear()
            for ip in ("127.0.0.2", "127.0.0.3"):
                assert [hold(ip) for _ in range(8)] == [0] * 8
            assert get("127.0.0.4")[0] == 503
            results["global"] = "16 held, third source 503"
            clear()
            path = "/api/v1/web/messages/import"
            assert hold("127.0.0.2", path=path) == 0
            assert hold("127.0.0.3", path=path) == 0
            assert hold("127.0.0.4", path=path) == 503
            assert get("127.0.0.4")[0] == 200
            results["import"] = "2 held, third import 503, ordinary request 200"
            clear()
            status, body = get("127.0.0.2", "192.0.2.100")
            assert status == 200 and "127.0.0.2|http" in body, body
            results["untrusted_headers"] = "ignored"
        else:
            outcomes = [hold("127.0.0.2", "192.0.2.1") for _ in range(16)]
            assert outcomes.count(0) == 8 and outcomes.count(503) == 8, outcomes
            status, body = get("127.0.0.2", "192.0.2.2")
            assert status == 200 and "192.0.2.2|https" in body, body
            assert get("127.0.0.3", "192.0.2.3")[0] == 444
            results["trusted_proxy"] = "client budgets independent, HTTPS retained, untrusted 444"
        print(json.dumps({"mode": mode, "results": results}), flush=True)
    finally:
        clear()


def image_ref(path: str, name: str) -> str:
    """使用候选 Dockerfile 的固定摘要，禁止测试浮动 tag。"""
    source = (ROOT / path).read_text()
    match = re.search(rf"^FROM ({name}:[^\s@]+@sha256:[0-9a-f]{{64}})", source, re.M)
    if match is None:
        raise RuntimeError("candidate pinned image missing")
    return match[1]


def main() -> None:
    sys.path.insert(0, str(ROOT / "deploy/scripts"))
    from render_trusted_proxy_conf import render

    def docker(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [str(ROOT / "scripts/docker_public.sh"), "run", "--", "docker", *arguments],
            check=False, text=True, capture_output=True,
        )
        if check and result.returncode:
            raise RuntimeError(f"isolated Nginx probe failed: {result.stderr[-4000:]}")
        return result

    nginx = image_ref("frontend/Dockerfile", "nginx")
    python = image_ref("backend/Dockerfile", "python")
    with tempfile.TemporaryDirectory(prefix="sms-nginx-admission-") as directory:
        workspace = Path(directory)
        source = (ROOT / "deploy/nginx.conf").read_text()
        source = source.replace("http://api:8000", "http://127.0.0.1:8000")
        source = source.replace("    server {", (
            '    server { listen 127.0.0.1:8000; location / { return 200 '
            '"$http_x_real_ip|$http_x_forwarded_proto"; } }\n    server {'
        ), 1)
        (workspace / "nginx.conf").write_text(source)
        shutil.copyfile(__file__, workspace / "probe.py")
        shutil.copyfile(ROOT / "deploy/nginx-security-headers.conf", workspace / "headers.conf")
        for mode in ("direct", "external"):
            (workspace / "trusted.conf").write_text(render(
                "0" if mode == "direct" else "1", "" if mode == "direct" else "127.0.0.2/32"
            ))
            name = f"sms-nginx-probe-{uuid4().hex}"
            try:
                docker(
                    "run", "--rm", "--detach", "--platform", "linux/amd64",
                    "--name", name, "--network", "none",
                    "--volume", f"{workspace}/nginx.conf:/etc/nginx/nginx.conf:ro",
                    "--volume", f"{workspace}/trusted.conf:/etc/nginx/trusted-proxies.conf:ro",
                    "--volume",
                    f"{workspace}/headers.conf:/etc/nginx/browser-security-headers.conf:ro",
                    nginx,
                )
                result = docker(
                    "run", "--rm", "--platform", "linux/amd64", "--network", f"container:{name}",
                    "--volume", f"{workspace}:/probe:ro", python,
                    "python", "/probe/probe.py", "--client", mode,
                )
                print(result.stdout.strip(), flush=True)
            finally:
                docker("rm", "--force", name, check=False)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--client" and sys.argv[2] in {"direct", "external"}:
        client(sys.argv[2])
    elif len(sys.argv) == 1:
        main()
    else:
        raise SystemExit("usage: python scripts/verify_nginx_admission.py")
