from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import app.core.runtime_resources as runtime_resources

BACKEND = Path(__file__).resolve().parents[1]


def load_checker_module() -> Any:
    path = BACKEND / "scripts_support/check_migration.py"
    assert path.is_file(), "迁移一致性检查器尚未实现"
    spec = importlib.util.spec_from_file_location("migration_checker", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_catalog_query_covers_required_structure_sets() -> None:
    module = load_checker_module()
    query = module.CATALOG_QUERY.lower()

    for token in (
        "information_schema.tables",
        "information_schema.columns",
        "pg_indexes",
        "pg_constraint",
    ):
        assert token in query
    assert "alembic_version" in query


def test_compare_catalogs_reports_directional_difference() -> None:
    module = load_checker_module()

    module.compare_catalogs(("T|app", "C|app|id"), ("C|app|id", "T|app"))
    with pytest.raises(RuntimeError, match="only in schema.sql"):
        module.compare_catalogs(("T|app", "I|app|idx"), ("T|app",))


def test_async_checks_close_shared_resources_before_event_loop_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_checker_module()
    events: list[str] = []

    async def operation() -> int:
        events.append("operation")
        return 7

    async def close_resources() -> None:
        events.append("close")

    monkeypatch.setattr(
        runtime_resources,
        "close_runtime_resources",
        close_resources,
    )

    assert module.run_async_check(operation()) == 7
    assert events == ["operation", "close"]


def test_security_alignment_migration_supports_current_schema_baseline() -> None:
    source = (BACKEND / "migrations/versions/0014_security_alignment_hardening.py").read_text(
        encoding="utf-8"
    )

    assert 'revision: str = "0014_security_hardening"' in source
    assert "ADD COLUMN IF NOT EXISTS" in source
    assert "DROP COLUMN IF EXISTS auth_version" in source


def test_account_provider_migration_is_idempotent_for_current_schema_baseline() -> None:
    source = (BACKEND / "migrations/versions/0015_account_provider_model.py").read_text(
        encoding="utf-8"
    )

    assert 'revision: str = "0015_account_provider_model"' in source
    assert 'down_revision: str | None = "0014_security_hardening"' in source
    assert "DROP TABLE IF EXISTS role_mapping" in source
    assert "DROP TABLE IF EXISTS sys_user" in source
    for table in (
        "user_account",
        "auth_provider",
        "auth_identity",
        "local_credential",
        "external_role_mapping",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in source
    assert "ON CONFLICT (code) DO NOTHING" in source


def test_approval_threshold_snapshot_keeps_legacy_writer_compatible() -> None:
    baseline = (
        BACKEND / "migrations/versions/0020_approval_threshold_snapshot.py"
    ).read_text(encoding="utf-8")
    migration = (
        BACKEND / "migrations/versions/0021_approval_legacy_writer_default.py"
    ).read_text(
        encoding="utf-8",
    )
    schema = (BACKEND.parent / "schema.sql").read_text(encoding="utf-8")
    repository = (BACKEND / "app/services/pipeline_repository.py").read_text(
        encoding="utf-8"
    )
    checker = (BACKEND / "scripts_support/check_migration.py").read_text(
        encoding="utf-8"
    )

    assert "DEFAULT 'snapshot'" in baseline
    assert 'revision = "0021_approval_legacy_default"' in migration
    assert 'down_revision = "0020_approval_threshold"' in migration
    assert "ALTER COLUMN trigger_threshold_source" in migration
    assert "SET DEFAULT 'legacy_unknown'" in migration
    assert "DEFAULT 'legacy_unknown'" in schema
    assert "trigger_threshold_source" in repository
    assert "'snapshot'" in repository
    assert "legacy_approval_build" in checker
    assert '"upgrade", "0020_approval_threshold"' in checker
    assert "INSERT INTO approval(batch_id,applicant,dept,expires_at)" in checker


def test_retired_user_migrations_noop_after_latest_schema_baseline() -> None:
    for revision in (
        "0006_user_directory_snapshot.py",
        "0014_security_alignment_hardening.py",
    ):
        source = (BACKEND / "migrations/versions" / revision).read_text(encoding="utf-8")
        assert source.count("ALTER TABLE IF EXISTS sys_user") >= 2
        assert "DROP COLUMN IF EXISTS" in source

    checker = (BACKEND / "scripts_support/check_migration.py").read_text(encoding="utf-8")
    assert "table_name='user_account'" in checker
    assert "ALTER TABLE sys_user DROP COLUMN" not in checker


def test_wait_ignores_temporary_init_server_until_entrypoint_is_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_checker_module()
    log_outputs = iter(
        (
            "database system is ready to accept connections",
            "PostgreSQL init process complete; ready for start up.",
        )
    )
    calls: list[list[str]] = []

    def fake_run(command: Sequence[str], **_kwargs: object) -> SimpleNamespace:
        argv = list(command)
        calls.append(argv)
        if argv[1] == "logs":
            return SimpleNamespace(returncode=0, stdout=next(log_outputs), stderr="")
        if argv[1] == "inspect":
            return SimpleNamespace(returncode=0, stdout="true|0\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(module, "run", fake_run)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    module.wait_for_postgres("temporary-db")

    assert [call[1] for call in calls] == ["logs", "inspect", "logs", "exec"]


def test_wait_reports_logs_when_postgres_exits_during_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_checker_module()
    calls: list[list[str]] = []

    def fake_run(command: Sequence[str], **_kwargs: object) -> SimpleNamespace:
        argv = list(command)
        calls.append(argv)
        if argv[1] == "logs":
            return SimpleNamespace(
                returncode=0,
                stdout="",
                stderr="cat: /run/secrets/db_owner_password: Permission denied",
            )
        if argv[1] == "inspect":
            return SimpleNamespace(returncode=0, stdout="false|1\n", stderr="")
        raise AssertionError(argv)

    monkeypatch.setattr(module, "run", fake_run)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="db_owner_password: Permission denied"):
        module.wait_for_postgres("temporary-db")

    assert [call[1] for call in calls] == ["logs", "inspect"]


def test_wait_reports_last_logs_when_postgres_never_completes_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_checker_module()

    def fake_run(command: Sequence[str], **_kwargs: object) -> SimpleNamespace:
        argv = list(command)
        if argv[1] == "logs":
            return SimpleNamespace(
                returncode=0,
                stdout="database system is ready to accept connections",
                stderr="",
            )
        if argv[1] == "inspect":
            return SimpleNamespace(returncode=0, stdout="true|0\n", stderr="")
        raise AssertionError(argv)

    monkeypatch.setattr(module, "run", fake_run)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    with pytest.raises(
        RuntimeError,
        match="database system is ready to accept connections",
    ):
        module.wait_for_postgres("temporary-db")


def test_start_postgres_retains_failed_container_until_final_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = load_checker_module()
    owner_secret = tmp_path / "db_owner_password"
    data_aes_secret = tmp_path / "data_aes_key"
    data_hmac_secret = tmp_path / "data_hmac_key"
    init_script = tmp_path / "01-create-app-role.sh"
    for path in (owner_secret, data_aes_secret, data_hmac_secret, init_script):
        path.write_text("test", encoding="utf-8")

    monkeypatch.setattr(module, "OWNER_SECRET", owner_secret)
    monkeypatch.setattr(module, "DATA_AES_SECRET", data_aes_secret)
    monkeypatch.setattr(module, "DATA_HMAC_SECRET", data_hmac_secret)
    monkeypatch.setattr(module, "INIT_SCRIPT", init_script)
    monkeypatch.setattr(module, "wait_for_postgres", lambda _container: None)
    commands: list[list[str]] = []

    def fake_run(command: Sequence[str], **_kwargs: object) -> SimpleNamespace:
        argv = list(command)
        commands.append(argv)
        if argv[1] == "port":
            return SimpleNamespace(returncode=0, stdout="127.0.0.1:54321\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="container-id\n", stderr="")

    monkeypatch.setattr(module, "run", fake_run)

    assert module.start_postgres("temporary-db") == 54321
    docker_run = commands[0]
    assert docker_run[:3] == ["docker", "run", "-d"]
    assert "--rm" not in docker_run
    assert docker_run[docker_run.index("--entrypoint") : docker_run.index("--entrypoint") + 2] == [
        "--entrypoint",
        "/bin/sh",
    ]
    assert any(
        value.endswith("db_owner_password:/run/source-secrets/db_owner_password:ro")
        for value in docker_run
    )
    assert not any("db_app_password" in value for value in docker_run)
    stage_command = docker_run[-1]
    assert "cp /run/source-secrets/db_owner_password /run/secrets/db_owner_password" in (
        stage_command
    )
    assert "chown postgres:postgres /run/secrets/db_owner_password" in stage_command
    assert "chmod 0400 /run/secrets/db_owner_password" in stage_command
    assert "exec /usr/local/bin/docker-entrypoint.sh postgres" in stage_command
