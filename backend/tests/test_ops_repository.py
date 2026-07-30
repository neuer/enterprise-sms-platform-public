from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from app.core.jobtrack import JobSpec
from app.services.ops import (
    AlertQuery,
    JobRecord,
    RawLogQuery,
    UnmatchedQuery,
)
from app.services.ops_repository import SqlOpsRepository

NOW = datetime(2026, 7, 12, 8, 0, tzinfo=UTC)


class FakeResult:
    def __init__(self, rows: list[dict[str, object]] | None = None, scalar: object = None) -> None:
        self.rows = rows or []
        self.scalar = scalar

    def mappings(self) -> FakeResult:
        return self

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self.rows)

    def scalar_one(self) -> object:
        return self.scalar

    def one_or_none(self) -> dict[str, object] | None:
        assert len(self.rows) <= 1
        return self.rows[0] if self.rows else None


class FakeConnection:
    def __init__(self, results: list[FakeResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, Any]] = []

    async def execute(self, statement: object, params: Any = None) -> FakeResult:
        self.calls.append((str(statement), params))
        return self.results.pop(0)


class FakeContext:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, *_: object) -> None:
        return None


class FakeEngine:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def connect(self) -> FakeContext:
        return FakeContext(self.connection)

    def begin(self) -> FakeContext:
        return FakeContext(self.connection)

    async def dispose(self) -> None:
        return None


def repository(results: list[FakeResult]) -> tuple[SqlOpsRepository, FakeConnection]:
    connection = FakeConnection(results)
    value = SqlOpsRepository()
    value._engine = lambda: FakeEngine(connection)  # type: ignore[method-assign]
    return value, connection


@pytest.mark.asyncio
async def test_raw_replay_claim_is_atomic_and_reclaims_only_stale_leases() -> None:
    repo, connection = repository(
        [
            FakeResult(
                [
                    {
                        "id": 9,
                        "source": "report",
                        "payload_enc": b"ciphertext",
                        "payload_sha256": "a" * 64,
                        "key_version": 2,
                        "processed": False,
                        "claimed": True,
                    }
                ]
            )
        ]
    )

    assert hasattr(repo, "claim_raw_for_replay")
    claim = await repo.claim_raw_for_replay(9)

    assert claim is not None and claim.claimed is True
    assert claim.record.id == 9
    sql, params = connection.calls[0]
    normalized_sql = " ".join(sql.split())
    assert "UPDATE raw_vendor_log" in normalized_sql
    assert "processing_started_at=now()" in normalized_sql
    assert "processed=false" in normalized_sql
    assert "processing_started_at IS NULL" in normalized_sql
    assert "interval '15 minutes'" in normalized_sql
    assert "RETURNING" in normalized_sql
    assert params == {"raw_id": 9}


@pytest.mark.asyncio
async def test_job_list_casts_datetime_parameters_for_postgres_interval_math() -> None:
    repo, connection = repository(
        [
            FakeResult(
                [
                    {
                        "job_name": "poll_report",
                        "last_run_at": NOW,
                        "last_status": "success",
                        "last_duration_ms": 10,
                        "last_items": 1,
                        "success_rate_24h": 1.0,
                        "stalled": False,
                    }
                ]
            )
        ]
    )

    result = await repo.list_jobs((JobSpec("poll_report", 60),), now=NOW)

    assert isinstance(result[0], JobRecord)
    sql = connection.calls[0][0]
    assert "CAST(:day_start AS timestamptz)" in sql
    assert "CAST(:now AS timestamptz) -" in sql


@pytest.mark.asyncio
async def test_raw_replay_audit_binds_numeric_id_without_asyncpg_text_coercion() -> None:
    repo, connection = repository([FakeResult()])

    await repo.audit_raw_replay(
        9,
        source="report",
        items=1,
        actor="admin01",
        ip="10.0.0.8",
    )

    sql, params = connection.calls[0]
    assert "CAST(CAST(:raw_id AS bigint) AS text)" in sql
    assert params["raw_id"] == 9


@pytest.mark.asyncio
async def test_alert_list_is_filtered_and_paginated_with_safe_detail() -> None:
    repo, connection = repository(
        [
            FakeResult(scalar=1),
            FakeResult(
                [
                    {
                        "id": 7,
                        "alert_type": "job_failed",
                        "level": "crit",
                        "title": "任务失败",
                        "detail": {"job_name": "poll_report"},
                        "channels": "log-sink",
                        "created_at": NOW,
                    }
                ]
            ),
        ]
    )
    page = await repo.list_alerts(AlertQuery("job_failed", "crit", None, None, 2, 20))

    assert page.items[0].detail == {"job_name": "poll_report"}
    sql, params = connection.calls[1]
    assert "ORDER BY created_at DESC,id DESC" in sql
    assert params["alert_type"] == "job_failed" and params["offset"] == 20


@pytest.mark.asyncio
async def test_raw_list_never_selects_ciphertext_or_payload_hash() -> None:
    repo, connection = repository(
        [
            FakeResult(scalar=1),
            FakeResult(
                [
                    {
                        "id": 9,
                        "source": "report",
                        "item_count": 3,
                        "custom_id_count": 2,
                        "processed": False,
                        "error": "ValueError: report fields are incomplete",
                        "fetched_at": NOW,
                    }
                ]
            ),
        ]
    )
    page = await repo.list_raw_logs(RawLogQuery("report", False, 1, 20))

    assert page.items[0].custom_id_count == 2
    sql = connection.calls[1][0]
    assert "payload_enc" not in sql and "payload_sha256" not in sql
    assert "cardinality(custom_ids) custom_id_count" in sql


@pytest.mark.asyncio
async def test_uncertain_list_uses_exact_timestamp_without_state_mutation() -> None:
    repo, connection = repository(
        [
            FakeResult(scalar=1),
            FakeResult(
                [
                    {
                        "chunk_id": 3,
                        "batch_no": "BATCH-1",
                        "custom_id": "CUSTOM-1",
                        "phone_count": 50,
                        "vendor_code": None,
                        "uncertain_since": NOW,
                        "age_seconds": 3600,
                    }
                ]
            ),
        ]
    )
    page = await repo.list_uncertain(1, 20)

    assert page.items[0].age_seconds == 3600
    sql = connection.calls[1][0]
    assert "COALESCE(c.uncertain_since,b.created_at)" in sql
    assert "UPDATE" not in sql and "status='uncertain'" in sql


@pytest.mark.asyncio
async def test_unmatched_list_returns_mask_only_and_binds_hmac_candidates() -> None:
    repo, connection = repository(
        [
            FakeResult(scalar=1),
            FakeResult(
                [
                    {
                        "id": 5,
                        "vendor_task_id": "vendor-1",
                        "custom_id": "legacy-1",
                        "phone_mask": "138****8000",
                        "report_status": 1,
                        "report_desc": "DELIVRD",
                        "report_time": NOW,
                        "created_at": NOW,
                    }
                ]
            ),
        ]
    )
    query = UnmatchedQuery(("a" * 64,), None, None, 1, 20)
    page = await repo.list_unmatched(query)

    assert page.items[0].phone_mask == "138****8000"
    sql, params = connection.calls[1]
    assert (
        "phone_enc" not in sql and "phone_hmac" not in sql.split("SELECT", 1)[1].split("FROM", 1)[0]
    )
    assert "phone_hmac=ANY(CAST(:phone_hmacs AS char(64)[]))" in sql
    assert params["phone_hmacs"] == ["a" * 64]
