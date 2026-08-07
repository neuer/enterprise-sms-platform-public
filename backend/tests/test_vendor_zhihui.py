from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
import pytest

from app.vendor.codes import ERROR_POLICIES
from app.vendor.zhihui import (
    VendorApiError,
    VendorProtocolError,
    VendorResponseTooLarge,
    VendorTotalTimeout,
    VendorTransportError,
    ZhihuiClient,
)


def test_default_vendor_client_disables_environment_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    ZhihuiClient(
        base_url="https://vendor.example.test",
        secret_name="name",
        secret_key="key",
    )

    assert captured["trust_env"] is False


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, dict[str, Any]]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append((request.url.path, body))
        return httpx.Response(200, json=self.responses[request.url.path], request=request)


def make_client(transport: httpx.AsyncBaseTransport) -> ZhihuiClient:
    http_client = httpx.AsyncClient(transport=transport, base_url="http://vendor.test")
    return ZhihuiClient(
        base_url="http://vendor.test",
        secret_name="secret-name-value",
        secret_key="secret-key-value",
        http_client=http_client,
    )


@pytest.mark.asyncio
async def test_all_eight_endpoints_use_exact_camel_case_payloads() -> None:
    transport = RecordingTransport(
        {
            "/Sms/Api/Send": {"code": 0, "msg": None, "data": "task-1"},
            "/Sms/Api/GetReport": {
                "code": 0,
                "msg": "",
                "data": [{"customId ": "custom-1", "phone": "13800138000"}],
            },
            "/Sms/Api/GetReply": {"code": 0, "msg": None, "data": []},
            "/Sms/Api/GetBalance": {"code": 0, "msg": None, "data": 10000},
            "/Sms/Api/BindTemplate": {"code": 0, "msg": None, "data": 21},
            "/Sms/Api/GetTemplateState": {"code": 0, "msg": None, "data": []},
            "/Sms/Api/BindSign": {"code": 0, "msg": None, "data": 31},
            "/Sms/Api/GetSignState": {"code": 0, "msg": None, "data": []},
        }
    )
    client = make_client(transport)

    assert (
        await client.send(
            ["13800138000", "13900139000"],
            "验证码123456",
            template_id="9",
            sign_name="【青鸾】",
            custom_id="a" * 32,
        )
        == "task-1"
    )
    reports = await client.get_report()
    assert reports[0]["customId"] == "custom-1"
    assert json.loads(reports.raw_payload)["data"][0]["phone"] == "13800138000"
    assert await client.get_reply() == []
    assert await client.get_balance() == 10000
    assert await client.bind_template("验证码{s6}") == 21
    assert await client.get_template_state([21, 22]) == []
    assert await client.bind_sign("【青鸾】") == 31
    assert await client.get_sign_state([31]) == []
    await client.aclose()

    common = {"secretName": "secret-name-value", "secretKey": "secret-key-value"}
    assert transport.requests == [
        (
            "/Sms/Api/Send",
            common
            | {
                "mobile": "13800138000,13900139000",
                "content": "验证码123456",
                "templateId": "9",
                "extCode": "",
                "signName": "【青鸾】",
                "timing": "",
                "customId": "a" * 32,
            },
        ),
        ("/Sms/Api/GetReport", common),
        ("/Sms/Api/GetReply", common),
        ("/Sms/Api/GetBalance", common),
        ("/Sms/Api/BindTemplate", common | {"templateContent": "验证码{s6}"}),
        ("/Sms/Api/GetTemplateState", common | {"templateIds": [21, 22]}),
        ("/Sms/Api/BindSign", common | {"signName": "【青鸾】"}),
        ("/Sms/Api/GetSignState", common | {"signIds": [31]}),
    ]


@pytest.mark.asyncio
async def test_vendor_error_exposes_complete_policy_without_secret_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = RecordingTransport(
        {"/Sms/Api/Send": {"code": 5002, "msg": "号码13800138000过快", "data": None}}
    )
    client = make_client(transport)

    with caplog.at_level(logging.INFO), pytest.raises(VendorApiError) as captured:
        await client.send(["13800138000"], "测试")
    await client.aclose()

    assert captured.value.code == 5002
    assert captured.value.policy.retry_delays_s == (1, 2, 4, 8, 16)
    logs = caplog.text
    assert "secret-name-value" not in logs
    assert "secret-key-value" not in logs
    assert "13800138000" not in logs


@pytest.mark.asyncio
async def test_network_timeout_is_classified_as_result_unknown() -> None:
    async def timeout(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow vendor")

    client = make_client(httpx.MockTransport(timeout))
    with pytest.raises(VendorTransportError) as captured:
        await client.send(["13800138000"], "测试")
    await client.aclose()

    assert captured.value.result_unknown is True


@pytest.mark.asyncio
async def test_invalid_envelope_is_protocol_error() -> None:
    transport = RecordingTransport({"/Sms/Api/GetBalance": {"Code": 0, "data": 1}})
    client = make_client(transport)
    with pytest.raises(VendorProtocolError):
        await client.get_balance()
    await client.aclose()


def test_error_policy_table_is_complete_and_marks_life_line_actions() -> None:
    assert set(ERROR_POLICIES) == {
        9,
        429,
        999,
        *range(1000, 1013),
        *range(5000, 5004),
        *range(10000, 10015),
    }
    assert ERROR_POLICIES[999].balance_blocked is True
    assert ERROR_POLICIES[999].pause_queues is True
    assert ERROR_POLICIES[1006].shrink_batch_once is True
    assert ERROR_POLICIES[1011].delay_s == 1800
    assert ERROR_POLICIES[10010].delay_s == 300
    assert ERROR_POLICIES[10003].pause_queues is True


@pytest.mark.asyncio
async def test_vendor_response_body_over_hard_limit_fails_closed() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            content=b'{"code":0,"msg":null,"data":' + b"x" * 64 + b"}",
            request=request,
        )
    )
    client = ZhihuiClient(
        base_url="http://vendor.test",
        secret_name="name",
        secret_key="key",
        http_client=httpx.AsyncClient(transport=transport),
        max_response_body_bytes=32,
    )
    with pytest.raises(VendorResponseTooLarge) as captured:
        await client.get_balance()
    await client.aclose()
    assert captured.value.result_unknown is True


@pytest.mark.asyncio
async def test_vendor_declared_content_length_over_limit_fails_before_read() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            content=b"{}",
            headers={"content-length": "4096"},
            request=request,
        )
    )
    client = ZhihuiClient(
        base_url="http://vendor.test",
        secret_name="name",
        secret_key="key",
        http_client=httpx.AsyncClient(transport=transport),
        max_response_body_bytes=1024,
    )
    with pytest.raises(VendorResponseTooLarge):
        await client.get_balance()
    await client.aclose()


@pytest.mark.asyncio
async def test_vendor_total_timeout_covers_slow_chunked_response() -> None:
    async def slow_stream(request: httpx.Request) -> httpx.Response:
        async def body():
            yield b'{"code":0'
            await asyncio.sleep(0.2)
            yield b',"msg":null,"data":1}'

        return httpx.Response(200, content=body(), request=request)

    client = ZhihuiClient(
        base_url="http://vendor.test",
        secret_name="name",
        secret_key="key",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(slow_stream)),
        total_timeout_s=0.05,
    )
    with pytest.raises(VendorTotalTimeout):
        await client.get_balance()
    await client.aclose()
