"""普通 API 到 root vendor-control-agent 的固定 Unix Socket 客户端。"""

from __future__ import annotations

import asyncio
import struct
from contextlib import suppress
from typing import Protocol

from vendor_control_protocol import (
    MAX_FRAME_BYTES,
    ControlRequest,
    ControlResponse,
    ProtocolError,
    decode_response,
    encode_request,
)

CONTROL_SOCKET_PATH = "/run/vendor-control/vendor-control.sock"
CONTROL_TIMEOUT_SECONDS = 3.0


class ControlAgentUnavailable(ConnectionError):
    """代理不可达或协议无效；永不包含底层异常文本。"""


class ControlTransport(Protocol):
    async def exchange(self, frame: bytes) -> bytes: ...


class UnixSocketTransport:
    """只连接编译期固定的本地 socket，不接收调用方路径。"""

    def __init__(self, timeout_seconds: float = CONTROL_TIMEOUT_SECONDS) -> None:
        if timeout_seconds <= 0:
            raise ValueError("control timeout must be positive")
        self.timeout_seconds = timeout_seconds

    async def exchange(self, frame: bytes) -> bytes:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(CONTROL_SOCKET_PATH),
            timeout=self.timeout_seconds,
        )
        try:
            writer.write(frame)
            await asyncio.wait_for(writer.drain(), timeout=self.timeout_seconds)
            header = await asyncio.wait_for(
                reader.readexactly(4),
                timeout=self.timeout_seconds,
            )
            declared = struct.unpack("!I", header)[0]
            if declared < 1 or declared > MAX_FRAME_BYTES:
                raise ProtocolError("响应帧长度无效")
            payload = await asyncio.wait_for(
                reader.readexactly(declared),
                timeout=self.timeout_seconds,
            )
            return header + payload
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()


class VendorControlClient:
    """发送严格操作并把所有传输/协议故障映射为统一安全错误。"""

    def __init__(self, transport: ControlTransport | None = None) -> None:
        self.transport = transport or UnixSocketTransport()

    async def request(
        self,
        operation: str,
        *,
        operation_id: str,
        body: dict[str, object],
    ) -> ControlResponse:
        try:
            frame = encode_request(ControlRequest(operation_id, operation, body))
            response = decode_response(await self.transport.exchange(frame))
            if response.operation_id != operation_id:
                raise ProtocolError("响应 operation id 不匹配")
            return response
        except (OSError, TimeoutError, asyncio.IncompleteReadError, ProtocolError, ValueError):
            raise ControlAgentUnavailable("控制代理不可用") from None
