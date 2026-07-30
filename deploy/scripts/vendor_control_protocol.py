#!/usr/bin/env python3
"""vendor-control-agent 与普通 API 共用的严格本地帧协议。"""

from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

SCHEMA_VERSION = 1
MAX_FRAME_BYTES = 65_536
MAX_STRING_BYTES = 32_768
REQUEST_FIELDS = {"schema_version", "operation_id", "operation", "body"}
RESPONSE_FIELDS = {"schema_version", "operation_id", "status", "safe_code", "body"}
OPERATIONS = {
    "health",
    "create_seal_session",
    "install_credentials",
    "reset_configuration",
    "rotate_credentials",
    "activate",
    "pause",
    "resume",
    "status",
}
_EMPTY_BODY_OPERATIONS = {"health", "reset_configuration", "activate", "status"}
_SEAL_SESSION_FIELDS = {"operation", "actor"}
_CREDENTIAL_FIELDS = {
    "actor",
    "session_id",
    "wrapped_key",
    "nonce",
    "ciphertext",
    "aad",
    "algorithm",
}
_SAFE_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_SAFE_RESPONSE_FIELDS = {
    "aad",
    "agent_status",
    "batch_no",
    "checkpoint_id",
    "configured",
    "credential_state",
    "daily_limit",
    "expires_at",
    "heartbeat_at",
    "installed_at",
    "operation_status",
    "public_key",
    "recipient_count",
    "remaining_segments",
    "session_id",
    "status",
    "used_segments",
    "vendor_code",
}


class ProtocolError(ValueError):
    """协议或帧不符合固定合同；异常不携带原始 payload。"""


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


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("协议包含重复字段")
        result[key] = value
    return result


def _require_uuid(value: object) -> str:
    if type(value) is not str:
        raise ProtocolError("operation id 无效")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        raise ProtocolError("operation id 无效") from None
    if str(parsed) != value:
        raise ProtocolError("operation id 无效")
    return value


def _require_safe_string(value: object, *, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise ProtocolError("协议字符串无效")
    raw = value.encode("utf-8")
    if (not allow_empty and not raw) or len(raw) > MAX_STRING_BYTES:
        raise ProtocolError("协议字符串无效")
    if "\x00" in value or "\r" in value or "\n" in value or "../" in value:
        raise ProtocolError("协议字符串无效")
    return value


def _validate_request_body(operation: str, body: object) -> dict[str, object]:
    if type(body) is not dict:
        raise ProtocolError("operation body 无效")
    typed = cast(dict[str, object], body)
    fields = set(typed)
    if operation in _EMPTY_BODY_OPERATIONS:
        if fields:
            raise ProtocolError("operation body 字段无效")
        return typed
    if operation == "create_seal_session":
        if fields != _SEAL_SESSION_FIELDS:
            raise ProtocolError("operation body 字段无效")
        if typed["operation"] not in {"install_credentials", "rotate_credentials"}:
            raise ProtocolError("seal session 操作无效")
        _require_safe_string(typed["actor"])
        return typed
    if operation in {"install_credentials", "rotate_credentials"}:
        if fields != _CREDENTIAL_FIELDS:
            raise ProtocolError("operation body 字段无效")
        for field in _CREDENTIAL_FIELDS:
            _require_safe_string(typed[field])
        if typed["algorithm"] != "RSA-OAEP-256+A256GCM":
            raise ProtocolError("凭据封装算法无效")
        return typed
    if operation == "pause":
        if fields != {"pause_kind"} or typed["pause_kind"] != "manual":
            raise ProtocolError("暂停类型无效")
        return typed
    if operation == "resume":
        if fields != {"pause_kind"} or typed["pause_kind"] not in {"manual", "critical"}:
            raise ProtocolError("恢复类型无效")
        return typed
    raise ProtocolError("operation 无效")


def _validate_response_body(body: object) -> dict[str, object]:
    if type(body) is not dict:
        raise ProtocolError("响应 body 无效")
    typed = cast(dict[str, object], body)
    if not set(typed).issubset(_SAFE_RESPONSE_FIELDS):
        raise ProtocolError("响应 body 字段无效")
    for value in typed.values():
        if value is None or type(value) in {bool, int}:
            continue
        _require_safe_string(value, allow_empty=True)
    return typed


def _encode_document(document: dict[str, object]) -> bytes:
    try:
        payload = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ProtocolError("协议 JSON 无效") from None
    if not payload or len(payload) > MAX_FRAME_BYTES:
        raise ProtocolError("协议帧长度无效")
    return struct.pack("!I", len(payload)) + payload


def _decode_document(frame: bytes) -> dict[str, object]:
    if len(frame) < 4:
        raise ProtocolError("协议帧不完整")
    declared = struct.unpack("!I", frame[:4])[0]
    if declared < 1 or declared > MAX_FRAME_BYTES or len(frame) != declared + 4:
        raise ProtocolError("协议帧长度无效")
    try:
        decoded = json.loads(
            frame[4:].decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except ProtocolError:
        raise
    except (UnicodeError, json.JSONDecodeError):
        raise ProtocolError("协议 JSON 无效") from None
    if type(decoded) is not dict:
        raise ProtocolError("协议 envelope 无效")
    return cast(dict[str, object], decoded)


def encode_request(request: ControlRequest) -> bytes:
    operation_id = _require_uuid(request.operation_id)
    if request.operation not in OPERATIONS:
        raise ProtocolError("operation 无效")
    body = _validate_request_body(request.operation, request.body)
    return _encode_document(
        {
            "schema_version": SCHEMA_VERSION,
            "operation_id": operation_id,
            "operation": request.operation,
            "body": body,
        }
    )


def decode_request(frame: bytes) -> ControlRequest:
    document = _decode_document(frame)
    if set(document) != REQUEST_FIELDS or document.get("schema_version") != SCHEMA_VERSION:
        raise ProtocolError("请求 envelope 字段无效")
    operation_id = _require_uuid(document["operation_id"])
    operation = document["operation"]
    if type(operation) is not str or operation not in OPERATIONS:
        raise ProtocolError("operation 无效")
    body = _validate_request_body(operation, document["body"])
    return ControlRequest(operation_id, operation, body)


def encode_response(response: ControlResponse) -> bytes:
    operation_id = _require_uuid(response.operation_id)
    if response.status not in {"ok", "error"}:
        raise ProtocolError("响应状态无效")
    if response.status == "ok" and response.safe_code is not None:
        raise ProtocolError("成功响应不得携带错误码")
    if response.status == "error" and (
        type(response.safe_code) is not str
        or _SAFE_CODE.fullmatch(response.safe_code) is None
    ):
        raise ProtocolError("安全错误码无效")
    body = _validate_response_body(response.body)
    return _encode_document(
        {
            "schema_version": SCHEMA_VERSION,
            "operation_id": operation_id,
            "status": response.status,
            "safe_code": response.safe_code,
            "body": body,
        }
    )


def decode_response(frame: bytes) -> ControlResponse:
    document = _decode_document(frame)
    if set(document) != RESPONSE_FIELDS or document.get("schema_version") != SCHEMA_VERSION:
        raise ProtocolError("响应 envelope 字段无效")
    response = ControlResponse(
        operation_id=_require_uuid(document["operation_id"]),
        status=str(document["status"]),
        safe_code=(str(document["safe_code"]) if document["safe_code"] is not None else None),
        body=_validate_response_body(document["body"]),
    )
    # 复用编码端校验状态与 safe_code，但不返回重新编码结果。
    encode_response(response)
    return response
