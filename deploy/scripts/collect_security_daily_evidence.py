#!/usr/bin/env python3
"""从主机日志生成脱敏安全日报证据快照。

该采集器只输出聚合计数和覆盖状态，不把日志原文、IP、账号或请求路径写入
快照。它是主机侧的独立入口；平台 API 只消费已经落盘的 JSON，不读取主机日志。
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import json
import os
import re
import stat
import sys
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from render_security_daily_report import parse_report

SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
MAX_LOG_BYTES = 256 * 1024 * 1024
DEFAULT_OWNER_UID = 10001
DEFAULT_WEB_LOG = Path("/opt/sms-platform/deploy/security-report-nginx/access.log")
DEFAULT_DOCKER_ROOT = Path("/var/lib/docker")
SYSLOG_PREFIX = re.compile(r"^(?P<month>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+")
ISO_PREFIX = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})T")
SPACE_ISO_PREFIX = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})\s")
NGINX_BRACKET_DATE = re.compile(
    r"\[(?P<day>\d{2})/(?P<month>[A-Z][a-z]{2})/(?P<year>\d{4}):"
)
MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}
FAILED_SSH = re.compile(
    r"(?:Failed password|Invalid user|authentication failure|MaxStartups throttling)",
    re.I,
)
ACCEPTED_SSH = re.compile(
    r"\bAccepted\s+(?:publickey|keyboard-interactive|password|key)\b", re.I
)
FAIL2BAN_BAN = re.compile(r"\bBan\s+", re.I)
HTTP_STATUS = re.compile(r"\s(?P<status>[1-5]\d{2})(?:\s|$)")
SENSITIVE_PATH = re.compile(
    r"(?:/\.env(?:\b|/)|/\.git(?:\b|/)|/debug(?:\b|/)|/actuator(?:\b|/))", re.I
)
SSHD_MESSAGE_RE = re.compile(r"\bsshd(?:\[\d+\])?:", re.IGNORECASE)
FAIL2BAN_MESSAGE_RE = re.compile(
    r"\bfail2ban(?:\.\w+)?(?:\[\d+\])?:", re.IGNORECASE
)
IPV4_RE = re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")
IPV6_RE = re.compile(r"(?:[0-9A-Fa-f]{1,4}:){3,7}[0-9A-Fa-f]{1,4}")
WEB_METHOD_URI_RE = re.compile(
    r"\b(?:GET|POST|PUT|DELETE|HEAD|PATCH|OPTIONS)\s+([^\s\"']+)"
)
WEB_BRACKET_URI_RE = re.compile(r"\]\s+(\S+)\s+[1-5]\d{2}")
WEB_LEAD_URI_RE = re.compile(r"^\S+\s+(\S+)\s+[1-5]\d{2}")
SENSITIVE_MARKERS = ("/.env", "/.git", "/debug", "/actuator")


class CollectorError(RuntimeError):
    """采集输入或输出不满足安全边界。"""


@dataclass(frozen=True, slots=True)
class LogCounts:
    available: bool
    total: int = 0
    first: int = 0
    second: int = 0
    third: int = 0
    top_sources: tuple[tuple[str, int], ...] = ()
    unattributed: bool = False


def _line_matches_date(line: str, report_date: date) -> bool:
    iso = ISO_PREFIX.match(line)
    if iso is not None:
        return iso.group("date") == report_date.isoformat()
    space = SPACE_ISO_PREFIX.match(line)
    if space is not None:
        return space.group("date") == report_date.isoformat()
    syslog = SYSLOG_PREFIX.match(line)
    if syslog is not None:
        return (
            MONTHS.get(syslog.group("month")) == report_date.month
            and int(syslog.group("day")) == report_date.day
        )
    bracket = NGINX_BRACKET_DATE.search(line)
    if bracket is not None:
        return (
            int(bracket.group("day")) == report_date.day
            and MONTHS.get(bracket.group("month")) == report_date.month
            and int(bracket.group("year")) == report_date.year
        )
    return False


def _log_file_paths(path: Path) -> list[Path]:
    """枚举主日志、昨日轮转文件与压缩轮转文件，只接受普通文件。"""

    candidates = (
        path,
        path.with_name(path.name + ".1"),
        path.with_name(path.name + ".1.gz"),
    )
    files: list[Path] = []
    for candidate in candidates:
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise CollectorError("security evidence log is unavailable") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            continue
        if metadata.st_size > MAX_LOG_BYTES:
            raise CollectorError("security evidence log is too large")
        files.append(candidate)
    return files


def _iter_lines(paths: Sequence[Path]) -> Iterator[str]:
    """按给定顺序读取普通日志文件；gz 文件透明解压。"""

    for candidate in paths:
        opener = gzip.open if candidate.suffix == ".gz" else open
        try:
            with opener(candidate, "rt", encoding="utf-8", errors="replace") as source:
                yield from source
        except (OSError, UnicodeError) as error:
            raise CollectorError("security evidence log is unavailable") from error


def _first_ip(line: str) -> str | None:
    """从日志行提取首个 IPv4/IPv6 地址；仅用于聚合计数，不保留原始行。"""

    match = IPV4_RE.search(line) or IPV6_RE.search(line)
    return match.group(0) if match is not None else None


def _sanitize_uri(uri: str) -> str:
    """去掉查询串与尾部分隔符，并对手机号/令牌文本打码后截断。"""

    value = uri.split("?", 1)[0].strip().strip("\"'.,;)]")
    value = re.sub(r"(?<!\d)1\d{10}(?!\d)", "[phone]", value)
    value = re.sub(
        r"Bearer\s+[A-Za-z0-9._~+/-]+",
        "[token]",
        value,
        flags=re.IGNORECASE,
    )
    return value[:80]


def _web_uri(line: str) -> str | None:
    """从常见 nginx/访问日志行提取请求 URI（不含查询串）。"""

    for pattern in (WEB_METHOD_URI_RE, WEB_BRACKET_URI_RE, WEB_LEAD_URI_RE):
        match = pattern.search(line)
        if match is None:
            continue
        uri = _sanitize_uri(match.group(1))
        if uri and uri != "-":
            return uri
    return None


def _scan_log(
    path: Path,
    report_date: date,
    patterns: Sequence[re.Pattern[str]],
    *,
    source_pattern: re.Pattern[str] | None = None,
    message_required: re.Pattern[str] | None = None,
    bracketed_empty_is_zero: bool = False,
) -> LogCounts:
    """只扫描固定日志文件并返回匹配计数，不保留任何原始行。

    message_required 用于限定真实程序日志（如 sshd/fail2ban），避免把
    sudo COMMAND 等命令文本里的关键词误判为安全事件。
    """

    files = _log_file_paths(path)
    if not files:
        return LogCounts(False)
    counters = [0] * len(patterns)
    total = 0
    sources: Counter[str] = Counter()
    earliest: date | None = None
    latest: date | None = None
    for line in _iter_lines(files):
        line_date = _line_date(line, report_date.year)
        if line_date is not None:
            if earliest is None or line_date < earliest:
                earliest = line_date
            if latest is None or line_date > latest:
                latest = line_date
        if not _line_matches_date(line, report_date):
            continue
        total += 1
        if message_required is not None and message_required.search(line) is None:
            continue
        for index, pattern in enumerate(patterns):
            if pattern.search(line):
                counters[index] += 1
        if source_pattern is not None and source_pattern.search(line):
            source = _first_ip(line)
            if source is not None:
                sources[source] += 1
    top_sources = tuple(
        sorted(sources.items(), key=lambda item: (-item[1], item[0]))[:5]
    )
    if total == 0:
        if (
            bracketed_empty_is_zero
            and earliest is not None
            and latest is not None
            and earliest < report_date < latest
        ):
            # Fail2ban 在没有事件时不会为当天写日志。前后日期的可解析日志
            # 夹住目标日，证明同一证据源跨越了该窗口；应报告 0 次封禁，
            # 不能把安静日期误写成“未接入”。只有单侧旧/新日志仍失败关闭。
            return LogCounts(True)
        # 文件存在不代表覆盖目标日期；轮转后只剩新日志时必须显式缺失，
        # 禁止把“没有该日证据”伪装成零事件。
        return LogCounts(False, unattributed=True)
    return LogCounts(True, total, *counters, top_sources=top_sources)


@dataclass(frozen=True, slots=True)
class WebCounts:
    """Web/API 访问日志聚合；first 为状态码行数，其余按类别计数。"""

    available: bool
    unattributed: bool = False
    total: int = 0
    status_count: int = 0
    count_4xx: int = 0
    count_5xx: int = 0
    sensitive: int = 0
    top_5xx: tuple[tuple[str, int], ...] = ()
    top_4xx: tuple[tuple[str, int], ...] = ()
    sensitive_paths: tuple[tuple[str, int], ...] = ()


def _web_container_logs(docker_root: Path) -> list[Path]:
    """定位平台 web 容器的 docker json 日志，不依赖 docker CLI 或 socket。"""

    if not docker_root.is_dir():
        return []
    logs: list[Path] = []
    for config_path in docker_root.glob("containers/*/config.v2.json"):
        try:
            with config_path.open("r", encoding="utf-8") as source:
                data = json.load(source)
            name = str(data.get("Name") or "")
        except (OSError, ValueError):
            continue
        if "sms-platform" in name and "-web-" in name:
            container_dir = config_path.parent
            logs.append(container_dir / f"{container_dir.name}-json.log")
    return logs


def _docker_log_line_date(line: str, report_date: date) -> bool:
    """按 docker json 日志的 time 字段判断上海自然日归属。"""

    try:
        stamp = str(json.loads(line).get("time") or "")
        if not stamp:
            return False
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        return parsed.astimezone(SHANGHAI_TZ).date() == report_date
    except (ValueError, TypeError, json.JSONDecodeError):
        return False


def _docker_log_content(line: str) -> str:
    """提取 docker json 日志行的实际内容；非 JSON 行原样返回。"""

    try:
        return str(json.loads(line).get("log") or "")
    except (ValueError, TypeError, json.JSONDecodeError):
        return line


def _line_date(line: str, reference_year: int) -> date | None:
    """从日志行提取归属日期；无法解析返回 None。"""

    iso = ISO_PREFIX.match(line)
    if iso is not None:
        return date.fromisoformat(iso.group("date"))
    space = SPACE_ISO_PREFIX.match(line)
    if space is not None:
        return date.fromisoformat(space.group("date"))
    syslog = SYSLOG_PREFIX.match(line)
    if syslog is not None:
        month = MONTHS.get(syslog.group("month"))
        if month is not None:
            return date(reference_year, month, int(syslog.group("day")))
    bracket = NGINX_BRACKET_DATE.search(line)
    if bracket is not None:
        month = MONTHS.get(bracket.group("month"))
        if month is not None:
            return date(
                int(bracket.group("year")),
                month,
                int(bracket.group("day")),
            )
    return None


def _docker_log_line_date_obj(line: str) -> date | None:
    """提取 docker json 日志 time 字段对应的上海自然日。"""

    try:
        stamp = str(json.loads(line).get("time") or "")
        if not stamp:
            return None
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        return parsed.astimezone(SHANGHAI_TZ).date()
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _scan_web(
    web_log: Path,
    docker_root: Path,
    report_date: date,
) -> WebCounts:
    """聚合 Nginx 访问日志：优先持久化文件，缺失时回退容器 stdout 日志。"""

    files = _log_file_paths(web_log)
    docker_logs = _web_container_logs(docker_root) if not files else []
    if not files and not docker_logs:
        return WebCounts(False)
    earliest: date | None = None

    def observe(line_date: date | None) -> None:
        nonlocal earliest
        if line_date is not None and (earliest is None or line_date < earliest):
            earliest = line_date

    total = 0
    status_count = 0
    count_4xx = 0
    count_5xx = 0
    sensitive = 0
    five_xx: Counter[str] = Counter()
    four_xx: Counter[str] = Counter()
    sensitive_paths: Counter[str] = Counter()
    for line in _iter_lines(files):
        observe(_line_date(line, report_date.year))
        if not _line_matches_date(line, report_date):
            continue
        status_match = HTTP_STATUS.search(line)
        if status_match is None:
            continue
        total += 1
        status_count += 1
        status = int(status_match.group("status"))
        uri = _web_uri(line)
        if 400 <= status < 500:
            count_4xx += 1
            if uri is not None:
                four_xx[uri] += 1
        elif 500 <= status < 600:
            count_5xx += 1
            if uri is not None:
                five_xx[uri] += 1
        if uri is not None and SENSITIVE_PATH.search(uri):
            sensitive += 1
            normalized_uri = uri.casefold()
            for marker in SENSITIVE_MARKERS:
                if marker in normalized_uri:
                    sensitive_paths[marker] += 1
    for candidate in docker_logs:
        for line in _iter_lines([candidate]):
            observe(_docker_log_line_date_obj(line))
            if not _docker_log_line_date(line, report_date):
                continue
            content = _docker_log_content(line)
            status_match = HTTP_STATUS.search(content)
            if status_match is None:
                continue
            total += 1
            status_count += 1
            status = int(status_match.group("status"))
            uri = _web_uri(content)
            if 400 <= status < 500:
                count_4xx += 1
                if uri is not None:
                    four_xx[uri] += 1
            elif 500 <= status < 600:
                count_5xx += 1
                if uri is not None:
                    five_xx[uri] += 1
            if uri is not None and SENSITIVE_PATH.search(uri):
                sensitive += 1
                normalized_uri = uri.casefold()
                for marker in SENSITIVE_MARKERS:
                    if marker in normalized_uri:
                        sensitive_paths[marker] += 1
    if earliest is None or earliest > report_date:
        # 源存在但未覆盖报告窗口（例如容器/日志轮换后新起文件，或日志
        # 格式升级前旧行没有时间戳），视为缺失而不是用 0 冒充完整覆盖。
        return WebCounts(False, unattributed=True)
    return WebCounts(
        available=True,
        total=total,
        status_count=status_count,
        count_4xx=count_4xx,
        count_5xx=count_5xx,
        sensitive=sensitive,
        top_5xx=tuple(
            sorted(five_xx.items(), key=lambda item: (-item[1], item[0]))[:5]
        ),
        top_4xx=tuple(
            sorted(four_xx.items(), key=lambda item: (-item[1], item[0]))[:5]
        ),
        sensitive_paths=tuple(
            sorted(sensitive_paths.items(), key=lambda item: (-item[1], item[0]))
        ),
    )


def _platform_containers(docker_root: Path) -> list[dict[str, Any]]:
    """读取平台容器的持久化运行态配置，不保留任何容器标识以外的明细。"""

    if not docker_root.is_dir():
        return []
    containers: list[dict[str, Any]] = []
    for config_path in docker_root.glob("containers/*/config.v2.json"):
        try:
            with config_path.open("r", encoding="utf-8") as source:
                data = json.load(source)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        name = str(data.get("Name") or "")
        if name.startswith("/sms-platform-") or name.startswith("/sms-security-report-"):
            if name.endswith(("-migrate-1", "-db-role-provision-1")):
                # 一次性初始化容器按设计退出，不计入长期运行态探针。
                continue
            containers.append(data)
    return containers


def _runtime_rows(docker_root: Path) -> tuple[list[dict[str, str]], bool]:
    """聚合平台容器运行态；探针只输出计数，不输出容器 ID 或日志。"""

    rows: list[dict[str, str]] = [
        {"label": "证据采集器", "value": "已写入脱敏快照", "assessment": "正常", "tone": "good"},
    ]
    containers = _platform_containers(docker_root)
    if not containers:
        rows.append(
            {
                "label": "平台运行态",
                "value": "未接入独立探针",
                "assessment": "证据缺失",
                "tone": "warn",
            }
        )
        return rows, False
    total = len(containers)
    running_count = 0
    unhealthy = 0
    for container in containers:
        state = container.get("State")
        health_status = ""
        if not isinstance(state, dict):
            is_running = False
            restarting = False
            paused = False
        else:
            is_running = bool(state.get("Running"))
            restarting = bool(state.get("Restarting"))
            paused = bool(state.get("Paused"))
            health = state.get("Health")
            health_status = (
                str(health.get("Status") or "").casefold()
                if isinstance(health, dict)
                else ""
            )
        if is_running:
            running_count += 1
        if not is_running or paused or restarting or health_status == "unhealthy":
            unhealthy += 1
    rows.extend(
        [
            {
                "label": "平台容器总数",
                "value": f"{total} 个",
                "assessment": "按容器运行态聚合",
                "tone": "good",
            },
            {
                "label": "运行中容器",
                "value": f"{running_count} 个",
                "assessment": "状态来自 Docker 运行态",
                "tone": "good" if running_count == total else "warn",
            },
            {
                "label": "异常容器",
                "value": f"{unhealthy} 个",
                "assessment": "未运行、暂停、重启或健康检查失败",
                "tone": "good" if unhealthy == 0 else "danger",
            },
        ]
    )
    return rows, True


def _coverage(
    source: str,
    window: str,
    available: bool,
    note: str,
    *,
    missing_note: str | None = None,
) -> dict[str, str]:
    unavailable_note = missing_note or f"{note}；采集器未发现可读取的证据源"
    return {
        "source": source,
        "window": window,
        "status": "完整" if available else "缺失",
        "note": note if available else unavailable_note,
        "tone": "good" if available else "warn",
    }


def _detail(label: str, value: str, *, available: bool, note: str) -> dict[str, str]:
    return {
        "label": label,
        "value": value if available else "未接入",
        "assessment": note if available else "证据缺失",
        "tone": "good" if available else "warn",
    }


def collect_report(
    report_date: date,
    *,
    generated_at: datetime,
    auth_log: Path,
    fail2ban_log: Path,
    web_log: Path,
    docker_root: Path,
) -> dict[str, Any]:
    """生成一份只含聚合事实的安全日报 payload。"""

    if generated_at.tzinfo is None or generated_at.utcoffset() != timedelta(hours=8):
        raise CollectorError("generated_at must use +08:00")
    period_end = datetime.combine(
        report_date,
        datetime.max.time().replace(microsecond=0),
        tzinfo=SHANGHAI_TZ,
    )
    if generated_at <= period_end:
        raise CollectorError("generated_at must be after the report period")

    ssh = _scan_log(
        auth_log,
        report_date,
        (FAILED_SSH, ACCEPTED_SSH),
        source_pattern=FAILED_SSH,
        message_required=SSHD_MESSAGE_RE,
    )
    bans = _scan_log(
        fail2ban_log,
        report_date,
        (FAIL2BAN_BAN,),
        message_required=FAIL2BAN_MESSAGE_RE,
        bracketed_empty_is_zero=True,
    )
    web = _scan_web(web_log, docker_root, report_date)
    available_sources = sum((ssh.available, bans.available, web.available))
    if available_sources == 0:
        raise CollectorError("no security evidence source is available")
    runtime_rows, runtime_available = _runtime_rows(docker_root)

    gaps = [
        name
        for name, available in (
            ("SSH journal", ssh.available),
            ("Fail2ban", bans.available),
            ("Web/API access log", web.available),
            ("管理审计", False),
            ("运行态探针", runtime_available),
        )
        if not available
    ]
    status = "high" if web.sensitive else ("attention" if gaps or ssh.first else "normal")
    summary = "主机侧安全日志已完成脱敏聚合；" + (
        f"发现 {web.sensitive} 次敏感路径命中，需要人工核查。"
        if web.sensitive
        else "未发现敏感路径命中。"
    )
    pending = (
        f"待接入证据源：{'、'.join(gaps)}。"
        if gaps
        else ("存在 SSH 失败认证，需要确认是否属于授权运维。" if ssh.first else "无待确认事项。")
    )
    gap_value = str(len(gaps))
    ssh_value = str(ssh.first) if ssh.available else "不可用"
    accepted_value = str(ssh.second) if ssh.available else "不可用"
    ban_value = str(bans.first) if bans.available else "不可用"
    web_error_value = str(web.count_5xx) if web.available else "不可用"
    metrics = [
        {
            "label": "SSH 认证失败",
            "value": ssh_value,
            "tone": "warn" if ssh.first else "good",
            "note": "按日志聚合" if ssh.available else "证据缺失",
        },
        {
            "label": "SSH 成功认证",
            "value": accepted_value,
            "tone": "good" if ssh.available else "warn",
            "note": "不含来源明细" if ssh.available else "证据缺失",
        },
        {
            "label": "Fail2ban 封禁",
            "value": ban_value,
            "tone": "good" if bans.available else "warn",
            "note": "按日志聚合" if bans.available else "证据缺失",
        },
        {
            "label": "Web/API 5xx",
            "value": web_error_value,
            "tone": (
                "good"
                if web.available and not web.count_5xx
                else ("danger" if web.count_5xx else "warn")
            ),
            "note": "按访问日志聚合" if web.available else "证据缺失",
        },
        {
            "label": "证据覆盖缺口",
            "value": gap_value,
            "tone": "warn" if gaps else "good",
            "note": "缺口数量",
        },
    ]
    actions: list[dict[str, str]] = []
    if web.sensitive:
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
    if not actions:
        actions.append(
            {
                "priority": "low",
                "title": "保持现有采集计划",
                "detail": "继续按日报周期生成脱敏结构化快照。",
            }
        )

    def top_value(
        entries: Sequence[tuple[str, int]],
        *,
        limit: int,
    ) -> str:
        parts: list[tuple[str, int]] = []
        for name, count in entries[:limit]:
            candidate = (name[:80], count)
            if parts and (
                sum(len(part[0]) + len(str(part[1])) + 4 for part in parts)
                + len(candidate[0])
                + len(str(candidate[1]))
                + 4
                > 150
            ):
                break
            parts.append(candidate)
        while parts and (
            sum(len(name) + len(str(count)) + 4 for name, count in parts) - 1 > 150
        ):
            index = max(range(len(parts)), key=lambda i: len(parts[i][0]))
            name, count = parts[index]
            shortened = name[: max(8, len(name) - 12)]
            parts[index] = (shortened, count)
            if len(shortened) <= 8 and len(parts) > 1:
                del parts[index]
            elif len(shortened) <= 8:
                break
        return "、".join(f"{name} ×{count}" for name, count in parts)

    ssh_rows = [
        _detail(
            "失败认证",
            f"{ssh.first} 次",
            available=ssh.available,
            note="含失败来源 Top",
        ),
        _detail("成功认证", f"{ssh.second} 次", available=ssh.available, note="不展示来源明细"),
        _detail(
            "Fail2ban", f"{bans.first} 次封禁", available=bans.available, note="策略日志可读"
        ),
    ]
    if ssh.available and ssh.top_sources:
        ssh_rows.append(
            {
                "label": "失败来源 Top",
                "value": top_value(ssh.top_sources, limit=5),
                "assessment": "攻击来源，已按日聚合",
                "tone": "warn",
            }
        )

    web_rows = [
        _detail("访问请求", f"{web.total} 条", available=web.available, note="仅保留数量"),
        _detail("拒绝请求", f"{web.count_4xx} 条", available=web.available, note="按状态码聚合"),
        _detail(
            "服务端错误", f"{web.count_5xx} 条", available=web.available, note="按状态码聚合"
        ),
        _detail(
            "敏感路径",
            f"{web.sensitive} 次命中",
            available=web.available,
            note="不保留原始路径",
        ),
    ]
    if web.available and web.top_5xx:
        web_rows.append(
            {
                "label": "5xx 路径 Top",
                "value": top_value(web.top_5xx, limit=3),
                "assessment": "服务端错误路径",
                "tone": "danger",
            }
        )
    if web.available and web.top_4xx:
        web_rows.append(
            {
                "label": "4xx 路径 Top",
                "value": top_value(web.top_4xx, limit=3),
                "assessment": "客户端错误路径",
                "tone": "warn",
            }
        )
    if web.available and web.sensitive_paths:
        web_rows.append(
            {
                "label": "敏感路径明细",
                "value": top_value(web.sensitive_paths, limit=4),
                "assessment": "命中模式",
                "tone": "danger",
            }
        )

    window = f"{report_date.isoformat()} 00:00 — 23:59（UTC+8）"
    payload: dict[str, Any] = {
        "schema_version": 1,
        "report_date": report_date.isoformat(),
        "period_start": datetime.combine(
            report_date, datetime.min.time(), tzinfo=SHANGHAI_TZ
        ).isoformat(),
        "period_end": period_end.isoformat(),
        "generated_at": generated_at.isoformat(),
        "status": status,
        "summary": summary,
        "pending_confirmation": pending,
        "metrics": metrics,
        "ssh": ssh_rows,
        "web": web_rows,
        "audit": [],
        "runtime": runtime_rows,
        "actions": actions,
        "coverage": [
            _coverage(
                "SSH journal",
                window,
                ssh.available,
                "认证事件仅保留计数",
                missing_note=(
                    "日志已接入，但目标日期无可归属记录"
                    if ssh.unattributed
                    else "认证事件仅保留计数"
                ),
            ),
            _coverage(
                "Fail2ban",
                window,
                bans.available,
                "封禁事件仅保留计数",
                missing_note=(
                    "日志已接入，但目标日期无可归属记录"
                    if bans.unattributed
                    else "封禁事件仅保留计数"
                ),
            ),
            _coverage(
                "Web/API access log",
                window,
                web.available,
                "请求仅按状态码聚合",
                missing_note=(
                    "日志已接入，但该日无带时间戳记录可归属（格式升级过渡期）"
                    if web.unattributed
                    else "请求仅按状态码聚合"
                ),
            ),
            _coverage("管理审计", window, False, "需由平台审计事实单独接入"),
            _coverage("运行态探针", window, runtime_available, "容器状态仅保留聚合计数"),
        ],
    }
    parse_report(payload)
    return payload


def write_snapshot(payload: dict[str, Any], output_dir: Path, *, owner_uid: int) -> Path:
    """以固定权限原子写入指定日期快照。"""

    try:
        if output_dir.exists() and output_dir.is_symlink():
            raise CollectorError("security evidence output directory is unsafe")
        output_dir.mkdir(mode=0o750, parents=False, exist_ok=True)
        if os.geteuid() == 0:
            # The systemd unit drops CAP_DAC_OVERRIDE, so root must own the
            # directory while writing; ownership is handed back to the runtime
            # uid only after the snapshot is durable.
            os.chown(output_dir, 0, -1)
            os.chmod(output_dir, 0o750)
        metadata = output_dir.lstat()
    except (OSError, CollectorError) as error:
        if isinstance(error, CollectorError):
            raise
        raise CollectorError("security evidence output directory is unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise CollectorError("security evidence output directory is unavailable")
    report_date = str(payload["report_date"])
    destination = output_dir / f"{report_date}.json"
    temporary = output_dir / f".{report_date}.{os.getpid()}.tmp"
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
        )
        os.chmod(temporary, 0o640)
        if os.geteuid() == 0:
            os.chown(temporary, owner_uid, -1)
        os.replace(temporary, destination)
        if os.geteuid() == 0:
            os.chown(output_dir, owner_uid, -1)
            os.chmod(output_dir, 0o750)
    except OSError as error:
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise CollectorError("security evidence snapshot could not be written") from error
    return destination


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Write a redacted security daily evidence snapshot"
    )
    parser.add_argument("--report-date", type=date.fromisoformat)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--auth-log", type=Path, default=Path("/var/log/auth.log"))
    parser.add_argument("--fail2ban-log", type=Path, default=Path("/var/log/fail2ban.log"))
    parser.add_argument("--web-log", type=Path, default=DEFAULT_WEB_LOG)
    parser.add_argument("--docker-root", type=Path, default=DEFAULT_DOCKER_ROOT)
    parser.add_argument("--owner-uid", type=int, default=DEFAULT_OWNER_UID)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    now = datetime.now(SHANGHAI_TZ)
    report_date = args.report_date or (now.date() - timedelta(days=1))
    try:
        payload = collect_report(
            report_date,
            generated_at=now,
            auth_log=args.auth_log,
            fail2ban_log=args.fail2ban_log,
            web_log=args.web_log,
            docker_root=args.docker_root,
        )
        destination = write_snapshot(payload, args.output_dir, owner_uid=args.owner_uid)
    except (CollectorError, ValueError):
        print("security evidence collection blocked", file=sys.stderr)
        return 1
    print(f"security evidence snapshot written: {destination.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
