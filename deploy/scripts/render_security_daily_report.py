#!/usr/bin/env python3
"""将严格校验的主机安全摘要渲染为邮件 HTML 与纯文本。"""

from __future__ import annotations

import argparse
import html as html_module
import ipaddress
import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from string import Template
from typing import Literal, cast

Status = Literal["normal", "attention", "high"]
Tone = Literal["neutral", "good", "warn", "danger"]
Priority = Literal["high", "medium", "low"]

TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "templates"
HTML_TEMPLATE = TEMPLATE_ROOT / "security_daily_report.html"
TEXT_TEMPLATE = TEMPLATE_ROOT / "security_daily_report.txt"
PHONE_IN_TEXT = re.compile(r"(?<!\d)1\d{10}(?!\d)")
BEARER_IN_TEXT = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]+", re.IGNORECASE)
FORBIDDEN_KEYS = {
    "credential",
    "mobile",
    "mobiles",
    "password",
    "phone",
    "phones",
    "secret",
    "token",
}
STATUSES: frozenset[str] = frozenset({"normal", "attention", "high"})
TONES: frozenset[str] = frozenset({"neutral", "good", "warn", "danger"})
PRIORITIES: frozenset[str] = frozenset({"high", "medium", "low"})


class ReportValidationError(ValueError):
    """日报结构、时间或隐私约束不成立。"""


@dataclass(frozen=True, slots=True)
class Metric:
    label: str
    value: str
    tone: Tone
    note: str


@dataclass(frozen=True, slots=True)
class DetailRow:
    label: str
    value: str
    assessment: str
    tone: Tone


@dataclass(frozen=True, slots=True)
class AuditRow:
    time: str
    actor: str
    source_ip: str
    action: str
    assessment: str
    tone: Tone


@dataclass(frozen=True, slots=True)
class ActionItem:
    priority: Priority
    title: str
    detail: str


@dataclass(frozen=True, slots=True)
class CoverageItem:
    source: str
    window: str
    status: str
    note: str
    tone: Tone


@dataclass(frozen=True, slots=True)
class SecurityDailyReport:
    report_date: str
    period_start: str
    period_end: str
    generated_at: str
    status: Status
    summary: str
    pending_confirmation: str
    metrics: tuple[Metric, ...]
    ssh: tuple[DetailRow, ...]
    web: tuple[DetailRow, ...]
    audit: tuple[AuditRow, ...]
    runtime: tuple[DetailRow, ...]
    actions: tuple[ActionItem, ...]
    coverage: tuple[CoverageItem, ...]


def _mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ReportValidationError(f"{field} must be an object")
    return cast(dict[str, object], value)


def _strict_mapping(
    value: object,
    field: str,
    *,
    required: frozenset[str],
) -> dict[str, object]:
    result = _mapping(value, field)
    unknown = set(result) - required
    if unknown:
        raise ReportValidationError(f"{field} contains unknown fields: {sorted(unknown)}")
    missing = required - set(result)
    if missing:
        raise ReportValidationError(f"{field} is missing fields: {sorted(missing)}")
    return result


