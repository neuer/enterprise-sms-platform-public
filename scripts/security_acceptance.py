#!/usr/bin/env python3
"""在 mock Compose 栈上执行不泄露证据内容的运行态安全验收。"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from runtime_credentials import read_secret_file

TAB_ID = "00000000000000000000000000000001"
PHONE_PATTERN = re.compile(r"(?<!\d)1\d{10}(?!\d)")
UUID_PATTERN = re.compile(
    r"(?<![0-9A-Fa-f])"
    r"[0-9A-Fa-f]{8}-"
    r"[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{12}"
    r"(?![0-9A-Fa-f])"
)
AUDIT_SQL = """
SELECT count(*) FROM audit_log
WHERE coalesce((before_val - 'batch_no')::text,'') ~ '(^|[^0-9])1[0-9]{10}([^0-9]|$)'
   OR coalesce((after_val - 'batch_no')::text,'') ~ '(^|[^0-9])1[0-9]{10}([^0-9]|$)'
   OR coalesce(before_val::text,'') ~* '\"(phone(s|_enc|_hmac)?|mobiles)\"[[:space:]]*:'
   OR coalesce(after_val::text,'') ~* '\"(phone(s|_enc|_hmac)?|mobiles)\"[[:space:]]*:'
""".strip()
OTP_SQL = """
SELECT count(*) FROM sms_batch
WHERE category='verify' AND content ~ '[0-9]{4,8}'
""".strip()


class AcceptanceFailure(RuntimeError):
    """只公开检查编号与安全摘要，不包含服务响应或命中原文。"""


class CommandFailure(AcceptanceFailure):
    def __init__(self, executable: str, returncode: int) -> None:
        super().__init__(f"command failed: {Path(executable).name} rc={returncode}")


class Runner(Protocol):
    def run(self, command: Sequence[str], *, cwd: Path | None = None) -> bytes: ...


class CommandRunner:
    def run(self, command: Sequence[str], *, cwd: Path | None = None) -> bytes:
        if isinstance(command, (str, bytes)) or not command:
            raise ValueError("command must be a non-empty argv sequence")
        argv = [str(item) for item in command]
        result = subprocess.run(argv, cwd=cwd, capture_output=True, check=False)
        if result.returncode != 0:
            raise CommandFailure(argv[0], result.returncode)
        return result.stdout


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    data: object


class HttpClient(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        payload: object = None,
        token: str | None = None,
    ) -> HttpResponse: ...


class StdlibHttpClient:
    def __init__(self, base_url: str, *, timeout_s: float = 10) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    @staticmethod
    def _decode(body: bytes) -> object:
        if not body:
            return None
        try:
            return json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: object = None,
        token: str | None = None,
    ) -> HttpResponse:
        headers = {"Accept": "application/json"}
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode()
            headers["Content-Type"] = "application/json"
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                return HttpResponse(response.status, self._decode(response.read()))
        except urllib.error.HTTPError as error:
            return HttpResponse(error.code, self._decode(error.read()))
        except urllib.error.URLError as error:
            raise AcceptanceFailure("SEC-00 API unavailable") from error


def _secret_candidates(secrets: Mapping[str, bytes]) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    for name, raw_value in secrets.items():
        try:
            text = raw_value.decode("utf-8").strip()
        except UnicodeDecodeError:
            continue
        values = [text]
        for line in text.splitlines():
            value = line.partition("=")[2] if "=" in line else line
            values.append(value.strip())
        for value in values:
            if len(value) >= 8:
                candidates.append((name, value))
    return candidates


def scan_logs(logs: bytes, secrets: Mapping[str, bytes]) -> None:
    """扫描运行日志，但错误中永不回显命中原文。"""

    text = logs.decode("utf-8", errors="replace")
    phone_scan_text = UUID_PATTERN.sub("[OPAQUE_UUID]", text)
    if PHONE_PATTERN.search(phone_scan_text):
        raise AcceptanceFailure("SEC-07 runtime logs contain an unmasked phone")
    for name, value in _secret_candidates(secrets):
        if value in text:
            raise AcceptanceFailure(f"SEC-07 runtime logs contain secret file: {name}")


def _mapping(value: object, check: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise AcceptanceFailure(f"{check} returned an invalid JSON shape")
    return value


class AcceptanceSuite:
    """组合认证、注入、SSRF、数据库与容器日志的七项黑盒检查。"""

    def __init__(
        self,
        http: HttpClient,
        runner: Runner,
        *,
        compose_file: Path,
        secrets_dir: Path,
        repository_root: Path,
    ) -> None:
        self.http = http
        self.runner = runner
        self.compose_file = compose_file
        self.secrets_dir = secrets_dir
        self.repository_root = repository_root
        self.mock_password = read_secret_file(
            secrets_dir / "ldap_bind_password",
            label="mock password",
        )

    @staticmethod
    def _expect(response: HttpResponse, status: int, check: str) -> None:
        if response.status != status:
            raise AcceptanceFailure(f"{check} expected HTTP {status}, got {response.status}")

    @classmethod
    def _expect_error(
        cls,
        response: HttpResponse,
        status: int,
        code: str,
        check: str,
    ) -> None:
        cls._expect(response, status, check)
        if _mapping(response.data, check).get("code") != code:
            raise AcceptanceFailure(f"{check} returned an unexpected error domain")

    def _login(self, username: str) -> str:
        response = self.http.request(
            "POST",
            "/api/v1/web/auth/login",
            payload={
                "provider_code": "ad",
                "username": username,
                "password": self.mock_password,
                "tab_id": TAB_ID,
            },
        )
        self._expect(response, 200, "SEC-01")
        token = _mapping(response.data, "SEC-01").get("token")
        if not isinstance(token, str) or not token:
            raise AcceptanceFailure("SEC-01 login response omitted token")
        return token

    def _compose(self, *arguments: str) -> list[str]:
        return [
            "docker",
            "compose",
            "-f",
            str(self.compose_file),
            "--profile",
            "dev",
            *arguments,
        ]

    def _database_count(self, sql: str, check: str) -> int:
        output = self.runner.run(
            self._compose(
                "exec",
                "-T",
                "postgres",
                "psql",
                "-X",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                "sms_owner",
                "-d",
                "sms",
                "-Atc",
                sql,
            ),
            cwd=self.repository_root,
        )
        value = output.decode("ascii", errors="strict").strip()
        if not value.isdecimal():
            raise AcceptanceFailure(f"{check} returned an invalid database count")
        return int(value)

    def _read_secrets(self) -> dict[str, bytes]:
        values: dict[str, bytes] = {}
        if not self.secrets_dir.is_dir():
            raise AcceptanceFailure("SEC-07 secrets directory unavailable")
        for path in sorted(self.secrets_dir.iterdir()):
            if path.is_symlink() or not path.is_file():
                continue
            if path.stat().st_size > 64 * 1024:
                raise AcceptanceFailure(f"SEC-07 secret file too large: {path.name}")
            values[path.name] = path.read_bytes()
        return values

    def run(self) -> list[str]:
        checks: list[str] = []
        admin_token = self._login("admin01")
        viewer_token = self._login("viewer01")
        checks.append("SEC-01 mock authentication")

        forbidden = self.http.request(
            "GET",
            "/api/v1/web/admin/audit-logs?page=1&page_size=1",
            token=viewer_token,
        )
        self._expect(forbidden, 403, "SEC-02")
        if _mapping(forbidden.data, "SEC-02").get("code") != "FORBIDDEN":
            raise AcceptanceFailure("SEC-02 viewer authorization was not uniformly denied")
        checks.append("SEC-02 authorization scope")

        login_injection = self.http.request(
            "POST",
            "/api/v1/web/auth/login",
            payload={
                "provider_code": "ad",
                "username": "admin01' OR '1'='1",
                "password": self.mock_password,
                "tab_id": TAB_ID,
            },
        )
        self._expect_error(login_injection, 401, "UNAUTHORIZED", "SEC-03")
        path_injection = self.http.request(
            "POST",
            "/api/v1/web/messages/%27%20OR%201%3D1--/phone/decrypt",
            token=admin_token,
        )
        self._expect_error(path_injection, 400, "INVALID_PARAM", "SEC-03")
        baseline = self.http.request(
            "GET",
            "/api/v1/web/batches?status=never-match&page=1&size=1",
            token=viewer_token,
        )
        injected = self.http.request(
            "GET",
            "/api/v1/web/batches?status=%27%20OR%201%3D1--&page=1&size=1",
            token=viewer_token,
        )
        self._expect(baseline, 200, "SEC-03")
        self._expect(injected, 200, "SEC-03")
        baseline_total = _mapping(baseline.data, "SEC-03").get("total")
        injected_total = _mapping(injected.data, "SEC-03").get("total")
        if baseline_total != 0 or injected_total != baseline_total:
            raise AcceptanceFailure("SEC-03 filter injection changed scoped result")
        checks.append("SEC-03 SQL injection boundaries")

        ssrf = self.http.request(
            "POST",
            "/api/v1/web/admin/apps",
            payload={
                "name": "security-ssrf-probe",
                "dept": "安全验收",
                "callback_url": "http://127.0.0.1:9028/_mock/callback",
            },
            token=admin_token,
        )
        self._expect(ssrf, 422, "SEC-04")
        checks.append("SEC-04 callback SSRF save boundary")

        if self._database_count(AUDIT_SQL, "SEC-05") != 0:
            raise AcceptanceFailure("SEC-05 audit payload contains prohibited PII")
        checks.append("SEC-05 audit payload PII")

        if self._database_count(OTP_SQL, "SEC-06") != 0:
            raise AcceptanceFailure("SEC-06 verify content contains an unmasked OTP")
        checks.append("SEC-06 verify OTP persistence")

        logs = self.runner.run(
            self._compose("logs", "--no-color", "--no-log-prefix"),
            cwd=self.repository_root,
        )
        scan_logs(logs, self._read_secrets())
        checks.append("SEC-07 runtime logs and secrets")
        return checks


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--compose-file", type=Path, default=root / "deploy/docker-compose.yml")
    parser.add_argument("--secrets-dir", type=Path, default=root / "deploy/secrets")
    args = parser.parse_args()
    try:
        checks = AcceptanceSuite(
            StdlibHttpClient(args.base),
            CommandRunner(),
            compose_file=args.compose_file,
            secrets_dir=args.secrets_dir,
            repository_root=root,
        ).run()
    except (AcceptanceFailure, OSError, UnicodeError, ValueError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "success", "checks": checks}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
