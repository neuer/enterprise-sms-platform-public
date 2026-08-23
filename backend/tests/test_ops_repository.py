from __future__ import annotations

import inspect
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from app.core.auth.accounts import SecurityPrincipal
from app.core.correlation import correlation_scope
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

    def scalars(self) -> list[object]:
        return [row["id"] for row in self.rows]


class FakeConnection:
    def __init__(self, results: list[FakeResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, Any]] = []

    async def execute(self, statement: object, params: Any = None) -> FakeResult:
        self.calls.append((str(statement), params))
        return self.results.pop(0)

    async def scalar(self, statement: object, params: Any = None) -> object:
        result = await self.execute(statement, params)
        return result.scalar


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
                        "http_status": 200,
                        "content_encoding": "identity",
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
    assert "capture_state IN ('complete','complete_too_large')" in normalized_sql
    assert "replay_eligibility IN ('automatic', 'manual')" in normalized_sql
    assert "processing_started_at IS NULL" in normalized_sql
    assert "interval '15 minutes'" in normalized_sql
    assert "RETURNING" in normalized_sql
    assert "item_count" in normalized_sql
    assert params == {"raw_id": 9}


@pytest.mark.asyncio
async def test_system_raw_replay_claim_only_allows_automatic_complete() -> None:
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
                        "http_status": 200,
                        "content_encoding": "identity",
                        "claimed": True,
                    }
                ]
            )
        ]
    )
    claim = await repo.claim_raw_for_replay(9, allow_manual=False)
    assert claim is not None and claim.claimed is True
    sql = " ".join(connection.calls[0][0].split())
    assert "capture_state IN ('complete')" in sql
    assert "replay_eligibility IN ('automatic')" in sql
    assert "complete_too_large" not in sql


@pytest.mark.asyncio
async def test_job_trigger_audit_binds_live_principal_before_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """手动触发必须显式绑定人类主体；禁止再写会触发 500 的 legacy INSERT。"""

    bound: list[dict[str, object]] = []

    async def fake_bind(connection: object, **kwargs: object) -> None:
        bound.append({"connection": connection, **kwargs})

    monkeypatch.setattr(
        "app.services.ops_repository.bind_connection_audit_subject",
        fake_bind,
    )
    repo, connection = repository([FakeResult()])
    principal = SecurityPrincipal(1, 10, "admin01", "平台部", "admin")

    with correlation_scope():
        await repo.audit_job_trigger(
            "poll_report",
            actor="admin01",
            ip="10.0.0.8",
            principal=principal,
        )

    assert bound == [
        {
            "connection": connection,
            "subject_kind": "human",
            "actor_name": "admin01",
            "account_id": 1,
            "identity_id": 10,
        }
    ]
    sql, params = connection.calls[0]
    assert "INSERT INTO audit_log" in sql
    assert "actor_subject_kind" in sql
    assert "actor_account_id" in sql
    assert "jsonb_build_object" not in sql
    assert params["action"] == "job_trigger"
    assert params["object_type"] == "job"
    assert params["object_id"] == "poll_report"
    assert params["account_id"] == 1
    assert params["identity_id"] == 10
    assert params["actor"] == "admin01"
    assert params["role"] == "admin"
    assert json.loads(str(params["after"])) == {"status": "requested"}


@pytest.mark.asyncio
async def test_job_trigger_audit_fails_closed_when_actor_does_not_match_principal() -> None:
    repo, connection = repository([])
    human = SecurityPrincipal(1, 10, "admin01", "平台部", "admin")

    with correlation_scope(), pytest.raises(RuntimeError, match="audit principal"):
        await repo.audit_job_trigger(
            "poll_report",
            actor="other-admin",
            ip="10.0.0.8",
            principal=human,
        )

    assert connection.calls == []


