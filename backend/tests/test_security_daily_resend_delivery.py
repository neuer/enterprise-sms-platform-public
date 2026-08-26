from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "deploy" / "scripts"
SENDER_SCRIPT = SCRIPTS / "send_security_daily_report_resend.py"
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


def test_delivery_uses_exact_endpoint_safe_headers_and_rendered_variants() -> None:
    module = _module()
    report = module.renderer.load_report(SAMPLE)
    transport = FakeTransport([(200, b'{"id":"email-123"}')])
    client = module.ResendClient(
        api_key="re_sensitive_value",
        transport=transport,
        sleep=lambda _delay: None,
    )

    request_id = UUID("11111111-2222-3333-4444-555555555555")
    receipt = client.send(
        report,
        recipients=("lin.tong@example.com",),
        request_id=request_id,
    )

    assert receipt.email_id == "email-123"
    assert receipt.report_date == "2026-07-15"
    assert len(transport.requests) == 1
    headers, encoded = transport.requests[0]
    assert headers == {
        "Authorization": "Bearer re_sensitive_value",
        "Content-Type": "application/json",
        "Idempotency-Key": f"security-daily-2026-07-15-{request_id}-g1",
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

    request_id = UUID("22222222-3333-4444-5555-666666666666")
    receipt = client.send(
        report,
        recipients=("lin.tong@example.com",),
        request_id=request_id,
    )

    assert receipt.email_id == "email-456"
    assert delays == [1.0, 2.0]
    assert len(transport.requests) == 3
    assert transport.requests[0] == transport.requests[1] == transport.requests[2]
    assert (
        transport.requests[0][0]["Idempotency-Key"]
        == f"security-daily-2026-07-15-{request_id}-g1"
    )


def test_regenerated_delivery_uses_a_fresh_idempotency_key() -> None:
    module = _module()
    report = module.renderer.load_report(SAMPLE)
    transport = FakeTransport(
        [
            (200, b'{"id":"email-first"}'),
            (200, b'{"id":"email-second"}'),
        ]
    )
    client = module.ResendClient(
        api_key="re_sensitive_value",
        transport=transport,
        sleep=lambda _delay: None,
    )

    client.send(
        report,
        recipients=("lin.tong@example.com",),
        request_id=UUID("33333333-4444-5555-6666-777777777777"),
    )
    client.send(
        report,
        recipients=("lin.tong@example.com",),
        request_id=UUID("44444444-5555-6666-7777-888888888888"),
    )

    keys = [request[0]["Idempotency-Key"] for request in transport.requests]
    assert len(set(keys)) == 2


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


def test_resend_transport_absolute_deadline_interrupts_slow_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()

    class SlowConnection:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return None

        def request(self, *_args: object, **_kwargs: object) -> None:
            time.sleep(0.05)

        def close(self) -> None:
            return None

    monkeypatch.setattr(module, "RESEND_ABSOLUTE_DEADLINE_S", 0.01)
    monkeypatch.setattr(module.http.client, "HTTPSConnection", SlowConnection)

    with pytest.raises(TimeoutError, match="absolute deadline"):
        module.ResendHttpsTransport().post(headers={}, body=b"{}")


def test_mailer_config_reader_and_control_loop_use_ui_synced_config(
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
                    "config_version": 1,
                    "payload": payload,
            }
        ),
        encoding="utf-8",
    )
    config_file = tmp_path / "resend.json"
    config_file.write_text(
        json.dumps(
                {
                    "api_key": "re_test_value",
                    "recipients": ["security-owner@example.com"],
                    "config_version": 1,
                }
        ),
        encoding="utf-8",
    )
    assert module.read_mailer_configuration(config_file).recipients == (
        "security-owner@example.com",
    )

    result = module.serve_control(
        control_dir,
        config_file=config_file,
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


def test_companion_container_uses_only_dedicated_ui_config_file() -> None:
    compose = yaml.safe_load(COMPANION_COMPOSE.read_text(encoding="utf-8"))
    service = compose["services"]["security-report-mailer"]

    assert set(compose["services"]) == {"security-report-mailer"}
    assert service["read_only"] is True
    assert service["user"] == "10001:10001"
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    serialized = json.dumps(compose, ensure_ascii=False)
    assert "resend.json" in serialized
    assert "security-report-config" in serialized
    assert "secrets" not in service
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
    assert "install -d -m 0755 /app/deploy/scripts /app/deploy/templates" in dockerfile
    assert "pip install" not in dockerfile


def test_docker_build_context_excludes_active_mailer_config_and_runtime_data() -> None:
    patterns = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert "deploy/security-report-config/" in patterns
    assert "deploy/security-report-control/" in patterns
    assert "deploy/security-report-nginx/" in patterns

    legacy_dirs = (
        ROOT / "deploy" / "security-report" / "config",
        ROOT / "deploy" / "security-report" / "runtime",
        ROOT / "deploy" / "security-report" / "secrets",
    )
    assert all(not path.exists() for path in legacy_dirs)


def test_repo_examples_never_contain_real_recipient_or_api_key() -> None:
    checked_paths = [
        SENDER_SCRIPT,
        COMPANION_COMPOSE,
        COMPANION_DOCKERFILE,
        ROOT / "deploy" / "security-report" / "README.md",
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


SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")


def _write_mailer_request(
    path: Path,
    *,
    request_id: UUID,
    payload: dict[str, Any],
    delivery_id: str = "10086",
    delivery_generation: int = 1,
    recipient_set_digest: str = "",
    config_version: int = 1,
) -> None:
    body: dict[str, Any] = {
        "request_id": str(request_id),
        "report_date": payload["report_date"],
        "action": "send",
        "config_version": config_version,
        "delivery_id": delivery_id,
        "delivery_generation": delivery_generation,
        "payload": payload,
    }
    if recipient_set_digest:
        body["recipient_set_digest"] = recipient_set_digest
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")


def _write_mailer_config(path: Path, *, recipients: list[str] | None = None) -> None:
    path.write_text(
        json.dumps(
            {
                "api_key": "re_test_value",
                "recipients": recipients or ["security-owner@example.com"],
                "config_version": 1,
            }
        ),
        encoding="utf-8",
    )


def test_claim_before_provider_is_recovered_after_restart(tmp_path: Path) -> None:
    module = _module()
    control_dir = tmp_path / "control"
    request_dir = control_dir / "requests"
    result_dir = control_dir / "results"
    request_dir.mkdir(parents=True)
    result_dir.mkdir()
    request_id = uuid4()
    payload = json.loads(SAMPLE.read_text(encoding="utf-8"))
    claim = request_dir / f".{request_id}.{uuid4().hex}.processing"
    _write_mailer_request(claim, request_id=request_id, payload=payload)
    config_file = tmp_path / "resend.json"
    _write_mailer_config(config_file)
    transport = FakeTransport([(200, b'{"id":"email-recovered"}')])

    recovered = module.recover_stale_claims(
        request_dir,
        result_dir,
        now=datetime.now(SHANGHAI) + timedelta(seconds=60),
    )
    assert recovered == 1
    assert (request_dir / f"{request_id}.json").is_file()
    assert not claim.exists()
    assert module.serve_control(
        control_dir,
        config_file=config_file,
        once=True,
        transport=transport,
        sleep=lambda _delay: None,
    ) == 0
    result = json.loads((result_dir / f"{request_id}.json").read_text(encoding="utf-8"))
    assert result["state"] == "sent"
    assert transport.requests[0][0]["Idempotency-Key"].endswith("-10086-g1")
    encoded = json.dumps(result)
    assert "re_test_value" not in encoded
    assert "security-owner@example.com" not in encoded


def test_provider_success_without_result_retries_same_delivery_identity(
    tmp_path: Path,
) -> None:
    module = _module()
    control_dir = tmp_path / "control"
    request_dir = control_dir / "requests"
    result_dir = control_dir / "results"
    request_dir.mkdir(parents=True)
    result_dir.mkdir()
    request_id = uuid4()
    payload = json.loads(SAMPLE.read_text(encoding="utf-8"))
    claim = request_dir / f".{request_id}.{uuid4().hex}.processing"
    _write_mailer_request(claim, request_id=request_id, payload=payload, delivery_id="10086")
    config_file = tmp_path / "resend.json"
    _write_mailer_config(config_file)
    transport = FakeTransport([(200, b'{"id":"email-same"}')])
    module.recover_stale_claims(
        request_dir,
        result_dir,
        now=datetime.now(SHANGHAI) + timedelta(minutes=2),
    )
    module.serve_control(
        control_dir,
        config_file=config_file,
        once=True,
        transport=transport,
        sleep=lambda _delay: None,
    )
    assert len(transport.requests) == 1
    assert transport.requests[0][0]["Idempotency-Key"] == (
        f"security-daily-{payload['report_date']}-10086-g1"
    )
    assert json.loads((result_dir / f"{request_id}.json").read_text())["state"] == "sent"


def test_existing_result_cleans_claim_without_second_provider_call(tmp_path: Path) -> None:
    module = _module()
    control_dir = tmp_path / "control"
    request_dir = control_dir / "requests"
    result_dir = control_dir / "results"
    request_dir.mkdir(parents=True)
    result_dir.mkdir()
    request_id = uuid4()
    payload = json.loads(SAMPLE.read_text(encoding="utf-8"))
    claim = request_dir / f".{request_id}.{uuid4().hex}.processing"
    _write_mailer_request(claim, request_id=request_id, payload=payload)
    (result_dir / f"{request_id}.json").write_text(
        json.dumps(
            {
                "request_id": str(request_id),
                "report_date": payload["report_date"],
                "state": "sent",
                "completed_at": "2026-07-16T08:02:00+08:00",
                "error": None,
            }
        ),
        encoding="utf-8",
    )
    transport = FakeTransport([(200, b'{"id":"should-not-send"}')])
    recovered = module.recover_stale_claims(request_dir, result_dir)
    assert recovered == 1
    assert not claim.exists()
    module.serve_control(
        control_dir,
        config_file=tmp_path / "unused.json",
        once=True,
        transport=transport,
        sleep=lambda _delay: None,
    )
    assert transport.requests == []


def test_active_claim_lease_is_not_stolen(tmp_path: Path) -> None:
    module = _module()
    request_dir = tmp_path / "requests"
    result_dir = tmp_path / "results"
    request_dir.mkdir()
    result_dir.mkdir()
    request_id = uuid4()
    payload = json.loads(SAMPLE.read_text(encoding="utf-8"))
    claim = request_dir / f".{request_id}.{uuid4().hex}.processing"
    _write_mailer_request(claim, request_id=request_id, payload=payload)
    now = datetime(2026, 7, 16, 8, 0, tzinfo=SHANGHAI)
    body = json.loads(claim.read_text(encoding="utf-8"))
    body.update(
        {
            "claim_id": "active",
            "claimed_at": now.isoformat(),
            "lease_expires_at": (now + timedelta(seconds=45)).isoformat(),
            "boot_generation": "boot-1",
        }
    )
    claim.write_text(json.dumps(body), encoding="utf-8")
    assert module.recover_stale_claims(request_dir, result_dir, now=now) == 0
    assert claim.exists()
    assert not (request_dir / f"{request_id}.json").exists()


def test_stale_claim_requeue_is_reentrant(tmp_path: Path) -> None:
    module = _module()
    request_dir = tmp_path / "requests"
    result_dir = tmp_path / "results"
    request_dir.mkdir()
    result_dir.mkdir()
    request_id = uuid4()
    payload = json.loads(SAMPLE.read_text(encoding="utf-8"))
    claim = request_dir / f".{request_id}.{uuid4().hex}.processing"
    _write_mailer_request(claim, request_id=request_id, payload=payload)
    future = datetime.now(SHANGHAI) + timedelta(minutes=2)
    assert module.recover_stale_claims(request_dir, result_dir, now=future) == 1
    leftover = request_dir / f".{request_id}.{uuid4().hex}.processing"
    leftover.write_text(
        (request_dir / f"{request_id}.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    assert module.recover_stale_claims(request_dir, result_dir, now=future) == 1
    assert (request_dir / f"{request_id}.json").is_file()


def test_hundred_stale_claims_do_not_starve_newer_request(tmp_path: Path) -> None:
    module = _module()
    control_dir = tmp_path / "control"
    request_dir = control_dir / "requests"
    result_dir = control_dir / "results"
    request_dir.mkdir(parents=True)
    result_dir.mkdir()
    payload = json.loads(SAMPLE.read_text(encoding="utf-8"))
    expired = datetime(2026, 7, 16, 7, 0, tzinfo=SHANGHAI)
    for _ in range(100):
        stale_id = uuid4()
        claim = request_dir / f".{stale_id}.{uuid4().hex}.processing"
        _write_mailer_request(claim, request_id=stale_id, payload=payload, delivery_id="old")
        os.utime(claim, (1_800_000_000, 1_800_000_000))
        body = json.loads(claim.read_text(encoding="utf-8"))
        body["lease_expires_at"] = expired.isoformat()
        claim.write_text(json.dumps(body), encoding="utf-8")
    new_id = uuid4()
    new_path = request_dir / f"{new_id}.json"
    _write_mailer_request(new_path, request_id=new_id, payload=payload, delivery_id="newest")
    os.utime(new_path, (1_900_000_000, 1_900_000_000))
    config_file = tmp_path / "resend.json"
    _write_mailer_config(config_file)
    transport = FakeTransport([(200, b'{"id":"email-new"}')] * 20)
    module.serve_control(
        control_dir,
        config_file=config_file,
        once=True,
        transport=transport,
        sleep=lambda _delay: None,
    )
    assert transport.requests
    assert transport.requests[0][0]["Idempotency-Key"].endswith("-newest-g1")
    assert (result_dir / f"{new_id}.json").is_file()
    remaining_claims = list(request_dir.glob(".*.processing"))
    assert len(remaining_claims) == 92


def test_control_result_fsyncs_temp_final_and_parent_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    events: list[str] = []
    real_fsync = os.fsync

    def tracked_fsync(descriptor: int) -> None:
        kind = "directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file"
        events.append(f"fsync:{kind}")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", tracked_fsync)
    request_id = uuid4()
    module._write_control_result(tmp_path, request_id, "2026-07-15", "sent")
    assert events[:3] == ["fsync:file", "fsync:file", "fsync:directory"]
    restarted = json.loads((tmp_path / "results" / f"{request_id}.json").read_text())
    assert restarted["state"] == "sent"


def test_recipient_digest_mismatch_fails_closed_without_provider(tmp_path: Path) -> None:
    module = _module()
    control_dir = tmp_path / "control"
    request_dir = control_dir / "requests"
    request_dir.mkdir(parents=True)
    request_id = uuid4()
    payload = json.loads(SAMPLE.read_text(encoding="utf-8"))
    digest = module._recipient_set_digest(("old-owner@example.com",))
    _write_mailer_request(
        request_dir / f"{request_id}.json",
        request_id=request_id,
        payload=payload,
        recipient_set_digest=digest,
    )
    config_file = tmp_path / "resend.json"
    _write_mailer_config(config_file)
    transport = FakeTransport([(200, b'{"id":"should-not-send"}')])
    assert (
        module.process_control_request(
            request_dir / f"{request_id}.json",
            control_dir=control_dir,
            config_file=config_file,
            transport=transport,
        )
        == "failed"
    )
    assert transport.requests == []
    result = json.loads((control_dir / "results" / f"{request_id}.json").read_text())
    assert result["state"] == "failed"
    assert "re_test_value" not in json.dumps(result)
    assert "old-owner@example.com" not in json.dumps(result)


def test_new_delivery_generation_uses_new_provider_identity() -> None:
    module = _module()
    report = module.renderer.load_report(SAMPLE)
    transport = FakeTransport(
        [(200, b'{"id":"g1"}'), (200, b'{"id":"g2"}')]
    )
    client = module.ResendClient(
        api_key="re_test_value",
        transport=transport,
        sleep=lambda _delay: None,
    )
    client.send(
        report,
        recipients=("security-owner@example.com",),
        delivery_id="10086",
        delivery_generation=1,
    )
    client.send(
        report,
        recipients=("security-owner@example.com",),
        delivery_id="10086",
        delivery_generation=2,
    )
    keys = [item[0]["Idempotency-Key"] for item in transport.requests]
    assert keys == [
        f"security-daily-{report.report_date}-10086-g1",
        f"security-daily-{report.report_date}-10086-g2",
    ]
