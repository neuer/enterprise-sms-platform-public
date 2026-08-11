#!/usr/bin/env python3
"""在临时 PostgreSQL 16 中比较 schema.sql 与 Alembic 建库结构。"""

from __future__ import annotations

import asyncio
import importlib
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Awaitable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))
SECRETS_DIR = Path(
    os.environ.get(
        "SMS_MIGRATION_CHECK_SECRETS_DIR",
        str(ROOT / "deploy/secrets"),
    )
)
OWNER_SECRET = SECRETS_DIR / "db_owner_password"
INIT_SCRIPT = ROOT / "deploy/initdb/01-create-app-role.sh"

CATALOG_QUERY = r"""
SELECT row_value FROM (
  SELECT 'T|' || table_name AS row_value
  FROM information_schema.tables
  WHERE table_schema = 'public' AND table_name <> 'alembic_version'

  UNION ALL
  SELECT concat_ws('|', 'C', table_name, column_name,
                   data_type, udt_name, is_nullable, coalesce(column_default, ''))
  FROM information_schema.columns
  WHERE table_schema = 'public' AND table_name <> 'alembic_version'

  UNION ALL
  SELECT concat_ws('|', 'I', tablename, indexname, indexdef)
  FROM pg_indexes
  WHERE schemaname = 'public' AND tablename <> 'alembic_version'

  UNION ALL
  SELECT concat_ws('|', 'K', rel.relname, con.conname, con.contype::text,
                   pg_get_constraintdef(con.oid, true))
  FROM pg_constraint con
  JOIN pg_class rel ON rel.oid = con.conrelid
  JOIN pg_namespace ns ON ns.oid = rel.relnamespace
  WHERE ns.nspname = 'public' AND rel.relname <> 'alembic_version'
) catalog
ORDER BY row_value;
"""


def run(
    command: Sequence[str],
    *,
    input_text: str | None = None,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """运行无 shell 命令，避免密码插值与命令注入。"""

    return subprocess.run(
        command,
        input=input_text,
        cwd=cwd,
        env=env,
        check=check,
        text=True,
        capture_output=True,
    )


async def _await_with_runtime_shutdown[T](operation: Awaitable[T]) -> T:
    """隔离脚本内事件循环，禁止共享连接跨 asyncio.run 泄漏。"""

    from app.core.runtime_resources import close_runtime_resources  # noqa: PLC0415

    try:
        return await operation
    finally:
        await close_runtime_resources()


def run_async_check[T](operation: Awaitable[T]) -> T:
    """执行单个异步检查，并在事件循环关闭前释放共享运行资源。"""

    return asyncio.run(_await_with_runtime_shutdown(operation))


def compare_catalogs(schema_rows: Sequence[str], alembic_rows: Sequence[str]) -> None:
    """比较四类结构集合并输出双向差异。"""

    schema_set = set(schema_rows)
    alembic_set = set(alembic_rows)
    only_schema = sorted(schema_set - alembic_set)
    only_alembic = sorted(alembic_set - schema_set)
    if not only_schema and not only_alembic:
        return
    details: list[str] = []
    if only_schema:
        details.append("only in schema.sql:\n  " + "\n  ".join(only_schema))
    if only_alembic:
        details.append("only in alembic:\n  " + "\n  ".join(only_alembic))
    raise RuntimeError("migration structure mismatch\n" + "\n".join(details))


def docker_psql(container: str, database: str, sql: str, *, stdin: str | None = None) -> str:
    command = [
        "docker",
        "exec",
        "-i",
        container,
        "psql",
        "-U",
        "sms_owner",
        "-d",
        database,
        "-v",
        "ON_ERROR_STOP=1",
        "-At",
    ]
    if stdin is None:
        command.extend(["-c", sql])
    result = run(command, input_text=stdin)
    return result.stdout


def wait_for_postgres(container: str) -> None:
    """等待 entrypoint 初始化结束后的正式数据库就绪，最多 60 秒。"""

    log_output = ""
    state_output = "unknown"
    for _attempt in range(60):
        logs = run(["docker", "logs", container], check=False)
        log_output = logs.stdout + logs.stderr
        initialized = "PostgreSQL init process complete; ready for start up." in log_output
        if not initialized:
            state = run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{.State.Running}}|{{.State.ExitCode}}",
                    container,
                ],
                check=False,
            )
            state_output = state.stdout.strip() or state_output
            if state.returncode == 0 and state.stdout.strip().startswith("false|"):
                detail = log_output.strip()[-2000:] or "no container logs"
                raise RuntimeError(
                    f"temporary PostgreSQL exited during initialization "
                    f"({state.stdout.strip()}):\n{detail}"
                )
            time.sleep(1)
            continue
        result = run(
            ["docker", "exec", container, "pg_isready", "-U", "sms_owner", "-d", "postgres"],
            check=False,
        )
        if result.returncode == 0:
            return
        time.sleep(1)
    detail = log_output.strip()[-2000:] or "no container logs"
    raise RuntimeError(f"temporary PostgreSQL did not become ready ({state_output}):\n{detail}")


def start_postgres(container: str) -> int:
    """以 secrets 文件启动隔离 PostgreSQL 并返回随机主机端口。"""

    for required in (OWNER_SECRET, INIT_SCRIPT):
        if not required.is_file():
            raise RuntimeError(f"required migration-check asset missing: {required}")
    run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container,
            "-p",
            "127.0.0.1::5432",
            "-e",
            "POSTGRES_DB=postgres",
            "-e",
            "POSTGRES_USER=sms_owner",
            "-e",
            "POSTGRES_PASSWORD_FILE=/run/secrets/db_owner_password",
            "-v",
            f"{OWNER_SECRET}:/run/source-secrets/db_owner_password:ro",
            "-v",
            f"{INIT_SCRIPT}:/docker-entrypoint-initdb.d/01-create-app-role.sh:ro",
            "--entrypoint",
            "/bin/sh",
            "postgres:16",
            "-ec",
            """
mkdir -p /run/secrets
cp /run/source-secrets/db_owner_password /run/secrets/db_owner_password
chown postgres:postgres /run/secrets/db_owner_password
chmod 0400 /run/secrets/db_owner_password
exec /usr/local/bin/docker-entrypoint.sh postgres
""".strip(),
        ]
    )
    wait_for_postgres(container)
    mapping = run(["docker", "port", container, "5432/tcp"]).stdout.strip()
    return int(mapping.rsplit(":", 1)[1])


def catalog(container: str, database: str) -> tuple[str, ...]:
    output = docker_psql(container, database, CATALOG_QUERY)
    return tuple(line for line in output.splitlines() if line)


def verify_vendor_test_budget_privileges(container: str, database: str) -> None:
    """验证发送角色只能写入账本，不能删除证据或取得 owner 权限。"""

    result = docker_psql(
        container,
        database,
        """
        SELECT
          (has_table_privilege('sms_send','vendor_test_daily_usage','SELECT')
           AND has_table_privilege('sms_send','vendor_test_send_attempt','SELECT'))::int,
          (has_table_privilege('sms_send','vendor_test_daily_usage','INSERT')
           AND has_table_privilege('sms_send','vendor_test_send_attempt','INSERT'))::int,
          (has_table_privilege('sms_send','vendor_test_daily_usage','UPDATE')
           AND has_table_privilege('sms_send','vendor_test_send_attempt','UPDATE'))::int,
          (NOT has_table_privilege('sms_send','vendor_test_daily_usage','DELETE')
           AND NOT has_table_privilege('sms_send','vendor_test_send_attempt','DELETE'))::int,
          (NOT has_table_privilege('sms_send','vendor_test_daily_usage','TRUNCATE')
           AND NOT has_table_privilege('sms_send','vendor_test_send_attempt','TRUNCATE'))::int,
          (SELECT NOT rolsuper FROM pg_roles WHERE rolname='sms_send')::int,
          (NOT pg_has_role('sms_send','sms_owner','MEMBER'))::int,
          COALESCE((
            SELECT count(*)=2 AND bool_and(tableowner='sms_owner')
            FROM pg_tables
            WHERE schemaname='public'
              AND tablename IN ('vendor_test_daily_usage','vendor_test_send_attempt')
          ),FALSE)::int
        """,
    ).strip()
    if result != "1|1|1|1|1|1|1|1":
        raise RuntimeError("vendor live test budget ledger privileges are unsafe")


