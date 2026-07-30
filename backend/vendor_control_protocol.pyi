from dataclasses import dataclass

SCHEMA_VERSION: int
MAX_FRAME_BYTES: int
MAX_STRING_BYTES: int
REQUEST_FIELDS: set[str]
RESPONSE_FIELDS: set[str]
OPERATIONS: set[str]


class ProtocolError(ValueError): ...


@dataclass(frozen=True, slots=True)
class ControlRequest:
    operation_id: str
    operation: str
    body: dict[str, object]


@dataclass(frozen=True, slots=True)
class ControlResponse:
    operation_id: str
    status: str
    safe_code: str | None
    body: dict[str, object]


def encode_request(request: ControlRequest) -> bytes: ...
def decode_request(frame: bytes) -> ControlRequest: ...
def encode_response(response: ControlResponse) -> bytes: ...
def decode_response(frame: bytes) -> ControlResponse: ...
