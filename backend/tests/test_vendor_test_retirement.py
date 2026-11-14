"""真实联调删表范围与迁移检查的失败关闭合同。"""

from __future__ import annotations

import importlib

import pytest

from scripts_support.check_migration import verify_vendor_test_tables_retired

RETIRED_TABLES = {
    "vendor_test_daily_usage",
    "vendor_test_send_attempt",
    "vendor_test_recipient",
    "vendor_test_recipient_hmac_alias",
    "vendor_test_operation",
}


def test_retirement_only_drops_the_five_tables_and_refuses_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("migrations.versions.0139_retire_vendor_test_tables")
    statements: list[str] = []
    monkeypatch.setattr(module.op, "execute", lambda sql: statements.append(str(sql)))

    module.upgrade()

    assert module.down_revision == "0138_unmatched_report_time"
    assert set(statements) == {
        f"DROP TABLE IF EXISTS public.{name}" for name in RETIRED_TABLES
    }
    assert len(statements) == len(RETIRED_TABLES)
    with pytest.raises(RuntimeError, match="retired vendor test data cannot be downgraded"):
        module.downgrade()


@pytest.mark.parametrize("remaining", ["1", "8", "", "invalid"])
def test_retirement_catalog_check_fails_closed(
    monkeypatch: pytest.MonkeyPatch, remaining: str,
) -> None:
    monkeypatch.setattr(
        "scripts_support.check_migration.docker_psql",
        lambda *_args, **_kwargs: "0\n",
    )
    verify_vendor_test_tables_retired("isolated-postgres", "synthetic")
    monkeypatch.setattr(
        "scripts_support.check_migration.docker_psql",
        lambda *_args, **_kwargs: remaining,
    )
    with pytest.raises(RuntimeError, match="retired vendor test relations remain"):
        verify_vendor_test_tables_retired("isolated-postgres", "synthetic")
