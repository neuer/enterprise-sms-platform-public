#!/bin/sh
# 首次 initdb 只创建无登录运行角色，使 schema grant 可执行。
# LOGIN 与独立密码由 migrate 前的 provision-db-roles 服务从 Docker secrets 设置。
set -eu

psql -v ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" <<'SQL'
DO $roles$
DECLARE
  role_name text;
BEGIN
  FOREACH role_name IN ARRAY ARRAY[
    'sms_auth',
    'sms_accept',
    'sms_send',
    'sms_callback',
    'sms_export',
    'sms_scheduler',
    'sms_metrics',
    -- 兼容历史迁移中的显式 GRANT；0034 会永久撤权并保持 NOLOGIN。
    'sms_app'
  ]
  LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=role_name) THEN
      EXECUTE format(
        'CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT',
        role_name
      );
    END IF;
  END LOOP;
END
$roles$;
SQL
