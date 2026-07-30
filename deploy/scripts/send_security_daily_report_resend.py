#!/usr/bin/env python3
"""通过固定 Resend HTTPS 端点投递已脱敏的服务器安全日报。"""

import argparse
import http.client
import json
import re
import ssl
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import render_security_daily_report as renderer

RESEND_HOST = "api.resend.com"
RESEND_PORT = 443
RESEND_PATH = "/emails"
RESEND_TIMEOUT_S = 10.0
MAX_RESPONSE_BYTES = 64 * 1024
MAX_HTML_BYTES = 256 * 1024
MAX_TEXT_BYTES = 128 * 1024
MAX_RECIPIENTS = 3
MAX_ATTEMPTS = 3
RETRY_DELAYS_S = (1.0, 2.0)
TRANSIENT_STATUSES = frozenset({408, 429, *range(500, 600)})
DEFAULT_SENDER = "短信平台安全日报 <security-daily@reports.example.com>"
USER_AGENT = "sms-platform-security-daily/1.0"
EMAIL_PATTERN = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63}"
)
PROVIDER_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")


class ResendConfigurationError(ValueError):
    """本地 secret、收件人或输入文件不符合安全约束。"""


class ResendDeliveryError(RuntimeError):
    """Resend 拒绝、传输失败或响应协议异常。"""


class ResendTransport(Protocol):
    """最小同步传输协议，测试可注入无网络实现。"""

    def post(self, *, headers: dict[str, str], body: bytes) -> tuple[int, bytes]: ...


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    """仅保留不含正文、收件人和凭据的投递回执。"""

    email_id: str
    report_date: str


