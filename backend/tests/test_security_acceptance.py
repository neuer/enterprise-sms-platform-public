from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from security_acceptance import (  # noqa: E402
    AUDIT_SQL,
    AcceptanceFailure,
    AcceptanceSuite,
    HttpResponse,
    scan_logs,
)


def test_audit_scan_excludes_only_allowed_top_level_batch_reference() -> None:
    assert "before_val - 'batch_no'" in AUDIT_SQL
    assert "after_val - 'batch_no'" in AUDIT_SQL
    assert "phone(s|_enc|_hmac)?|mobiles" in AUDIT_SQL


class FakeHttp:
    def __init__(
        self,
        *,
        ssrf_status: int = 422,
        injection_total: int = 0,
        login_injection_status: int = 401,
        path_injection_status: int = 400,
    ) -> None:
        self.ssrf_status = ssrf_status
        self.injection_total = injection_total
        self.login_injection_status = login_injection_status
        self.path_injection_status = path_injection_status
        self.calls: list[tuple[str, str, object, str | None]] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: object = None,
        token: str | None = None,
    ) -> HttpResponse:
        self.calls.append((method, path, payload, token))
        if path == "/api/v1/web/auth/login":
            username = payload["username"]  # type: ignore[index]
            if "password" not in payload or payload.get("provider_code") != "ad":  # type: ignore[union-attr,operator]
                return HttpResponse(400, {"code": "INVALID_PARAM"})
            if "OR" in username:
                code = "UNAUTHORIZED" if self.login_injection_status == 401 else "INVALID_PARAM"
                return HttpResponse(self.login_injection_status, {"code": code})
            return HttpResponse(200, {"token": f"token-{username}"})
        if path.startswith("/api/v1/web/admin/audit-logs"):
            return HttpResponse(403, {"code": "FORBIDDEN"})
        if path.startswith("/api/v1/messages/batches/"):
            return HttpResponse(401, {"code": "UNAUTHORIZED"})
        if path.startswith("/api/v1/web/messages/") and path.endswith("/phone/decrypt"):
            if token is None:
                return HttpResponse(401, {"code": "UNAUTHORIZED"})
            code = "INVALID_PARAM" if self.path_injection_status == 400 else "UNAUTHORIZED"
            return HttpResponse(self.path_injection_status, {"code": code})
        if path.startswith("/api/v1/web/batches?status=never-match"):
            return HttpResponse(200, {"total": 0, "items": []})
        if path.startswith("/api/v1/web/batches?status="):
            return HttpResponse(200, {"total": self.injection_total, "items": []})
        if path == "/api/v1/web/admin/apps":
            return HttpResponse(self.ssrf_status, {"code": "INVALID_PARAM"})
        raise AssertionError(f"unexpected request: {method} {path}")


class FakeRunner:
    def __init__(self, *, audit_count: int = 0, otp_count: int = 0, logs: bytes = b"") -> None:
        self.audit_count = audit_count
        self.otp_count = otp_count
        self.logs = logs
        self.calls: list[list[str]] = []

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
    ) -> bytes:
        argv = list(command)
        self.calls.append(argv)
        joined = " ".join(argv)
        if "audit_log" in joined:
            return f"{self.audit_count}\n".encode()
        if "category='verify'" in joined:
            return f"{self.otp_count}\n".encode()
        if "logs" in argv:
            return self.logs
        raise AssertionError(f"unexpected command: {argv}")


def make_suite(
    tmp_path: Path,
    *,
    http: FakeHttp | None = None,
    runner: FakeRunner | None = None,
) -> AcceptanceSuite:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "jwt_secret").write_text("never-log-this-secret-value\n", encoding="utf-8")
    (secrets / "ldap_bind_password").write_text(
        "in-memory-security-test-password\n",
        encoding="utf-8",
    )
    for secret in secrets.iterdir():
        secret.chmod(0o600)
    return AcceptanceSuite(
        http or FakeHttp(),
        runner or FakeRunner(),
        compose_file=tmp_path / "docker-compose.yml",
        secrets_dir=secrets,
        repository_root=tmp_path,
    )


