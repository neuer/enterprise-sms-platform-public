#!/usr/bin/env python3
"""通过固定 Resend HTTPS 端点投递已脱敏的服务器安全日报。"""

import argparse
import hashlib
import http.client
import json
import os
import re
import signal
import ssl
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

import render_security_daily_report as renderer

RESEND_HOST = "api.resend.com"
RESEND_PORT = 443
RESEND_PATH = "/emails"
RESEND_TIMEOUT_S = 10.0
RESEND_ABSOLUTE_DEADLINE_S = 15.0
MAX_RESPONSE_BYTES = 64 * 1024
MAX_HTML_BYTES = 256 * 1024
MAX_TEXT_BYTES = 128 * 1024
MAX_RECIPIENTS = 3
MAX_ATTEMPTS = 3
MAX_CONTROL_REQUEST_BYTES = 384 * 1024
MAX_CONTROL_ERROR_LENGTH = 256
CLAIM_LEASE_SECONDS = 45
MAX_STALE_CLAIMS_PER_SWEEP = 8
LEASE_FIELDS = frozenset(
    {"claim_id", "claimed_at", "lease_expires_at", "boot_generation"}
)
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
    config_version: int
    report: renderer.SecurityDailyReport
    delivery_id: str
    delivery_generation: int = 1
    recipient_set_digest: str = ""


@dataclass(frozen=True, slots=True)
class MailerConfiguration:
    """由安全日报 UI 同步的 Resend Key 和收件人配置。"""

    api_key: str
    recipients: tuple[str, ...]
    config_version: int


class ResendHttpsTransport:
    """固定连接 api.resend.com，不继承代理且不跟随重定向。"""

    def __init__(self) -> None:
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        self._context = context

    @staticmethod
    @contextmanager
    def _absolute_deadline(seconds: float) -> Iterator[None]:
        """Linux 单进程 mailer 的可执行总截止，覆盖 DNS/TCP/TLS/读写。"""

        previous_handler = signal.getsignal(signal.SIGALRM)

        def expired(_signum: int, _frame: object) -> None:
            raise TimeoutError("Resend absolute deadline exceeded")

        signal.signal(signal.SIGALRM, expired)
        previous_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
        try:
            yield
        finally:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)
            signal.signal(signal.SIGALRM, previous_handler)

    def post(self, *, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
        with self._absolute_deadline(RESEND_ABSOLUTE_DEADLINE_S):
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


def _validate_api_key(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ResendConfigurationError("Resend API key is empty")
    if len(normalized) > 512 or any(character.isspace() for character in normalized):
        raise ResendConfigurationError("Resend API key has an invalid value")
    return normalized


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


def read_mailer_configuration(path: str | Path) -> MailerConfiguration:
    """读取安全日报配置页同步的 JSON 配置。"""

    config_path = Path(path)
    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ResendConfigurationError("mailer configuration is unavailable") from exc
    if not isinstance(value, dict) or set(value) != {
        "api_key",
        "recipients",
        "config_version",
    }:
        raise ResendConfigurationError("mailer configuration has invalid fields")
    api_key = value.get("api_key")
    recipients = value.get("recipients")
    config_version = value.get("config_version")
    if not isinstance(api_key, str) or not isinstance(recipients, list) or not all(
        isinstance(item, str) for item in recipients
    ) or not isinstance(config_version, int) or isinstance(config_version, bool) or config_version < 1:
        raise ResendConfigurationError("mailer configuration has invalid values")
    return MailerConfiguration(
        api_key=_validate_api_key(api_key),
        recipients=_validate_recipients(recipients),
        config_version=config_version,
    )


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
        request_id: UUID | None = None,
        delivery_id: str | None = None,
        delivery_generation: int = 1,
    ) -> DeliveryReceipt:
        normalized_recipients = _validate_recipients(recipients)
        body = _render_payload(report, normalized_recipients)
        identity = delivery_id or (str(request_id) if request_id is not None else "")
        generation = delivery_generation if delivery_generation >= 1 else 1
        idempotency_key = f"security-daily-{report.report_date}"
        if identity:
            # 同一 Delivery Generation 复用稳定键；配置变化后的新世代使用新键。
            idempotency_key = f"{idempotency_key}-{identity}-g{generation}"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
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
        fields = set(value)
        required = {"request_id", "report_date", "action", "config_version", "payload"}
        optional = {
            "delivery_id",
            "delivery_generation",
            "recipient_set_digest",
            *LEASE_FIELDS,
        }
        if not required <= fields or not fields <= required | optional:
            raise ResendConfigurationError("control request has invalid fields")
        request_id = UUID(str(value["request_id"]))
        report_date = str(value["report_date"])
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", report_date):
            raise ResendConfigurationError("control request date is invalid")
        action = str(value["action"])
        if action not in {"send", "retry"}:
            raise ResendConfigurationError("control request action is invalid")
        config_version = value["config_version"]
        if (
            not isinstance(config_version, int)
            or isinstance(config_version, bool)
            or config_version < 1
        ):
            raise ResendConfigurationError("control request configuration version is invalid")
        raw_delivery_id = value.get("delivery_id", str(request_id))
        delivery_id = str(raw_delivery_id)
        if (
            not delivery_id
            or len(delivery_id) > 128
            or any(character.isspace() for character in delivery_id)
        ):
            raise ResendConfigurationError("control request delivery identity is invalid")
        report = renderer.parse_report(value["payload"])
        raw_generation = value.get("delivery_generation", 1)
        delivery_generation = int(raw_generation)
        if (
            not isinstance(raw_generation, int)
            or isinstance(raw_generation, bool)
            or delivery_generation < 1
        ):
            raise ResendConfigurationError("control request delivery generation is invalid")
        recipient_digest = str(value.get("recipient_set_digest", ""))
        if recipient_digest and (
            len(recipient_digest) != 64
            or any(character not in "0123456789abcdef" for character in recipient_digest)
        ):
            raise ResendConfigurationError("control request recipient digest is invalid")
    except ResendConfigurationError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, renderer.ReportValidationError):
        raise ResendConfigurationError("control request is invalid") from None
    if report.report_date != report_date:
        raise ResendConfigurationError("control request date does not match report")
    return ControlRequest(
        request_id,
        report_date,
        action,
        config_version,
        report,
        delivery_id,
        delivery_generation,
        recipient_digest,
    )


