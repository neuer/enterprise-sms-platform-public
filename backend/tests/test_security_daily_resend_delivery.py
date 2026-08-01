from __future__ import annotations

import importlib.util
import io
import json
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "deploy" / "scripts"
SENDER_SCRIPT = SCRIPTS / "send_security_daily_report_resend.py"
INSTALLER_SCRIPT = SCRIPTS / "install_resend_api_key.py"
SAMPLE = ROOT / "deploy" / "templates" / "security_daily_report.sample.json"
COMPANION_COMPOSE = ROOT / "deploy" / "security-report" / "docker-compose.yml"
COMPANION_DOCKERFILE = ROOT / "deploy" / "security-report" / "Dockerfile"


def _module() -> ModuleType:
    assert SENDER_SCRIPT.is_file(), "Resend 安全日报发送器尚未实现"
    scripts_path = str(SCRIPTS)
    if scripts_path not in sys.path:
        sys.path.insert(0, scripts_path)
    spec = importlib.util.spec_from_file_location(
        "send_security_daily_report_resend",
        SENDER_SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _installer_module() -> ModuleType:
    assert INSTALLER_SCRIPT.is_file(), "Resend Key 安装器尚未实现"
    spec = importlib.util.spec_from_file_location(
        "install_resend_api_key",
        INSTALLER_SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeTransport:
    def __init__(
        self,
        outcomes: list[tuple[int, bytes] | Exception],
    ) -> None:
        self.outcomes = outcomes
        self.requests: list[tuple[dict[str, str], bytes]] = []

    def post(self, *, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
        self.requests.append((headers, body))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_secret_reader_strips_only_newline_and_rejects_unsafe_files(
    tmp_path: Path,
) -> None:
    module = _module()
    secret_file = tmp_path / "resend_api_key"
    secret_file.write_text("re_test_value\r\n", encoding="utf-8")

    assert module.read_api_key(secret_file) == "re_test_value"

    secret_file.write_text("re_test value\n", encoding="utf-8")
    with pytest.raises(module.ResendConfigurationError, match="invalid"):
        module.read_api_key(secret_file)

    secret_file.write_text("\n", encoding="utf-8")
    with pytest.raises(module.ResendConfigurationError, match="empty"):
        module.read_api_key(secret_file)

    with pytest.raises(module.ResendConfigurationError, match="unavailable"):
        module.read_api_key(tmp_path / "missing")


def test_key_installer_is_atomic_private_and_rejects_symlink(
    tmp_path: Path,
) -> None:
    module = _installer_module()
    destination = tmp_path / "resend_api_key"

    module.install_key(destination, io.BytesIO(b"re_first_value\n"))

    assert destination.read_bytes() == b"re_first_value\n"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600

    module.install_key(destination, io.BytesIO(b"re_rotated_value\r\n"))

    assert destination.read_bytes() == b"re_rotated_value\n"
    assert not list(tmp_path.glob(".resend_api_key.*.tmp"))

    destination.unlink()
    destination.symlink_to(tmp_path / "outside")
    with pytest.raises(module.ResendKeyInstallError, match="symlink"):
        module.install_key(destination, io.BytesIO(b"re_never_written\n"))


def test_delivery_uses_exact_endpoint_safe_headers_and_rendered_variants() -> None:
    module = _module()
    report = module.renderer.load_report(SAMPLE)
    transport = FakeTransport([(200, b'{"id":"email-123"}')])
    client = module.ResendClient(
        api_key="re_sensitive_value",
        transport=transport,
        sleep=lambda _delay: None,
    )

    receipt = client.send(report, recipients=("lin.tong@example.com",))

    assert receipt.email_id == "email-123"
    assert receipt.report_date == "2026-07-15"
    assert len(transport.requests) == 1
    headers, encoded = transport.requests[0]
    assert headers == {
        "Authorization": "Bearer re_sensitive_value",
        "Content-Type": "application/json",
        "Idempotency-Key": "security-daily-2026-07-15",
        "User-Agent": "sms-platform-security-daily/1.0",
    }
    payload = json.loads(encoded)
    assert payload["from"] == "短信平台安全日报 <security-daily@reports.neuer.cn>"
    assert payload["to"] == ["lin.tong@example.com"]
    assert payload["subject"] == "[短信平台安全日报][关注] 2026-07-15"
    assert "服务器安全日报" in payload["html"]
    assert "服务器安全日报" in payload["text"]
    assert payload.keys() == {"from", "to", "subject", "html", "text"}


def test_transient_failure_retries_with_the_same_idempotent_request() -> None:
    module = _module()
    report = module.renderer.load_report(SAMPLE)
    transport = FakeTransport(
        [
            (429, b'{"message":"rate limited"}'),
            OSError("temporary connection problem"),
            (200, b'{"id":"email-456"}'),
        ]
    )
    delays: list[float] = []
    client = module.ResendClient(
        api_key="re_sensitive_value",
        transport=transport,
        sleep=delays.append,
    )

    receipt = client.send(report, recipients=("lin.tong@example.com",))

    assert receipt.email_id == "email-456"
    assert delays == [1.0, 2.0]
    assert len(transport.requests) == 3
    assert transport.requests[0] == transport.requests[1] == transport.requests[2]


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (400, b'{"message":"bad security.owner@example.com re_sensitive_value"}'),
        (401, b'{"message":"invalid key re_sensitive_value"}'),
        (422, b'{"message":"body contained 13800138000"}'),
    ],
)
def test_provider_error_never_exposes_response_body(
    status: int,
    body: bytes,
) -> None:
    module = _module()
    report = module.renderer.load_report(SAMPLE)
    client = module.ResendClient(
        api_key="re_sensitive_value",
        transport=FakeTransport([(status, body)]),
        sleep=lambda _delay: None,
    )

    with pytest.raises(module.ResendDeliveryError) as captured:
        client.send(report, recipients=("lin.tong@example.com",))

    message = str(captured.value)
    assert message == f"Resend API rejected the security report (HTTP {status})"
    assert "re_sensitive_value" not in message
    assert "security.owner@example.com" not in message
    assert "13800138000" not in message


@pytest.mark.parametrize(
    "recipients",
    [
        (),
        ("not-an-address",),
        ("one@example.com", "two@example.com", "three@example.com", "four@example.com"),
    ],
)
def test_recipient_validation_fails_closed(recipients: tuple[str, ...]) -> None:
    module = _module()
    report = module.renderer.load_report(SAMPLE)
    client = module.ResendClient(
        api_key="re_sensitive_value",
        transport=FakeTransport([(200, b'{"id":"unused"}')]),
        sleep=lambda _delay: None,
    )

    with pytest.raises(module.ResendConfigurationError, match="recipient"):
        client.send(report, recipients=recipients)


def test_recipient_file_is_bounded_and_does_not_accept_comments_as_addresses(
    tmp_path: Path,
) -> None:
    module = _module()
    recipients_file = tmp_path / "recipients"
    recipients_file.write_text(
        "# security owners\nlin.tong@example.com\nops@example.com\n",
        encoding="utf-8",
    )

    assert module.read_recipients(recipients_file) == (
        "lin.tong@example.com",
        "ops@example.com",
    )


def test_default_https_transport_is_fixed_to_resend_without_proxy_or_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    captured: dict[str, Any] = {}

    class FakeResponse:
        status = 200

        def read(self, size: int) -> bytes:
            captured["read_size"] = size
            return b'{"id":"email-789"}'

    class FakeConnection:
        def __init__(self, host: str, port: int, **kwargs: Any) -> None:
            captured["connection"] = (host, port, kwargs)

        def request(
            self,
            method: str,
            path: str,
            *,
            body: bytes,
            headers: dict[str, str],
        ) -> None:
            captured["request"] = (method, path, body, headers)

        def getresponse(self) -> FakeResponse:
            return FakeResponse()

        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr(module.http.client, "HTTPSConnection", FakeConnection)

    status, body = module.ResendHttpsTransport().post(
        headers={"Authorization": "Bearer re_test"},
        body=b"{}",
    )

    assert status == 200
    assert body == b'{"id":"email-789"}'
    host, port, kwargs = captured["connection"]
    assert (host, port) == ("api.resend.com", 443)
    assert kwargs["timeout"] == 10.0
    assert kwargs["context"].minimum_version.name == "TLSv1_2"
    method, path, encoded, headers = captured["request"]
    assert (method, path, encoded) == ("POST", "/emails", b"{}")
    assert headers == {"Authorization": "Bearer re_test"}
    assert captured["read_size"] == module.MAX_RESPONSE_BYTES + 1
    assert captured["closed"] is True


def test_control_loop_sends_redacted_request_and_writes_only_safe_result(
    tmp_path: Path,
) -> None:
    module = _module()
    control_dir = tmp_path / "control"
    request_dir = control_dir / "requests"
    request_dir.mkdir(parents=True)
    request_id = uuid4()
    payload = json.loads(SAMPLE.read_text(encoding="utf-8"))
    request_path = request_dir / f"{request_id}.json"
    request_path.write_text(
        json.dumps(
            {
                "request_id": str(request_id),
                "report_date": payload["report_date"],
                "action": "send",
                "payload": payload,
            }
        ),
        encoding="utf-8",
    )
    key_file = tmp_path / "key"
    key_file.write_text("re_test_value\n", encoding="utf-8")
    recipients_file = tmp_path / "recipients"
    recipients_file.write_text("security-owner@example.com\n", encoding="utf-8")

    result = module.serve_control(
        control_dir,
        api_key_file=key_file,
        recipients_file=recipients_file,
        once=True,
        transport=FakeTransport([(200, b'{"id":"email-123"}')]),
        sleep=lambda _delay: None,
    )

    assert result == 0
    assert not request_path.exists()
    result_payload = json.loads(
        (control_dir / "results" / f"{request_id}.json").read_text(encoding="utf-8")
    )
    assert result_payload["state"] == "sent"
    encoded = json.dumps(result_payload, ensure_ascii=False)
    assert "re_test_value" not in encoded
    assert "security-owner@example.com" not in encoded
    assert "服务器安全日报" not in encoded


def test_companion_container_uses_only_dedicated_files_and_docker_secret() -> None:
    compose = yaml.safe_load(COMPANION_COMPOSE.read_text(encoding="utf-8"))
    service = compose["services"]["security-report-mailer"]

    assert set(compose["services"]) == {"security-report-mailer"}
    assert service["read_only"] is True
    assert service["user"] == "10001:10001"
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["secrets"] == [
        {"source": "resend_api_key", "target": "resend_api_key"}
    ]
    assert compose["secrets"]["resend_api_key"]["file"] == "./secrets/resend_api_key"
    serialized = json.dumps(compose, ensure_ascii=False)
    assert "RESEND_API_KEY" not in serialized
    assert "security.owner@example.com" not in serialized

    dockerfile = COMPANION_DOCKERFILE.read_text(encoding="utf-8")
    assert (
        "FROM python:3.12-alpine@sha256:"
        "6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df"
        in dockerfile
    )
    assert "USER security-report" in dockerfile
    assert "pip install" not in dockerfile


def test_docker_build_context_excludes_report_secrets_and_runtime_data() -> None:
    patterns = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    config_ignore = (
        ROOT / "deploy" / "security-report" / "config" / ".gitignore"
    ).read_text(encoding="utf-8").splitlines()

    assert "deploy/security-report/secrets/" in patterns
    assert "deploy/security-report/runtime/" in patterns
    assert "deploy/security-report/config/recipients.txt" in patterns
    assert "recipients.txt" in config_ignore


def test_repo_examples_never_contain_real_recipient_or_api_key() -> None:
    checked_paths = [
        SENDER_SCRIPT,
        INSTALLER_SCRIPT,
        COMPANION_COMPOSE,
        COMPANION_DOCKERFILE,
        ROOT / "deploy" / "security-report" / "README.md",
        ROOT / "deploy" / "security-report" / "config" / "recipients.example.txt",
    ]
    for path in checked_paths:
        content = path.read_text(encoding="utf-8")
        assert "security.owner@example.com" not in content
        assert "re_sensitive_value" not in content
        assert "re_test_" not in content


def test_retry_sleep_is_injected_and_never_exceeds_two_delays() -> None:
    module = _module()
    report = module.renderer.load_report(SAMPLE)
    delays: list[float] = []
    transport = FakeTransport(
        [
            TimeoutError("first"),
            OSError("second"),
            OSError("third"),
        ]
    )
    client = module.ResendClient(
        api_key="re_sensitive_value",
        transport=transport,
        sleep=delays.append,
    )

    with pytest.raises(module.ResendDeliveryError, match="transport failed"):
        client.send(report, recipients=("lin.tong@example.com",))

    assert delays == [1.0, 2.0]
    assert len(transport.requests) == 3


def test_sleep_callable_type_is_simple_and_synchronous() -> None:
    module = _module()
    annotation = module.ResendClient.__init__.__annotations__["sleep"]

    assert annotation == Callable[[float], None] | None
