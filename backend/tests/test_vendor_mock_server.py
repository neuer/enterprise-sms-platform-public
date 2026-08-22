from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.vendor.mock_server as mock_module

AUTH = {"secretName": "dev-name", "secretKey": "dev-key"}


@pytest.fixture(autouse=True)
def reset_mock() -> None:
    with TestClient(mock_module.app) as client:
        assert client.post("/_mock/state", json={"reset": True}).status_code == 200


def post(client: TestClient, path: str, payload: dict[str, Any] | None = None) -> Any:
    response = client.post(path, json=AUTH | (payload or {}))
    assert response.status_code == 200
    return response.json()


def test_send_generates_reports_after_two_seconds_and_pull_consumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    monkeypatch.setattr(mock_module, "monotonic", lambda: now[0])
    with TestClient(mock_module.app) as client:
        sent = post(
            client,
            "/Sms/Api/Send",
            {
                "mobile": "13800138000,19900001234,19910001234",
                "content": "测试内容",
                "templateId": "",
                "extCode": "",
                "signName": "",
                "timing": "",
                "customId": "custom-1",
            },
        )
        assert sent == {"code": 0, "msg": None, "data": "1"}
        assert post(client, "/Sms/Api/GetReport")["data"] == []

        now[0] += 2.0
        reports = post(client, "/Sms/Api/GetReport")["data"]
        assert [(item["phone"], item["reportStatus"]) for item in reports] == [
            ("13800138000", 1),
            ("19900001234", 2),
        ]
        assert all(item["customId"] == "custom-1" for item in reports)
        assert post(client, "/Sms/Api/GetReport")["data"] == []


@pytest.mark.asyncio
async def test_send_finishes_after_client_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    original_sleep = mock_module.asyncio.sleep

    async def marked_sleep(seconds: float) -> None:
        started.set()
        await original_sleep(seconds)

    monkeypatch.setattr(mock_module.asyncio, "sleep", marked_sleep)
    mock_module.STATE.reset()
    mock_module.STATE.latency_ms = 40
    payload = {
        "mobile": "13800138000",
        "content": "超时验收",
        "customId": "timeout-1",
    }

    async def cancelled_client() -> None:
        await mock_module.send(payload)

    task = asyncio.create_task(cancelled_client())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.08)
    assert [item["customId"] for item in mock_module.STATE.send_calls] == ["timeout-1"]
    assert [item["customId"] for item in mock_module.STATE.pending_reports] == ["timeout-1"]


def test_error_balance_and_latency_are_deterministically_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    with TestClient(mock_module.app) as client:
        client.post(
            "/_mock/state",
            json={"next_send_code": 5002, "times": 1, "balance": 4321, "latency_ms": 25},
        )
        payload = {
            "mobile": "13800138000",
            "content": "验证码123456",
            "customId": "x",
        }
        assert post(client, "/Sms/Api/Send", payload)["code"] == 5002
        assert post(client, "/Sms/Api/Send", payload)["code"] == 0
        assert post(client, "/Sms/Api/GetBalance")["data"] == 4321
        assert sleeps == [0.025, 0.025, 0.025]

        state = client.get("/_mock/state").json()
        assert state["send_calls"][0]["mobile"] == "138****8000"
        assert state["send_calls"][0]["content"] == "验证码123456"
        assert "13800138000" not in str(state)


def test_enqueue_pull_requeue_and_phone_validation() -> None:
    with TestClient(mock_module.app) as client:
        injected = {
            "taskId": "legacy-1",
            "customId": "legacy-x",
            "phone": "13800138000",
            "reportStatus": 1,
        }
        assert client.post("/_mock/state", json={"enqueue_report": injected}).status_code == 200
        first = post(client, "/Sms/Api/GetReport")["data"]
        assert first[0]["taskId"] == "legacy-1"
        client.post("/_mock/state", json={"requeue_reports": True})
        assert post(client, "/Sms/Api/GetReport")["data"][0]["customId"] == "legacy-x"

        reply = {
            "taskId": "1",
            "customId": "x",
            "phone": "13900139000",
            "contents": "TD",
        }
        assert client.post("/_mock/state", json={"enqueue_reply": reply}).status_code == 200
        assert post(client, "/Sms/Api/GetReply")["data"][0]["contents"] == "TD"
        assert post(client, "/Sms/Api/GetReply")["data"] == []
        assert (
            client.post(
                "/_mock/state",
                json={"enqueue_report": injected | {"phone": "not-phone"}},
            ).status_code
            == 422
        )


def test_template_sign_and_state_endpoints_follow_contract() -> None:
    with TestClient(mock_module.app) as client:
        template_id = post(
            client,
            "/Sms/Api/BindTemplate",
            {"templateContent": "验证码{s6}"},
        )["data"]
        sign_id = post(client, "/Sms/Api/BindSign", {"signName": "【青鸾】"})["data"]
        template_state = post(
            client,
            "/Sms/Api/GetTemplateState",
            {"templateIds": [template_id]},
        )["data"]
        sign_state = post(client, "/Sms/Api/GetSignState", {"signIds": [sign_id]})["data"]
        assert template_state == [{"id": template_id, "checkType": 1, "checkRemark": None}]
        assert sign_state == [{"id": sign_id, "checkType": 1, "checkRemark": None}]
        assert client.get("/_mock/state").json()["template_contents"] == ["验证码{s6}"]


def test_callback_sink_fails_n_times_then_records_success() -> None:
    with TestClient(mock_module.app) as client:
        client.post("/_mock/state", json={"callback_failures": 2, "callback_status": 503})
        headers = {"X-Sms-Signature": "abc", "X-Sms-Timestamp": "123"}
        statuses = [
            client.post(
                "/_mock/callback",
                content=b'{"event":"delivered"}',
                headers=headers,
            ).status_code
            for _ in range(3)
        ]
        assert statuses == [503, 503, 200]
        callbacks = client.get("/_mock/callbacks").json()
        assert len(callbacks) == 3
        assert callbacks[0]["raw_body"] == '{"event":"delivered"}'
        assert callbacks[0]["signature"] == "abc"
        assert client.delete("/_mock/callbacks").status_code == 200
        assert client.get("/_mock/callbacks").json() == []


def test_mock_control_restores_faults_and_retains_prior_callback_prefix() -> None:
    with TestClient(mock_module.app) as client:
        assert (
            client.post(
                "/_mock/state",
                json={"next_send_code": 999, "times": 3, "callback_failures": 2},
            ).status_code
            == 200
        )
        headers = {"X-Sms-Signature": "safe", "X-Sms-Timestamp": "123"}
        for _ in range(2):
            client.post("/_mock/callback", content=b"{}", headers=headers)

        state = client.get("/_mock/state").json()
        assert state["callback_failures"] == 0
        assert state["callback_status"] == 500
        assert state["callback_count"] == 2

        restored = client.post(
            "/_mock/state",
            json={
                "clear_send_error": True,
                "callback_failures": 0,
                "callback_status": 503,
                "retain_callback_count": 1,
            },
        )
        assert restored.status_code == 200
        state = restored.json()
        assert state["next_send_code"] is None and state["next_send_times"] == 0
        assert state["callback_status"] == 503 and state["callback_count"] == 1
        assert len(client.get("/_mock/callbacks").json()) == 1
        assert client.post("/_mock/state", json={"retain_callback_count": 2}).status_code == 422
