"""安全日报的预览渲染、时间线与调度辅助。"""

from __future__ import annotations

import html
import json
from datetime import datetime, timedelta
from typing import Any

from app.services.security_daily.contract import (
    SHANGHAI_TZ,
    ConfigurationState,
    SecurityDailyReportRecord,
    validate_security_daily_payload,
)


def _next_schedule(now: datetime) -> datetime:
    local = now.astimezone(SHANGHAI_TZ)
    target = local.replace(hour=8, minute=0, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return target


def resolve_configuration_state(
    *, enabled: bool, resend_configured: bool, recipient_count: int
) -> ConfigurationState:
    """把当前安全日报配置归一为可解释的状态，不用默认值伪造可用性。"""

    if not enabled:
        return "disabled"
    if not resend_configured:
        return "dispatcher_missing"
    if recipient_count == 0:
        return "recipients_empty"
    return "ready"


def _timeline(record: SecurityDailyReportRecord) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if record.generated_at is not None:
        events.append({"type": "generated", "at": record.generated_at, "label": "报告生成"})
    if record.last_error_at is not None:
        if record.generation_status == "unavailable":
            event_type = "evidence_unavailable"
            label = "证据不可用"
        elif record.generation_status == "failed":
            event_type = "generation_failed"
            label = "生成失败"
        elif record.delivery_status == "unknown":
            event_type = "delivery_unknown"
            label = "投递结果未知"
        else:
            event_type = "delivery_failed"
            label = "投递失败"
        events.append(
            {
                "type": event_type,
                "at": record.last_error_at,
                "label": label,
                "detail": record.last_error,
            }
        )
    if record.delivered_at is not None:
        events.append({"type": "sent", "at": record.delivered_at, "label": "已投递"})
    return events


def _render_preview(payload: dict[str, Any]) -> tuple[str, str]:
    """基于同一脱敏对象生成安全的 HTML/纯文本预览。"""

    validated = validate_security_daily_payload(payload)
    report_date = str(validated["report_date"])
    status = {"normal": "正常", "attention": "关注", "high": "高风险"}[validated["status"]]
    sections = [
        ("管理摘要", validated["summary"]),
        ("待确认", validated["pending_confirmation"] or "无"),
        (
            "今日需要你处理",
            "\n".join(
                f"[{item['priority']}] {item['title']}: {item['detail']}"
                for item in validated["actions"]
            )
            or "今日无需处理",
        ),
    ]
    for key, label in (
        ("metrics", "核心指标"),
        ("ssh", "SSH 与主机安全"),
        ("web", "Web/API"),
        ("audit", "管理审计"),
        ("runtime", "运行状态"),
        ("coverage", "证据范围"),
    ):
        sections.append((label, json.dumps(validated[key], ensure_ascii=False, indent=2)))
    text_lines = [f"服务器安全日报 [{status}]", f"日期：{report_date}", ""]
    text_lines.extend(f"{label}\n{value}\n" for label, value in sections)
    text_preview = "\n".join(text_lines)
    html_sections = "".join(
        f"<section><h2>{html.escape(label)}</h2><pre>{html.escape(str(value))}</pre></section>"
        for label, value in sections
    )
    html_preview = (
        "<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
        f"<title>服务器安全日报</title><main><h1>服务器安全日报 [{html.escape(status)}]</h1>"
        f"<p>日期：{html.escape(report_date)}</p>{html_sections}</main></html>"
    )
    return html_preview, text_preview
