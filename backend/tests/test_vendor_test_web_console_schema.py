from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
REVISION = BACKEND / "migrations/versions/0017_vendor_test_web_console.py"
VENDOR_CODE_REVISION = (
    BACKEND / "migrations/versions/0018_vendor_test_operation_vendor_code.py"
)
HMAC_ALIAS_REVISION = (
    BACKEND / "migrations/versions/0019_vendor_test_recipient_hmac_alias.py"
)


def _load_revision() -> ModuleType:
    assert REVISION.is_file(), "0017 真实联调页面迁移尚未实现"
    spec = importlib.util.spec_from_file_location("vendor_test_console_revision", REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_vendor_code_revision() -> ModuleType:
    assert VENDOR_CODE_REVISION.is_file(), "0018 厂商错误码迁移尚未实现"
    spec = importlib.util.spec_from_file_location(
        "vendor_test_vendor_code_revision",
        VENDOR_CODE_REVISION,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_hmac_alias_revision() -> ModuleType:
    assert HMAC_ALIAS_REVISION.is_file(), "0019 测试号码 HMAC 别名迁移尚未实现"
    spec = importlib.util.spec_from_file_location(
        "vendor_test_hmac_alias_revision",
        HMAC_ALIAS_REVISION,
    )
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


def _table(schema: str, name: str) -> str:
    return schema.split(f"CREATE TABLE {name}", maxsplit=1)[1].split(");", maxsplit=1)[0]


def test_vendor_test_console_migration_is_linear_expand_only_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_revision()
    executed: list[str] = []
    monkeypatch.setattr(module.op, "execute", lambda statement: executed.append(str(statement)))

    module.upgrade()

    sql = "\n".join(executed)
    assert module.revision == "0017_vendor_test_web_console"
    assert module.down_revision == "0016_vendor_live_test_budget"
    assert "CREATE TABLE IF NOT EXISTS vendor_test_recipient" in sql
    assert "CREATE TABLE IF NOT EXISTS vendor_test_operation" in sql
    assert "CREATE INDEX IF NOT EXISTS idx_vendor_test_recipient_active" in sql
    assert "CREATE INDEX IF NOT EXISTS idx_vendor_test_operation_status_time" in sql
    assert "idx_vendor_test_recipient_active" in sql
    assert "idx_vendor_test_operation_status_time" in sql
    assert "DROP TABLE" not in sql.upper()
    assert "DROP COLUMN" not in sql.upper()
    assert re.search(r"(?im)^\s*TRUNCATE\s", sql) is None
    with pytest.raises(RuntimeError, match="vendor test web console"):
        module.downgrade()


def test_vendor_code_migration_is_expand_only_and_current_baseline_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_vendor_code_revision()
    executed: list[str] = []
    monkeypatch.setattr(module.op, "execute", lambda statement: executed.append(str(statement)))

    module.upgrade()

    sql = "\n".join(executed)
    assert module.revision == "0018_vendor_code"
    assert module.down_revision == "0017_vendor_test_web_console"
    assert "ADD COLUMN IF NOT EXISTS vendor_code" in sql
    assert "pg_constraint" in sql
    assert "chk_vendor_test_operation_vendor_code" in sql
    assert "DROP TABLE" not in sql.upper()
    assert "DROP COLUMN" not in sql.upper()
    with pytest.raises(RuntimeError, match="evidence downgrade"):
        module.downgrade()


def test_hmac_alias_migration_backfills_and_is_expand_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_hmac_alias_revision()
    executed: list[str] = []
    monkeypatch.setattr(module.op, "execute", lambda statement: executed.append(str(statement)))

    module.upgrade()

    sql = "\n".join(executed)
    assert module.revision == "0019_vendor_hmac_alias"
    assert module.down_revision == "0018_vendor_code"
    assert "CREATE TABLE IF NOT EXISTS vendor_test_recipient_hmac_alias" in sql
    assert "REFERENCES vendor_test_recipient(id) ON DELETE CASCADE" in sql
    assert "UNIQUE (hmac_key_version, hmac_digest)" in sql
    assert "INSERT INTO vendor_test_recipient_hmac_alias" in sql
    assert "SELECT id,key_version,phone_hmac FROM vendor_test_recipient" in sql
    assert "GRANT SELECT, INSERT, DELETE" in sql
    assert "REVOKE UPDATE, TRUNCATE" in sql
    assert "DROP TABLE" not in sql.upper()
    with pytest.raises(RuntimeError, match="hmac alias"):
        module.downgrade()


def test_vendor_test_recipient_schema_uses_only_encrypted_phone_quartet() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    recipient = _table(schema, "vendor_test_recipient")

    for required in (
        "phone_enc",
        "phone_hmac",
        "phone_mask",
        "key_version",
        "UNIQUE (key_version, phone_hmac)",
        "status IN ('active','disabled')",
        "disabled_by",
        "disabled_at",
    ):
        assert required in recipient

    declared = {
        match.group(1).casefold()
        for line in recipient.splitlines()
        if (match := re.match(r"\s*([a-z_][a-z0-9_]*)\s+", line, re.IGNORECASE))
    }
    assert not (
        {"phone", "mobile", "content", "payload", "secret", "credential", "ciphertext"}
        & declared
    )


def test_vendor_test_recipient_hmac_alias_schema_contains_only_index_projection() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    alias = _table(schema, "vendor_test_recipient_hmac_alias")

    for required in (
        "recipient_id",
        "hmac_key_version",
        "hmac_digest",
        "ON DELETE CASCADE",
        "PRIMARY KEY (recipient_id, hmac_key_version)",
        "UNIQUE (hmac_key_version, hmac_digest)",
    ):
        assert required in alias
    assert "phone_enc" not in alias and "phone_mask" not in alias
    assert "phone_hmac" not in alias
    assert re.search(r"\bkey_version\s", alias) is None


def test_vendor_test_operation_schema_contains_only_safe_metadata() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    operation = _table(schema, "vendor_test_operation")

    for required in (
        "operation_type",
        "actor",
        "status",
        "safe_code",
        "batch_no",
        "checkpoint_id",
        "requested_at",
        "completed_at",
    ):
        assert required in operation
    declared = {
        match.group(1).casefold()
        for line in operation.splitlines()
        if (match := re.match(r"\s*([a-z_][a-z0-9_]*)\s+", line, re.IGNORECASE))
    }
    assert not (
        {
            "phone",
            "mobile",
            "content",
            "payload",
            "secret",
            "credential",
            "ciphertext",
            "hmac",
        }
        & declared
    )


def test_vendor_test_console_schema_has_least_privilege_grants() -> None:
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")

    assert "vendor_test_recipient,\n    vendor_test_recipient_hmac_alias" in schema
    assert "vendor_test_send_attempt,\n    vendor_test_operation\nTO sms_accept" in schema
    assert "vendor_test_recipient_id_seq," in schema
    assert "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM sms_app" in schema
    assert "ALTER ROLE sms_app NOLOGIN" in schema


def test_migration_checker_rejects_unsafe_console_privileges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checker = _load_checker()
    monkeypatch.setattr(
        checker,
        "docker_psql",
        lambda *_args, **_kwargs: "1|1|1|1|1|1|1|1|1|1|1|1|1|1\n",
    )
    checker.verify_vendor_test_console_privileges("postgres", "database")

    monkeypatch.setattr(
        checker,
        "docker_psql",
        lambda *_args, **_kwargs: "1|1|1|1|1|0|1|1|1|1|1|1|1|1\n",
    )
    with pytest.raises(RuntimeError, match="console privileges"):
        checker.verify_vendor_test_console_privileges("postgres", "database")
