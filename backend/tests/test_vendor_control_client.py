from __future__ import annotations

import logging

import pytest


class FakeTransport:
    def __init__(self, response: bytes | Exception) -> None:
        self.response = response
        self.frames: list[bytes] = []

    async def exchange(self, frame: bytes) -> bytes:
        self.frames.append(frame)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.mark.asyncio
async def test_client_round_trip_uses_protocol_without_logging_request_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.services.vendor_control_client import VendorControlClient
    from vendor_control_protocol import ControlResponse, decode_request, encode_response

    operation_id = "c0a80101-0000-4000-8000-000000000001"
    sentinel = "ciphertext-formal-key-sentinel"
    transport = FakeTransport(
        encode_response(
            ControlResponse(
                operation_id=operation_id,
                status="ok",
                safe_code=None,
                body={"operation_status": "succeeded"},
            )
        )
    )
    client = VendorControlClient(transport=transport)
    caplog.set_level(logging.DEBUG)

    response = await client.request(
        "install_credentials",
        operation_id=operation_id,
        body={
            "actor": "admin",
            "session_id": "seal-session-1",
            "wrapped_key": "d3JhcHBlZA==",
            "nonce": "bm9uY2U=",
            "ciphertext": sentinel,
            "aad": "YWFk",
            "algorithm": "RSA-OAEP-256+A256GCM",
        },
    )

    assert response.status == "ok"
    assert decode_request(transport.frames[0]).body["ciphertext"] == sentinel
    assert sentinel not in caplog.text


@pytest.mark.parametrize(
    "failure",
    (TimeoutError("private-timeout-detail"), OSError("private-socket-detail")),
)
@pytest.mark.asyncio
async def test_client_maps_transport_failures_without_raw_exception_text(
    failure: Exception,
) -> None:
    from app.services.vendor_control_client import (
        ControlAgentUnavailable,
        VendorControlClient,
    )

    client = VendorControlClient(transport=FakeTransport(failure))

    with pytest.raises(ControlAgentUnavailable) as captured:
        await client.request(
            "status",
            operation_id="c0a80101-0000-4000-8000-000000000001",
            body={},
        )

    assert str(captured.value) == "控制代理不可用"
    assert "private" not in str(captured.value)


@pytest.mark.asyncio
async def test_client_rejects_mismatched_operation_id_and_malformed_response() -> None:
    from app.services.vendor_control_client import (
        ControlAgentUnavailable,
        VendorControlClient,
    )
    from vendor_control_protocol import ControlResponse, encode_response

    transport = FakeTransport(
        encode_response(
            ControlResponse(
                operation_id="c0a80101-0000-4000-8000-000000000099",
                status="ok",
                safe_code=None,
                body={},
            )
        )
    )
    client = VendorControlClient(transport=transport)

    with pytest.raises(ControlAgentUnavailable):
        await client.request(
            "health",
            operation_id="c0a80101-0000-4000-8000-000000000001",
            body={},
        )


def test_default_transport_uses_only_fixed_socket_path() -> None:
    from app.services.vendor_control_client import CONTROL_SOCKET_PATH, UnixSocketTransport

    assert CONTROL_SOCKET_PATH == "/run/vendor-control/vendor-control.sock"
    assert "path" not in UnixSocketTransport.__init__.__annotations__
