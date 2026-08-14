from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from app.services.security_daily import SecurityDailyAuditEvent, SecurityDailyAuditEvidence
from app.tasks.security_daily import (
    generate_security_daily_once,
    submit_security_daily_deliveries,
)

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "deploy" / "templates" / "security_daily_report.sample.json"
SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")


class FakeDeliveryService:
    def __init__(self) -> None:
        self.submitted: list[date] = []

    async def submit_auto_delivery(self, report_date: date) -> None:
        self.submitted.append(report_date)


class FakePendingRepository:
    async def pending_delivery_requests(self) -> tuple[tuple[UUID, date], ...]:
        pending_dates = (
            date(2026, 8, 12),
            date(2026, 8, 11),
            date(2026, 8, 11),
            *(date(2026, 8, day) for day in range(10, 3, -1)),
        )
        return tuple((UUID(int=index), value) for index, value in enumerate(pending_dates, 1))


@pytest.mark.asyncio
async def test_delivery_sweep_includes_unique_historical_pending_dates() -> None:
    service = FakeDeliveryService()

    await submit_security_daily_deliveries(
        service,
        FakePendingRepository(),
        current_report_date=date(2026, 8, 12),
    )

    assert service.submitted == [
        date(2026, 8, 12),
        date(2026, 8, 11),
        date(2026, 8, 10),
        date(2026, 8, 9),
        date(2026, 8, 8),
        date(2026, 8, 7),
        date(2026, 8, 6),
        date(2026, 8, 5),
    ]


class FakeRepository:
    def __init__(self) -> None:
        self.ingested: list[dict[str, Any]] = []
        self.unavailable: list[tuple[str, str]] = []
        self.audit: SecurityDailyAuditEvidence | None = None

    async def audit_evidence(
        self, period_start: Any, period_end: Any
    ) -> SecurityDailyAuditEvidence | None:
        return self.audit

    async def ingest_payload(
        self,
        payload: dict[str, Any],
        *,
        recipient_count: int,
        force: bool = False,
        generation_source: str = "auto",
    ) -> bool:
        self.ingested.append(
            {
                "payload": payload,
                "recipient_count": recipient_count,
                "force": force,
                "generation_source": generation_source,
            }
        )
        return True

    async def mark_unavailable(
        self,
        report_date: Any,
        *,
        period_start: Any,
        period_end: Any,
        reason: str,
        generation_source: str = "auto",
    ) -> bool:
        if (report_date.isoformat(), reason) in self.unavailable:
            return False
        self.unavailable.append((report_date.isoformat(), reason))
        return True

    async def get_latest_report(self, report_date: Any, **kwargs: Any) -> None:
        del report_date, kwargs
        return None

    async def mark_delivery_failed(self, report_date: Any, message: str) -> bool:
        return True


@pytest.mark.asyncio
async def test_generation_waits_until_0800_and_marks_missing_evidence_unavailable(
    tmp_path: Path,
) -> None:
    repository = FakeRepository()
    before_schedule = await generate_security_daily_once(
        repository,
        tmp_path,
        now=datetime(2026, 7, 16, 7, 59, tzinfo=SHANGHAI),
        enabled=True,
        recipient_count=1,
    )
    assert before_schedule == 0
    assert repository.unavailable == []

    changed = await generate_security_daily_once(
        repository,
        tmp_path,
        now=datetime(2026, 7, 16, 8, 0, tzinfo=SHANGHAI),
        enabled=True,
        recipient_count=1,
    )
    assert changed == 1
    assert repository.unavailable == [("2026-07-15", "安全日报证据源不可用")]

    repeated = await generate_security_daily_once(
        repository,
        tmp_path,
        now=datetime(2026, 7, 16, 8, 1, tzinfo=SHANGHAI),
        enabled=True,
        recipient_count=1,
    )
    assert repeated == 0


