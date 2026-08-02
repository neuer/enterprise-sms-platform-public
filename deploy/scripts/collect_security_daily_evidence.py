#!/usr/bin/env python3
"""从主机日志生成脱敏安全日报证据快照。

该采集器只输出聚合计数和覆盖状态，不把日志原文、IP、账号或请求路径写入
快照。它是主机侧的独立入口；平台 API 只消费已经落盘的 JSON，不读取主机日志。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import stat
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from render_security_daily_report import parse_report

SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
MAX_LOG_BYTES = 256 * 1024 * 1024
DEFAULT_OWNER_UID = 10001
SYSLOG_PREFIX = re.compile(r"^(?P<month>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+")
ISO_PREFIX = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})T")
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
FAILED_SSH = re.compile(r"(?:Failed password|Invalid user|authentication failure)", re.I)
ACCEPTED_SSH = re.compile(r"\bAccepted\s+(?:publickey|keyboard-interactive|password)\b", re.I)
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
    syslog = SYSLOG_PREFIX.match(line)
    if syslog is None:
        return False
    return (
        MONTHS.get(syslog.group("month")) == report_date.month
        and int(syslog.group("day")) == report_date.day
    )


def _scan_log(path: Path, report_date: date, patterns: Sequence[re.Pattern[str]]) -> LogCounts:
    """只扫描固定日志文件并返回匹配计数，不保留任何原始行。"""

    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            return LogCounts(False)
        if metadata.st_size > MAX_LOG_BYTES:
            raise CollectorError("security evidence log is too large")
        counters = [0] * len(patterns)
        total = 0
        with path.open("r", encoding="utf-8", errors="replace") as source:
            for line in source:
                if not _line_matches_date(line, report_date):
                    continue
                total += 1
                for index, pattern in enumerate(patterns):
                    if pattern.search(line):
                        counters[index] += 1
        return LogCounts(True, total, *counters)
    except FileNotFoundError:
        return LogCounts(False)
    except CollectorError:
        raise
    except (OSError, UnicodeError) as error:
        raise CollectorError("security evidence log is unavailable") from error


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
    web = _scan_log(web_log, report_date, (HTTP_STATUS, SENSITIVE_PATH))
    available_sources = sum((ssh.available, bans.available, web.available))
    if available_sources == 0:
        raise CollectorError("no security evidence source is available")

    web_4xx = 0
    web_5xx = 0
    web_sensitive = 0
    if web.available:
        # ``first`` is the HTTP status match count, so rescan status classes only
        # in memory; neither status lines nor paths are persisted.
        try:
            with web_log.open("r", encoding="utf-8", errors="replace") as source:
                for line in source:
                    if not _line_matches_date(line, report_date):
                        continue
                    status_match = HTTP_STATUS.search(line)
                    if status_match is not None:
                        status = int(status_match.group("status"))
                        web_4xx += int(400 <= status < 500)
                        web_5xx += int(500 <= status < 600)
                    web_sensitive += int(SENSITIVE_PATH.search(line) is not None)
        except (OSError, UnicodeError) as error:
            raise CollectorError("security evidence log is unavailable") from error

    gaps = [
        name
        for name, available in (
            ("SSH journal", ssh.available),
            ("Fail2ban", bans.available),
            ("Web/API access log", web.available),
            ("管理审计", False),
            ("运行态探针", False),
        )
        if not available
    ]
    status = "high" if web_sensitive else ("attention" if gaps or ssh.first else "normal")
    summary = "主机侧安全日志已完成脱敏聚合；" + (
        f"发现 {web_sensitive} 次敏感路径命中，需要人工核查。"
        if web_sensitive
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
    web_error_value = str(web_5xx) if web.available else "不可用"
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
            "tone": "good" if web.available and not web_5xx else ("danger" if web_5xx else "warn"),
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
    if web_sensitive:
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
            _detail("拒绝请求", f"{web_4xx} 条", available=web.available, note="按状态码聚合"),
            _detail("服务端错误", f"{web_5xx} 条", available=web.available, note="按状态码聚合"),
            _detail(
                "敏感路径",
                f"{web_sensitive} 次命中",
                available=web.available,
                note="不保留原始路径",
            ),
        ],
        "audit": [],
        "runtime": [
            {
                "label": "证据采集器",
                "value": "已写入脱敏快照",
                "assessment": "正常",
                "tone": "good",
            },
            {
                "label": "平台运行态",
                "value": "未接入独立探针",
                "assessment": "证据缺失",
                "tone": "warn",
            },
        ],
        "actions": actions,
        "coverage": [
            _coverage("SSH journal", window, ssh.available, "认证事件仅保留计数"),
            _coverage("Fail2ban", window, bans.available, "封禁事件仅保留计数"),
            _coverage("Web/API access log", window, web.available, "请求仅按状态码聚合"),
            _coverage("管理审计", window, False, "需由平台审计事实单独接入"),
            _coverage("运行态探针", window, False, "需由平台健康探针单独接入"),
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
    parser.add_argument("--web-log", type=Path, default=Path("/var/log/nginx/access.log"))
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
        )
        destination = write_snapshot(payload, args.output_dir, owner_uid=args.owner_uid)
    except (CollectorError, ValueError):
        print("security evidence collection blocked", file=sys.stderr)
        return 1
    print(f"security evidence snapshot written: {destination.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
