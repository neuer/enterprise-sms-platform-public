from __future__ import annotations

import base64
import hashlib
import importlib.util
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from sqlalchemy.schema import ExecutableDDLElement

from app.services.crypto import CryptoService, EncryptionContext

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"


class BackfillResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def mappings(self) -> BackfillResult:
        return self

    def all(self) -> list[dict[str, object]]:
        return self.rows

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self.rows)


class BackfillConnection:
    def __init__(self, selects: list[list[dict[str, object]]]) -> None:
        self.selects = selects
        self.calls: list[tuple[str, dict[str, object] | None]] = []

    def execute(
        self,
        statement: object,
        params: dict[str, object] | None = None,
    ) -> BackfillResult:
        sql = str(statement)
        self.calls.append((sql, params))
        if sql.lstrip().startswith("SELECT"):
            return BackfillResult(self.selects.pop(0))
        return BackfillResult([])


def load_baseline() -> ModuleType:
    revisions = list((BACKEND / "migrations/versions").glob("0001_*.py"))
    assert len(revisions) == 1, "Alembic 首个迁移尚未建立或基准版本数异常"
    spec = importlib.util.spec_from_file_location("baseline_revision", revisions[0])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_first_migration_executes_lossless_schema_slices(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_baseline()
    executed: list[object] = []
    monkeypatch.setattr(module.op, "execute", executed.append)

    module.upgrade()

    assert len(executed) > 1
    assert all(isinstance(statement, ExecutableDDLElement) for statement in executed)
    assert "".join(str(statement) for statement in executed) == (
        ROOT / "schema.sql"
    ).read_text(encoding="utf-8")
    assert module.down_revision is None


def test_schema_splitter_ignores_semicolons_in_literals_and_comments() -> None:
    module = load_baseline()
    sql = "SELECT ';'; -- keep ;\n/* block ; */ SELECT \";\";\n"

    statements = module.split_sql_statements(sql)

    assert statements == ("SELECT ';';", ' -- keep ;\n/* block ; */ SELECT \";\";\n')
    assert "".join(statements) == sql


def test_baseline_downgrade_refuses_destructive_rollback() -> None:
    module = load_baseline()

    with pytest.raises(RuntimeError, match="baseline"):
        module.downgrade()


def test_container_includes_alembic_assets_and_canonical_schema() -> None:
    dockerfile = (BACKEND / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY backend/alembic.ini" in dockerfile
    assert "COPY backend/migrations" in dockerfile
    assert "COPY schema.sql" in dockerfile


def test_migration_checker_explicitly_uses_mock_vendor_mode() -> None:
    checker = (BACKEND / "scripts_support/check_migration.py").read_text(encoding="utf-8")

    assert '"DEBUG": "1"' in checker
    assert '"AUTH_MOCK": "1"' in checker
    assert '"VENDOR_MOCK": "1"' in checker
    assert '"DATA_AES_KEY_FILE": str(DATA_AES_SECRET)' in checker
    assert '"DATA_HMAC_KEY_FILE": str(DATA_HMAC_SECRET)' in checker


def test_alert_smtp_relay_config_is_in_schema_and_followup_migration() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    for key in ("alert_smtp_host", "alert_smtp_port", "alert_mail_from"):
        assert f"('{key}'" in schema
    assert "alert_smtp_password" not in schema

    revisions = list((BACKEND / "migrations/versions").glob("0004_alert_smtp_config.py"))
    assert len(revisions) == 1
    spec = importlib.util.spec_from_file_location("alert_smtp_revision", revisions[0])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "0003_sign_vendor_id"


def test_export_worker_lease_is_in_schema_and_followup_migration() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    assert "started_at  TIMESTAMPTZ" in schema

    revisions = list((BACKEND / "migrations/versions").glob("0005_export_started_at.py"))
    assert len(revisions) == 1
    spec = importlib.util.spec_from_file_location("export_lease_revision", revisions[0])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "0004_alert_smtp_config"


def test_user_directory_snapshot_is_in_schema_and_followup_migration() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    assert "source_groups" in schema
    assert "last_synced_at" in schema

    revisions = list(
        (BACKEND / "migrations/versions").glob("0006_user_directory_snapshot.py")
    )
    assert len(revisions) == 1
    spec = importlib.util.spec_from_file_location("user_snapshot_revision", revisions[0])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "0005_export_started_at"


def test_chunk_uncertain_timestamp_is_in_schema_and_followup_migration() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    assert "uncertain_since" in schema

    revisions = list(
        (BACKEND / "migrations/versions").glob("0007_chunk_uncertain_since.py")
    )
    assert len(revisions) == 1
    spec = importlib.util.spec_from_file_location("chunk_uncertain_revision", revisions[0])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "0006_user_directory_snapshot"


def test_audit_payload_pii_guard_is_in_schema_and_followup_migration() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    assert "ck_audit_payload_no_pii" in schema
    assert "phone_hmac" in schema and "mobile_list" in schema
    assert "before_val - 'batch_no'" in schema
    assert "after_val - 'batch_no'" in schema

    revisions = list((BACKEND / "migrations/versions").glob("0008_audit_payload_guard.py"))
    assert len(revisions) == 1
    spec = importlib.util.spec_from_file_location("audit_guard_revision", revisions[0])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "0007_chunk_uncertain_since"
    assert "pg_constraint" in module.upgrade.__code__.co_consts[1]

    fixes = list(
        (BACKEND / "migrations/versions").glob("0009_audit_batch_no_guard.py")
    )
    assert len(fixes) == 1
    fix_spec = importlib.util.spec_from_file_location("audit_batch_no_revision", fixes[0])
    assert fix_spec is not None and fix_spec.loader is not None
    fix_module = importlib.util.module_from_spec(fix_spec)
    fix_spec.loader.exec_module(fix_module)
    assert fix_module.down_revision == "0008_audit_payload_guard"


def test_chunk_submitting_timestamp_is_in_schema_and_followup_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    assert "-- v1.6.9：" in schema
    assert "submitting_since TIMESTAMPTZ" in schema
    assert "idx_chunk_submitting" in schema

    revisions = list(
        (BACKEND / "migrations/versions").glob("0010_chunk_submitting_since.py")
    )
    assert len(revisions) == 1
    spec = importlib.util.spec_from_file_location("chunk_submitting_revision", revisions[0])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "0009_audit_batch_no_guard"

    class EmptyCatalog:
        def get_columns(self, _table: str) -> list[dict[str, object]]:
            return []

        def get_indexes(self, _table: str) -> list[dict[str, object]]:
            return []

    executed: list[str] = []
    added_columns: list[tuple[str, Any]] = []
    created_indexes: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(module.op, "get_bind", lambda: object())
    monkeypatch.setattr(module.sa, "inspect", lambda _bind: EmptyCatalog())
    monkeypatch.setattr(module.op, "execute", lambda sql: executed.append(str(sql)))
    monkeypatch.setattr(
        module.op,
        "add_column",
        lambda table, column: added_columns.append((table, column)),
    )
    monkeypatch.setattr(
        module.op,
        "create_index",
        lambda *args, **kwargs: created_indexes.append((args, kwargs)),
    )
    module.upgrade()
    assert any("WHERE status='submitting'" in sql for sql in executed)
    assert added_columns[0][0] == "sms_chunk"
    assert added_columns[0][1].name == "submitting_since"
    assert created_indexes[0][0][:3] == (
        "idx_chunk_submitting",
        "sms_chunk",
        ["submitting_since"],
    )
    assert "postgresql_where" in created_indexes[0][1]


def test_chunk_retry_due_time_is_in_schema_and_followup_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    assert "-- v1.6.10：" in schema
    assert "retry_not_before TIMESTAMPTZ" in schema
    revisions = list(
        (BACKEND / "migrations/versions").glob("0011_chunk_retry_not_before.py")
    )
    assert len(revisions) == 1
    spec = importlib.util.spec_from_file_location("chunk_retry_due_revision", revisions[0])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "0010_chunk_submitting_since"

    class Catalog:
        def __init__(self, *, existing: bool) -> None:
            self.existing = existing

        def get_columns(self, _table: str) -> list[dict[str, object]]:
            return [{"name": "retry_not_before"}] if self.existing else []

        def get_indexes(self, _table: str) -> list[dict[str, object]]:
            return [{"name": "idx_chunk_retry_due"}] if self.existing else []

    added: list[object] = []
    indexed: list[tuple[tuple[object, ...], dict[str, object]]] = []
    executed: list[str] = []
    monkeypatch.setattr(module.op, "get_bind", lambda: object())
    monkeypatch.setattr(module.sa, "inspect", lambda _bind: Catalog(existing=False))
    monkeypatch.setattr(module.op, "add_column", lambda *_args: added.append(_args))
    monkeypatch.setattr(module.op, "execute", lambda sql: executed.append(str(sql)))
    monkeypatch.setattr(
        module.op,
        "create_index",
        lambda *args, **kwargs: indexed.append((args, kwargs)),
    )
    module.upgrade()
    assert len(added) == 1
    assert indexed[0][0][:3] == (
        "idx_chunk_retry_due",
        "sms_chunk",
        ["retry_not_before"],
    )
    assert "postgresql_where" in indexed[0][1]
    assert any("WHERE status='retrying'" in sql for sql in executed)

    monkeypatch.setattr(module.sa, "inspect", lambda _bind: Catalog(existing=True))
    module.upgrade()
    assert len(added) == 1
    assert len(indexed) == 1


def test_callback_event_keys_migration_backfills_composite_refs_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    assert "-- v1.6.11：" in schema
    assert "event_keys    CHAR(64)[] NOT NULL DEFAULT '{}'" in schema
    revision = BACKEND / "migrations/versions/0012_callback_event_keys.py"
    spec = importlib.util.spec_from_file_location("callback_event_revision", revision)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "0011_chunk_retry_not_before"

    class Catalog:
        def get_table_names(self) -> list[str]:
            return []

        def get_columns(self, _table: str) -> list[dict[str, object]]:
            return []

        def get_indexes(self, _table: str) -> list[dict[str, object]]:
            return []

    executed: list[str] = []
    monkeypatch.setattr(module.op, "get_bind", lambda: object())
    monkeypatch.setattr(module.sa, "inspect", lambda _bind: Catalog())
    monkeypatch.setattr(module.op, "execute", lambda sql: executed.append(str(sql)))
    monkeypatch.setattr(module.op, "add_column", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module.op, "create_table", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module.op, "create_index", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module.op, "drop_constraint", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module.op, "create_check_constraint", lambda *_args, **_kwargs: None)
    module.upgrade()
    sql = "\n".join(executed)
    assert "WITH ORDINALITY" in sql
    assert "m.id=r.message_id AND m.created_at=r.message_created_at" in sql
    assert "digest(" in sql and "'sha256'" in sql
    assert "ambiguous legacy callback events" in sql
    assert "callback event key backfill incomplete" in sql
    executed.clear()
    monkeypatch.setattr(module.op, "drop_column", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module.op, "drop_table", lambda *_args, **_kwargs: None)
    module.downgrade()
    assert "callback event downgrade unsafe" in executed[0]


def test_account_provider_model_replaces_username_centric_schema() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")

    for table in (
        "user_account",
        "auth_provider",
        "auth_identity",
        "local_credential",
        "external_role_mapping",
    ):
        assert f"CREATE TABLE {table}" in schema
    assert "CREATE TABLE sys_user" not in schema
    assert "CREATE TABLE role_mapping" not in schema
    assert "UNIQUE (normalized_login_name)" in schema
    assert "UNIQUE (provider_id, external_subject)" in schema
    assert "('local', '本地账号', 'local', TRUE)" in schema
    assert "('ad', 'AD 账号', 'ldap', FALSE)" in schema


def test_vendor_live_test_budget_is_in_schema_and_followup_migration() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0016_vendor_live_test_budget.py"

    assert "-- v1.6.15：" in schema
    assert "vendor_attempt_count" in schema
    assert "CREATE TABLE vendor_test_daily_usage" in schema
    assert "CREATE TABLE vendor_test_send_attempt" in schema
    assert revision.is_file()


def test_vendor_test_reset_operation_constraint_is_linear_and_data_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    operation_table = schema.split(
        "CREATE TABLE vendor_test_operation",
        maxsplit=1,
    )[1].split(");", maxsplit=1)[0]
    revision = (
        BACKEND / "migrations/versions/0022_vendor_test_reset_operation.py"
    )
    spec = importlib.util.spec_from_file_location(
        "vendor_test_reset_operation_revision",
        revision,
    )
    assert revision.is_file()
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    dropped: list[tuple[tuple[object, ...], dict[str, object]]] = []
    created: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        module.op,
        "drop_constraint",
        lambda *args, **kwargs: dropped.append((args, kwargs)),
    )
    monkeypatch.setattr(
        module.op,
        "create_check_constraint",
        lambda *args, **kwargs: created.append((args, kwargs)),
    )

    module.upgrade()
    module.downgrade()

    old_types = {
        "install_credentials",
        "rotate_credentials",
        "activate",
        "pause",
        "resume",
        "uat_send",
    }
    assert module.revision == "0022_vendor_test_reset_operation"
    assert module.down_revision == "0021_approval_legacy_default"
    assert len(dropped) == len(created) == 2
    assert all(
        call[0][0] == "vendor_test_operation_operation_type_check"
        and call[0][1] == "vendor_test_operation"
        and call[1] == {"type_": "check"}
        for call in dropped
    )
    upgrade_condition = str(created[0][0][2])
    downgrade_condition = str(created[1][0][2])
    assert all(f"'{operation_type}'" in operation_table for operation_type in old_types)
    assert "'reset_configuration'" in operation_table
    assert all(f"'{operation_type}'" in upgrade_condition for operation_type in old_types)
    assert "'reset_configuration'" in upgrade_condition
    assert all(f"'{operation_type}'" in downgrade_condition for operation_type in old_types)
    assert "'reset_configuration'" not in downgrade_condition

    source = revision.read_text(encoding="utf-8").upper()
    for forbidden in ("UPDATE ", "DELETE ", "TRUNCATE ", "INSERT INTO"):
        assert forbidden not in source


def test_vendor_uat_acceptance_lease_is_in_schema_and_followup_migration() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0023_vendor_uat_acceptance_lease.py"

    assert "-- v1.6.22：" in schema
    operation_table = schema.split(
        "CREATE TABLE vendor_test_operation",
        maxsplit=1,
    )[1].split(");", maxsplit=1)[0]
    assert "lease_expires_at TIMESTAMPTZ" in operation_table
    assert "chk_vendor_test_operation_uat_lease" in operation_table
    assert "idx_vendor_test_operation_uat_lease" in schema
    assert revision.is_file()

    spec = importlib.util.spec_from_file_location(
        "vendor_uat_acceptance_lease_revision",
        revision,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0023_vendor_uat_acceptance_lease"
    assert module.down_revision == "0022_vendor_test_reset_operation"


def test_raw_replay_processing_lease_is_in_schema_and_followup_migration() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0038_raw_replay_processing_lease.py"

    assert "-- v1.6.37：" in schema
    raw_table = schema.split(
        "CREATE TABLE raw_vendor_log",
        maxsplit=1,
    )[1].split(");", maxsplit=1)[0]
    assert "processing_started_at TIMESTAMPTZ" in raw_table
    assert "idx_raw_processing_lease" in schema
    assert revision.is_file()

    spec = importlib.util.spec_from_file_location(
        "raw_replay_processing_lease_revision",
        revision,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0038_raw_replay_processing_lease"
    assert module.down_revision == "0037_metrics_access_boundary"
    source = revision.read_text(encoding="utf-8")
    assert "cannot downgrade raw replay processing lease with active claims" in source
    assert "GRANT SELECT (queue,state) ON outbox_event TO sms_metrics" in source
    assert "DROP INDEX IF EXISTS idx_raw_processing_lease" in source
    assert "DROP COLUMN IF EXISTS processing_started_at" in source


def test_raw_processing_lease_epoch_is_in_schema_and_followup_migration() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0076_raw_processing_lease.py"

    raw_table = schema.split(
        "CREATE TABLE raw_vendor_log",
        maxsplit=1,
    )[1].split("CREATE INDEX idx_raw_unprocessed", maxsplit=1)[0]
    assert "processing_lease_id UUID" in raw_table
    assert "processing_lease_epoch BIGINT" in raw_table
    assert "processing_lease_expires_at TIMESTAMPTZ" in raw_table
    assert "ck_raw_vendor_processed_consistency" in raw_table
    assert "idx_raw_processing_lease_epoch" in schema
    assert "CHECK (task_kind IN ('callback','export','raw'))" in schema
    assert revision.is_file()

    spec = importlib.util.spec_from_file_location(
        "raw_processing_lease_revision",
        revision,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0076_raw_processing_lease"
    assert module.down_revision == "0075_raw_parse_eligibility"
    source = revision.read_text(encoding="utf-8")
    assert "processing_lease_id" in source
    assert "ck_raw_vendor_processed_consistency" in source
    assert "GRANT UPDATE" in source
    assert "sms_accept" in source


def test_system_replay_audit_state_is_in_schema_and_followup_migration() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0077_raw_system_replay_audit.py"

    raw_table = schema.split(
        "CREATE TABLE raw_vendor_log",
        maxsplit=1,
    )[1].split("CREATE INDEX idx_raw_unprocessed", maxsplit=1)[0]
    assert "system_replay_audit_state VARCHAR(16)" in raw_table
    assert "idx_raw_system_replay_audit_pending" in schema
    assert "idx_audit_raw_replay_system_epoch" in schema
    assert revision.is_file()

    spec = importlib.util.spec_from_file_location(
        "raw_system_replay_audit_revision",
        revision,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0077_raw_system_replay_audit"
    assert module.down_revision == "0076_raw_processing_lease"
    source = revision.read_text(encoding="utf-8")
    assert "system_replay_audit_state" in source
    assert "idx_audit_raw_replay_system_epoch" in source
    assert "REVOKE UPDATE ON raw_vendor_log" not in source


def test_system_raw_replay_audit_producer_is_whitelisted() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0078_system_raw_replay_audit_producer.py"
    assert "NEW.actor='system-reconcile' AND NEW.action='raw_replay'" in schema
    assert "GRANT UPDATE (system_replay_audit_state)" not in schema
    send_grants = schema.split("-- 发送、拉取、对账、统计与业务 worker。", maxsplit=1)[1].split(
        "-- 回调 worker", maxsplit=1
    )[0]
    send_select = send_grants.split("GRANT SELECT ON", maxsplit=1)[1].split(
        "TO sms_send;", maxsplit=1
    )[0]
    assert "audit_log" not in send_select
    assert (
        "GRANT INSERT ON\n    report_event, reply_event, worker_lease_event, audit_log\n"
        "TO sms_send;"
    ) in send_grants
    assert revision.is_file()

    spec = importlib.util.spec_from_file_location(
        "system_raw_replay_audit_producer_revision",
        revision,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0078_system_raw_replay_audit_producer"
    assert module.down_revision == "0077_raw_system_replay_audit"
    source = revision.read_text(encoding="utf-8")
    assert "system-reconcile" in source
    assert "raw_replay" in source


def test_correlation_audit_guard_preserves_immutable_legacy_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = BACKEND / "migrations/versions/0033_correlation_audit_chain.py"
    spec = importlib.util.spec_from_file_location(
        "correlation_audit_chain_revision",
        revision,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    executed: list[str] = []
    monkeypatch.setattr(module.op, "execute", lambda sql: executed.append(str(sql)))

    module.upgrade()

    guard = next(
        statement
        for statement in executed
        if "ADD CONSTRAINT ck_audit_payload_no_pii" in statement
    )
    validation = next(
        statement
        for statement in executed
        if "VALIDATE CONSTRAINT ck_audit_payload_no_pii" in statement
    )
    assert ") NOT VALID" in guard
    assert "WHEN check_violation" in validation
    assert "UPDATE audit_log SET before_val" not in "\n".join(executed)
    assert "UPDATE audit_log SET after_val" not in "\n".join(executed)
    assert "DELETE FROM audit_log" not in "\n".join(executed)


def test_manual_job_outbox_is_in_schema_and_followup_migration() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0039_manual_job_outbox.py"

    assert "-- v1.6.38：" in schema
    outbox_table = schema.split(
        "CREATE TABLE outbox_event",
        maxsplit=1,
    )[1].split(");", maxsplit=1)[0]
    assert "'app.tasks.outbox.trigger_job'" in outbox_table
    assert revision.is_file()

    spec = importlib.util.spec_from_file_location(
        "manual_job_outbox_revision",
        revision,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0039_manual_job_outbox"
    assert module.down_revision == "0038_raw_replay_processing_lease"
    source = revision.read_text(encoding="utf-8")
    assert "cannot downgrade manual job outbox with persisted events" in source
    assert "DROP CONSTRAINT IF EXISTS ck_outbox_task_name" in source


def test_template_sync_outbox_contract_is_in_schema_and_current_migration() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0061_vendor_binding_outbox.py"
    source = revision.read_text(encoding="utf-8")

    for contract in (schema, source):
        assert contract.count("'app.tasks.sync_template'") >= 3
        assert "template[.]sync[:][1-9][0-9]*[:][1-9][0-9]*" in contract


def test_sign_adoption_contract_is_in_schema_and_followup_migration() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0081_sign_adoption_contract.py"
    source = revision.read_text(encoding="utf-8")

    assert "-- v1.6.67：" in schema
    for contract in (schema, source):
        assert contract.count("'app.tasks.adopt_sign'") >= 3
        assert "sign[.]adopt" in contract
        assert "'template_sync','sign_sync','sign_adopt'" in contract
    assert 'revision = "0081_sign_adoption_contract"' in source
    assert 'down_revision = "0080_security_daily_delivery_generation"' in source


def test_outbox_realtime_report_queue_is_named_and_forward_compatible() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = (
        BACKEND
        / "migrations/versions/0082_outbox_realtime_report_queue.py"
    )
    source = revision.read_text(encoding="utf-8")

    assert "-- v1.6.68：" in schema
    assert "CONSTRAINT ck_outbox_queue" in schema
    assert "'realtime','realtime-report','bulk','callback'" in schema.replace(
        "\n", ""
    ).replace(" ", "")
    assert 'revision = "0082_outbox_realtime_report_queue"' in source
    assert 'down_revision = "0081_sign_adoption_contract"' in source
    assert (
        "RENAME CONSTRAINT outbox_event_queue_check TO ck_outbox_queue"
        in source.replace("\n", " ")
    )
    assert "WHEN undefined_object THEN NULL" in source
    assert "DROP CONSTRAINT IF EXISTS ck_outbox_queue" in source
    assert "WHERE queue='realtime-report'" in source


def test_password_change_token_lease_is_fenced_and_expand_only() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = (
        BACKEND
        / "migrations/versions/0083_password_change_token_lease.py"
    )
    source = revision.read_text(encoding="utf-8")

    assert "-- v1.6.69：" in schema
    for contract in (schema, source):
        assert "processing_lease_id" in contract
        assert "processing_lease_expires_at" in contract
        assert "password_change_token_status_check" in contract
        assert "ck_password_change_processing_lease" in contract
        assert "idx_password_change_processing_lease" in contract
        assert "'available','processing','consumed','revoked','expired'" in contract.replace(
            "\n", ""
        ).replace(" ", "")
    assert 'revision = "0083_password_change_token_lease"' in source
    assert 'down_revision = "0082_outbox_realtime_report_queue"' in source
    assert "cannot remove password change leases while tokens are processing" in source


def test_auth_security_audit_and_ad_freshness_are_expand_only() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = (
        BACKEND
        / "migrations/versions/0084_auth_security_and_ad_freshness.py"
    )
    source = revision.read_text(encoding="utf-8")

    assert "-- v1.6.70：" in schema
    for contract in (schema, source):
        assert "ad_session_max_age_minutes" in contract
        assert "idx_audit_auth_security_transition" in contract
        assert "auth_account_locked" in contract
        assert "auth_ip_banned" in contract
    assert 'revision = "0084_auth_security_and_ad_freshness"' in source
    assert 'down_revision = "0083_password_change_token_lease"' in source
    assert "CREATE OR REPLACE FUNCTION enforce_live_audit_principal()" in source
    assert "pg_get_functiondef" in source
    assert "cannot restore auth system audit action allowlist safely" in source
    assert "DELETE FROM audit_log" not in source


def test_api_key_pepper_versions_are_expand_only() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0085_api_key_pepper_versions.py"
    source = revision.read_text(encoding="utf-8")

    assert "-- v1.6.71：" in schema
    for contract in (schema, source):
        assert "api_key_hash_version" in contract
        assert "api_key_prev_hash_version" in contract
        assert "ck_app_api_key_hash_version" in contract
    assert 'revision = "0085_api_key_pepper_versions"' in source
    assert 'down_revision = "0084_auth_security_and_ad_freshness"' in source
    assert "DELETE FROM" not in source


def test_chunk_ready_outbox_is_expand_only() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0086_chunk_ready_outbox.py"
    source = revision.read_text(encoding="utf-8")

    assert "-- v1.6.72：" in schema
    for contract in (schema, source):
        assert "'app.tasks.send.process_chunk'" in contract
        assert "chunk[.]ready[:][1-9][0-9]*" in contract
    assert 'revision = "0086_chunk_ready_outbox"' in source
    assert 'down_revision = "0085_api_key_pepper_versions"' in source
    assert "DELETE FROM" not in source


def test_uncertain_conservative_terminal_is_expand_only() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0087_uncertain_conservative_terminal.py"
    source = revision.read_text(encoding="utf-8")

    assert "-- v1.6.73：" in schema
    for contract in (schema, source):
        assert "completed_unknown" in contract
        assert "unknown_terminal" in contract
        assert "uncertain_max_lifetime_hours" in contract
        assert "sms_uncertain_resolution" in contract
        assert "late_evidence_at" in contract
    assert "GRANT SELECT (state) ON sms_uncertain_resolution TO sms_metrics" in source
    assert "completed_with_unknown" not in source
    assert 'revision = "0087_uncertain_conservative_terminal"' in source
    assert 'down_revision = "0086_chunk_ready_outbox"' in source
    assert "DELETE FROM" not in source
    assert "ALTER COLUMN" not in source


def test_app_admission_defaults_is_expand_only() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0088_app_admission_defaults.py"
    source = revision.read_text(encoding="utf-8")

    assert "-- v1.6.74：" in schema
    for contract in (schema, source):
        assert "recipient_limit_per_min" in contract
        assert "segment_limit_per_min" in contract
        assert "max_in_flight_chunks" in contract
        assert "allow_market_api_bulk" in contract
        assert "ip_allowlist_exempt_until" in contract
        assert "unlimited_quota_exempt_until" in contract
        assert "ck_app_recipient_limit_per_min" in contract
    assert "DEFAULT 'notice'" in source
    assert 'revision = "0088_app_admission_defaults"' in source
    assert 'down_revision = "0087_uncertain_conservative_terminal"' in source
    assert "DELETE FROM" not in source


def test_send_admission_metrics_grant_is_expand_only() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0089_send_admission_metrics_grant.py"
    source = revision.read_text(encoding="utf-8")

    assert "-- v1.6.75：" in schema
    assert "GRANT SELECT (queue, state, created_at)" in schema
    assert "GRANT SELECT (created_at)" in source
    assert "outbox_event TO sms_metrics" in source
    assert 'revision = "0089_send_admission_metrics_grant"' in source
    assert 'down_revision = "0088_app_admission_defaults"' in source
    assert "DELETE FROM" not in source
    assert "sys_config" not in source


def test_api_key_digest_algorithms_are_expand_only() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0091_api_key_digest_algorithms.py"
    source = revision.read_text(encoding="utf-8")

    assert "-- v1.6.77：" in schema
    for contract in (schema, source):
        assert "api_key_hash_algorithm" in contract
        assert "legacy_data_hmac_pepper_v1" in contract
        assert "ck_app_api_key_algorithm_version" in contract
        assert "api_key_unclassified_algorithms" in contract
    assert 'revision = "0091_api_key_digest_algorithms"' in source
    assert 'down_revision = "0090_vendor_routing"' in source
    assert "DELETE FROM" not in source
    assert "NULL 版本静默" in source or "不得静默" in schema


def test_send_lifecycle_r2_facts_are_expand_only() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0092_send_lifecycle_r2_facts.py"
    source = revision.read_text(encoding="utf-8")

    assert "-- v1.6.78：" in schema
    for contract in (schema, source):
        assert "sms_uncertain_child" in contract
        assert "usage_chunk_allocation" in contract
        assert "send_inflight_balance" in contract
        assert "send_admission_state" in contract
        assert "send_runtime_heartbeat" in contract
        assert "sms_scheduler" in contract
        assert "apply_uncertain_effect" in contract
        assert "uncertain-unused" in contract
    assert 'revision = "0092_send_lifecycle_r2_facts"' in source
    assert 'down_revision = "0091_api_key_digest_algorithms"' in source
    assert "DELETE FROM" not in source


def test_auth_r2_credential_version_is_expand_only() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0093_auth_r2_credential_version.py"
    source = revision.read_text(encoding="utf-8")

    assert "-- v1.6.79：" in schema
    for contract in (schema, source):
        assert "credential_version" in contract
        assert "issued_credential_version" in contract
        assert "ck_local_credential_version_positive" in contract
        assert "ck_password_change_issued_credential_version" in contract
    assert 'revision = "0093_auth_r2_credential_version"' in source
    assert 'down_revision = "0092_send_lifecycle_r2_facts"' in source
    assert "DELETE FROM" not in source
    assert "ADD COLUMN IF NOT EXISTS" in source


def test_send_inflight_reservation_lifecycle_is_expand_only() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0094_send_inflight_reservation_lifecycle.py"
    source = revision.read_text(encoding="utf-8")

    assert "-- v1.6.80：" in schema
    for contract in (schema, source):
        assert "batch_bound" in contract
        assert "send_inflight_reservation_id" in contract
        assert "ck_inflight_released_pair" in contract
        assert "uk_sms_batch_send_inflight_reservation" in contract
    assert 'revision = "0094_send_inflight_reservation_lifecycle"' in source
    assert 'down_revision = "0093_auth_r2_credential_version"' in source
    assert "DELETE FROM" not in source
    assert "ADD COLUMN IF NOT EXISTS" in source


def test_vendor_attempt_generation_machine_is_expand_only() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0095_vendor_attempt_generation_machine.py"
    source = revision.read_text(encoding="utf-8")

    assert "-- v1.6.81：" in schema
    for contract in (schema, source):
        assert "invoking" in contract
        assert "uk_sms_vendor_attempt_generation" in contract
        assert "invoke_started_at" in contract
    assert 'revision = "0095_vendor_attempt_generation_machine"' in source
    assert 'down_revision = "0094_send_inflight_reservation_lifecycle"' in source
    assert "DELETE FROM" not in source
    assert "ON CONFLICT DO NOTHING" not in source


def test_idempotency_claim_generation_is_expand_only() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0096_idempotency_claim_generation.py"
    source = revision.read_text(encoding="utf-8")

    assert "-- v1.6.82：" in schema
    for contract in (schema, source):
        assert "idempotency_claim" in contract
        assert "uk_idempotency_claim_scope" in contract
    assert 'revision = "0096_idempotency_claim_generation"' in source
    assert 'down_revision = "0095_vendor_attempt_generation_machine"' in source
    assert "DELETE FROM" not in source


def test_vendor_attempt_atomic_finalize_is_expand_only() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0098_vendor_attempt_atomic_finalize.py"
    source = revision.read_text(encoding="utf-8")

    assert "-- v1.6.84：" in schema
    for contract in (schema, source):
        assert "inconsistent" in contract
        assert "sms_vendor_attempt_outcome_check" in contract
    assert 'revision = "0098_vendor_attempt_atomic_finalize"' in source
    assert 'down_revision = "0097_auth_session_policy"' in source
    assert "DELETE FROM" not in source
    assert source.split("def downgrade")[0].count("UPDATE ") == 0


def test_auth_session_policy_is_expand_only() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0097_auth_session_policy.py"
    source = revision.read_text(encoding="utf-8")

    assert "-- v1.6.83：" in schema
    for contract in (schema, source):
        assert "auth_session_policy" in contract
        assert "ad_session_max_age_minutes" in contract
        assert "revision" in contract
    assert 'revision = "0097_auth_session_policy"' in source
    assert 'down_revision = "0096_idempotency_claim_generation"' in source
    assert "DELETE FROM" not in source


def test_vendor_routing_is_expand_only() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0090_vendor_routing.py"
    source = revision.read_text(encoding="utf-8")

    assert "-- v1.6.76：" in schema
    for contract in (schema, source):
        assert "sms_vendor_attempt" in contract
        assert "selected_vendor" in contract
        assert "uk_sms_vendor_attempt_irreversible" in contract
        assert "GRANT SELECT (outcome, created_at)" in contract
    assert 'revision = "0090_vendor_routing"' in source
    assert 'down_revision = "0089_send_admission_metrics_grant"' in source
    assert "DELETE FROM" not in source
    assert "sys_config" not in source
    assert "completed_with_unknown" not in source


def test_background_task_role_matrix_covers_import_and_cleanup_paths() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0040_background_task_role_matrix.py"
    source = revision.read_text(encoding="utf-8")

    for fragment in (
        "GRANT SELECT ON user_account, import_task, import_phone TO sms_send",
        "GRANT UPDATE, DELETE ON import_task TO sms_send",
        "GRANT INSERT, DELETE ON import_phone TO sms_send",
        "GRANT DELETE ON idempotency_record, callback_task, callback_report_event TO sms_send",
        "GRANT USAGE, SELECT ON SEQUENCE import_phone_id_seq TO sms_send",
    ):
        assert fragment in source
    assert "user_account, app, dept_quota" in schema
    assert "import_task, import_phone, approval" in schema
    assert "GRANT UPDATE, DELETE ON import_task TO sms_send" in schema
    assert "GRANT INSERT, DELETE ON import_phone TO sms_send" in schema
    assert "GRANT INSERT, UPDATE, DELETE ON callback_task TO sms_send" in schema

    spec = importlib.util.spec_from_file_location(
        "background_task_role_matrix_revision",
        revision,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0040_background_task_role_matrix"
    assert module.down_revision == "0039_manual_job_outbox"


def test_security_daily_control_migration_is_current_baseline_safe() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0041_security_daily_control.py"
    source = revision.read_text(encoding="utf-8")

    assert "security_daily_report" in schema
    assert "security_daily_delivery_request" in schema
    assert "CREATE TABLE IF NOT EXISTS security_daily_report" in source
    assert "CREATE TABLE IF NOT EXISTS security_daily_delivery_request" in source
    assert "GRANT SELECT, INSERT, UPDATE ON security_daily_report TO sms_send" in source

    spec = importlib.util.spec_from_file_location(
        "security_daily_control_revision",
        revision,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0041_security_daily_control"
    assert module.down_revision == "0040_background_task_role_matrix"


def test_security_daily_runtime_config_migration_backfills_incremental_databases() -> None:
    revision = BACKEND / "migrations/versions/0042_security_daily_config.py"
    source = revision.read_text(encoding="utf-8")

    for key in (
        "security_daily_enabled",
        "security_daily_recipient_count",
        "security_daily_resend_configured",
    ):
        assert key in source
    assert "ON CONFLICT(key) DO NOTHING" in source

    spec = importlib.util.spec_from_file_location(
        "security_daily_runtime_config_revision",
        revision,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0042_security_daily_config"
    assert module.down_revision == "0041_security_daily_control"


def test_security_daily_ui_config_migration_adds_resend_key_and_recipients() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0043_security_daily_ui_config.py"
    source = revision.read_text(encoding="utf-8")

    assert "security_daily_resend_api_key" in schema
    assert "security_daily_recipient" in schema
    assert "CREATE TABLE IF NOT EXISTS security_daily_recipient" in source
    assert "GRANT SELECT ON security_daily_recipient TO sms_send" in source

    spec = importlib.util.spec_from_file_location(
        "security_daily_ui_config_revision",
        revision,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0043_security_daily_ui_config"
    assert module.down_revision == "0042_security_daily_config"


def test_security_daily_audit_evidence_view_is_minimal_send_role_grant() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0044_security_daily_audit_view.py"
    source = revision.read_text(encoding="utf-8")

    assert "-- v1.6.41：" in schema
    assert "CREATE VIEW security_daily_audit_evidence" in schema
    assert "GRANT SELECT ON security_daily_audit_evidence TO sms_send" in schema
    assert "GRANT SELECT ON security_daily_audit_evidence TO sms_accept" in schema
    view = schema.split("CREATE VIEW security_daily_audit_evidence", maxsplit=1)[1].split(
        ";",
        maxsplit=1,
    )[0]
    assert "before_val" not in view
    assert "after_val" not in view
    assert "CREATE OR REPLACE VIEW security_daily_audit_evidence" in source
    assert "GRANT SELECT ON security_daily_audit_evidence TO sms_send" in source
    assert "DROP VIEW security_daily_audit_evidence" in source

    spec = importlib.util.spec_from_file_location(
        "security_daily_audit_evidence_revision",
        revision,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0044_security_daily_audit_view"
    assert module.down_revision == "0043_security_daily_ui_config"


def test_security_daily_audit_view_grants_accept_role() -> None:
    revision = BACKEND / "migrations/versions/0048_security_daily_audit_accept.py"
    source = revision.read_text(encoding="utf-8")

    assert "GRANT SELECT ON security_daily_audit_evidence TO sms_accept" in source
    assert "REVOKE SELECT ON security_daily_audit_evidence FROM sms_accept" in source

    spec = importlib.util.spec_from_file_location(
        "security_daily_audit_accept_revision",
        revision,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0048_security_daily_audit_accept"
    assert module.down_revision == "0047_widen_alembic_version"


def test_security_daily_delivery_request_grants_send_role_for_auto_path() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0051_security_daily_delivery_send.py"
    source = revision.read_text(encoding="utf-8")

    assert "-- v1.6.45：" in schema
    assert (
        "GRANT SELECT, INSERT, UPDATE ON security_daily_delivery_request TO sms_send"
        in schema
    )
    assert (
        "GRANT SELECT, INSERT, UPDATE ON security_daily_delivery_request TO sms_send"
        in source
    )
    assert (
        "REVOKE SELECT, INSERT, UPDATE ON security_daily_delivery_request FROM sms_send"
        in source
    )

    spec = importlib.util.spec_from_file_location(
        "security_daily_delivery_send_revision",
        revision,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0051_security_daily_delivery_send"
    assert module.down_revision == "0050_beat_scan_config"


def test_security_scan_remediation_migration_is_fail_closed_and_idempotent() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0062_security_scan_remediations.py"
    source = revision.read_text(encoding="utf-8")

    for fragment in (
        "request_hash_key_version",
        "ck_idem_request_fingerprint",
        "CREATE TABLE IF NOT EXISTS sms_resend_action",
        "INSERT INTO sms_resend_action",
        "REVOKE SELECT ON sms_batch FROM sms_callback",
        "security_daily_config_version",
        "config_version",
    ):
        assert fragment in source
    for fragment in (
        "CREATE TABLE sms_resend_action",
        "request_hash_key_version SMALLINT",
        "ck_idem_request_fingerprint",
        "ck_security_daily_request_config_version",
        "security_daily_config_version",
    ):
        assert fragment in schema

    spec = importlib.util.spec_from_file_location("security_scan_revision", revision)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0062_security_scan_remediations"
    assert module.down_revision == "0061_vendor_binding_outbox"


def test_sensitive_content_migration_encrypts_history_and_preserves_raw_status() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    compose = (ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0063_sensitive_content_and_raw_first.py"
    source = revision.read_text(encoding="utf-8")

    for fragment in (
        "display_content_enc BYTEA       NOT NULL",
        "event_key_version SMALLINT NOT NULL",
        "content_enc     BYTEA NOT NULL",
        "http_status    SMALLINT    NOT NULL DEFAULT 200",
        "content_encoding VARCHAR(16) NOT NULL DEFAULT 'identity'",
        "ck_sms_batch_content_marker",
        "ck_sms_reply_content_marker",
        "ck_reply_event_content_marker",
    ):
        assert fragment in schema
    for fragment in (
        "CryptoService.from_settings(get_settings())",
        "_backfill_batch_content(crypto)",
        "_backfill_reply_content(crypto)",
        "stable_hmac_fingerprint",
        "encrypt_bound_packed_text",
        "ALTER TABLE raw_vendor_log ADD COLUMN IF NOT EXISTS http_status",
        "content_encoding VARCHAR(16) DEFAULT 'identity'",
    ):
        assert fragment in source
    assert "'[redacted]'" not in source
    assert "SMS_COMPONENT: migrate" in compose
    migrate_service = compose.split("  migrate:", maxsplit=1)[1].split(
        "\n  api:", maxsplit=1
    )[0]
    assert "source: data_aes_key" in migrate_service
    assert "source: data_hmac_key" in migrate_service

    spec = importlib.util.spec_from_file_location("sensitive_content_revision", revision)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0063_sensitive_content_and_raw_first"
    assert module.down_revision == "0062_security_scan_remediations"


def test_sensitive_content_backfill_preserves_plaintext_only_inside_ciphertext(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = BACKEND / "migrations/versions/0063_sensitive_content_and_raw_first.py"
    spec = importlib.util.spec_from_file_location("sensitive_backfill_revision", revision)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    key = base64.b64encode(b"m" * 32).decode()
    crypto = CryptoService.from_secret_values(key, key)

    batch_connection = BackfillConnection(
        [[{"id": 7, "batch_no": "BATCH-7", "content": "验证码123456"}], []]
    )
    monkeypatch.setattr(module.op, "get_bind", lambda: batch_connection)
    module._backfill_batch_content(crypto)
    batch_update = next(
        params
        for sql, params in batch_connection.calls
        if sql.lstrip().startswith("UPDATE sms_batch")
    )
    assert batch_update is not None
    assert "content" not in batch_update
    assert batch_update["content_enc"] != "验证码123456".encode()
    assert crypto.decrypt_bound_packed_text(
        bytes(batch_update["content_enc"]),
        EncryptionContext(
            domain="sms-display-content",
            table="sms_batch",
            column="display_content_enc",
            object_id="BATCH-7",
        ),
    ) == "验证码123456"

    old_event_key = hashlib.sha256(b"legacy reply").hexdigest()
    reply_connection = BackfillConnection(
        [
            [
                {
                    "event_key": old_event_key,
                    "raw_id": None,
                    "vendor_task_id": "task-1",
                    "custom_id": None,
                    "phone_enc": b"phone-ciphertext",
                    "phone_hmac": "a" * 64,
                    "phone_mask": "138****8000",
                    "key_version": 1,
                    "ext_code": "01",
                    "content": "TD",
                    "reply_time": datetime(2026, 8, 11, tzinfo=UTC),
                    "created_at": datetime(2026, 8, 11, tzinfo=UTC),
                }
            ],
            [],
        ]
    )
    monkeypatch.setattr(module.op, "get_bind", lambda: reply_connection)
    module._backfill_reply_content(crypto)
    reply_insert = next(
        params
        for sql, params in reply_connection.calls
        if sql.lstrip().startswith("INSERT INTO reply_event")
    )
    assert reply_insert is not None
    assert "content" not in reply_insert
    assert reply_insert["content_enc"] != b"TD"
    assert reply_insert["event_key"] != old_event_key
    assert crypto.decrypt_bound_packed_text(
        bytes(reply_insert["content_enc"]),
        EncryptionContext(
            domain="reply-content",
            table="reply_event",
            column="content_enc",
            object_id=str(reply_insert["event_key"]),
        ),
    ) == "TD"
    statements = [sql.lstrip() for sql, _params in reply_connection.calls]
    assert any(sql.startswith("UPDATE sms_reply") for sql in statements)
    assert any(sql.startswith("DELETE FROM reply_event") for sql in statements)


def test_round3_migration_encrypts_templates_and_archives_metadata_before_redaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0064_security_scan_round3.py"
    source = revision.read_text(encoding="utf-8")
    for fragment in (
        "content_enc          BYTEA        NOT NULL",
        "ck_sms_template_content_marker",
        "CREATE TABLE sensitive_metadata_archive",
        "REVOKE ALL ON sensitive_metadata_archive FROM PUBLIC",
        "acceptance[:](v2[:][0-9a-f]{64}",
    ):
        assert fragment in schema
    assert "INSERT INTO sensitive_metadata_archive" in source
    assert "UPDATE {table} SET {value_column}='[redacted-phone]'" in source

    spec = importlib.util.spec_from_file_location("round3_revision", revision)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0064_security_scan_round3"
    assert module.down_revision == "0063_sensitive_content_and_raw_first"

    key = base64.b64encode(b"n" * 32).decode()
    crypto = CryptoService.from_secret_values(key, key)
    template_connection = BackfillConnection(
        [[{"id": 17, "content": "验证码{1}"}], []]
    )
    monkeypatch.setattr(module.op, "get_bind", lambda: template_connection)
    module._backfill_template_content(crypto)
    template_update = next(
        params
        for sql, params in template_connection.calls
        if sql.lstrip().startswith("UPDATE sms_template")
    )
    assert template_update is not None
    assert crypto.decrypt_bound_packed_text(
        bytes(template_update["content_enc"]),
        EncryptionContext(
            domain="sms-template-content",
            table="sms_template",
            column="content_enc",
            object_id="17",
        ),
    ) == "验证码{1}"

    metadata_connection = BackfillConnection(
        [
            [{"source_key": 7, "source_row": "7", "source_value": "联系13900139000"}],
            [],
            [],
            [],
            [],
        ]
    )
    monkeypatch.setattr(module.op, "get_bind", lambda: metadata_connection)
    module._archive_and_redact_metadata(crypto)
    archive_index = next(
        index
        for index, (sql, _params) in enumerate(metadata_connection.calls)
        if sql.lstrip().startswith("INSERT INTO sensitive_metadata_archive")
    )
    redact_index = next(
        index
        for index, (sql, _params) in enumerate(metadata_connection.calls)
        if sql.lstrip().startswith("UPDATE sms_batch")
    )
    assert archive_index < redact_index
    archive_params = metadata_connection.calls[archive_index][1]
    assert archive_params is not None
    assert "13900139000" not in str(archive_params)
    assert crypto.decrypt_bound_packed_text(
        bytes(archive_params["value_enc"]),
        EncryptionContext(
            domain="sensitive-metadata-archive",
            table="sensitive_metadata_archive",
            column="value_enc",
            object_id="sms_batch:7:remark",
        ),
    ) == "联系13900139000"


def test_round4_migration_pseudonymizes_vendor_metadata_and_guards_raw_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0065_security_scan_round4.py"
    source = revision.read_text(encoding="utf-8")
    for fragment in (
        "ck_sms_chunk_vendor_task_pseudonym",
        "ck_report_event_custom_pseudonym",
        "ck_reply_event_ext_code_redacted",
        "CREATE OR REPLACE FUNCTION enforce_raw_vendor_custom_ids()",
        "trg_raw_vendor_custom_ids",
        "GRANT UPDATE (expires_at) ON callback_authority_lease TO sms_callback",
    ):
        assert fragment in schema
        assert fragment in source

    spec = importlib.util.spec_from_file_location("round4_revision", revision)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    key = base64.b64encode(b"v" * 32).decode()
    crypto = CryptoService.from_secret_values(key, key)
    created_at = datetime(2026, 8, 11, tzinfo=UTC)
    connection = BackfillConnection(
        [
            [{"id": 1, "vendor_task_id": "task-phone-13800138000"}],
            [],
            [
                {
                    "event_key": "a" * 64,
                    "vendor_task_id": "task-1",
                    "custom_id": "otp123456",
                }
            ],
            [],
            [
                {
                    "event_key": "b" * 64,
                    "vendor_task_id": "task-2",
                    "custom_id": "contentfragment",
                }
            ],
            [],
            [{"id": 2, "created_at": created_at, "vendor_task_id": "task-2"}],
            [],
            [{"id": 3, "vendor_task_id": "task-3", "custom_id": "legacy"}],
            [],
        ]
    )
    monkeypatch.setattr(module.op, "get_bind", lambda: connection)

    module._pseudonymize_vendor_metadata(crypto)

    updates = [params for sql, params in connection.calls if sql.lstrip().startswith("UPDATE")]
    assert len(updates) == 5
    assert all(params is not None for params in updates)
    serialized = str(updates)
    for plaintext in (
        "13800138000",
        "otp123456",
        "contentfragment",
        "legacy",
    ):
        assert plaintext not in serialized
    fingerprints = [
        value
        for params in updates
        for key, value in params.items()
        if key in {"vendor_task_id", "custom_id"}
    ]
    assert len(fingerprints) == 8
    assert all(isinstance(value, str) and len(value) == 64 for value in fingerprints)


def test_security_daily_generation_source_migration_is_expand_only() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0045_security_daily_source.py"
    source = revision.read_text(encoding="utf-8")

    assert "-- v1.6.42：" in schema
    assert "generation_source  VARCHAR(8)" in schema
    assert "generation_source IN ('auto','manual')" in schema
    assert (
        "ADD COLUMN IF NOT EXISTS generation_source VARCHAR(8) NOT NULL DEFAULT 'auto'"
        in source
    )
    assert "CHECK (generation_source IN ('auto','manual'))" in source

    spec = importlib.util.spec_from_file_location(
        "security_daily_generation_source_revision",
        revision,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0045_security_daily_source"
    assert module.down_revision == "0044_security_daily_audit_view"


def test_security_daily_records_append_and_auto_daily_stays_singleton() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0046_security_daily_append.py"
    source = revision.read_text(encoding="utf-8")

    assert "-- v1.6.43：" in schema
    table = schema.split("CREATE TABLE security_daily_report", maxsplit=1)[1].split(
        ");",
        maxsplit=1,
    )[0]
    assert "report_date       DATE NOT NULL," in table
    assert "security_daily_report_report_date_key" in schema
    assert "WHERE generation_source='auto'" in schema
    assert "DROP CONSTRAINT IF EXISTS security_daily_report_report_date_key" in source
    assert "ON CONFLICT (report_date) WHERE generation_source='auto'" in (
        BACKEND / "app/services/security_daily_repository.py"
    ).read_text(encoding="utf-8")

    spec = importlib.util.spec_from_file_location(
        "security_daily_append_revision",
        revision,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0046_security_daily_append"
    assert module.down_revision == "0045_security_daily_source"


def test_all_migration_revision_ids_fit_alembic_version_column() -> None:
    revisions = sorted((BACKEND / "migrations/versions").glob("00*.py"))
    assert revisions
    for path in revisions:
        if path.name == "__init__.py":
            continue
        spec = importlib.util.spec_from_file_location(path.stem, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert isinstance(module.revision, str) and module.revision
        assert len(module.revision) <= 64, (
            f"{path.name}: revision id 超过 alembic_version VARCHAR(64) 上限"
        )


def test_export_authorization_scope_is_in_schema_and_fail_closed_migration() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0024_export_authorization_scope.py"

    assert "-- v1.6.24：" in schema
    export_table = schema.split("CREATE TABLE export_task", maxsplit=1)[1].split(
        ");",
        maxsplit=1,
    )[0]
    for field in (
        "public_id   UUID NOT NULL UNIQUE DEFAULT gen_random_uuid()",
        "creator_account_id BIGINT REFERENCES user_account(id)",
        "scope_dept  VARCHAR(128)",
        "scope_resolved BOOLEAN NOT NULL DEFAULT FALSE",
    ):
        assert field in export_table
    assert revision.is_file()

    source = revision.read_text(encoding="utf-8")
    assert 'revision = "0024_export_authorization_scope"' in source
    assert 'down_revision = "0023_vendor_uat_acceptance_lease"' in source
    assert "identity.normalized_login_name=lower(btrim(task.creator))" in source
    assert "filters ? 'scope_dept'" in source
    assert "scope_resolved=TRUE" in source
    assert "downgrade is intentionally blocked" in source


def test_security_session_projection_revision_contract() -> None:
    revision = BACKEND / "migrations/versions/0025_security_session_projection.py"
    source = revision.read_text(encoding="utf-8")

    assert revision.is_file()
    spec = importlib.util.spec_from_file_location(
        "security_session_projection_revision",
        revision,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0025_security_session_projection"
    assert module.down_revision == "0024_export_authorization_scope"
    assert "ADD COLUMN IF NOT EXISTS auth_version BIGINT" in source
    assert "ADD COLUMN IF NOT EXISTS security_version BIGINT" in source
    assert "sync_account_security_versions" in source
    assert "zz_trg_account_security_version_sync" in source
    assert "RENAME COLUMN auth_version TO security_version" not in source
    assert "DROP TRIGGER IF EXISTS trg_user_account_security_version" in source
    assert "trg_user_account_security_version" in source
    assert "trg_auth_identity_security_version" in source
    assert "trg_auth_provider_security_version" in source
    assert "trg_external_role_mapping_security_version" in source
    assert "OLD.provider_id,NEW.provider_id" in source
    with pytest.raises(RuntimeError, match="intentionally blocked"):
        module.downgrade()


def test_stable_principal_migration_replaces_legacy_self_approval_check() -> None:
    revision = BACKEND / "migrations/versions/0026_stable_principal_ids.py"
    source = revision.read_text(encoding="utf-8")

    assert "DROP CONSTRAINT IF EXISTS chk_no_self_approve" in source
    assert "ADD CONSTRAINT chk_no_self_approve CHECK" in source
    assert "chk_no_self_approve_account" not in source


def test_callback_export_fencing_is_in_schema_and_followup_migration() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0029_worker_fencing_leases.py"

    callback_table = schema.split("CREATE TABLE callback_task", maxsplit=1)[1].split(
        ");",
        maxsplit=1,
    )[0]
    export_table = schema.split("CREATE TABLE export_task", maxsplit=1)[1].split(
        ");",
        maxsplit=1,
    )[0]
    for table in (callback_table, export_table):
        assert "lease_id" in table
        assert "lease_expires_at" in table
        assert "takeover_count" in table
    assert "event_id      UUID NOT NULL DEFAULT gen_random_uuid()" in callback_table
    assert "CREATE TABLE worker_lease_event" in schema
    assert "idx_cb_lease_expiry" in schema
    assert "idx_export_lease_expiry" in schema

    spec = importlib.util.spec_from_file_location("worker_fencing_revision", revision)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0029_worker_fencing_leases"
    assert module.down_revision == "0028_usage_fact_ledger"


def test_vendor_event_facts_are_in_schema_and_followup_migration() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0030_vendor_event_facts.py"

    assert "CREATE TABLE report_event (" in schema
    assert "CREATE TABLE report_event_projection (" in schema
    assert "CREATE TABLE reply_event (" in schema
    assert "report_event_key CHAR(64)" in schema
    assert "event_key      CHAR(64)    NOT NULL" in schema
    assert "ON CONFLICT(event_key) DO NOTHING" in (
        BACKEND / "app/services/report_repository.py"
    ).read_text(encoding="utf-8")
    assert "ON CONFLICT(event_key) DO NOTHING" in (
        BACKEND / "app/services/reply_repository.py"
    ).read_text(encoding="utf-8")

    spec = importlib.util.spec_from_file_location("vendor_event_revision", revision)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0030_vendor_event_facts"
    assert module.down_revision == "0029_worker_fencing_leases"


def test_import_reservation_state_is_in_schema_and_followup_migration() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0031_import_reservation_state.py"
    repository = (BACKEND / "app/services/import_repository.py").read_text(
        encoding="utf-8"
    )
    pipeline_repository = (
        BACKEND / "app/services/pipeline_repository.py"
    ).read_text(encoding="utf-8")

    for fragment in (
        "state        VARCHAR(10) NOT NULL DEFAULT 'ready'",
        "reservation_id UUID",
        "reserved_by_account_id BIGINT",
        "reservation_expires_at TIMESTAMPTZ",
        "consumed_batch_id BIGINT",
        "payload_purged_at TIMESTAMPTZ",
        "CONSTRAINT uk_import_consumed_batch UNIQUE",
        "CONSTRAINT ck_import_task_reservation_state",
    ):
        assert fragment in schema
    assert "UPDATE import_task SET used=true" not in repository
    assert "FOR UPDATE OF t" in repository
    assert "state='consumed'" in repository
    assert "consume_import_reservation" in pipeline_repository

    spec = importlib.util.spec_from_file_location("import_reservation_revision", revision)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0031_import_reservation_state"
    assert module.down_revision == "0030_vendor_event_facts"


def test_async_import_runtime_is_in_schema_and_followup_migration() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0032_async_import_runtime.py"
    repository = (BACKEND / "app/services/import_repository.py").read_text(
        encoding="utf-8"
    )

    for fragment in (
        "parse_status VARCHAR(12) NOT NULL DEFAULT 'ready'",
        "source_file  VARCHAR(256)",
        "parse_lease_id UUID",
        "parse_lease_expires_at TIMESTAMPTZ",
        "CONSTRAINT ck_import_parse_status",
        "CONSTRAINT ck_import_parse_source",
        "CONSTRAINT ck_import_parse_lease",
        "idx_import_parse_due",
    ):
        assert fragment in schema
    assert "parse_status='processing'" in repository
    assert "parse_lease_id=:lease_id" in repository
    assert "parse_lease_expires_at>now()" in repository

    spec = importlib.util.spec_from_file_location("async_import_revision", revision)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0032_async_import_runtime"
    assert module.down_revision == "0031_import_reservation_state"


def test_database_role_matrix_covers_report_callback_producer_path() -> None:
    revision = BACKEND / "migrations/versions/0034_database_role_matrix.py"
    source = revision.read_text(encoding="utf-8")

    assert "report_event,reply_event,callback_report_event" in source
    assert "GRANT INSERT,UPDATE ON callback_task TO sms_send" in source
    assert "callback_task_id_seq" in source

    spec = importlib.util.spec_from_file_location("database_role_revision", revision)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0034_database_role_matrix"
    assert module.down_revision == "0033_correlation_audit_chain"


def test_sensitive_sys_config_rows_are_hidden_by_runtime_role_policy() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0054_sensitive_config_rls.py"
    source = revision.read_text(encoding="utf-8")

    for fragment in (
        "ALTER TABLE sys_config ENABLE ROW LEVEL SECURITY",
        "CREATE POLICY sys_config_accept_all",
        "CREATE POLICY sys_config_callback_select",
        "CREATE POLICY sys_config_nonsecret_select",
        "security_daily_resend_api_key",
        "alert_wecom_webhook",
        "FUNCTION alert_channel_availability()",
        "SECURITY DEFINER",
        "REVOKE ALL ON FUNCTION alert_channel_availability() FROM PUBLIC",
        "status,file_path,row_count,started_at,lease_id,lease_expires_at",
    ):
        assert fragment in schema
        assert fragment in source

    assert "GRANT UPDATE (" in schema
    assert "REVOKE UPDATE ON export_task FROM sms_export" in source

    spec = importlib.util.spec_from_file_location("sensitive_config_rls_revision", revision)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0054_sensitive_config_rls"
    assert module.down_revision == "0053_idempotency_scope"


def test_atomic_password_change_is_in_schema_and_followup_migration() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0035_atomic_password_change.py"
    repository = (BACKEND / "app/core/auth/users.py").read_text(encoding="utf-8")

    for fragment in (
        "CREATE TABLE password_change_token (",
        "token_hash              CHAR(64)    NOT NULL UNIQUE",
        "issued_security_version BIGINT      NOT NULL",
        "status                  VARCHAR(12) NOT NULL DEFAULT 'available'",
        "CONSTRAINT ck_password_change_consumed",
        "idx_password_change_account_available",
    ):
        assert fragment in schema
    assert "FOR UPDATE OF pct,ua" in repository
    assert "pct.issued_security_version=ua.security_version" in repository
    assert "status=CASE WHEN id=:token_id THEN 'consumed' ELSE 'revoked' END" in (
        repository
    )

    spec = importlib.util.spec_from_file_location("atomic_password_revision", revision)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0035_atomic_password_change"
    assert module.down_revision == "0034_database_role_matrix"
    source = revision.read_text(encoding="utf-8")
    assert "cannot downgrade atomic password change with retained tokens" in source
    assert "DROP TABLE IF EXISTS password_change_token" in source


def test_callback_crypto_context_is_in_schema_and_followup_migration() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0036_callback_crypto_context.py"

    for fragment in (
        "callback_secret_enc BYTEA NOT NULL",
        "callback_secret_key_version SMALLINT NOT NULL",
        "CONSTRAINT chk_callback_signature_version",
        "CHECK (signature_version = 1)",
    ):
        assert fragment in schema

    spec = importlib.util.spec_from_file_location("callback_crypto_revision", revision)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0036_callback_crypto_context"
    assert module.down_revision == "0035_atomic_password_change"
    source = revision.read_text(encoding="utf-8")
    assert "ALTER COLUMN callback_secret_enc SET NOT NULL" in source
    assert "ALTER COLUMN callback_secret_key_version SET NOT NULL" in source


def test_security_findings_hardening_migration_is_baseline_idempotent() -> None:
    """0066 必须兼容已含当前 schema.sql 对象的 0001 空库基线。"""

    revision = BACKEND / "migrations/versions/0066_security_findings_hardening.py"
    source = revision.read_text(encoding="utf-8")

    assert "ADD COLUMN IF NOT EXISTS dept" in source
    assert "conname='ck_external_role_mapping_dept'" in source
    assert "CREATE TABLE IF NOT EXISTS blacklist_hmac_alias" in source
    assert "ON UPDATE CASCADE ON DELETE CASCADE" in source


def test_security_daily_publish_outbox_migration_is_expand_only() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0079_security_daily_publish_outbox.py"
    source = revision.read_text(encoding="utf-8")

    for fragment in (
        "security_daily_config_publish_state",
        "security_daily_config_file_version",
        "security_daily_config_operation_id",
        "'unknown'",
        "ck_security_daily_request_completion",
    ):
        assert fragment in schema
        assert fragment in source

    spec = importlib.util.spec_from_file_location(
        "security_daily_publish_outbox_revision", revision
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0079_security_daily_publish_outbox"
    assert module.down_revision == "0078_system_raw_replay_audit_producer"


def test_security_daily_delivery_generation_migration_is_expand_only() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    revision = BACKEND / "migrations/versions/0080_security_daily_delivery_generation.py"
    source = revision.read_text(encoding="utf-8")

    for fragment in (
        "delivery_generation",
        "recipient_set_digest",
        "ck_security_daily_report_delivery_generation",
        "ck_security_daily_request_delivery_generation",
        "ck_security_daily_request_recipient_digest",
    ):
        assert fragment in schema
        assert fragment in source

    assert "ADD COLUMN IF NOT EXISTS delivery_generation" in source
    assert "ADD COLUMN IF NOT EXISTS recipient_set_digest" in source
    assert "DROP TABLE" not in source
    assert "DROP COLUMN" not in source
    assert "DO $$" not in source

    spec = importlib.util.spec_from_file_location(
        "security_daily_delivery_generation_revision", revision
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0080_security_daily_delivery_generation"
    assert module.down_revision == "0079_security_daily_publish_outbox"
