from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import inspect
import json
import ssl
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypedDict
from uuid import UUID

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

import app.services.callback as callback_module
from app.services.app_management import CallbackUrlValidator
from app.services.callback import (
    BatchFinishedData,
    CallbackDelivery,
    CallbackMaterial,
    CallbackMessage,
    CallbackTaskRef,
    HttpxCallbackTransport,
    MessageReportData,
    build_callback_ssl_context,
)
from app.services.crypto import CryptoService, EncryptionContext
from app.vendor.mock_server import app as mock_app

EVENT_ID = UUID("10000000-0000-4000-8000-000000000009")
LEASE_ID = UUID("20000000-0000-4000-8000-000000000009")
CORRELATION_ID = UUID("30000000-0000-4000-8000-000000000009")


class CallbackPostArgs(TypedDict):
    url: str
    raw_body: bytes
    headers: dict[str, str]
    timeout_s: float
    follow_redirects: bool
    approved_ips: tuple[str, ...]
    original_hostname: str | None


def crypto() -> CryptoService:
    key = base64.b64encode(b"c" * 32).decode()
    return CryptoService.from_secret_values(key, key)


class FakeRepository:
    def __init__(self, material: CallbackMaterial | None) -> None:
        self.material = material

    async def load_material(
        self,
        task_id: int,
        lease_id: UUID,
    ) -> CallbackMaterial | None:
        assert task_id == 9 and lease_id == LEASE_ID
        return self.material


class FakeValidator:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[str] = []

    async def validate_for_outbound(self, url: str) -> str:
        self.calls.append(url)
        if self.fail:
            raise ValueError("blocked")
        return url


class FakeTransport:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.calls: list[dict[str, Any]] = []

    async def post(self, **values: Any) -> int:
        self.calls.append(values)
        return self.status


def task(event: str, secret: str) -> CallbackTaskRef:
    app_name = "callback-app"
    secret_enc = crypto().encrypt_bound_packed_text(
        secret,
        EncryptionContext(
            domain="callback-secret",
            table="app",
            column="callback_secret_enc",
            object_id=app_name,
        ),
    )
    return CallbackTaskRef(
        task_id=9,
        event_id=EVENT_ID,
        app_name=app_name,
        event=event,
        url="http://callback.internal/hook",
        callback_secret_enc=secret_enc,
        callback_secret_key_version=int.from_bytes(secret_enc[:2], "big"),
        signature_version=1,
        batch_id=8,
        message_ids=(21,),
        message_times=(datetime(2026, 7, 12, 8, 0, tzinfo=UTC),),
        correlation_id=CORRELATION_ID,
    )


def test_callback_transport_explicitly_disables_environment_proxies() -> None:
    source = inspect.getsource(callback_module.HttpxCallbackTransport)
    assert "trust_env=False" in source


@pytest.mark.asyncio
async def test_pinned_callback_transport_disables_cross_hostname_keepalive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204, request=request)

    def transport_factory(**kwargs: Any) -> httpx.AsyncBaseTransport:
        captured.update(kwargs)
        return httpx.MockTransport(ok)

    monkeypatch.setattr(callback_module.httpx, "AsyncHTTPTransport", transport_factory)
    transport = HttpxCallbackTransport()

    limits = captured["limits"]
    assert isinstance(limits, httpx.Limits)
    assert limits.max_keepalive_connections == 0
    assert captured["http2"] is False
    await transport.aclose()


