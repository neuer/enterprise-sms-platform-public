"""安全日报生成期业务规则：审计证据注入、环比、状态判定与问题通报。"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from app.services.security_daily.classify import _audit_assessment, _audit_tone
from app.services.security_daily.contract import (
    SecurityDailyAuditEvidence,
    validate_security_daily_payload,
)

SSH_FAILED_CONFIRM_THRESHOLD = 20
COUNT_VALUE_PREFIX = re.compile(r"^\s*(?P<count>\d+)(?=$|[\s次条个（])")

SSH_FAILURE_METRIC_LABELS = ("SSH 认证失败", "攻击尝试")
WEB_5XX_METRIC_LABEL = "Web/API 5xx"
GAP_METRIC_LABEL = "证据覆盖缺口"
SENSITIVE_PATH_LABEL = "敏感路径"
RUNTIME_UNHEALTHY_LABEL = "异常容器"


def _audit_source_display(value: str) -> str:
    """最小化日报中的审计来源地址；审计事实记录仍保留原始值。"""

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


def _enrich_audit_evidence(
    payload: dict[str, Any], evidence: SecurityDailyAuditEvidence
) -> dict[str, Any]:
    """把平台审计摘要注入日报，并翻转管理审计覆盖状态。"""

    payload["audit"] = [
        {
            "time": event.time,
            "actor": event.actor,
            "source_ip": _audit_source_display(event.source_ip),
            "action": event.action,
            "assessment": _audit_assessment(event.action),
            "tone": _audit_tone(event.action),
        }
        for event in evidence.events[:10]
    ]
    for item in payload["coverage"]:
        if item["source"] == "管理审计":
            item["status"] = "完整"
            item["note"] = "按平台审计事实表聚合，不读取载荷明细"
            item["tone"] = "good"
    if evidence.total > 0 and evidence.category_counts:
        category_text = "、".join(
            f"{label} {count}" for label, count in evidence.category_counts[:3]
        )
        payload["summary"] = (
            f"{payload['summary']} 管理审计共 {evidence.total} 条：{category_text}。"
        )
    return payload


def _count_from_value(value: str) -> int | None:
    """解析指标前缀计数；不可解析时返回 None，禁止静默伪造为 0。"""

    match = COUNT_VALUE_PREFIX.match(str(value))
    return int(match.group("count")) if match is not None else None


def _count_delta_suffix(current: str, previous: str) -> str | None:
    """生成“（昨日 N，↑x%）”环比后缀；任一值不可解析时返回 None。"""

    current_count = _count_from_value(current)
    previous_count = _count_from_value(previous)
    if current_count is None or previous_count is None:
        return None
    if current_count == previous_count:
        return "（与昨日持平）"
    if previous_count == 0:
        return f"（昨日 0，新增 {current_count}）"
    delta = current_count - previous_count
    numerator = abs(delta) * 100
    # 常规四舍五入，避免 bankers rounding 把 12.5% 显示为 12%。
    percent = (2 * numerator + previous_count) // (2 * previous_count)
    arrow = "↑" if delta > 0 else "↓"
    return f"（昨日 {previous_count}，{arrow}{percent}%）"


def _labeled_row(
    rows: Sequence[dict[str, Any]], *labels: str
) -> dict[str, Any] | None:
    """按语义标签定位行，避免展示项重排后把不同指标互相比较。"""

    expected = frozenset(labels)
    return next((item for item in rows if str(item.get("label")) in expected), None)


def _enrich_day_over_day(payload: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    """在平台侧追加昨日环比：SSH 失败、Web 5xx、敏感路径三个关键面。"""

    comparisons: list[str] = []
    specifications = (
        ("metrics", SSH_FAILURE_METRIC_LABELS, "SSH 失败认证"),
        ("metrics", (WEB_5XX_METRIC_LABEL,), "Web 5xx"),
        ("web", (SENSITIVE_PATH_LABEL,), "敏感路径"),
    )
    for section, labels, label in specifications:
        current = _labeled_row(payload[section], *labels)
        old = _labeled_row(previous.get(section, []), *labels)
        if current is None or old is None:
            continue
        suffix = _count_delta_suffix(current["value"], old["value"])
        if suffix is None:
            continue
        current["value"] = f"{current['value']}{suffix}"
        comparisons.append(f"{label}{suffix}")
    if comparisons:
        payload["summary"] = f"{payload['summary']} 较昨日：{'、'.join(comparisons)}。"
    return payload


def _finalize_security_daily_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """在平台侧重算状态/待确认/建议，覆盖采集快照与审计注入后的口径漂移。"""

    gaps = [str(item["source"]) for item in payload["coverage"] if item.get("status") != "完整"]
    ssh_metric = _labeled_row(payload["metrics"], *SSH_FAILURE_METRIC_LABELS)
    web_5xx_metric = _labeled_row(payload["metrics"], WEB_5XX_METRIC_LABEL)
    gap_metric = _labeled_row(payload["metrics"], GAP_METRIC_LABEL)
    sensitive_row = _labeled_row(payload["web"], SENSITIVE_PATH_LABEL)
    runtime_unhealthy_row = _labeled_row(payload["runtime"], RUNTIME_UNHEALTHY_LABEL)

    ssh_failed = (
        _count_from_value(str(ssh_metric["value"])) if ssh_metric is not None else None
    )
    web_5xx = (
        _count_from_value(str(web_5xx_metric["value"]))
        if web_5xx_metric is not None
        else None
    )
    web_sensitive = (
        _count_from_value(str(sensitive_row["value"]))
        if sensitive_row is not None
        else None
    )
    runtime_unhealthy = (
        _count_from_value(str(runtime_unhealthy_row["value"]))
        if runtime_unhealthy_row is not None
        else None
    )
    ssh_failed_count = ssh_failed or 0
    web_5xx_count = web_5xx or 0
    web_sensitive_count = web_sensitive or 0
    runtime_unhealthy_count = runtime_unhealthy or 0
    ssh_needs_confirm = (
        ssh_failed is not None and ssh_failed >= SSH_FAILED_CONFIRM_THRESHOLD
    )
    status = (
        "high"
        if web_sensitive_count
        else (
            "attention"
            if gaps
            or ssh_needs_confirm
            or web_5xx_count
            or runtime_unhealthy_count
            else "normal"
        )
    )
    pending_items: list[str] = []
    if web_sensitive_count:
        pending_items.append(f"敏感路径命中 {web_sensitive_count} 次，需要核查访问来源")
    if gaps:
        pending_items.append(f"待接入证据源：{'、'.join(gaps)}")
    if ssh_needs_confirm:
        pending_items.append("存在 SSH 失败认证，需要确认是否属于授权运维")
    if web_5xx_count:
        pending_items.append(f"存在 {web_5xx_count} 次 Web/API 5xx，需要核查服务异常")
    if runtime_unhealthy_count:
        pending_items.append(f"存在 {runtime_unhealthy_count} 个异常容器，需要核查运行态")
    pending = "；".join(pending_items) + "。" if pending_items else "无待确认事项。"
    actions: list[dict[str, str]] = []
    if web_sensitive_count:
        actions.append(
            {
                "priority": "high",
                "title": "核查敏感路径命中",
                "detail": "访问日志发现敏感路径命中；日报不保留原始路径，请到受控日志平台核对。",
            }
        )
    if gaps:
        actions.append(
            {
                "priority": "medium",
                "title": "补齐日报证据源",
                "detail": f"接入{'、'.join(gaps)}后，下一日报才可覆盖完整安全面。",
            }
        )
    if ssh_needs_confirm:
        actions.append(
            {
                "priority": "medium",
                "title": "核查 SSH 失败认证",
                "detail": (
                    f"当日发现 {ssh_failed_count} 次 SSH 失败认证；日报不保留来源明细，"
                    "请结合主机安全日志核对。"
                ),
            }
        )
    if web_5xx_count:
        actions.append(
            {
                "priority": "medium",
                "title": "核查 Web/API 5xx",
                "detail": (
                    f"当日发现 {web_5xx_count} 次服务端错误，请结合受控访问日志核对。"
                ),
            }
        )
    if runtime_unhealthy_count:
        actions.append(
            {
                "priority": "medium",
                "title": "核查平台异常容器",
                "detail": (
                    f"采集时发现 {runtime_unhealthy_count} 个容器未运行、暂停、重启或"
                    "健康检查失败，请核对当前运行态。"
                ),
            }
        )
    payload["status"] = status
    payload["pending_confirmation"] = pending
    payload["actions"] = actions
    if gap_metric is not None:
        gap_metric["value"] = str(len(gaps))
        gap_metric["tone"] = "warn" if gaps else "good"
        gap_metric["note"] = "缺口数量"
    if ssh_failed_count and not ssh_needs_confirm and ssh_metric is not None:
        ssh_metric["tone"] = "neutral"
        ssh_metric["note"] = "低于关注阈值，无需人工确认"
    return payload


def _problem_payload(
    report_date: date,
    *,
    period_start: datetime,
    period_end: datetime,
    generated_at: datetime,
    reason: str,
) -> dict[str, Any]:
    """构建“证据不可用”问题通报 payload；指标一律显式不可用，不虚构数值。"""

    window = f"{report_date.isoformat()} 00:00 — 23:59（UTC+8）"
    unavailable = {"value": "不可用", "tone": "warn", "note": "证据缺失"}
    detail = {"value": "不可用", "assessment": "证据缺失", "tone": "warn"}
    payload = {
        "schema_version": 1,
        "report_date": report_date.isoformat(),
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "generated_at": generated_at.isoformat(),
        "status": "attention",
        "summary": f"安全日报证据不可用：{reason}。未生成任何脱敏指标，请检查采集器与日志源。",
        "pending_confirmation": "需要人工核查证据源状态。",
        "metrics": [
            {"label": "SSH 认证失败", **unavailable},
            {"label": "SSH 成功认证", **unavailable},
            {"label": "Fail2ban 封禁", **unavailable},
            {"label": "Web/API 5xx", **unavailable},
            {"label": "证据覆盖缺口", "value": "全部", "tone": "danger", "note": "证据源不可用"},
        ],
        "ssh": [
            {"label": "失败认证", **detail},
            {"label": "成功认证", **detail},
            {"label": "Fail2ban", **detail},
        ],
        "web": [
            {"label": "访问请求", **detail},
            {"label": "拒绝请求", **detail},
            {"label": "服务端错误", **detail},
            {"label": "敏感路径", **detail},
        ],
        "audit": [],
        "runtime": [
            {
                "label": "证据采集器",
                "value": "未产出快照",
                "assessment": "需要核查",
                "tone": "warn",
            }
        ],
        "actions": [
            {
                "priority": "high",
                "title": "核查安全日报证据源",
                "detail": f"{reason}；请检查主机采集器、日志文件与权限后重新生成。",
            }
        ],
        "coverage": [
            {
                "source": name,
                "window": window,
                "status": "缺失",
                "note": "证据源不可用",
                "tone": "warn",
            }
            for name in (
                "SSH journal",
                "Fail2ban",
                "Web/API access log",
                "管理审计",
                "运行态探针",
            )
        ],
    }
    return validate_security_daily_payload(payload)
