"""结果回调临时 body 构造、HMAC 签名与受控 HTTP 投递。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from app.core.correlation import current_correlation_id
from app.services.crypto import CryptoService, EncryptionContext

LOGGER = logging.getLogger(__name__)


def build_callback_ssl_context(
    *,
    ca_file: Path | None,
    cert_file: Path | None,
    key_file: Path | None,
) -> ssl.SSLContext:
    """构造生产 HTTPS/mTLS 客户端上下文；私钥只由 SSL 库从挂载文件读取。"""

    context = ssl.create_default_context(cafile=str(ca_file) if ca_file else None)
    if (cert_file is None) != (key_file is None):
        raise ValueError("callback mTLS certificate and key must be configured together")
    if cert_file is not None and key_file is not None:
        context.load_cert_chain(str(cert_file), str(key_file))
    return context


@dataclass(frozen=True, slots=True)
class CallbackTaskRef:
    task_id: int
    event_id: UUID
    app_name: str
    event: str
    url: str
    callback_secret_enc: bytes
    callback_secret_key_version: int
    signature_version: int
    batch_id: int
    message_ids: tuple[int, ...]
    message_times: tuple[datetime, ...]
    correlation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class BatchFinishedData:
    batch_no: str
    biz_id: str | None
    category: str
    status: str
    total: int
    delivered: int
    failed: int
    unknown: int
    finished_at: datetime


@dataclass(frozen=True, slots=True)
class CallbackMessage:
    phone_enc: bytes
    phone_hmac: str
    key_version: int
    status: str
    report_desc: str
    report_time: datetime


@dataclass(frozen=True, slots=True)
class MessageReportData:
    batch_no: str
    biz_id: str | None
    items: tuple[CallbackMessage, ...]


@dataclass(frozen=True, slots=True)
class CallbackMaterial:
    task: CallbackTaskRef
    batch: BatchFinishedData | None = None
    message_report: MessageReportData | None = None


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    success: bool
    http_code: int | None
    error: str | None = None


class CallbackMaterialRepository(Protocol):
    async def load_material(
        self,
        task_id: int,
        lease_id: UUID,
    ) -> CallbackMaterial | None: ...


class OutboundValidator(Protocol):
    async def validate_for_outbound(self, url: str) -> Any: ...


class CallbackTransport(Protocol):
    async def post(
        self,
        *,
        url: str,
        raw_body: bytes,
        headers: dict[str, str],
        timeout_s: float,
        follow_redirects: bool,
        approved_ips: tuple[str, ...] = (),
        original_hostname: str | None = None,
    ) -> int: ...


class HttpxCallbackTransport:
    """复用有界连接池、固定单截止时间且禁重定向的 callback HTTP 边界。"""

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        ssl_context: ssl.SSLContext | None = None,
        max_connections: int = 20,
        max_keepalive_connections: int = 10,
        max_response_header_bytes: int = 16 * 1024,
        max_response_body_bytes: int = 4 * 1024,
    ) -> None:
        if min(
            max_connections,
            max_keepalive_connections,
            max_response_header_bytes,
            max_response_body_bytes,
        ) < 1:
            raise ValueError("callback transport limits must be positive")
        self.transport = transport
        self.clock = clock
        self.max_response_header_bytes = max_response_header_bytes
        self.max_response_body_bytes = max_response_body_bytes
        limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=min(max_keepalive_connections, max_connections),
            keepalive_expiry=30,
        )
        owned_transport = transport or httpx.AsyncHTTPTransport(
            verify=ssl_context or True,
            limits=limits,
            retries=0,
        )
        self._client = httpx.AsyncClient(
            timeout=None,
            follow_redirects=False,
            transport=owned_transport,
            trust_env=False,
        )

    async def aclose(self) -> None:
        """关闭复用连接池；常驻 worker 通常在进程退出时统一回收。"""

        await self._client.aclose()

    async def _bounded_status(self, request: httpx.Request) -> int:
        response = await self._client.send(request, stream=True)
        try:
            header_bytes = sum(
                len(name.encode("ascii", "ignore")) + len(value.encode("latin-1", "ignore")) + 4
                for name, value in response.headers.multi_items()
            )
            if header_bytes > self.max_response_header_bytes:
                raise ValueError("callback response headers exceed limit")
            declared = response.headers.get("content-length")
            if declared is not None:
                try:
                    declared_length = int(declared)
                except ValueError as exc:
                    raise ValueError("callback response content-length is invalid") from exc
                if declared_length < 0:
                    raise ValueError("callback response content-length is invalid")
                if declared_length > self.max_response_body_bytes:
                    raise ValueError("callback response body exceeds limit")
            received = 0
            async for chunk in response.aiter_bytes():
                received += len(chunk)
                if received > self.max_response_body_bytes:
                    raise ValueError("callback response body exceeds limit")
            return response.status_code
        finally:
            await response.aclose()

    async def post(
        self,
        *,
        url: str,
        raw_body: bytes,
        headers: dict[str, str],
        timeout_s: float,
        follow_redirects: bool,
        approved_ips: tuple[str, ...] = (),
        original_hostname: str | None = None,
    ) -> int:
        original_url = httpx.URL(url)
        parsed = urlsplit(url)
        target_ips: tuple[str | None, ...] = approved_ips or (None,)
        deadline = self.clock() + timeout_s
        last_connect_error: httpx.ConnectError | httpx.ConnectTimeout | None = None
        if follow_redirects:
            raise ValueError("callback redirects are forbidden")
        async with asyncio.timeout(timeout_s):
            for index, target_ip in enumerate(target_ips):
                remaining = deadline - self.clock()
                if remaining <= 0:
                    if last_connect_error is not None:
                        raise last_connect_error
                    raise httpx.ConnectTimeout("callback timeout budget exhausted")
                remaining_addresses = len(target_ips) - index
                connect_budget = remaining / remaining_addresses
                request_url = (
                    original_url
                    if target_ip is None
                    else original_url.copy_with(host=target_ip)
                )
                request_headers = dict(headers)
                if target_ip is not None:
                    request_headers["Host"] = parsed.netloc
                request = self._client.build_request(
                    "POST", request_url, content=raw_body, headers=request_headers
                )
                if original_hostname is not None:
                    request.extensions["sni_hostname"] = original_hostname
                request.extensions["timeout"] = {
                    "connect": connect_budget,
                    "read": remaining,
                    "write": remaining,
                    "pool": remaining,
                }
                try:
                    return await self._bounded_status(request)
                except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                    last_connect_error = exc
                    continue
        assert last_connect_error is not None
        raise last_connect_error


def utc_now() -> datetime:
    return datetime.now(UTC)


class CallbackDelivery:
    """受控解密只发生在 deliver 调用栈内，生成的 raw body 永不返回仓储。"""

    def __init__(
        self,
        repository: CallbackMaterialRepository,
        crypto: CryptoService,
        validator: OutboundValidator,
        transport: CallbackTransport,
        *,
        clock: Callable[[], datetime] = utc_now,
        timeout_s: float = 5,
    ) -> None:
        self.repository = repository
        self.crypto = crypto
        self.validator = validator
        self.transport = transport
        self.clock = clock
        self.timeout_s = timeout_s

    def _body(self, material: CallbackMaterial) -> dict[str, Any]:
        if material.task.event == "batch.finished" and material.batch is not None:
            batch_data = material.batch
            return {
                "event_id": str(material.task.event_id),
                "event": "batch.finished",
                "batch_no": batch_data.batch_no,
                "biz_id": batch_data.biz_id,
                "category": batch_data.category,
                "status": batch_data.status,
                "total": batch_data.total,
                "delivered": batch_data.delivered,
                "failed": batch_data.failed,
                "unknown": batch_data.unknown,
                "finished_at": batch_data.finished_at.isoformat(),
            }
        if material.task.event == "message.report" and material.message_report is not None:
            report_data = material.message_report
            return {
                "event_id": str(material.task.event_id),
                "event": "message.report",
                "batch_no": report_data.batch_no,
                "biz_id": report_data.biz_id,
                "items": [
                    {
                        "phone": self.crypto.decrypt_phone(
                            item.phone_enc,
                            item.key_version,
                            item.phone_hmac,
                        ),
                        "status": item.status,
                        "report_desc": item.report_desc,
                        "report_time": item.report_time.isoformat(),
                    }
                    for item in report_data.items
                ],
            }
        raise ValueError("callback material does not match event")

    async def deliver(self, task_id: int, lease_id: UUID) -> DeliveryOutcome:
        try:
            material = await self.repository.load_material(task_id, lease_id)
            if material is None:
                raise LookupError("callback task unavailable")
            approved = await self.validator.validate_for_outbound(material.task.url)
            if isinstance(approved, str):
                url = approved
                approved_ips: tuple[str, ...] = ()
                original_hostname = None
            else:
                url = approved.url
                approved_ips = approved.addresses
                original_hostname = approved.hostname
            body = self._body(material)
            raw_body = json.dumps(
                body,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
            moment = self.clock()
            if moment.tzinfo is None or moment.utcoffset() is None:
                raise ValueError("callback clock must include timezone")
            timestamp = str(int(moment.timestamp()))
            packed_version = int.from_bytes(
                material.task.callback_secret_enc[:2],
                "big",
            )
            if packed_version != material.task.callback_secret_key_version:
                raise ValueError("callback secret version mismatch")
            secret = self.crypto.decrypt_bound_packed_text(
                material.task.callback_secret_enc,
                EncryptionContext(
                    domain="callback-secret",
                    table="app",
                    column="callback_secret_enc",
                    object_id=material.task.app_name,
                ),
            )
            if material.task.signature_version != 1:
                raise ValueError("unsupported callback signature version")
            signature = hmac.new(
                secret.encode(),
                timestamp.encode() + b"." + raw_body,
                hashlib.sha256,
            ).hexdigest()
            status = await self.transport.post(
                url=url,
                raw_body=raw_body,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "X-Sms-Event-Id": str(material.task.event_id),
                    "X-Sms-Correlation-Id": str(
                        material.task.correlation_id or material.task.event_id
                    ),
                    "X-Sms-Timestamp": timestamp,
                    "X-Sms-Signature": signature,
                    "X-Sms-Signature-Version": str(material.task.signature_version),
                    "X-Sms-Secret-Version": str(
                        material.task.callback_secret_key_version
                    ),
                },
                timeout_s=self.timeout_s,
                follow_redirects=False,
                approved_ips=approved_ips,
                original_hostname=original_hostname,
            )
            return DeliveryOutcome(200 <= status < 300, status)
        except Exception as error:
            LOGGER.error(
                "callback_delivery_failed",
                extra={
                    "correlation_id": str(current_correlation_id()),
                    "callback_task_id": task_id,
                    "error_type": type(error).__name__,
                },
                exc_info=(type(error), error, error.__traceback__),
            )
            return DeliveryOutcome(False, None, type(error).__name__)