def verify_vendor_test_console_privileges(container: str, database: str) -> None:
    """验证受理角色的联调号码最小 DML 与操作证据不可删除。"""

    result = docker_psql(
        container,
        database,
        """
        SELECT
          has_table_privilege('sms_accept','vendor_test_recipient','SELECT')::int,
          (has_table_privilege('sms_accept','vendor_test_recipient','INSERT')
           AND has_table_privilege('sms_accept','vendor_test_recipient','UPDATE')
           AND has_table_privilege('sms_accept','vendor_test_recipient','DELETE'))::int,
          (NOT has_table_privilege('sms_accept','vendor_test_recipient','TRUNCATE'))::int,
          has_table_privilege('sms_accept','vendor_test_operation','SELECT')::int,
          (has_table_privilege('sms_accept','vendor_test_operation','INSERT')
           AND has_table_privilege('sms_accept','vendor_test_operation','UPDATE'))::int,
          (NOT has_table_privilege('sms_accept','vendor_test_operation','DELETE'))::int,
          (NOT has_table_privilege('sms_accept','vendor_test_operation','TRUNCATE'))::int,
          has_table_privilege(
            'sms_accept','vendor_test_recipient_hmac_alias','SELECT'
          )::int,
          (has_table_privilege(
             'sms_accept','vendor_test_recipient_hmac_alias','INSERT'
           ) AND has_table_privilege(
             'sms_accept','vendor_test_recipient_hmac_alias','DELETE'
           ))::int,
          (NOT has_table_privilege(
             'sms_accept','vendor_test_recipient_hmac_alias','UPDATE'
           ) AND NOT has_table_privilege(
             'sms_accept','vendor_test_recipient_hmac_alias','TRUNCATE'
           ))::int,
          (has_sequence_privilege('sms_accept','vendor_test_recipient_id_seq','USAGE')
           AND has_sequence_privilege('sms_accept','vendor_test_recipient_id_seq','SELECT'))::int,
          (SELECT NOT rolsuper FROM pg_roles WHERE rolname='sms_accept')::int,
          (NOT pg_has_role('sms_accept','sms_owner','MEMBER'))::int,
          COALESCE((
            SELECT count(*)=3 AND bool_and(tableowner='sms_owner')
            FROM pg_tables
            WHERE schemaname='public'
              AND tablename IN (
                'vendor_test_recipient',
                'vendor_test_recipient_hmac_alias',
                'vendor_test_operation'
              )
          ),FALSE)::int
        """,
    ).strip()
    if result != "1|1|1|1|1|1|1|1|1|1|1|1|1|1":
        raise RuntimeError("vendor test web console privileges are unsafe")


def verify_outbox_privileges(container: str, database: str) -> None:
    """验证调度角色可推进 outbox 但不能删除、截断或取得 owner 权限。"""

    result = docker_psql(
        container,
        database,
        """
        SELECT
          has_table_privilege('sms_scheduler','outbox_event','SELECT')::int,
          has_table_privilege('sms_scheduler','outbox_event','INSERT')::int,
          has_table_privilege('sms_scheduler','outbox_event','UPDATE')::int,
          (NOT has_table_privilege('sms_scheduler','outbox_event','DELETE'))::int,
          (NOT has_table_privilege('sms_scheduler','outbox_event','TRUNCATE'))::int,
          (SELECT tableowner='sms_owner' FROM pg_tables
           WHERE schemaname='public' AND tablename='outbox_event')::int,
          (SELECT NOT rolsuper FROM pg_roles WHERE rolname='sms_scheduler')::int,
          (NOT pg_has_role('sms_scheduler','sms_owner','MEMBER'))::int
        """,
    ).strip()
    if result != "1|1|1|1|1|1|1|1":
        raise RuntimeError("transactional outbox privileges are unsafe")


def verify_audit_payload_guard(container: str, database: str) -> None:
    """数据库层必须拒绝手机号、密文/HMAC 列表、token、secret 与请求正文。"""

    docker_psql(
        container,
        database,
        """
        DO $guard$
        DECLARE
          payload jsonb;
          blocked boolean;
        BEGIN
          FOREACH payload IN ARRAY ARRAY[
            '{"phone":"13800138000"}'::jsonb,
            '{"phone_enc":["ciphertext"]}'::jsonb,
            '{"phone_hmac":["digest"]}'::jsonb,
            '{"access_token":"value"}'::jsonb,
            '{"callback_secret":"value"}'::jsonb,
            '{"request_body":{"safe":true}}'::jsonb
          ] LOOP
            blocked := false;
            BEGIN
              INSERT INTO audit_log(actor,action,after_val)
              VALUES ('migration-guard','payload_guard_probe',payload);
            EXCEPTION WHEN check_violation THEN
              blocked := true;
            END;
            IF NOT blocked THEN
              RAISE EXCEPTION 'audit payload guard accepted forbidden value: %',
                payload;
            END IF;
          END LOOP;
        END
        $guard$;
        """,
    )


def verify_audit_context_forgery_rejected(container: str, database: str) -> None:
    """业务角色即使可写自定义 GUC，也不能伪造认证主体。"""

    docker_psql(
        container,
        database,
        """
        INSERT INTO audit_context_signing_key(key_kind,key_material)
        VALUES
          ('principal',decode(repeat('11',32),'hex')),
          ('system:api',decode(repeat('33',32),'hex')),
          ('system:realtime',decode(repeat('22',32),'hex')),
          ('system:bulk',decode(repeat('44',32),'hex'))
        ON CONFLICT(key_kind) DO UPDATE SET key_material=EXCLUDED.key_material;
        INSERT INTO app(name,dept,api_key_hash,api_key_prefix,created_by)
        VALUES(
          'audit-forgery-probe','security',repeat('a',64),'forgery0','migration-check'
        ) ON CONFLICT(name) DO NOTHING;
        """,
    )
    statement = """
BEGIN;
SET SESSION AUTHORIZATION sms_accept;
SELECT set_config('sms.correlation_id','30000000-0000-4000-8000-000000000099',true);
SELECT set_config('sms.audit_subject_kind','api_app',true);
SELECT set_config('sms.audit_actor_name','forged-application',true);
SELECT set_config('sms.audit_account_id','',true);
SELECT set_config('sms.audit_identity_id','',true);
SELECT set_config(
  'sms.audit_app_id',
  (SELECT id::text FROM app WHERE name='audit-forgery-probe'),
  true
);
SELECT set_config('sms.audit_context_signature',repeat('0',64),true);
INSERT INTO audit_log(
  actor,actor_subject_kind,actor_app_id,action,object_type,object_id
) SELECT
  'forged-application','api_app',id,'config_update','sys_config','vendor_qps'
FROM app WHERE name='audit-forgery-probe';
COMMIT;
"""
    result = run(
        [
            "docker",
            "exec",
            container,
            "psql",
            "-U",
            "sms_owner",
            "-d",
            database,
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            statement,
        ],
        check=False,
    )
    output = result.stdout + result.stderr
    if result.returncode == 0 or "audit context signature is invalid" not in output:
        raise RuntimeError("runtime role forged authenticated audit attribution")

    system_statement = """
BEGIN;
SET SESSION AUTHORIZATION sms_send;
SELECT set_config('sms.correlation_id','30000000-0000-4000-8000-000000000098',true);
SELECT set_config('sms.audit_subject_kind','system',true);
SELECT set_config('sms.audit_actor_name','vendor-state-sync',true);
SELECT set_config('sms.audit_account_id','',true);
SELECT set_config('sms.audit_identity_id','',true);
SELECT set_config('sms.audit_app_id','',true);
SELECT set_config('sms.audit_producer_domain','realtime',true);
SELECT set_config('sms.audit_action','template_sync',true);
SELECT set_config('sms.audit_context_signature',repeat('0',64),true);
INSERT INTO audit_log(
  actor,actor_subject_kind,role,action,object_type,object_id
) VALUES(
  'vendor-state-sync','system','system','template_sync','template','1'
);
COMMIT;
"""
    result = run(
        [
            "docker",
            "exec",
            container,
            "psql",
            "-U",
            "sms_owner",
            "-d",
            database,
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            system_statement,
        ],
        check=False,
    )
    output = result.stdout + result.stderr
    if result.returncode == 0 or "system audit context signature is invalid" not in output:
        raise RuntimeError("runtime role forged authenticated system audit event")