@pytest.mark.asyncio
async def test_batch_finished_body_is_canonical_and_signed_over_exact_raw_bytes() -> None:
    secret = "callback-secret"
    material = CallbackMaterial(
        task("batch.finished", secret),
        batch=BatchFinishedData(
            "BATCH-1",
            "biz-1",
            "notice",
            "completed",
            3,
            2,
            1,
            0,
            datetime(2026, 7, 12, 16, 0, tzinfo=UTC),
        ),
    )
    transport = FakeTransport()
    validator = FakeValidator()
    delivery = CallbackDelivery(
        FakeRepository(material),
        crypto(),
        validator,
        transport,
        clock=lambda: datetime(2026, 7, 12, 8, 0, tzinfo=UTC),
    )

    outcome = await delivery.deliver(9, LEASE_ID)

    assert outcome.success and outcome.http_code == 200
    call = transport.calls[0]
    assert call["timeout_s"] == 5
    assert call["follow_redirects"] is False
    assert validator.calls == [material.task.url]
    parsed = json.loads(call["raw_body"])
    assert parsed["event"] == "batch.finished"
    assert parsed["event_id"] == str(EVENT_ID)
    assert parsed["batch_no"] == "BATCH-1"
    assert call["headers"]["X-Sms-Event-Id"] == str(EVENT_ID)
    assert call["headers"]["X-Sms-Correlation-Id"] == str(CORRELATION_ID)
    timestamp = call["headers"]["X-Sms-Timestamp"]
    expected = hmac.new(
        secret.encode(),
        timestamp.encode() + b"." + call["raw_body"],
        hashlib.sha256,
    ).hexdigest()
    assert call["headers"]["X-Sms-Signature"] == expected


@pytest.mark.asyncio
async def test_callback_retry_keeps_task_secret_and_signature_version_after_rotation() -> None:
    snapshotted_secret = "secret-before-rotation"
    rotated_secret = "secret-after-rotation"
    material = CallbackMaterial(
        task("batch.finished", snapshotted_secret),
        batch=BatchFinishedData(
            "BATCH-1", None, "notice", "completed", 1, 1, 0, 0, datetime.now(UTC)
        ),
    )
    transport = FakeTransport()
    delivery = CallbackDelivery(
        FakeRepository(material),
        crypto(),
        FakeValidator(),
        transport,
        clock=lambda: datetime(2026, 7, 12, 8, 0, tzinfo=UTC),
    )

    assert (await delivery.deliver(9, LEASE_ID)).success
    assert (await delivery.deliver(9, LEASE_ID)).success
    assert len(transport.calls) == 2
    for call in transport.calls:
        timestamp = call["headers"]["X-Sms-Timestamp"]
        expected = hmac.new(
            snapshotted_secret.encode(),
            timestamp.encode() + b"." + call["raw_body"],
            hashlib.sha256,
        ).hexdigest()
        rotated = hmac.new(
            rotated_secret.encode(),
            timestamp.encode() + b"." + call["raw_body"],
            hashlib.sha256,
        ).hexdigest()
        assert call["headers"]["X-Sms-Signature"] == expected
        assert call["headers"]["X-Sms-Signature"] != rotated
        assert call["headers"]["X-Sms-Signature-Version"] == "1"
        assert call["headers"]["X-Sms-Secret-Version"] == "1"


@pytest.mark.asyncio
async def test_message_report_decrypts_phone_only_while_building_delivery_body() -> None:
    protected = crypto().protect_phone("13800138000")
    task_ref = task("message.report", "callback-secret")
    message = CallbackMessage(
        protected.phone_enc,
        protected.phone_hmac,
        protected.key_version,
        "delivered",
        "DELIVRD",
        datetime(2026, 7, 12, 8, 1, tzinfo=UTC),
    )
    material = CallbackMaterial(
        task_ref,
        message_report=MessageReportData("BATCH-1", None, (message,)),
    )
    transport = FakeTransport()

    await CallbackDelivery(
        FakeRepository(material),
        crypto(),
        FakeValidator(),
        transport,
    ).deliver(9, LEASE_ID)

    assert not hasattr(task_ref, "phone") and not hasattr(task_ref, "body")
    assert not hasattr(message, "phone")
    assert json.loads(transport.calls[0]["raw_body"])["items"][0]["phone"] == "13800138000"