def test_security_suite_covers_auth_injection_ssrf_database_and_logs(tmp_path: Path) -> None:
    http = FakeHttp()
    runner = FakeRunner()
    checks = make_suite(tmp_path, http=http, runner=runner).run()

    assert checks == [
        "SEC-01 mock authentication",
        "SEC-02 authorization scope",
        "SEC-03 SQL injection boundaries",
        "SEC-04 callback SSRF save boundary",
        "SEC-05 audit payload PII",
        "SEC-06 verify OTP persistence",
        "SEC-07 runtime logs and secrets",
    ]
    assert any("audit_log" in " ".join(call) for call in runner.calls)
    assert any("category='verify'" in " ".join(call) for call in runner.calls)
    assert all("--profile" in call and "dev" in call for call in runner.calls)
    assert runner.calls[-1][-2:] == ["--no-color", "--no-log-prefix"]


def test_injection_probes_use_valid_shape_and_authenticated_path(tmp_path: Path) -> None:
    http = FakeHttp()
    make_suite(tmp_path, http=http).run()

    login_probe = next(
        call
        for call in http.calls
        if call[1] == "/api/v1/web/auth/login"
        and isinstance(call[2], dict)
        and "OR" in str(call[2].get("username"))
    )
    assert login_probe[2] == {
        "provider_code": "ad",
        "username": "admin01' OR '1'='1",
        "password": "in-memory-security-test-password",
    }
    path_probe = next(
        call for call in http.calls if call[1].endswith("/phone/decrypt")
    )
    assert path_probe[0] == "POST"
    assert path_probe[3] == "token-admin01"


@pytest.mark.parametrize(
    "http",
    [
        FakeHttp(login_injection_status=400),
        FakeHttp(path_injection_status=401),
        FakeHttp(path_injection_status=404),
    ],
)
def test_injection_probes_reject_validation_auth_and_not_found_shortcuts(
    tmp_path: Path,
    http: FakeHttp,
) -> None:
    with pytest.raises(AcceptanceFailure, match="SEC-03"):
        make_suite(tmp_path, http=http).run()


@pytest.mark.parametrize(
    ("http", "runner", "check"),
    [
        (FakeHttp(injection_total=1), FakeRunner(), "SEC-03"),
        (FakeHttp(ssrf_status=200), FakeRunner(), "SEC-04"),
        (FakeHttp(), FakeRunner(audit_count=1), "SEC-05"),
        (FakeHttp(), FakeRunner(otp_count=1), "SEC-06"),
    ],
)
def test_security_suite_fails_closed_with_check_id_only(
    tmp_path: Path,
    http: FakeHttp,
    runner: FakeRunner,
    check: str,
) -> None:
    with pytest.raises(AcceptanceFailure) as captured:
        make_suite(tmp_path, http=http, runner=runner).run()
    assert check in str(captured.value)
    assert "token-admin01" not in str(captured.value)


def test_log_scan_redacts_phone_and_secret_values() -> None:
    phone = "13800138000"
    secret = "never-log-this-secret-value"
    with pytest.raises(AcceptanceFailure) as phone_error:
        scan_logs(f"payload={phone}".encode(), {"jwt_secret": secret.encode()})
    assert "SEC-07" in str(phone_error.value) and phone not in str(phone_error.value)

    with pytest.raises(AcceptanceFailure) as secret_error:
        scan_logs(f"credential={secret}".encode(), {"jwt_secret": secret.encode()})
    assert "jwt_secret" in str(secret_error.value) and secret not in str(secret_error.value)


def test_log_scan_ignores_phone_like_digits_inside_standard_uuid() -> None:
    scan_logs(
        b"task_id=fa39c47a-4a50-468f-85ca-b12345678901",
        {"jwt_secret": b"never-log-this-secret-value"},
    )
