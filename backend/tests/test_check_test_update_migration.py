from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "scripts"))

from check_test_update_migration import (  # noqa: E402
    ExpandOnlyError,
    check_expand_only,
    find_migration_head,
)


def test_current_live_test_ledger_migration_is_expand_only() -> None:
    checked = check_expand_only(
        ROOT / "backend/migrations/versions",
        "0015_account_provider_model",
        "0016_vendor_live_test_budget",
    )

    assert [item.revision for item in checked] == ["0016_vendor_live_test_budget"]


def test_current_server_migration_train_is_expand_only() -> None:
    migration_directory = ROOT / "backend/migrations/versions"
    checked = check_expand_only(
        migration_directory,
        "0021_approval_legacy_default",
        find_migration_head(migration_directory),
    )

    assert [item.revision for item in checked] == [
        "0022_vendor_test_reset_operation",
        "0023_vendor_uat_acceptance_lease",
        "0024_export_authorization_scope",
        "0025_security_session_projection",
        "0026_stable_principal_ids",
        "0027_transactional_outbox",
        "0028_usage_fact_ledger",
        "0029_worker_fencing_leases",
        "0030_vendor_event_facts",
        "0031_import_reservation_state",
        "0032_async_import_runtime",
        "0033_correlation_audit_chain",
        "0034_database_role_matrix",
        "0035_atomic_password_change",
        "0036_callback_crypto_context",
        "0037_metrics_access_boundary",
        "0038_raw_replay_processing_lease",
        "0039_manual_job_outbox",
        "0040_background_task_role_matrix",
        "0041_security_daily_control",
        "0042_security_daily_config",
        "0043_security_daily_ui_config",
        "0044_security_daily_audit_view",
        "0045_security_daily_source",
        "0046_security_daily_append",
        "0047_widen_alembic_version",
        "0048_security_daily_audit_accept",
        "0049_app_ip_allowlist",
        "0050_beat_scan_config",
        "0051_security_daily_delivery_send",
        "0052_idempotency_request_hash",
        "0053_idempotency_scope",
        "0054_sensitive_config_rls",
        "0055_callback_revocation",
        "0056_audit_attribution_context",
        "0057_wecom_credential_encryption",
        "0058_audit_writer_enforcement",
        "0059_authenticated_audit_alert",
        "0060_audit_producer_domains",
        "0061_vendor_binding_outbox",
    ]


def test_accepts_additive_row_security_controls(tmp_path: Path) -> None:
    (tmp_path / "0001_base.py").write_text(
        "revision='0001_base'\n"
        "down_revision=None\n"
        "def upgrade():\n"
        "    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "0002_rls.py").write_text(
        "from alembic import op\n"
        "revision='0002_rls'\n"
        "down_revision='0001_base'\n"
        "def upgrade():\n"
        "    op.execute('ALTER TABLE sys_config ENABLE ROW LEVEL SECURITY')\n"
        "    op.execute('CREATE POLICY config_read ON sys_config FOR SELECT USING (true)')\n"
        "    op.execute('CREATE FUNCTION safe_config() RETURNS boolean "
        "LANGUAGE sql AS $$ SELECT true $$')\n",
        encoding="utf-8",
    )

    checked = check_expand_only(tmp_path, "0001_base", "0002_rls")

    assert [item.revision for item in checked] == ["0002_rls"]


def test_accepts_drop_not_null_as_expand_only(tmp_path: Path) -> None:
    (tmp_path / "0001_base.py").write_text(
        "revision='0001_base'\n"
        "down_revision=None\n"
        "def upgrade():\n"
        "    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "0002_widen.py").write_text(
        "from alembic import op\n"
        "revision='0002_widen'\n"
        "down_revision='0001_base'\n"
        "def upgrade():\n"
        "    op.execute('ALTER TABLE protected_data ALTER COLUMN value DROP NOT NULL')\n",
        encoding="utf-8",
    )

    checked = check_expand_only(tmp_path, "0001_base", "0002_widen")

    assert [item.revision for item in checked] == ["0002_widen"]


def test_fast_update_refuses_to_cross_destructive_0015() -> None:
    with pytest.raises(ExpandOnlyError, match="0015_account_provider_model.*DROP TABLE"):
        check_expand_only(
            ROOT / "backend/migrations/versions",
            "0014_security_hardening",
            "0016_vendor_live_test_budget",
        )


@pytest.mark.parametrize(
    "statement",
    [
        "DROP TABLE protected_data",
        "TRUNCATE TABLE protected_data",
        "DELETE FROM protected_data",
        "ALTER TABLE protected_data DROP COLUMN value",
        "ALTER TABLE protected_data ALTER COLUMN value TYPE bigint",
        "ALTER TABLE protected_data RENAME COLUMN value TO old_value",
    ],
)
def test_rejects_destructive_raw_sql(tmp_path: Path, statement: str) -> None:
    migration = tmp_path / "0002_bad.py"
    migration.write_text(
        "from alembic import op\n"
        "revision='0002_bad'\n"
        "down_revision='0001_base'\n"
        "def upgrade():\n"
        f"    op.execute({statement!r})\n"
        "def downgrade():\n"
        "    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "0001_base.py").write_text(
        "revision='0001_base'\n"
        "down_revision=None\n"
        "def upgrade():\n"
        "    pass\n",
        encoding="utf-8",
    )

    with pytest.raises(ExpandOnlyError, match="destructive SQL"):
        check_expand_only(tmp_path, "0001_base", "0002_bad")


