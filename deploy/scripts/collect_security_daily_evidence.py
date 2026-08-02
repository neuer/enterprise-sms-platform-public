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


class CollectorError(RuntimeError):
    """采集输入或输出不满足安全边界。"""


@dataclass(frozen=True, slots=True)
class LogCounts:
    available: bool
    total: int = 0
    first: int = 0
    second: int = 0
    third: int = 0


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
                for line in source:
                    yield line
        except (OSError, UnicodeError) as error:
            raise CollectorError("security evidence log is unavailable") from error


def _scan_log(path: Path, report_date: date, patterns: Sequence[re.Pattern[str]]) -> LogCounts:
    """只扫描固定日志文件并返回匹配计数，不保留任何原始行。"""

    files = _log_file_paths(path)
    if not files:
        return LogCounts(False)
    counters = [0] * len(patterns)
    total = 0
    for line in _iter_lines(files):
        if not _line_matches_date(line, report_date):
            continue
        total += 1
        for index, pattern in enumerate(patterns):
            if pattern.search(line):
                counters[index] += 1
    return LogCounts(True, total, *counters)


@dataclass(frozen=True, slots=True)
class WebCounts:
    """Web/API 访问日志聚合；first 为状态码行数，其余按类别计数。"""

    available: bool
    total: int = 0
    status_count: int = 0
    count_4xx: int = 0
    count_5xx: int = 0
    sensitive: int = 0


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
    total = 0
    status_count = 0
    count_4xx = 0
    count_5xx = 0
    sensitive = 0
    for line in _iter_lines(files):
        if not _line_matches_date(line, report_date):
            continue
        total += 1
        status_match = HTTP_STATUS.search(line)
        if status_match is not None:
            status_count += 1
            status = int(status_match.group("status"))
            count_4xx += int(400 <= status < 500)
            count_5xx += int(500 <= status < 600)
        sensitive += int(SENSITIVE_PATH.search(line) is not None)
    for candidate in docker_logs:
        for line in _iter_lines([candidate]):
            if not _docker_log_line_date(line, report_date):
                continue
            content = _docker_log_content(line)
            total += 1
            status_match = HTTP_STATUS.search(content)
            if status_match is not None:
                status_count += 1
                status = int(status_match.group("status"))
                count_4xx += int(400 <= status < 500)
                count_5xx += int(500 <= status < 600)
            sensitive += int(SENSITIVE_PATH.search(content) is not None)
    return WebCounts(True, total, status_count, count_4xx, count_5xx, sensitive)


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
    running = 0
    unhealthy = 0
    healthy = 0
    checked = 0
    for container in containers:
        state = container.get("State")
        status = str(state.get("Status") or "unknown") if isinstance(state, dict) else "unknown"
        health = state.get("Health") if isinstance(state, dict) else None
        health_status = str(health.get("Status") or "") if isinstance(health, dict) else ""
        if status == "running":
            running += 1
        if status != "running" or health_status == "unhealthy":
            unhealthy += 1
        if health_status:
            checked += 1
            if health_status == "healthy":
                healthy += 1
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
                "value": f"{running} 个",
                "assessment": "状态来自 Docker 运行态",
                "tone": "good" if running == total else "warn",
            },
            {
                "label": "异常容器",
                "value": f"{unhealthy} 个",
                "assessment": "未运行或健康检查不通过",
                "tone": "good" if unhealthy == 0 else "danger",
            },
            {
                "label": "健康检查通过",
                "value": f"{healthy}/{checked} 个",
                "assessment": "仅统计配置了健康检查的容器",
                "tone": "good" if checked and healthy == checked else "warn",
            },
        ]
    )
    return rows, True


def _coverage(source: str, window: str, available: bool, note: str) -> dict[str, str]:
    return {
        "source": source,
        "window": window,
        "status": "完整" if available else "缺失",
        "note": note if available else f"{note}；采集器未发现可读取的证据源",
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

    ssh = _scan_log(auth_log, report_date, (FAILED_SSH, ACCEPTED_SSH))
    bans = _scan_log(fail2ban_log, report_date, (FAIL2BAN_BAN,))
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
        "ssh": [
            _detail("失败认证", f"{ssh.first} 次", available=ssh.available, note="仅保留聚合数量"),
            _detail("成功认证", f"{ssh.second} 次", available=ssh.available, note="不展示来源明细"),
            _detail(
                "Fail2ban", f"{bans.first} 次封禁", available=bans.available, note="策略日志可读"
            ),
        ],
        "web": [
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
        ],
        "audit": [],
        "runtime": runtime_rows,
        "actions": actions,
        "coverage": [
            _coverage("SSH journal", window, ssh.available, "认证事件仅保留计数"),
            _coverage("Fail2ban", window, bans.available, "封禁事件仅保留计数"),
            _coverage("Web/API access log", window, web.available, "请求仅按状态码聚合"),
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