class ResendHttpsTransport:
    """固定连接 api.resend.com，不继承代理且不跟随重定向。"""

    def __init__(self) -> None:
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        self._context = context

    def post(self, *, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
        connection = http.client.HTTPSConnection(
            RESEND_HOST,
            RESEND_PORT,
            timeout=RESEND_TIMEOUT_S,
            context=self._context,
        )
        try:
            connection.request("POST", RESEND_PATH, body=body, headers=headers)
            response = connection.getresponse()
            response_body = response.read(MAX_RESPONSE_BYTES + 1)
        finally:
            connection.close()
        if len(response_body) > MAX_RESPONSE_BYTES:
            raise ResendDeliveryError("Resend response exceeded the safe size limit")
        return response.status, response_body


def read_api_key(path: str | Path) -> str:
    """从 Docker secret 文件读取 Key，仅移除行尾换行。"""

    secret_path = Path(path)
    try:
        value = secret_path.read_text(encoding="utf-8").rstrip("\r\n")
    except (OSError, UnicodeError) as exc:
        raise ResendConfigurationError("Resend API key file is unavailable") from exc
    if not value:
        raise ResendConfigurationError("Resend API key file is empty")
    if len(value) > 512 or any(character.isspace() for character in value):
        raise ResendConfigurationError("Resend API key file has an invalid value")
    return value


def _validate_recipients(recipients: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(recipient.strip() for recipient in recipients)
    if not 1 <= len(normalized) <= MAX_RECIPIENTS:
        raise ResendConfigurationError(
            f"recipient count must be between 1 and {MAX_RECIPIENTS}"
        )
    if any(
        len(recipient) > 254
        or EMAIL_PATTERN.fullmatch(recipient) is None
        or len(recipient.partition("@")[0]) > 64
        for recipient in normalized
    ):
        raise ResendConfigurationError("recipient address is invalid")
    if len({recipient.casefold() for recipient in normalized}) != len(normalized):
        raise ResendConfigurationError("recipient addresses must be unique")
    return normalized


def read_recipients(path: str | Path) -> tuple[str, ...]:
    """从独立只读配置读取收件人；每行一个，井号行为注释。"""

    recipients_path = Path(path)
    try:
        lines = recipients_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ResendConfigurationError("recipient file is unavailable") from exc
    recipients = [
        line.strip()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return _validate_recipients(recipients)


def _render_payload(
    report: renderer.SecurityDailyReport,
    recipients: tuple[str, ...],
) -> bytes:
    html = renderer.render_html(report)
    text = renderer.render_text(report)
    if len(html.encode("utf-8")) > MAX_HTML_BYTES:
        raise ResendConfigurationError("rendered HTML exceeds the safe size limit")
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ResendConfigurationError("rendered text exceeds the safe size limit")
    return json.dumps(
        {
            "from": DEFAULT_SENDER,
            "to": list(recipients),
            "subject": renderer.render_subject(report),
            "html": html,
            "text": text,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _parse_receipt(response_body: bytes, report_date: str) -> DeliveryReceipt:
    try:
        payload = json.loads(response_body)
    except (UnicodeError, ValueError):
        raise ResendDeliveryError("Resend returned an invalid response") from None
    if (
        not isinstance(payload, dict)
        or set(payload) != {"id"}
        or not isinstance(payload["id"], str)
        or PROVIDER_ID_PATTERN.fullmatch(payload["id"]) is None
    ):
        raise ResendDeliveryError("Resend returned an invalid response")
    return DeliveryReceipt(email_id=payload["id"], report_date=report_date)


class ResendClient:
    """使用日报日期幂等键，安全重试瞬时错误且从不记录敏感载荷。"""

    def __init__(
        self,
        *,
        api_key: str,
        transport: ResendTransport | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if not api_key or len(api_key) > 512 or any(
            character.isspace() for character in api_key
        ):
            raise ResendConfigurationError("Resend API key is invalid")
        self._api_key = api_key
        self._transport = transport or ResendHttpsTransport()
        self._sleep = sleep or time.sleep

    def send(
        self,
        report: renderer.SecurityDailyReport,
        *,
        recipients: Sequence[str],
    ) -> DeliveryReceipt:
        normalized_recipients = _validate_recipients(recipients)
        body = _render_payload(report, normalized_recipients)
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Idempotency-Key": f"security-daily-{report.report_date}",
            "User-Agent": USER_AGENT,
        }
        for attempt in range(MAX_ATTEMPTS):
            try:
                status, response_body = self._transport.post(
                    headers=headers,
                    body=body,
                )
            except ResendDeliveryError:
                raise
            except (OSError, http.client.HTTPException):
                if attempt == MAX_ATTEMPTS - 1:
                    raise ResendDeliveryError(
                        "Resend transport failed after safe retries"
                    ) from None
                self._sleep(RETRY_DELAYS_S[attempt])
                continue
            if 200 <= status < 300:
                return _parse_receipt(response_body, report.report_date)
            if status in TRANSIENT_STATUSES and attempt < MAX_ATTEMPTS - 1:
                self._sleep(RETRY_DELAYS_S[attempt])
                continue
            raise ResendDeliveryError(
                f"Resend API rejected the security report (HTTP {status})"
            )
        raise AssertionError("unreachable Resend retry state")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate or send a redacted security daily report through Resend"
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument(
        "--api-key-file",
        type=Path,
        default=Path("/run/secrets/resend_api_key"),
    )
    parser.add_argument(
        "--recipients-file",
        type=Path,
        default=Path("/run/config/recipients"),
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="perform the external delivery; default only validates inputs",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = renderer.load_report(args.input)
        recipients = read_recipients(args.recipients_file)
        if not args.send:
            print(
                "security report validated "
                f"report_date={report.report_date} recipients={len(recipients)}"
            )
            return 0
        api_key = read_api_key(args.api_key_file)
        receipt = ResendClient(api_key=api_key).send(
            report,
            recipients=recipients,
        )
    except (
        renderer.ReportValidationError,
        ResendConfigurationError,
        ResendDeliveryError,
    ) as exc:
        print(f"security report delivery failed: {exc}", file=sys.stderr)
        return 2
    print(f"security report accepted report_date={receipt.report_date}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
