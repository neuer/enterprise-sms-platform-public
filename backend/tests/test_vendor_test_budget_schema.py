from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
REVISION = BACKEND / "migrations/versions/0016_vendor_live_test_budget.py"


def _load_revision() -> ModuleType:
    assert REVISION.is_file(), "0016 真实联调每日额度迁移尚未实现"
    spec = importlib.util.spec_from_file_location("vendor_test_budget_revision", REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_checker() -> ModuleType:
    checker = BACKEND / "scripts_support/check_migration.py"
    spec = importlib.util.spec_from_file_location("migration_checker", checker)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_vendor_test_budget_migration_is_linear_expand_only_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_revision()
    executed: list[str] = []
    monkeypatch.setattr(module.op, "execute", lambda statement: executed.append(str(statement)))

    module.upgrade()

    sql = "\n".join(executed)
    assert module.revision == "0016_vendor_live_test_budget"
    assert module.down_revision == "0015_account_provider_model"
    assert "ADD COLUMN IF NOT EXISTS vendor_attempt_count" in sql
    assert "CREATE TABLE IF NOT EXISTS vendor_test_daily_usage" in sql
    assert "CREATE TABLE IF NOT EXISTS vendor_test_send_attempt" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uk_vendor_test_reserved_chunk" in sql
    assert "DROP TABLE" not in sql.upper()
    assert "DROP COLUMN" not in sql.upper()
    assert re.search(r"(?im)^\s*TRUNCATE\s", sql) is None
    with pytest.raises(RuntimeError, match="vendor live test budget"):
        module.downgrade()


def test_vendor_test_budget_schema_has_database_limit_and_no_pii_fields() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    usage = schema.split("CREATE TABLE vendor_test_daily_usage", maxsplit=1)[1].split(
        ");", maxsplit=1
    )[0]
    attempts = schema.split("CREATE TABLE vendor_test_send_attempt", maxsplit=1)[1].split(
        ");", maxsplit=1
    )[0]

    assert "in_flight_segments" in usage
    assert "confirmed_segments" in usage
    assert "uncertain_segments" in usage
    assert "<= 100" in usage
    assert "usage_date" in attempts
    assert "chunk_id" in attempts
    assert "attempt_no" in attempts
    assert "segments" in attempts
    assert "reserved" in attempts
    assert "confirmed" in attempts
    assert "uncertain" in attempts
    assert "released" in attempts
    forbidden = ("phone", "mobile", "content", "secret", "payload", "credential")
    assert all(token not in usage.casefold() for token in forbidden)
    assert all(token not in attempts.casefold() for token in forbidden)


def test_vendor_test_budget_schema_preserves_evidence_permissions() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")

    assert "vendor_test_daily_usage, vendor_test_send_attempt" in schema
    assert "vendor_test_send_attempt, vendor_test_operation\nTO sms_send" in schema
    assert "vendor_test_send_attempt_id_seq," in schema
    assert "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM sms_app" in schema


def test_vendor_test_attempt_constraints_bind_one_reserved_attempt_per_chunk() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")

    assert "UNIQUE (chunk_id, attempt_no)" in schema
    assert "uk_vendor_test_reserved_chunk" in schema
    assert "WHERE status = 'reserved'" in schema
    assert "chk_vendor_test_attempt_settlement" in schema


def test_migration_checker_rejects_unsafe_budget_ledger_privileges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checker = _load_checker()
    monkeypatch.setattr(checker, "docker_psql", lambda *_args, **_kwargs: "1|1|1|1|1|1|1|1\n")
    checker.verify_vendor_test_budget_privileges("postgres", "database")

    monkeypatch.setattr(checker, "docker_psql", lambda *_args, **_kwargs: "1|1|1|0|1|1|1|1\n")
    with pytest.raises(RuntimeError, match="budget ledger privileges"):
        checker.verify_vendor_test_budget_privileges("postgres", "database")