def verify_legitimate_audit_signatures(port: int, database: str) -> None:
    """真实 PostgreSQL 必须与 Python 的长主体/系统动作规范化逐字一致。"""

    async def verify() -> None:
        from sqlalchemy import text  # noqa: PLC0415
        from sqlalchemy.engine import URL  # noqa: PLC0415
        from sqlalchemy.ext.asyncio import create_async_engine  # noqa: PLC0415

        from app.core.audit_context import (  # noqa: PLC0415
            sign_audit_context,
            sign_system_audit_context,
        )

        password = OWNER_SECRET.read_text(encoding="utf-8").rstrip("\r\n")
        database_url = URL.create(
            "postgresql+asyncpg",
            username="sms_owner",
            password=password,
            host="127.0.0.1",
            port=port,
            database=database,
        )
        engine = create_async_engine(database_url, hide_parameters=True)
        try:
            async with engine.begin() as connection:
                await connection.execute(text("SET SESSION AUTHORIZATION sms_accept"))
                identity = (
                    await connection.execute(
                        text(
                            "SELECT current_user AS database_user,"
                            "txid_current() AS txid"
                        )
                    )
                ).mappings().one()
                app_id = int(
                    (
                        await connection.execute(
                            text(
                                "SELECT id FROM app "
                                "WHERE name='audit-forgery-probe'"
                            )
                        )
                    ).scalar_one()
                )
                correlation_id = "30000000-0000-4000-8000-000000000097"
                actor_name = "中" * 64
                signature = sign_audit_context(
                    bytes.fromhex("11" * 32),
                    txid=int(identity["txid"]),
                    database_user=str(identity["database_user"]),
                    correlation_id=correlation_id,
                    subject_kind="api_app",
                    actor_name=actor_name,
                    account_id="",
                    identity_id="",
                    app_id=str(app_id),
                )
                await connection.execute(
                    text(
                        """
                        SELECT
                          set_config('sms.correlation_id',:correlation,TRUE),
                          set_config('sms.audit_subject_kind','api_app',TRUE),
                          set_config('sms.audit_actor_name',:actor,TRUE),
                          set_config('sms.audit_account_id','',TRUE),
                          set_config('sms.audit_identity_id','',TRUE),
                          set_config('sms.audit_app_id',:app_id,TRUE),
                          set_config('sms.audit_action','',TRUE),
                          set_config('sms.audit_context_signature',:signature,TRUE)
                        """
                    ),
                    {
                        "correlation": correlation_id,
                        "actor": actor_name,
                        "app_id": str(app_id),
                        "signature": signature,
                    },
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO audit_log(
                          actor,actor_subject_kind,actor_app_id,action,
                          object_type,object_id
                        ) VALUES(
                          :actor,'api_app',:app_id,'config_update',
                          'sys_config','long-actor-probe'
                        )
                        """
                    ),
                    {"actor": actor_name, "app_id": app_id},
                )
                await connection.execute(text("RESET SESSION AUTHORIZATION"))

            async with engine.begin() as connection:
                await connection.execute(text("SET SESSION AUTHORIZATION sms_send"))
                identity = (
                    await connection.execute(
                        text(
                            "SELECT current_user AS database_user,"
                            "txid_current() AS txid"
                        )
                    )
                ).mappings().one()
                correlation_id = "30000000-0000-4000-8000-000000000096"
                signature = sign_system_audit_context(
                    bytes.fromhex("22" * 32),
                    txid=int(identity["txid"]),
                    database_user=str(identity["database_user"]),
                    correlation_id=correlation_id,
                    producer_domain="realtime",
                    actor_name="vendor-state-sync",
                    action="template_sync",
                )
                await connection.execute(
                    text(
                        """
                        SELECT
                          set_config('sms.correlation_id',:correlation,TRUE),
                          set_config('sms.audit_subject_kind','system',TRUE),
                          set_config('sms.audit_actor_name','vendor-state-sync',TRUE),
                          set_config('sms.audit_account_id','',TRUE),
                          set_config('sms.audit_identity_id','',TRUE),
                          set_config('sms.audit_app_id','',TRUE),
                          set_config('sms.audit_producer_domain','realtime',TRUE),
                          set_config('sms.audit_action','template_sync',TRUE),
                          set_config('sms.audit_context_signature',:signature,TRUE)
                        """
                    ),
                    {"correlation": correlation_id, "signature": signature},
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO audit_log(
                          actor,actor_subject_kind,role,action,object_type,object_id
                        ) VALUES(
                          'vendor-state-sync','system','system','template_sync',
                          'template','signed-system-probe'
                        )
                        """
                    )
                )
                await connection.execute(text("RESET SESSION AUTHORIZATION"))

            cross_domain_rejected = False
            try:
                async with engine.begin() as connection:
                    await connection.execute(text("SET SESSION AUTHORIZATION sms_send"))
                    identity = (
                        await connection.execute(
                            text(
                                "SELECT current_user AS database_user,"
                                "txid_current() AS txid"
                            )
                        )
                    ).mappings().one()
                    correlation_id = "30000000-0000-4000-8000-000000000095"
                    signature = sign_system_audit_context(
                        bytes.fromhex("22" * 32),
                        txid=int(identity["txid"]),
                        database_user=str(identity["database_user"]),
                        correlation_id=correlation_id,
                        producer_domain="bulk",
                        actor_name="import-parser",
                        action="message_import",
                    )
                    await connection.execute(
                        text(
                            """
                            SELECT
                              set_config('sms.correlation_id',:correlation,TRUE),
                              set_config('sms.audit_subject_kind','system',TRUE),
                              set_config('sms.audit_actor_name','import-parser',TRUE),
                              set_config('sms.audit_account_id','',TRUE),
                              set_config('sms.audit_identity_id','',TRUE),
                              set_config('sms.audit_app_id','',TRUE),
                              set_config('sms.audit_producer_domain','bulk',TRUE),
                              set_config('sms.audit_action','message_import',TRUE),
                              set_config('sms.audit_context_signature',:signature,TRUE)
                            """
                        ),
                        {"correlation": correlation_id, "signature": signature},
                    )
                    await connection.execute(
                        text(
                            """
                            INSERT INTO audit_log(
                              actor,actor_subject_kind,role,action,object_type,object_id
                            ) VALUES(
                              'import-parser','system','system','message_import',
                              'import_task','cross-domain-probe'
                            )
                            """
                        )
                    )
            except Exception as error:
                if "system audit context signature is invalid" not in str(error):
                    raise
                cross_domain_rejected = True
            if not cross_domain_rejected:
                raise RuntimeError("realtime audit key forged a bulk producer event")
        finally:
            await engine.dispose()

    asyncio.run(verify())


def verify_worker_lease_privileges(container: str, database: str) -> None:
    """租约事实只允许发送角色读取/追加，禁止篡改或清空。"""

    result = docker_psql(
        container,
        database,
        """
        SELECT
          has_table_privilege('sms_send','worker_lease_event','SELECT')::int,
          has_table_privilege('sms_send','worker_lease_event','INSERT')::int,
          (NOT has_table_privilege(
            'sms_send','worker_lease_event','UPDATE'
          ))::int,
          (NOT has_table_privilege(
            'sms_send','worker_lease_event','DELETE'
          ))::int,
          (NOT has_table_privilege(
            'sms_send','worker_lease_event','TRUNCATE'
          ))::int,
          has_sequence_privilege(
            'sms_send','worker_lease_event_id_seq','USAGE'
          )::int,
          (SELECT tableowner='sms_owner' FROM pg_tables
           WHERE schemaname='public' AND tablename='worker_lease_event')::int
        """,
    ).strip()
    if result != "1|1|1|1|1|1|1":
        raise RuntimeError("worker lease evidence privileges are unsafe")


def verify_vendor_event_privileges(container: str, database: str) -> None:
    """发送角色对厂商事实只允许追加；投影可更新但不可删除。"""

    result = docker_psql(
        container,
        database,
        """
        SELECT
          has_table_privilege('sms_send','report_event','SELECT')::int,
          has_table_privilege('sms_send','report_event','INSERT')::int,
          (NOT has_table_privilege('sms_send','report_event','UPDATE'))::int,
          (NOT has_table_privilege('sms_send','report_event','DELETE'))::int,
          (NOT has_table_privilege('sms_send','report_event','TRUNCATE'))::int,
          has_table_privilege('sms_send','reply_event','SELECT')::int,
          has_table_privilege('sms_send','reply_event','INSERT')::int,
          (NOT has_table_privilege('sms_send','reply_event','UPDATE'))::int,
          (NOT has_table_privilege('sms_send','reply_event','DELETE'))::int,
          (NOT has_table_privilege('sms_send','reply_event','TRUNCATE'))::int,
          has_table_privilege(
            'sms_send','report_event_projection','UPDATE'
          )::int,
          (NOT has_table_privilege(
            'sms_send','report_event_projection','DELETE'
          ))::int,
          (NOT has_table_privilege(
            'sms_send','report_event_projection','TRUNCATE'
          ))::int
        """,
    ).strip()
    if result != "1|1|1|1|1|1|1|1|1|1|1|1|1":
        raise RuntimeError("vendor event fact privileges are unsafe")


def verify_callback_revocation_boundary(container: str, database: str) -> None:
    """API 角色不能改回调任务，但 app 撤销触发器必须同事务生效。"""

    function_boundary = docker_psql(
        container,
        database,
        """
        SELECT
          (owner.rolname='sms_owner')::int,
          proc.prosecdef::int,
          (NOT has_function_privilege(
            'sms_accept',
            'revoke_callback_tasks_on_app_change()',
            'EXECUTE'
          ))::int
        FROM pg_proc proc
        JOIN pg_roles owner ON owner.oid=proc.proowner
        WHERE proc.oid='revoke_callback_tasks_on_app_change()'::regprocedure
        """,
    ).strip()
    if function_boundary != "1|1|1":
        raise RuntimeError(
            f"callback revocation function boundary is unsafe: {function_boundary}"
        )
    docker_psql(
        container,
        database,
        """
        INSERT INTO app(
          name,dept,api_key_hash,api_key_prefix,callback_url,
          callback_secret_enc,callback_report_enabled,created_by
        ) VALUES(
          'migration-callback-revoke','migration',repeat('a',64),'aaaaaaaa',
          'https://callback-old.internal/hook',decode('0001aa','hex'),true,
          'migration-check'
        );
        INSERT INTO callback_task(
          app_id,event,url,callback_secret_enc,callback_secret_key_version
        ) SELECT
          id,'message.report',callback_url,callback_secret_enc,1
        FROM app WHERE name='migration-callback-revoke';
        """,
    )
    assert_role_sql_denied(
        container,
        database,
        "sms_accept",
        "UPDATE callback_task SET status=status",
    )
    docker_psql(
        container,
        database,
        """
        SET ROLE sms_accept;
        UPDATE app SET callback_url='https://callback-new.internal/hook'
        WHERE name='migration-callback-revoke';
        RESET ROLE;
        """,
    )
    revoked = docker_psql(
        container,
        database,
        """
        SELECT status||'|'||last_error||'|'||
          (lease_id IS NULL)::int||'|'||(lease_expires_at IS NULL)::int
        FROM callback_task
        WHERE app_id=(
          SELECT id FROM app WHERE name='migration-callback-revoke'
        )
        """,
    ).strip()
    if revoked != "dead|CallbackConfigRevoked|1|1":
        raise RuntimeError(f"callback revocation trigger is unsafe: {revoked}")
    docker_psql(
        container,
        database,
        """
        DELETE FROM callback_task WHERE app_id=(
          SELECT id FROM app WHERE name='migration-callback-revoke'
        );
        DELETE FROM app WHERE name='migration-callback-revoke';
        """,
    )


