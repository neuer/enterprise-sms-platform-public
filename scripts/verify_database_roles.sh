#!/usr/bin/env bash
# 在已启动的测试 Compose 中验证数据库职责隔离；只使用合成空操作。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose() {
  SMS_PLATFORM_ROOT="$ROOT" \
  SMS_SECRETS_MODE=development \
  SMS_RUNTIME_ROOT="${SMS_RUNTIME_ROOT:-${TMPDIR:-/tmp}/sms-platform-${UID}/secrets}" \
  COMPOSE_PROFILES=dev \
    "$ROOT/deploy/sms-compose" "$@"
}

result="$(
  compose exec -T postgres psql -U sms_owner -d sms -v ON_ERROR_STOP=1 -Atc "
    WITH runtime(role_name) AS (
      VALUES
        ('sms_auth'),('sms_accept'),('sms_send'),('sms_callback'),
        ('sms_export'),('sms_scheduler'),('sms_metrics')
    )
    SELECT
      (SELECT count(*)=7 AND bool_and(
         rolcanlogin AND NOT rolsuper AND NOT rolcreatedb
         AND NOT rolcreaterole AND NOT rolreplication AND NOT rolinherit
       ) FROM runtime JOIN pg_roles ON rolname=role_name),
      (SELECT bool_and(
         NOT has_database_privilege(role_name,current_database(),'TEMP')
       ) FROM runtime),
      (SELECT bool_and(
         has_table_privilege(role_name,'audit_log','INSERT')
         AND NOT has_table_privilege(role_name,'audit_log','UPDATE')
         AND NOT has_table_privilege(role_name,'audit_log','DELETE')
         AND NOT has_table_privilege(role_name,'audit_log','TRUNCATE')
       ) FROM runtime WHERE role_name <> 'sms_metrics'),
      (NOT has_table_privilege('sms_metrics','audit_log','INSERT')),
      (NOT has_table_privilege('sms_callback','user_account','UPDATE')),
      (NOT has_table_privilege('sms_callback','auth_provider','UPDATE')),
      (NOT has_table_privilege('sms_callback','app','UPDATE')),
      (NOT has_table_privilege('sms_callback','sys_config','UPDATE')),
      (has_table_privilege('sms_auth','password_change_token','SELECT')
       AND has_table_privilege('sms_auth','password_change_token','INSERT')
       AND has_table_privilege('sms_auth','password_change_token','UPDATE')
       AND NOT has_table_privilege('sms_auth','password_change_token','DELETE')
       AND NOT has_table_privilege('sms_auth','password_change_token','TRUNCATE')
      AND has_sequence_privilege(
         'sms_auth','password_change_token_id_seq','USAGE')),
      has_table_privilege('sms_callback','callback_task','UPDATE'),
      (has_table_privilege('sms_send','callback_task','INSERT')
       AND has_table_privilege('sms_send','callback_task','UPDATE')
       AND has_table_privilege('sms_send','callback_report_event','INSERT')
       AND has_sequence_privilege('sms_send','callback_task_id_seq','USAGE')),
      (has_table_privilege('sms_send','user_account','SELECT')
       AND has_table_privilege('sms_send','import_task','SELECT')
       AND has_table_privilege('sms_send','import_task','UPDATE')
       AND has_table_privilege('sms_send','import_task','DELETE')
       AND NOT has_table_privilege('sms_send','import_task','INSERT')
       AND has_table_privilege('sms_send','import_phone','SELECT')
       AND has_table_privilege('sms_send','import_phone','INSERT')
       AND has_table_privilege('sms_send','import_phone','DELETE')
       AND NOT has_table_privilege('sms_send','import_phone','UPDATE')
       AND has_table_privilege('sms_send','idempotency_record','DELETE')
       AND has_table_privilege('sms_send','callback_task','DELETE')
       AND has_table_privilege('sms_send','callback_report_event','DELETE')
       AND has_sequence_privilege('sms_send','import_phone_id_seq','USAGE')),
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
       )),
      (NOT EXISTS (
        SELECT 1 FROM information_schema.role_table_grants
        WHERE grantee='sms_metrics' AND privilege_type <> 'SELECT'
      )),
      (has_table_privilege('sms_accept','alembic_version','SELECT')
       AND has_table_privilege('sms_accept','callback_task','INSERT')
       AND has_table_privilege('sms_accept','alert_log','INSERT')
       AND has_sequence_privilege('sms_accept','alert_log_id_seq','USAGE')),
      (has_table_privilege('sms_callback','job_run','SELECT')
       AND has_table_privilege('sms_callback','job_run','INSERT')
       AND has_table_privilege('sms_callback','job_run','UPDATE')
       AND NOT has_table_privilege('sms_callback','job_run','DELETE')
       AND NOT has_table_privilege('sms_callback','job_run','TRUNCATE')),
      (SELECT NOT rolcanlogin FROM pg_roles WHERE rolname='sms_app'),
      (NOT EXISTS (
        SELECT 1 FROM information_schema.role_table_grants
        WHERE grantee='sms_app'
      ));
  "
)"
  [ "$result" = "t|t|t|t|t|t|t|t|t|t|t|t|t|t|t|t|t|t" ] || {
  echo "数据库运行角色矩阵异常: $result" >&2
  exit 1
}

if compose exec -T postgres psql -U sms_owner -d sms -v ON_ERROR_STOP=1 \
  -c "SET ROLE sms_callback; UPDATE user_account SET display_name=display_name" \
  >/dev/null 2>&1; then
  echo "sms_callback 不得修改账号" >&2
  exit 1
fi
if compose exec -T postgres psql -U sms_owner -d sms -v ON_ERROR_STOP=1 \
  -c "SET ROLE sms_metrics; INSERT INTO job_run(job_name,status) VALUES('forbidden','running')" \
  >/dev/null 2>&1; then
  echo "sms_metrics 不得写入业务表" >&2
  exit 1
fi
compose exec -T postgres psql -U sms_owner -d sms -v ON_ERROR_STOP=1 \
  -c "SET ROLE sms_metrics; SELECT
    (SELECT count(status) FROM sms_chunk)
    +(SELECT count(lease_id) FROM callback_task)
    +(SELECT count(lease_id) FROM export_task)
    +(SELECT count(event_type) FROM worker_lease_event)
    +(SELECT count(job_name) FROM job_run)
    +(SELECT count(kind) FROM usage_projection_drift)
    +(SELECT count(category) FROM sms_batch)" \
  >/dev/null
if compose exec -T postgres psql -U sms_owner -d sms -v ON_ERROR_STOP=1 \
  -c "SET ROLE sms_metrics; SELECT batch_no FROM sms_batch LIMIT 1" \
  >/dev/null 2>&1; then
  echo "sms_metrics 不得读取批次号等非聚合列" >&2
  exit 1
fi

compose exec -T postgres psql -U sms_owner -d sms -v ON_ERROR_STOP=1 >/dev/null <<'SQL'
CREATE TABLE role_matrix_future_probe(id bigint PRIMARY KEY);
DO $verify$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.role_table_grants
    WHERE table_schema='public'
      AND table_name='role_matrix_future_probe'
      AND grantee IN (
        'sms_auth','sms_accept','sms_send','sms_callback',
        'sms_export','sms_scheduler','sms_metrics','sms_app'
      )
  ) THEN
    RAISE EXCEPTION 'future table inherited runtime privileges';
  END IF;
END
$verify$;
DROP TABLE role_matrix_future_probe;
SQL

echo "数据库角色矩阵验证通过：七职责隔离、审计不可篡改、未来表默认拒绝"
