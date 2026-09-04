"""PR 快速性能门禁与候选容量/故障矩阵合同。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.services.send_admission import SendAdmissionFacts, decide
from app.services.usage_ledger import _ensure_frequency_subjects_many

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(SCRIPTS))

from perf_capacity import (  # noqa: E402
    CapacityGateFailure,
    CapacityThresholds,
    assert_thresholds,
    build_report,
    require_real_postgres,
    scenario_recipient_count,
    validate_report_payload,
)
from perf_fault_matrix import FAULT_CASES, assert_matrix  # noqa: E402
from test_frequency_batch_sql import RecordingConnection, decision_item  # noqa: E402


def _metrics(recipient_count: int, *, sql_count: int = 8, p99_ms: float = 900) -> dict[str, object]:
    return {
        "recipient_count": recipient_count,
        "accepted_recipients_per_s": 120.0,
        "segments_per_s": 120.0,
        "p50_ms": 200.0,
        "p95_ms": 400.0,
        "p99_ms": p99_ms,
        "sql_count": sql_count,
        "lock_wait_ms": 5.0,
        "pool_occupancy": 0.2,
        "wal_bytes": 1024,
        "redis_ops": 30,
        "worker_rss_bytes": 80_000_000,
        "outbox_oldest_age_s": 1.2,
        "converge_s": 12.0,
    }


@pytest.mark.asyncio
async def test_pr_gate_bounds_frequency_sql_for_new_and_alias_subjects() -> None:
    connection = RecordingConnection()
    items = [decision_item(f"n{index}", versions=3) for index in range(1000)]
    await _ensure_frequency_subjects_many(connection, items)
    assert len(connection.statements) <= 8
    assert all("FOR item IN" not in sql for sql in connection.statements)


def test_capacity_report_requires_full_metrics_and_forbids_phones() -> None:
    report = build_report(
        scenario="recipients_1000",
        metrics=_metrics(1000),
        commit="c" * 40,
    )
    payload = report.as_json()
    validate_report_payload(payload)
    assert payload["commit"] == "c" * 40
    assert payload["recipient_count"] == 1000
    with pytest.raises(CapacityGateFailure, match="phone"):
        validate_report_payload({**payload, "note": "13800138000"})


def test_capacity_thresholds_fail_the_candidate_gate() -> None:
    report = build_report(
        scenario="recipients_1000",
        metrics=_metrics(1000, sql_count=80, p99_ms=90_000),
        commit="c" * 40,
    )
    with pytest.raises(CapacityGateFailure, match="P99"):
        assert_thresholds(report, CapacityThresholds())


def test_10000_recipients_requires_real_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OUTBOX_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("PERF_ALLOW_10K", raising=False)
    assert scenario_recipient_count("recipients_10000") == 10_000
    with pytest.raises(CapacityGateFailure, match="OUTBOX_POSTGRES_DSN"):
        require_real_postgres(10_000)
    monkeypatch.setenv("OUTBOX_POSTGRES_DSN", "postgresql://sms@127.0.0.1/sms")
    require_real_postgres(10_000)


def test_fault_matrix_forbids_automatic_resend() -> None:
    assert_matrix()
    assert {item.name for item in FAULT_CASES} >= {
        "vendor_success_response_lost",
        "vendor_success_mark_submitted_failed",
        "redis_flush_projection_rebuild",
        "worker_broker_backlog_drain",
    }
    assert all(item.auto_resend is False for item in FAULT_CASES)


def test_admission_keeps_small_requests_from_unbounded_starvation() -> None:
    facts = SendAdmissionFacts(
        outbox_active=250,
        outbox_oldest_age_s=10,
        outbox_dead=0,
        uncertain_overdue=0,
        callback_dead=0,
        realtime_paused=False,
        bulk_paused=False,
        vendor_failures=0,
    )
    allowed = decide(
        facts,
        previous_state="open",
        category="verify",
        recipient_count=10,
    )
    denied = decide(
        facts,
        previous_state="open",
        category="market",
        recipient_count=500,
    )
    assert allowed.allowed is True
    assert denied.allowed is False


def test_performance_docs_bind_layered_gates() -> None:
    document = (ROOT / "docs/PERFORMANCE.md").read_text(encoding="utf-8")
    for token in (
        "perf_capacity.py",
        "perf_fault_matrix.py",
        "recipients_10000",
        "frequency_hmac_alias_merge",
        "OUTBOX_POSTGRES_DSN",
        "P99",
        "sql_count",
        "worker_rss_bytes",
        "outbox_oldest_age",
    ):
        assert token in document
    assert "日常 CI" in document