def _text(
    value: object,
    field: str,
    *,
    max_length: int = 500,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ReportValidationError(f"{field} must be text")
    result = value.strip()
    if not allow_empty and not result:
        raise ReportValidationError(f"{field} must not be empty")
    if len(result) > max_length:
        raise ReportValidationError(f"{field} is too long")
    return result


def _items(value: object, field: str, *, maximum: int) -> list[object]:
    if not isinstance(value, list):
        raise ReportValidationError(f"{field} must be a list")
    result = cast(list[object], value)
    if len(result) > maximum:
        raise ReportValidationError(f"{field} contains too many items")
    return result


def _enum(value: object, field: str, allowed: frozenset[str]) -> str:
    result = _text(value, field, max_length=32)
    if result not in allowed:
        raise ReportValidationError(f"{field} has an invalid value")
    return result


def _shanghai_timestamp(value: object, field: str) -> str:
    result = _text(value, field, max_length=35)
    if not result.endswith("+08:00"):
        raise ReportValidationError(f"{field} must use +08:00")
    try:
        parsed = datetime.fromisoformat(result)
    except ValueError as exc:
        raise ReportValidationError(f"{field} must be ISO 8601") from exc
    if parsed.utcoffset() != timedelta(hours=8):
        raise ReportValidationError(f"{field} must use Asia/Shanghai offset")
    return result


def _report_date(value: object) -> str:
    result = _text(value, "report_date", max_length=10)
    try:
        parsed = date.fromisoformat(result)
    except ValueError as exc:
        raise ReportValidationError("report_date must be YYYY-MM-DD") from exc
    if parsed.isoformat() != result:
        raise ReportValidationError("report_date must be canonical")
    return result


def _assert_no_sensitive_payload(value: object, path: str = "report") -> None:
    if isinstance(value, dict):
        mapping = _mapping(value, path)
        for key, nested in mapping.items():
            if key.casefold() in FORBIDDEN_KEYS:
                raise ReportValidationError(f"{path}.{key} is a forbidden field")
            _assert_no_sensitive_payload(nested, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, nested in enumerate(cast(list[object], value)):
            _assert_no_sensitive_payload(nested, f"{path}[{index}]")
        return
    if isinstance(value, str) and (
        PHONE_IN_TEXT.search(value)
        or BEARER_IN_TEXT.search(value)
        or "BEGIN PRIVATE KEY" in value.upper()
    ):
        raise ReportValidationError(f"{path} contains forbidden PII or credential text")


def _metric(value: object, index: int) -> Metric:
    field = f"metrics[{index}]"
    item = _strict_mapping(
        value,
        field,
        required=frozenset({"label", "value", "tone", "note"}),
    )
    tone = cast(Tone, _enum(item["tone"], f"{field}.tone", TONES))
    return Metric(
        _text(item["label"], f"{field}.label", max_length=32),
        _text(item["value"], f"{field}.value", max_length=32),
        tone,
        _text(item["note"], f"{field}.note", max_length=80, allow_empty=True),
    )


def _detail(value: object, field: str) -> DetailRow:
    item = _strict_mapping(
        value,
        field,
        required=frozenset({"label", "value", "assessment", "tone"}),
    )
    tone = cast(Tone, _enum(item["tone"], f"{field}.tone", TONES))
    return DetailRow(
        _text(item["label"], f"{field}.label", max_length=40),
        _text(item["value"], f"{field}.value", max_length=160),
        _text(item["assessment"], f"{field}.assessment", max_length=80),
        tone,
    )


def _audit(value: object, index: int) -> AuditRow:
    field = f"audit[{index}]"
    item = _strict_mapping(
        value,
        field,
        required=frozenset(
            {"time", "actor", "source_ip", "action", "assessment", "tone"}
        ),
    )
    tone = cast(Tone, _enum(item["tone"], f"{field}.tone", TONES))
    return AuditRow(
        _text(item["time"], f"{field}.time", max_length=32),
        _text(item["actor"], f"{field}.actor", max_length=64),
        _text(item["source_ip"], f"{field}.source_ip", max_length=64),
        _text(item["action"], f"{field}.action", max_length=80),
        _text(item["assessment"], f"{field}.assessment", max_length=80),
        tone,
    )


def _action(value: object, index: int) -> ActionItem:
    field = f"actions[{index}]"
    item = _strict_mapping(
        value,
        field,
        required=frozenset({"priority", "title", "detail"}),
    )
    priority = cast(
        Priority,
        _enum(item["priority"], f"{field}.priority", PRIORITIES),
    )
    return ActionItem(
        priority,
        _text(item["title"], f"{field}.title", max_length=80),
        _text(item["detail"], f"{field}.detail", max_length=300),
    )


def _coverage(value: object, index: int) -> CoverageItem:
    field = f"coverage[{index}]"
    item = _strict_mapping(
        value,
        field,
        required=frozenset({"source", "window", "status", "note", "tone"}),
    )
    tone = cast(Tone, _enum(item["tone"], f"{field}.tone", TONES))
    return CoverageItem(
        _text(item["source"], f"{field}.source", max_length=60),
        _text(item["window"], f"{field}.window", max_length=100),
        _text(item["status"], f"{field}.status", max_length=40),
        _text(item["note"], f"{field}.note", max_length=240),
        tone,
    )


def parse_report(payload: object) -> SecurityDailyReport:
    """校验外部摘要并返回不可变日报对象。"""

    _assert_no_sensitive_payload(payload)
    required = frozenset(
        {
            "schema_version",
            "report_date",
            "period_start",
            "period_end",
            "generated_at",
            "status",
            "summary",
            "pending_confirmation",
            "metrics",
            "ssh",
            "web",
            "audit",
            "runtime",
            "actions",
            "coverage",
        }
    )
    data = _strict_mapping(payload, "report", required=required)
    if data["schema_version"] != 1:
        raise ReportValidationError("schema_version must be 1")

    report_date = _report_date(data["report_date"])
    period_start = _shanghai_timestamp(data["period_start"], "period_start")
    period_end = _shanghai_timestamp(data["period_end"], "period_end")
    generated_at = _shanghai_timestamp(data["generated_at"], "generated_at")
    start_value = datetime.fromisoformat(period_start)
    end_value = datetime.fromisoformat(period_end)
    if start_value >= end_value or start_value.date().isoformat() != report_date:
        raise ReportValidationError("report period does not match report_date")
    if end_value.date().isoformat() != report_date:
        raise ReportValidationError("period_end does not match report_date")
    if datetime.fromisoformat(generated_at) <= end_value:
        raise ReportValidationError("generated_at must be later than period_end")

    metrics = _items(data["metrics"], "metrics", maximum=5)
    if len(metrics) != 5:
        raise ReportValidationError("metrics must contain exactly five items")

    def detail_rows(name: str) -> tuple[DetailRow, ...]:
        values = _items(data[name], name, maximum=10)
        return tuple(_detail(value, f"{name}[{index}]") for index, value in enumerate(values))

    status = cast(Status, _enum(data["status"], "status", STATUSES))
    return SecurityDailyReport(
        report_date=report_date,
        period_start=period_start,
        period_end=period_end,
        generated_at=generated_at,
        status=status,
        summary=_text(data["summary"], "summary", max_length=500),
        pending_confirmation=_text(
            data["pending_confirmation"],
            "pending_confirmation",
            max_length=500,
            allow_empty=True,
        ),
        metrics=tuple(_metric(value, index) for index, value in enumerate(metrics)),
        ssh=detail_rows("ssh"),
        web=detail_rows("web"),
        audit=tuple(
            _audit(value, index)
            for index, value in enumerate(_items(data["audit"], "audit", maximum=10))
        ),
        runtime=detail_rows("runtime"),
        actions=tuple(
            _action(value, index)
            for index, value in enumerate(_items(data["actions"], "actions", maximum=10))
        ),
        coverage=tuple(
            _coverage(value, index)
            for index, value in enumerate(
                _items(data["coverage"], "coverage", maximum=10)
            )
        ),
    )


def load_report(path: Path) -> SecurityDailyReport:
    """从 UTF-8 JSON 文件读取并校验日报。"""

    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportValidationError(f"cannot load report: {path}") from exc
    return parse_report(payload)


def _h(value: str) -> str:
    return html_module.escape(value, quote=True)


def _audit_source_display(value: str) -> str:
    """邮件仅展示 IPv6 /64，审计 payload 仍保留完整真实来源。"""

    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return value
    if isinstance(address, ipaddress.IPv4Address):
        return str(address)
    if address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    if not address.is_global:
        return f"{address.compressed}（IPv6）"
    network = ipaddress.ip_network(f"{address.compressed}/64", strict=False)
    return f"{network.network_address.compressed}/64（IPv6，脱敏）"


def _tone(tone: Tone) -> tuple[str, str, str]:
    return {
        "neutral": ("#334155", "#e8edf2", "信息"),
        "good": ("#166534", "#e7f4ea", "正常"),
        "warn": ("#8a4b08", "#fff1d6", "关注"),
        "danger": ("#991b1b", "#fde8e8", "高风险"),
    }[tone]


def _status(report: SecurityDailyReport) -> tuple[str, str, str, str]:
    return {
        "normal": ("#166534", "#e7f4ea", "正常", "未发现成功入侵证据"),
        "attention": ("#8a4b08", "#fff1d6", "关注", "存在需要人工确认的安全事项"),
        "high": ("#991b1b", "#fde8e8", "高风险", "发现需要立即处置的安全事件"),
    }[report.status]


def _detail_html(rows: tuple[DetailRow, ...]) -> str:
    output: list[str] = []
    for row in rows:
        color, background, label = _tone(row.tone)
        output.append(
            '<tr><td class="detail-label" style="padding:13px 0;border-bottom:1px solid #d9dee3;'
            'vertical-align:top;width:28%;font-size:13px;line-height:20px;color:#52606d;">'
            f"{_h(row.label)}</td>"
            '<td class="detail-value" style="padding:13px 14px;border-bottom:1px solid #d9dee3;'
            'vertical-align:top;font-size:14px;line-height:21px;color:#18222c;'
            'overflow-wrap:anywhere;">'
            f"{_h(row.value)}</td>"
            '<td class="detail-assessment" style="padding:13px 0;border-bottom:1px solid #d9dee3;'
            'vertical-align:top;text-align:right;width:25%;">'
            f'<span style="display:inline-block;padding:3px 7px;background:{background};'
            f'color:{color};font-size:11px;line-height:16px;font-weight:700;">'
            f"{_h(label)} · {_h(row.assessment)}</span></td></tr>"
        )
    return "".join(output)


def _metrics_html(metrics: tuple[Metric, ...]) -> tuple[str, str, str, str, str]:
    primary = metrics[0]
    primary_color, _, _ = _tone(primary.tone)
    cells: list[str] = []
    for metric in metrics[1:]:
        color, background, _ = _tone(metric.tone)
        cells.append(
            '<td class="metric-cell" style="width:50%;padding:14px 15px;'
            f'background:{background};border:4px solid #f4f1e9;vertical-align:top;">'
            f'<div style="font-size:11px;line-height:16px;color:{color};'
            f'font-weight:700;letter-spacing:.04em;">{_h(metric.label)}</div>'
            '<div style="margin-top:4px;font-family:Georgia,Times New Roman,serif;'
            f'font-size:28px;line-height:32px;color:#18222c;">{_h(metric.value)}</div>'
            f'<div style="margin-top:3px;font-size:11px;line-height:16px;color:#66727e;">'
            f"{_h(metric.note)}</div></td>"
        )
    rows = "".join(
        f'<tr>{cells[index]}{cells[index + 1]}</tr>' for index in range(0, len(cells), 2)
    )
    return (
        _h(primary.label),
        _h(primary.value),
        _h(primary.note),
        primary_color,
        rows,
    )


def _audit_html(rows: tuple[AuditRow, ...]) -> str:
    output: list[str] = []
    for row in rows:
        color, background, label = _tone(row.tone)
        source_ip = _audit_source_display(row.source_ip)
        output.append(
            '<tr><td class="audit-time" style="padding:13px 0;border-bottom:1px solid #d9dee3;'
            'vertical-align:top;width:20%;font-size:12px;line-height:19px;color:#52606d;">'
            f"{_h(row.time)}</td>"
            '<td class="audit-actor" style="padding:13px 10px;border-bottom:1px solid #d9dee3;'
            'vertical-align:top;font-size:13px;line-height:20px;color:#18222c;">'
            f'<strong>{_h(row.actor)}</strong><br><span style="color:#66727e;">'
            f"{_h(source_ip)}</span></td>"
            '<td class="audit-action" style="padding:13px 10px;border-bottom:1px solid #d9dee3;'
            'vertical-align:top;font-size:13px;line-height:20px;color:#18222c;'
            f'overflow-wrap:anywhere;">{_h(row.action)}</td>'
            '<td class="audit-assessment" style="padding:13px 0;border-bottom:1px solid #d9dee3;'
            'vertical-align:top;text-align:right;width:22%;">'
            f'<span style="display:inline-block;padding:3px 7px;background:{background};'
            f'color:{color};font-size:11px;line-height:16px;font-weight:700;">'
            f"{_h(label)} · {_h(row.assessment)}</span></td></tr>"
        )
    return "".join(output)


def _actions_html(items: tuple[ActionItem, ...]) -> str:
    if not items:
        return (
            '<tr><td style="padding:14px 0;font-size:14px;line-height:21px;'
            'color:#4b5863;">今日无需处理，继续保持现有采集计划。</td></tr>'
        )
    styles = {
        "high": ("01", "立即", "#991b1b", "#fde8e8"),
        "medium": ("02", "今日", "#8a4b08", "#fff1d6"),
        "low": ("03", "计划", "#334155", "#e8edf2"),
    }
    output: list[str] = []
    for item in items:
        number, priority, color, background = styles[item.priority]
        output.append(
            '<tr><td style="padding:14px 0;border-bottom:1px solid #d9dee3;'
            'vertical-align:top;width:48px;">'
            f'<span style="display:inline-block;width:34px;height:34px;background:{background};'
            f'color:{color};font-family:Georgia,Times New Roman,serif;font-size:15px;'
            f'line-height:34px;text-align:center;">{number}</span></td>'
            '<td style="padding:14px 0;border-bottom:1px solid #d9dee3;vertical-align:top;">'
            f'<div style="font-size:11px;line-height:16px;color:{color};font-weight:800;'
            f'letter-spacing:.08em;">{priority}</div>'
            '<div style="margin-top:2px;font-size:14px;line-height:21px;color:#18222c;'
            f'font-weight:700;">{_h(item.title)}</div>'
            '<div style="margin-top:4px;font-size:13px;line-height:20px;color:#52606d;">'
            f"{_h(item.detail)}</div></td></tr>"
        )
    return "".join(output)


def _coverage_html(items: tuple[CoverageItem, ...]) -> str:
    output: list[str] = []
    for item in items:
        color, background, label = _tone(item.tone)
        output.append(
            '<tr><td class="coverage-source" style="padding:12px 0;border-bottom:1px solid #d9dee3;'
            'vertical-align:top;width:25%;font-size:13px;line-height:20px;color:#18222c;'
            f'font-weight:700;">{_h(item.source)}</td>'
            '<td class="coverage-detail" style="padding:12px 12px;border-bottom:1px solid #d9dee3;'
            'vertical-align:top;font-size:12px;line-height:19px;color:#52606d;">'
            f"{_h(item.window)}<br>{_h(item.note)}</td>"
            '<td class="coverage-status" style="padding:12px 0;border-bottom:1px solid #d9dee3;'
            'vertical-align:top;text-align:right;width:20%;">'
            f'<span style="display:inline-block;padding:3px 7px;background:{background};'
            f'color:{color};font-size:11px;line-height:16px;font-weight:700;">'
            f"{_h(label)} · {_h(item.status)}</span></td></tr>"
        )
    return "".join(output)


def _period(report: SecurityDailyReport) -> str:
    start = datetime.fromisoformat(report.period_start)
    end = datetime.fromisoformat(report.period_end)
    return f"{start:%Y-%m-%d %H:%M} — {end:%H:%M}（UTC+8）"


def render_subject(report: SecurityDailyReport) -> str:
    """生成不含主机或来源信息的稳定主题。"""

    label = {"normal": "正常", "attention": "关注", "high": "高风险"}[report.status]
    return f"[短信平台安全日报][{label}] {report.report_date}"


def render_html(report: SecurityDailyReport) -> str:
    """渲染无外部资源、邮件客户端兼容的 HTML。"""

    template = Template(HTML_TEMPLATE.read_text(encoding="utf-8"))
    status_color, status_background, status_label, status_kicker = _status(report)
    primary_label, primary_value, primary_note, primary_color, metric_rows = _metrics_html(
        report.metrics
    )
    pending = report.pending_confirmation or "无待确认事项"
    return template.substitute(
        preheader=_h(f"{status_label}：{report.summary}"),
        report_date=_h(report.report_date),
        period=_h(_period(report)),
        generated_at=_h(report.generated_at),
        status_color=status_color,
        status_background=status_background,
        status_label=_h(status_label),
        status_kicker=_h(status_kicker),
        summary=_h(report.summary),
        pending_confirmation=_h(pending),
        primary_label=primary_label,
        primary_value=primary_value,
        primary_note=primary_note,
        primary_color=primary_color,
        metric_rows=metric_rows,
        ssh_rows=_detail_html(report.ssh),
        web_rows=_detail_html(report.web),
        audit_rows=_audit_html(report.audit),
        runtime_rows=_detail_html(report.runtime),
        action_rows=_actions_html(report.actions),
        coverage_rows=_coverage_html(report.coverage),
    )


def _detail_text(rows: tuple[DetailRow, ...]) -> str:
    return "\n".join(
        f"- {row.label}: {row.value} [{row.assessment}]" for row in rows
    ) or "- 无"


def render_text(report: SecurityDailyReport) -> str:
    """渲染与 HTML 口径一致的纯文本。"""

    template = Template(TEXT_TEMPLATE.read_text(encoding="utf-8"))
    _, _, status_label, _ = _status(report)
    metrics = "\n".join(
        f"- {item.label}: {item.value}（{item.note}）" for item in report.metrics
    )
    audit = "\n".join(
        f"- {row.time} | {row.actor} | {_audit_source_display(row.source_ip)} | "
        f"{row.action} | {row.assessment}"
        for row in report.audit
    ) or "- 无"
    actions = "\n".join(
        f"- [{item.priority.upper()}] {item.title}: {item.detail}"
        for item in report.actions
    ) or "今日无需处理"
    coverage = "\n".join(
        f"- {item.source}: {item.window} | {item.status} | {item.note}"
        for item in report.coverage
    ) or "- 无"
    return template.substitute(
        status_label=status_label,
        report_date=report.report_date,
        period=_period(report),
        generated_at=report.generated_at,
        summary=report.summary,
        pending_confirmation=report.pending_confirmation or "无待确认事项",
        metrics=metrics,
        ssh_rows=_detail_text(report.ssh),
        web_rows=_detail_text(report.web),
        audit_rows=audit,
        runtime_rows=_detail_text(report.runtime),
        action_rows=actions,
        coverage_rows=coverage,
    )


def _write_output(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render a security daily email draft")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--html-output", type=Path, required=True)
    parser.add_argument("--text-output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = load_report(args.input)
    _write_output(args.html_output, render_html(report))
    _write_output(args.text_output, render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