def assert_role_sql_denied(
    container: str,
    database: str,
    role: str,
    sql: str,
) -> None:
    """用真实 PostgreSQL 执行越权语句，必须由权限系统拒绝。"""

    result = run(
        [
            "docker",
            "exec",
            container,
            "psql",
            "-U",
            "sms_owner",
            "-d",
            database,
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            f"SET ROLE {role}; {sql}",
        ],
        check=False,
    )
    if result.returncode == 0 or "permission denied" not in (result.stdout + result.stderr):
        raise RuntimeError(f"{role} unexpectedly executed forbidden database operation")


def verify_runtime_role_matrix(container: str, database: str) -> None:
    """验证七角色精确边界、审计只增、未来表默认拒绝和旧角色停用。"""

    result = docker_psql(
        container,
        database,
        """
        WITH runtime(role_name) AS (
          VALUES
            ('sms_auth'),('sms_accept'),('sms_send'),('sms_callback'),
            ('sms_export'),('sms_scheduler'),('sms_metrics')
        )
        SELECT
          (SELECT count(*)=7 AND bool_and(
             NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole
             AND NOT rolreplication AND NOT rolinherit
           )
           FROM runtime JOIN pg_roles ON rolname=role_name)::int,
          (SELECT bool_and(
             has_table_privilege(role_name,'audit_log','INSERT')
             AND NOT has_table_privilege(role_name,'audit_log','UPDATE')
             AND NOT has_table_privilege(role_name,'audit_log','DELETE')
             AND NOT has_table_privilege(role_name,'audit_log','TRUNCATE')
           ) FROM runtime WHERE role_name <> 'sms_metrics')::int,
          (NOT has_table_privilege('sms_metrics','audit_log','INSERT')
           AND NOT has_table_privilege('sms_metrics','audit_log','UPDATE')
           AND NOT has_table_privilege('sms_metrics','audit_log','DELETE')
           AND NOT has_table_privilege('sms_metrics','audit_log','TRUNCATE'))::int,
          (NOT has_table_privilege('sms_callback','user_account','UPDATE'))::int,
          (NOT has_table_privilege('sms_callback','auth_provider','UPDATE'))::int,
          (NOT has_table_privilege('sms_callback','app','UPDATE'))::int,
          (NOT has_table_privilege('sms_callback','sys_config','UPDATE'))::int,
          (SELECT bool_and(
             NOT has_table_privilege(
               role_name,'audit_context_signing_key','SELECT'
             )
           ) FROM runtime)::int,
          (has_table_privilege('sms_auth','password_change_token','SELECT')
           AND has_table_privilege('sms_auth','password_change_token','INSERT')
           AND has_table_privilege('sms_auth','password_change_token','UPDATE')
           AND NOT has_table_privilege('sms_auth','password_change_token','DELETE')
           AND NOT has_table_privilege('sms_auth','password_change_token','TRUNCATE')
           AND has_sequence_privilege(
             'sms_auth','password_change_token_id_seq','USAGE'))::int,
          has_table_privilege('sms_callback','callback_task','UPDATE')::int,
          (has_table_privilege('sms_send','callback_task','INSERT')
           AND has_table_privilege('sms_send','callback_task','UPDATE')
           AND has_table_privilege('sms_send','callback_report_event','INSERT')
           AND has_sequence_privilege('sms_send','callback_task_id_seq','USAGE'))::int,
          (has_column_privilege(
             'sms_send','sms_template','vendor_template_id','UPDATE')
           AND has_column_privilege(
             'sms_send','sms_template','vendor_state','UPDATE')
           AND has_column_privilege(
             'sms_send','sms_template','vendor_reject_reason','UPDATE')
           AND has_column_privilege(
             'sms_send','sms_template','updated_at','UPDATE')
           AND NOT has_column_privilege(
             'sms_send','sms_template','content','UPDATE')
           AND NOT has_column_privilege(
             'sms_send','sms_template','var_specs','UPDATE')
           AND has_column_privilege(
             'sms_send','sms_sign','vendor_sign_id','UPDATE')
           AND has_column_privilege(
             'sms_send','sms_sign','vendor_state','UPDATE')
           AND has_column_privilege(
             'sms_send','sms_sign','vendor_reject_reason','UPDATE')
           AND NOT has_column_privilege(
             'sms_send','sms_sign','name','UPDATE')
           AND NOT has_table_privilege('sms_send','sms_template','INSERT')
           AND NOT has_table_privilege('sms_send','sms_template','DELETE')
           AND NOT has_table_privilege('sms_send','sms_template','TRUNCATE')
           AND NOT has_table_privilege('sms_send','sms_sign','INSERT')
           AND NOT has_table_privilege('sms_send','sms_sign','DELETE')
           AND NOT has_table_privilege('sms_send','sms_sign','TRUNCATE'))::int,
          (has_column_privilege('sms_export','export_task','status','UPDATE')
           AND has_column_privilege('sms_export','export_task','file_path','UPDATE')
           AND has_column_privilege('sms_export','export_task','lease_id','UPDATE')
           AND has_column_privilege(
             'sms_export','export_task','lease_expires_at','UPDATE'
           )
           AND NOT has_column_privilege(
             'sms_export','export_task','creator_account_id','UPDATE'
           )
           AND NOT has_column_privilege(
             'sms_export','export_task','creator_identity_id','UPDATE'
           )
           AND NOT has_column_privilege(
             'sms_export','export_task','scope_dept','UPDATE'
           )
           AND NOT has_column_privilege(
             'sms_export','export_task','scope_resolved','UPDATE'
           ))::int,
          has_table_privilege('sms_scheduler','job_run','UPDATE')::int,
          (has_column_privilege('sms_metrics','sms_batch','category','SELECT')
           AND has_column_privilege('sms_metrics','sms_chunk','status','SELECT')
           AND has_column_privilege('sms_metrics','outbox_event','queue','SELECT')
           AND has_column_privilege('sms_metrics','outbox_event','state','SELECT')
           AND has_column_privilege(
             'sms_metrics','callback_task','lease_expires_at','SELECT'
           )
           AND NOT has_column_privilege(
             'sms_metrics','sms_batch','batch_no','SELECT'
           )
           AND NOT has_column_privilege(
             'sms_metrics','sms_chunk','custom_id','SELECT'
           )
           AND NOT has_column_privilege(
             'sms_metrics','outbox_event','dedup_key','SELECT'
           ))::int,
          (NOT EXISTS (
             SELECT 1 FROM information_schema.role_table_grants
             WHERE grantee='sms_metrics' AND privilege_type <> 'SELECT'
          ))::int,
          (has_table_privilege('sms_accept','alembic_version','SELECT')
           AND has_table_privilege('sms_accept','callback_task','INSERT')
           AND has_table_privilege('sms_accept','alert_log','INSERT')
           AND has_sequence_privilege('sms_accept','alert_log_id_seq','USAGE'))::int,
          (has_table_privilege('sms_callback','job_run','SELECT')
           AND has_table_privilege('sms_callback','job_run','INSERT')
           AND has_table_privilege('sms_callback','job_run','UPDATE')
           AND NOT has_table_privilege('sms_callback','job_run','DELETE')
           AND NOT has_table_privilege('sms_callback','job_run','TRUNCATE'))::int,
          (SELECT NOT rolcanlogin FROM pg_roles WHERE rolname='sms_app')::int,
          (NOT EXISTS (
             SELECT 1 FROM information_schema.role_table_grants
             WHERE grantee='sms_app'
           ))::int
        """,
    ).strip()
    if result != "1|1|1|1|1|1|1|1|1|1|1|1|1|1|1|1|1|1|1|1":
        raise RuntimeError(f"database runtime role matrix is unsafe: {result}")

    docker_psql(
        container,
        database,
        "CREATE TABLE role_matrix_future_probe(id bigint PRIMARY KEY)",
    )
    future = docker_psql(
        container,
        database,
        """
        SELECT (NOT EXISTS (
          SELECT 1 FROM information_schema.role_table_grants
          WHERE table_schema='public'
            AND table_name='role_matrix_future_probe'
            AND grantee IN (
              'sms_auth','sms_accept','sms_send','sms_callback',
              'sms_export','sms_scheduler','sms_metrics','sms_app'
            )
        ))::int
        """,
    ).strip()
    docker_psql(container, database, "DROP TABLE role_matrix_future_probe")
    if future != "1":
        raise RuntimeError("future table inherited runtime privileges")

    docker_psql(
        container,
        database,
        """
        SET ROLE sms_metrics;
        SELECT
          (SELECT count(status) FROM sms_chunk)
          +(SELECT count(lease_id) FROM callback_task)
          +(SELECT count(lease_id) FROM export_task)
          +(SELECT count(event_type) FROM worker_lease_event)
          +(SELECT count(job_name) FROM job_run)
          +(SELECT count(kind) FROM usage_projection_drift)
          +(SELECT count(category) FROM sms_batch)
        """,
    )
    assert_role_sql_denied(
        container,
        database,
        "sms_callback",
        "UPDATE user_account SET display_name=display_name",
    )
    assert_role_sql_denied(
        container,
        database,
        "sms_metrics",
        "INSERT INTO job_run(job_name,status) VALUES('forbidden','running')",
    )
    assert_role_sql_denied(
        container,
        database,
        "sms_metrics",
        "SELECT batch_no FROM sms_batch LIMIT 1",
    )
    assert_role_sql_denied(
        container,
        database,
        "sms_export",
        "UPDATE export_task SET creator_account_id=creator_account_id",
    )
    assert_role_sql_denied(
        container,
        database,
        "sms_send",
        "UPDATE sms_template SET content=content",
    )
    assert_role_sql_denied(
        container,
        database,
        "sms_send",
        "UPDATE sms_sign SET name=name",
    )
    for role in (
        "sms_auth",
        "sms_accept",
        "sms_send",
        "sms_callback",
        "sms_export",
        "sms_scheduler",
        "sms_metrics",
    ):
        assert_role_sql_denied(
            container,
            database,
            role,
            "UPDATE audit_log SET actor=actor",
        )


