"""智慧信息短信网关的唯一 HTTP 适配层。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from time import perf_counter
from typing import Any, cast

import httpx

from app.settings import Settings, get_settings
from app.vendor.codes import VendorErrorPolicy, policy_for

LOGGER = logging.getLogger(__name__)
PHONE_PATTERN = re.compile(r"^1\d{10}$")
PHONE_IN_TEXT = re.compile(r"(?<!\d)(1\d{10})(?!\d)")
CUSTOM_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{0,36}$")
VENDOR_TIMEOUT_S = 10.0
VENDOR_MAX_RESPONSE_HEADER_BYTES = 64 * 1024
VENDOR_MAX_RESPONSE_BODY_BYTES = 4 * 1024 * 1024


class VendorError(RuntimeError):
    """所有厂商适配错误的共同基类。"""


class VendorTransportError(VendorError):
    """网络或超时错误；发送结果未知，调用方严禁自动重发。"""

    result_unknown = True


class VendorResponseTooLarge(VendorTransportError):
    """厂商响应超过硬上限；请求可能已被上游接收，保持结果未知。"""


class VendorTotalTimeout(VendorTransportError):
    """厂商请求完整生命周期超过绝对总截止时间；保持结果未知。"""


class VendorProtocolError(VendorError):
    """HTTP 状态、JSON 或响应包络不符合厂商契约。"""


class VendorApiError(VendorError):
    """厂商返回非零 code，并携带后续 worker 所需处置策略。"""

    def __init__(self, code: int, message: str | None) -> None:
        self.code = code
        self.vendor_message = message
        self.policy: VendorErrorPolicy = policy_for(code)
        super().__init__(f"vendor error code={code}: {self.policy.description}")


@dataclass(frozen=True, slots=True)
class _VendorPayload:
    data: Any
    raw_body: bytes


class PulledRecords(list[dict[str, Any]]):
    """拉取记录及必须先加密持久化的完整原始响应字节。"""

    def __init__(self, records: list[dict[str, Any]], raw_payload: bytes) -> None:
        super().__init__(records)
        self.raw_payload = raw_payload


@dataclass(frozen=True, slots=True)
class RawPulledPayload:
    """尚未做业务结构校验的拉取响应，供调用方先加密落地。"""

    raw_payload: bytes
    data: Any


def _sanitize_text(value: object) -> str:
    text = "" if value is None else str(value)
    return PHONE_IN_TEXT.sub(
        lambda match: f"{match.group(1)[:3]}****{match.group(1)[-4:]}",
        text,
    )


def _strip_keys(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key).strip(): _strip_keys(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_strip_keys(item) for item in value]
    return value


class ZhihuiClient:
    """封装全部八个厂商接口，业务代码不得直接调用 httpx。"""

    def __init__(
        self,
        *,
        base_url: str,
        secret_name: str,
        secret_key: str,
        http_client: httpx.AsyncClient | None = None,
        max_response_header_bytes: int = VENDOR_MAX_RESPONSE_HEADER_BYTES,
        max_response_body_bytes: int = VENDOR_MAX_RESPONSE_BODY_BYTES,
        total_timeout_s: float = VENDOR_TIMEOUT_S,
    ) -> None:
        if not secret_name or not secret_key:
            raise ValueError("vendor credentials must not be empty")
        if (
            max_response_header_bytes < 1
            or max_response_body_bytes < 1
            or total_timeout_s <= 0
        ):
            raise ValueError("vendor response limits and timeout must be positive")
        self._secret_name = secret_name
        self._secret_key = secret_key
        self._base_url = base_url.rstrip("/")
        self.max_response_header_bytes = max_response_header_bytes
        self.max_response_body_bytes = max_response_body_bytes
        self.total_timeout_s = total_timeout_s
        self._client = http_client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(VENDOR_TIMEOUT_S),
            headers={"Content-Type": "application/json; charset=UTF-8"},
            trust_env=False,
        )

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> ZhihuiClient:
        """仅经 Docker secrets 白名单读取厂商凭据。"""

        selected = settings or get_settings()
        return cls(
            base_url=selected.vendor_base_url,
            secret_name=selected.credential("vendor_secret_name"),
            secret_key=selected.credential("vendor_secret_key"),
        )

    async def __aenter__(self) -> ZhihuiClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(
        self,
        path: str,
        payload: Mapping[str, Any] | None = None,
    ) -> _VendorPayload:
        body = {
            "secretName": self._secret_name,
            "secretKey": self._secret_key,
            **(payload or {}),
        }
        started = perf_counter()
        try:
            async with asyncio.timeout(self.total_timeout_s):
                url = httpx.URL(self._base_url).join(path)
                request = self._client.build_request("POST", url, json=body)
                response = await self._client.send(request, stream=True)
                try:
                    self._check_response_header_limits(response)
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > self.max_response_body_bytes:
                            raise VendorResponseTooLarge(
                                "vendor response body exceeds hard limit"
                            )
                        chunks.append(chunk)
                    content = b"".join(chunks)
                finally:
                    await response.aclose()
        except TimeoutError:
            raise VendorTotalTimeout("vendor request exceeded absolute deadline") from None
        except VendorError:
            raise
        except httpx.TransportError:
            LOGGER.error("vendor transport error endpoint=%s", path)
            raise VendorTransportError("vendor transport failed; result unknown") from None

        duration_ms = round((perf_counter() - started) * 1000)
        if not response.is_success:
            LOGGER.error(
                "vendor HTTP error endpoint=%s status=%s duration_ms=%s",
                path,
                response.status_code,
                duration_ms,
            )
            raise VendorProtocolError(f"vendor HTTP status {response.status_code}")
        try:
            envelope = json.loads(content)
        except (ValueError, UnicodeError):
            raise VendorProtocolError("vendor response is not JSON") from None
        if not isinstance(envelope, dict):
            raise VendorProtocolError("vendor response envelope must be an object")

        code = envelope.get("code")
        message = envelope.get("msg")
        if not isinstance(code, int) or isinstance(code, bool) or "data" not in envelope:
            raise VendorProtocolError("vendor response envelope is invalid")
        if message is not None and not isinstance(message, str):
            raise VendorProtocolError("vendor response msg is invalid")
        if code != 0:
            LOGGER.warning(
                "vendor API error endpoint=%s code=%s msg=%s duration_ms=%s",
                path,
                code,
                _sanitize_text(message),
                duration_ms,
            )
            raise VendorApiError(code, message)
        LOGGER.info("vendor API success endpoint=%s duration_ms=%s", path, duration_ms)
        return _VendorPayload(_strip_keys(envelope["data"]), content)

    def _check_response_header_limits(self, response: httpx.Response) -> None:
        """在读取响应体前校验响应头和声明长度。"""

        header_bytes = sum(
            len(name.encode("ascii", "ignore"))
            + len(value.encode("latin-1", "ignore"))
            + 4
            for name, value in response.headers.multi_items()
        )
        if header_bytes > self.max_response_header_bytes:
            raise VendorResponseTooLarge("vendor response headers exceed hard limit")
        declared = response.headers.get("content-length")
        if declared is not None:
            try:
                declared_length = int(declared)
            except ValueError:
                raise VendorProtocolError("vendor content-length is invalid") from None
            if declared_length < 0 or declared_length > self.max_response_body_bytes:
                raise VendorResponseTooLarge("vendor response body exceeds hard limit")

    async def send(
        self,
        mobiles: Sequence[str],
        content: str,
        *,
        template_id: str = "",
        ext_code: str = "",
        sign_name: str = "",
        custom_id: str = "",
    ) -> str:
        """发送同内容短信；传输异常一律作为结果未知抛出。"""

        if not mobiles or len(set(mobiles)) != len(mobiles):
            raise ValueError("mobiles must be non-empty and unique")
        if any(PHONE_PATTERN.fullmatch(phone) is None for phone in mobiles):
            raise ValueError("invalid mobile")
        if not content or len(content) > 500:
            raise ValueError("content length must be 1..500")
        if CUSTOM_ID_PATTERN.fullmatch(custom_id) is None:
            raise ValueError("custom_id must be at most 36 alphanumeric characters")
        if ext_code and (len(ext_code) > 6 or not ext_code.isdigit()):
            raise ValueError("ext_code must be at most 6 digits")
        response = await self._post(
            "/Sms/Api/Send",
            {
                "mobile": ",".join(mobiles),
                "content": content,
                "templateId": template_id,
                "extCode": ext_code,
                "signName": sign_name,
                "timing": "",
                "customId": custom_id,
            },
        )
        if not isinstance(response.data, str):
            raise VendorProtocolError("Send.data must be a string taskId")
        return response.data

    async def get_report(self) -> PulledRecords:
        """拉取状态报告；调用方必须按先加密落原始响应再解析的协议处理。"""

        response = await self.get_report_raw()
        return PulledRecords(
            self._object_list(response.data, "GetReport"),
            response.raw_payload,
        )

    async def get_report_raw(self) -> RawPulledPayload:
        """返回完整原始字节，业务结构校验必须在持久化之后进行。"""

        response = await self._post("/Sms/Api/GetReport")
        return RawPulledPayload(response.raw_body, response.data)

    async def get_reply(self) -> PulledRecords:
        """拉取上行回复；调用方必须按拉走即消费协议处理。"""

        response = await self.get_reply_raw()
        return PulledRecords(
            self._object_list(response.data, "GetReply"),
            response.raw_payload,
        )

    async def get_reply_raw(self) -> RawPulledPayload:
        """返回未校验的回复原始响应，支持同样的 raw-first 协议。"""

        response = await self._post("/Sms/Api/GetReply")
        return RawPulledPayload(response.raw_body, response.data)

    async def get_balance(self) -> int:
        """查询厂商剩余计费条。"""

        response = await self._post("/Sms/Api/GetBalance")
        if not isinstance(response.data, int) or isinstance(response.data, bool):
            raise VendorProtocolError("GetBalance.data must be an integer")
        return response.data

    async def bind_template(self, template_content: str) -> int:
        """提交已经由 template 服务转换为 `{sN}` 的模板。"""

        response = await self._post(
            "/Sms/Api/BindTemplate",
            {"templateContent": template_content},
        )
        return self._integer_id(response.data, "BindTemplate")

    async def get_template_state(self, template_ids: Sequence[int]) -> list[dict[str, Any]]:
        """批量查询厂商模板审核状态。"""

        response = await self._post(
            "/Sms/Api/GetTemplateState",
            {"templateIds": list(template_ids)},
        )
        return self._object_list(response.data, "GetTemplateState")

    async def bind_sign(self, sign_name: str) -> int:
        """提交包含中文方括号的短信签名。"""

        response = await self._post("/Sms/Api/BindSign", {"signName": sign_name})
        return self._integer_id(response.data, "BindSign")

    async def get_sign_state(self, sign_ids: Sequence[int]) -> list[dict[str, Any]]:
        """批量查询厂商签名审核状态。"""

        response = await self._post(
            "/Sms/Api/GetSignState",
            {"signIds": list(sign_ids)},
        )
        return self._object_list(response.data, "GetSignState")

    @staticmethod
    def _object_list(data: Any, operation: str) -> list[dict[str, Any]]:
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise VendorProtocolError(f"{operation}.data must be an object array")
        return cast(list[dict[str, Any]], data)

    @staticmethod
    def _integer_id(data: Any, operation: str) -> int:
        if not isinstance(data, int) or isinstance(data, bool):
            raise VendorProtocolError(f"{operation}.data must be an integer")
        return data
