"""本地源码运行桥；容器构建时由 deploy 中的唯一协议实现覆盖本文件。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_SOURCE = Path(__file__).resolve().parent.parent / "deploy/scripts/vendor_control_protocol.py"
_SPEC = importlib.util.spec_from_file_location("_sms_vendor_control_protocol", _SOURCE)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("vendor control protocol source is unavailable")
_IMPLEMENTATION = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _IMPLEMENTATION
_SPEC.loader.exec_module(_IMPLEMENTATION)


def _export(module: ModuleType, name: str) -> object:
    return getattr(module, name)


SCHEMA_VERSION = _export(_IMPLEMENTATION, "SCHEMA_VERSION")
MAX_FRAME_BYTES = _export(_IMPLEMENTATION, "MAX_FRAME_BYTES")
MAX_STRING_BYTES = _export(_IMPLEMENTATION, "MAX_STRING_BYTES")
REQUEST_FIELDS = _export(_IMPLEMENTATION, "REQUEST_FIELDS")
RESPONSE_FIELDS = _export(_IMPLEMENTATION, "RESPONSE_FIELDS")
OPERATIONS = _export(_IMPLEMENTATION, "OPERATIONS")
ProtocolError = _export(_IMPLEMENTATION, "ProtocolError")
ControlRequest = _export(_IMPLEMENTATION, "ControlRequest")
ControlResponse = _export(_IMPLEMENTATION, "ControlResponse")
encode_request = _export(_IMPLEMENTATION, "encode_request")
decode_request = _export(_IMPLEMENTATION, "decode_request")
encode_response = _export(_IMPLEMENTATION, "encode_response")
decode_response = _export(_IMPLEMENTATION, "decode_response")

__all__ = [
    "MAX_FRAME_BYTES",
    "MAX_STRING_BYTES",
    "OPERATIONS",
    "REQUEST_FIELDS",
    "RESPONSE_FIELDS",
    "SCHEMA_VERSION",
    "ControlRequest",
    "ControlResponse",
    "ProtocolError",
    "decode_request",
    "decode_response",
    "encode_request",
    "encode_response",
]
