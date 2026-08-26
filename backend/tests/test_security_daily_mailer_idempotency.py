from __future__ import annotations

import json
import sys
from pathlib import Path
from uuid import uuid4

SCRIPTS = Path(__file__).resolve().parents[2] / "deploy" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import render_security_daily_report as renderer  # noqa: E402
import send_security_daily_report_resend as mailer  # noqa: E402

SAMPLE = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "templates"
    / "security_daily_report.sample.json"
)


class CaptureTransport:
    def __init__(self) -> None:
        self.headers: dict[str, str] | None = None

    def post(self, *, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
        del body
        self.headers = headers
        return 200, b'{"id":"email_abc123"}'


def test_resend_idempotency_key_binds_stable_delivery_identity_not_new_request() -> None:
    report = renderer.parse_report(json.loads(SAMPLE.read_text(encoding="utf-8")))
    transport = CaptureTransport()
    client = mailer.ResendClient(api_key="re_test_value", transport=transport, sleep=lambda _: None)
    first_request = uuid4()
    second_request = uuid4()
    client.send(
        report,
        recipients=("security-owner@example.com",),
        request_id=first_request,
        delivery_id="10086",
    )
    first_key = transport.headers["Idempotency-Key"] if transport.headers else ""
    client.send(
        report,
        recipients=("security-owner@example.com",),
        request_id=second_request,
        delivery_id="10086",
    )
    second_key = transport.headers["Idempotency-Key"] if transport.headers else ""
    assert first_key == second_key
    assert first_key == f"security-daily-{report.report_date}-10086-g1"
    assert str(first_request) not in first_key
    assert str(second_request) not in second_key
    assert "re_test_value" not in first_key


def test_control_request_accepts_delivery_id(tmp_path: Path) -> None:
    request_id = uuid4()
    payload = json.loads(SAMPLE.read_text(encoding="utf-8"))
    path = tmp_path / f"{request_id}.json"
    path.write_text(
        json.dumps(
            {
                "request_id": str(request_id),
                "report_date": payload["report_date"],
                "action": "send",
                "config_version": 2,
                "delivery_id": "10086",
                "payload": payload,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    request = mailer._control_request(path)
    assert request.delivery_id == "10086"
    assert request.config_version == 2
