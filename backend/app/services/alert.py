"""统一告警事件校验、原子去重编排与外部渠道投递。"""

from __future__ import annotations

import json
import logging
import re
import smtplib
from collections.abc import Iterable
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit

import httpx

from app.core.bounded_executor import run_bounded

LOGGER = logging.getLogger(__name__)
PHONE_IN_TEXT = re.compile(r"(?<!\d)1\d{10}(?!\d)")
PHONE_KEYS = {"phone", "phones", "mobile", "mobiles"}
ALERT_LEVELS = {"info", "warn", "crit"}
WECOM_HOST = "qyapi.weixin.qq.com"
WECOM_PATH = "/cgi-bin/webhook/send"


@dataclass(frozen=True, slots=True)
class SmtpRouting:
    """无认证企业 SMTP relay 的非敏感路由参数。"""

    host: str
    port: int
    sender: str
    recipients: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AlertRouting:
    """一次告警使用的渠道快照。"""

    wecom_webhook: str
    smtp: SmtpRouting | None


@dataclass(frozen=True, slots=True)
class AlertEvent:
    """通过 PII 校验后才允许进入持久层与渠道的告警事件。"""

    alert_type: str
    level: str
    title: str
    detail: dict[str, Any]
    dedup_key: str


class AlertRepository(Protocol):
    async def load_routing(self) -> AlertRouting: ...

    async def claim(
        self,
        *,
        alert_type: str,
        level: str,
        title: str,
        detail: dict[str, Any],
        channels: str,
        dedup_key: str,
        dedup_hours: int,
    ) -> int | None: ...


class WeComSender(Protocol):
    async def send(self, webhook: str, event: AlertEvent) -> None: ...


class SmtpSender(Protocol):
    async def send(self, routing: SmtpRouting, event: AlertEvent) -> None: ...


def is_allowed_wecom_webhook(webhook: str) -> bool:
    """仅接受企业微信机器人官方 HTTPS 端点及单一非空 key 参数。"""

    try:
        parsed = urlsplit(webhook)
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == WECOM_HOST
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and parsed.path == WECOM_PATH
        and not parsed.fragment
        and set(query) == {"key"}
        and len(query["key"]) == 1
        and bool(query["key"][0].strip())
    )


def safe_alert_routing(
    routing: AlertRouting,
    allowed_smtp_hosts: Iterable[str],
) -> AlertRouting:
    """对历史配置和发送前路由做相同的 fail-closed 过滤。"""

    allowed = frozenset(host.casefold() for host in allowed_smtp_hosts)
    webhook = routing.wecom_webhook if is_allowed_wecom_webhook(routing.wecom_webhook) else ""
    smtp = routing.smtp
    if smtp is not None and smtp.host.casefold() not in allowed:
        smtp = None
    return AlertRouting(webhook, smtp)


def validate_alert_destinations(
    values: dict[str, str],
    allowed_smtp_hosts: Iterable[str],
) -> None:
    """保存配置前验证告警目标，防止管理员扩大部署侧出站边界。"""

    webhook = values.get("alert_wecom_webhook", "").strip()
    if webhook and not is_allowed_wecom_webhook(webhook):
        raise ValueError("alert_wecom_webhook 仅允许企业微信官方 HTTPS 机器人地址")
    smtp_host = values.get("alert_smtp_host", "smtp").strip().casefold() or "smtp"
    allowed = frozenset(host.casefold() for host in allowed_smtp_hosts)
    if smtp_host not in allowed:
        raise ValueError("alert_smtp_host 不在部署允许列表")


def _assert_no_pii(value: Any, *, key: str | None = None) -> None:
    if key is not None and key.casefold() in PHONE_KEYS:
        raise ValueError("alert detail contains forbidden PII field")
    if isinstance(value, dict):
        for nested_key, nested_value in value.items():
            _assert_no_pii(nested_value, key=str(nested_key))
    elif isinstance(value, (list, tuple, set)):
        for nested_value in value:
            _assert_no_pii(nested_value)
    elif isinstance(value, str) and PHONE_IN_TEXT.search(value):
        raise ValueError("alert detail contains forbidden PII text")


