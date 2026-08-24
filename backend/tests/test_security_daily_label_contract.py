"""采集器快照标签与平台 enrich 期望标签的契约。

平台 ``app.services.security_daily.enrich`` 通过 ``_labeled_row`` 按中文标签定位
collector 快照中的指标/明细行（环比、状态判定都依赖该匹配）。collector 脚本内联了
这些标签字符串，本测试用 ``collect_report`` 的真实产出钉住两侧，标签漂移即失败。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

from app.services.security_daily import enrich

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "deploy" / "scripts"
COLLECTOR_SCRIPT = SCRIPTS / "collect_security_daily_evidence.py"

SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
REPORT_DATE = date(2026, 8, 1)
GENERATED_AT = datetime(2026, 8, 2, 7, 50, tzinfo=SHANGHAI)


def _collector() -> ModuleType:
    assert COLLECTOR_SCRIPT.is_file(), "安全日报证据采集器尚未实现"
    scripts_path = str(SCRIPTS)
    if scripts_path not in sys.path:
        sys.path.insert(0, scripts_path)
    spec = importlib.util.spec_from_file_location(
        "collect_security_daily_evidence",
        COLLECTOR_SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _collect_payload(tmp_path: Path) -> dict[str, Any]:
    """用最小可用证据源产出一份真实 collector 快照。"""

    auth_log = tmp_path / "auth.log"
    auth_log.write_text(
        "Aug 1 01:00:00 host sshd[1]: Failed password for invalid user "
        "from 198.51.100.27 port 22\n"
        "Aug 1 02:00:00 host sshd[2]: Accepted publickey for operator\n",
        encoding="utf-8",
    )
    fail2ban_log = tmp_path / "fail2ban.log"
    fail2ban_log.write_text(
        "2026-08-01T03:00:00+08:00 fail2ban: Ban 192.0.2.8\n",
        encoding="utf-8",
    )
    web_log = tmp_path / "access.log"
    web_log.write_text(
        "2026-08-01T04:00:00+08:00 GET / 200\n"
        "2026-08-01T04:02:00+08:00 GET /health 500\n",
        encoding="utf-8",
    )
    container = tmp_path / "containers" / "sms-platform-api-1"
    container.mkdir(parents=True)
    (container / "config.v2.json").write_text(
        json.dumps(
            {
                "Name": "/sms-platform-api-1",
                "State": {
                    "Running": True,
                    "Paused": False,
                    "Restarting": False,
                    "Health": {"Status": "healthy"},
                },
            }
        ),
        encoding="utf-8",
    )
    return _collector().collect_report(
        REPORT_DATE,
        generated_at=GENERATED_AT,
        auth_log=auth_log,
        fail2ban_log=fail2ban_log,
        web_log=web_log,
        docker_root=tmp_path,
    )


def _labels(payload: dict[str, Any], section: str) -> set[str]:
    return {str(item["label"]) for item in payload[section]}


def test_collector_labels_cover_enrich_expectations(tmp_path: Path) -> None:
    payload = _collect_payload(tmp_path)

    metric_labels = _labels(payload, "metrics")
    assert {
        enrich.WEB_5XX_METRIC_LABEL,
        enrich.GAP_METRIC_LABEL,
    } <= metric_labels
    # SSH 失败指标允许任一历史别名，但必须存在其一。
    assert metric_labels & set(enrich.SSH_FAILURE_METRIC_LABELS)
    assert enrich.SENSITIVE_PATH_LABEL in _labels(payload, "web")
    assert enrich.RUNTIME_UNHEALTHY_LABEL in _labels(payload, "runtime")


def test_enrich_labeled_row_resolves_every_expected_label(tmp_path: Path) -> None:
    payload = _collect_payload(tmp_path)

    assert (
        enrich._labeled_row(payload["metrics"], *enrich.SSH_FAILURE_METRIC_LABELS)
        is not None
    )
    assert (
        enrich._labeled_row(payload["metrics"], enrich.WEB_5XX_METRIC_LABEL) is not None
    )
    assert enrich._labeled_row(payload["metrics"], enrich.GAP_METRIC_LABEL) is not None
    assert enrich._labeled_row(payload["web"], enrich.SENSITIVE_PATH_LABEL) is not None
    assert (
        enrich._labeled_row(payload["runtime"], enrich.RUNTIME_UNHEALTHY_LABEL)
        is not None
    )