@pytest.mark.asyncio
async def test_non_2xx_and_outbound_validation_failure_become_safe_outcomes() -> None:
    material = CallbackMaterial(
        task("batch.finished", "secret"),
        batch=BatchFinishedData("B", None, "notice", "completed", 1, 0, 1, 0, datetime.now(UTC)),
    )
    failed = await CallbackDelivery(
        FakeRepository(material),
        crypto(),
        FakeValidator(),
        FakeTransport(500),
    ).deliver(9, LEASE_ID)
    blocked_transport = FakeTransport()
    blocked = await CallbackDelivery(
        FakeRepository(material),
        crypto(),
        FakeValidator(fail=True),
        blocked_transport,
    ).deliver(9, LEASE_ID)

    assert failed.success is False and failed.http_code == 500
    assert blocked.success is False and blocked.error == "ValueError"
    assert blocked_transport.calls == []


@pytest.mark.asyncio
async def test_real_transport_to_mock_sink_preserves_raw_signature_and_failure_code() -> None:
    secret = "callback-secret"
    material = CallbackMaterial(
        replace(task("batch.finished", secret), url="http://mock/_mock/callback"),
        batch=BatchFinishedData(
            "BATCH-1", None, "notice", "completed", 1, 1, 0, 0, datetime.now(UTC)
        ),
    )
    transport = httpx.ASGITransport(app=mock_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mock") as client:
        await client.delete("/_mock/callbacks")
        delivery = CallbackDelivery(
            FakeRepository(material),
            crypto(),
            FakeValidator(),
            HttpxCallbackTransport(transport=transport),
            clock=lambda: datetime(2026, 7, 12, 8, 0, tzinfo=UTC),
        )
        assert (await delivery.deliver(9, LEASE_ID)).success
        received = (await client.get("/_mock/callbacks")).json()[0]
        expected = hmac.new(
            secret.encode(),
            received["timestamp"].encode() + b"." + received["raw_body"].encode(),
            hashlib.sha256,
        ).hexdigest()
        assert received["signature"] == expected
        assert json.loads(received["raw_body"])["event_id"] == str(EVENT_ID)

        assert (await delivery.deliver(9, LEASE_ID)).success
        duplicate = (await client.get("/_mock/callbacks")).json()
        assert len(duplicate) == 2
        assert {
            json.loads(item["raw_body"])["event_id"] for item in duplicate
        } == {str(EVENT_ID)}

        await client.post("/_mock/state", json={"callback_failures": 1, "callback_status": 500})
        failed = await delivery.deliver(9, LEASE_ID)
        assert failed.success is False and failed.http_code == 500


@pytest.mark.asyncio
async def test_callback_connects_to_approved_ip_with_original_host_and_sni() -> None:
    resolver_calls = 0

    def rebinding_resolver(_hostname: str) -> list[str]:
        nonlocal resolver_calls
        resolver_calls += 1
        return ["10.1.2.3"] if resolver_calls == 1 else ["127.0.0.1"]

    seen: dict[str, Any] = {}

    async def record(request: httpx.Request) -> httpx.Response:
        seen["host"] = request.url.host
        seen["header"] = request.headers["host"]
        seen["sni"] = request.extensions["sni_hostname"]
        return httpx.Response(200, request=request)

    material = CallbackMaterial(
        replace(task("batch.finished", "secret"), url="https://callback.example/hook"),
        batch=BatchFinishedData("B", None, "notice", "completed", 1, 1, 0, 0, datetime.now(UTC)),
    )
    outcome = await CallbackDelivery(
        FakeRepository(material),
        crypto(),
        CallbackUrlValidator(
            "10.0.0.0/8",
            resolver=rebinding_resolver,
            allow_http=False,
        ),
        HttpxCallbackTransport(transport=httpx.MockTransport(record)),
    ).deliver(9, LEASE_ID)

    assert outcome.success
    assert resolver_calls == 1
    assert seen == {
        "host": "10.1.2.3",
        "header": "callback.example",
        "sni": "callback.example",
    }


@pytest.mark.asyncio
async def test_callback_tries_next_approved_ip_only_after_connect_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    attempts: list[tuple[str, str, str]] = []

    async def connect_then_succeed(request: httpx.Request) -> httpx.Response:
        attempts.append(
            (request.url.host, request.headers["host"], request.extensions["sni_hostname"])
        )
        if request.url.host == "10.1.1.1":
            raise httpx.ConnectError("unreachable", request=request)
        return httpx.Response(200, request=request)

    status = await HttpxCallbackTransport(transport=httpx.MockTransport(connect_then_succeed)).post(
        url="https://callback.example/hook",
        raw_body=b"{}",
        headers={"Content-Type": "application/json"},
        timeout_s=5,
        follow_redirects=False,
        approved_ips=("10.1.1.1", "10.1.1.2"),
        original_hostname="callback.example",
    )
    assert status == 200
    assert attempts == [
        ("10.1.1.1", "callback.example", "callback.example"),
        ("10.1.1.2", "callback.example", "callback.example"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["response", "read_timeout", "write_timeout"])
async def test_callback_does_not_retry_an_ip_after_request_may_have_been_sent(
    failure: str,
) -> None:
    attempts: list[str] = []

    async def fail_after_connect(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.host)
        if failure == "read_timeout":
            raise httpx.ReadTimeout("uncertain", request=request)
        if failure == "write_timeout":
            raise httpx.WriteTimeout("uncertain", request=request)
        return httpx.Response(500, request=request)

    transport = HttpxCallbackTransport(transport=httpx.MockTransport(fail_after_connect))
    values: CallbackPostArgs = {
        "url": "https://callback.example/hook",
        "raw_body": b"{}",
        "headers": {"Content-Type": "application/json"},
        "timeout_s": 5,
        "follow_redirects": False,
        "approved_ips": ("10.1.1.1", "10.1.1.2"),
        "original_hostname": "callback.example",
    }
    if failure in {"read_timeout", "write_timeout"}:
        expected = httpx.ReadTimeout if failure == "read_timeout" else httpx.WriteTimeout
        with pytest.raises(expected):
            await transport.post(**values)
    else:
        assert await transport.post(**values) == 500
    assert attempts == ["10.1.1.1"]


@pytest.mark.asyncio
async def test_callback_raises_last_connect_error_after_all_approved_ips_fail() -> None:
    attempts: list[str] = []

    async def unavailable(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.host)
        raise httpx.ConnectError(f"failed:{request.url.host}", request=request)

    with pytest.raises(httpx.ConnectError, match="failed:10.1.1.2"):
        await HttpxCallbackTransport(transport=httpx.MockTransport(unavailable)).post(
            url="https://callback.example/hook",
            raw_body=b"{}",
            headers={},
            timeout_s=5,
            follow_redirects=False,
            approved_ips=("10.1.1.1", "10.1.1.2"),
            original_hostname="callback.example",
        )
    assert attempts == ["10.1.1.1", "10.1.1.2"]


@pytest.mark.asyncio
async def test_connect_timeout_uses_shrinking_single_deadline_budget() -> None:
    moments = iter([100.0, 100.0, 102.0, 102.0])
    budgets: list[dict[str, float]] = []

    async def timeout_then_succeed(request: httpx.Request) -> httpx.Response:
        budgets.append(request.extensions["timeout"])
        if request.url.host == "10.1.1.1":
            raise httpx.ConnectTimeout("first timed out", request=request)
        return httpx.Response(200, request=request)

    status = await HttpxCallbackTransport(
        transport=httpx.MockTransport(timeout_then_succeed),
        clock=lambda: next(moments),
    ).post(
        url="https://callback.example/hook",
        raw_body=b"{}",
        headers={},
        timeout_s=5,
        follow_redirects=False,
        approved_ips=("10.1.1.1", "10.1.1.2"),
        original_hostname="callback.example",
    )
    assert status == 200
    assert budgets[0]["connect"] == pytest.approx(2.5)
    assert budgets[1]["connect"] == pytest.approx(3.0)
    assert budgets[1]["read"] == pytest.approx(3.0)


@pytest.mark.asyncio
async def test_all_connect_timeouts_raise_last_timeout() -> None:
    attempts: list[str] = []

    async def timeout(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.host)
        raise httpx.ConnectTimeout(f"timeout:{request.url.host}", request=request)

    with pytest.raises(httpx.ConnectTimeout, match="timeout:10.1.1.2"):
        await HttpxCallbackTransport(transport=httpx.MockTransport(timeout)).post(
            url="https://callback.example/hook",
            raw_body=b"{}",
            headers={},
            timeout_s=5,
            follow_redirects=False,
            approved_ips=("10.1.1.1", "10.1.1.2"),
            original_hostname="callback.example",
        )
    assert attempts == ["10.1.1.1", "10.1.1.2"]


@pytest.mark.asyncio
async def test_callback_wall_clock_timeout_stops_before_trying_next_ip() -> None:
    attempts: list[str] = []

    async def hangs(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.host)
        await asyncio.sleep(1)
        return httpx.Response(200, request=request)

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await HttpxCallbackTransport(transport=httpx.MockTransport(hangs)).post(
            url="https://callback.example/hook",
            raw_body=b"{}",
            headers={},
            timeout_s=0.02,
            follow_redirects=False,
            approved_ips=("10.1.1.1", "10.1.1.2"),
            original_hostname="callback.example",
        )
    assert time.monotonic() - started < 0.25
    assert attempts == ["10.1.1.1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "body", "message"),
    (
        ({"content-length": "9"}, b"123456789", "body"),
        ({}, b"123456789", "body"),
        ({"x-large": "123456789"}, b"", "headers"),
    ),
)
async def test_callback_response_memory_is_bounded(
    headers: dict[str, str],
    body: bytes,
    message: str,
) -> None:
    async def oversized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=headers, content=body, request=request)

    transport = HttpxCallbackTransport(
        transport=httpx.MockTransport(oversized),
        max_response_body_bytes=8,
        max_response_header_bytes=8 if message == "headers" else 1024,
    )
    with pytest.raises(ValueError, match=message):
        await transport.post(
            url="https://callback.example/hook",
            raw_body=b"{}",
            headers={},
            timeout_s=1,
            follow_redirects=False,
        )
    await transport.aclose()


@pytest.mark.asyncio
async def test_callback_stops_streaming_oversized_response_at_body_limit() -> None:
    class LargeStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.chunks_read = 0

        async def __aiter__(self) -> AsyncIterator[bytes]:
            for _ in range(1_000_000):
                self.chunks_read += 1
                yield b"1234"

    stream = LargeStream()

    async def oversized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, request=request)

    transport = HttpxCallbackTransport(
        transport=httpx.MockTransport(oversized),
        max_response_body_bytes=8,
    )
    with pytest.raises(ValueError, match="body"):
        await transport.post(
            url="https://callback.example/hook",
            raw_body=b"{}",
            headers={},
            timeout_s=1,
            follow_redirects=False,
        )
    assert stream.chunks_read == 3
    await transport.aclose()