def _event(
    *,
    alert_type: str,
    level: str,
    title: str,
    detail: dict[str, Any],
    dedup_key: str,
) -> AlertEvent:
    if not alert_type or len(alert_type) > 32:
        raise ValueError("invalid alert_type")
    if level not in ALERT_LEVELS:
        raise ValueError("invalid alert level")
    if not title or len(title) > 128:
        raise ValueError("invalid alert title")
    if not dedup_key or len(dedup_key) > 128:
        raise ValueError("invalid alert dedup_key")
    _assert_no_pii(title)
    _assert_no_pii(dedup_key)
    _assert_no_pii(detail)
    try:
        json.dumps(detail, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("alert detail must be JSON serializable") from exc
    return AlertEvent(alert_type, level, title, detail, dedup_key)


class WeComChannel:
    """企业微信机器人渠道；不记录 webhook 或响应正文。"""

    async def send(self, webhook: str, event: AlertEvent) -> None:
        if not is_allowed_wecom_webhook(webhook):
            raise ValueError("wecom webhook is not allowed")
        body = json.dumps(event.detail, ensure_ascii=False, sort_keys=True)
        payload = {
            "msgtype": "markdown",
            "markdown": {"content": f"**[{event.level.upper()}] {event.title}**\n> {body}"},
        }
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            response = await client.post(webhook, json=payload)
            response.raise_for_status()
            parsed = response.json()
            if not isinstance(parsed, dict) or parsed.get("errcode") != 0:
                raise RuntimeError("wecom alert rejected")


class SmtpChannel:
    """通过无认证内网 SMTP relay 投递告警。"""

    def __init__(self, allowed_hosts: Iterable[str]) -> None:
        self.allowed_hosts = frozenset(host.casefold() for host in allowed_hosts)

    def _send_sync(self, routing: SmtpRouting, event: AlertEvent) -> None:
        if routing.host.casefold() not in self.allowed_hosts:
            raise ValueError("smtp host is not allowed")
        message = EmailMessage()
        message["Subject"] = f"[SMS][{event.level.upper()}] {event.title}"
        message["From"] = routing.sender
        message["To"] = ", ".join(routing.recipients)
        message.set_content(json.dumps(event.detail, ensure_ascii=False, sort_keys=True))
        with smtplib.SMTP(routing.host, routing.port, timeout=5) as client:
            client.send_message(message)

    async def send(self, routing: SmtpRouting, event: AlertEvent) -> None:
        await run_bounded(self._send_sync, routing, event, timeout_s=10)


class AlertService:
    """先落库去重，再尽力投递已配置渠道；渠道失败不破坏业务主链路。"""

    def __init__(
        self,
        repository: AlertRepository,
        *,
        wecom: WeComSender | None = None,
        smtp: SmtpSender | None = None,
        allowed_smtp_hosts: Iterable[str] | None = None,
    ) -> None:
        if allowed_smtp_hosts is None:
            from app.settings import get_settings

            allowed_smtp_hosts = get_settings().alert_smtp_allowed_host_set
        self.allowed_smtp_hosts = frozenset(
            host.casefold() for host in allowed_smtp_hosts
        )
        self.repository = repository
        self.wecom = wecom or WeComChannel()
        self.smtp = smtp or SmtpChannel(self.allowed_smtp_hosts)

    async def emit(
        self,
        *,
        alert_type: str,
        level: str,
        title: str,
        detail: dict[str, Any],
        dedup_key: str,
        dedup_hours: int = 4,
    ) -> None:
        if not 1 <= dedup_hours <= 168:
            raise ValueError("invalid alert dedup_hours")
        event = _event(
            alert_type=alert_type,
            level=level,
            title=title,
            detail=detail,
            dedup_key=dedup_key,
        )
        routing = safe_alert_routing(
            await self.repository.load_routing(),
            self.allowed_smtp_hosts,
        )
        channel_names = ["log-sink"]
        if routing.wecom_webhook:
            channel_names.append("wecom")
        if routing.smtp is not None:
            channel_names.append("smtp")
        alert_id = await self.repository.claim(
            alert_type=event.alert_type,
            level=event.level,
            title=event.title,
            detail=event.detail,
            channels=",".join(channel_names),
            dedup_key=event.dedup_key,
            dedup_hours=dedup_hours,
        )
        if alert_id is None:
            LOGGER.info(
                "alert deduplicated",
                extra={"alert_type": event.alert_type, "dedup_key": event.dedup_key},
            )
            return
        LOGGER.warning(
            "alert recorded",
            extra={
                "alert_id": alert_id,
                "alert_type": event.alert_type,
                "level": event.level,
                "dedup_key": event.dedup_key,
            },
        )
        if getattr(self.repository, "uses_outbox", False):
            return
        if routing.wecom_webhook:
            try:
                await self.wecom.send(routing.wecom_webhook, event)
            except Exception as exc:
                LOGGER.error(
                    "wecom alert delivery failed",
                    extra={"alert_id": alert_id, "error_type": type(exc).__name__},
                )
        if routing.smtp is not None:
            try:
                await self.smtp.send(routing.smtp, event)
            except Exception as exc:
                LOGGER.error(
                    "smtp alert delivery failed",
                    extra={"alert_id": alert_id, "error_type": type(exc).__name__},
                )
