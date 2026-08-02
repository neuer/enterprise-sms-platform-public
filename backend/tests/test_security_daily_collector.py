from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))

from collect_security_daily_evidence import (  # noqa: E402
    CollectorError,
    collect_report,
    write_snapshot,
)

SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
REPORT_DATE = date(2026, 8, 1)
GENERATED_AT = datetime(2026, 8, 2, 7, 50, tzinfo=SHANGHAI)


def _logs(tmp_path: Path) -> tuple[Path, Path, Path]:
    auth = tmp_path / "auth.log"
    auth.write_text(
        "Aug 1 01:00:00 host sshd[1]: Failed password for invalid user\n"
        "Aug 1 02:00:00 host sshd[2]: Accepted publickey for operator\n"
        "Jul 31 23:59:59 host sshd[3]: Failed password for old\n",
        encoding="utf-8",
    )
    fail2ban = tmp_path / "fail2ban.log"
    fail2ban.write_text(
        "2026-08-01T03:00:00+08:00 fail2ban: Ban 192.0.2.8\n",
        encoding="utf-8",
    )
    web = tmp_path / "access.log"
    web.write_text(
        "2026-08-01T04:00:00+08:00 GET / 200\n"
        "2026-08-01T04:01:00+08:00 GET /.env 404\n"
        "2026-08-01T04:02:00+08:00 GET /health 500\n",
        encoding="utf-8",
    )
    return auth, fail2ban, web


def test_collector_writes_only_aggregated_redacted_evidence(tmp_path: Path) -> None:
    auth, fail2ban, web = _logs(tmp_path)

    payload = collect_report(
        REPORT_DATE,
        generated_at=GENERATED_AT,
        auth_log=auth,
        fail2ban_log=fail2ban,
        web_log=web,
    )

    assert payload["status"] == "high"
    assert payload["metrics"][0]["value"] == "1"
    assert payload["metrics"][1]["value"] == "1"
    assert payload["metrics"][2]["value"] == "1"
    assert payload["metrics"][3]["value"] == "1"
    encoded = json.dumps(payload, ensure_ascii=False)
    assert "192.0.2.8" not in encoded
    assert "operator" not in encoded
    assert "/.env" not in encoded

    destination = write_snapshot(payload, tmp_path, owner_uid=10_001)
    assert json.loads(destination.read_text(encoding="utf-8"))["report_date"] == "2026-08-01"
    assert destination.stat().st_mode & 0o007 == 0


def test_collector_keeps_missing_sources_as_explicit_coverage_gaps(tmp_path: Path) -> None:
    auth = tmp_path / "auth.log"
    auth.write_text("Aug 1 01:00:00 host sshd[1]: Accepted publickey\n", encoding="utf-8")

    payload = collect_report(
        REPORT_DATE,
        generated_at=GENERATED_AT,
        auth_log=auth,
        fail2ban_log=tmp_path / "missing-fail2ban.log",
        web_log=tmp_path / "missing-web.log",
    )

    assert payload["status"] == "attention"
    assert {item["source"] for item in payload["coverage"] if item["tone"] == "warn"} >= {
        "Fail2ban",
        "Web/API access log",
        "管理审计",
        "运行态探针",
    }
    assert payload["metrics"][2]["value"] == "不可用"


def test_collector_refuses_to_write_when_no_source_is_available(tmp_path: Path) -> None:
    with pytest.raises(CollectorError, match="no security evidence source"):
        collect_report(
            REPORT_DATE,
            generated_at=GENERATED_AT,
            auth_log=tmp_path / "missing-auth.log",
            fail2ban_log=tmp_path / "missing-fail2ban.log",
            web_log=tmp_path / "missing-web.log",
        )