def _fsync_path(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _recipient_set_digest(recipients: Sequence[str]) -> str:
    material = ",".join(sorted(item.strip().casefold() for item in recipients))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _boot_generation(control_dir: Path) -> str:
    path = control_dir / ".mailer-boot"
    if path.is_file():
        value = path.read_text(encoding="utf-8").strip()
        if value and len(value) <= 64 and " " not in value:
            return value
    generation = uuid4().hex
    path.write_text(generation + "\n", encoding="utf-8")
    path.chmod(0o600)
    return generation


def _request_id_from_claim(path: Path) -> UUID | None:
    name = path.name
    if not name.startswith(".") or not name.endswith(".processing"):
        return None
    body = name[1 : -len(".processing")]
    request_part, separator, _claim = body.rpartition(".")
    if separator != ".":
        return None
    try:
        return UUID(request_part)
    except ValueError:
        return None


def _requeue_claim(claim: Path, request_id: UUID) -> None:
    payload = json.loads(claim.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ResendConfigurationError("stale claim is invalid")
    for field in LEASE_FIELDS:
        payload.pop(field, None)
    destination = claim.with_name(f"{request_id}.json")
    temporary = claim.with_name(f".{request_id}.{uuid4().hex}.requeue")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.chmod(0o600)
    _fsync_path(temporary)
    temporary.replace(destination)
    _fsync_path(destination)
    with suppress(OSError):
        claim.unlink(missing_ok=True)


def _write_claim_lease(claim: Path, *, claim_id: str, boot_generation: str) -> None:
    payload = json.loads(claim.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ResendConfigurationError("control request is invalid")
    now = datetime.now(SHANGHAI_TZ)
    payload.update(
        {
            "claim_id": claim_id,
            "claimed_at": now.isoformat(),
            "lease_expires_at": (now + timedelta(seconds=CLAIM_LEASE_SECONDS)).isoformat(),
            "boot_generation": boot_generation,
        }
    )
    temporary = claim.with_name(f".{claim_id}.lease.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.chmod(0o600)
    _fsync_path(temporary)
    temporary.replace(claim)
    _fsync_path(claim)
    _fsync_path(claim.parent)


def recover_stale_claims(
    request_dir: Path,
    result_dir: Path,
    *,
    now: datetime | None = None,
    limit: int = MAX_STALE_CLAIMS_PER_SWEEP,
) -> int:
    """回收过期 Claim；活跃租约不动，已有 Result 只清理 Claim。"""

    current = now or datetime.now(SHANGHAI_TZ)
    recovered = 0
    claims = sorted(
        request_dir.glob(".*.processing"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for claim in claims:
        if recovered >= limit:
            break
        request_id = _request_id_from_claim(claim)
        if request_id is None:
            continue
        if (result_dir / f"{request_id}.json").is_file():
            with suppress(OSError):
                claim.unlink(missing_ok=True)
            recovered += 1
            continue
        try:
            payload = json.loads(claim.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            print("security report stale claim unreadable", file=sys.stderr)
            continue
        expires_raw = payload.get("lease_expires_at") if isinstance(payload, dict) else None
        try:
            expires = (
                datetime.fromisoformat(str(expires_raw))
                if expires_raw
                else current - timedelta(seconds=1)
            )
        except ValueError:
            expires = current - timedelta(seconds=1)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=SHANGHAI_TZ)
        if expires > current:
            continue
        try:
            _requeue_claim(claim, request_id)
        except (OSError, ResendConfigurationError):
            print("security report stale claim recover failed", file=sys.stderr)
            continue
        recovered += 1
    return recovered


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
    temporary = result_dir / f".{request_id}.{uuid4().hex}.tmp"
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
        _fsync_path(temporary)
        temporary.replace(destination)
        _fsync_path(destination)
        _fsync_path(result_dir)
    except OSError as exc:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise ResendConfigurationError("control result cannot be written") from exc


def process_control_request(
    path: Path,
    *,
    control_dir: Path,
    config_file: Path,
    transport: ResendTransport | None = None,
    sleep: Callable[[float], None] | None = None,
) -> str:
    """处理一个 API 请求；失败也写回可重试的失败结果。"""

    request = _control_request(path)
    try:
        configuration = read_mailer_configuration(config_file)
        if configuration.config_version != request.config_version:
            raise ResendConfigurationError("mailer configuration version mismatch")
        expected_digest = _recipient_set_digest(configuration.recipients)
        if (
            request.recipient_set_digest
            and request.recipient_set_digest != expected_digest
        ):
            raise ResendConfigurationError(
                "mailer recipient set does not match delivery generation"
            )
        receipt = ResendClient(
            api_key=configuration.api_key,
            transport=transport,
            sleep=sleep,
        ).send(
            request.report,
            recipients=configuration.recipients,
            request_id=request.request_id,
            delivery_id=request.delivery_id,
            delivery_generation=request.delivery_generation,
        )
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
    config_file: Path,
    poll_seconds: float = 1.0,
    once: bool = False,
    transport: ResendTransport | None = None,
    sleep: Callable[[float], None] | None = None,
) -> int:
    """轮询受控请求目录；只由此进程读取 UI 同步的 mailer 配置并访问外网。"""

    if poll_seconds <= 0 or poll_seconds > 60:
        raise ResendConfigurationError("control polling interval is invalid")
    request_dir = control_dir / "requests"
    request_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    result_dir = control_dir / "results"
    result_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    boot_generation = _boot_generation(control_dir)
    wait = sleep or time.sleep
    while True:
        processed = False
        recover_stale_claims(request_dir, result_dir)
        try:
            candidates = sorted(
                request_dir.glob("*.json"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError as exc:
            raise ResendConfigurationError("control request directory is unavailable") from exc
        for path in candidates:
            claim_id = uuid4().hex
            claim = path.with_name(f".{path.stem}.{claim_id}.processing")
            try:
                path.replace(claim)
            except OSError:
                continue
            processed = True
            try:
                _write_claim_lease(
                    claim, claim_id=claim_id, boot_generation=boot_generation
                )
                state = process_control_request(
                    claim,
                    control_dir=control_dir,
                    config_file=config_file,
                    transport=transport,
                    sleep=sleep,
                )
                print(f"security report control result state={state}")
            except (ResendConfigurationError, ResendDeliveryError) as exc:
                # malformed requests cannot be safely associated with a date;
                # remove them and leave a generic operator-visible error.
                print(f"security report control request rejected: {exc}", file=sys.stderr)
            finally:
                request_id = _request_id_from_claim(claim)
                if request_id is not None and (
                    result_dir / f"{request_id}.json"
                ).is_file():
                    with suppress(OSError):
                        claim.unlink(missing_ok=True)
        if once:
            return 0
        if not processed:
            wait(poll_seconds)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Serve redacted security daily report delivery through Resend"
    )
    parser.add_argument(
        "--config-file",
        type=Path,
        default=Path("/run/config/resend.json"),
    )
    parser.add_argument(
        "--control-dir",
        type=Path,
        default=Path("/run/control"),
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="serve API control requests using the UI-synchronized configuration",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="process one control directory sweep and exit",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.serve and not args.once:
        _parser().error("--serve or --once is required")
    try:
        return serve_control(
            args.control_dir,
            config_file=args.config_file,
            once=bool(args.once),
        )
    except (ResendConfigurationError, ResendDeliveryError) as exc:
        print(f"security report control failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
