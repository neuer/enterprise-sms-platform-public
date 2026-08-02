from __future__ import annotations

import gzip
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
        docker_root=tmp_path,
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
        docker_root=tmp_path,
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
            docker_root=tmp_path,
        )


def test_collector_reads_rotated_and_gzipped_logs_with_real_formats(
    tmp_path: Path,
) -> None:
    auth = tmp_path / "auth.log"
    auth.write_text(
        "2026-08-02T00:05:00+08:00 host sshd[1]: Accepted key ED25519 SHA256:abc\n",
        encoding="utf-8",
    )
    rotated_auth = tmp_path / "auth.log.1"
    rotated_auth.write_text(
        "2026-08-01T01:00:00+08:00 host sshd[2]: Failed password for invalid user\n"
        "2026-08-01T02:00:00+08:00 host sshd[3]: Accepted key ED25519 SHA256:abc\n"
        "2026-08-01T03:00:00+08:00 host sshd[4]: Connection closed by invalid user root\n",
        encoding="utf-8",
    )
    fail2ban = tmp_path / "fail2ban.log"
    fail2ban.write_text("2026-08-02 00:00:06,264 fail2ban.server: rollover\n", encoding="utf-8")
    with gzip.open(tmp_path / "fail2ban.log.1.gz", "wt", encoding="utf-8") as source:
        source.write("2026-08-01 07:50:01,123 fail2ban.actions: Ban 192.0.2.8\n")
    web = tmp_path / "access.log"
    web.write_text(
        "127.0.0.1 - - [01/Aug/2026:04:00:01 +0800] \"GET /api/health HTTP/1.1\" 200 12\n"
        "127.0.0.1 - - [01/Aug/2026:04:01:01 +0800] \"GET /.env HTTP/1.1\" 404 4\n"
        "127.0.0.1 - - [01/Aug/2026:04:02:01 +0800] \"GET /readyz HTTP/1.1\" 500 5\n",
        encoding="utf-8",
    )

    payload = collect_report(
        REPORT_DATE,
        generated_at=GENERATED_AT,
        auth_log=auth,
        fail2ban_log=fail2ban,
        web_log=web,
        docker_root=tmp_path,
    )

    assert payload["metrics"][0]["value"] == "2"  # Failed password + invalid user
    assert payload["metrics"][1]["value"] == "1"  # Accepted key
    assert payload["metrics"][2]["value"] == "1"  # Fail2ban ban from .1.gz
    assert payload["metrics"][3]["value"] == "1"  # Web 5xx from bracket format
    assert payload["web"][1]["value"] == "1 条"  # 4xx
    assert payload["web"][3]["value"] == "1 次命中"  # sensitive /.env
    assert payload["status"] == "high"


def test_collector_falls_back_to_web_container_log_and_runtime_probe(
    tmp_path: Path,
) -> None:
    auth = tmp_path / "auth.log"
    auth.write_text("Aug 1 01:00:00 host sshd[1]: Accepted publickey\n", encoding="utf-8")
    docker_root = tmp_path / "docker"
    container_dir = docker_root / "containers" / "web01"
    container_dir.mkdir(parents=True)
    container_dir.joinpath("config.v2.json").write_text(
        json.dumps(
            {
                "Name": "/sms-platform-web-1",
                "State": {
                    "Running": True,
                    "Health": {"Status": "healthy"},
                },
            }
        ),
        encoding="utf-8",
    )
    container_dir.joinpath("web01-json.log").write_text(
        json.dumps({"log": "203.0.113.9 /api/v1/reports 500 23\n", "time": "2026-08-01T04:00:00Z"})
        + "\n"
        + json.dumps(
            {"log": "203.0.113.9 /api/v1/reports 200 23\n", "time": "2026-08-01T04:01:00Z"}
        )
        + "\n"
        + json.dumps({"log": "203.0.113.9 / 200 3\n", "time": "2026-08-02T04:00:00Z"})
        + "\n",
        encoding="utf-8",
    )
    migrate_dir = docker_root / "containers" / "migrate01"
    migrate_dir.mkdir()
    migrate_dir.joinpath("config.v2.json").write_text(
        json.dumps(
            {
                "Name": "/sms-platform-migrate-1",
                "State": {"Running": False, "FinishedAt": "2026-08-02T00:00:00Z"},
            }
        ),
        encoding="utf-8",
    )

    payload = collect_report(
        REPORT_DATE,
        generated_at=GENERATED_AT,
        auth_log=auth,
        fail2ban_log=tmp_path / "missing-fail2ban.log",
        web_log=tmp_path / "missing-web.log",
        docker_root=docker_root,
    )

    assert payload["metrics"][3]["value"] == "1"
    assert payload["web"][0]["value"] == "2 条"
    assert payload["web"][1]["value"] == "0 条"
    assert {item["source"] for item in payload["coverage"] if item["tone"] == "warn"} == {
        "Fail2ban",
        "管理审计",
    }
    runtime_values = {item["label"]: item["value"] for item in payload["runtime"]}
    assert runtime_values["平台容器总数"] == "1 个"
    assert runtime_values["运行中容器"] == "1 个"
    assert runtime_values["异常容器"] == "0 个"
    assert runtime_values["健康检查通过"] == "1/1 个"
    assert payload["status"] == "attention"  # Fail2ban 缺口保留


def test_collector_marks_web_unavailable_when_log_does_not_cover_window(
    tmp_path: Path,
) -> None:
    auth = tmp_path / "auth.log"
    auth.write_text("Aug 1 01:00:00 host sshd[1]: Accepted publickey\n", encoding="utf-8")
    docker_root = tmp_path / "docker"
    container_dir = docker_root / "containers" / "web01"
    container_dir.mkdir(parents=True)
    container_dir.joinpath("config.v2.json").write_text(
        json.dumps(
            {
                "Name": "/sms-platform-web-1",
                "State": {"Running": True, "Health": {"Status": "healthy"}},
            }
        ),
        encoding="utf-8",
    )
    container_dir.joinpath("web01-json.log").write_text(
        json.dumps({"log": "203.0.113.9 / 200 3\n", "time": "2026-08-02T04:00:00Z"}) + "\n",
        encoding="utf-8",
    )

    payload = collect_report(
        REPORT_DATE,
        generated_at=GENERATED_AT,
        auth_log=auth,
        fail2ban_log=tmp_path / "missing-fail2ban.log",
        web_log=tmp_path / "missing-web.log",
        docker_root=docker_root,
    )

    assert payload["metrics"][3]["value"] == "不可用"
    assert {item["source"] for item in payload["coverage"] if item["tone"] == "warn"} == {
        "Fail2ban",
        "Web/API access log",
        "管理审计",
    }
