from __future__ import annotations

import base64
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

import app.services.pipeline_repository as pipeline_repository_module
import app.services.reconcile_repository as reconcile_repository_module
import app.services.report_repository as report_repository_module
import app.services.uncertain_repository as uncertain_repository_module
from app.core.auth.accounts import SecurityPrincipal
from app.core.correlation import correlation_scope
from app.services.approval_repository import SqlApprovalRepository, record_pending_approval_alert
from app.services.batch_query import BatchAccessScope, BatchNotFound, BatchQueryService
from app.services.blacklist import BlacklistEntry, BlacklistUpsertResult
from app.services.blacklist_repository import SqlBlacklistRepository
from app.services.crypto import CryptoService, EncryptionContext
from app.services.idempotency import IdempotencyScope
from app.services.import_repository import (
    ImportStateConflict,
    SqlImportRepository,
    consume_import_reservation,
)
from app.services.imports import ImportPhone, ImportResult, RemovedPhone
from app.services.pipeline_repository import SqlPipelineStore, SqlTemplateRenderer
from app.services.reconcile_repository import SqlRecoveryRepository
from app.services.report_ingest import ProtectedReport, ReportApplyResult
from app.services.report_repository import SqlReportRepository
from app.services.resend import SqlResendRepository
from app.services.scheduling_repository import SqlSchedulingRepository
from app.services.sensitive_repository import SqlSensitiveWordRepository
from app.services.template_repository import SqlTemplateRepository
from app.services.uncertain import UncertainChunk
from app.services.uncertain_repository import SqlUncertainRepository

OPERATOR = SecurityPrincipal(1, 10, "operator01", "业务一部", "operator")


def content_crypto() -> CryptoService:
    key = base64.b64encode(b"c" * 32).decode()
    return CryptoService.from_secret_values(key, key)


def batch_content(batch_no: str, content: str) -> bytes:
    return content_crypto().encrypt_bound_packed_text(
        content,
        EncryptionContext(
            domain="sms-display-content",
            table="sms_batch",
            column="display_content_enc",
            object_id=batch_no,
        ),
    )


class FakeResult:
    def __init__(
        self,
        *,
        rows: list[dict[str, object]] | None = None,
        scalar: object = None,
        scalars: Iterable[object] = (),
        rowcount: int = 0,
    ) -> None:
        self.rows = rows or []
        self.scalar = scalar
        self.scalar_values = list(scalars)
        self.rowcount = rowcount

    def mappings(self) -> FakeResult:
        return self

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self.rows)

    def first(self) -> dict[str, object] | None:
        return self.rows[0] if self.rows else None

    def one(self) -> dict[str, object]:
        assert len(self.rows) == 1
        return self.rows[0]

    def one_or_none(self) -> dict[str, object] | None:
        assert len(self.rows) <= 1
        return self.rows[0] if self.rows else None

    def scalar_one(self) -> object:
        return self.scalar

    def scalar_one_or_none(self) -> object:
        return self.scalar

    def scalars(self) -> list[object]:
        return self.scalar_values


class FakeConnection:
    def __init__(
        self,
        results: list[FakeResult],
        *,
        scalar_values: list[object] | None = None,
    ) -> None:
        self.results = results
        self.scalar_values = scalar_values or []
        self.calls: list[tuple[str, object]] = []

    async def execute(self, statement: object, params: object = None) -> FakeResult:
        self.calls.append((str(statement), params))
        return self.results.pop(0)

    async def scalar(self, statement: object, params: object = None) -> object:
        self.calls.append((str(statement), params))
        return self.scalar_values.pop(0)


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
        self.disposed = False

    def connect(self) -> FakeContext:
        return FakeContext(self.connection)

    def begin(self) -> FakeContext:
        return FakeContext(self.connection)

    async def dispose(self) -> None:
        self.disposed = True


def bind_engine(
    monkeypatch: pytest.MonkeyPatch,
    repository: object,
    connection: FakeConnection,
) -> FakeEngine:
    engine = FakeEngine(connection)
    monkeypatch.setattr(repository, "_engine", lambda: engine)
    return engine


def protected_report() -> ProtectedReport:
    return ProtectedReport(
        event_key="c" * 64,
        vendor_task_id="d" * 64,
        custom_id="e" * 64,
        match_custom_id="custom-1",
        phone_enc=b"ciphertext",
        phone_hmac="a" * 64,
        phone_mask="138****8000",
        key_version=1,
        report_status=1,
        message_status="delivered",
        report_desc="DELIVRD",
        report_time=datetime(2026, 7, 11, 8, 0, tzinfo=UTC),
        phone_hmacs=("b" * 64, "a" * 64),
    )