def test_rejects_raw_sql_that_cannot_be_statically_confirmed(tmp_path: Path) -> None:
    (tmp_path / "0001_base.py").write_text(
        "revision='0001_base'\n"
        "down_revision=None\n"
        "def upgrade():\n"
        "    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "0002_dynamic.py").write_text(
        "from alembic import op\n"
        "revision='0002_dynamic'\n"
        "down_revision='0001_base'\n"
        "SQL='CREATE TABLE unsafe(id bigint)'\n"
        "def upgrade():\n"
        "    op.execute(SQL)\n",
        encoding="utf-8",
    )

    with pytest.raises(ExpandOnlyError, match="statically confirm"):
        check_expand_only(tmp_path, "0001_base", "0002_dynamic")


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE protected_data SET value=1; DROP TRIGGER trg_guard ON protected_data",
        (
            "DO $$ BEGIN DROP TRIGGER trg_guard ON protected_data; "
            "ALTER TABLE protected_data ADD COLUMN value bigint; END $$"
        ),
    ],
)
def test_rejects_destructive_sql_hidden_after_allowed_prefix(
    tmp_path: Path,
    statement: str,
) -> None:
    (tmp_path / "0001_base.py").write_text(
        "revision='0001_base'\n"
        "down_revision=None\n"
        "def upgrade():\n"
        "    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "0002_bad.py").write_text(
        "from alembic import op\n"
        "revision='0002_bad'\n"
        "down_revision='0001_base'\n"
        "def upgrade():\n"
        f"    op.execute({statement!r})\n",
        encoding="utf-8",
    )

    with pytest.raises(ExpandOnlyError):
        check_expand_only(tmp_path, "0001_base", "0002_bad")


@pytest.mark.parametrize(
    "statement,error",
    [
        (
            "DROP TRIGGER IF EXISTS trg_guard ON protected_data",
            "trigger drop is not replaced",
        ),
        (
            "ALTER TABLE protected_data "
            "DROP CONSTRAINT IF EXISTS ck_protected_data",
            "constraint drop is not replaced",
        ),
    ],
)
def test_rejects_unpaired_control_replacement(
    tmp_path: Path,
    statement: str,
    error: str,
) -> None:
    (tmp_path / "0001_base.py").write_text(
        "revision='0001_base'\n"
        "down_revision=None\n"
        "def upgrade():\n"
        "    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "0002_bad.py").write_text(
        "from alembic import op\n"
        "revision='0002_bad'\n"
        "down_revision='0001_base'\n"
        "def upgrade():\n"
        f"    op.execute({statement!r})\n",
        encoding="utf-8",
    )

    with pytest.raises(ExpandOnlyError, match=error):
        check_expand_only(tmp_path, "0001_base", "0002_bad")


def test_accepts_constraint_drop_replaced_by_same_named_unique_index(
    tmp_path: Path,
) -> None:
    (tmp_path / "0001_base.py").write_text(
        "revision='0001_base'\n"
        "down_revision=None\n"
        "def upgrade():\n"
        "    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "0002_replacement.py").write_text(
        "from alembic import op\n"
        "revision='0002_replacement'\n"
        "down_revision='0001_base'\n"
        "def upgrade():\n"
        "    op.execute('ALTER TABLE protected_data '\n"
        "               'DROP CONSTRAINT IF EXISTS uq_protected_date')\n"
        "    op.execute('CREATE UNIQUE INDEX uq_protected_date '\n"
        "               'ON protected_data(report_date) WHERE kind=\\'auto\\'')\n"
        "def downgrade():\n"
        "    pass\n",
        encoding="utf-8",
    )

    checked = check_expand_only(tmp_path, "0001_base", "0002_replacement")

    assert [item.revision for item in checked] == ["0002_replacement"]


def test_accepts_alembic_version_widening_raw_sql(tmp_path: Path) -> None:
    (tmp_path / "0001_base.py").write_text(
        "revision='0001_base'\n"
        "down_revision=None\n"
        "def upgrade():\n"
        "    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "0002_widen.py").write_text(
        "from alembic import op\n"
        "revision='0002_widen'\n"
        "down_revision='0001_base'\n"
        "def upgrade():\n"
        "    op.execute('ALTER TABLE alembic_version '\n"
        "               'ALTER COLUMN version_num TYPE VARCHAR(64)')\n"
        "def downgrade():\n"
        "    pass\n",
        encoding="utf-8",
    )

    checked = check_expand_only(tmp_path, "0001_base", "0002_widen")

    assert [item.revision for item in checked] == ["0002_widen"]


def test_rejects_downgrade_direction() -> None:
    with pytest.raises(ExpandOnlyError, match="downgrade"):
        check_expand_only(
            ROOT / "backend/migrations/versions",
            "0016_vendor_live_test_budget",
            "0015_account_provider_model",
        )