def test_job_trigger_audit_source_does_not_use_legacy_unattributed_insert() -> None:
    source = inspect.getsource(SqlOpsRepository.audit_job_trigger)
    assert "bind_connection_audit_subject" in source
    assert "insert_audit" in source
    assert "jsonb_build_object('status','requested')" not in source


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
async def test_human_raw_replay_audit_binds_live_principal_before_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bound: list[dict[str, object]] = []

    async def fake_bind(connection: object, **kwargs: object) -> None:
        bound.append({"connection": connection, **kwargs})

    monkeypatch.setattr(
        "app.services.ops_repository.bind_connection_audit_subject",
        fake_bind,
    )
    repo, connection = repository([FakeResult()])
    principal = SecurityPrincipal(1, 10, "admin01", "平台部", "admin")

    with correlation_scope():
        await repo.audit_raw_replay(
            9,
            source="report",
            items=1,
            actor="admin01",
            ip="10.0.0.8",
            principal=principal,
        )

    assert bound == [
        {
            "connection": connection,
            "subject_kind": "human",
            "actor_name": "admin01",
            "account_id": 1,
            "identity_id": 10,
        }
    ]
    sql, params = connection.calls[0]
    assert "INSERT INTO audit_log" in sql
    assert "actor_subject_kind" in sql
    assert "actor_account_id" in sql
    assert "CAST(CAST(:object_id AS bigint) AS text)" in sql
    assert "jsonb_build_object" not in sql
    assert params["action"] == "raw_replay"
    assert params["object_type"] == "raw_vendor_log"
    assert params["object_id"] == 9
    assert params["account_id"] == 1
    assert params["identity_id"] == 10
    assert params["actor"] == "admin01"
    assert json.loads(str(params["after"])) == {"source": "report", "items": 1}


@pytest.mark.asyncio
async def test_human_raw_replay_audit_fails_closed_when_actor_does_not_match_principal() -> None:
    repo, connection = repository([])
    human = SecurityPrincipal(1, 10, "admin01", "平台部", "admin")

    with correlation_scope(), pytest.raises(RuntimeError, match="audit principal"):
        await repo.audit_raw_replay(
            9,
            source="report",
            items=1,
            actor="other-admin",
            ip="10.0.0.8",
            principal=human,
        )

    assert connection.calls == []


@pytest.mark.asyncio
async def test_system_raw_replay_audit_rejects_human_principal() -> None:
    repo, connection = repository([])
    human = SecurityPrincipal(1, 10, "admin01", "平台部", "admin")

    with correlation_scope(), pytest.raises(RuntimeError, match="cannot bind a human"):
        await repo.audit_raw_replay(
            9,
            source="reply",
            items=2,
            actor="system-reconcile",
            ip="127.0.0.1",
            system_producer=True,
            principal=human,
        )

    assert connection.calls == []


def test_human_raw_replay_audit_source_does_not_use_legacy_unattributed_insert() -> None:
    source = inspect.getsource(SqlOpsRepository.audit_raw_replay)
    assert "bind_connection_audit_subject" in source
    assert "insert_audit" in source
    assert ":actor,'admin',CAST(:ip AS inet),'raw_replay'" not in source


@pytest.mark.asyncio
async def test_system_raw_replay_audit_binds_system_producer_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reconcile 自动重放必须走 system 主体，禁止伪造 admin 人类审计。"""

    bound: list[dict[str, object]] = []

    async def fake_bind(connection: object, *, actor_name: str, action: str) -> None:
        bound.append({"actor_name": actor_name, "action": action, "connection": connection})

    monkeypatch.setattr(
        "app.services.ops_repository.bind_connection_system_audit",
        fake_bind,
    )
    repo, connection = repository([FakeResult()])

    await repo.audit_raw_replay(
        9,
        source="reply",
        items=2,
        actor="system-reconcile",
        ip="127.0.0.1",
        system_producer=True,
    )

    assert bound == [
        {
            "actor_name": "system-reconcile",
            "action": "raw_replay",
            "connection": connection,
        }
    ]
    sql, params = connection.calls[0]
    assert "actor_subject_kind" in sql
    assert "'system'" in sql
    assert params["actor"] == "system-reconcile"
    assert params["raw_id"] == 9
    assert "ip" not in params


@pytest.mark.asyncio
async def test_has_human_raw_replay_audit_queries_any_raw_replay_audit() -> None:
    repo, connection = repository([FakeResult(scalar=1)])

    assert await repo.has_human_raw_replay_audit(9) is True
    sql, params = connection.calls[0]
    assert "action='raw_replay'" in sql
    assert "object_type='raw_vendor_log'" in sql
    assert "CAST(CAST(:raw_id AS bigint) AS text)" in sql
    assert params == {"raw_id": 9}


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
async def test_stale_raw_ids_only_include_complete_captures() -> None:
    repo, connection = repository([FakeResult(rows=[{"id": 4}])])
    assert await repo.list_stale_unprocessed_raw_ids() == [4]
    sql = " ".join(connection.calls[0][0].split())
    assert "capture_state='complete'" in sql
    assert "replay_eligibility='automatic'" in sql
    assert "processed=false" in sql


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
    assert "capture_state" in sql
    assert "parse_state" in sql
    assert "replay_eligibility" in sql


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