@pytest.mark.asyncio
async def test_generation_consumes_only_validated_redacted_snapshot(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    incoming.joinpath("2026-07-15.json").write_text(
        SAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    repository = FakeRepository()

    changed = await generate_security_daily_once(
        repository,
        tmp_path,
        now=datetime(2026, 7, 16, 8, 1, tzinfo=SHANGHAI),
        enabled=True,
        recipient_count=2,
    )

    assert changed == 1
    assert len(repository.ingested) == 1
    assert repository.ingested[0]["recipient_count"] == 2
    assert repository.ingested[0]["payload"]["generated_at"] == ("2026-07-16T08:01:00+08:00")
    assert repository.unavailable == []


@pytest.mark.asyncio
async def test_generation_rejects_incomplete_detail_sections_before_enrichment(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    sample = json.loads(SAMPLE.read_text(encoding="utf-8"))
    sample["web"] = sample["web"][:1]
    incoming.joinpath("2026-07-15.json").write_text(
        json.dumps(sample, ensure_ascii=False), encoding="utf-8"
    )
    repository = FakeRepository()

    changed = await generate_security_daily_once(
        repository,
        tmp_path,
        now=datetime(2026, 7, 16, 8, 1, tzinfo=SHANGHAI),
        enabled=True,
        recipient_count=2,
    )

    assert changed == 1
    assert repository.ingested == []
    assert repository.unavailable == [("2026-07-15", "安全日报证据源校验失败")]


@pytest.mark.asyncio
async def test_generation_injects_platform_audit_evidence_and_recomputes_gaps(
    tmp_path: Path,
) -> None:
    native_ipv6 = "2606:4700:4700:1111:2222:3333:4444:5555"
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    sample = json.loads(SAMPLE.read_text(encoding="utf-8"))
    sample["metrics"][4] = {
        "label": "证据覆盖缺口",
        "value": "1",
        "tone": "warn",
        "note": "缺口数量",
    }
    sample["coverage"] = [
        {
            "source": "SSH journal",
            "window": "2026-07-15 00:00 — 23:59（UTC+8）",
            "status": "完整",
            "note": "认证事件仅保留计数",
            "tone": "good",
        },
        {
            "source": "Fail2ban",
            "window": "2026-07-15 00:00 — 23:59（UTC+8）",
            "status": "完整",
            "note": "封禁事件仅保留计数",
            "tone": "good",
        },
        {
            "source": "Web/API access log",
            "window": "2026-07-15 00:00 — 23:59（UTC+8）",
            "status": "完整",
            "note": "请求仅按状态码聚合",
            "tone": "good",
        },
        {
            "source": "管理审计",
            "window": "2026-07-15 00:00 — 23:59（UTC+8）",
            "status": "缺失",
            "note": "需由平台审计事实单独接入",
            "tone": "warn",
        },
        {
            "source": "运行态探针",
            "window": "2026-07-15 00:00 — 23:59（UTC+8）",
            "status": "完整",
            "note": "容器状态仅保留聚合计数",
            "tone": "good",
        },
    ]
    incoming.joinpath("2026-07-15.json").write_text(
        json.dumps(sample, ensure_ascii=False), encoding="utf-8"
    )
    repository = FakeRepository()
    repository.audit = SecurityDailyAuditEvidence(
        total=3,
        events=(
            SecurityDailyAuditEvent(
                time="2026-07-15 09:00:00",
                actor="admin",
                source_ip="198.51.100.7",
                action="config_update",
            ),
            SecurityDailyAuditEvent(
                time="2026-07-15 08:00:00",
                actor="admin",
                source_ip=native_ipv6,
                action="login",
            ),
        ),
        category_counts=(("登录认证", 2), ("系统配置", 1)),
    )

    changed = await generate_security_daily_once(
        repository,
        tmp_path,
        now=datetime(2026, 7, 16, 8, 1, tzinfo=SHANGHAI),
        enabled=True,
        recipient_count=2,
    )

    assert changed == 1
    payload = repository.ingested[0]["payload"]
    assert [row["action"] for row in payload["audit"]] == ["config_update", "login"]
    assert payload["audit"][0]["tone"] == "warn"
    assert payload["audit"][0]["source_ip"] == "198.51.100.7"
    assert payload["audit"][1]["source_ip"] == (
        "2606:4700:4700:1111::/64（IPv6，脱敏）"
    )
    assert repository.audit.events[1].source_ip == native_ipv6
    audit_coverage = next(item for item in payload["coverage"] if item["source"] == "管理审计")
    assert audit_coverage["status"] == "完整"
    gap_metric = next(item for item in payload["metrics"] if item["label"] == "证据覆盖缺口")
    assert gap_metric["value"] == "0"
    assert gap_metric["tone"] == "good"
    assert "管理审计" not in payload["pending_confirmation"]
    assert "管理审计共 3 条" in payload["summary"]
    assert "登录认证 2" in payload["summary"]
    assert "系统配置 1" in payload["summary"]