def run_check() -> None:
    """分别构建两个空库并比较表、列、索引和约束。"""

    from app.services.callback_repository import report_event_key  # noqa: PLC0415

    container = f"sms-migration-check-{uuid4().hex[:12]}"
    try:
        port = start_postgres(container)
        docker_psql(container, "postgres", "CREATE DATABASE schema_build")
        docker_psql(container, "postgres", "CREATE DATABASE alembic_build")
        docker_psql(container, "postgres", "CREATE DATABASE legacy_build")
        docker_psql(container, "postgres", "CREATE DATABASE legacy_single_build")
        docker_psql(container, "postgres", "CREATE DATABASE legacy_auth_build")
        docker_psql(container, "postgres", "CREATE DATABASE legacy_approval_build")
        docker_psql(
            container,
            "schema_build",
            "CREATE TABLE alembic_version(version_num varchar(64) NOT NULL)",
        )
        schema_sql = (ROOT / "schema.sql").read_text(encoding="utf-8")
        docker_psql(container, "schema_build", "", stdin=schema_sql)

        alembic = shutil.which("alembic")
        if alembic is None:
            raise RuntimeError("alembic executable not found; run via uv run")
        migration_env = dict(os.environ)
        migration_env.update(
            {
                "ENVIRONMENT": "test",
                "DEBUG": "1",
                "AUTH_MOCK": "1",
                "VENDOR_MOCK": "1",
                "DB_HOST": "127.0.0.1",
                "DB_PORT": str(port),
                "DB_NAME": "alembic_build",
                "DB_OWNER_PASSWORD_FILE": str(OWNER_SECRET),
            }
        )
        run(
            [alembic, "-c", str(BACKEND / "alembic.ini"), "upgrade", "head"],
            cwd=BACKEND,
            env=migration_env,
        )
        head_revision = docker_psql(
            container,
            "alembic_build",
            "SELECT version_num FROM alembic_version",
        ).strip()
        compare_catalogs(catalog(container, "schema_build"), catalog(container, "alembic_build"))
        verify_audit_payload_guard(container, "schema_build")
        verify_audit_payload_guard(container, "alembic_build")
        verify_audit_context_forgery_rejected(container, "schema_build")
        verify_audit_context_forgery_rejected(container, "alembic_build")
        verify_legitimate_audit_signatures(port, "schema_build")
        verify_legitimate_audit_signatures(port, "alembic_build")
        verify_runtime_role_matrix(container, "schema_build")
        verify_runtime_role_matrix(container, "alembic_build")
        for database in ("schema_build", "alembic_build"):
            verify_vendor_test_budget_privileges(container, database)
            verify_vendor_test_console_privileges(container, database)
            verify_outbox_privileges(container, database)
            verify_worker_lease_privileges(container, database)
            verify_vendor_event_privileges(container, database)
            verify_callback_revocation_boundary(container, database)
        docker_psql(
            container,
            "alembic_build",
            """
            INSERT INTO outbox_event(
              id,dedup_key,event_type,aggregate_type,aggregate_id,
              task_name,queue,args
            ) VALUES(
              gen_random_uuid(),'migration-check:unfinished','batch.ready',
              'sms_batch','MIGRATION-CHECK','app.tasks.send.process_batch',
              'realtime','[]'::jsonb
            )
            """,
        )
        blocked_outbox_downgrade = run(
            [
                alembic,
                "-c",
                str(BACKEND / "alembic.ini"),
                "downgrade",
                "0026_stable_principal_ids",
            ],
            cwd=BACKEND,
            env=migration_env,
            check=False,
        )
        blocked_output = (
            blocked_outbox_downgrade.stdout + blocked_outbox_downgrade.stderr
        )
        if (
            blocked_outbox_downgrade.returncode == 0
            or "cannot downgrade transactional outbox with unfinished events"
            not in blocked_output
        ):
            raise RuntimeError("unfinished outbox downgrade was not rejected")
        preserved_outbox = docker_psql(
            container,
            "alembic_build",
            """
            SELECT
              (to_regclass('public.outbox_event') IS NOT NULL)::int,
              (to_regclass('public.worker_lease_event') IS NOT NULL)::int,
              (SELECT version_num FROM alembic_version)
            """,
        ).strip()
        if preserved_outbox != f"1|1|{head_revision}":
            raise RuntimeError(
                f"failed outbox downgrade damaged current schema: {preserved_outbox}"
            )
        docker_psql(
            container,
            "alembic_build",
            "DELETE FROM outbox_event WHERE dedup_key='migration-check:unfinished'",
        )
        run(
            [
                alembic,
                "-c",
                str(BACKEND / "alembic.ini"),
                "downgrade",
                "0026_stable_principal_ids",
            ],
            cwd=BACKEND,
            env=migration_env,
        )
        missing_outbox = docker_psql(
            container,
            "alembic_build",
            "SELECT (to_regclass('public.outbox_event') IS NULL)::int",
        ).strip()
        if missing_outbox != "1":
            raise RuntimeError("empty outbox downgrade did not remove expand table")
        run(
            [alembic, "-c", str(BACKEND / "alembic.ini"), "upgrade", "head"],
            cwd=BACKEND,
            env=migration_env,
        )
        compare_catalogs(
            catalog(container, "schema_build"),
            catalog(container, "alembic_build"),
        )

        approval_env = dict(migration_env)
        approval_env["DB_NAME"] = "legacy_approval_build"
        run(
            [alembic, "-c", str(BACKEND / "alembic.ini"), "upgrade", "0020_approval_threshold"],
            cwd=BACKEND,
            env=approval_env,
        )
        # 首迁移从当前 schema.sql 建空库；这里显式复原 c66b3e7 已落地的 0020 默认值，
        # 才能验证持久数据库从旧 revision 升级，而不是只验证当前空库。
        docker_psql(
            container,
            "legacy_approval_build",
            "ALTER TABLE approval ALTER COLUMN trigger_threshold_source "
            "SET DEFAULT 'snapshot'",
        )
        baseline_approval_default = docker_psql(
            container,
            "legacy_approval_build",
            "SELECT (column_default LIKE '''snapshot''%')::int "
            "FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='approval' "
            "AND column_name='trigger_threshold_source'",
        ).strip()
        if baseline_approval_default != "1":
            raise RuntimeError("0020 approval source baseline default drifted")
        run(
            [alembic, "-c", str(BACKEND / "alembic.ini"), "upgrade", "head"],
            cwd=BACKEND,
            env=approval_env,
        )
        docker_psql(
            container,
            "legacy_approval_build",
            "INSERT INTO sms_batch(batch_no,channel,dept,content,send_content_enc) "
            "VALUES(repeat('l',32),'web','legacy','compatibility',decode('00','hex'))",
        )
        docker_psql(
            container,
            "legacy_approval_build",
            "INSERT INTO approval(batch_id,applicant,dept,expires_at) "
            "SELECT id,'legacy-writer','legacy',now()+interval '1 hour' "
            "FROM sms_batch WHERE batch_no=repeat('l',32)",
        )
        legacy_approval = docker_psql(
            container,
            "legacy_approval_build",
            "SELECT trigger_threshold_source||'|'||"
            "coalesce(trigger_threshold::text,'NULL') FROM approval",
        ).strip()
        if legacy_approval != "legacy_unknown|NULL":
            raise RuntimeError("0021 did not preserve legacy approval writer compatibility")

        auth_env = dict(migration_env)
        auth_env["DB_NAME"] = "legacy_auth_build"
        run(
            [alembic, "-c", str(BACKEND / "alembic.ini"), "upgrade", "0012_callback_event_keys"],
            cwd=BACKEND,
            env=auth_env,
        )
        docker_psql(
            container,
            "legacy_auth_build",
            "DELETE FROM sys_config WHERE key IN ('login_fail_limit','login_lock_minutes')",
        )
        run(
            [alembic, "-c", str(BACKEND / "alembic.ini"), "upgrade", "0013_auth_runtime_config"],
            cwd=BACKEND,
            env=auth_env,
        )
        inserted_auth = docker_psql(
            container,
            "legacy_auth_build",
            "SELECT string_agg(key||'='||value,',' ORDER BY key) FROM sys_config "
            "WHERE key IN ('login_fail_limit','login_lock_minutes')",
        ).strip()
        if inserted_auth != "login_fail_limit=5,login_lock_minutes=15":
            raise RuntimeError("0013 did not insert default auth runtime config")
        run(
            [alembic, "-c", str(BACKEND / "alembic.ini"), "downgrade", "0012_callback_event_keys"],
            cwd=BACKEND,
            env=auth_env,
        )
        docker_psql(
            container,
            "legacy_auth_build",
            "INSERT INTO sys_config(key,value,value_type) VALUES('login_fail_limit','9','int')",
        )
        run(
            [alembic, "-c", str(BACKEND / "alembic.ini"), "upgrade", "0013_auth_runtime_config"],
            cwd=BACKEND,
            env=auth_env,
        )
        preserved_auth = docker_psql(
            container,
            "legacy_auth_build",
            "SELECT string_agg(key||'='||value,',' ORDER BY key) FROM sys_config "
            "WHERE key IN ('login_fail_limit','login_lock_minutes')",
        ).strip()
        if preserved_auth != "login_fail_limit=9,login_lock_minutes=15":
            raise RuntimeError("0013 overwrote custom auth runtime config")
        run(
            [alembic, "-c", str(BACKEND / "alembic.ini"), "downgrade", "0012_callback_event_keys"],
            cwd=BACKEND,
            env=auth_env,
        )
        remaining_auth = docker_psql(
            container,
            "legacy_auth_build",
            "SELECT count(*) FROM sys_config "
            "WHERE key IN ('login_fail_limit','login_lock_minutes')",
        ).strip()
        if remaining_auth != "0":
            raise RuntimeError("0013 downgrade left auth runtime config")

        run(
            [alembic, "-c", str(BACKEND / "alembic.ini"), "upgrade", "0013_auth_runtime_config"],
            cwd=BACKEND,
            env=auth_env,
        )
        run(
            [
                alembic,
                "-c",
                str(BACKEND / "alembic.ini"),
                "upgrade",
                "0014_security_hardening",
            ],
            cwd=BACKEND,
            env=auth_env,
        )
        session_version_default = docker_psql(
            container,
            "legacy_auth_build",
            "SELECT string_agg(column_name||'='||column_default,',' ORDER BY column_name) "
            "FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='user_account' "
            "AND column_name IN ('auth_version','security_version')",
        ).strip()
        if session_version_default != "auth_version=1,security_version=1":
            raise RuntimeError("account model session version defaults are not synchronized")
        run(
            [alembic, "-c", str(BACKEND / "alembic.ini"), "downgrade", "0013_auth_runtime_config"],
            cwd=BACKEND,
            env=auth_env,
        )
        retired_user_table = docker_psql(
            container,
            "legacy_auth_build",
            "SELECT (to_regclass('public.sys_user') IS NULL)::int",
        ).strip()
        if retired_user_table != "1":
            raise RuntimeError("retired sys_user unexpectedly exists in current baseline")

        async def verify_concurrent_config_updates() -> None:
            from sqlalchemy import text  # noqa: PLC0415
            from sqlalchemy.engine import URL  # noqa: PLC0415
            from sqlalchemy.exc import DBAPIError  # noqa: PLC0415
            from sqlalchemy.ext.asyncio import create_async_engine  # noqa: PLC0415

            from app.core.auth.accounts import SecurityPrincipal  # noqa: PLC0415
            from app.core.auth.principal_context import (  # noqa: PLC0415
                audit_principal_scope,
            )
            from app.core.auth.users import SqlUserRepository  # noqa: PLC0415
            from app.core.correlation import correlation_scope  # noqa: PLC0415
            from app.services.admin import ConfigUpdate, InvalidAdminQuery  # noqa: PLC0415
            from app.services.admin_repository import SqlAdminRepository  # noqa: PLC0415
            from app.services.sensitive_repository import (  # noqa: PLC0415
                SqlSensitiveWordRepository,
            )

            password = OWNER_SECRET.read_text(encoding="utf-8").rstrip("\r\n")
            database_url = URL.create(
                "postgresql+asyncpg",
                username="sms_owner",
                password=password,
                host="127.0.0.1",
                port=port,
                database="alembic_build",
            )
            repository = SqlAdminRepository(
                cast(
                    Any,
                    SimpleNamespace(
                        database_url=database_url,
                        database_url_for=lambda _role: database_url,
                        alert_smtp_allowed_host_set=frozenset({"smtp"}),
                    ),
                )
            )
            engine = create_async_engine(database_url, hide_parameters=True)
            principals: list[SecurityPrincipal] = []
            async with engine.begin() as connection:
                provider_id = int(
                    (
                        await connection.execute(
                            text("SELECT id FROM auth_provider WHERE code='local'")
                        )
                    ).scalar_one()
                )
                for login in ("admin-a", "admin-b"):
                    account_id = int(
                        (
                            await connection.execute(
                                text(
                                    """
                                    INSERT INTO user_account(display_name,dept,role)
                                    VALUES(:login,'平台部','admin') RETURNING id
                                    """
                                ),
                                {"login": login},
                            )
                        ).scalar_one()
                    )
                    identity_id = int(
                        (
                            await connection.execute(
                                text(
                                    """
                                    INSERT INTO auth_identity(
                                      account_id,provider_id,login_name,
                                      normalized_login_name,external_subject
                                    ) VALUES(
                                      :account_id,:provider_id,:login,:login,
                                      :external_subject
                                    ) RETURNING id
                                    """
                                ),
                                {
                                    "account_id": account_id,
                                    "provider_id": provider_id,
                                    "login": login,
                                    "external_subject": f"local:{login}",
                                },
                            )
                        ).scalar_one()
                    )
                    principals.append(
                        SecurityPrincipal(
                            account_id,
                            identity_id,
                            login,
                            "平台部",
                            "admin",
                        )
                    )
                await connection.execute(
                    text(
                        """
                        INSERT INTO local_credential(
                          identity_id,password_hash,must_change_password
                        ) VALUES(:identity_id,'old-audit-probe-hash',FALSE)
                        """
                    ),
                    {"identity_id": principals[0].identity_id},
                )
            await engine.dispose()

            password_correlation = uuid4()
            user_repository = SqlUserRepository(
                cast(
                    Any,
                    SimpleNamespace(database_url_for=lambda _role: database_url),
                )
            )
            with (
                audit_principal_scope(principals[0]),
                correlation_scope(password_correlation),
            ):
                await user_repository.change_local_password(
                    account_id=principals[0].account_id,
                    identity_id=principals[0].identity_id,
                    password_hash="new-audit-probe-hash",
                    actor=principals[0].login_name,
                    ip="127.0.0.1",
                )
            password_engine = create_async_engine(database_url, hide_parameters=True)
            async with password_engine.connect() as connection:
                password_row = (
                    await connection.execute(
                        text(
                            """
                            SELECT lc.password_hash,ua.security_version,
                              al.actor_account_id,al.actor_identity_id,al.correlation_id
                            FROM local_credential lc
                            JOIN auth_identity ai ON ai.id=lc.identity_id
                            JOIN user_account ua ON ua.id=ai.account_id
                            JOIN audit_log al ON al.object_id=ua.id::text
                              AND al.action='local_password_change'
                            WHERE lc.identity_id=:identity_id
                            ORDER BY al.id DESC LIMIT 1
                            """
                        ),
                        {"identity_id": principals[0].identity_id},
                    )
                ).mappings().one()
            await password_engine.dispose()
            if (
                password_row["password_hash"] != "new-audit-probe-hash"
                or int(password_row["security_version"]) != 2
                or int(password_row["actor_account_id"]) != principals[0].account_id
                or int(password_row["actor_identity_id"]) != principals[0].identity_id
                or password_row["correlation_id"] != password_correlation
            ):
                raise RuntimeError("normal local password change was not committed atomically")
            results = await asyncio.gather(
                repository.update_configs(
                    (ConfigUpdate("vendor_qps", "3"),),
                    principal=principals[0],
                    ip="127.0.0.1",
                ),
                repository.update_configs(
                    (ConfigUpdate("reserved_realtime_qps", "4"),),
                    principal=principals[1],
                    ip="127.0.0.1",
                ),
                return_exceptions=True,
            )
            successes = [item for item in results if not isinstance(item, BaseException)]
            failures = [item for item in results if isinstance(item, InvalidAdminQuery)]
            if len(successes) != 1 or len(failures) != 1:
                other_failures = sorted(
                    type(item).__name__
                    for item in results
                    if isinstance(item, BaseException)
                    and not isinstance(item, InvalidAdminQuery)
                )
                raise RuntimeError(
                    "concurrent config updates bypassed final policy gate: "
                    f"success={len(successes)} invalid={len(failures)} "
                    f"other={','.join(other_failures) or 'none'}"
                )
            try:
                await repository.update_configs(
                    (ConfigUpdate("callback_allow_cidrs", "0.0.0.0/0"),),
                    principal=principals[0],
                    ip="127.0.0.1",
                )
            except InvalidAdminQuery:
                pass
            else:
                raise RuntimeError("public callback CIDR bypassed transaction policy gate")

            audit_correlation = uuid4()
            audit_repository = SqlSensitiveWordRepository(
                cast(Any, SimpleNamespace(database_url=database_url))
            )
            with audit_principal_scope(principals[0]), correlation_scope(audit_correlation):
                created = await audit_repository.add_many(
                    ["migration-audit-attribution-probe"],
                    actor="display-only-must-not-win",
                )
            if not created.created:
                raise RuntimeError("audit attribution probe did not create a word")
            audit_engine = create_async_engine(database_url, hide_parameters=True)
            async with audit_engine.connect() as connection:
                attributed = (
                    await connection.execute(
                        text(
                            """
                            SELECT actor,actor_subject_kind,actor_account_id,
                              actor_identity_id,correlation_id
                            FROM audit_log
                            WHERE action='sensitive_word_add'
                              AND after_val->>'count'='1'
                            ORDER BY id DESC LIMIT 1
                            """
                        )
                    )
                ).mappings().one()
            if (
                attributed["actor"] != principals[0].login_name
                or attributed["actor_subject_kind"] != "human"
                or int(attributed["actor_account_id"]) != principals[0].account_id
                or int(attributed["actor_identity_id"]) != principals[0].identity_id
                or attributed["correlation_id"] != audit_correlation
            ):
                raise RuntimeError("live audit was not bound to the stable principal context")
            try:
                async with audit_engine.begin() as connection:
                    await connection.execute(
                        text(
                            """
                            INSERT INTO audit_log(actor,action,object_type,object_id)
                            VALUES('display-only','unattributed_probe','probe','1')
                            """
                        )
                    )
            except DBAPIError:
                pass
            else:
                raise RuntimeError("unattributed live audit event was accepted")
            await audit_engine.dispose()

        run_async_check(verify_concurrent_config_updates())
        final_qps = (
            docker_psql(
                container,
                "alembic_build",
                "SELECT max(value) FILTER (WHERE key='vendor_qps'),"
                "max(value) FILTER (WHERE key='reserved_realtime_qps') FROM sys_config",
            )
            .strip()
            .split("|")
        )
        if len(final_qps) != 2 or int(final_qps[1]) >= int(final_qps[0]):
            raise RuntimeError("concurrent config updates committed an invalid QPS policy")

        from sqlalchemy.engine import URL  # noqa: PLC0415

        from app.services.runtime_policy import (  # noqa: PLC0415
            SqlRuntimePolicyLoader,
            parse_private_callback_cidrs,
        )

        task_database_url = URL.create(
            "postgresql+asyncpg",
            username="sms_owner",
            password=OWNER_SECRET.read_text(encoding="utf-8").rstrip("\r\n"),
            host="127.0.0.1",
            port=port,
            database="alembic_build",
        )
        task_settings = cast(
            Any,
            SimpleNamespace(
                database_url=task_database_url,
                database_url_for=lambda _role: task_database_url,
                alert_smtp_allowed_host_set=frozenset({"smtp"}),
                environment="test",
                callback_ca_certs_file=None,
                callback_mtls_cert_file=None,
                callback_mtls_key_file=None,
                callback_egress_networks=parse_private_callback_cidrs(
                    "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7"
                ),
                callback_egress_port_set=frozenset({80, 443}),
            ),
        )
        task_loader = SqlRuntimePolicyLoader(task_settings)
        run_async_check(task_loader.load())
        run_async_check(task_loader.load())

        callback_task = cast(Any, importlib.import_module("app.tasks.callback_worker"))
        reconcile_task = cast(Any, importlib.import_module("app.tasks.reconcile"))

        class TestCrypto:
            @classmethod
            def from_settings(cls, _settings: Any) -> TestCrypto:
                return cls()

        callback_task.get_settings = lambda: task_settings
        callback_task.CryptoService = TestCrypto
        reconcile_task.get_settings = lambda: task_settings
        reconcile_task.CryptoService = TestCrypto
        for _attempt in range(2):
            if run_async_check(callback_task._deliver(-1)) != 0:
                raise RuntimeError("empty callback task unexpectedly delivered")
            run_async_check(reconcile_task._reconcile())

        message_created_at = "2026-07-01T15:59:59.123456Z"
        report_time = "2026-07-01T16:00:01.654321Z"
        expected = report_event_key(
            message_id=21,
            message_created_at=datetime.fromisoformat(message_created_at),
            custom_id="  custom-1  ",
            report_status=2,
            report_desc=" 失败 Δ  ",
            report_time=datetime.fromisoformat(report_time),
        )
        actual = docker_psql(
            container,
            "alembic_build",
            """
            WITH v AS (SELECT
              '21'::text message_id,
              '2026-07-01T15:59:59.123456Z'::text message_created_at,
              trim('  custom-1  ') custom_id,
              '2'::text report_status,
              ' 失败 Δ  '::text report_desc,
              '2026-07-01T16:00:01.654321Z'::text report_time
            ) SELECT encode(digest(
              octet_length(message_id)::text || ':' || message_id ||
              octet_length(message_created_at)::text || ':' || message_created_at ||
              octet_length(custom_id)::text || ':' || custom_id ||
              octet_length(report_status)::text || ':' || report_status ||
              octet_length(report_desc)::text || ':' || report_desc ||
              octet_length(report_time)::text || ':' || report_time,
              'sha256'),'hex') FROM v
            """,
        ).strip()
        if actual != expected:
            raise RuntimeError("Python/SQL callback event digest mismatch")

        legacy_env = dict(migration_env)
        legacy_env["DB_NAME"] = "legacy_build"
        run(
            [alembic, "-c", str(BACKEND / "alembic.ini"), "upgrade", "0011_chunk_retry_not_before"],
            cwd=BACKEND,
            env=legacy_env,
        )
        docker_psql(
            container,
            "legacy_build",
            """
            ALTER TABLE callback_task DROP CONSTRAINT chk_cb_message_refs;
            ALTER TABLE callback_task DROP COLUMN event_keys;
            DROP TABLE callback_report_event;
            ALTER TABLE callback_task ADD CONSTRAINT chk_cb_message_refs CHECK (
              cardinality(message_ids)=cardinality(message_times));
            INSERT INTO app(id,name,dept,api_key_hash,api_key_prefix,created_by)
              VALUES(1,'legacy','dept',repeat('a',64),'12345678','test');
            INSERT INTO sms_batch(id,batch_no,channel,app_id,dept,content,send_content_enc)
              VALUES(1,repeat('b',32),'api',1,'dept','masked','\\x01');
            INSERT INTO sms_chunk(id,batch_id,chunk_no,custom_id,vendor_task_id,phone_count)
              VALUES(1,1,1,repeat('c',32),'vendor-task-1',1);
            INSERT INTO sms_message(
              id,batch_id,chunk_id,phone_enc,phone_hmac,phone_mask,status,
              report_status,report_desc,report_time,created_at)
              VALUES(21,1,1,'\\x01',repeat('d',64),'138****8000','failed',
                2,'FAILED','2026-07-01T16:00:01.654321Z',
                '2026-07-01T15:59:59.123456Z');
            INSERT INTO callback_task(
              app_id,event,batch_id,message_ids,message_times,url,
              callback_secret_enc,callback_secret_key_version,signature_version)
              VALUES
                (1,'message.report',1,ARRAY[21],ARRAY['2026-07-01T15:59:59.123456Z'::timestamptz],
                  'http://callback',decode('0001','hex'),1,1),
                (1,'message.report',1,ARRAY[21],ARRAY['2026-07-01T15:59:59.123456Z'::timestamptz],
                  'http://callback',decode('0001','hex'),1,1);
            """,
        )
        ambiguous = run(
            [
                alembic,
                "-c",
                str(BACKEND / "alembic.ini"),
                "upgrade",
                "0012_callback_event_keys",
            ],
            cwd=BACKEND,
            env=legacy_env,
            check=False,
        )
        if ambiguous.returncode == 0 or "ambiguous legacy callback events" not in (
            ambiguous.stdout + ambiguous.stderr
        ):
            raise RuntimeError("ambiguous legacy callback events were not rejected")

        single_env = dict(migration_env)
        single_env["DB_NAME"] = "legacy_single_build"
        run(
            [
                alembic,
                "-c",
                str(BACKEND / "alembic.ini"),
                "upgrade",
                "0011_chunk_retry_not_before",
            ],
            cwd=BACKEND,
            env=single_env,
        )
        docker_psql(
            container,
            "legacy_single_build",
            """
            ALTER TABLE callback_task DROP CONSTRAINT chk_cb_message_refs;
            ALTER TABLE callback_task DROP COLUMN event_keys;
            DROP TABLE callback_report_event;
            ALTER TABLE callback_task ADD CONSTRAINT chk_cb_message_refs CHECK (
              cardinality(message_ids)=cardinality(message_times));
            INSERT INTO app(id,name,dept,api_key_hash,api_key_prefix,created_by)
              VALUES(1,'legacy-single','dept',repeat('a',64),'12345678','test');
            INSERT INTO sms_batch(id,batch_no,channel,app_id,dept,content,send_content_enc)
              VALUES(1,repeat('e',32),'api',1,'dept','masked','\\x01');
            INSERT INTO sms_chunk(id,batch_id,chunk_no,custom_id,vendor_task_id,phone_count)
              VALUES(1,1,1,'  custom-1  ','  vendor-task-1  ',1);
            INSERT INTO sms_message(
              id,batch_id,chunk_id,phone_enc,phone_hmac,phone_mask,status,
              report_status,report_desc,report_time,created_at)
              VALUES(21,1,1,'\\x01',repeat('f',64),'138****8000','failed',
                2,' 失败 Δ  ','2026-07-01T16:00:01.654321Z',
                '2026-07-01T15:59:59.123456Z');
            INSERT INTO callback_task(
              app_id,event,batch_id,message_ids,message_times,url,
              callback_secret_enc,callback_secret_key_version,signature_version)
              VALUES(1,'message.report',1,ARRAY[21],
                ARRAY['2026-07-01T15:59:59.123456Z'::timestamptz],
                'http://callback',decode('0001','hex'),1,1);
            """,
        )
        run(
            [
                alembic,
                "-c",
                str(BACKEND / "alembic.ini"),
                "upgrade",
                "0012_callback_event_keys",
            ],
            cwd=BACKEND,
            env=single_env,
        )
        migrated = (
            docker_psql(
                container,
                "legacy_single_build",
                """
            SELECT trim(event_keys[1]),
              to_char(message_times[1] AT TIME ZONE 'UTC',
                'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
              cardinality(message_ids),cardinality(message_times),cardinality(event_keys),
              e.message_status,e.report_desc,
              to_char(e.report_time AT TIME ZONE 'UTC',
                'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
            FROM callback_task t JOIN callback_report_event e
              ON e.event_key=t.event_keys[1]
            WHERE t.event='message.report'
            """,
            )
            .strip()
            .split("|")
        )
        if migrated != [
            expected,
            message_created_at,
            "1",
            "1",
            "1",
            "failed",
            " 失败 Δ  ",
            report_time,
        ]:
            raise RuntimeError("legacy callback event migration produced invalid refs")
        docker_psql(
            container,
            "legacy_single_build",
            """
            DO $$ BEGIN
              BEGIN
                UPDATE callback_task SET event_keys='{}'::char(64)[]
                WHERE event='message.report';
                RAISE EXCEPTION 'callback ref constraint did not reject mismatch';
              EXCEPTION WHEN check_violation THEN NULL;
              END;
            END $$
            """,
        )
        run(
            [
                alembic,
                "-c",
                str(BACKEND / "alembic.ini"),
                "downgrade",
                "0011_chunk_retry_not_before",
            ],
            cwd=BACKEND,
            env=single_env,
        )
        downgraded = docker_psql(
            container,
            "legacy_single_build",
            """
            SELECT (to_regclass('public.callback_report_event') IS NULL)::int,
              (NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema='public' AND table_name='callback_task'
                  AND column_name='event_keys'))::int
            """,
        ).strip()
        if downgraded != "1|1":
            raise RuntimeError("single callback event downgrade left normalized schema")
        run(
            [
                alembic,
                "-c",
                str(BACKEND / "alembic.ini"),
                "upgrade",
                "0012_callback_event_keys",
            ],
            cwd=BACKEND,
            env=single_env,
        )
        restored_key = docker_psql(
            container,
            "legacy_single_build",
            "SELECT trim(event_keys[1]) FROM callback_task WHERE event='message.report'",
        ).strip()
        if restored_key != expected:
            raise RuntimeError("single callback event reupgrade did not restore event key")
        docker_psql(
            container,
            "legacy_single_build",
            """
            INSERT INTO callback_report_event(
              event_key,batch_id,message_id,message_created_at,
              message_status,report_desc,report_time)
            VALUES
              (repeat('1',64),1,21,'2026-07-01T15:59:59.123456Z',
                'delivered','EVENT-A','2026-07-01T16:01:00Z'),
              (repeat('2',64),1,21,'2026-07-01T15:59:59.123456Z',
                'failed','EVENT-B','2026-07-01T16:02:00Z');
            INSERT INTO callback_task(
              app_id,event,batch_id,message_ids,message_times,event_keys,url,
              callback_secret_enc,callback_secret_key_version,signature_version)
            VALUES(1,'message.report',1,ARRAY[21,21],
              ARRAY['2026-07-01T15:59:59.123456Z'::timestamptz,
                    '2026-07-01T15:59:59.123456Z'::timestamptz],
              ARRAY[repeat('1',64)::char(64),repeat('2',64)::char(64)],
              'http://callback',decode('0001','hex'),1,1);
            UPDATE sms_message SET status='unknown',report_desc='CURRENT-C',
              report_time='2026-07-01T16:03:00Z'
            WHERE id=21 AND created_at='2026-07-01T15:59:59.123456Z';
            INSERT INTO callback_task(
              app_id,event,batch_id,url,
              callback_secret_enc,callback_secret_key_version,signature_version)
              SELECT 1,'batch.finished',1,'http://callback',
                decode('0001','hex'),1,1
              FROM generate_series(1,200);
            """,
        )
        snapshots = docker_psql(
            container,
            "legacy_single_build",
            """
            WITH refs AS (
              SELECT * FROM callback_task t
              CROSS JOIN LATERAL unnest(t.message_ids,t.message_times,t.event_keys)
                WITH ORDINALITY AS r(message_id,message_created_at,event_key,ord)
              WHERE cardinality(t.event_keys)=2
            )
            SELECT string_agg(e.message_status || ':' || e.report_desc,',' ORDER BY r.ord)
            FROM refs r JOIN sms_message m
              ON m.id=r.message_id AND m.created_at=r.message_created_at
            JOIN callback_report_event e ON e.event_key=r.event_key
            """,
        ).strip()
        if snapshots != "delivered:EVENT-A,failed:EVENT-B":
            raise RuntimeError("callback report snapshots were replaced by current message state")
        plan = docker_psql(
            container,
            "legacy_single_build",
            """
            SET enable_seqscan=off;
            EXPLAIN (COSTS OFF)
            SELECT id FROM callback_task
            WHERE event_keys @> ARRAY[repeat('1',64)::char(64)]
            """,
        )
        if "idx_cb_event_keys" not in plan or "Bitmap Index Scan" not in plan:
            raise RuntimeError("callback event key GIN index is not usable")
        unsafe_downgrade = run(
            [
                alembic,
                "-c",
                str(BACKEND / "alembic.ini"),
                "downgrade",
                "0011_chunk_retry_not_before",
            ],
            cwd=BACKEND,
            env=single_env,
            check=False,
        )
        unsafe_output = unsafe_downgrade.stdout + unsafe_downgrade.stderr
        if (
            unsafe_downgrade.returncode == 0
            or "callback event downgrade unsafe" not in unsafe_output
        ):
            raise RuntimeError("multi-event callback downgrade was not rejected")
        preserved = docker_psql(
            container,
            "legacy_single_build",
            """
            SELECT (to_regclass('public.callback_report_event') IS NOT NULL)::int,
              (EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema='public' AND table_name='callback_task'
                  AND column_name='event_keys'))::int
            """,
        ).strip()
        if preserved != "1|1":
            raise RuntimeError("failed callback downgrade damaged normalized schema")
        docker_psql(
            container,
            "legacy_single_build",
            """
            DROP INDEX uk_cb_batch_source_event;
            ALTER TABLE callback_task DROP CONSTRAINT fk_cb_source_report_event;
            ALTER TABLE callback_task DROP COLUMN source_report_event_key;
            ALTER TABLE sms_message DROP CONSTRAINT fk_message_report_event;
            ALTER TABLE sms_message DROP COLUMN report_event_key;
            ALTER TABLE sms_reply DROP CONSTRAINT fk_reply_event;
            DROP INDEX idx_reply_event;
            ALTER TABLE sms_reply DROP COLUMN event_key;
            ALTER TABLE unmatched_report
              DROP CONSTRAINT fk_unmatched_report_event;
            ALTER TABLE unmatched_report
              DROP CONSTRAINT uk_unmatched_report_event;
            ALTER TABLE unmatched_report DROP COLUMN event_key;
            DROP TABLE report_event_projection;
            DROP TABLE reply_event;
            DROP TABLE report_event;

            INSERT INTO sms_reply(
              vendor_task_id,batch_id,phone_enc,phone_hmac,phone_mask,
              key_version,ext_code,content,reply_time,created_at
            ) VALUES(
              'legacy-reply-task',1,'\\x02',repeat('a',64),'138****8000',
              1,'01','TD','2026-07-01T16:04:00Z','2026-07-01T16:04:00Z'
            );
            INSERT INTO unmatched_report(
              vendor_task_id,custom_id,phone_enc,phone_hmac,phone_mask,
              key_version,report_status,report_desc,report_time,created_at
            ) VALUES(
              'legacy-unmatched-task','legacy-unmatched-custom',
              '\\x03',repeat('b',64),'139****9000',1,2,'FAILED',
              '2026-07-01T16:05:00Z','2026-07-01T16:05:00Z'
            );
            """,
        )
        run(
            [alembic, "-c", str(BACKEND / "alembic.ini"), "upgrade", "head"],
            cwd=BACKEND,
            env=single_env,
        )
        vendor_event_backfill = docker_psql(
            container,
            "legacy_single_build",
            """
            SELECT
              (SELECT count(*) FROM report_event
               WHERE raw_id IS NULL)::int,
              (SELECT count(*) FROM report_event_projection
               WHERE projection_changed)::int,
              (SELECT count(*) FROM reply_event
               WHERE raw_id IS NULL)::int,
              (SELECT count(*) FROM sms_reply
               WHERE event_key IS NOT NULL)::int,
              (SELECT count(*) FROM unmatched_report
               WHERE event_key IS NOT NULL)::int
            """,
        ).strip()
        if vendor_event_backfill != "2|1|1|1|1":
            raise RuntimeError("legacy vendor event facts were not safely backfilled")
    finally:
        run(["docker", "rm", "-f", container], check=False)


def main() -> int:
    try:
        run_check()
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        print(f"迁移一致性检查失败: {detail}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"迁移一致性检查失败: {exc}", file=sys.stderr)
        return 1
    print("迁移一致性检查通过: tables/columns/indexes/constraints")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