@pytest.mark.asyncio
async def test_callback_transport_reuses_one_client_pool() -> None:
    client_ids: list[int] = []

    async def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204, request=request)

    transport = HttpxCallbackTransport(transport=httpx.MockTransport(ok))
    client_ids.append(id(transport._client))
    for _ in range(2):
        assert (
            await transport.post(
                url="https://callback.example/hook",
                raw_body=b"{}",
                headers={},
                timeout_s=1,
                follow_redirects=False,
            )
            == 204
        )
        client_ids.append(id(transport._client))
    assert len(set(client_ids)) == 1
    await transport.aclose()


@pytest.mark.asyncio
async def test_callback_redirect_is_returned_without_following_location() -> None:
    requested: list[str] = []

    async def redirect(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "https://127.0.0.1/private"},
            request=request,
        )

    transport = HttpxCallbackTransport(transport=httpx.MockTransport(redirect))
    assert (
        await transport.post(
            url="https://callback.internal/hook",
            raw_body=b"{}",
            headers={},
            timeout_s=1,
            follow_redirects=False,
        )
        == 302
    )
    assert requested == ["https://callback.internal/hook"]
    await transport.aclose()


def _write_test_certificate_chain(root: Path) -> tuple[Path, Path, Path, Path, Path]:
    """生成仅供本测试进程使用的 CA、服务端证书和客户端证书。"""

    now = datetime.now(UTC)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "callback-test-ca")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=False,
                key_cert_sign=True,
                key_agreement=False,
                content_commitment=False,
                data_encipherment=False,
                encipher_only=False,
                decipher_only=False,
                crl_sign=True,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    def issue_leaf(
        common_name: str,
        usage: x509.ObjectIdentifier,
        *,
        server_name: str | None = None,
    ) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        builder = (
            x509.CertificateBuilder()
            .subject_name(
                x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
            )
            .issuer_name(ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
        )
        if server_name is not None:
            builder = builder.add_extension(
                x509.SubjectAlternativeName([x509.DNSName(server_name)]),
                critical=False,
            )
        return key, builder.sign(ca_key, hashes.SHA256())

    server_key, server_cert = issue_leaf(
        "callback.internal",
        ExtendedKeyUsageOID.SERVER_AUTH,
        server_name="callback.internal",
    )
    client_key, client_cert = issue_leaf(
        "sms-callback-client",
        ExtendedKeyUsageOID.CLIENT_AUTH,
    )
    paths = tuple(
        root / name
        for name in ("ca.pem", "server.pem", "server-key.pem", "client.pem", "client-key.pem")
    )
    ca_path, server_path, server_key_path, client_path, client_key_path = paths
    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    server_path.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    client_path.write_bytes(client_cert.public_bytes(serialization.Encoding.PEM))
    for path, key in ((server_key_path, server_key), (client_key_path, client_key)):
        path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    return paths


@pytest.mark.asyncio
async def test_callback_https_mtls_uses_real_chain_pinned_ip_host_and_sni(
    tmp_path: Path,
) -> None:
    ca_file, server_cert, server_key, client_cert, client_key = (
        _write_test_certificate_chain(tmp_path)
    )
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(server_cert, server_key)
    server_context.load_verify_locations(cafile=ca_file)
    server_context.verify_mode = ssl.CERT_REQUIRED
    peer_verified: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    async def receive(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        ssl_object = writer.get_extra_info("ssl_object")
        if not peer_verified.done():
            peer_verified.set_result(bool(ssl_object and ssl_object.getpeercert()))
        headers = await reader.readuntil(b"\r\n\r\n")
        content_length = 0
        for line in headers.decode("latin-1").splitlines():
            if line.lower().startswith("content-length:"):
                content_length = int(line.split(":", 1)[1].strip())
        if content_length:
            await reader.readexactly(content_length)
        writer.write(
            b"HTTP/1.1 204 No Content\r\n"
            b"Content-Length: 0\r\n"
            b"Connection: close\r\n\r\n"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(
        receive,
        "127.0.0.1",
        0,
        ssl=server_context,
    )
    port = int(server.sockets[0].getsockname()[1])
    transport = HttpxCallbackTransport(
        ssl_context=build_callback_ssl_context(
            ca_file=ca_file,
            cert_file=client_cert,
            key_file=client_key,
        )
    )
    try:
        assert (
            await transport.post(
                url=f"https://callback.internal:{port}/hook",
                raw_body=b"{}",
                headers={"Content-Type": "application/json"},
                timeout_s=3,
                follow_redirects=False,
                approved_ips=("127.0.0.1",),
                original_hostname="callback.internal",
            )
            == 204
        )
        assert await asyncio.wait_for(peer_verified, timeout=1)
    finally:
        await transport.aclose()
        server.close()
        await server.wait_closed()