@pytest.mark.asyncio
async def test_template_renderer_binds_authoritative_department(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(
        [
            FakeResult(
                rows=[
                    {
                        "content_enc": content_crypto().encrypt_bound_packed_text(
                            "尊敬的{1}",
                            EncryptionContext(
                                domain="sms-template-content",
                                table="sms_template",
                                column="content_enc",
                                object_id="17",
                            ),
                        ),
                        "var_specs": [{"pos": 1, "max_len": 10}],
                    }
                ]
            )
        ]
    )
    engine = FakeEngine(connection)
    monkeypatch.setattr(pipeline_repository_module, "database_engine", lambda _url: engine)
    renderer = SqlTemplateRenderer(
        cast(Any, SimpleNamespace(database_url="postgresql+asyncpg://unused")),
        content_crypto(),
    )

    assert await renderer.render(17, ["用户"], "平台技术部") == "尊敬的用户"
    sql, params = connection.calls[0]
    assert "dept=:dept" in sql
    assert params == {"template_id": 17, "dept": "平台技术部"}


@pytest.mark.asyncio
async def test_template_repository_update_persists_only_object_bound_ciphertext(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crypto = content_crypto()
    encrypted = crypto.encrypt_bound_packed_text(
        "验证码{1}",
        EncryptionContext(
            domain="sms-template-content",
            table="sms_template",
            column="content_enc",
            object_id="17",
        ),
    )
    encrypted_name = crypto.encrypt_bound_packed_text(
        "验证码",
        EncryptionContext(
            domain="sms-template-name",
            table="sms_template",
            column="name_enc",
            object_id="17",
        ),
    )
    outbox_id = UUID("20000000-0000-4000-8000-000000000017")
    connection = FakeConnection(
        [
            FakeResult(scalar=17),
            FakeResult(),
            FakeResult(
                rows=[
                    {
                        "id": outbox_id,
                        "event_type": "template.bind",
                        "aggregate_type": "sms_template",
                        "aggregate_id": "17",
                        "task_name": "app.tasks.bind_template",
                        "queue": "realtime",
                        "args": [17],
                        "max_attempts": 1,
                        "correlation_id": None,
                    }
                ]
            ),
            FakeResult(
                rows=[
                    {
                        "id": 17,
                        "name_enc": encrypted_name,
                        "content_enc": encrypted,
                        "var_specs": [{"pos": 1, "max_len": 6}],
                        "dept": "平台技术部",
                        "vendor_template_id": None,
                        "vendor_state": "pending",
                        "vendor_reject_reason": None,
                    }
                ]
            ),
        ]
    )
    repository = SqlTemplateRepository(
        cast(Any, SimpleNamespace(database_url="postgresql+asyncpg://unused")),
        crypto,
    )
    bind_engine(monkeypatch, repository, connection)

    record = await repository.update(
        17,
        name="验证码",
        content="验证码{1}",
        var_specs=[{"pos": 1, "max_len": 6}],
        actor="operator01",
    )

    assert record is not None and record.content == "验证码{1}"
    update_sql, update_params = connection.calls[0]
    assert "content='[encrypted]'" in update_sql
    assert "验证码{1}" not in str(update_params)
    assert "验证码" not in str(update_params)
    assert isinstance(update_params, dict)
    assert crypto.decrypt_bound_packed_text(
        update_params["content_enc"],
        EncryptionContext(
            domain="sms-template-content",
            table="sms_template",
            column="content_enc",
            object_id="17",
        ),
    ) == "验证码{1}"
    assert crypto.decrypt_bound_packed_text(
        update_params["name_enc"],
        EncryptionContext(
            domain="sms-template-name",
            table="sms_template",
            column="name_enc",
            object_id="17",
        ),
    ) == "验证码"


@pytest.mark.asyncio
async def test_pending_approval_notification_is_log_sink_without_pii() -> None:
    connection = FakeConnection([FakeResult()])
    await record_pending_approval_alert(
        connection,
        batch_no="batch-1",
        dept="业务一部",
        category="market",
        total=50,
    )
    sql, params = connection.calls[0]
    assert "alert_log" in sql
    assert params["channels"] == "log-sink"  # type: ignore[index]
    assert "phone" not in str(params).lower()


@pytest.mark.asyncio
async def test_approval_repository_returns_batch_status_for_enqueue_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SqlApprovalRepository(crypto=content_crypto())
    connection = FakeConnection(
        [
            FakeResult(
                rows=[
                    {
                        "approval_id": 3,
                        "batch_no": "batch-1 ",
                        "applicant": "operator01",
                        "applicant_account_id": 1,
                        "applicant_identity_id": 10,
                        "app_id": 7,
                        "dept": "平台部",
                        "quota_date": "20260711",
                        "quota_cost": 20,
                        "category": "market",
                        "status": "approved",
                        "batch_status": "scheduled",
                    }
                ]
            )
        ]
    )
    bind_engine(monkeypatch, repository, connection)

    approval = await repository.get(3)

    assert approval is not None
    assert approval.batch_status == "scheduled"
    assert "b.status batch_status" in connection.calls[0][0]


@pytest.mark.asyncio
async def test_approval_list_returns_historical_billing_schedule_and_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduled_at = datetime(2026, 7, 21, 1, 0, tzinfo=UTC)
    repository = SqlApprovalRepository(crypto=content_crypto())
    connection = FakeConnection(
        [
            FakeResult(
                rows=[
                    {
                        "id": 9,
                        "batch_no": "batch-9",
                        "category": "market",
                        "applicant": "operator01",
                        "dept": "市场部",
                        "total": 60,
                        "segments": 2,
                        "estimated_segments": 120,
                        "scheduled_at": scheduled_at,
                        "trigger_threshold": 50,
                        "trigger_threshold_source": "snapshot",
                        "display_content_enc": batch_content("batch-9", "活动回T退订"),
                        "status": "pending",
                        "approver": None,
                        "reason": None,
                        "expires_at": scheduled_at,
                        "decided_at": None,
                        "created_at": scheduled_at,
                    }
                ]
            ),
        ],
        scalar_values=[1],
    )
    bind_engine(monkeypatch, repository, connection)

    result = await repository.list_page(status="pending", dept=None, page=1)

    assert result["items"][0]["estimated_segments"] == 120  # type: ignore[index]
    assert result["items"][0]["expires_at"] == scheduled_at  # type: ignore[index]
    assert result["items"][0]["decided_at"] is None  # type: ignore[index]
    select_sql = connection.calls[1][0]
    assert "b.segments" in select_sql
    assert "b.quota_cost estimated_segments" in select_sql
    assert "b.scheduled_at" in select_sql
    assert "p.trigger_threshold" in select_sql
    assert "p.trigger_threshold_source" in select_sql
    assert "p.expires_at" in select_sql
    assert "p.decided_at" in select_sql


@pytest.mark.asyncio
async def test_import_repository_persists_reserves_and_scopes_removed_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = SqlImportRepository()
    repository.settings.import_storage_dir = tmp_path
    expires_at = datetime(2026, 7, 12, 8, 0, tzinfo=UTC)
    result = ImportResult(
        [ImportPhone(b"cipher", "a" * 64, "138****8000", 1, 2)],
        [RemovedPhone("139****9000", 3, "blacklist")],
    )
    connection = FakeConnection(
        [
            FakeResult(
                rows=[
                    {
                        "id": 9,
                        "import_id": "11111111-1111-1111-1111-111111111111",
                        "expires_at": expires_at,
                    }
                ]
            ),
            FakeResult(),
            FakeResult(),
            FakeResult(),
        ]
    )
    engine = bind_engine(monkeypatch, repository, connection)
    stored = await repository.persist(
        result,
        principal=OPERATOR,
        filename="../phones.csv",
        expire_hours=6,
    )
    assert stored.valid == 1 and stored.blacklisted == 1
    assert stored.invalid_file == f"{stored.import_id}.csv"
    assert stored.invalid_file is not None
    assert (
        (tmp_path / stored.invalid_file)
        .read_text(encoding="utf-8")
        .startswith("phone_mask,source_row,reason")
    )
    assert connection.calls[0][1]["filename"] == "upload.csv"  # type: ignore[index]
    assert connection.calls[0][1]["expire_hours"] == 6  # type: ignore[index]
    assert "make_interval" in connection.calls[0][0]
    assert "13800138000" not in str(connection.calls)
    assert engine.disposed

    phone_row = {
        "phone_enc": b"cipher",
        "phone_hmac": "a" * 64,
        "phone_mask": "138****8000",
        "key_version": 1,
        "source_row": 2,
    }
    reservation_expires_at = datetime(2026, 7, 12, 8, 5, tzinfo=UTC)
    connection = FakeConnection(
        [
            FakeResult(
                rows=[
                    {
                        "id": 9,
                        "state": "ready",
                        "reservation_id": None,
                        "reservation_expires_at": None,
                        "consumed_batch_no": None,
                    }
                ]
            ),
            FakeResult(
                rows=[
                    {
                        "id": 9,
                        "reservation_expires_at": reservation_expires_at,
                    }
                ]
            ),
            FakeResult(rows=[phone_row]),
        ]
    )
    bind_engine(monkeypatch, repository, connection)
    reservation = await repository.reserve(stored.import_id, principal=OPERATOR)
    assert reservation.phones == tuple(result.valid)
    assert reservation.expires_at == reservation_expires_at
    assert "FOR UPDATE OF t" in connection.calls[0][0]
    assert "state='reserved'" in connection.calls[1][0]

    connection = FakeConnection([FakeResult(scalar=stored.invalid_file)])
    bind_engine(monkeypatch, repository, connection)
    assert await repository.invalid_file(stored.import_id, principal=OPERATOR) == (
        tmp_path / stored.invalid_file
    )

    connection = FakeConnection([FakeResult(scalar="../outside.csv")])
    bind_engine(monkeypatch, repository, connection)
    assert await repository.invalid_file(stored.import_id, principal=OPERATOR) is None

    connection = FakeConnection([FakeResult(rows=[])])
    bind_engine(monkeypatch, repository, connection)
    with pytest.raises(ImportStateConflict):
        await repository.reserve(stored.import_id, principal=OPERATOR)


@pytest.mark.asyncio
async def test_import_reservation_is_consumed_inside_batch_transaction() -> None:
    reservation_id = UUID("22222222-2222-4222-8222-222222222222")
    connection = FakeConnection([FakeResult(scalar=9)])

    await consume_import_reservation(
        cast(Any, connection),
        reservation_id=reservation_id,
        batch_id=77,
        principal=OPERATOR,
    )

    sql, params = connection.calls[0]
    assert "state='consumed'" in sql
    assert "reservation_expires_at>now()" in sql
    assert params == {
        "reservation_id": str(reservation_id),
        "batch_id": 77,
        "actor_account_id": OPERATOR.account_id,
    }

    with pytest.raises(ImportStateConflict):
        await consume_import_reservation(
            cast(Any, FakeConnection([FakeResult(scalar=None)])),
            reservation_id=reservation_id,
            batch_id=77,
            principal=OPERATOR,
        )


@pytest.mark.asyncio
async def test_reschedule_uses_runtime_approval_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SqlSchedulingRepository()
    connection = FakeConnection(
        [
            FakeResult(rows=[{"id": 9, "channel": "web", "had_approval": True}]),
            FakeResult(),
            FakeResult(),
            FakeResult(),
            FakeResult(),
        ]
    )
    bind_engine(monkeypatch, repository, connection)

    changed = await repository.reschedule(
        "batch-1",
        BatchAccessScope(dept="平台部"),
        datetime(2026, 7, 13, 8, tzinfo=UTC),
        approval_expire_hours=6,
    )

    assert changed is True
    expiry_sql, expiry_params = connection.calls[2]
    assert "GREATEST" in expiry_sql
    assert expiry_params["id"] == 9  # type: ignore[index]
    approval_sql, approval_params = connection.calls[3]
    assert "make_interval" in approval_sql
    assert approval_params["expire_hours"] == 6  # type: ignore[index]


@pytest.mark.asyncio
async def test_recovery_repository_selects_only_recoverable_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(
        [
            FakeResult(rowcount=1),
            FakeResult(rows=[{"batch_no": "batch-1", "category": "notice"}]),
            FakeResult(rows=[{"batch_no": "batch-2", "category": "market", "chunk_id": 8}]),
        ]
    )
    engine = FakeEngine(connection)
    monkeypatch.setattr(
        reconcile_repository_module,
        "database_engine",
        lambda *_args, **_kwargs: engine,
    )
    settings = cast(Any, SimpleNamespace(database_url="postgresql+asyncpg://ignored"))
    work = await SqlRecoveryRepository(settings).stalled()
    assert [(item.kind, item.batch_no, item.chunk_id) for item in work] == [
        ("batch", "batch-1", None),
        ("chunk", "batch-2", 8),
    ]
    recovery_sql = connection.calls[0][0]
    assert "UPDATE sms_chunk" in recovery_sql
    assert "vendor_test_send_attempt" in recovery_sql
    assert "status='reserved'" in recovery_sql
    assert "status='uncertain'" in recovery_sql
    assert "in_flight_segments" in recovery_sql
    assert "uncertain_segments" in recovery_sql
    assert "status='submitting'" in recovery_sql
    assert "status='uncertain'" in recovery_sql
    assert "uncertain_since=COALESCE(c.submitting_since,now())" in recovery_sql
    assert "submitting_since=NULL" in recovery_sql
    assert "c.submitting_since<now()-interval '5 minutes'" in recovery_sql
    assert "b.status IN ('queued','sending')" in recovery_sql
    assert "b.updated_at" not in recovery_sql
    assert "5 minutes" in recovery_sql
    enqueue_sql = connection.calls[2][0]
    assert "c.status='pending'" in enqueue_sql
    assert "c.status='retrying'" in enqueue_sql
    assert "retry_not_before<=now()" in enqueue_sql
    assert "submitting" not in enqueue_sql
    assert "submitted" not in enqueue_sql
    assert "uncertain" not in enqueue_sql
    assert engine.disposed


@pytest.mark.asyncio
async def test_blacklist_repository_persists_hmac_rows_and_count_only_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SqlBlacklistRepository()
    entry = BlacklistEntry("a" * 64, b"cipher", "138****8000", 1, "import", "投诉")
    connection = FakeConnection(
        [
            FakeResult(rows=[]),
            FakeResult(),
            FakeResult(),
            FakeResult(),
            FakeResult(rows=[{"database_user": "sms_accept", "txid": 7}]),
            FakeResult(),
            FakeResult(),
        ]
    )
    bind_engine(monkeypatch, repository, connection)
    with correlation_scope(UUID("d8c138c4-5b7e-4b06-afa9-30bde2e6b7f0")):
        outcome = await repository.upsert_many(
            [entry], principal=OPERATOR, ip="10.0.0.8", source="import"
        )
    assert (outcome.added, outcome.updated) == (1, 0)
    audit_params = connection.calls[-1][1]
    # 审计只允许数量/来源；禁止用短号段做子串检查，UUID/txid 可巧合含 "138"
    assert audit_params["after"] == '{"count": 1, "source": "import"}'
    assert audit_params["before"] is None
    for token in ("138****8000", "a" * 64, "cipher"):
        assert token not in str(audit_params)

    connection = FakeConnection(
        [
            FakeResult(scalar=1),
            FakeResult(rows=[{"database_user": "sms_accept", "txid": 8}]),
            FakeResult(),
            FakeResult(),
        ]
    )
    bind_engine(monkeypatch, repository, connection)
    assert (
        await repository.delete(
            "a" * 64,
            principal=OPERATOR,
            ip="10.0.0.8",
        )
        is True
    )

    row = {
        "phone_hmac": "a" * 64,
        "phone_enc": b"cipher",
        "phone_mask": "138****8000",
        "key_version": 1,
        "source": "import",
        "remark": "投诉",
        "created_at": datetime(2026, 7, 11, 8, 0, tzinfo=UTC),
    }
    connection = FakeConnection([FakeResult(rows=[row])], scalar_values=[1])
    bind_engine(monkeypatch, repository, connection)
    page = await repository.list_page(source=None, keyword="8000", page=1, size=20)
    assert page.total == 1
    assert page.items[0].phone_mask == "138****8000"
    count_sql, count_params = connection.calls[0]
    assert "count(*)" in count_sql
    assert count_params["keyword"] == "%8000%"  # type: ignore[index]

    connection = FakeConnection([FakeResult(scalars=["a" * 64 + " "])])
    bind_engine(monkeypatch, repository, connection)
    assert await repository.all_hmacs() == {"a" * 64}


@pytest.mark.asyncio
async def test_blacklist_rotation_renames_canonical_digest_and_replaces_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SqlBlacklistRepository()
    entry = BlacklistEntry(
        "b" * 64,
        b"cipher-v2",
        "138****8000",
        2,
        "manual",
        None,
        hmac_candidates=((1, "a" * 64), (2, "b" * 64)),
    )
    connection = FakeConnection(
        [
            FakeResult(
                rows=[
                    {
                        "hmac_digest": "a" * 64,
                        "blacklist_digest": "a" * 64,
                    }
                ]
            ),
            FakeResult(),
            FakeResult(),
            FakeResult(),
            FakeResult(rows=[{"database_user": "sms_accept", "txid": 9}]),
            FakeResult(),
            FakeResult(),
        ]
    )
    bind_engine(monkeypatch, repository, connection)

    outcome = await repository.upsert_many(
        [entry],
        principal=OPERATOR,
        ip="10.0.0.8",
        source="manual",
    )

    assert outcome == BlacklistUpsertResult(added=0, updated=1)
    update_sql, update_params = connection.calls[1]
    assert "UPDATE blacklist SET" in update_sql
    assert update_params[0]["current"] == "a" * 64  # type: ignore[index]
    assert update_params[0]["canonical"] == "b" * 64  # type: ignore[index]
    alias_call = connection.calls[3]
    assert "INSERT INTO blacklist_hmac_alias" in alias_call[0]
    assert {row["digest"] for row in alias_call[1]} == {"a" * 64, "b" * 64}  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_sensitive_word_repository_paginates_and_reports_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SqlSensitiveWordRepository()
    row = {"id": 7, "word": "诈骗", "created_at": datetime(2026, 7, 11, 8, 0, tzinfo=UTC)}
    connection = FakeConnection([FakeResult(rows=[row])], scalar_values=[1])
    bind_engine(monkeypatch, repository, connection)
    page = await repository.list_page(keyword="诈", page=1, size=20)
    assert page.total == 1
    assert page.items[0].word == "诈骗"
    count_sql, count_params = connection.calls[0]
    assert "count(*)" in count_sql
    assert count_params["keyword"] == "%诈%"  # type: ignore[index]

    connection = FakeConnection([FakeResult(rows=[row]), FakeResult(), FakeResult()])
    bind_engine(monkeypatch, repository, connection)
    result = await repository.add_many(["诈骗", "赌博"], actor="admin01")
    assert [item.word for item in result.created] == ["诈骗"]
    assert result.skipped == 1
    audit_params = connection.calls[2][1]
    assert "诈骗" not in str(audit_params)
    assert '"count": 1' in audit_params["after"]  # type: ignore[index]


@pytest.mark.asyncio
async def test_report_repository_commits_raw_then_updates_matched_and_unmatched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SqlReportRepository()

    connection = FakeConnection([FakeResult(scalar=None)])
    engine = bind_engine(monkeypatch, repository, connection)
    assert await repository.report_timeout_hours() == 48
    assert engine.disposed

    connection = FakeConnection([FakeResult(scalar=17)])
    bind_engine(monkeypatch, repository, connection)
    assert await repository.persist_raw(payload_enc=b"raw") == 17
    raw_insert_sql = connection.calls[0][0]
    assert "raw_vendor_log" in raw_insert_sql
    assert "processing_started_at" in raw_insert_sql
    assert "processing_lease_id" in raw_insert_sql
    assert "processing_lease_epoch" in raw_insert_sql
    assert "capture_state" in raw_insert_sql
    assert "now()" in raw_insert_sql

    report = protected_report()
    callback_events: list[tuple[str, object]] = []

    async def batch_callback(
        _connection: object,
        batch_id: int,
        **_values: object,
    ) -> None:
        callback_events.append(("batch", batch_id))

    async def message_callback(
        _connection: object,
        *,
        batch_id: int,
        message_id: int,
        created_at: object,
        event_key: str,
        **_snapshot: object,
    ) -> None:
        callback_events.append(("message", (batch_id, message_id, created_at)))

    monkeypatch.setattr(report_repository_module, "enqueue_batch_finished", batch_callback)
    monkeypatch.setattr(report_repository_module, "enqueue_message_report", message_callback)
    connection = FakeConnection(
        [
            FakeResult(),
            FakeResult(
                rows=[
                    {
                        "id": 8,
                        "created_at": report.report_time,
                        "batch_id": 3,
                    }
                ]
            ),
            FakeResult(),
            FakeResult(
                rows=[
                    {
                        "id": 8,
                        "created_at": report.report_time,
                        "batch_id": 3,
                    }
                ]
            ),
            FakeResult(scalar="c" * 64),
            FakeResult(scalar=8),
            FakeResult(),
            FakeResult(),
            FakeResult(),
        ]
    )
    bind_engine(monkeypatch, repository, connection)
    assert await repository.apply_report(17, report) == ReportApplyResult(3, True)
    assert "ON CONFLICT(event_key) DO NOTHING" in connection.calls[0][0]
    assert "m.phone_hmac=ANY(CAST(:phone_hmacs AS char(64)[]))" in connection.calls[1][0]
    assert connection.calls[1][1]["phone_hmacs"] == ["b" * 64, "a" * 64]  # type: ignore[index]
    assert "sms_batch" in connection.calls[2][0]
    assert "FOR UPDATE" in connection.calls[2][0]
    assert "FOR UPDATE OF m" in connection.calls[3][0]
    assert "report_event_projection" in connection.calls[4][0]
    assert connection.calls[5][1]["status"] == "delivered"  # type: ignore[index]
    assert "report_event_key" in connection.calls[5][0]
    assert "m.report_status IS DISTINCT FROM 1" in connection.calls[5][0]
    assert "m.report_status = 1" not in connection.calls[5][0]
    assert "WHEN CAST(:report_status AS smallint)=1 THEN 4" in connection.calls[5][0]
    # 应用成功后把消息归属日标脏，供窗口外统计补算（#342）。
    assert "stat_dirty_date" in connection.calls[7][0]
    assert "ON CONFLICT(stat_date) DO NOTHING" in connection.calls[7][0]
    assert callback_events == [
        ("batch", 3),
        ("message", (3, 8, report.report_time)),
    ]

    connection = FakeConnection([FakeResult(), FakeResult()])
    bind_engine(monkeypatch, repository, connection)
    assert await repository.apply_report(17, report) is None

    connection = FakeConnection([FakeResult(), FakeResult()])
    bind_engine(monkeypatch, repository, connection)
    await repository.persist_unmatched(17, report)
    params = connection.calls[1][1]
    assert params["phone_enc"] == b"ciphertext"  # type: ignore[index]
    assert params["phone_hmac"] == "a" * 64  # type: ignore[index]
    assert "phone_hmacs" not in params  # type: ignore[operator]
    unmatched_sql = connection.calls[1][0]
    assert "ON CONFLICT(event_key) DO NOTHING" in unmatched_sql
    assert "CAST(:custom_id AS varchar(64))" in unmatched_sql
    assert "CAST(:phone_hmac AS char(64))" in unmatched_sql
    assert "CAST(:report_status AS smallint)" in unmatched_sql
    assert "CAST(:report_time AS timestamptz)" in unmatched_sql

    connection = FakeConnection([FakeResult(scalars=["known000000000000000000000000001"])])
    bind_engine(monkeypatch, repository, connection)
    assert await repository.filter_known_custom_ids(
        ["known000000000000000000000000001", "untrusted"]
    ) == ["known000000000000000000000000001"]
    assert "FROM sms_chunk" in connection.calls[0][0]


@pytest.mark.asyncio
async def test_report_repository_tracks_raw_errors_and_expires_each_batch_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SqlReportRepository()
    connection = FakeConnection([FakeResult(rowcount=1), FakeResult(rowcount=1)])
    bind_engine(monkeypatch, repository, connection)
    from uuid import UUID

    from app.services.raw_lease import RawProcessingLease

    processed_lease = RawProcessingLease(4, UUID(int=4), 1)
    error_lease = RawProcessingLease(5, UUID(int=5), 2)
    repository.remember_lease(processed_lease)
    repository.remember_lease(error_lease)
    await repository.mark_processed(4)
    await repository.mark_error(5, "bad payload")
    assert "processing_started_at=NULL" in connection.calls[0][0]
    assert "processing_started_at=NULL" in connection.calls[1][0]
    assert "processing_lease_id" in connection.calls[0][0]
    assert "processing_lease_epoch" in connection.calls[0][0]
    assert "parse_state" in connection.calls[0][0]
    assert "replay_eligibility" in connection.calls[0][0]
    assert connection.calls[0][1] == {
        "id": 4,
        "processed": True,
        "error": None,
        "parse_state": "processed",
        "replay_eligibility": "never",
        "lease_id": str(processed_lease.lease_id),
        "epoch": 1,
        "system_replay_audit_state": None,
    }
    assert connection.calls[1][1] == {
        "id": 5,
        "processed": False,
        "error": "bad payload",
        "parse_state": "unattempted",
        "replay_eligibility": "manual",
        "lease_id": str(error_lease.lease_id),
        "epoch": 2,
        "system_replay_audit_state": None,
    }

    connection = FakeConnection(
        [
            FakeResult(scalars=[2, 3]),
            FakeResult(),
            FakeResult(scalar=1),
            FakeResult(),
            FakeResult(),
            FakeResult(scalar=1),
            FakeResult(),
        ]
    )

    async def no_callback(
        _connection: object,
        _batch_id: int,
        **_values: object,
    ) -> None:
        return None

    monkeypatch.setattr(report_repository_module, "enqueue_batch_finished", no_callback)
    bind_engine(monkeypatch, repository, connection)
    assert await repository.expire_unknown(48) == 2
    assert connection.calls[0][0].lstrip().startswith("SELECT DISTINCT m.batch_id")
    assert "sms_batch" in connection.calls[1][0] and "FOR UPDATE" in connection.calls[1][0]
    assert "UPDATE sms_message" in connection.calls[2][0]
    assert "stat_dirty_date" in connection.calls[2][0]
    assert connection.calls[1][1] == {"batch_id": 2}
    assert connection.calls[2][1] == {"batch_id": 2, "hours": 48}
    assert "sms_batch" in connection.calls[4][0] and "FOR UPDATE" in connection.calls[4][0]
    assert "UPDATE sms_message" in connection.calls[5][0]


@pytest.mark.asyncio
async def test_uncertain_repository_uses_gin_evidence_and_guarded_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SqlUncertainRepository()
    created_at = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    connection = FakeConnection(
        [
            FakeResult(
                rows=[
                    {
                        "id": 3,
                        "custom_id": "custom-1 ",
                        "uncertain_since": created_at,
                    }
                ]
            )
        ]
    )
    bind_engine(monkeypatch, repository, connection)
    assert await repository.list_uncertain() == [UncertainChunk(3, "custom-1", created_at)]

    connection = FakeConnection(
        [
            FakeResult(
                rows=[
                    {
                        "id": 9,
                        "source": "report",
                        "payload_enc": b"raw",
                        "payload_sha256": "a" * 64,
                        "key_version": 2,
                    }
                ]
            )
        ]
    )
    bind_engine(monkeypatch, repository, connection)
    candidates = await repository.raw_candidates("custom-1")
    assert candidates[0].payload_enc == b"raw"
    assert "custom_ids @>" in connection.calls[0][0]

    connection = FakeConnection([FakeResult(rowcount=1), FakeResult()])
    bind_engine(monkeypatch, repository, connection)
    await repository.resolve_submitted(3, "task-1")
    assert len(connection.calls) == 2
    assert "status='uncertain'" in connection.calls[0][0]

    connection = FakeConnection([FakeResult(rowcount=0)])
    bind_engine(monkeypatch, repository, connection)
    await repository.resolve_submitted(3, "task-raced")
    assert len(connection.calls) == 1


@pytest.mark.asyncio
async def test_uncertain_overdue_alert_is_log_sink_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[dict[str, object]] = []

    class FakeSink:
        async def emit(self, **values: object) -> None:
            emitted.append(values)

    monkeypatch.setattr(
        uncertain_repository_module,
        "SqlAlertService",
        lambda _settings: FakeSink(),
    )
    chunk = UncertainChunk(
        4,
        "custom-4",
        datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
    )
    await SqlUncertainRepository().alert_overdue(chunk)
    assert emitted == [
        {
            "alert_type": "uncertain_overdue",
            "level": "crit",
            "title": "发送结果未知超过24小时",
            "detail": {"chunk_id": 4, "custom_id": "custom-4"},
            "dedup_key": "uncertain_overdue:4",
        }
    ]


@pytest.mark.asyncio
async def test_batch_query_scopes_sql_and_never_selects_phone_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert BatchAccessScope(app_id=7).sql()[1] == {"scope_app_id": 7}
    assert BatchAccessScope(dept="平台部").sql()[1] == {"scope_dept": "平台部"}
    assert BatchAccessScope(all_departments=True).sql() == ("TRUE", {})
    with pytest.raises(ValueError):
        BatchAccessScope().sql()

    service = BatchQueryService(crypto=content_crypto())
    row: dict[str, object] = {
        "batch_no": "batch-1",
        "display_content_enc": batch_content("batch-1", "验证码******"),
    }
    connection = FakeConnection([FakeResult(rows=[row])])
    bind_engine(monkeypatch, service, connection)
    assert await service.get_batch("batch-1", BatchAccessScope(app_id=7)) == {
        "batch_no": "batch-1",
        "content": "验证码******",
    }

    connection = FakeConnection([FakeResult()])
    bind_engine(monkeypatch, service, connection)
    with pytest.raises(BatchNotFound):
        await service.get_batch("missing", BatchAccessScope(app_id=7))

    detail: dict[str, object] = {
        "id": 1,
        "phone": "138****8000",
        "status": "delivered",
    }
    connection = FakeConnection([FakeResult(rows=[detail])], scalar_values=[1])
    bind_engine(monkeypatch, service, connection)
    page = await service.list_details(
        "batch-1",
        BatchAccessScope(dept="平台部"),
        status="delivered",
        page=2,
        size=20,
    )
    assert page == {"total": 1, "items": [detail]}
    select_sql = connection.calls[1][0]
    assert "phone_mask AS phone" in select_sql
    assert "phone_enc" not in select_sql
    assert "phone_hmac" not in select_sql
    assert connection.calls[1][1]["offset"] == 20  # type: ignore[index]


@pytest.mark.asyncio
async def test_batch_list_builds_only_present_filters_for_asyncpg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = BatchQueryService()
    connection = FakeConnection(
        [FakeResult(scalar=0), FakeResult(rows=[]), FakeResult(rows=[])]
    )
    bind_engine(monkeypatch, service, connection)

    result = await service.list_batches(
        scope=BatchAccessScope(dept="业务一部"),
        category=None,
        statuses=["' OR 1=1--"],
        channel=None,
        app_id=None,
        is_test=None,
        batch_no=None,
        start=None,
        end=None,
        page=1,
        size=20,
    )

    assert result == {"total": 0, "status_counts": {}, "items": []}
    for sql, params in (connection.calls[0], connection.calls[2]):
        assert "IS NULL" not in sql
        assert "b.status=ANY(:statuses)" in sql
        assert params == {
            "scope_dept": "业务一部",
            "statuses": ["' OR 1=1--"],
            "limit": 20,
            "offset": 0,
        }
    counts_sql, counts_params = connection.calls[1]
    assert "GROUP BY b.status" in counts_sql
    assert ":statuses" not in counts_sql
    assert counts_params == {"scope_dept": "业务一部"}


@pytest.mark.asyncio
async def test_batch_list_status_counts_are_faceted_without_status_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = BatchQueryService()
    connection = FakeConnection(
        [
            FakeResult(scalar=3),
            FakeResult(
                rows=[
                    {"status": "sending", "n": 2},
                    {"status": "balance_blocked", "n": 1},
                ]
            ),
            FakeResult(rows=[]),
        ]
    )
    bind_engine(monkeypatch, service, connection)

    result = await service.list_batches(
        scope=BatchAccessScope(all_departments=True),
        category="notice",
        statuses=["sending", "queued"],
        channel=None,
        app_id=None,
        is_test=None,
        batch_no=None,
        start=None,
        end=None,
        page=1,
        size=20,
    )

    assert result["status_counts"] == {"sending": 2, "balance_blocked": 1}
    counts_sql, counts_params = connection.calls[1]
    assert "b.category=:category" in counts_sql
    assert ":statuses" not in counts_sql
    assert counts_params == {"category": "notice"}


@pytest.mark.asyncio
async def test_batch_list_batch_no_filter_escapes_like_wildcards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = BatchQueryService()
    connection = FakeConnection(
        [FakeResult(scalar=0), FakeResult(rows=[]), FakeResult(rows=[])]
    )
    bind_engine(monkeypatch, service, connection)

    await service.list_batches(
        scope=BatchAccessScope(dept="业务一部"),
        category=None,
        statuses=None,
        channel=None,
        app_id=None,
        is_test=None,
        batch_no=" AB_100% ",
        start=None,
        end=None,
        page=1,
        size=20,
    )

    for sql, params in connection.calls:
        assert "trim(b.batch_no) ILIKE :batch_no" in sql
        assert params["batch_no"] == "%AB\\_100\\%%"


@pytest.mark.asyncio
async def test_pipeline_read_repository_returns_config_filters_and_idempotency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlPipelineStore()
    connection = FakeConnection(
        [
            FakeResult(rows=[{"key": "verify_freq_per_minute", "value": "2"}]),
            FakeResult(scalar=500),
        ]
    )
    bind_engine(monkeypatch, store, connection)
    assert await store.load_config("平台部") == {
        "verify_freq_per_minute": "2",
        "dept_daily_quota": "500",
    }
    assert not store._engine().disposed

    response_row = {
        "batch_no": "batch-1 ",
        "total": 2,
        "removed_duplicate": 1,
        "removed_blacklist": 2,
        "removed_freq": 3,
        "segments": 1,
        "quota_cost": 2,
        "status": "queued",
        "deferred_reason": None,
        "scheduled_at": None,
    }
    connection = FakeConnection([FakeResult(rows=[response_row])])
    bind_engine(monkeypatch, store, connection)
    response = await store.response_for("batch-1")
    assert response.batch_no == "batch-1"
    assert response.idempotent is True

    assert await store.blacklisted(set()) == set()

    class FakeRedis:
        class Lock:
            async def __aenter__(self) -> None:
                return None

            async def __aexit__(self, *_: object) -> None:
                return None

        def lock(self, *_: object, **__: object) -> Lock:
            return self.Lock()

        async def get(self, key: str) -> str:
            return "1"

        async def smismember(self, key: str, values: list[str]) -> list[bool]:
            return [value in {"a", "b"} for value in values]

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(
        pipeline_repository_module,
        "redis_client",
        lambda *_args, **_kwargs: FakeRedis(),
    )
    assert await store.blacklisted({"a", "b"}) == {"a", "b"}

    pipeline_repository_module.sensitive_word_index.loaded = False
    pipeline_repository_module.sensitive_word_index.revision = None
    connection = FakeConnection(
        [
            FakeResult(scalar=1),
            FakeResult(scalar=1),
            FakeResult(scalars=["敏感", "禁止"]),
        ]
    )
    bind_engine(monkeypatch, store, connection)
    assert await store.sensitive_hits("敏感内容") == ["敏感"]
    assert "sys_config" in connection.calls[0][0]
    assert "sensitive_word" in connection.calls[2][0]

    connection = FakeConnection([FakeResult(scalar=True)])
    bind_engine(monkeypatch, store, connection)
    scope = IdempotencyScope("app", "7")
    assert await store.exists(scope, "biz-1", "batch-1") is True

    connection = FakeConnection([FakeResult(scalar="batch-1 ")])
    bind_engine(monkeypatch, store, connection)
    assert await store.find_existing(scope, "biz-1") == "batch-1"

    connection = FakeConnection([FakeResult(scalar=None)])
    bind_engine(monkeypatch, store, connection)
    assert await store.find_existing(scope, "missing") is None

    connection = FakeConnection(
        [
            FakeResult(
                rows=[
                    {
                        "request_hash": "a" * 64,
                        "request_hash_key_version": 2,
                    }
                ]
            )
        ]
    )
    bind_engine(monkeypatch, store, connection)
    fingerprint = await store.find_request_fingerprint(scope, "biz-1")
    assert fingerprint is not None
    assert fingerprint.digest == "a" * 64
    assert fingerprint.key_version == 2
    assert "balance_blocked" in connection.calls[0][0]

    connection = FakeConnection([FakeResult(rows=[])])
    bind_engine(monkeypatch, store, connection)
    assert await store.find_request_fingerprint(scope, "legacy") is None

    connection = FakeConnection([FakeResult(scalar="child-1")])
    bind_engine(monkeypatch, store, connection)
    assert await store.find_resend_child("source-1") == "child-1"
    assert "sms_resend_action" in connection.calls[0][0]


@pytest.mark.asyncio
async def test_resend_repository_scopes_batch_and_reads_only_failed_ciphertext(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SqlResendRepository()
    batch = {
        "batch_no": "original-1",
        "dept": "平台部",
        "category": "verify",
        "channel": "api",
        "send_content_enc": b"packed-content",
        "sign_name": "【青鸾】",
        "consent_confirmed": False,
        "is_test": False,
    }
    failed = {
        "phone_enc": b"ciphertext",
        "phone_hmac": "a" * 64,
        "key_version": 2,
    }
    connection = FakeConnection([FakeResult(rows=[batch]), FakeResult(rows=[failed])])
    bind_engine(monkeypatch, repository, connection)

    source = await repository.load("original-1", BatchAccessScope(app_id=7))

    assert source.batch_no == "original-1"
    assert source.failed_phones[0].phone_enc == b"ciphertext"
    assert "b.app_id=:scope_app_id" in connection.calls[0][0]
    assert connection.calls[0][1] == {"batch_no": "original-1", "scope_app_id": 7}
    assert "m.status='failed'" in connection.calls[1][0]
    assert "phone_mask" not in connection.calls[1][0]

    missing = FakeConnection([FakeResult()])
    bind_engine(monkeypatch, repository, missing)
    with pytest.raises(BatchNotFound):
        await repository.load("missing", BatchAccessScope(app_id=7))
