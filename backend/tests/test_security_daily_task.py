from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from app.tasks.security_daily import generate_security_daily_once

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "deploy" / "templates" / "security_daily_report.sample.json"
SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")


class FakeRepository:
    def __init__(self) -> None:
        self.ingested: list[dict[str, Any]] = []
        self.unavailable: list[tuple[str, str]] = []

    async def ingest_payload(
        self, payload: dict[str, Any], *, recipient_count: int
    ) -> bool:
        self.ingested.append({"payload": payload, "recipient_count": recipient_count})
        return True

    async def mark_unavailable(
        self,
        report_date: Any,
        *,
        period_start: Any,
        period_end: Any,
        reason: str,
    ) -> bool:
        if (report_date.isoformat(), reason) in self.unavailable:
            return False
        self.unavailable.append((report_date.isoformat(), reason))
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
    assert repository.unavailable == []
