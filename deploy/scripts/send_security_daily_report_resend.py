#!/usr/bin/env python3
"""通过固定 Resend HTTPS 端点投递已脱敏的服务器安全日报。"""

import argparse
import http.client
import json
import os
import re
import ssl
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol
from uuid import UUID

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
MAX_CONTROL_REQUEST_BYTES = 384 * 1024
MAX_CONTROL_ERROR_LENGTH = 256
RETRY_DELAYS_S = (1.0, 2.0)
TRANSIENT_STATUSES = frozenset({408, 429, *range(500, 600)})
DEFAULT_SENDER = "短信平台安全日报 <security-daily@reports.neuer.cn>"
USER_AGENT = "sms-platform-security-daily/1.0"
SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
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


@dataclass(frozen=True, slots=True)
class ControlRequest:
    """已校验的 API 脱敏投递请求；不包含 Resend Key 或收件人。"""

    request_id: UUID
    report_date: str
    action: str
    report: renderer.SecurityDailyReport


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


def _control_request(path: Path) -> ControlRequest:
    """严格读取 API 写入的单文件请求，并再次执行 mailer 侧契约校验。"""

    try:
        if path.stat().st_size > MAX_CONTROL_REQUEST_BYTES:
            raise ResendConfigurationError("control request is too large")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or set(value) != {
            "request_id",
            "report_date",
            "action",
            "payload",
        }:
            raise ResendConfigurationError("control request has invalid fields")
        request_id = UUID(str(value["request_id"]))
        report_date = str(value["report_date"])
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", report_date):
            raise ResendConfigurationError("control request date is invalid")
        action = str(value["action"])
        if action not in {"send", "retry"}:
            raise ResendConfigurationError("control request action is invalid")
        report = renderer.parse_report(value["payload"])
    except ResendConfigurationError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, renderer.ReportValidationError):
        raise ResendConfigurationError("control request is invalid") from None
    if report.report_date != report_date:
        raise ResendConfigurationError("control request date does not match report")
    return ControlRequest(request_id, report_date, action, report)


def _write_control_result(
    control_dir: Path,
    request_id: UUID,
    report_date: str,
    state: str,
    error: str | None = None,
) -> None:
    """原子写回不含凭据、地址和正文的投递结果。"""

    if state not in {"sent", "failed"}:
        raise ResendConfigurationError("control result state is invalid")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", report_date):
        raise ResendConfigurationError("control result date is invalid")
    safe_error = error[:MAX_CONTROL_ERROR_LENGTH] if error else None
    result_dir = control_dir / "results"
    result_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = result_dir / f"{request_id}.json"
    temporary = result_dir / f".{request_id}.tmp"
    body = {
        "request_id": str(request_id),
        "report_date": report_date,
        "state": state,
        "completed_at": datetime.now(SHANGHAI_TZ).isoformat(),
        "error": safe_error,
    }
    try:
        temporary.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(destination)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ResendConfigurationError("control result cannot be written") from exc


def process_control_request(
    path: Path,
    *,
    control_dir: Path,
    api_key_file: Path,
    recipients_file: Path,
    transport: ResendTransport | None = None,
    sleep: Callable[[float], None] | None = None,
) -> str:
    """处理一个 API 请求；失败也写回可重试的失败结果。"""

    request = _control_request(path)
    try:
        api_key = read_api_key(api_key_file)
        recipients = read_recipients(recipients_file)
        receipt = ResendClient(
            api_key=api_key,
            transport=transport,
            sleep=sleep,
        ).send(request.report, recipients=recipients)
    except (ResendConfigurationError, ResendDeliveryError) as exc:
        _write_control_result(
            control_dir,
            request.request_id,
            request.report_date,
            "failed",
            str(exc),
        )
        return "failed"
    if receipt.report_date != request.report_date:
        raise ResendDeliveryError("Resend receipt date mismatch")
    _write_control_result(
        control_dir,
        request.request_id,
        request.report_date,
        "sent",
    )
    return "sent"


def serve_control(
    control_dir: Path,
    *,
    api_key_file: Path,
    recipients_file: Path,
    poll_seconds: float = 1.0,
    once: bool = False,
    transport: ResendTransport | None = None,
    sleep: Callable[[float], None] | None = None,
) -> int:
    """轮询受控请求目录；只由此进程加载 Resend secret 并访问外网。"""

    if poll_seconds <= 0 or poll_seconds > 60:
        raise ResendConfigurationError("control polling interval is invalid")
    request_dir = control_dir / "requests"
    request_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    (control_dir / "results").mkdir(mode=0o700, parents=True, exist_ok=True)
    wait = sleep or time.sleep
    while True:
        processed = False
        try:
            candidates = sorted(request_dir.glob("*.json"))
        except OSError as exc:
            raise ResendConfigurationError("control request directory is unavailable") from exc
        for path in candidates:
            claim = path.with_name(f".{path.stem}.{os.getpid()}.processing")
            try:
                path.replace(claim)
            except OSError:
                continue
            processed = True
            try:
                state = process_control_request(
                    claim,
                    control_dir=control_dir,
                    api_key_file=api_key_file,
                    recipients_file=recipients_file,
                    transport=transport,
                    sleep=sleep,
                )
                print(f"security report control result state={state}")
            except (ResendConfigurationError, ResendDeliveryError) as exc:
                # malformed requests cannot be safely associated with a date;
                # remove them and leave a generic operator-visible error.
                print(f"security report control request rejected: {exc}", file=sys.stderr)
            finally:
                try:
                    claim.unlink(missing_ok=True)
                except OSError:
                    pass
        if once:
            return 0
        if not processed:
            wait(poll_seconds)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate or send a redacted security daily report through Resend"
    )
    parser.add_argument("--input", type=Path)
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
    parser.add_argument(
        "--control-dir",
        type=Path,
        default=Path("/run/control"),
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="serve API control requests; only this process reads the Resend secret",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="process one control directory sweep and exit",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.serve or args.once:
        try:
            return serve_control(
                args.control_dir,
                api_key_file=args.api_key_file,
                recipients_file=args.recipients_file,
                once=True if args.once else False,
            )
        except (ResendConfigurationError, ResendDeliveryError) as exc:
            print(f"security report control failed: {exc}", file=sys.stderr)
            return 2
    if args.input is None:
        _parser().error("--input is required unless --serve or --once is used")
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
