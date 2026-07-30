from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))


def test_request_contract_has_exact_operations_and_fields() -> None:
    import vendor_control_protocol as protocol

    assert {
        "schema_version",
        "operation_id",
        "operation",
        "body",
    } == protocol.REQUEST_FIELDS
    assert {
        "health",
        "create_seal_session",
        "install_credentials",
        "reset_configuration",
        "rotate_credentials",
        "activate",
        "pause",
        "resume",
        "status",
    } == protocol.OPERATIONS


def test_request_round_trip_uses_canonical_length_prefixed_json() -> None:
    import vendor_control_protocol as protocol

    request = protocol.ControlRequest(
        operation_id="c0a80101-0000-4000-8000-000000000001",
        operation="activate",
        body={},
    )

    frame = protocol.encode_request(request)
    decoded = protocol.decode_request(frame)

    assert decoded == request
    length = struct.unpack("!I", frame[:4])[0]
    assert length == len(frame) - 4
    assert frame[4:] == json.dumps(
        {
            "body": {},
            "operation": "activate",
            "operation_id": request.operation_id,
            "schema_version": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def test_reset_configuration_wire_request_accepts_only_empty_body() -> None:
    import vendor_control_protocol as protocol

    request = protocol.ControlRequest(
        operation_id="c0a80101-0000-4000-8000-000000000002",
        operation="reset_configuration",
        body={},
    )

    assert protocol.decode_request(protocol.encode_request(request)) == request

    for body in ({"path": "/tmp"}, {"unexpected": True}):
        with pytest.raises(protocol.ProtocolError):
            protocol.encode_request(
                protocol.ControlRequest(
                    operation_id=request.operation_id,
                    operation=request.operation,
                    body=body,
                )
            )


@pytest.mark.parametrize(
    "raw",
    (
        b'{"schema_version":1,"schema_version":1,"operation_id":"x","operation":"status","body":{}}',
        b'{"schema_version":1,"operation_id":"x","operation":"status","body":{},"path":"/tmp"}',
        b'{"schema_version":1,"operation_id":"x","operation":"shell","body":{}}',
    ),
)
def test_request_rejects_duplicate_unknown_fields_and_operations(raw: bytes) -> None:
    import vendor_control_protocol as protocol

    frame = struct.pack("!I", len(raw)) + raw
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_request(frame)


@pytest.mark.parametrize(
    ("operation", "body"),
    (
        ("activate", {"path": "/tmp"}),
        ("pause", {"pause_kind": "../critical"}),
        ("resume", {"pause_kind": "manual\n"}),
        ("install_credentials", {"session_id": "bad\x00"}),
        ("install_credentials", {"argv": ["sh"]}),
    ),
)
def test_operation_body_rejects_unknown_traversal_newline_nul_and_partial_envelopes(
    operation: str,
    body: dict[str, object],
) -> None:
    import vendor_control_protocol as protocol

    request = protocol.ControlRequest(
        operation_id="c0a80101-0000-4000-8000-000000000001",
        operation=operation,
        body=body,
    )
    with pytest.raises(protocol.ProtocolError):
        protocol.encode_request(request)


def test_frame_rejects_oversize_partial_header_and_partial_body() -> None:
    import vendor_control_protocol as protocol

    with pytest.raises(protocol.ProtocolError):
        protocol.decode_request(b"\x00\x01")
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_request(struct.pack("!I", 10) + b"{}")
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_request(struct.pack("!I", protocol.MAX_FRAME_BYTES + 1))


@pytest.mark.parametrize("safe_code", ("bad-code", "x" * 65, "含中文"))
def test_response_rejects_unapproved_safe_codes(safe_code: str) -> None:
    import vendor_control_protocol as protocol

    response = protocol.ControlResponse(
        operation_id="c0a80101-0000-4000-8000-000000000001",
        status="error",
        safe_code=safe_code,
        body={},
    )
    with pytest.raises(protocol.ProtocolError):
        protocol.encode_response(response)


def test_response_rejects_sensitive_or_unknown_body_fields() -> None:
    import vendor_control_protocol as protocol

    for body in ({"secret": "value"}, {"phone": "13800138000"}, {"path": "/tmp"}):
        response = protocol.ControlResponse(
            operation_id="c0a80101-0000-4000-8000-000000000001",
            status="error",
            safe_code="CONTROL_FAILED",
            body=body,
        )
        with pytest.raises(protocol.ProtocolError):
            protocol.encode_response(response)
